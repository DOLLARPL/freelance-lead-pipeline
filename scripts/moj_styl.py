"""Dopisanie wysłanej wiadomości do pliku stylu (MOJ-STYL.md).

Użycie::

    cd /opt/data/projects/snajper-ofert
    uv run python scripts/moj_styl.py pokaz
    uv run python scripts/moj_styl.py dodaj --tytul "Subiekt i WooCommerce" \
        --klucz useme:145473 --plik /tmp/wyslane.txt

Do pliku trafia **finalna wersja wiadomości, którą Jan faktycznie wysłał** (po swojej
redakcji) — nie draft wygenerowany przez system. Z tego pliku biorą się przykłady do
promptu przy pisaniu kolejnych draftów.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freelance_pipeline.proposal import (
    APPROVED_STYLE_PATH,
    ProposalError,
    load_approved_examples,
    record_approved_example,
)


def _text_from(args: argparse.Namespace) -> str:
    if args.plik:
        return Path(args.plik).read_text(encoding="utf-8")
    if args.tekst:
        return args.tekst
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="moj_styl", description=__doc__)
    parser.add_argument("--sciezka", default=str(APPROVED_STYLE_PATH), help="inny plik stylu (testy)")
    sub = parser.add_subparsers(dest="polecenie", required=True)

    dodaj = sub.add_parser("dodaj", help="dopisz wysłaną wiadomość")
    dodaj.add_argument("--tytul", required=True, help="tytuł zlecenia")
    dodaj.add_argument("--klucz", default="", help="klucz oferty, np. useme:145473")
    dodaj.add_argument("--plik", default="", help="plik z treścią wiadomości")
    dodaj.add_argument("--tekst", default="", help="treść wiadomości wprost")

    sub.add_parser("pokaz", help="wypisz, ile wiadomości jest w pliku stylu")

    args = parser.parse_args(argv)
    path = Path(args.sciezka)

    if args.polecenie == "pokaz":
        examples = load_approved_examples(path)
        print(f"{path}: {len(examples)} wiadomości")
        for index, example in enumerate(examples, start=1):
            print(f"  {index}. {example.title} ({len(example.message)} znaków, klucz: {example.key or '—'})")
        return 0

    message = _text_from(args).strip()
    if len(message) < 80:
        print("treść wygląda na niepełną (mniej niż 80 znaków) — nic nie dopisałem", file=sys.stderr)
        return 2
    try:
        example = record_approved_example(
            args.tytul, message, key=args.klucz, path=args.sciezka and path
        )
    except ProposalError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    total = len(load_approved_examples(path))
    print(f"dopisane: {example.title} (w pliku {total} wiadomości)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
