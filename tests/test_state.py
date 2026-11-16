import json
import multiprocessing
import os
import threading
import time
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

import pytest

from freelance_pipeline.state import SeenStore, SeenStoreCorruptionError


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "seen_ids.json"


def test_starts_empty_when_no_file(store_path: Path):
    store = SeenStore(store_path)
    assert store.load() == {}


def test_add_and_load_roundtrip_per_source(store_path: Path):
    store = SeenStore(store_path)
    store.add("useme", "35", "42")
    store.add("useme", "42")
    store.add("upwork", "7")
    assert store.load() == {"useme": {"35", "42"}, "upwork": {"7"}}


def test_contains_is_per_source(store_path: Path):
    store = SeenStore(store_path)
    store.add("useme", "35")
    assert store.contains("useme", "35")
    assert not store.contains("upwork", "35")
    assert not store.contains("useme", "42")


def test_writes_valid_json_object_of_sorted_lists(store_path: Path):
    SeenStore(store_path).add("useme", "b", "a")
    data = json.loads(store_path.read_text())
    assert data == {"useme": ["a", "b"]}


def test_rejects_non_string_ids(store_path: Path):
    store = SeenStore(store_path)
    with pytest.raises(TypeError):
        store.add("useme", 42)
    with pytest.raises(TypeError):
        store.contains("useme", 42)  # type: ignore[arg-type]


def test_rejects_blank_source_or_id(store_path: Path):
    store = SeenStore(store_path)
    with pytest.raises(ValueError):
        store.add("", "x")
    with pytest.raises(ValueError):
        store.add("useme", "  ")


def test_creates_missing_parent_dirs(tmp_path: Path):
    store = SeenStore(tmp_path / "nested" / "deep" / "ids.json")
    store.add("useme", "1")
    assert store.contains("useme", "1")


def test_malformed_json_raises_corruption_error(store_path: Path):
    store_path.write_text("{ not json")
    with pytest.raises(SeenStoreCorruptionError):
        SeenStore(store_path).load()


def test_non_object_json_raises_corruption_error(store_path: Path):
    store_path.write_text(json.dumps(["useme:35"]))
    with pytest.raises(SeenStoreCorruptionError):
        SeenStore(store_path).load()


def test_non_list_ids_raise_corruption_error(store_path: Path):
    store_path.write_text(json.dumps({"useme": "35"}))
    with pytest.raises(SeenStoreCorruptionError):
        SeenStore(store_path).load()


def test_non_string_id_raises_corruption_error(store_path: Path):
    store_path.write_text(json.dumps({"useme": [35]}))
    with pytest.raises(SeenStoreCorruptionError):
        SeenStore(store_path).load()


def test_corrupt_state_is_not_silently_overwritten(store_path: Path):
    store_path.write_text("{ broken")
    with pytest.raises(SeenStoreCorruptionError):
        SeenStore(store_path).add("useme", "1")
    assert store_path.read_text() == "{ broken"


def test_each_write_uses_fresh_random_temp_file(store_path: Path, monkeypatch):
    real_replace = os.replace
    names = []

    def fake_replace(src, dst):
        names.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    store = SeenStore(store_path)
    store.add("useme", "1")
    store.add("useme", "2")
    assert len(names) == 2
    assert names[0] != names[1]
    assert all(name.startswith(store_path.name + ".") for name in names)
    assert all(len(name) > len(store_path.name) + 6 for name in names)


def test_replace_failure_cleans_temp_files(store_path: Path, monkeypatch):
    SeenStore(store_path).add("useme", "1")
    original = store_path.read_text()

    def boom(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        SeenStore(store_path).add("useme", "2")
    assert list(store_path.parent.glob(store_path.name + ".*")) == []
    assert store_path.read_text() == original


def test_temp_file_written_alongside_store(store_path: Path, monkeypatch):
    real_replace = os.replace
    seen_tmp = {}

    def fake_replace(src, dst):
        seen_tmp["src"] = Path(src)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    SeenStore(store_path).add("useme", "1")
    assert seen_tmp["src"].parent == store_path.parent


def _concurrent_worker(path: Path, source: str, ids, barrier):
    store = SeenStore(path)
    barrier.wait()
    for id_ in ids:
        store.add(source, id_)
        time.sleep(0.0005)


def test_two_concurrent_writers_lose_no_updates(tmp_path: Path):
    path = tmp_path / "nested" / "seen" / "ids.json"
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    ids_a = [str(i) for i in range(40)]
    ids_b = [str(i) for i in range(40)]
    p1 = ctx.Process(target=_concurrent_worker, args=(path, "useme", ids_a, barrier))
    p2 = ctx.Process(target=_concurrent_worker, args=(path, "upwork", ids_b, barrier))
    p1.start()
    p2.start()
    p1.join(60)
    p2.join(60)
    if p1.is_alive():
        p1.terminate()
    if p2.is_alive():
        p2.terminate()
    assert p1.exitcode == 0, p1.exitcode
    assert p2.exitcode == 0, p2.exitcode
    data = SeenStore(path).load()
    assert data["useme"] == set(ids_a)
    assert data["upwork"] == set(ids_b)


def test_add_buckets_roundtrips_multiple_buckets(store_path: Path):
    SeenStore(store_path).add_buckets(
        {"useme": ["35", "42"], "__canonical_urls__": ["https://a/x"]}
    )
    data = json.loads(store_path.read_text())
    assert data["useme"] == ["35", "42"]
    assert data["__canonical_urls__"] == ["https://a/x"]


def test_add_buckets_merges_with_existing_state(store_path: Path):
    store = SeenStore(store_path)
    store.add("useme", "1")
    store.add_buckets({"useme": ["2"], "upwork": ["7"]})
    assert store.load() == {"useme": {"1", "2"}, "upwork": {"7"}}


def test_add_buckets_writes_exactly_once(store_path: Path, monkeypatch):
    real_replace = os.replace
    names = []

    def fake_replace(src, dst):
        names.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    SeenStore(store_path).add_buckets(
        {"useme": ["1", "2"], "__canonical_urls__": ["u1", "u2"]}
    )
    assert len(names) == 1
    assert names[0].startswith(store_path.name + ".")


def test_add_buckets_blank_or_non_string_keys_rejected(store_path: Path):
    store = SeenStore(store_path)
    with pytest.raises(ValueError):
        store.add_buckets({"": ["1"]})
    with pytest.raises(TypeError):
        store.add_buckets({42: ["1"]})


def test_add_buckets_non_string_values_rejected(store_path: Path):
    store = SeenStore(store_path)
    with pytest.raises(TypeError):
        store.add_buckets({"useme": [42]})
    with pytest.raises(ValueError):
        store.add_buckets({"useme": ["  "]})


def test_add_buckets_bypass_keeps_load_compatible(store_path: Path):
    SeenStore(store_path).add_buckets({"useme": ["a"], "__canonical_urls__": ["u"]})
    assert SeenStore(store_path).load() == {"useme": {"a"}, "__canonical_urls__": {"u"}}


def test_add_blocks_while_lock_is_held(tmp_path: Path):
    store = SeenStore(tmp_path / "seen" / "ids.json")
    store.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = store.lock_path
    with lock_file.open("a+") as lf:
        flock(lf, LOCK_EX)
        started = threading.Event()
        result = {}

        def do_add():
            started.set()
            store.add("useme", "1")
            result["done"] = True

        thread = threading.Thread(target=do_add)
        thread.start()
        started.wait()
        time.sleep(0.25)
        assert "done" not in result
        assert thread.is_alive()
        flock(lf, LOCK_UN)
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert result.get("done") is True
    assert store.contains("useme", "1")