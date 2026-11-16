from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from freelance_pipeline.http import (
    DEFAULT_USER_AGENT,
    HTTPClient,
    SourceError,
    SourceHTTPError,
)
from freelance_pipeline.models import JobLead
from freelance_pipeline.urls import canonicalize_url

# Only ``/<locale>?/job/<slug>`` is an offer page. Category/city/navigation
# links live under other paths (e.g. ``/backend``, ``/companies``) and the
# disallowed ``/api/``, ``/posting/``, ``/pdf/``, ``*/job/job/*`` and
# ``/signal`` routes never match, so a navigation or posting-management path
# can never be mistaken for an offer slug.
OFFER_PATH_RE = re.compile(
    r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?job/(?P<slug>[a-z0-9-]+)/?$"
)

SALARY_RANGE_RE = re.compile(
    r"(?P<lo>\d[\d\u00a0 ]*(?:[.,]\d+)?)\s*[-–—~]\s*(?P<hi>\d[\d\u00a0 ]*(?:[.,]\d+)?)"
)
SALARY_AMOUNT_RE = re.compile(r"(?P<amount>\d[\d\u00a0 ]*(?:[.,]\d+)?)")
CURRENCY_RE = re.compile(r"\b(?P<cur>PLN|EUR|USD|GBP|CZK|HUF|UAH)\b")
# Text rendered in a card's application-status slot when the posting is not
# takeable anymore; never matches text in the live, active listing.
CLOSED_MARKERS = (
    "closed",
    "zakończon",
    "zamknięt",
    "unavailable",
    "niedostępn",
    "expired",
    "wygaśnięto",
    "przeterminowano",
    "removed",
    "usunięto",
    "ended",
    "zarchiwizowano",
)


class NoFluffJobsError(SourceError):
    """Raised when the public No Fluff Jobs listing cannot be collected."""


