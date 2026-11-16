from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from freelance_pipeline.http import (
    DEFAULT_USER_AGENT,
    HTTPClient,
    SourceError,
    SourceHTTPError,
)
from freelance_pipeline.models import JobLead
from freelance_pipeline.urls import canonicalize_url

# Jan's stage 5b (2026-09-21): the Polish board was the bottleneck, not the
# filter, so Useme is collected from more than one category. „Programowanie i IT”
# stays first (it is where the web/app/integration commissions live), and the
# three office-work lanes below are where repetitive, automatable work is
# posted — the material the ZAMIEN axis reads. Analytics is the fourth: reports,
# tracking and data integrations. Every path keeps the same newest-first order
# and the same page budget, and the adapter dedupes ids across categories.
PRODUCTION_CATEGORY_PATHS: tuple[str, ...] = (
    "/pl/jobs/category/programowanie-i-it,35",
    "/pl/jobs/category/prace-biurowe,40/wprowadzanie-danych,132",
    "/pl/jobs/category/prace-biurowe,40/customer-support,133",
    "/pl/jobs/category/prace-biurowe,40/bazy-danych,130",
    "/pl/jobs/category/marketing,37/analityka-internetowa,115",
)

# Only links shaped exactly like an offer page are jobs:
# ``/pl/jobs/<slug>,<id>/`` with a single slug segment and no further path.
# Category URLs (``/pl/jobs/category/…``), subcategories
# (``/pl/jobs/category/…,35/software,100/``) and navigation links
# (``/pl/jobs/``, ``/pl/jobs/new/``) never match, so category ids such as the
# ``35`` in ``/pl/jobs/category/programowanie-i-it,35/`` can never leak in as
# job ids the way the legacy broad selector+regex allowed.
JOB_PATH_RE = re.compile(r"^/pl/jobs/(?P<slug>[^/,]+),(?P<id>\d+)/?$")

# Legacy scrape_page skipped completed offers and offers published a month or
# more ago by scanning the whole card text for these markers.
COMPLETED_MARKERS = ("zakończone", "zakończona", "zakończony")
# Legacy fetch_detail used these full phrases on the offer detail page.
COMPLETED_PHRASES = ("składanie ofert zostało zakończone", "zlecenie zakończone")
STALE_MARKERS = ("miesięcy temu", "miesiąc temu", "miesiące temu", "rok temu", "lat temu")

_EXPIRES_RE = re.compile(r"znika za\s+(\d+)\s+(?:dni|dzień)", re.IGNORECASE)

# Polish relative-time units: minut, godzin(ę/y), dni, dzień, tygodni(e), tydzień.
_PUBLISHED_RE = re.compile(
    r"(\d+)\s+(minut|godzin[ęy]?|dni|dzień|tygodni[ea]?|tydzień)\s+temu",
    re.IGNORECASE,
)
_PUBLISHED_UNIT_SECONDS = {
    "minut": 60,
    "godzin": 3600,
    "dni": 86400,
    "tydzień": 7 * 86400,
}

_BUDGET_RE = re.compile(
    r"(?P<amount>\d[\d\s\u00a0.,]*(?:[.,]\d+)?)\s*(?P<currency>PLN|zł|EUR|USD|€)",
    re.IGNORECASE,
)
_CURRENCY_ALIASES = {
    "pln": "PLN",
    "zł": "PLN",
    "eur": "EUR",
    "€": "EUR",
    "usd": "USD",
}


def classify_job_href(href: Any) -> tuple[str, str] | None:
    """Return ``(source_id, url_path)`` for a real Useme offer link, else ``None``.

    The classification is based purely on the URL structure: an offer link is
    ``/pl/jobs/<slug>,<id>/`` where the slug is a single path segment. Links to
    category pages, subcategories, the category root or the "new offer" form
    have a different shape and are rejected, so category ids can never be
    mistaken for job ids.
    """
    if not isinstance(href, str):
        return None
    href = href.strip()
    if not href:
        return None
    try:
        path = urlsplit(href).path
    except ValueError:
        path = href
    match = JOB_PATH_RE.match(path)
    if match is None:
        return None
    return match.group("id"), path


