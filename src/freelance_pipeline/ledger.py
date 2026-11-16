"""Rolling offer ledger: what Jan has seen, answered or skipped.

``state.json`` (see :mod:`freelance_pipeline.state`) only remembers "this ID was
already collected", which is why an offer whose Telegram delivery failed comes
back as new, and why "I answered this one" has no representation anywhere.
This module adds the missing half: one record per offer, with a lifecycle

    new  ->  seen  ->  replied / skipped
                  \\-> expired (no decision within the TTL)

so the rolling list can show exactly what has piled up since the last delivery
(and nothing else), and so :mod:`freelance_pipeline.proposal` has somewhere to
store the drafted message next to the offer it belongs to.

The file is a JSON object keyed by ``"<source>:<source_id>"`` (the same
identity :class:`freelance_pipeline.dedup.PipelineState` dedupes on), written
atomically under an flock — the same discipline as ``SeenStore``, because both
files are touched by the cron in one run.

The ledger never decides *policy*: TTL defaults live here as constants, but
what a status means for delivery is the caller's business.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

from freelance_pipeline.dedup import URLS_BUCKET
from freelance_pipeline.models import JobLead
from freelance_pipeline.state import SeenStore

STATUS_NEW = "new"
STATUS_SEEN = "seen"
STATUS_REPLIED = "replied"
STATUS_SKIPPED = "skipped"
STATUS_EXPIRED = "expired"

#: Every legal lifecycle state, in the order the loop goes through them.
STATUSES: tuple[str, ...] = (
    STATUS_NEW,
    STATUS_SEEN,
    STATUS_REPLIED,
    STATUS_SKIPPED,
    STATUS_EXPIRED,
)

#: Jan made a call on these; the rolling list must never show them again.
DECIDED_STATUSES: tuple[str, ...] = (STATUS_REPLIED, STATUS_SKIPPED)

#: An offer nobody decided on within this many days leaves the list (D3: the
#: rolling list shows only ``new``, so this is the only thing keeping the file
#: from growing forever).
DEFAULT_EXPIRY_DAYS = 21


class LedgerCorruptionError(ValueError):
    """Raised when the ledger file exists but cannot be trusted."""


def ledger_key(source: str, source_id: str) -> str:
    """The one identity of an offer across runs: ``"<source>:<source_id>"``."""
    _require_str("source", source)
    _require_str("source_id", source_id)
    return f"{source}:{source_id}"


def split_key(key: str) -> tuple[str, str]:
    """Inverse of :func:`ledger_key`; raises ``ValueError`` on a malformed key."""
    _require_str("key", key)
    source, separator, source_id = key.partition(":")
    if not separator or not source or not source_id:
        raise ValueError(f"ledger key must look like '<source>:<source_id>', got {key!r}")
    return source, source_id


def _require_str(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str, got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{name} must not be empty or whitespace")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_at(name: str, value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerCorruptionError(f"{name} must be an ISO-8601 string or null, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LedgerCorruptionError(f"{name} is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise LedgerCorruptionError(f"{name} must be timezone-aware: {value!r}")
    return parsed


def _aware(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OfferSeed:
    """A newly collected offer, as it enters the ledger for the first time."""

    source: str
    source_id: str
    url: str = ""
    canonical_url: str = ""
    title: str = ""
    bucket: str = ""
    score: float | None = None

    def __post_init__(self) -> None:
        ledger_key(self.source, self.source_id)
        for name in ("url", "canonical_url", "title", "bucket"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a str, got {type(value).__name__}")
        if self.score is not None and isinstance(self.score, bool):
            raise TypeError("score must be a number or None, got bool")

    @classmethod
    def from_lead(cls, lead: JobLead, *, bucket: str, score: float | None = None) -> OfferSeed:
        return cls(
            source=lead.source,
            source_id=lead.source_id,
            url=lead.url,
            canonical_url=lead.canonical_url or "",
            title=lead.title or "",
            bucket=bucket,
            score=score,
        )

    @property
    def key(self) -> str:
        return ledger_key(self.source, self.source_id)


@dataclass(frozen=True, slots=True)
class OfferRecord:
    """One offer plus everything the pipeline has learned about it so far."""

    key: str
    source: str
    source_id: str
    url: str = ""
    canonical_url: str = ""
    title: str = ""
    bucket: str = ""
    score: float | None = None
    first_seen_at: datetime | None = None
    in_report_at: datetime | None = None
    status: str = STATUS_NEW
    status_changed_at: datetime | None = None
    draft: str | None = None
    draft_at: datetime | None = None
    rules_version: str | None = None

    def __post_init__(self) -> None:
        key_source, key_id = split_key(self.key)
        if (key_source, key_id) != (self.source, self.source_id):
            raise ValueError(
                f"key {self.key!r} does not match source/source_id "
                f"{self.source!r}/{self.source_id!r}"
            )
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}; expected one of {STATUSES}")
        for name in ("first_seen_at", "in_report_at", "status_changed_at", "draft_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(name, value))

    @property
    def decided(self) -> bool:
        return self.status in DECIDED_STATUSES

    def to_json(self) -> dict[str, object]:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "source": self.source,
            "source_id": self.source_id,
            "bucket": self.bucket,
            "score": self.score,
            "first_seen_at": _iso(self.first_seen_at) if self.first_seen_at else None,
            "in_report_at": _iso(self.in_report_at) if self.in_report_at else None,
            "status": self.status,
            "status_changed_at": _iso(self.status_changed_at) if self.status_changed_at else None,
            "draft": self.draft,
            "draft_at": _iso(self.draft_at) if self.draft_at else None,
            "rules_version": self.rules_version,
        }

    @classmethod
    def from_json(cls, key: str, data: object) -> OfferRecord:
        if not isinstance(data, dict):
            raise LedgerCorruptionError(
                f"ledger entry {key!r} must be a JSON object, got {type(data).__name__}"
            )
        source, source_id = split_key(key)
        record_source = data.get("source", source)
        record_id = data.get("source_id", source_id)
        if record_source != source or record_id != source_id:
            raise LedgerCorruptionError(
                f"ledger entry {key!r} disagrees with its own source/source_id "
                f"({record_source!r}/{record_id!r})"
            )
        status = data.get("status", STATUS_NEW)
        if status not in STATUSES:
            raise LedgerCorruptionError(f"ledger entry {key!r} has unknown status {status!r}")
        score = data.get("score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
            raise LedgerCorruptionError(f"ledger entry {key!r} has a non-numeric score {score!r}")
        draft = data.get("draft")
        if draft is not None and not isinstance(draft, str):
            raise LedgerCorruptionError(f"ledger entry {key!r} has a non-string draft")
        rules_version = data.get("rules_version")
        if rules_version is not None and not isinstance(rules_version, str):
            raise LedgerCorruptionError(f"ledger entry {key!r} has a non-string rules_version")
        for name in ("url", "canonical_url", "title", "bucket"):
            value = data.get(name, "")
            if not isinstance(value, str):
                raise LedgerCorruptionError(
                    f"ledger entry {key!r} field {name!r} must be a string, got {type(value).__name__}"
                )
        return cls(
            key=key,
            source=source,
            source_id=source_id,
            url=data.get("url", ""),
            canonical_url=data.get("canonical_url", ""),
            title=data.get("title", ""),
            bucket=data.get("bucket", ""),
            score=float(score) if score is not None else None,
            first_seen_at=_parse_at("first_seen_at", data.get("first_seen_at")),
            in_report_at=_parse_at("in_report_at", data.get("in_report_at")),
            status=status,
            status_changed_at=_parse_at("status_changed_at", data.get("status_changed_at")),
            draft=draft,
            draft_at=_parse_at("draft_at", data.get("draft_at")),
            rules_version=rules_version,
        )


class Ledger:
    """Atomic, lock-guarded JSON store of :class:`OfferRecord` rows."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.lock_path = self._path.parent / f".{self._path.name}.lock"

    @property
    def path(self) -> Path:
        return self._path

    # ---------------------------------------------------------------- reading

    def load(self) -> dict[str, OfferRecord]:
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerCorruptionError(f"cannot read ledger {self._path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LedgerCorruptionError(f"ledger {self._path} contains invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LedgerCorruptionError(
                f"ledger {self._path} must contain a JSON object, got {type(data).__name__}"
            )
        records: dict[str, OfferRecord] = {}
        for key, entry in data.items():
            if not isinstance(key, str) or not key.strip():
                raise LedgerCorruptionError(f"ledger {self._path} has an invalid key {key!r}")
            try:
                records[key] = OfferRecord.from_json(key, entry)
            except ValueError as exc:
                raise LedgerCorruptionError(f"ledger {self._path}: {exc}") from exc
        return records

    def get(self, key: str) -> OfferRecord | None:
        return self.load().get(key)

    def by_status(self, status: str, *, limit: int | None = None) -> tuple[OfferRecord, ...]:
        self._check_status(status)
        records = [record for record in self.load().values() if record.status == status]
        records.sort(key=lambda record: (record.first_seen_at or datetime.min.replace(tzinfo=UTC), record.key))
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must not be negative")
            records = records[:limit]
        return tuple(records)

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for record in self.load().values():
            counts[record.status] += 1
        return counts

    def __len__(self) -> int:
        return len(self.load())

    # ---------------------------------------------------------------- writing

    def add(self, seeds: Iterable[OfferSeed], *, now: datetime | None = None) -> tuple[str, ...]:
        """Insert new offers as ``new``; existing keys keep their status.

        Returns the keys that were actually created, in input order — an offer
        already in the ledger (even ``expired``) is never resurrected, so a
        re-collected row can not reappear on the rolling list.
        """
        stamp = _aware("now", now or _now())
        prepared: list[OfferSeed] = list(seeds)
        for seed in prepared:
            if not isinstance(seed, OfferSeed):
                raise TypeError(f"seeds must be OfferSeed instances, got {type(seed).__name__}")
        if not prepared:
            return ()
        added: list[str] = []
        with self._lock():
            records = self.load()
            for seed in prepared:
                if seed.key in records:
                    continue
                records[seed.key] = OfferRecord(
                    key=seed.key,
                    source=seed.source,
                    source_id=seed.source_id,
                    url=seed.url,
                    canonical_url=seed.canonical_url,
                    title=seed.title,
                    bucket=seed.bucket,
                    score=seed.score,
                    first_seen_at=stamp,
                    status=STATUS_NEW,
                )
                added.append(seed.key)
            if added:
                self._write(records)
        return tuple(added)

    def mark_status(
        self,
        keys: Iterable[str],
        status: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Move offers to ``status``; returns the keys whose status changed.

        An unknown key is a ``KeyError`` (a wrong row number must be loud, not
        silent), while a key already in the target status is simply not
        reported as changed and keeps its original ``status_changed_at``.
        """
        self._check_status(status)
        stamp = _aware("now", now or _now())
        targets = list(keys)
        if not targets:
            return ()
        changed: list[str] = []
        with self._lock():
            records = self.load()
            unknown = [key for key in targets if key not in records]
            if unknown:
                raise KeyError(f"ledger has no offer(s) {', '.join(sorted(unknown))}")
            for key in targets:
                record = records[key]
                if record.status == status:
                    continue
                records[key] = self._replace(record, status=status, status_changed_at=stamp)
                changed.append(key)
            if changed:
                self._write(records)
        return tuple(changed)

    def mark_delivered(self, keys: Iterable[str], *, now: datetime | None = None) -> tuple[str, ...]:
        """``new -> seen`` for rows that just went out on the rolling list.

        ``in_report_at`` is stamped here (not in :meth:`mark_status`) because it
        means "this offer was put in front of Jan", which only delivery knows.
        Rows already past ``new`` are left alone.
        """
        stamp = _aware("now", now or _now())
        targets = list(keys)
        if not targets:
            return ()
        changed: list[str] = []
        with self._lock():
            records = self.load()
            unknown = [key for key in targets if key not in records]
            if unknown:
                raise KeyError(f"ledger has no offer(s) {', '.join(sorted(unknown))}")
            for key in targets:
                record = records[key]
                if record.status != STATUS_NEW:
                    continue
                records[key] = self._replace(
                    record, status=STATUS_SEEN, status_changed_at=stamp, in_report_at=stamp
                )
                changed.append(key)
            if changed:
                self._write(records)
        return tuple(changed)

    def set_draft(
        self,
        key: str,
        draft: str,
        *,
        rules_version: str | None = None,
        now: datetime | None = None,
    ) -> OfferRecord:
        """Attach the drafted message (etap 3) to an existing offer."""
        _require_str("draft", draft)
        if rules_version is not None:
            _require_str("rules_version", rules_version)
        stamp = _aware("now", now or _now())
        with self._lock():
            records = self.load()
            record = records.get(key)
            if record is None:
                raise KeyError(f"ledger has no offer {key!r}")
            record = self._replace(
                record,
                draft=draft,
                draft_at=stamp,
                rules_version=rules_version if rules_version is not None else record.rules_version,
            )
            records[key] = record
            self._write(records)
        return record

    def expire(
        self,
        *,
        max_age_days: float = DEFAULT_EXPIRY_DAYS,
        max_age_hours: float | None = None,
        only_delivered: bool = False,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Retire undecided offers older than the given age.

        ``max_age_hours`` takes precedence over ``max_age_days`` when both are
        given; a fraction is fine (``max_age_days=0.5`` is twelve hours).
        ``only_delivered`` skips offers Jan has never seen in a report or push,
        so the first import of a new source cannot vanish before it is shown.

        Only ``new`` and ``seen`` can expire: a decision Jan made is not
        something time is allowed to erase.
        """
        if max_age_hours is not None:
            if max_age_hours < 0:
                raise ValueError("max_age_hours must not be negative")
            age = timedelta(hours=max_age_hours)
        else:
            if max_age_days < 0:
                raise ValueError("max_age_days must not be negative")
            age = timedelta(days=max_age_days)
        stamp = _aware("now", now or _now())
        cutoff = stamp - age
        expired: list[str] = []
        with self._lock():
            records = self.load()
            for key, record in list(records.items()):
                if record.status not in (STATUS_NEW, STATUS_SEEN):
                    continue
                if only_delivered and record.in_report_at is None:
                    continue
                first_seen = record.first_seen_at
                if first_seen is None or first_seen >= cutoff:
                    continue
                records[key] = self._replace(record, status=STATUS_EXPIRED, status_changed_at=stamp)
                expired.append(key)
            if expired:
                self._write(records)
        return tuple(expired)

    def migrate_from_state(
        self,
        state_path: str | Path,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Seed the ledger from an existing ``state.json``.

        Everything ``state.json`` already knows was, by definition, delivered in
        an earlier run, so it enters the ledger as ``seen`` rather than ``new``
        — otherwise the first rolling list after switching over would repeat
        every offer of the past weeks. URLs are not carried over: the state file
        stores them in a bucket without the id they belong to.
        """
        stamp = _aware("now", now or _now())
        known = SeenStore(state_path).load()
        known.pop(URLS_BUCKET, None)
        added: list[str] = []
        with self._lock():
            records = self.load()
            for source, ids in known.items():
                for source_id in sorted(ids):
                    key = ledger_key(source, source_id)
                    if key in records:
                        continue
                    records[key] = OfferRecord(
                        key=key,
                        source=source,
                        source_id=source_id,
                        first_seen_at=stamp,
                        in_report_at=stamp,
                        status=STATUS_SEEN,
                        status_changed_at=stamp,
                    )
                    added.append(key)
            if added:
                self._write(records)
        return tuple(added)

    # ---------------------------------------------------------------- plumbing

    @staticmethod
    def _replace(record: OfferRecord, **changes: object) -> OfferRecord:
        return OfferRecord(
            **{**{field: getattr(record, field) for field in _RECORD_FIELDS}, **changes}  # type: ignore[arg-type]
        )

    @staticmethod
    def _check_status(status: object) -> None:
        _require_str("status", status)
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _write(self, records: dict[str, OfferRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {key: record.to_json() for key, record in sorted(records.items())},
            ensure_ascii=False,
            indent=2,
        )
        fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), prefix=f"{self._path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # Panel (uid 10000) i crony (root) pisza ten sam plik. Bez 664 plik
            # po zapisie roota zostaje 600 i panel nie moze juz zapisac statusu.
            os.chmod(tmp_path, 0o664)
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


_RECORD_FIELDS: tuple[str, ...] = (
    "key",
    "source",
    "source_id",
    "url",
    "canonical_url",
    "title",
    "bucket",
    "score",
    "first_seen_at",
    "in_report_at",
    "status",
    "status_changed_at",
    "draft",
    "draft_at",
    "rules_version",
)

# Keep the field list honest: adding a field to OfferRecord without adding it
# here would make _replace silently drop it.
assert set(_RECORD_FIELDS) == {field for field in OfferRecord.__slots__}, (
    "OfferRecord._RECORD_FIELDS is out of date"
)

__all__: Sequence[str] = (
    "DECIDED_STATUSES",
    "DEFAULT_EXPIRY_DAYS",
    "STATUSES",
    "STATUS_EXPIRED",
    "STATUS_NEW",
    "STATUS_REPLIED",
    "STATUS_SEEN",
    "STATUS_SKIPPED",
    "Ledger",
    "LedgerCorruptionError",
    "OfferRecord",
    "OfferSeed",
    "ledger_key",
    "split_key",
)
