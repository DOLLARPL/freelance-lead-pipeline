"""Portfolio-gap backlog: what clients keep asking for and Jan cannot show yet.

The scorer may return an optional ``gap`` per lead („this offer needs something
that is not in the portfolio"). This module turns those observations into one
append-only list, deduplicated and counted, so the same missing capability asked
for by several clients becomes visible instead of being rediscovered every run.

Nothing here is ever delivered to the chat on its own — Jan reads it on request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from freelance_pipeline.scoring import ScoreBucket, ScoredLead

MAX_ENTRIES = 30
VISIBLE_FROM_COUNT = 2
_NORMALIZE_RE = re.compile(r"[^a-z0-9ąćęłńóśźż ]+")


def normalize_gap(text: str) -> str:
    """Stable key for deduplication: lowercase, punctuation stripped, spaces collapsed."""
    lowered = text.strip().lower()
    cleaned = _NORMALIZE_RE.sub(" ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


@dataclass(frozen=True, slots=True)
class PocEntry:
    """One missing capability, with the offers that asked for it."""

    gap: str
    key: str
    count: int
    first_seen: str
    last_seen: str
    urls: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "gap": self.gap,
            "key": self.key,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "urls": list(self.urls),
        }


def load_backlog(path: Path) -> list[PocEntry]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries: list[PocEntry] = []
    for raw in payload.get("entries", []) if isinstance(payload, dict) else []:
        if not isinstance(raw, dict) or not raw.get("key"):
            continue
        entries.append(
            PocEntry(
                gap=str(raw.get("gap", "")),
                key=str(raw["key"]),
                count=int(raw.get("count", 1)),
                first_seen=str(raw.get("first_seen", "")),
                last_seen=str(raw.get("last_seen", "")),
                urls=tuple(str(url) for url in raw.get("urls", []) if url),
            )
        )
    return entries


def update_backlog(path: Path, scored: Sequence[ScoredLead], *, now: datetime | None = None) -> list[PocEntry]:
    """Merge this run's gaps into the backlog and persist it (atomic replace)."""
    stamp = (now if now is not None else datetime.now(UTC)).strftime("%Y-%m-%d")
    entries = {entry.key: entry for entry in load_backlog(path)}
    for lead in scored:
        # Only offers Jan would actually take can justify building a PoC —
        # rejected ads (accounting, penetration testing, employment) must not
        # shape the portfolio.
        if lead.bucket is ScoreBucket.ODRZUC:
            continue
        gap = (lead.gap or "").strip()
        if not gap:
            continue
        key = normalize_gap(gap)
        if not key:
            continue
        existing = entries.get(key)
        if existing is None:
            entries[key] = PocEntry(
                gap=gap,
                key=key,
                count=1,
                first_seen=stamp,
                last_seen=stamp,
                urls=(lead.lead.url,) if lead.lead.url else (),
            )
            continue
        fresh = bool(lead.lead.url) and lead.lead.url not in existing.urls
        urls = tuple(dict.fromkeys(existing.urls + ((lead.lead.url,) if lead.lead.url else ())))
        entries[key] = PocEntry(
            gap=existing.gap,
            key=key,
            # The count tracks DISTINCT offers, so re-running the same offers
            # (e.g. verification runs on a state copy) cannot inflate the signal.
            count=existing.count + 1 if fresh else max(existing.count, 1),
            first_seen=existing.first_seen or stamp,
            last_seen=stamp,
            urls=urls[-5:],
        )
    ordered = sorted(entries.values(), key=lambda entry: (-entry.count, entry.gap.lower()))[:MAX_ENTRIES]
    if not ordered and not path.exists():
        return []  # nothing to remember yet — do not create an empty file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"entries": [entry.to_dict() for entry in ordered]}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    tmp.replace(path)
    return ordered


def render_backlog(entries: Sequence[PocEntry], *, min_count: int = VISIBLE_FROM_COUNT) -> str:
    """Human-readable list; by default only gaps confirmed by 2+ offers."""
    visible = [entry for entry in entries if entry.count >= min_count]
    lines = [
        "# PoC — czego brakuje w portfolio (z ofert, które przyszły)",
        f"pozycji w pliku: {len(entries)} | pokazane (>= {min_count} ofert): {len(visible)}",
        "",
    ]
    if not visible:
        lines.append(
            "Na razie żadna luka nie powtórzyła się w dwóch ofertach — nic tu nie ma. "
            "Użyj --all, żeby zobaczyć pojedyncze sygnały."
        )
        return "\n".join(lines)
    for entry in visible:
        lines.append(f"- ({entry.count}x) {entry.gap}")
        if entry.urls:
            lines.append(f"  np. {entry.urls[0]}")
    return "\n".join(lines)


__all__ = [
    "MAX_ENTRIES",
    "VISIBLE_FROM_COUNT",
    "PocEntry",
    "load_backlog",
    "normalize_gap",
    "render_backlog",
    "update_backlog",
]
