from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from freelance_pipeline.registry import ACTIVE_SOURCES, INACTIVE_SOURCES

WRAPPER = Path(__file__).parents[1] / "scripts" / "run_production.sh"


def _wrapper_sources() -> str:
    match = re.search(r"^SOURCES=(.+)$", WRAPPER.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, "scripts/run_production.sh has no SOURCES= line"
    return match.group(1).strip()


def test_wrapper_sources_match_the_registry():
    """The wrapper flag overrides ``ACTIVE_SOURCES``, so the two must agree.

    They drifted once (the wrapper still collected Oferteo and the English
    sources while the registry said otherwise) and nothing caught it — this is
    the guard that makes the drift impossible to reintroduce silently.
    """
    assert _wrapper_sources() == ",".join(ACTIVE_SOURCES)
    for name in INACTIVE_SOURCES:
        assert name not in ACTIVE_SOURCES


def test_wrapper_loads_key_and_passes_explicit_production_config(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "capture.txt"
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$OPENROUTER_API_KEY\" \"$@\" > \"$CAPTURE\"\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")
    state_dir = tmp_path / "state"

    result = subprocess.run(
        [str(WRAPPER)],
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CAPTURE": str(capture),
            "SNAJPER_ENV_FILE": str(env_file),
            "SNAJPER_STATE_DIR": str(state_dir),
            "SNAJPER_REPO": str(WRAPPER.parents[1]),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[0] == "test-key"
    assert "google/gemini-2.5-flash-lite" in args
    assert "useme,facebook_groups" in args
    assert ",".join(ACTIVE_SOURCES) in args
    assert str(state_dir / "state.json") in args
    report_args = [arg for arg in args if "/reports/report-" in arg]
    audit_args = [arg for arg in args if "/reports/rejected-" in arg]
    moze_args = [arg for arg in args if "/reports/moze-" in arg]
    top_args = [arg for arg in args if "/reports/top-" in arg]
    zamien_args = [arg for arg in args if "/reports/zamien-" in arg]
    assert len(report_args) == 1
    assert len(audit_args) == 1
    assert len(moze_args) == 1
    assert len(top_args) == 1
    assert len(zamien_args) == 1


def test_wrapper_fails_without_openrouter_key(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER=value\n", encoding="utf-8")

    result = subprocess.run(
        [str(WRAPPER)],
        env={
            "PATH": os.environ["PATH"],
            "SNAJPER_ENV_FILE": str(env_file),
            "SNAJPER_STATE_DIR": str(tmp_path / "state"),
            "SNAJPER_REPO": str(WRAPPER.parents[1]),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "OPENROUTER_API_KEY" in result.stderr
    assert "test-key" not in result.stderr
