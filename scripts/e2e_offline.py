"""Offline, fixture-driven double-run E2E for Snajper v2.

Runs the full production pipeline (collection -> failure isolation -> staged
dedup -> deterministic scoring -> report -> file delivery -> atomic state
commit) twice against canned fixtures, using a caller-supplied safe state path.

    uv run python scripts/e2e_offline.py --state /tmp/snajper-e2e/state.json \
        --report /tmp/snajper-e2e/report.txt

Run 1 must produce new candidates and a non-empty report; run 2 over the same
state must find zero new candidates (zero duplicates) and leave the report
empty. The script exits non-zero if either invariant fails.

No production cache, cron, Telegram channel or secret file is ever touched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Snajper v2 pipeline twice offline and verify zero duplicates on run 2."
    )
    parser.add_argument("--state", required=True, help="caller-supplied safe state path (JSON)")
    parser.add_argument("--report", default=None, help="report sink file; defaults to <state>.report.txt")
    parser.add_argument(
        "--fixtures",
        default=str(DEFAULT_FIXTURES),
        help="directory with the offline fixture payloads",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state = Path(args.state)
    fixtures = Path(args.fixtures)
    if not fixtures.is_dir():
        print(f"e2e: fixtures directory not found: {fixtures}", file=sys.stderr)
        return 2
    report = Path(args.report) if args.report else state.with_suffix(".e2e-report.txt")

    from freelance_pipeline.cli import main as cli_main

    first_exit = cli_main(
        ["run", "--fixtures", str(fixtures), "--state", str(state), "--report", str(report)]
    )
    first_report = _read_text_or_empty(report)
    print(f"e2e: run 1 exit={first_exit}, report size={len(first_report)}")

    second_exit = cli_main(
        ["run", "--fixtures", str(fixtures), "--state", str(state), "--report", str(report)]
    )
    if second_exit != 0:
        print(f"e2e: run 2 exited {second_exit}; expected 0", file=sys.stderr)
        return 1
    second_report = _read_text_or_empty(report)
    if not first_report:
        print("e2e: run 1 produced no report (expected new offers)", file=sys.stderr)
        return 1
    if second_report:
        print("e2e: run 2 produced duplicates (report not empty)", file=sys.stderr)
        return 1
    print("e2e: OK — run 2 found zero new candidates; state committed atomically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())