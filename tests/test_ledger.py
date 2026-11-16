from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

import pytest

from freelance_pipeline.dedup import URLS_BUCKET
from freelance_pipeline.ledger import (
    DEFAULT_EXPIRY_DAYS,
    STATUS_EXPIRED,
    STATUS_NEW,
    STATUS_REPLIED,
    STATUS_SEEN,
    STATUS_SKIPPED,
    STATUSES,
    Ledger,
    LedgerCorruptionError,
    OfferRecord,
    OfferSeed,
    ledger_key,
    split_key,
)
from freelance_pipeline.models import JobLead
from freelance_pipeline.state import SeenStore

NOW = datetime(2026, 10, 7, 5, 30, tzinfo=UTC)


def _days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.json"


def _record(ledger: Ledger, key: str) -> OfferRecord:
    """Ledger lookup for tests: the row must exist, or the test is wrong."""
    record = ledger.get(key)
    assert record is not None, f"{key} is not in the ledger"
    return record


def _seed(source: str = "useme", source_id: str = "145473", **kwargs) -> OfferSeed:
    return OfferSeed(source=source, source_id=source_id, **kwargs)


# ---------------------------------------------------------------- identity


def test_ledger_key_roundtrips_through_split_key():
    key = ledger_key("useme", "145473")
    assert key == "useme:145473"
    assert split_key(key) == ("useme", "145473")


def test_split_key_keeps_colons_in_the_id():
    assert split_key("facebook_groups:123:456") == ("facebook_groups", "123:456")


def test_ledger_key_rejects_blank_parts():
    with pytest.raises(ValueError):
        ledger_key("", "1")
    with pytest.raises(ValueError):
        ledger_key("useme", "  ")
    with pytest.raises(TypeError):
        ledger_key("useme", 7)  # type: ignore[arg-type]


def test_split_key_rejects_malformed_keys():
    with pytest.raises(ValueError):
        split_key("no-colon-here")
    with pytest.raises(ValueError):
        split_key("useme:")


def test_offer_seed_from_lead_copies_identity_and_metadata():
    lead = JobLead(
        source="useme",
        source_id="145473",
        url="https://useme.com/pl/jobs/x,145473/",
        canonical_url="https://useme.com/pl/jobs/x,145473/",
        title="Zlecę przygotowanie leadów",
    )
    seed = OfferSeed.from_lead(lead, bucket="TOP", score=100)
    assert seed.key == "useme:145473"
    assert (seed.bucket, seed.score, seed.title) == ("TOP", 100, "Zlecę przygotowanie leadów")


# ---------------------------------------------------------------- reading


def test_missing_file_reads_as_empty(ledger_path: Path):
    ledger = Ledger(ledger_path)
    assert ledger.load() == {}
    assert len(ledger) == 0
    assert ledger.counts() == {status: 0 for status in STATUSES}


def test_load_returns_records_with_typed_timestamps(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(url="https://useme.com/a", title="T")], now=NOW)
    record = _record(ledger, "useme:145473")
    assert record.status == STATUS_NEW
    assert record.first_seen_at == NOW
    assert record.url == "https://useme.com/a"
    assert record.in_report_at is None
    assert record.draft is None


def test_creates_missing_parent_dirs(tmp_path: Path):
    ledger = Ledger(tmp_path / "nested" / "deep" / "ledger.json")
    ledger.add([_seed()], now=NOW)
    assert ledger.get("useme:145473") is not None


# ---------------------------------------------------------------- adding


def test_add_returns_only_newly_created_keys_in_input_order(ledger_path: Path):
    ledger = Ledger(ledger_path)
    added = ledger.add([_seed(source_id="2"), _seed(source_id="1")], now=NOW)
    assert added == ("useme:2", "useme:1")
    assert ledger.add([_seed(source_id="1"), _seed(source_id="3")], now=NOW) == ("useme:3",)


