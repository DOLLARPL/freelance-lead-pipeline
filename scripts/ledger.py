"""Oznaczanie ofert w rejestrze: „3 odpisałem", „5 odrzuć", „wszystkie odpisałem".

Użycie::

    cd /opt/data/projects/snajper-ofert
    uv run python scripts/ledger.py pokaz                      # co czeka na decyzję
    uv run python scripts/ledger.py oznacz --status replied 3 7
    uv run python scripts/ledger.py oznacz --status skipped 5
    uv run python scripts/ledger.py oznacz --status replied --all

Numery biorą się z **ostatniej rolling listy** (`rolling-<run>.keys.json` obok pliku listy).
Numer, którego nie ma w tej mapie, to błąd — nigdy ciche „nic się nie stało".
`--all` (albo słowo „wszystkie") bierze wszystkie oferty czekające na decyzję, czyli te
ze statusem `seen`; przydaje się, gdy Jan odpowiada na całą listę naraz.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from freelance_pipeline.ledger import (
    DECIDED_STATUSES,
    STATUS_NEW,
    STATUS_SEEN,
    STATUSES,
    Ledger,
    LedgerCorruptionError,
)
from freelance_pipeline.rolling import RollingError, resolve_numbers

DEFAULT_STATE_DIR = Path("/opt/data/cache/snajper-v2")
DECIDABLE = ("replied", "skipped", "seen")
#: Słowa, które Jan może napisać zamiast `--all`.
ALL_WORDS = ("wszystkie", "all")


def newest_keys(state_dir: Path) -> Path | None:
    # Numeracja jest wspólna dla rolling listy i trybu natychmiastowego: Jan widzi „[3]”
    # raz w pliku o 06:30, raz w wiadomości o 14:16, a „3 odpisałem” ma trafić w oba.
    found = sorted(
        list(state_dir.glob("reports/rolling-*.keys.json"))
        + list(state_dir.glob("reports/instant-*.keys.json")),
        key=lambda path: path.stat().st_mtime,
    )
    return found[-1] if found else None


def load_mapping(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        raise RollingError(
            "nie ma mapy numerów z ostatniej listy (rolling-*.keys.json) — "
            "oznaczanie działa po wysłanej liście"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RollingError(f"{path} nie zawiera mapy numer → klucz")
    return {str(number): str(key) for number, key in payload.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--ledger", default="", help="domyślnie <state-dir>/ledger.json")
    parser.add_argument("--keys", default="", help="mapa numerów (domyślnie najnowsza)")
    sub = parser.add_subparsers(dest="polecenie", required=True)

    pokaz = sub.add_parser("pokaz", help="wypisz oferty w danym statusie")
    pokaz.add_argument("--status", default=STATUS_NEW, choices=list(STATUSES))
    pokaz.add_argument("--limit", type=int, default=20)

    oznacz = sub.add_parser("oznacz", help="zmień status ofertom z ostatniej listy")
    oznacz.add_argument("--status", required=True, choices=list(DECIDABLE))
    oznacz.add_argument(
        "--all",
        action="store_true",
        help="wszystkie oferty czekające na decyzję (status seen), nie tylko z ostatniej listy",
    )
    oznacz.add_argument(
        "numery",
        nargs="*",
        help="numery z ostatniej rolling listy, np. 3 7 (albo słowo „wszystkie\")",
    )

    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    ledger = Ledger(args.ledger or state_dir / "ledger.json")

    try:
        if args.polecenie == "pokaz":
            records = ledger.by_status(args.status, limit=args.limit)
            print(f"{args.status}: {len(records)} (liczniki: {ledger.counts()})")
            for record in records:
                marker = "✓" if record.decided else "·"
                print(f"  {marker} {record.key} · {record.title[:60]} · {record.first_seen_at:%Y-%m-%d}")
            return 0

        every = args.all or [word.lower() for word in args.numery] == ["wszystkie"]
        if every:
            keys = [record.key for record in ledger.by_status(STATUS_SEEN)]
            if not keys:
                print("nie ma ofert czekających na decyzję (status seen) — nic do oznaczenia")
                return 0
        elif not args.numery:
            print(
                "ledger: podaj numery z ostatniej listy (np. 3 7) albo --all",
                file=sys.stderr,
            )
            return 2
        else:
            mapping = load_mapping(Path(args.keys) if args.keys else newest_keys(state_dir))
            keys = resolve_numbers(args.numery, mapping)
        changed = ledger.mark_status(keys, args.status)
        if not changed:
            print(f"nic do zmiany — te oferty już mają status {args.status}")
            return 0
        print(f"oznaczone jako {args.status}: {len(changed)}")
        for key in changed:
            record = ledger.get(key)
            title = record.title[:60] if record else ""
            print(f"  {key} · {title}")
        if args.status in DECIDED_STATUSES:
            print(f"decyzja zapisana — te oferty nie wrócą na listę (liczniki: {ledger.counts()})")
        return 0
    except (LedgerCorruptionError, RollingError) as exc:
        print(f"ledger: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
