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

# Only ``/oferta-pracy/<slug>`` is an offer page. Category and navigation links
# live under the plural ``/oferty-pracy/...`` (e.g.
# ``/oferty-pracy/wszystkie-lokalizacje/sales``) and the API under ``/api/``,
# none of which match, so a category slug can never leak in as an offer id.
OFFER_PATH_RE = re.compile(r"^/oferta-pracy/(?P<slug>[a-z0-9-]+)/?$")

WORK_MODES = ("Stacjonarnie", "Zdalnie", "Hybrydowo")
# Values rendered in the contract/schedule block that are schedules, not
# contract types.
SCHEDULE_VALUES = ("Pełny etat", "Niepełny etat", "Dowolna", "Freelance")

BUDGET_RE = re.compile(
    r"(?P<amount>\d[\d\u00a0 ]*(?:[.,]\d+)?)(?:\s*-\s*(?P<amount_hi>\d[\d\u00a0 ]*(?:[.,]\d+)?))?"
)
BADGE_RE = re.compile(r"Nowa|Wygasa za \d+ dni")
LOCATION_COUNT_RE = re.compile(r"\+(\d+)\s+Lokalizacj")


class RocketJobsError(SourceError):
    """Raised when the public RocketJobs listing cannot be collected."""


class RocketJobsJobs:
    """Read the server-rendered public RocketJobs offers page.

    robots.txt allows the bare ``/oferty-pracy`` listing page while disallowing
    comma filter URLs (``/oferty-pracy/*,*``), ``/api/`` and internal asset
    paths. Collection therefore issues exactly one GET to ``/oferty-pracy``
    with no query pagination; the server renders the first page of offer cards
    into the HTML. Each card links to ``/oferta-pracy/<slug>``; since the
    listing exposes no separate public id, the canonical offer slug is used as
    the stable ``source_id``. Cards whose href is not an offer URL (category,
    navigation, API) are skipped, as are absolute or protocol-relative hrefs
    whose normalized hostname is not ``rocketjobs.pl`` (``www.`` is
    canonicalized away), duplicates are dropped, and a page that does
    not even carry the listing shell (e.g. a bot block or an empty document)
    is treated as malformed rather than a valid empty listing.
    """

    source = "rocketjobs"
    base_url = "https://rocketjobs.pl/oferty-pracy"
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
            raise ValueError("RocketJobs max_pages must be 1")
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
            raise RocketJobsError(str(exc)) from exc
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception as exc:
            raise RocketJobsError(f"[{self.source}] could not parse HTML: {exc}") from exc
        if not self._is_valid_listing_shell(soup):
            raise RocketJobsError(f"[{self.source}] invalid listing page structure")

        leads: list[JobLead] = []
        seen: set[str] = set()
        for card in soup.select("a.offer-card"):
            lead = self._card_to_lead(card)
            if lead is None or lead.source_id in seen:
                continue
            seen.add(lead.source_id)
            leads.append(lead)
        return leads

    @staticmethod
    def _is_valid_listing_shell(soup: Any) -> bool:
        """A genuine listing has offer cards or at least the category/nav UI.

        A valid-but-empty listing still renders the category and navigation
        links; a bot-block/interstitial page or a bare document does not.
        """
        return bool(
            soup.select_one("a.offer-card")
            or soup.select_one("a.offer_list_category_link")
        )

    def _card_to_lead(self, card: Any) -> JobLead | None:
        li = card.parent
        while li is not None and li.name != "li":
            li = li.parent
        if li is None:
            return None
        href = (card.get("href") or "").strip()
        slug = self._slug_from_url(href)
        if slug is None:
            return None
        title_link = li.select_one(".offer_list_offer_title_link")
        title = (
            self._clean(title_link.get_text(" ", strip=True))
            if title_link is not None
            else ""
        )
        if not title:
            return None

        canonical = canonicalize_url(self._absolute_url(href))
        contract_type, schedule = self._contracts(li)
        budget, currency = self._salary(li)
        raw = {
            "slug": slug,
            "url": canonical,
            "title": title,
            "salary_text": self._salary_text(li),
            "currency": currency,
            "seniority": self._seniority(li),
            "schedule": schedule,
            "badge": self._badge(li),
            "location_count": self._location_count(li),
        }
        return JobLead(
            source=self.source,
            source_id=slug,
            url=canonical,
            canonical_url=canonical,
            title=title,
            company_or_author=self._company(li),
            budget=budget,
            currency=currency,
            contract_type=contract_type,
            work_mode=self._work_mode(li),
            location=self._location(li),
            skills=self._skills(li),
            status="active",
            raw_data=raw,
        )

    @staticmethod
    def _canonical_host(host: str | None) -> str | None:
        """Normalize a hostname: lowercase, strip trailing dot, drop the www prefix."""
        if not host:
            return None
        host = host.lower().rstrip(".")
        if host == "www.rocketjobs.pl":
            return "rocketjobs.pl"
        return host

    @staticmethod
    def _slug_from_url(href: str) -> str | None:
        if not href:
            return None
        try:
            parts = urlsplit(href)
        except ValueError:
            return None
        if (parts.scheme or parts.netloc) and (
            RocketJobsJobs._canonical_host(parts.hostname) != "rocketjobs.pl"
        ):
            return None
        match = OFFER_PATH_RE.match(parts.path)
        return match.group("slug") if match is not None else None

    def _absolute_url(self, href: str) -> str:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            host = self._canonical_host(parts.hostname)
            if host is None or host != "rocketjobs.pl":
                return href
            return urlunsplit(("https", host, parts.path, parts.query, ""))
        base = urlsplit(self.base_url)
        return urlunsplit((base.scheme, base.netloc, href, "", ""))

    @staticmethod
    def _company(li: Any) -> str | None:
        img = li.find("img")
        alt = (img.get("alt") or "").strip() if img is not None else ""
        if alt:
            return alt
        p = li.select_one("p.mui-i7a9we")
        return RocketJobsJobs._clean(p.get_text(" ", strip=True)) if p is not None else None

    @staticmethod
    def _work_mode(li: Any) -> str | None:
        p = li.select_one("p.mui-yn0q9l")
        if p is not None:
            value = RocketJobsJobs._clean(p.get_text(" ", strip=True))
            if value:
                return value
        text = RocketJobsJobs._clean(li.get_text(" ", strip=True))
        return next((mode for mode in WORK_MODES if mode in text), None)

    @staticmethod
    def _location(li: Any) -> str | None:
        p = li.select_one("p.mui-gtp4go")
        if p is not None:
            value = RocketJobsJobs._clean(p.get_text(" ", strip=True))
            if value:
                return value
        div = li.select_one("div.mui-vvropy")
        if div is not None:
            value = RocketJobsJobs._clean(div.get_text(" ", strip=True))
            if value:
                return value
        return None

    @staticmethod
    def _salary(li: Any) -> tuple[float | None, str | None]:
        amount_el = li.select_one("span.mui-16ziixw")
        if amount_el is None:
            return None, None
        amount_text = RocketJobsJobs._clean(amount_el.get_text(" ", strip=True))
        match = BUDGET_RE.match(amount_text)
        if match is None:
            return None, None
        budget = RocketJobsJobs._parse_amount(match.group("amount"))
        if budget is None:
            return None, None
        currency = None
        currency_el = li.select_one("span.mui-428puh")
        if currency_el is not None:
            currency_text = RocketJobsJobs._clean(currency_el.get_text(" ", strip=True))
            currency = currency_text.split("/", 1)[0].strip().upper() or None
        return budget, currency

    @staticmethod
    def _salary_text(li: Any) -> str | None:
        hidden = li.select_one("span.mui-qinz4u")
        if hidden is not None:
            value = RocketJobsJobs._clean(hidden.get_text(" ", strip=True))
            if value:
                return value
        amount_el = li.select_one("span.mui-16ziixw")
        if amount_el is None:
            return None
        parts = [RocketJobsJobs._clean(amount_el.get_text(" ", strip=True))]
        currency_el = li.select_one("span.mui-428puh")
        if currency_el is not None:
            parts.append(RocketJobsJobs._clean(currency_el.get_text(" ", strip=True)))
        value = " ".join(parts)
        return value or None

    @staticmethod
    def _skills(li: Any) -> tuple[str, ...]:
        skills: list[str] = []
        for el in li.select("div.mui-15a2h75"):
            value = RocketJobsJobs._clean(el.get_text(" ", strip=True))
            if value:
                skills.append(value)
        return tuple(dict.fromkeys(skills))

    @staticmethod
    def _contracts(li: Any) -> tuple[str | None, str | None]:
        labels: list[str] = []
        schedule: str | None = None
        for span in li.select("span.mui-1rupd51"):
            value = RocketJobsJobs._clean(span.get_text(" ", strip=True))
            if not value:
                continue
            if value in SCHEDULE_VALUES:
                schedule = value
            else:
                labels.append(value)
        contract_type = ", ".join(dict.fromkeys(labels)) or None
        return contract_type, schedule

    @staticmethod
    def _seniority(li: Any) -> str | None:
        span = li.select_one("span.mui-7uoh14")
        if span is None:
            return None
        value = RocketJobsJobs._clean(span.get_text(" ", strip=True))
        return value or None

    @staticmethod
    def _badge(li: Any) -> str | None:
        match = BADGE_RE.search(RocketJobsJobs._clean(li.get_text(" ", strip=True)))
        return match.group(0) if match is not None else None

    @staticmethod
    def _location_count(li: Any) -> int | None:
        match = LOCATION_COUNT_RE.search(RocketJobsJobs._clean(li.get_text(" ", strip=True)))
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _parse_amount(value: str) -> float | None:
        if not value:
            return None
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


__all__ = ["RocketJobsError", "RocketJobsJobs"]