def test_add_never_resurrects_a_decided_or_expired_offer(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1"), _seed(source_id="2")], now=NOW)
    ledger.mark_status(["useme:1"], STATUS_REPLIED, now=NOW)
    ledger.mark_status(["useme:2"], STATUS_EXPIRED, now=NOW)

    assert ledger.add([_seed(source_id="1"), _seed(source_id="2")], now=NOW) == ()
    assert _record(ledger, "useme:1").status == STATUS_REPLIED
    assert _record(ledger, "useme:2").status == STATUS_EXPIRED


def test_add_ignores_empty_input_without_touching_the_file(ledger_path: Path):
    ledger = Ledger(ledger_path)
    assert ledger.add([], now=NOW) == ()
    assert not ledger_path.exists()


def test_add_rejects_plain_values(ledger_path: Path):
    with pytest.raises(TypeError):
        Ledger(ledger_path).add(["useme:1"])  # type: ignore[list-item]


def test_add_writes_exactly_once_for_a_batch(ledger_path: Path, monkeypatch):
    real_replace = os.replace
    names = []

    def fake_replace(src, dst):
        names.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    Ledger(ledger_path).add([_seed(source_id="1"), _seed(source_id="2")], now=NOW)
    assert len(names) == 1
    assert names[0].startswith(ledger_path.name + ".")


def test_written_file_is_sorted_json_with_iso_timestamps(ledger_path: Path):
    Ledger(ledger_path).add([_seed(source_id="2"), _seed(source_id="1")], now=NOW)
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert list(data) == ["useme:1", "useme:2"]
    assert data["useme:1"]["first_seen_at"] == "2026-10-07T05:30:00Z"
    assert data["useme:1"]["status"] == STATUS_NEW


def test_replace_failure_cleans_temp_files(ledger_path: Path, monkeypatch):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1")], now=NOW)
    original = ledger_path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ledger.add([_seed(source_id="2")], now=NOW)
    assert list(ledger_path.parent.glob(ledger_path.name + ".*")) == []
    assert ledger_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------- statuses


def test_mark_status_moves_offers_and_stamps_the_change(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1"), _seed(source_id="2")], now=NOW)
    later = NOW + timedelta(hours=2)

    assert ledger.mark_status(["useme:1"], STATUS_REPLIED, now=later) == ("useme:1",)
    record = _record(ledger, "useme:1")
    assert record.status == STATUS_REPLIED
    assert record.status_changed_at == later
    assert record.decided is True
    assert _record(ledger, "useme:2").status == STATUS_NEW


def test_mark_status_is_a_noop_when_the_status_already_matches(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1")], now=NOW)
    ledger.mark_status(["useme:1"], STATUS_SKIPPED, now=NOW)
    assert ledger.mark_status(["useme:1"], STATUS_SKIPPED, now=NOW + timedelta(days=3)) == ()
    assert _record(ledger, "useme:1").status_changed_at == NOW


def test_mark_status_rejects_an_unknown_key_and_writes_nothing(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1")], now=NOW)
    original = ledger_path.read_text(encoding="utf-8")

    with pytest.raises(KeyError):
        ledger.mark_status(["useme:1", "useme:404"], STATUS_REPLIED, now=NOW)
    assert ledger_path.read_text(encoding="utf-8") == original
    assert _record(ledger, "useme:1").status == STATUS_NEW


def test_mark_status_rejects_an_unknown_status(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1")], now=NOW)
    with pytest.raises(ValueError):
        ledger.mark_status(["useme:1"], "answered", now=NOW)


def test_mark_status_with_no_keys_is_silent(ledger_path: Path):
    assert Ledger(ledger_path).mark_status([], STATUS_REPLIED, now=NOW) == ()


def test_mark_delivered_stamps_in_report_at_and_only_touches_new_rows(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1"), _seed(source_id="2")], now=NOW)
    ledger.mark_status(["useme:2"], STATUS_SKIPPED, now=NOW)
    sent = NOW + timedelta(minutes=15)

    assert ledger.mark_delivered(["useme:1", "useme:2"], now=sent) == ("useme:1",)
    delivered = _record(ledger, "useme:1")
    assert delivered.status == STATUS_SEEN
    assert delivered.in_report_at == sent
    assert delivered.status_changed_at == sent
    assert _record(ledger, "useme:2").status == STATUS_SKIPPED


def test_mark_delivered_rejects_an_unknown_key(ledger_path: Path):
    with pytest.raises(KeyError):
        Ledger(ledger_path).mark_delivered(["useme:404"], now=NOW)


def test_by_status_lists_oldest_first_and_honours_the_limit(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1")], now=_days_ago(3))
    ledger.add([_seed(source_id="2")], now=_days_ago(1))
    ledger.add([_seed(source_id="3")], now=NOW)
    ledger.mark_status(["useme:2"], STATUS_REPLIED, now=NOW)

    assert [row.key for row in ledger.by_status(STATUS_NEW)] == ["useme:1", "useme:3"]
    assert [row.key for row in ledger.by_status(STATUS_NEW, limit=1)] == ["useme:1"]
    assert [row.key for row in ledger.by_status(STATUS_REPLIED)] == ["useme:2"]
    with pytest.raises(ValueError):
        ledger.by_status(STATUS_NEW, limit=-1)


def test_counts_covers_every_status(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id=str(i)) for i in range(4)], now=NOW)
    ledger.mark_status(["useme:0"], STATUS_SEEN, now=NOW)
    ledger.mark_status(["useme:1"], STATUS_REPLIED, now=NOW)
    ledger.mark_status(["useme:2"], STATUS_SKIPPED, now=NOW)
    assert ledger.counts() == {
        STATUS_NEW: 1,
        STATUS_SEEN: 1,
        STATUS_REPLIED: 1,
        STATUS_SKIPPED: 1,
        STATUS_EXPIRED: 0,
    }


# ---------------------------------------------------------------- drafts


def test_set_draft_stores_text_version_and_timestamp(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed()], now=NOW)
    record = ledger.set_draft(
        "useme:145473", "Cześć, mam gotowy pomysł…", rules_version="2026-10-07.1", now=NOW
    )
    assert record.draft == "Cześć, mam gotowy pomysł…"
    assert record.draft_at == NOW
    assert record.rules_version == "2026-10-07.1"
    assert _record(ledger, "useme:145473").draft == "Cześć, mam gotowy pomysł…"


def test_set_draft_keeps_the_previous_rules_version_when_omitted(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed()], now=NOW)
    ledger.set_draft("useme:145473", "pierwsza", rules_version="2026-10-07.1", now=NOW)
    record = ledger.set_draft("useme:145473", "druga", now=NOW + timedelta(hours=1))
    assert record.draft == "druga"
    assert record.rules_version == "2026-10-07.1"


def test_set_draft_rejects_unknown_offer_and_blank_text(ledger_path: Path):
    ledger = Ledger(ledger_path)
    with pytest.raises(KeyError):
        ledger.set_draft("useme:404", "cokolwiek", now=NOW)
    ledger.add([_seed()], now=NOW)
    with pytest.raises(ValueError):
        ledger.set_draft("useme:145473", "   ", now=NOW)


# ---------------------------------------------------------------- expiry


def test_expire_retires_only_undecided_old_offers(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add(
        [
            _seed(source_id="old-new"),
            _seed(source_id="old-seen"),
            _seed(source_id="old-replied"),
        ],
        now=_days_ago(30),
    )
    ledger.mark_status(["useme:old-seen"], STATUS_SEEN, now=_days_ago(29))
    ledger.mark_status(["useme:old-replied"], STATUS_REPLIED, now=_days_ago(29))
    ledger.add([_seed(source_id="fresh")], now=NOW)

    expired = ledger.expire(now=NOW)
    assert set(expired) == {"useme:old-new", "useme:old-seen"}
    assert _record(ledger, "useme:old-new").status == STATUS_EXPIRED
    assert _record(ledger, "useme:old-seen").status == STATUS_EXPIRED
    assert _record(ledger, "useme:old-replied").status == STATUS_REPLIED
    assert _record(ledger, "useme:fresh").status == STATUS_NEW


def test_expire_honours_a_custom_ttl_and_defaults_to_21_days(ledger_path: Path):
    assert DEFAULT_EXPIRY_DAYS == 21
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="1")], now=_days_ago(22))
    ledger.add([_seed(source_id="2")], now=_days_ago(20))
    ledger.add([_seed(source_id="3")], now=_days_ago(8))

    assert ledger.expire(now=NOW) == ("useme:1",)
    assert ledger.expire(max_age_days=10, now=NOW) == ("useme:2",)
    with pytest.raises(ValueError):
        ledger.expire(max_age_days=-1, now=NOW)


def test_expire_przyjmuje_godziny_i_pomija_niedostarczone(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.add([_seed(source_id="fresh")], now=NOW - timedelta(hours=10))
    ledger.add([_seed(source_id="old")], now=NOW - timedelta(hours=20))
    ledger.add([_seed(source_id="hidden")], now=NOW - timedelta(hours=20))
    ledger.mark_delivered(["useme:fresh", "useme:old"], now=NOW - timedelta(hours=1))

    assert ledger.expire(max_age_hours=12, only_delivered=True, now=NOW) == ("useme:old",)
    assert _record(ledger, "useme:fresh").status == STATUS_SEEN
    assert _record(ledger, "useme:hidden").status == STATUS_NEW
    with pytest.raises(ValueError):
        ledger.expire(max_age_hours=-1, now=NOW)


def test_expire_is_silent_on_a_fresh_ledger(ledger_path: Path):
    assert Ledger(ledger_path).expire(now=NOW) == ()


# ---------------------------------------------------------------- migration


def test_migrate_from_state_seeds_offers_as_seen_and_skips_urls(tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = SeenStore(state_path)
    store.add_buckets(
        {
            "useme": ["145473", "145474"],
            "facebook_groups": ["99"],
            URLS_BUCKET: ["https://useme.com/pl/jobs/x,145473/"],
        }
    )

    ledger = Ledger(tmp_path / "ledger.json")
    added = ledger.migrate_from_state(state_path, now=NOW)

    assert set(added) == {"useme:145473", "useme:145474", "facebook_groups:99"}
    assert ledger.counts()[STATUS_SEEN] == 3
    assert ledger.counts()[STATUS_NEW] == 0
    assert _record(ledger, "useme:145473").in_report_at == NOW
    assert all(not record.key.startswith("__") for record in ledger.by_status(STATUS_SEEN))


def test_migrate_from_state_is_idempotent_and_keeps_decisions(tmp_path: Path):
    state_path = tmp_path / "state.json"
    SeenStore(state_path).add("useme", "1")
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.add([_seed(source_id="2")], now=NOW)
    ledger.mark_status(["useme:2"], STATUS_REPLIED, now=NOW)

    assert ledger.migrate_from_state(state_path, now=NOW) == ("useme:1",)
    assert ledger.migrate_from_state(state_path, now=NOW) == ()
    assert _record(ledger, "useme:2").status == STATUS_REPLIED


def test_migrate_from_a_missing_state_file_adds_nothing(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    assert ledger.migrate_from_state(tmp_path / "nope.json", now=NOW) == ()
    assert not ledger.path.exists()


# ---------------------------------------------------------------- corruption


def test_malformed_json_raises_corruption_error(ledger_path: Path):
    ledger_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        Ledger(ledger_path).load()


def test_non_object_json_raises_corruption_error(ledger_path: Path):
    ledger_path.write_text(json.dumps(["useme:1"]), encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        Ledger(ledger_path).load()


def test_non_object_entry_raises_corruption_error(ledger_path: Path):
    ledger_path.write_text(json.dumps({"useme:1": "new"}), encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        Ledger(ledger_path).load()


def test_unknown_status_in_file_raises_corruption_error(ledger_path: Path):
    ledger_path.write_text(json.dumps({"useme:1": {"status": "answered"}}), encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        Ledger(ledger_path).load()


def test_bad_timestamp_in_file_raises_corruption_error(ledger_path: Path):
    ledger_path.write_text(
        json.dumps({"useme:1": {"status": "new", "first_seen_at": "wczoraj"}}), encoding="utf-8"
    )
    with pytest.raises(LedgerCorruptionError):
        Ledger(ledger_path).load()


def test_entry_disagreeing_with_its_key_raises_corruption_error(ledger_path: Path):
    ledger_path.write_text(
        json.dumps({"useme:1": {"status": "new", "source": "facebook_groups"}}), encoding="utf-8"
    )
    with pytest.raises(LedgerCorruptionError):
        Ledger(ledger_path).load()


def test_corrupt_ledger_is_not_silently_overwritten(ledger_path: Path):
    ledger_path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(LedgerCorruptionError):
        Ledger(ledger_path).add([_seed()], now=NOW)
    assert ledger_path.read_text(encoding="utf-8") == "{ broken"


def test_offer_record_rejects_a_mismatched_key():
    with pytest.raises(ValueError):
        OfferRecord(key="useme:1", source="useme", source_id="2")
    with pytest.raises(ValueError):
        OfferRecord(key="useme:1", source="useme", source_id="1", status="answered")


# ---------------------------------------------------------------- concurrency


def test_add_blocks_while_the_lock_is_held(ledger_path: Path):
    ledger = Ledger(ledger_path)
    ledger.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger.lock_path.open("a+", encoding="utf-8") as lock_file:
        flock(lock_file, LOCK_EX)
        result: dict[str, object] = {}

        def do_add() -> None:
            ledger.add([_seed()], now=NOW)
            result["key"] = ledger.get("useme:145473")

        thread = threading.Thread(target=do_add)
        thread.start()
        time.sleep(0.25)
        assert result == {}
        assert thread.is_alive()
        flock(lock_file, LOCK_UN)
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert result["key"] is not None


def _concurrent_worker(path: Path, source: str, start: int, barrier) -> None:
    ledger = Ledger(path)
    barrier.wait()
    for index in range(start, start + 20):
        ledger.add([_seed(source=source, source_id=str(index))], now=NOW)
        time.sleep(0.0005)


def test_two_concurrent_writers_lose_no_updates(tmp_path: Path):
    path = tmp_path / "nested" / "ledger" / "ledger.json"
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    p1 = ctx.Process(target=_concurrent_worker, args=(path, "useme", 0, barrier))
    p2 = ctx.Process(target=_concurrent_worker, args=(path, "facebook_groups", 100, barrier))
    p1.start()
    p2.start()
    p1.join(60)
    p2.join(60)
    for process in (p1, p2):
        if process.is_alive():  # pragma: no cover - only on a hung child
            process.terminate()
        assert process.exitcode == 0, process.exitcode

    ledger = Ledger(path)
    useme = {record.key for record in ledger.by_status(STATUS_NEW) if record.source == "useme"}
    facebook = {
        record.key for record in ledger.by_status(STATUS_NEW) if record.source == "facebook_groups"
    }
    assert useme == {f"useme:{index}" for index in range(20)}
    assert facebook == {f"facebook_groups:{index}" for index in range(100, 120)}
    assert len(ledger) == 40
