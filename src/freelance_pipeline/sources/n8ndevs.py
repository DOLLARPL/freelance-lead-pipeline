from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from freelance_pipeline.http import (
    DEFAULT_USER_AGENT,
    HTTPClient,
    SourceError,
    SourceHTTPError,
)
from freelance_pipeline.models import JobLead
from freelance_pipeline.urls import canonicalize_url


class N8NDevsError(SourceError):
    """Raised when the n8ndevs job feed source cannot be collected."""


@dataclass(frozen=True, slots=True)
class Rate:
    """A parsed budget pill: amount range, unit and currency."""

    minimum: float
    maximum: float
    unit: str  # "hour" or "project"
    currency: str


# The feed prints a budget pill next to every title: "$19–40 / hour",
# "$15 / hour", "$250 fixed", "€75–150 / project". The rate decides whether a
# lead is worth Jan's time (his rule: ~$15/h is a joke, ~$15–20/h is marginal),
# so it must reach the scorer instead of being dropped with the markup.
_CURRENCY_BY_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP"}
_RATE_RANGE_RE = re.compile(
    r"([$€£])\s*(\d+(?:[.,]\d+)?)\s*(?:[–—-]|\bto\b)\s*[$€£]?\s*(\d+(?:[.,]\d+)?)"
    r"\s*(?:/|\bper\b)?\s*(hour|hr|project)?",
    re.IGNORECASE,
)
_RATE_SINGLE_RE = re.compile(
    r"([$€£])\s*(\d+(?:[.,]\d+)?)\s*(?:/|\bper\b)?\s*(hour|hr|project|fixed)?",
    re.IGNORECASE,
)


def parse_rate(text: str) -> Rate | None:
    """Parse one n8ndevs budget pill into a :class:`Rate`.

    Returns ``None`` when the text carries no recognisable amount, so a missing
    or malformed pill can never invent a rate.
    """
    if not text:
        return None
    ranged = _RATE_RANGE_RE.search(text)
    if ranged is not None:
        symbol, low, high, unit_raw = ranged.groups()
        minimum = _to_float(low)
        maximum = _to_float(high) if high else minimum
    else:
        single = _RATE_SINGLE_RE.search(text)
        if single is None:
            return None
        symbol, low, unit_raw = single.groups()
        minimum = maximum = _to_float(low)
    unit = (unit_raw or "").lower()
    if unit in {"hour", "hr"}:
        unit = "hour"
    else:
        unit = "project"
    return Rate(minimum=minimum, maximum=maximum, unit=unit, currency=_CURRENCY_BY_SYMBOL[symbol])


def _to_float(value: str) -> float:
    return float(value.replace(",", "."))


class N8NDevsJobs:
    """Specialist adapter for the public n8ndevs job feed (server-rendered HTML).

    ``n8ndevs.com/robots.txt`` allows the plain ``/jobs`` path but disallows all
    query strings (``Disallow: *?*``), so this adapter fetches exactly one page
    and refuses query-string pagination.
    """

    source = "n8ndevs"
    base_url = "https://n8ndevs.com/jobs"
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
        if max_pages is not None and max_pages != 1:
            raise ValueError(
                "n8ndevs robots.txt disallows query-string pagination (Disallow: *?*); "
                "max_pages must be 1"
            )
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
            raise N8NDevsError(str(exc)) from exc
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception as exc:
            raise N8NDevsError(f"[{self.source}] could not parse HTML: {exc}") from exc
        leads: list[JobLead] = []
        seen: set[str] = set()
        for card in soup.select(".job-list .grid > div"):
            lead = self._card_to_lead(card)
            if lead is None or lead.source_id in seen:
                continue
            seen.add(lead.source_id)
            leads.append(lead)
        return leads

    def _card_to_lead(self, card: Any) -> JobLead | None:
        link = card.select_one("a[href*='/jobs/']")
        if link is None:
            return None
        href = (link.get("href") or "").strip()
        if not href:
            return None
        source_id = href.rstrip("/").rsplit("/", 1)[-1]
        title = self._clean(link.get_text(" ", strip=True))
        if not source_id or not title:
            return None

        description = ""
        desc_el = card.select_one(".job-description")
        if desc_el is not None:
            description = self._clean(desc_el.get_text(" ", strip=True))

        skills = tuple(
            self._clean(span.get_text(" ", strip=True))
            for span in card.select("div.flex.flex-wrap.gap-1.mt-3.mb-3 > span")
            if span.get_text(" ", strip=True)
        )

        contract_type = None
        type_el = card.select_one("div.mb-3.text-sm.uppercase.font-medium.text-gray-600")
        if type_el is not None:
            contract_type = self._clean(type_el.get_text(" ", strip=True)) or None

        rate = None
        for span in card.select("span"):
            candidate = parse_rate(self._clean(span.get_text(" ", strip=True)))
            if candidate is not None:
                rate = candidate
                break

        canonical = canonicalize_url(href)
        return JobLead(
            source=self.source,
            source_id=source_id,
            url=canonical,
            canonical_url=canonical,
            title=title,
            description=description,
            contract_type=contract_type,
            skills=skills,
            budget=rate.minimum if rate else None,
            currency=rate.currency if rate else None,
            raw_data={
                "title": title,
                "url": href,
                "skills": list(skills),
                "contract_type": contract_type,
                **(
                    {
                        "rate_min": rate.minimum,
                        "rate_max": rate.maximum,
                        "rate_unit": rate.unit,
                    }
                    if rate
                    else {}
                ),
            },
        )

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()