"""Live read-only smoke collection for the specialist community adapters.

Run from the repo root (network required, read-only GETs only):

    uv run python scripts/smoke_live.py

Each adapter is instantiated with its production defaults and asked to
``collect()`` exactly once, wrapped in a wall-clock timeout so a hung network
can never hang the smoke. Collected leads are validated against the pipeline's
acceptance rules and any violation fails the run:

* duplicate ``source_id`` values within one source;
* invalid required fields (``source``/``source_id``/``url`` empty or missing);
* records the exclusion policy says must never be emitted: pinned/about
  category metadata topics, closed or archived topics;
* Useme leads whose URL is a category/navigation link or whose ``source_id``
  is not numeric (the legacy broad selector+regex confused category ids such
  as ``35`` in ``/pl/jobs/category/programowanie-i-it,35/`` with job ids);
* RocketJobs leads whose URL is not a canonical offer URL
  (``/oferta-pracy/<slug>``), hits the disallowed ``/api/`` path, or whose
  ``source_id`` does not equal the offer slug taken from that URL;
* No Fluff Jobs leads whose URL is not a canonical offer URL
  (``https://nofluffjobs.com/job/<slug>``), hits a disallowed path
  (``/api/``, ``/posting/``, ``/pdf/``, ``/signal``), or whose
  ``source_id`` does not equal the offer slug taken from that URL;
* RocketJobs aggregate enrichment coverage: a non-empty live batch must keep
  ``company_or_author``, ``work_mode``, ``location`` and ``skills`` populated
  on at least half of its leads and expose at least one budget/currency pair,
  so wholesale MUI selector drift fails loudly instead of silently degrading
  every lead to the required fields only.

The exit code is non-zero if any source fails a check, returns zero leads,
times out, or raises, so CI/shells can gate on it.
"""

from __future__ import annotations

import math
import re
import threading
from datetime import UTC, datetime

from freelance_pipeline.sources import (
    JustJoinITJobs,
    MakeHireHelp,
    N8NCommunityJobs,
    N8NDevsJobs,
    NoFluffJobsJobs,
    RocketJobsJobs,
    UsemeJobs,
)

ABOUT_TOPIC_RE = re.compile(r"(?i)^about (the|this) .{0,40} categor")
ROCKETJOBS_OFFER_RE = re.compile(r"^https://rocketjobs\.pl/oferta-pracy/([a-z0-9-]+)/?$")
NOFLUFFJOBS_OFFER_RE = re.compile(r"^https://nofluffjobs\.com/job/([a-z0-9-]+)/?$")
REQUIRED_FIELDS = ("source", "source_id", "url")
COLLECT_TIMEOUT = 60.0
NOFLUFFJOBS_ENRICHMENT_FIELDS = ("company_or_author", "skills")
# RocketJobs enrichment is extracted with MUI class selectors that can drift on
# a redesign. Coverage is validated in aggregate so a wholesale selector drift
# (all offers silently stripped back to the required fields) fails the smoke.
ROCKETJOBS_ENRICHMENT_RATIO = 0.5
ROCKETJOBS_ENRICHMENT_FIELDS = ("company_or_author", "work_mode", "location", "skills")
NOFLUFFJOBS_ENRICHMENT_RATIO = 0.5

SOURCES = (
    JustJoinITJobs(),
    MakeHireHelp(),
    N8NCommunityJobs(),
    N8NDevsJobs(),
    NoFluffJobsJobs(),
    UsemeJobs(),
    RocketJobsJobs(),
)


class CollectTimeoutError(TimeoutError):
    """Raised when a source's ``collect()`` exceeds the wall-clock budget."""


