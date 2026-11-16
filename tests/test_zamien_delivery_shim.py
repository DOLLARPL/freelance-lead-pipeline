"""Offline tests for the ZAMIEN Telegram shim (Jan's second message, 2026-09-21).

The shim is the delivery contract for the fourth bucket: a separate Telegram
message carrying the offers where Jan proposes automation instead of the hire
the client asked about. It is deliberately dumb — it prints the newest run's
``zamien-*.txt`` and stays silent otherwise — so these tests pin the four states
that matter: no file, empty file, fresh file with rows, and a stale file left
behind by an older (or failed) run.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

SHIM = Path("/opt/data/scripts/snajper-zamien-message.sh")


def run_shim(state_dir: Path, **env_overrides: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ["PATH"],
        "SNAJPER_STATE_DIR": str(state_dir),
        **env_overrides,
    }
    return subprocess.run(
        [str(SHIM)], env=env, text=True, capture_output=True, check=False
    )


def write_report(state_dir: Path, name: str, text: str, *, age_seconds: int = 0) -> Path:
    reports = state_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / name
    path.write_text(text, encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


@pytest.mark.skipif(not SHIM.is_file(), reason="shim lives outside the repo (Hermes scripts dir)")
def test_no_report_file_means_silence(tmp_path: Path):
    result = run_shim(tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.skipif(not SHIM.is_file(), reason="shim lives outside the repo (Hermes scripts dir)")
def test_empty_report_means_silence(tmp_path: Path):
    write_report(tmp_path, "zamien-20260921T102031Z-1.txt", "")
    result = run_shim(tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.skipif(not SHIM.is_file(), reason="shim lives outside the repo (Hermes scripts dir)")
def test_fresh_report_is_printed_verbatim(tmp_path: Path):
    text = "SNAJPER v2 — ZAMIEN: zaproponuj automatyzację zamiast zatrudnienia\n\n[ZAMIEN] 100/100 — test\n"
    write_report(tmp_path, "zamien-20260921T102031Z-1.txt", text)
    result = run_shim(tmp_path)
    assert result.returncode == 0
    assert result.stdout == text


@pytest.mark.skipif(not SHIM.is_file(), reason="shim lives outside the repo (Hermes scripts dir)")
def test_newest_of_several_reports_wins(tmp_path: Path):
    write_report(tmp_path, "zamien-20260920T053008Z-1.txt", "stary przebieg\n", age_seconds=3600)
    write_report(tmp_path, "zamien-20260921T102031Z-2.txt", "nowy przebieg\n")
    result = run_shim(tmp_path)
    assert result.stdout == "nowy przebieg\n"


@pytest.mark.skipif(not SHIM.is_file(), reason="shim lives outside the repo (Hermes scripts dir)")
def test_stale_report_is_not_sent_again(tmp_path: Path):
    """A failed main run must not re-send yesterday's ZAMIEN list."""
    write_report(tmp_path, "zamien-20260920T053008Z-1.txt", "stary przebieg\n", age_seconds=3 * 3600)
    result = run_shim(tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.skipif(not SHIM.is_file(), reason="shim lives outside the repo (Hermes scripts dir)")
def test_max_age_can_be_configured(tmp_path: Path):
    write_report(tmp_path, "zamien-20260921T102031Z-1.txt", "raport\n", age_seconds=2 * 3600)
    assert run_shim(tmp_path).stdout == ""
    assert run_shim(tmp_path, SNAJPER_ZAMIEN_MAX_AGE_MINUTES="600").stdout == "raport\n"
