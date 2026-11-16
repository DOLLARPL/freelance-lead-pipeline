from __future__ import annotations

import json
from datetime import UTC, datetime
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

from ._common import parse_iso


class JustJoinITError(SourceError):
    """Raised when the public Just Join IT listing cannot be collected."""


class JustJoinITJobs:
    """Read the server-rendered public Just Join IT offers page.

    The website's robots.txt allows ``/job-offers/all-locations`` while both
    ``/api/`` on the website and the separate API host are disallowed. The
    listing embeds the first 100 offers in Next.js Flight data, so collection
    deliberately uses one robots-allowed HTML request and no query pagination.
    """

    source = "justjoinit"
    base_url = "https://justjoin.it/job-offers/all-locations"
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
            raise ValueError("Just Join IT max_pages must be 1")
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
            offers = self._extract_offers(html)
        except SourceHTTPError as exc:
            raise JustJoinITError(str(exc)) from exc
        except JustJoinITError:
            raise
        except Exception as exc:
            raise JustJoinITError(f"[{self.source}] could not parse offers payload: {exc}") from exc

        leads: list[JobLead] = []
        seen: set[str] = set()
        for offer in offers:
            lead = self._offer_to_lead(offer)
            if lead is None or lead.source_id in seen:
                continue
            seen.add(lead.source_id)
            leads.append(lead)
        return leads

    def _extract_offers(self, html: str) -> list[Any]:
        soup = BeautifulSoup(html, "lxml")
        found_flight_data = False
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            prefix = "self.__next_f.push("
            if not text.startswith(prefix) or not text.endswith(")"):
                continue
            found_flight_data = True
            try:
                frame = json.loads(text[len(prefix) : -1])
            except json.JSONDecodeError:
                continue
            if not (
                isinstance(frame, list)
                and len(frame) >= 2
                and isinstance(frame[1], str)
            ):
                continue
            offers = self._offers_from_flight_text(frame[1])
            if offers is not None:
                return offers
        detail = "offers payload not found" if found_flight_data else "Next.js offers payload not found"
        raise JustJoinITError(f"[{self.source}] parse error: {detail}")

    def _offers_from_flight_text(self, text: str) -> list[Any] | None:
        decoder = json.JSONDecoder()
        cursor = 0
        saw_pages = False
        while True:
            start = text.find('{"pages"', cursor)
            if start < 0:
                break
            saw_pages = True
            cursor = start + 1
            try:
                payload, end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            cursor = start + end
            pages = payload.get("pages")
            if not isinstance(pages, list):
                raise JustJoinITError(f"[{self.source}] parse error: pages must be a list")
            if not pages:
                return []
            first_page = pages[0]
            if not isinstance(first_page, dict):
                raise JustJoinITError(f"[{self.source}] parse error: page must be an object")
            data = first_page.get("data")
            if not isinstance(data, list):
                raise JustJoinITError(f"[{self.source}] parse error: page data must be a list")
            return data
        if saw_pages:
            raise JustJoinITError(f"[{self.source}] parse error: malformed offers payload")
        return None

    def _offer_to_lead(self, offer: Any) -> JobLead | None:
        if not isinstance(offer, dict) or offer.get("displayOffer") is False:
            return None
        expired_at = parse_iso(offer.get("expiredAt"))
        if expired_at is not None and expired_at <= datetime.now(UTC):
            return None
        source_id = offer.get("guid")
        slug = offer.get("slug")
        title = offer.get("title")
        if not all(isinstance(value, str) and value.strip() for value in (source_id, slug, title)):
            return None

        original_contracts = self._original_contracts(offer.get("employmentTypes"))
        contract_type = ", ".join(dict.fromkeys(item["type"] for item in original_contracts)) or None
        salary = next(
            (
                item.get("fromPerUnit", item.get("from"))
                for item in original_contracts
                if self._is_number(item.get("fromPerUnit", item.get("from")))
            ),
            None,
        )
        currency = next(
            (
                item.get("currency")
                for item in original_contracts
                if isinstance(item.get("currency"), str) and item["currency"].strip()
            ),
            None,
        )
        skills = self._strings(offer.get("requiredSkills")) + self._strings(
            offer.get("niceToHaveSkills")
        )
        city = offer.get("city") if isinstance(offer.get("city"), str) else ""
        street = offer.get("street") if isinstance(offer.get("street"), str) else ""
        location = ", ".join(part.strip() for part in (city, street) if part.strip()) or None
        url = canonicalize_url(f"https://justjoin.it/job-offer/{slug.strip()}")

        return JobLead(
            source=self.source,
            source_id=source_id.strip(),
            url=url,
            canonical_url=url,
            title=title.strip(),
            description=str(offer.get("body") or "").strip(),
            company_or_author=self._optional_text(offer.get("companyName")),
            published_at=parse_iso(offer.get("publishedAt")),
            budget=float(salary) if salary is not None else None,
            currency=currency,
            contract_type=contract_type,
            work_mode=self._optional_text(offer.get("workplaceType")),
            location=location,
            skills=tuple(dict.fromkeys(skills)),
            status="active",
            raw_data=self._json_safe_offer(offer),
        )

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _is_number(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0

    @staticmethod
    def _original_contracts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [
            item
            for item in value
            if isinstance(item, dict)
            and item.get("currencySource") == "original"
            and isinstance(item.get("type"), str)
            and item["type"].strip()
        ]

    @staticmethod
    def _json_safe_offer(offer: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in offer.items()
            if value != "$undefined"
        }