def parse_budget(text: Any) -> tuple[float | None, str | None]:
    """Parse a Useme budget string into ``(amount, currency)``.

    Accepts Polish number formatting (``"1 234,56 PLN"``), common currency
    tokens (PLN/zł, EUR/€, USD) and maps "Do negocjacji" (negotiable) to
    ``(None, None)``. Unparseable input also yields ``(None, None)``.
    """
    if not isinstance(text, str):
        return None, None
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None, None
    match = _BUDGET_RE.search(text)
    if match is None:
        return None, None
    amount = match.group("amount").replace("\u00a0", "").replace(" ", "")
    if "," in amount and "." in amount:
        if amount.rfind(",") > amount.rfind("."):
            amount = amount.replace(".", "").replace(",", ".")
        else:
            amount = amount.replace(",", "").replace(".", ".")
    else:
        amount = amount.replace(",", ".")
    try:
        value = float(amount)
    except ValueError:
        return None, None
    if value < 0:
        return None, None
    currency = _CURRENCY_ALIASES.get(match.group("currency").lower())
    return value, currency


def parse_published_pl(text: Any, now: datetime | None = None) -> datetime | None:
    """Parse a Polish relative "Opublikowano" value into a UTC-aware datetime.

    Only sub-month ages are converted; month/year-old offers are considered
    stale and are skipped before this helper is ever consulted, so they yield
    ``None`` here.
    """
    if not isinstance(text, str):
        return None
    value = text.strip().lower()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        pass
    if now is None:
        now = datetime.now(UTC)
    if value.startswith("przed chwil"):
        return now
    if value in ("dziś", "dzis", "dzisiaj"):
        return now
    if value == "wczoraj":
        return now - timedelta(days=1)
    match = _PUBLISHED_RE.search(value)
    if match is None:
        return None
    count = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("godzin"):
        unit = "godzin"
    elif unit == "dzień":
        unit = "dni"
    elif unit.startswith("tygodni") or unit == "tydzień":
        unit = "tydzień"
    seconds = _PUBLISHED_UNIT_SECONDS.get(unit)
    if seconds is None:
        return None
    return now - timedelta(seconds=count * seconds)


class UsemeError(SourceError):
    """Raised when the Useme jobs source cannot be collected."""


