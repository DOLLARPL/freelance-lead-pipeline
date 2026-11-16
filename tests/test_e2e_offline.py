"""Offline E2E regression: running the full fixture-driven pipeline twice over
a caller-supplied state path — first run produces a report, second run finds
zero duplicates — exercising the real adapters, dedupe, deterministic scoring
and atomic commit through the production CLI entrypoint."""

from __future__ import annotations

from pathlib import Path

from scripts.e2e_offline import main as e2e_main

FIXTURES = Path(__file__).parent / "fixtures"


def test_offline_e2e_double_run_produces_zero_duplicates(tmp_path: Path):
    # e2e_main already runs the pipeline twice and asserts both invariants:
    # run 1 produces a report, run 2 finds zero new candidates.
    assert e2e_main(["--state", str(tmp_path / "state.json"), "--report", str(report := tmp_path / "report.txt"), "--fixtures", str(FIXTURES)]) == 0
    assert report.exists()
    # After the empty second run the report sink reflects "zero new".
    assert report.read_text(encoding="utf-8") == ""


def test_offline_e2e_missing_fixtures_is_usage_error(tmp_path: Path, capsys):
    state = tmp_path / "state.json"
    rc = e2e_main(["--state", str(state), "--fixtures", str(tmp_path / "nope")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_offline_adapters_cover_all_registry_sources():
    from freelance_pipeline.offline import build_offline_adapters

    adapters = build_offline_adapters(FIXTURES)
    from freelance_pipeline.registry import DEFAULT_ADAPTERS

    assert set(adapters) == set(DEFAULT_ADAPTERS)
    for source in adapters.values():
        assert source.collect()  # every fixture yields leads offline


def test_cli_requires_state(tmp_path: Path, capsys):
    from freelance_pipeline.cli import main as cli_main

    rc = cli_main(["run", "--fixtures", str(FIXTURES)])
    assert rc == 2
    assert "--state" in capsys.readouterr().err


def test_cli_module_executes_entrypoint():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "freelance_pipeline.cli", "run"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--state is required" in result.stderr


def test_telegram_flags_validate_config_but_never_send(tmp_path: Path, capsys):
    from freelance_pipeline.cli import main as cli_main

    state = tmp_path / "state.json"
    rc = cli_main(
        [
            "run",
            "--fixtures",
            str(FIXTURES),
            "--state",
            str(state),
            "--report",
            str(tmp_path / "r.txt"),
            "--telegram-token",
            "t0ken",
            "--telegram-chat-id",
            "-100123",
            "-v",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "dry-run" in err and "no message will be sent" in err


def test_telegram_bad_chat_id_is_rejected(tmp_path: Path, capsys):
    from freelance_pipeline.cli import main as cli_main

    rc = cli_main(
        [
            "run",
            "--fixtures",
            str(FIXTURES),
            "--state",
            str(tmp_path / "state.json"),
            "--telegram-token",
            "tok",
            "--telegram-chat-id",
            "abc",
        ]
    )
    assert rc == 2
    assert "numeric" in capsys.readouterr().err