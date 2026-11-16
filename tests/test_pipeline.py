"""Offline tests for the end-to-end pipeline wiring: collect -> dedupe ->
score -> report -> deliver -> commit, with state updated only after the
delivery callback confirms success and nothing committed on any failure."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_pipeline.dedup import PipelineState
from freelance_pipeline.delivery import DeliveryError, ReportDelivery
from freelance_pipeline.models import JobLead
from freelance_pipeline.openrouter import ScoreError
from freelance_pipeline.pipeline import PipelineResult, run_pipeline
from freelance_pipeline.scoring import DeterministicScorer, ScoreBucket, ScoredLead


def make_lead(source: str, source_id: str, url: str | None = None) -> JobLead:
    return JobLead(
        source=source,
        source_id=source_id,
        url=url or f"https://{source}.x/o/{source_id}",
        canonical_url=url or f"https://{source}.x/o/{source_id}",
        title=f"Oferta {source} {source_id}",
    )


class StubAdapter:
    def __init__(self, source: str, leads: list[JobLead] | None = None, *, error: Exception | None = None) -> None:
        self.source = source
        self.leads = leads if leads is not None else []
        self.error = error

    def collect(self) -> list[JobLead]:
        if self.error is not None:
            raise self.error
        return self.leads


class ExplodingScorer:
    def score(self, leads):
        raise ScoreError("LLM down")


class FixedScorer:
    """Returns one canned bucket per lead, in input order."""

    def __init__(self, buckets: list[ScoreBucket]) -> None:
        self.buckets = buckets

    def score(self, leads):
        return [
            ScoredLead(lead, 90 - index, bucket, f"powód {index}")
            for index, (lead, bucket) in enumerate(zip(leads, self.buckets, strict=True))
        ]


class RecordingDelivery:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_report(self, text: str) -> None:
        self.sent.append(text)


FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def run_with(
    adapters: dict,
    tmp_path: Path,
    *,
    scorer=None,
    delivery_path: str | None = None,
) -> PipelineResult:
    state = PipelineState(tmp_path / "state.json")
    delivery = ReportDelivery(path=delivery_path or str(tmp_path / "report.txt"))
    return run_pipeline(
        adapters=adapters,
        state=state,
        scorer=scorer or DeterministicScorer(),
        delivery=delivery,
        report_now=FIXED_NOW,
    )


def test_happy_path_wires_everything_and_commits_state(tmp_path: Path):
    adapters = {
        "useme": StubAdapter("useme", [make_lead("useme", "1"), make_lead("useme", "2")]),
    }
    result = run_with(adapters, tmp_path)
    assert len(result.candidates) == 2
    assert len(result.scored) == 2
    assert result.reported_count > 0
    assert result.rejected_audit  # deterministic scorer yields all buckets
    assert result.committed
    assert "Oferta useme" in result.report_text
    assert (tmp_path / "report.txt").exists()


def test_second_run_over_same_state_is_zero_new(tmp_path: Path):
    adapters = {"a": StubAdapter("a", [make_lead("a", "x", "https://a/x")])}
    first = run_with(adapters, tmp_path)
    second = run_with(adapters, tmp_path)
    assert len(first.candidates) == 1
    assert second.candidates == ()
    assert second.scored == ()
    assert second.committed is False
    assert second.report_text == ""


def test_failed_source_is_isolated_in_pipeline(tmp_path: Path):
    adapters = {
        "good": StubAdapter("good", [make_lead("good", "1")]),
        "broken": StubAdapter("broken", error=RuntimeError("[broken] nope")),
    }
    result = run_with(adapters, tmp_path)
    assert list(result.collection.successes_sources) == ["good"]
    assert list(result.collection.failed_sources) == ["broken"]
    assert len(result.candidates) == 1
    assert result.committed


def test_scoring_failure_leaves_everything_uncommitted(tmp_path: Path):
    adapters = {"a": StubAdapter("a", [make_lead("a", "1")])}
    state_path = tmp_path / "state.json"
    with pytest.raises(ScoreError):
        run_pipeline(
            adapters=adapters,
            state=PipelineState(state_path),
            scorer=ExplodingScorer(),
            delivery=ReportDelivery(path=str(tmp_path / "report.txt")),
            report_now=FIXED_NOW,
        )
    assert not state_path.exists()


def test_delivery_failure_leaves_everything_uncommitted(tmp_path: Path):
    adapters = {"a": StubAdapter("a", [make_lead("a", "1")])}
    state_path = tmp_path / "state.json"
    (tmp_path / "blocked.txt").write_text("file")
    with pytest.raises(DeliveryError):
        run_pipeline(
            adapters=adapters,
            state=PipelineState(state_path),
            scorer=DeterministicScorer(),
            delivery=ReportDelivery(path=str(tmp_path / "blocked.txt" / "r.txt")),
            report_now=FIXED_NOW,
        )
    assert not state_path.exists()


def test_rejected_audit_failure_leaves_everything_uncommitted(tmp_path: Path):
    adapters = {"a": StubAdapter("a", [make_lead("a", "1")])}
    state_path = tmp_path / "state.json"
    (tmp_path / "blocked.txt").write_text("file")
    with pytest.raises(DeliveryError):
        run_pipeline(
            adapters=adapters,
            state=PipelineState(state_path),
            scorer=DeterministicScorer(),
            delivery=ReportDelivery(path=str(tmp_path / "report.txt")),
            rejected_delivery=ReportDelivery(path=str(tmp_path / "blocked.txt" / "audit.txt")),
            report_now=FIXED_NOW,
        )
    assert not state_path.exists()


def test_cross_source_url_duplicate_in_same_batch_is_committed_once(tmp_path: Path):
    adapters = {
        "useme": StubAdapter("useme", [make_lead("useme", "1", "https://j/x")]),
        "nofluff": StubAdapter("nofluff", [make_lead("nofluff", "g", "https://j/x")]),
    }
    result = run_with(adapters, tmp_path)
    assert len(result.candidates) == 1
    assert list(result.collection.all).__len__() == 2
    assert result.committed
    assert PipelineState(tmp_path / "state.json").is_url_known("https://j/x")


def test_empty_collection_produces_empty_report(tmp_path: Path):
    result = run_with({"a": StubAdapter("a", [])}, tmp_path)
    assert result.candidates == ()
    assert result.report_text == ""
    assert result.committed is False


def test_odrzuc_only_run_still_commits_after_successful_delivery(tmp_path: Path):
    class AllRejectScorer:
        def score(self, leads):
            return [
                ScoredLead(lead, 10, ScoreBucket.ODRZUC, "odrzuć ten")
                for lead in leads
            ]

    adapters = {"a": StubAdapter("a", [make_lead("a", "1")])}
    state = PipelineState(tmp_path / "state.json")
    report = ReportDelivery(path=str(tmp_path / "report.txt"))
    result = run_pipeline(
        adapters=adapters,
        state=state,
        scorer=AllRejectScorer(),
        delivery=report,
        report_now=FIXED_NOW,
    )
    assert result.candidates
    assert result.report_text == ""
    assert result.committed
    assert state.is_known("a", "1")


def test_result_is_reproducible_across_two_processes(tmp_path: Path):
    ref = run_with({"a": StubAdapter("a", [make_lead("a", "b")])}, tmp_path)
    again = run_with({"a": StubAdapter("a", [make_lead("a", "b")])}, tmp_path / "other")
    assert ref.report_text == again.report_text


# ---------- delivery: TOP + MOŻE to the chat, MOŻE also to its own file ----------


def test_top_and_moze_go_to_chat_and_moze_also_to_its_file(tmp_path: Path):
    adapters = {
        "a": StubAdapter(
            "a",
            [
                make_lead("a", "1"),
                make_lead("a", "2"),
                make_lead("a", "3"),
            ],
        )
    }
    chat = RecordingDelivery()
    moze = RecordingDelivery()
    result = run_pipeline(
        adapters=adapters,
        state=PipelineState(tmp_path / "state.json"),
        scorer=FixedScorer([ScoreBucket.TOP, ScoreBucket.MOZE, ScoreBucket.ODRZUC]),
        delivery=chat,
        maybe_delivery=moze,
        report_now=FIXED_NOW,
    )

    assert len(chat.sent) == 1
    assert "[TOP]" in chat.sent[0] and "[MOŻE]" in chat.sent[0]
    assert "Oferta a 1" in chat.sent[0]
    assert "Oferta a 2" in chat.sent[0]  # MOŻE ships to the chat since 2026-09-22
    assert "Oferta a 3" not in chat.sent[0]  # ODRZUĆ still never reaches it
    assert len(moze.sent) == 1
    assert "Oferta a 2" in moze.sent[0]
    assert "Oferta a 1" not in moze.sent[0]
    assert result.report_text == chat.sent[0]
    assert result.maybe_report == moze.sent[0]
    assert result.committed


def test_run_with_only_rejected_leads_sends_an_empty_chat_report(tmp_path: Path):
    chat = RecordingDelivery()
    moze = RecordingDelivery()
    run_pipeline(
        adapters={"a": StubAdapter("a", [make_lead("a", "1")])},
        state=PipelineState(tmp_path / "state.json"),
        scorer=FixedScorer([ScoreBucket.ODRZUC]),
        delivery=chat,
        maybe_delivery=moze,
        report_now=FIXED_NOW,
    )
    assert chat.sent == [""]  # cron wrapper turns this into silence
    assert moze.sent == [""]


def test_maybe_delivery_failure_leaves_everything_uncommitted(tmp_path: Path):
    state_path = tmp_path / "state.json"
    (tmp_path / "blocked.txt").write_text("file")
    with pytest.raises(DeliveryError):
        run_pipeline(
            adapters={"a": StubAdapter("a", [make_lead("a", "1")])},
            state=PipelineState(state_path),
            scorer=FixedScorer([ScoreBucket.TOP]),
            delivery=ReportDelivery(path=str(tmp_path / "report.txt")),
            maybe_delivery=ReportDelivery(path=str(tmp_path / "blocked.txt" / "moze.txt")),
            report_now=FIXED_NOW,
        )
    assert not state_path.exists()


def test_chat_and_archive_both_keep_every_top_row(tmp_path: Path):
    # Since 2026-09-22 the chat report is uncapped (TOP + MOŻE), so the TOP
    # archive is no longer a rescue for rows the chat dropped — both carry the
    # full set, and the archive still holds the TOP-only slice.
    total = 17
    leads = [make_lead("a", str(index)) for index in range(total)]
    chat = RecordingDelivery()
    archive = RecordingDelivery()
    result = run_pipeline(
        adapters={"a": StubAdapter("a", leads)},
        state=PipelineState(tmp_path / "state.json"),
        scorer=FixedScorer([ScoreBucket.TOP] * total),
        delivery=chat,
        top_delivery=archive,
        report_now=FIXED_NOW,
    )

    assert chat.sent[0].count("[TOP]") == total
    assert archive.sent[0].count("[TOP]") == total
    for index in range(total):
        assert f"Oferta a {index}" in archive.sent[0]
        assert f"Oferta a {index}" in chat.sent[0]
    assert result.top_report == archive.sent[0]


def test_poc_backlog_is_written_only_when_requested(tmp_path: Path):
    class GapScorer:
        def score(self, leads):
            return [
                ScoredLead(lead, 80, ScoreBucket.TOP, "pasuje", "brak case study ERP")
                for lead in leads
            ]

    adapters = {"a": StubAdapter("a", [make_lead("a", "1")])}
    without = tmp_path / "bez.json"
    run_pipeline(
        adapters=adapters,
        state=PipelineState(tmp_path / "s1.json"),
        scorer=GapScorer(),
        delivery=RecordingDelivery(),
        report_now=FIXED_NOW,
    )
    assert not without.exists()

    backlog = tmp_path / "poc.json"
    result = run_pipeline(
        adapters=adapters,
        state=PipelineState(tmp_path / "s2.json"),
        scorer=GapScorer(),
        delivery=RecordingDelivery(),
        poc_backlog_path=backlog,
        report_now=FIXED_NOW,
    )
    assert backlog.exists()
    assert result.poc_entries and result.poc_entries[0].gap == "brak case study ERP"


def test_chat_report_never_mentions_poc_gaps(tmp_path: Path):
    class GapScorer:
        def score(self, leads):
            return [ScoredLead(lead, 80, ScoreBucket.MOZE, "słabe", "brak case study mobile") for lead in leads]

    chat = RecordingDelivery()
    run_pipeline(
        adapters={"a": StubAdapter("a", [make_lead("a", "1")])},
        state=PipelineState(tmp_path / "s.json"),
        scorer=GapScorer(),
        delivery=chat,
        poc_backlog_path=tmp_path / "poc.json",
        report_now=FIXED_NOW,
    )
    assert "brak case study mobile" not in "".join(chat.sent)


def test_zamien_report_gets_its_own_file_and_never_the_chat(tmp_path: Path):
    """Fourth bucket (Jan 2026-09-21): measured in a file, silent on the chat."""
    chat = RecordingDelivery()
    zamien = RecordingDelivery()
    result = run_pipeline(
        adapters={"useme": StubAdapter("useme", [make_lead("useme", "1")])},
        state=PipelineState(tmp_path / "state.json"),
        scorer=FixedScorer([ScoreBucket.ZAMIEN]),
        delivery=chat,
        zamien_delivery=zamien,
        report_now=FIXED_NOW,
    )

    assert chat.sent[0] == ""  # zero TOP still means silence
    assert "[ZAMIEN]" in zamien.sent[0]
    assert result.zamien_report == zamien.sent[0]
    assert result.zamien_count == 1
    assert "ZAMIEN" not in result.report_text
    assert "[ZAMIEN]" not in result.rejected_audit


def test_zamien_file_is_written_even_when_there_is_nothing_to_put_in_it(tmp_path: Path):
    zamien = RecordingDelivery()
    result = run_pipeline(
        adapters={"useme": StubAdapter("useme", [make_lead("useme", "1")])},
        state=PipelineState(tmp_path / "state.json"),
        scorer=FixedScorer([ScoreBucket.TOP]),
        delivery=RecordingDelivery(),
        zamien_delivery=zamien,
        report_now=FIXED_NOW,
    )

    assert zamien.sent == [""]
    assert result.zamien_count == 0
