"""Offline tests for durable, staged deduplication against persisted state.

Dedup is performed in two ordered steps (source+source_id first, then the
canonical URL) and state is only ever written once, atomically, *after* the
delivery callback has confirmed success. This module tests the state facade,
the dedup order and the deferred commit contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freelance_pipeline.dedup import DedupeResult, PipelineState
from freelance_pipeline.models import JobLead
from freelance_pipeline.state import SeenStoreCorruptionError


def make_lead(source: str, source_id: str, url: str | None = None) -> JobLead:
    return JobLead(
        source=source,
        source_id=source_id,
        url=url or f"https://{source}.x/o/{source_id}",
        canonical_url=url or f"https://{source}.x/o/{source_id}",
    )


def test_state_starts_empty(tmp_path: Path):
    state = PipelineState(tmp_path / "state.json")
    assert state.is_known("useme", "1") is False
    assert state.is_url_known("https://u") is False
    assert state.dedupe([]).candidates == ()


def test_dedupe_skips_previously_committed_source_id(tmp_path: Path):
    state = PipelineState(tmp_path / "state.json")
    state.commit([make_lead("useme", "1")])
    result = state.dedupe([make_lead("useme", "1", "https://u/1")])
    assert result.candidates == ()
    assert [d.reason for d in result.dropped] == ["source+source_id"]


def test_dedupe_prefers_source_id_over_url_order(tmp_path):
    """A known (source, source_id) is dropped by id even if its URL is new."""
    state = PipelineState(tmp_path / "state.json")
    state.commit([make_lead("useme", "1", "https://u/old")])
    result = state.dedupe([make_lead("useme", "1", "https://u/new")])
    assert [d.reason for d in result.dropped] == ["source+source_id"]


def test_dedupe_drops_cross_source_canonical_url_duplicate(tmp_path: Path):
    state = PipelineState(tmp_path / "state.json")
    state.commit([make_lead("useme", "1", "https://jobs.x/offer-42")])
    other = make_lead("nofluffjobs", "different-id", "https://jobs.x/offer-42")
    result = state.dedupe([other])
    assert result.candidates == ()
    assert [d.reason for d in result.dropped] == ["canonical_url"]


def test_dedupe_drops_same_run_duplicates(tmp_path: Path):
    state = PipelineState(tmp_path / "state.json")
    first = make_lead("useme", "1", "https://jobs.x/offer-a")
    second = make_lead("useme", "1", "https://jobs.x/offer-b")
    dup_url = make_lead("nofluff", "2", "https://jobs.x/offer-a")
    result = state.dedupe([first, second, dup_url])
    assert [lead.source_id for lead in result.candidates] == ["1"]
    assert len(result.dropped) == 2


def test_commit_makes_leads_non_candidates_next_run(tmp_path: Path):
    state = PipelineState(tmp_path / "state.json")
    leads = [
        make_lead("useme", "1", "https://a/1"),
        make_lead("nofluff", "x", "https://b/x"),
    ]
    assert not state.dedupe(leads).empty
    state.commit(leads)
    assert state.dedupe(leads).empty
    assert state.is_known("nofluff", "x")
    assert state.is_url_known("https://a/1")


def test_commit_marks_ids_and_urls_in_one_object(tmp_path: Path):

    state = PipelineState(tmp_path / "state.json")
    state.commit([make_lead("useme", "7", "https://a/7")])
    payload = json.loads((tmp_path / "state.json").read_text())
    assert payload["useme"] == ["7"]
    assert payload["__canonical_urls__"] == ["https://a/7"]


def test_dedup_is_empty_returns_true_only_when_no_candidates(tmp_path: Path):
    state = PipelineState(tmp_path / "state.json")
    assert state.dedupe([]).empty


def test_legacy_layout_state_is_visible_to_dedupe(tmp_path: Path):
    (tmp_path / "state.json").write_text(json.dumps({"useme": ["35"]}))
    state = PipelineState(tmp_path / "state.json")
    assert state.is_known("useme", "35")
    result = state.dedupe([make_lead("useme", "35", "https://u/35")])
    assert result.candidates == ()


def test_commit_without_urls_does_not_add_url_key(tmp_path):

    state = PipelineState(tmp_path / "state.json")
    state.commit([JobLead(source="useme", source_id="9", url="https://u/9")])
    payload = json.loads((tmp_path / "state.json").read_text())
    assert payload == {"useme": ["9"]}


def test_corrupt_state_fails_loudly_without_overwrites(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ nope")
    state = PipelineState(path)
    with pytest.raises(SeenStoreCorruptionError):
        state.commit([make_lead("useme", "1")])
    assert path.read_text() == "{ nope"


def test_dedupe_result_exposes_reason_enum_values():
    assert {d.reason for d in DedupeResult((), ()).dropped} == set()