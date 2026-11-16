from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None


class SeenStoreCorruptionError(ValueError):
    """Raised when the seen-IDs file exists but cannot be trusted."""


class SeenStore:
    """Atomic, lock-guarded JSON store of per-source seen IDs.

    The on-disk layout is a JSON object mapping source names to lists of
    string IDs, e.g. ``{"useme": ["35", "42"]}``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.lock_path = self._path.parent / f".{self._path.name}.lock"

    def load(self) -> dict[str, set[str]]:
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SeenStoreCorruptionError(f"cannot read seen store {self._path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SeenStoreCorruptionError(f"seen store {self._path} contains invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SeenStoreCorruptionError(
                f"seen store {self._path} must contain a JSON object, got {type(data).__name__}"
            )
        result: dict[str, set[str]] = {}
        for source, ids in data.items():
            if not isinstance(source, str) or not source.strip():
                raise SeenStoreCorruptionError(f"seen store {self._path} has invalid source key {source!r}")
            if not isinstance(ids, list):
                raise SeenStoreCorruptionError(
                    f"seen store {self._path} source {source!r} must map to a list of "
                    f"string IDs, got {type(ids).__name__}"
                )
            bucket: set[str] = set()
            for id_ in ids:
                if not isinstance(id_, str) or not id_.strip():
                    raise SeenStoreCorruptionError(
                        f"seen store {self._path} source {source!r} contains invalid ID {id_!r}"
                    )
                bucket.add(id_)
            result[source] = bucket
        return result

    @staticmethod
    def _check_str(value: object, label: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a str, got {type(value).__name__}")
        if not value.strip():
            raise ValueError(f"{label} must not be empty or whitespace")

    def add(self, source: str, *ids: str) -> None:
        self._check_str(source, "source")
        for id_ in ids:
            self._check_str(id_, "seen ID")
        self.add_buckets({source: ids})

    def add_buckets(self, buckets: Mapping[str, Iterable[str]]) -> None:
        """Atomically add many ``source -> ids`` buckets in a single write.

        Every key/value is validated up front; if anything is invalid nothing
        is written at all. The whole update happens under one lock and one
        atomic ``os.replace``, so committed state can never show a partial
        multi-key update (e.g. seen IDs without their canonical URLs).
        """
        validated: dict[str, list[str]] = {}
        for bucket, ids in buckets.items():
            self._check_str(bucket, "source")
            bucket_ids: list[str] = []
            for id_ in ids:
                self._check_str(id_, "seen ID")
                bucket_ids.append(id_)
            if bucket_ids:
                validated[bucket] = bucket_ids
        if not validated:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock():
            seen = self.load()
            for bucket, ids in validated.items():
                seen.setdefault(bucket, set()).update(ids)
            self._write(seen)

    def contains(self, source: str, id_: str) -> bool:
        self._check_str(source, "source")
        self._check_str(id_, "seen ID")
        return id_ in self.load().get(source, set())

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _write(self, seen: dict[str, set[str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {source: sorted(ids) for source, ids in seen.items()},
            ensure_ascii=False,
            indent=2,
        )
        fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), prefix=f"{self._path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
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