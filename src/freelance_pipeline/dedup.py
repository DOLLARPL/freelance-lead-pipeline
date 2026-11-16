from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from freelance_pipeline.models import JobLead
from freelance_pipeline.state import SeenStore

# Reserved, non-source bucket holding every already-handled canonical offer URL.
URLS_BUCKET = "__canonical_urls__"

_REASON_ID = "source+source_id"
_REASON_URL = "canonical_url"


@dataclass(frozen=True, slots=True)
class DedupeDrop:
    """One lead that was not a candidate this run, plus why."""

    lead: JobLead
    reason: str


@dataclass(frozen=True, slots=True)
class DedupeResult:
    """New candidates and the leads that were excluded, in input order."""

    candidates: tuple[JobLead, ...] = ()
    dropped: tuple[DedupeDrop, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.candidates


class PipelineState:
    """Persisted seen-state across two namespaces: source IDs and URLs.

    ``source -> {source_id, ...}`` reuses the legacy ``SeenStore`` layout so
    previously migrated state remains readable; the canonical-URL namespace
    lives under the reserved :data:`URLS_BUCKET` key in the same object. State
    is only ever written once, atomically (single lock + atomic replace), and
    only by the caller *after* the delivery callback has confirmed success.
    """

    def __init__(self, path: str | Path) -> None:
        self._store = SeenStore(path)

    @classmethod
    def from_store(cls, store: SeenStore) -> PipelineState:
        state = cls.__new__(cls)
        state._store = store
        return state

    def is_known(self, source: str, source_id: str) -> bool:
        return self._store.contains(source, source_id)

    def is_url_known(self, canonical_url: str) -> bool:
        return self._store.contains(URLS_BUCKET, canonical_url)

    def commit(self, leads: Sequence[JobLead]) -> None:
        """Atomically mark the leads as handled (IDs + canonical URLs)."""
        buckets: dict[str, list[str]] = {URLS_BUCKET: []}
        for lead in leads:
            buckets.setdefault(lead.source, []).append(lead.source_id)
            if lead.canonical_url:
                buckets[URLS_BUCKET].append(lead.canonical_url)
        self._store.add_buckets(buckets)

    def dedupe(self, leads: Sequence[JobLead]) -> DedupeResult:
        """Filter to new candidates: source+source_id first, then URL.

        Both rules are checked against persisted state *and* against
        already-accepted leads within this very batch, so cross-source and
        within-batch duplicates are both excluded and nothing is stored yet.
        """
        known = self._load_snapshot()
        candidates: list[JobLead] = []
        dropped: list[DedupeDrop] = []
        for lead in leads:
            if (lead.source, lead.source_id) in known.ids:
                dropped.append(DedupeDrop(lead, _REASON_ID))
                continue
            if lead.canonical_url and lead.canonical_url in known.urls:
                dropped.append(DedupeDrop(lead, _REASON_URL))
                continue
            known.ids.add((lead.source, lead.source_id))
            if lead.canonical_url:
                known.urls.add(lead.canonical_url)
            candidates.append(lead)
        return DedupeResult(candidates=tuple(candidates), dropped=tuple(dropped))

    def _load_snapshot(self):
        data = self._store.load()
        urls = set(data.pop(URLS_BUCKET, set()))
        ids = {(source, id_) for source, ids_ in data.items() for id_ in ids_}
        return _Snapshot(ids=ids, urls=urls)


@dataclass(slots=True)
class _Snapshot:
    ids: set[tuple[str, str]]
    urls: set[str]


__all__ = [
    "URLS_BUCKET",
    "DedupeDrop",
    "DedupeResult",
    "PipelineState",
]