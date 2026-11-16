"""Cross-source lead identity and merging.

The same job can arrive twice: as an Oferteo notification (``new``) and later
again as the ``☎`` mail once the contact is unlocked, or from two portals at
once. Identity keys are therefore explicit and ordered by strength:

1. ``oferteo:<rfp_id>`` when the portal gives a request number;
2. ``text:<hash>`` from normalized title + city + publication day, which also
   catches the same offer cross-posted somewhere else.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import replace

from freelance_pipeline.models import JobLead


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).lower()
    text = re.sub(r"[^a-z0-9ąćęłńóśźż ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(lead: JobLead) -> str:
    day = lead.published_at.date().isoformat() if lead.published_at else ""
    payload = "|".join(
        (normalize_text(lead.title), normalize_text(lead.location), day)
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def identity_keys(lead: JobLead) -> tuple[str, ...]:
    keys: list[str] = []
    rfp = None
    if lead.raw_data:
        rfp = lead.raw_data.get("rfp_id")
    if rfp:
        keys.append(f"oferteo:{rfp}")
    keys.append(f"text:{fingerprint(lead)}")
    return tuple(keys)


def _plain(value):
    from collections.abc import Mapping, Sequence

    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(v) for v in value]
    return value


def merge_leads(existing: JobLead, incoming: JobLead) -> JobLead:
    """Enrich ``existing`` with ``incoming`` without erasing known values."""
    changes = {}
    for field in (
        "description",
        "company_or_author",
        "published_at",
        "budget",
        "currency",
        "location",
        "status",
        "url",
        "canonical_url",
        "title",
    ):
        current = getattr(existing, field)
        candidate = getattr(incoming, field)
        if candidate not in (None, "", (), 0) and current in (None, "", (), 0):
            changes[field] = candidate
    if incoming.raw_data:
        merged = _plain(existing.raw_data or {})
        for key, value in dict(incoming.raw_data).items():
            if merged.get(key) in (None, "", {}, []):
                merged[key] = value
        changes["raw_data"] = merged
    return replace(existing, **changes) if changes else existing
