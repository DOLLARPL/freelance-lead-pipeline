"""Oferteo public list adapter (read-only radar, no paid contact reveal).

Oferteo is a pay-per-lead board: the seller's wizytówka is free, but every client
contact is unlocked with **points** (1 point ≈ 6–10.36 zł netto, a 25-point
package ≈ 259 zł). Two hard rules follow from that and are encoded here:

1. The adapter only ever reads the **public** category lists (``/zlecenia…``).
   ``robots.txt`` disallows ``/rfp/`` and ``/rfp/remote/uncover.ofer`` — the
   contact-reveal endpoint — so this module never builds a request to them and
   never logs in. Nothing is purchased automatically: a lead is a *radar ping*,
   not an unlocked contact.
2. Because the public list hides the description and masks the phone number,
   the only text available is the zapytanie **title** plus its metadata (city,
   buyer type, deadline, publication date). That is enough to decide whether a
   contact is worth paying for, and it is honest about what it does not know, so
   ``raw_data['contact_requires_points']`` is set on every lead and the chat row
   is rendered with an explicit paid-contact line.

Anonymous visitors get a registration dialog instead of a detail page, so the
public list itself has no clickable per-offer link. The portal does have one:
the dialog's login button points at ``/konto/zapytanie/<rfp-id>_X``, the request
page behind the free Wykonawca login where the hidden description is readable.
The adapter never opens it — it only records the address
(:data:`OFERTEO_OFFER_URL_TEMPLATE`) and keeps the category list as a second link,
so a report row can be opened exactly instead of searched for by hand.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from freelance_pipeline.http import (
    DEFAULT_USER_AGENT,
    HTTPClient,
    SourceError,
    SourceHTTPError,
    SourceParseError,
)
from freelance_pipeline.models import JobLead

# Lanes that actually carry Jan's offer. Jan's verdict 2026-09-25: the links in
# the report must be as narrow as the portal allows, because he was searching the
# exact zapytanie by hand inside a broad category. ``/zlecenia-it`` stays only
# because integrations and outsourcing live there; its service titles (laptop
# repair, home networks, data recovery, 3D printing) are removed
# deterministically in scoring.py, not by dropping the lane.
# ``/zlecenia-na-audyt`` and ``/zlecenia-na-szkolenia`` are deliberately absent:
# live sampling showed tax advice, business plans, BHP and first-aid courses.
OFERTEO_PRODUCTION_CATEGORY_PATHS: tuple[str, ...] = (
    "/zlecenia-na-tworzenie-stron-internetowych",
    "/zlecenia-na-sklepy-internetowe",
    "/zlecenia-na-obsluge-stron-internetowych",
    "/zlecenia-e-marketingowe",
    "/zlecenia-it",
)

# The portal's own per-zapytanie address, taken from the register dialog the
# public list opens on click (``/konto/zapytanie/<rfp-id>_X``). It is not a
# paid reveal — it is the request's page, behind the free Wykonawca login, and
# that is where the description the public list hides actually lives. Jan only
# has a category link otherwise, so every row carries this one too.
OFERTEO_OFFER_URL_TEMPLATE = "{base_url}/konto/zapytanie/{rfp_id}_X"

# Every visible card is a zapytanie; the container is the only structural
# invariant we require, so a redesign raises instead of returning zero leads.
_LIST_MARKER = "rfp-list"
_CARD_CLASS = "rfp-list-item"

_TITLE_SELECTOR = '[data-test="rfp_list__item__title"]'
_FIRST_NAME_SELECTOR = '[data-test="rfp_list__item__first_name"]'
_DATE_SELECTOR = '[data-test="rfp_list__item__date"]'
_CITY_SELECTOR = '[data-test="rfp_list__item__location_city"]'

_NON_DIGITS_RE = re.compile(r"[^0-9]")

# The buyer type is a closed vocabulary on the public list ("osoba prywatna"
# / "firma"); it is the first plain-text sibling in the tag row. Anything else
# in that slot is treated as layout noise, not as a lead field.
_BUYER_HINTS = ("osoba prywatna", "firma")


class OferteoError(SourceError):
    """Raised when the public Oferteo list cannot be collected or parsed."""


def _card_text(card: Any, selector: str) -> str:
    node = card.select_one(selector)
    return node.get_text(" ", strip=True) if node is not None else ""


def _tag_row_meta(card: Any) -> tuple[str | None, str | None]:
    """Return ``(buyer_type, deadline)`` from the card's tag row.

    The row holds the city (its own ``data-test`` span) plus two plain-text
    siblings: the buyer type and the deadline. The deadline is free-form
    ("możliwe od zaraz", "w ciągu miesiąca", "orientacyjna data: 04.01.2027"),
    so it is read **positionally** after the city is skipped, never matched
    against a hint list.
    """
    row = card.select_one(".rfp-tile_tags")
    if row is None:
        return None, None
    texts = [
        child.get_text(" ", strip=True)
        for child in row.find_all("div", recursive=False)
        if not child.select_one("[data-test]")
    ]
    texts = [text for text in texts if text]
    buyer = texts[0] if texts and texts[0].lower() in _BUYER_HINTS else None
    if buyer is None:
        return None, texts[0] if texts else None
    return buyer, texts[1] if len(texts) > 1 else None


def _published_at(card: Any) -> datetime | None:
    node = card.select_one(_DATE_SELECTOR)
    if node is None:
        return None
    raw = (node.get("title") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _card_to_lead(
    card: Any,
    *,
    base_url: str,
    category_path: str,
) -> JobLead | None:
    """Convert one ``div.rfp-list-item`` into a lead, or ``None`` if unusable."""
    raw_id = card.get("data-rfp-id")
    if not isinstance(raw_id, str) or not _NON_DIGITS_RE.sub("", raw_id):
        return None
    source_id = _NON_DIGITS_RE.sub("", raw_id)
    title = _card_text(card, _TITLE_SELECTOR)
    if not title:
        return None

    buyer_type, deadline = _tag_row_meta(card)
    contact_block = card.select_one(".rfp-tile_contact")
    contact_html = contact_block.get_text(" ", strip=True) if contact_block is not None else ""
    verified = "potwierdz" in contact_html.lower()
    offer_url = OFERTEO_OFFER_URL_TEMPLATE.format(base_url=base_url, rfp_id=source_id)
    listing_url = f"{base_url}{category_path}"

    description_parts = [f"Zapytanie z kategorii {category_path}."]
    if buyer_type:
        description_parts.append(f"Zlecający: {buyer_type}.")
    if deadline:
        description_parts.append(f"Termin: {deadline}.")
    description_parts.append(
        "Publiczna lista pokazuje tylko tytuł — treść zapytania widać po "
        "zalogowaniu na Oferteo, pod adresem zapytania."
    )
    description_parts.append(
        "Kontakt płatny: odblokowanie danych zlecającego wymaga punktów Oferteo, "
        "adapter nie odkrywa kontaktu automatycznie."
    )

    return JobLead(
        source="oferteo",
        source_id=source_id,
        # The exact request, not the category list: that is the link Jan needs so
        # he does not have to find the zapytanie by hand inside a broad lane.
        url=offer_url,
        title=title,
        description=" ".join(description_parts),
        company_or_author=_card_text(card, _FIRST_NAME_SELECTOR) or None,
        published_at=_published_at(card),
        location=_card_text(card, _CITY_SELECTOR) or None,
        language="pl",
        raw_data={
            "contact_requires_points": True,
            # The public list always masks the client's phone and hides the
            # description; the value is stored so a report row can say so
            # without the adapter ever touching the paid reveal endpoint.
            "contact_masked": True,
            "buyer_verified": verified,
            "buyer_type": buyer_type,
            "deadline": deadline,
            "category_path": category_path,
            "category_id": card.get("data-cat-id"),
            "rfp_id": source_id,
            "offer_url": offer_url,
            "listing_url": listing_url,
        },
    )


def parse_list_page(
    html: str,
    *,
    base_url: str,
    category_path: str,
) -> list[JobLead]:
    """Parse one public Oferteo listing page into leads (pure, offline-testable).

    Raises :class:`OferteoError` when the page carries no list container at all,
    so a layout change or a bot wall is reported instead of silently producing
    zero leads. An empty container is a legitimate empty result.
    """
    if not isinstance(html, str) or _LIST_MARKER not in html:
        raise OferteoError("[oferteo] listing page structure changed or blocked")
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as exc:  # noqa: BLE001 - any parser failure is a parse error
        raise OferteoError(f"[oferteo] could not parse HTML: {exc}") from exc

    leads: list[JobLead] = []
    for card in soup.select(f"div.{_CARD_CLASS}"):
        lead = _card_to_lead(card, base_url=base_url, category_path=category_path)
        if lead is not None:
            leads.append(lead)
    return leads


class OferteoJobs:
    """Specialist adapter for the public Oferteo category lists.

    Reads one page per configured category path (the lists are newest-first and
    paginated client-side, so there is nothing to walk) and deduplicates ids
    across lanes: the same zapytanie is listed in several categories at once.
    """

    source = "oferteo"
    base_url = "https://www.oferteo.pl"
    # Single-lane default (the IT lane), like every other adapter: offline
    # fixtures and adapter tests rely on it, the registry passes the production
    # lane set explicitly through ACTIVE_SOURCE_KWARGS.
    category_path = "/zlecenia-it"
    language = "pl"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        category_path: str | None = None,
        category_paths: Sequence[str] | None = None,
        session: Any | None = None,
        timeout: float = 15.0,
        user_agent: str | None = None,
    ) -> None:
        if category_path is not None and category_paths is not None:
            raise ValueError("pass either category_path or category_paths, not both")
        if category_paths is not None:
            paths = tuple(self._coerce_category_path(path) for path in category_paths)
            if not paths:
                raise ValueError("category_paths must contain at least one path")
        else:
            paths = (self._coerce_category_path(category_path or self.category_path),)
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.category_paths = paths
        self.category_path = paths[0]
        self.timeout = timeout
        self._http = HTTPClient(
            session=session,
            timeout=timeout,
            user_agent=user_agent or DEFAULT_USER_AGENT,
            source=self.source,
        )

    @staticmethod
    def _coerce_category_path(value: Any) -> str:
        if not isinstance(value, str) or not value.strip().startswith("/"):
            raise ValueError("category_path must be an absolute path starting with '/'")
        path = value.strip().rstrip("/")
        # Guard rail for rule 1 above: the contact-reveal endpoints are off
        # limits (robots.txt), so they can never be configured as a lane.
        if "/rfp/" in path or path.endswith("/rfp"):
            raise ValueError("Oferteo /rfp/ paths are robots-disallowed and never requested")
        return path

    def collect(self) -> list[JobLead]:
        """Collect every configured lane once, without duplicate zapytania."""
        leads: list[JobLead] = []
        seen: set[str] = set()
        for category_path in self.category_paths:
            url = f"{self.base_url}{category_path}"
            try:
                html = self._http.get(url).text
            except SourceHTTPError as exc:
                raise OferteoError(str(exc)) from exc
            except SourceParseError as exc:  # pragma: no cover - defensive
                raise OferteoError(str(exc)) from exc
            for lead in parse_list_page(
                html, base_url=self.base_url, category_path=category_path
            ):
                if lead.source_id in seen:
                    continue
                seen.add(lead.source_id)
                leads.append(lead)
        return leads


__all__ = [
    "OFERTEO_OFFER_URL_TEMPLATE",
    "OFERTEO_PRODUCTION_CATEGORY_PATHS",
    "OferteoError",
    "OferteoJobs",
    "parse_list_page",
]
