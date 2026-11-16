"""Print the newest Snajper v2 report of a given kind (no LLM, no network).

Usage::

    uv run python scripts/show_report.py --kind moze
    uv run python scripts/show_report.py --kind top --limit 10
    uv run python scripts/show_report.py --kind rejected --limit 25

Reads the per-run files written by the production wrapper. Both the production
report directory and the verification copy are scanned; the newest matching file
wins, and the header says which run it came from.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from freelance_pipeline.poc import load_backlog, render_backlog

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIRS = (
    Path("/opt/data/cache/snajper-v2/reports"),
    Path("/opt/data/cache/snajper-v2/verify/reports"),
)
POC_BACKLOGS = (
    Path("/opt/data/cache/snajper-v2/poc-backlog.json"),
    Path("/opt/data/cache/snajper-v2/verify/poc-backlog.json"),
)
PATTERNS = {
    "top": "top-*.txt",
    "moze": "moze-*.txt",
    "rejected": "rejected-*.txt",
    "chat": "report-*.txt",
    "zamien": "zamien-*.txt",
}
HEADERS = {
    "top": "TOP — wszystkie oferty z przebiegu (to, co nie zmieściło się na Telegramie)",
    "moze": "MOŻE — do przejrzenia (nigdy nie idą na Telegram)",
    "rejected": "ODRZUĆ — audyt odrzuconych",
    "chat": "To, co poszło na Telegram (TOP, maks. 15)",
    "zamien": "ZAMIEN — zaproponuj automatyzację zamiast zatrudnienia (idą osobną wiadomością na Telegram)",
    "poc": "PoC — czego brakuje w portfolio (zbierane z ofert)",
}
ROW_RE = re.compile(r"^\[(TOP|MOŻE|ODRZUĆ|ZAMIEN)\]\s+(\d+)/100\s+—\s+(.*)$")


def newest(kind: str) -> Path | None:
    candidates: list[Path] = []
    for directory in REPORT_DIRS:
        if directory.is_dir():
            candidates.extend(directory.glob(PATTERNS[kind]))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def render(path: Path, kind: str, limit: int | None) -> str:
    lines: list[str] = []
    entries: list[list[str]] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if ROW_RE.match(line):
            current = [line]
            entries.append(current)
        elif current is not None and line.strip():
            current.append(line)
    if limit is not None:
        entries = entries[:limit]
    lines.append(f"# {HEADERS[kind]}")
    lines.append(f"plik: {path}  |  pozycji: {len(entries)}")
    lines.append("")
    for entry in entries:
        for line in entry:
            lines.append(line if line.startswith("[") else line.rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="show_report", description=__doc__)
    parser.add_argument("--kind", choices=[*sorted(PATTERNS), "poc"], required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--all",
        action="store_true",
        help="dla --kind poc: pokaż też luki zgłoszone przez jedną ofertę",
    )
    args = parser.parse_args(argv)

    if args.kind == "poc":
        existing = [path for path in POC_BACKLOGS if path.is_file()]
        newest_backlog = max(existing, key=lambda path: path.stat().st_mtime) if existing else None
        entries = load_backlog(newest_backlog) if newest_backlog else []
        print(render_backlog(entries, min_count=1 if args.all else 2))
        if newest_backlog is not None:
            print(f"\nplik: {newest_backlog}")
        return 0
    path = newest(args.kind)
    if path is None:
        print(f"Brak plików {PATTERNS[args.kind]} — Snajper nie zapisał jeszcze przebiegu.")
        return 1
    print(render(path, args.kind, args.limit), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