class UsemeJobs:
    """Specialist adapter for the public Useme jobs board (server-rendered HTML).

    By default the newest offers from the Polish "Programowanie i IT" category
    are collected across pages 1-2 (``?order_by=-published_on``). ``max_pages``
    must be a plain ``int`` >= 1 and is safely capped at 5; pagination stops
    early as soon as a page yields zero offer cards.

    Offer links are distinguished from category/navigation links by their URL
    shape: only anchors inside ``article.job`` cards whose href matches
    ``/pl/jobs/<slug>,<id>/`` are treated as jobs. Category links such as
    ``/pl/jobs/category/programowanie-i-it,35/`` never qualify, so category ids
    can never masquerade as job ids.

    Offers that are already completed or were published a month or more ago are
    skipped (legacy policy). Summary data comes from the listing; when
    ``fetch_details`` is enabled each offer's detail page is fetched for the
    full description, authoritative budget and the published-at timestamp, and
    a detail page that marks the offer as completed or a month+ old drops it.
    """

    source = "useme"
    base_url = "https://useme.com"
    # Single-category default: an adapter built without options collects exactly
    # one lane (the IT board), which is what the offline fixtures and the adapter
    # tests rely on. The production registry passes
    # :data:`PRODUCTION_CATEGORY_PATHS` explicitly.
    category_path = "/pl/jobs/category/programowanie-i-it,35"
    order_by = "-published_on"
    language = "pl"
    max_pages = 2
    _MAX_PAGES_CAP = 5

    def __init__(
        self,
        *,
        base_url: str | None = None,
        category_path: str | None = None,
        category_paths: Sequence[str] | None = None,
        session: Any | None = None,
        timeout: float = 15.0,
        max_pages: int | None = None,
        user_agent: str | None = None,
        fetch_details: bool = False,
    ) -> None:
        if not isinstance(fetch_details, bool):
            raise TypeError(
                f"fetch_details must be a bool, got {type(fetch_details).__name__}"
            )
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
        self.max_pages = self._coerce_max_pages(
            max_pages if max_pages is not None else self.max_pages
        )
        self.fetch_details = fetch_details
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
        return value.strip().rstrip("/")

    @classmethod
    def _coerce_max_pages(cls, value: Any) -> int:
        """Validate ``max_pages`` as a plain int in ``[1, _MAX_PAGES_CAP]``."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"max_pages must be a plain int, got {type(value).__name__}")
        if value < 1:
            raise ValueError(f"max_pages must be >= 1, got {value}")
        return min(value, cls._MAX_PAGES_CAP)

    def _page_url(self, category: str, page: int) -> str:
        url = f"{self.base_url}{category}/?order_by={self.order_by}"
        if page > 0:
            url = f"{url}&page={page + 1}"
        return url

    def collect(self) -> list[JobLead]:
        """Collect every configured category, newest first, without duplicates.

        ``max_pages`` is the page budget *per category*; the loop stops early as
        soon as a page yields no offer cards. Ids are deduplicated across
        categories, so an offer that sits in two lanes is collected once, and the
        first lane that lists it decides its ``raw_data['category_path']``.
        """
        leads: list[JobLead] = []
        seen: set[str] = set()
        for category in self.category_paths:
            for page in range(self.max_pages):
                url = self._page_url(category, page)
                try:
                    html = self._http.get(url).text
                except SourceHTTPError as exc:
                    raise UsemeError(str(exc)) from exc
                try:
                    soup = BeautifulSoup(html, "lxml")
                except Exception as exc:
                    raise UsemeError(f"[{self.source}] could not parse HTML: {exc}") from exc
                cards = self._cards_from_soup(soup)
                if page == 0 and not cards and not self._is_valid_listing_shell(soup):
                    raise UsemeError(f"[{self.source}] invalid listing page structure")
                if not cards:
                    break
                for card, source_id, path in cards:
                    if source_id in seen:
                        continue
                    lead = self._card_to_lead(card, source_id, path, category=category)
                    if lead is None:
                        continue
                    if self.fetch_details:
                        lead = self._enrich_with_detail(lead)
                        if lead is None:
                            continue
                    seen.add(source_id)
                    leads.append(lead)
        return leads

    def _cards_from_soup(self, soup: Any) -> list[tuple[Any, str, str]]:
        """Return ``(card, source_id, url_path)`` for every genuine offer card."""
        cards: list[tuple[Any, str, str]] = []
        for card in soup.select("article.job"):
            link = card.select_one("a.job__title")
            if link is None:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            parsed = classify_job_href(href)
            if parsed is None:
                continue
            source_id, path = parsed
            cards.append((card, source_id, path))
        return cards

    @staticmethod
    def _is_valid_listing_shell(soup: Any) -> bool:
        """Distinguish a genuine empty jobs page from a block/interstitial page."""
        return bool(
            soup.select_one("a[href^='/pl/jobs/category/']")
            or soup.select_one("article.job")
        )

    def _card_to_lead(
        self, card: Any, source_id: str, path: str, *, category: str | None = None
    ) -> JobLead | None:
        link = card.select_one("a.job__title")
        title = self._clean(link.get_text(" ", strip=True)) if link is not None else ""
        if not title:
            return None
        card_text = self._clean(card.get_text(" ", strip=True))
        if self._has_completed(card_text) or self._is_stale(card_text):
            return None

        description = ""
        desc_el = card.select_one(".job__content p")
        if desc_el is not None:
            description = self._clean(desc_el.get_text(" ", strip=True))

        budget_text = ""
        budget_el = card.select_one(".job__budget-value")
        if budget_el is not None:
            budget_text = self._clean(budget_el.get_text(" ", strip=True))
        budget, currency = parse_budget(budget_text)

        canonical = canonicalize_url(f"{self.base_url}{path}")
        return JobLead(
            source=self.source,
            source_id=source_id,
            url=canonical,
            canonical_url=canonical,
            title=title,
            description=description,
            company_or_author=self._card_author(card),
            budget=budget,
            currency=currency,
            language=self.language,
            skills=self._card_skills(card),
            raw_data={
                "title": title,
                "url": canonical,
                "category_path": category or self.category_path,
                "slug": JOB_PATH_RE.match(path).group("slug"),
                "budget_text": budget_text or None,
                "expires_in_days": self._expires_in_days(card_text),
                "offers_count": self._offers_count(card),
                "category": self._card_category(card),
            },
        )

    def _enrich_with_detail(self, lead: JobLead) -> JobLead | None:
        """Fetch one offer's detail page; return ``None`` to drop the lead."""
        try:
            html = self._http.get(lead.url).text
        except SourceHTTPError:
            return lead
        detail = self._parse_detail(html)
        if detail.get("completed"):
            return None
        published_text = detail.get("published_text")
        if published_text and self._is_stale(published_text):
            return None
        description = detail.get("description") or lead.description
        budget, currency = parse_budget(detail.get("budget_text") or "")
        if budget is None:
            budget, currency = lead.budget, lead.currency
        published_at = lead.published_at
        if published_text:
            published_at = parse_published_pl(published_text) or published_at
        raw = dict(lead.raw_data.to_dict()) if lead.raw_data is not None else {}
        raw["published_text"] = published_text
        raw["description"] = description
        return replace(
            lead,
            description=description,
            budget=budget,
            currency=currency,
            published_at=published_at,
            raw_data=raw,
        )

    def _parse_detail(self, html: str) -> dict[str, Any]:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001 - tolerance on optional enrichment
            return {}
        text = self._detail_text(soup)
        return {
            "completed": self._has_completed_phrase(text),
            "published_text": self._summary_item_value(soup, "Opublikowano"),
            "budget_text": self._summary_item_value(soup, "Budżet"),
            "description": self._detail_description(soup),
        }

    @staticmethod
    def _detail_text(soup: Any) -> str:
        root = soup.select_one("main")
        if root is None:
            root = soup
        for tag in root.find_all(["script", "style", "noscript", "nav", "header", "footer"]):
            tag.decompose()
        return UsemeJobs._clean(root.get_text(" ", strip=True))

    @staticmethod
    def _detail_description(soup: Any) -> str | None:
        el = soup.select_one(".jobs-summary__item-text")
        if el is None:
            return None
        value = UsemeJobs._clean(el.get_text(" ", strip=True))
        return value or None

    @staticmethod
    def _summary_item_value(soup: Any, label: str) -> str | None:
        for item in soup.select(".jobs-summary__item"):
            label_el = item.select_one(".jobs-summary__item-label")
            if label_el is None:
                continue
            if UsemeJobs._clean(label_el.get_text(" ", strip=True)) != label:
                continue
            value_el = item.select_one(".jobs-summary__item-value")
            if value_el is None:
                return None
            value = UsemeJobs._clean(value_el.get_text(" ", strip=True))
            return value or None
        return None

    @staticmethod
    def _card_author(card: Any) -> str | None:
        strong = card.select_one(".job__headline strong[aria-label]")
        if strong is None:
            strong = card.select_one(".job__headline strong")
        if strong is None:
            return None
        value = UsemeJobs._clean(strong.get_text(" ", strip=True))
        return value or None

    @staticmethod
    def _card_skills(card: Any) -> tuple[str, ...]:
        skills: list[str] = []
        for tag in card.select(".job__skills a.tag"):
            value = UsemeJobs._clean(tag.get_text(" ", strip=True))
            if value:
                skills.append(value)
        return tuple(skills)

    @staticmethod
    def _card_category(card: Any) -> str | None:
        el = card.select_one(".job__category a p")
        if el is None:
            return None
        value = UsemeJobs._clean(el.get_text(" ", strip=True))
        return value or None

    @staticmethod
    def _expires_in_days(text: str) -> int | None:
        match = _EXPIRES_RE.search(text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _offers_count(card: Any) -> int | None:
        el = card.select_one(".job__header-details--offers")
        if el is None:
            return None
        match = re.search(r"\d+", UsemeJobs._clean(el.get_text(" ", strip=True)))
        return int(match.group()) if match else None

    @staticmethod
    def _has_completed(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in COMPLETED_MARKERS)

    @staticmethod
    def _has_completed_phrase(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in COMPLETED_PHRASES)

    @staticmethod
    def _is_stale(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in STALE_MARKERS)

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()


__all__ = [
    "PRODUCTION_CATEGORY_PATHS",
    "UsemeError",
    "UsemeJobs",
    "classify_job_href",
    "parse_budget",
    "parse_published_pl",
]