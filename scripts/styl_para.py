"""Wysyla do humanizera pare: ostatni draft systemu i wersja, ktora Jan wyslal.

Uzycie::

    cd /opt/data/projects/snajper-ofert
    .venv/bin/python scripts/styl_para.py --key useme:145473 --plik /tmp/moja.txt
    .venv/bin/python scripts/styl_para.py --key useme:145473 --tekst "Dzien dobry, ..."

Para trafia do ``poprawki.md`` humanizera (usluga ``humanizer`` na porcie 8011).
Draft bierze z bazy panelu (ostatnia wersja z origin ``model`` albo ``ledger``),
nigdy z plikow ``ODPOWIEDZI-*.md``, bo te sa outputem systemu.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freelance_pipeline import styl
from freelance_pipeline.web import store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="styl_para", description=__doc__)
    parser.add_argument("--key", required=True, help="klucz oferty, np. useme:145473")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plik", help="plik z wersja, ktora Jan faktycznie wyslal")
    group.add_argument("--tekst", help="wersja Jana podana wprost")
    parser.add_argument("--uwagi", default="", help="notatka dopisana do pary")
    parser.add_argument("--rejestr", default=styl.DEFAULT_REJESTR)
    parser.add_argument("--db", default=str(store.DEFAULT_DB_PATH))
    parser.add_argument("--dry", action="store_true", help="pokaz, co by poszlo, nic nie wysylaj")
    args = parser.parse_args(argv)

    moja = Path(args.plik).read_text(encoding="utf-8") if args.plik else args.tekst
    conn = store.connect(args.db)
    try:
        draft = store.draft_agenta(conn, args.key)
    finally:
        conn.close()
    if not draft:
        print(f"brak draftu systemu dla {args.key}", file=sys.stderr)
        return 2

    if args.dry:
        print(f"draft: {len(draft)} zn, moja: {len(moja or '')} zn")
        print("--- DRAFT ---")
        print(draft)
        print("--- MOJA ---")
        print(moja)
        return 0

    ok = styl.zapisz_pare(
        draft, moja or "", rejestr=args.rejestr, uwagi=args.uwagi or f"snajper: {args.key}"
    )
    print("zapisano pare" if ok else "nie zapisano (humanizer odrzucil albo nie dziala)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