class NoFluffJobsJobs:
    """Read the server-rendered public No Fluff Jobs offers page.

    robots.txt allows the bare ``/`` listing while disallowing ``/api/``,
    ``/posting/`` and locale posting routes, ``/pdf/``, ``/signal`` and the
    ``*/job/job/*`` pattern. Collection therefore issues exactly one GET to
    ``https://nofluffjobs.com`` with no query pagination; the server renders
    the first page of offer cards into the SSR HTML. Each card links to the
    canonical ``/job/<slug>`` offer URL; the canonical slug is used as the
    stable ``source_id`` (the ``id`` attribute on cards alternates between a
    short public id and a slug-derived one, so it is not stable). Cards whose
    href is not a same-host ``/job/<slug>`` URL (category, company, posting or
    navigation pages), cards without a title, cross-host hrefs and records
    whose application-status marks them closed/unavailable are skipped,
    duplicates are dropped, and a page that does not even carry the listing
    shell (e.g. a bot block or an empty document) is treated as malformed
    rather than as a valid empty listing.
    """

    source = "nofluffjobs"
    base_url = "https://nofluffjobs.com"
    max_pages = 1

    def __init__(
        self,
        *,
        base_url: str | None = None,
        session: Any | None = None,
        timeout: float = 15.0,
        max_pages: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.timeout = timeout
        if max_pages is not None and (
            isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages != 1
        ):
            raise ValueError("No Fluff Jobs max_pages must be 1")
        self.max_pages = 1
        self._http = HTTPClient(
            session=session,
            timeout=timeout,
            user_agent=user_agent or DEFAULT_USER_AGENT,
            source=self.source,
        )

    def collect(self) -> list[JobLead]:
        try:
            html = self._http.get(self.base_url).text
        except SourceHTTPError as exc:
            raise NoFluffJobsError(str(exc)) from exc
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception as exc:
            raise NoFluffJobsError(f"[{self.source}] could not parse HTML: {exc}") from exc
        if not self._is_valid_listing_shell(soup):
            raise NoFluffJobsError(f"[{self.source}] invalid listing page structure")

        leads: list[JobLead] = []
        seen: set[str] = set()
        for card in soup.select("a.posting-list-item"):
            lead = self._card_to_lead(card)
            if lead is None or lead.source_id in seen:
                continue
            seen.add(lead.source_id)
            leads.append(lead)
        return leads

    @staticmethod
    def _is_valid_listing_shell(soup: Any) -> bool:
        """A genuine listing carries offer cards or at least the search shell.

        A valid-but-empty listing still renders the ``nfj-postings-search``
        component (search box + filters); a bot-block/interstitial page or a
        bare document does not.
        """
        return bool(
            soup.select_one("a.posting-list-item") or soup.select_one("nfj-postings-search")
        )

    def _card_to_lead(self, card: Any) -> JobLead | None:
        href = (card.get("href") or "").strip()
        slug = self._slug_from_url(href)
        if slug is None:
            return None
        title = self._title(card)
        if not title:
            return None
        if self._is_closed(card):
            return None

        canonical = canonicalize_url(self._absolute_url(href))
        salary_el = card.select_one("nfj-posting-item-salary .posting-tag")
        salary_text = self._clean(
            salary_el.get_text(" ", strip=True) if salary_el is not None else ""
        )
        budget, salary_to, currency = self._salary(salary_text)
        tiles = card.select("nfj-posting-item-tiles > .posting-tag")
        skills = tuple(
            dict.fromkeys(
                value for el in tiles if (value := self._clean(el.get_text(" ", strip=True)))
            )
        )
        raw = {
            "slug": slug,
            "url": canonical,
            "salary_text": salary_text or None,
            "salary_to": salary_to,
            "currency": currency,
            "category": next(iter(skills), None),
            "requirements": skills[1:],
        }
        return JobLead(
            source=self.source,
            source_id=slug,
            url=canonical,
            canonical_url=canonical,
            title=title,
            company_or_author=self._company(card),
            budget=budget,
            currency=currency,
            location=self._location(card),
            skills=tuple(skills),
            status="active",
            raw_data=raw,
        )

    @staticmethod
    def _title(card: Any) -> str:
        title_el = card.select_one(
            '[data-cy="title position on the job offer listing"], h3.posting-title__position'
        )
        if title_el is None:
            return ""
        for badge in title_el.select(".title-badge"):
            badge.decompose()
        return NoFluffJobsJobs._clean(title_el.get_text(" ", strip=True))

    @staticmethod
    def _company(card: Any) -> str | None:
        el = card.select_one(".company-name")
        value = NoFluffJobsJobs._clean(el.get_text(" ", strip=True)) if el is not None else ""
        return value or None

    @staticmethod
    def _location(card: Any) -> str | None:
        loc = card.select_one("nfj-posting-item-city")
        if loc is None:
            return None
        for span in loc.find_all("span"):
            value = NoFluffJobsJobs._clean(span.get_text(" ", strip=True))
            if value:
                return value
        return None

    @staticmethod
    def _is_closed(card: Any) -> bool:
        status = card.select_one("nfj-posting-application-status")
        if status is None:
            return False
        text = NoFluffJobsJobs._clean(status.get_text(" ", strip=True)).lower()
        return bool(text) and any(marker in text for marker in CLOSED_MARKERS)

    @staticmethod
    def _canonical_host(host: str | None) -> str | None:
        """Normalize a hostname: lowercase, strip trailing dot, drop the www prefix."""
        if not host:
            return None
        host = host.lower().rstrip(".")
        if host == "www.nofluffjobs.com":
            return "nofluffjobs.com"
        return host

    @classmethod
    def _slug_from_url(cls, href: str) -> str | None:
        if not href:
            return None
        try:
            parts = urlsplit(href)
        except ValueError:
            return None
        if parts.scheme or parts.netloc:
            try:
                port = parts.port
            except ValueError:
                return None
            if parts.scheme != "https":
                return None
            if parts.username is not None or parts.password is not None:
                return None
            if port is not None and port != 443:
                return None
            if cls._canonical_host(parts.hostname) != "nofluffjobs.com":
                return None
        match = OFFER_PATH_RE.match(parts.path)
        return match.group("slug") if match is not None else None

    def _absolute_url(self, href: str) -> str:
        """Rebuild the locale-independent canonical offer URL.

        The SSR page renders offer links under the current locale prefix
        (``/job/<slug>`` or ``/pl/job/<slug>`` / ``/hu/job/...``). Both are the
        same offer, so the emitted URL always drops the locale segment:
        ``https://nofluffjobs.com/job/<slug>``.
        """
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            host = self._canonical_host(parts.hostname)
            if host is None or host != "nofluffjobs.com":
                return href
        match = OFFER_PATH_RE.match(parts.path)
        if match is None:
            return urlunsplit(("https", "nofluffjobs.com", parts.path, parts.query, ""))
        return f"https://nofluffjobs.com/job/{match.group('slug')}"

    @staticmethod
    def _salary(text: str) -> tuple[float | None, float | None, str | None]:
        currency = None
        match = CURRENCY_RE.search(text)
        if match is not None:
            currency = match.group("cur")
        match = SALARY_RANGE_RE.search(text)
        if match is not None:
            lo = NoFluffJobsJobs._parse_amount(match.group("lo"))
            hi = NoFluffJobsJobs._parse_amount(match.group("hi"))
            if lo is not None:
                return lo, hi, currency
        match = SALARY_AMOUNT_RE.search(text)
        if match is not None:
            amount = NoFluffJobsJobs._parse_amount(match.group("amount"))
            if amount is not None:
                return amount, None, currency
        return None, None, currency

    @staticmethod
    def _parse_amount(value: str) -> float | None:
        amount = value.replace("\u00a0", "").replace(" ", "")
        if "," in amount:
            amount = amount.replace(",", ".")
        try:
            return float(amount)
        except ValueError:
            return None

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()


__all__ = ["NoFluffJobsError", "NoFluffJobsJobs"]