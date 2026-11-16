import json
from pathlib import Path

import pytest

from freelance_pipeline.migration import (
    LegacyMigrationError,
    migrate_legacy_seen,
)
from freelance_pipeline.state import SeenStore


@pytest.fixture()
def legacy_path(tmp_path: Path) -> Path:
    return tmp_path / "legacy" / "seen_jobs.json"


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "nested" / "seen_ids.json"


def write_legacy(path: Path, ids) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids), encoding="utf-8")
    return path


def test_migrate_creates_store_and_merges_ids(legacy_path, store_path):
    write_legacy(legacy_path, ["35", "42", "143984"])
    added = migrate_legacy_seen(legacy_path, store_path)
    assert added == 3
    assert SeenStore(store_path).load() == {"useme": {"35", "42", "143984"}}


def test_migrate_does_not_modify_legacy_file(legacy_path, store_path):
    payload = '[\n  "35",\n  "42"\n]'
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(payload, encoding="utf-8")
    migrate_legacy_seen(legacy_path, store_path)
    assert legacy_path.read_text(encoding="utf-8") == payload


def test_migrate_does_not_modify_compact_legacy_file(legacy_path, store_path):
    payload = '["35", "42"]'
    write_legacy(legacy_path, json.loads(payload))
    migrate_legacy_seen(legacy_path, store_path)
    assert legacy_path.read_text(encoding="utf-8") == payload


def test_migrate_merges_into_existing_store_without_losing_other_sources(legacy_path, store_path):
    write_legacy(legacy_path, ["99"])
    SeenStore(store_path).add("useme", "1")
    SeenStore(store_path).add("upwork", "7")
    added = migrate_legacy_seen(legacy_path, store_path)
    assert added == 1
    assert SeenStore(store_path).load() == {"useme": {"1", "99"}, "upwork": {"7"}}


def test_migrate_counts_only_newly_added_ids(legacy_path, store_path):
    write_legacy(legacy_path, ["1", "99"])
    SeenStore(store_path).add("useme", "1")
    added = migrate_legacy_seen(legacy_path, store_path)
    assert added == 1
    assert SeenStore(store_path).load() == {"useme": {"1", "99"}}


def test_migrate_source_is_configurable(legacy_path, store_path):
    write_legacy(legacy_path, ["35", "42"])
    added = migrate_legacy_seen(legacy_path, store_path, source="upwork")
    assert added == 2
    assert SeenStore(store_path).load() == {"upwork": {"35", "42"}}


def test_migrate_accepts_any_explicitly_supplied_path(tmp_path, store_path):
    legacy = tmp_path / "arbitrary" / "cache" / "useme" / "old_seen.json"
    write_legacy(legacy, ["35", "42"])
    added = migrate_legacy_seen(legacy, store_path)
    assert added == 2
    assert SeenStore(store_path).load() == {"useme": {"35", "42"}}


def test_migrate_empty_list_is_a_noop(legacy_path, store_path):
    write_legacy(legacy_path, [])
    added = migrate_legacy_seen(legacy_path, store_path)
    assert added == 0
    assert SeenStore(store_path).load() == {}


def test_migrate_missing_file_raises(legacy_path, store_path):
    with pytest.raises(FileNotFoundError):
        migrate_legacy_seen(legacy_path, store_path)


def test_migrate_invalid_json_raises(legacy_path, store_path):
    write_legacy(legacy_path, ["ok"]).write_text("{ not json", encoding="utf-8")
    with pytest.raises(LegacyMigrationError):
        migrate_legacy_seen(legacy_path, store_path)


def test_migrate_non_list_raises(legacy_path, store_path):
    write_legacy(legacy_path, {"useme": ["35"]})
    with pytest.raises(LegacyMigrationError):
        migrate_legacy_seen(legacy_path, store_path)


def test_migrate_non_string_ids_raise(legacy_path, store_path):
    write_legacy(legacy_path, ["35", 42])
    with pytest.raises(LegacyMigrationError):
        migrate_legacy_seen(legacy_path, store_path)


def test_migrate_is_a_value_error_subclass():
    assert issubclass(LegacyMigrationError, ValueError)