"""CLI: plik ofert (`--leads-json`) — wejście dla rolling listy."""

from __future__ import annotations

import json
from pathlib import Path

from freelance_pipeline import pipeline
from freelance_pipeline.cli import main as cli_main
from freelance_pipeline.models import JobLead
from freelance_pipeline.orchestrator import CollectionRun
from freelance_pipeline.pipeline import PipelineResult
from freelance_pipeline.scoring import ScoreBucket, ScoredLead

WRAPPER = Path(__file__).parents[1] / "scripts" / "run_production.sh"


def _lead(source_id: str, bucket: ScoreBucket, *, source: str = "useme") -> ScoredLead:
    return ScoredLead(
        JobLead(
            source=source,
            source_id=source_id,
            url=f"https://useme.com/pl/jobs/x,{source_id}/",
            canonical_url=f"https://useme.com/pl/jobs/x,{source_id}/",
            title="Zlecę automatyzację",
            company_or_author="Łukasz",
            description="Klient chce listę firm B2B.",
            budget=1800.0,
            currency="PLN",
        ),
        score=100,
        bucket=bucket,
        rationale="Konkretny zakres od klienta.",
    )


def _result(scored: tuple[ScoredLead, ...]) -> PipelineResult:
    return PipelineResult(
        collection=CollectionRun(results=()),
        candidates=(),
        dropped=(),
        scored=scored,
        report_text="",
        top_report="",
        maybe_report="",
        rejected_audit="",
        zamien_report="",
        reported_count=1,
        committed=True,
    )


def test_leads_json_is_written_for_answerable_buckets(tmp_path: Path, monkeypatch):
    scored = (
        _lead("1", ScoreBucket.TOP),
        _lead("2", ScoreBucket.MOZE),
        _lead("3", ScoreBucket.ZAMIEN),
        _lead("4", ScoreBucket.ODRZUC),
    )
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kwargs: _result(scored))
    leads = tmp_path / "leads-20261007.json"

    rc = cli_main(
        [
            "run",
            "--state",
            str(tmp_path / "state.json"),
            "--report",
            "-",
            "--leads-json",
            str(leads),
        ]
    )

    assert rc == 0
    rows = json.loads(leads.read_text(encoding="utf-8"))
    assert [row["key"] for row in rows] == ["useme:1", "useme:2", "useme:3"]
    assert {row["bucket"] for row in rows} == {"TOP", "MOŻE", "ZAMIEN"}
    assert rows[0]["description"] == "Klient chce listę firm B2B."
    assert rows[0]["url"].endswith(",1/")
    assert rows[0]["budget"] == 1800.0


def test_leads_json_is_skipped_when_not_requested(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kwargs: _result(()))
    rc = cli_main(["run", "--state", str(tmp_path / "state.json"), "--report", "-"])
    assert rc == 0
    assert list(tmp_path.glob("leads-*")) == []


def test_empty_run_writes_an_empty_list(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda **kwargs: _result(()))
    leads = tmp_path / "leads.json"
    cli_main(["run", "--state", str(tmp_path / "state.json"), "--report", "-", "--leads-json", str(leads)])
    assert json.loads(leads.read_text(encoding="utf-8")) == []


def test_rejected_bucket_is_not_written_to_the_answer_file(tmp_path: Path, monkeypatch):
    """ODRZUĆ nie trafia do pliku ofert — na te ogłoszenia nie odpowiadamy."""
    monkeypatch.setattr(
        pipeline, "run_pipeline", lambda **kwargs: _result((_lead("9", ScoreBucket.ODRZUC),))
    )
    leads = tmp_path / "leads.json"
    cli_main(["run", "--state", str(tmp_path / "state.json"), "--report", "-", "--leads-json", str(leads)])
    assert json.loads(leads.read_text(encoding="utf-8")) == []


def test_production_wrapper_passes_the_leads_path():
    text = WRAPPER.read_text(encoding="utf-8")
    assert '--leads-json "$LEADS"' in text
    assert 'LEADS="$STATE_DIR/leads-$RUN_ID.json"' in text