def collect_with_timeout(source, timeout: float = COLLECT_TIMEOUT):
    """Run ``source.collect()`` on a daemon thread under a wall-clock budget.

    The adapter's own per-request ``timeout`` still applies to each HTTP call;
    this is an additional whole-collection guard so a misbehaving source
    cannot hang the smoke indefinitely.
    """
    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["leads"] = source.collect()
        except Exception as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    thread = threading.Thread(
        target=_run, name=f"smoke-{source.source}", daemon=True
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise CollectTimeoutError(
            f"[{source.source}] collect() exceeded {timeout:g}s wall-clock timeout"
        )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["leads"]


def _has_value(value) -> bool:
    """True when an optional enrichment field carries a real value."""
    if value is None:
        return False
    if isinstance(value, (str, tuple, list)):
        return bool(value)
    return True


def validate_rocketjobs_enrichment(leads):
    """Aggregate RocketJobs enrichment coverage check (empty errors = OK).

    Conservative by design: no single offer is required to carry any
    enrichment field, but a non-empty live batch must keep ``company_or_author``,
    ``work_mode``, ``location`` and ``skills`` populated on at least
    ``ROCKETJOBS_ENRICHMENT_RATIO`` of its leads and expose at least one
    budget/currency pair. Wholesale MUI selector drift therefore fails loudly
    instead of silently degrading every lead to the required fields only.
    """
    if not leads:
        return []
    count = len(leads)
    required = math.ceil(ROCKETJOBS_ENRICHMENT_RATIO * count)
    errors: list[str] = []
    for field in ROCKETJOBS_ENRICHMENT_FIELDS:
        present = sum(1 for lead in leads if _has_value(getattr(lead, field, None)))
        if present < required:
            errors.append(
                f"rocketjobs enrichment field {field!r} present on only "
                f"{present}/{count} leads (below {ROCKETJOBS_ENRICHMENT_RATIO:.0%} "
                f"threshold of {required}); selector drift likely"
            )
    if not any(lead.budget is not None and lead.currency for lead in leads):
        errors.append(
            "rocketjobs batch has no budget/currency pair; salary selector drift likely"
        )
    return errors


def validate_nofluffjobs_enrichment(leads):
    """Aggregate No Fluff Jobs enrichment coverage check (empty errors = OK).

    Conservative by design: no single offer is required to carry any
    enrichment field, but a non-empty live batch must keep ``company_or_author``
    and ``skills`` populated on at least half of its leads and expose at least
    one budget/currency pair. Wholesale SSR selector drift therefore fails
    loudly instead of silently degrading every lead to required fields only.
    """
    if not leads:
        return []
    count = len(leads)
    required = math.ceil(NOFLUFFJOBS_ENRICHMENT_RATIO * count)
    errors: list[str] = []
    for field in NOFLUFFJOBS_ENRICHMENT_FIELDS:
        present = sum(1 for lead in leads if _has_value(getattr(lead, field, None)))
        if present < required:
            errors.append(
                f"nofluffjobs enrichment field {field!r} present on only "
                f"{present}/{count} leads (below {NOFLUFFJOBS_ENRICHMENT_RATIO:.0%} "
                f"threshold of {required}); SSR selector drift likely"
            )
    if not any(lead.budget is not None and lead.currency for lead in leads):
        errors.append(
            "nofluffjobs batch has no budget/currency pair; salary selector drift likely"
        )
    return errors


def validate_leads(leads):
    """Return a list of acceptance-check violations for ``leads`` (empty = OK)."""
    errors: list[str] = []
    seen: dict[str, str] = {}
    for lead in leads:
        for field in REQUIRED_FIELDS:
            value = getattr(lead, field, None)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"lead {getattr(lead, 'source_id', '?')!r} has invalid required "
                    f"field {field!r}: {value!r}"
                )
        if lead.source_id in seen:
            errors.append(
                f"duplicate source_id {lead.source_id!r} "
                f"(titles {seen[lead.source_id]!r} vs {lead.title!r})"
            )
        else:
            seen[lead.source_id] = lead.title
        raw = lead.raw_data.to_dict() if lead.raw_data is not None else {}
        if raw.get("pinned") or raw.get("pinned_globally"):
            errors.append(f"lead {lead.source_id!r} is pinned metadata: {lead.title!r}")
        if raw.get("closed") or raw.get("archived"):
            errors.append(
                f"lead {lead.source_id!r} is closed/archived (exclusion policy "
                f"violation): {lead.title!r}"
            )
        if ABOUT_TOPIC_RE.match(lead.title or ""):
            errors.append(f"lead {lead.source_id!r} is an about-category topic: {lead.title!r}")
        if lead.source == "useme":
            if not isinstance(lead.source_id, str) or not lead.source_id.isdigit():
                errors.append(
                    f"useme lead has non-numeric source_id {lead.source_id!r} "
                    f"(category ids must never be treated as jobs)"
                )
            if "/category/" in (lead.url or ""):
                errors.append(
                    f"useme lead url is a category/navigation link: {lead.url!r}"
                )
        if lead.source == "justjoinit" and "/job-offer/" not in (lead.url or ""):
            errors.append(f"justjoinit lead url is not a job-offer URL: {lead.url!r}")
        if lead.source == "justjoinit":
            if raw.get("displayOffer") is False:
                errors.append(f"justjoinit lead {lead.source_id!r} is hidden")
            expired_at = raw.get("expiredAt")
            if isinstance(expired_at, str):
                try:
                    expired = datetime.fromisoformat(expired_at)
                except ValueError:
                    expired = None
                if (
                    expired is not None
                    and expired.utcoffset() is not None
                    and expired <= datetime.now(UTC)
                ):
                    errors.append(f"justjoinit lead {lead.source_id!r} is expired")
        if lead.source == "rocketjobs":
            if "/api/" in (lead.url or ""):
                errors.append(f"rocketjobs lead url hits the disallowed /api/: {lead.url!r}")
            match = ROCKETJOBS_OFFER_RE.match(lead.url or "")
            if match is None:
                errors.append(f"rocketjobs lead url is not a job offer URL: {lead.url!r}")
            elif lead.source_id != match.group(1):
                errors.append(
                    f"rocketjobs lead source_id {lead.source_id!r} does not match "
                    f"canonical offer slug {match.group(1)!r}"
                )
        if lead.source == "nofluffjobs":
            if any(
                fragment in (lead.url or "")
                for fragment in ("/api/", "/posting/", "/pdf/", "/signal")
            ):
                errors.append(f"nofluffjobs lead url hits a disallowed path: {lead.url!r}")
            match = NOFLUFFJOBS_OFFER_RE.match(lead.url or "")
            if match is None:
                errors.append(f"nofluffjobs lead url is not a job offer URL: {lead.url!r}")
            elif lead.source_id != match.group(1):
                errors.append(
                    f"nofluffjobs lead source_id {lead.source_id!r} does not match "
                    f"canonical offer slug {match.group(1)!r}"
                )
    if any(lead.source == "rocketjobs" for lead in leads):
        errors.extend(validate_rocketjobs_enrichment(leads))
    if any(lead.source == "nofluffjobs" for lead in leads):
        errors.extend(validate_nofluffjobs_enrichment(leads))
    return errors


def main() -> int:
    failures = 0
    for source in SOURCES:
        name = source.source
        try:
            leads = collect_with_timeout(source)
            errors = validate_leads(leads)
        except Exception as exc:  # noqa: BLE001 - report any source failure
            failures += 1
            print(f"[{name}] FAILED: {exc}")
            continue
        if not leads:
            failures += 1
            print(f"[{name}] FAILED: collected 0 leads")
            continue
        if errors:
            failures += 1
            print(f"[{name}] FAILED with {len(errors)} acceptance violation(s):")
            for error in errors:
                print(f"  - {error}")
            continue
        print(f"[{name}] OK: {len(leads)} leads; first: {leads[0].title!r}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())