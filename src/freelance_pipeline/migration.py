from __future__ import annotations

import json
from pathlib import Path

from freelance_pipeline.state import SeenStore


class LegacyMigrationError(ValueError):
    """Raised when a legacy seen file cannot be trusted as a list of string IDs."""


def load_legacy_seen_ids(path: str | Path) -> list[str]:
    """Read a legacy JSON list of string IDs without ever modifying the file.

    The legacy Useme scraper stored its seen ids as a bare JSON array of
    strings, e.g. ``["35", "42", "143984"]``. Anything else - a missing file,
    invalid JSON, a non-list payload or a non-string id - is rejected loudly
    instead of silently losing state.
    """
    legacy_path = Path(path)
    try:
        text = legacy_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"legacy seen file not found: {legacy_path}") from None
    except OSError as exc:
        raise LegacyMigrationError(
            f"cannot read legacy seen file {legacy_path}: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LegacyMigrationError(
            f"legacy seen file {legacy_path} contains invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise LegacyMigrationError(
            f"legacy seen file {legacy_path} must contain a JSON list of string "
            f"IDs, got {type(data).__name__}"
        )
    ids: list[str] = []
    for id_ in data:
        if not isinstance(id_, str) or not id_.strip():
            raise LegacyMigrationError(
                f"legacy seen file {legacy_path} contains invalid ID {id_!r}"
            )
        ids.append(id_)
    return ids


def migrate_legacy_seen(
    legacy_path: str | Path,
    seen_store_path: str | Path,
    *,
    source: str = "useme",
) -> int:
    """Merge a legacy JSON list of IDs into a ``SeenStore`` under ``source``.

    Both paths are explicit parameters: the caller decides where the legacy
    file lives and where the v2 store should be written. The legacy file is
    only read, never modified. Returns the number of IDs that were newly added
    (IDs already present in the store are left untouched).
    """
    ids = load_legacy_seen_ids(legacy_path)
    if not ids:
        return 0
    store = SeenStore(seen_store_path)
    existing = store.load().get(source, set())
    new_ids = [id_ for id_ in ids if id_ not in existing]
    if new_ids:
        store.add(source, *new_ids)
    return len(new_ids)


__all__ = [
    "LegacyMigrationError",
    "load_legacy_seen_ids",
    "migrate_legacy_seen",
]