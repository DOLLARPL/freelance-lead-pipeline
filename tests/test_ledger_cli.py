"""Oznaczanie z czatu: `scripts/ledger.py` + mapa numerów z rolling listy."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_pipeline.ledger import STATUS_NEW, STATUS_REPLIED, STATUSES, Ledger
from freelance_pipeline.rolling import (
    LeadRow,
    RollingEntry,
    RollingError,
    numbering,
    resolve_numbers,
)

SCRIPT = Path(__file__).parents[1] / "scripts" / "ledger.py"
NOW = datetime(2026, 10, 7, 6, 30, tzinfo=UTC)


def entry(index: int) -> RollingEntry:
    row = LeadRow(
        key=f"useme:{index}",
        source="useme",
        source_id=str(index),
        url=f"https://useme.com/pl/jobs/x,{index}/",
        title=f"Zlecenie {index}",
        author="Klient",
        description="Opis",
        bucket="TOP",
        score=90,
    )
    return RollingEntry(row=row, first_seen_at=NOW, draft_text="Cześć,\n\ntreść")


def state_dir(tmp_path: Path, count: int = 3) -> Path:
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.add([item.row.as_seed() for item in (entry(index) for index in range(1, count + 1))], now=NOW)
    reports = tmp_path / "reports"
    reports.mkdir()
    mapping = numbering([entry(index) for index in range(1, count + 1)])
    (reports / "rolling-20261007T063000Z.keys.json").write_text(
        json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--state-dir", str(tmp_path), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_numbering_follows_the_list_order():
    mapping = numbering([entry(3), entry(1)])
    assert mapping == {"1": "useme:3", "2": "useme:1"}


def test_resolve_numbers_deduplicates_and_rejects_unknown():
    mapping = {"1": "useme:1", "2": "useme:2"}
    assert resolve_numbers(["2", "1", "2"], mapping) == ["useme:2", "useme:1"]
    with pytest.raises(RollingError, match="nie znam numeru"):
        resolve_numbers(["9"], mapping)


def test_marking_changes_the_status(tmp_path: Path):
    state = state_dir(tmp_path)
    result = run(state, "oznacz", "--status", "replied", "1", "3")
    assert result.returncode == 0
    assert "oznaczone jako replied: 2" in result.stdout
    ledger = Ledger(state / "ledger.json")
    assert ledger.get("useme:1").status == STATUS_REPLIED
    assert ledger.get("useme:2").status == STATUS_NEW


def test_marking_the_same_offer_twice_is_a_noop(tmp_path: Path):
    state = state_dir(tmp_path)
    run(state, "oznacz", "--status", "skipped", "2")
    again = run(state, "oznacz", "--status", "skipped", "2")
    assert again.returncode == 0
    assert "nic do zmiany" in again.stdout


def test_unknown_number_is_a_loud_error(tmp_path: Path):
    state = state_dir(tmp_path, count=2)
    result = run(state, "oznacz", "--status", "replied", "7")
    assert result.returncode == 1
    assert "nie znam numeru" in result.stderr
    assert Ledger(state / "ledger.json").get("useme:1").status == STATUS_NEW


def test_marking_without_a_list_is_reported(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    ledger.add([entry(1).row.as_seed()], now=NOW)
    result = run(tmp_path, "oznacz", "--status", "replied", "1")
    assert result.returncode == 1
    assert "nie ma mapy numerów" in result.stderr


def test_pokaz_lists_a_status_with_counters(tmp_path: Path):
    state = state_dir(tmp_path)
    run(state, "oznacz", "--status", "replied", "1")
    result = run(state, "pokaz", "--status", "replied")
    assert result.returncode == 0
    assert "replied: 1" in result.stdout
    assert "useme:1" in result.stdout
    assert "✓" in result.stdout


@pytest.mark.parametrize("status", list(STATUSES))
def test_every_status_is_selectable_in_pokaz(tmp_path: Path, status: str):
    state = state_dir(tmp_path, count=1)
    result = run(state, "pokaz", "--status", status)
    assert result.returncode == 0
    # świeżo dodana oferta jest `new`, więc liczymy tylko to, że polecenie działa na każdym statusie
    assert f"{status}: " in result.stdout


def test_only_decisive_statuses_can_be_marked_by_hand(tmp_path: Path):
    state = state_dir(tmp_path, count=1)
    result = run(state, "oznacz", "--status", "new", "1")
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


# ------------------------------------------------- „odpisałem na wszystkie" (2026-10-16)


def seen_state_dir(tmp_path: Path, count: int = 3) -> Path:
    """Stan po wysłanej liście: oferty są `seen`, czekają na decyzję."""
    state = state_dir(tmp_path, count=count)
    ledger = Ledger(state / "ledger.json")
    ledger.mark_status([f"useme:{index}" for index in range(1, count + 1)], "seen", now=NOW)
    return state


def test_all_marks_every_offer_waiting_for_a_decision(tmp_path: Path):
    state = seen_state_dir(tmp_path)
    result = run(state, "oznacz", "--status", "replied", "--all")
    assert result.returncode == 0
    assert "oznaczone jako replied: 3" in result.stdout
    ledger = Ledger(state / "ledger.json")
    assert ledger.counts()["replied"] == 3
    assert ledger.counts()["seen"] == 0


def test_the_word_wszystkie_works_like_the_flag(tmp_path: Path):
    state = seen_state_dir(tmp_path, count=2)
    result = run(state, "oznacz", "--status", "replied", "wszystkie")
    assert result.returncode == 0
    assert "oznaczone jako replied: 2" in result.stdout
    assert Ledger(state / "ledger.json").counts()["replied"] == 2


def test_all_never_touches_offers_not_yet_shown(tmp_path: Path):
    """`new` = jeszcze nie wysłane, więc nie ma czego oznaczać jako odpisane."""
    state = state_dir(tmp_path, count=2)
    result = run(state, "oznacz", "--status", "replied", "--all")
    assert result.returncode == 0
    assert "nie ma ofert czekających na decyzję" in result.stdout
    assert Ledger(state / "ledger.json").counts()["new"] == 2


def test_without_numbers_and_without_all_it_says_so(tmp_path: Path):
    state = seen_state_dir(tmp_path, count=1)
    result = run(state, "oznacz", "--status", "replied")
    assert result.returncode == 2
    assert "albo --all" in result.stderr
    assert Ledger(state / "ledger.json").counts()["seen"] == 1
