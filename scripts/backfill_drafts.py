"""Uzupelnia brakujace drafty dla ofert, ktore przyszly trybem natychmiastowym.

Tryb natychmiastowy wysylal wiadomosc na Telegram, ale nie zapisywal jej w rejestrze,
wiec oferty dostarczone miedzy 05:30 a 06:30 (i wszystkie z Oferteo) nie mialy draftu
w panelu. Ten skrypt dopisuje je raz, z cennika i tym samym kontraktem stylu.

Uzycie (jako uid 10000, z kluczem modelu w srodowisku)::

    .venv/bin/python scripts/backfill_drafts.py --limit 5 --dry-run
    .venv/bin/python scripts/backfill_drafts.py --limit 40
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from freelance_pipeline.cennik import load_cennik  # noqa: E402
from freelance_pipeline.ledger import Ledger  # noqa: E402
from freelance_pipeline.portfolio import render_portfolio_block  # noqa: E402
from freelance_pipeline.pricing import price_best_effort, price_drafts  # noqa: E402
from freelance_pipeline.proposal import OfferInput, ProposalWriter  # noqa: E402
from freelance_pipeline.proposal_check import RULES_VERSION  # noqa: E402

CACHE = Path("/opt/data/cache/snajper-v2")
CENNIK = PROJECT / "config" / "cennik.yaml"


def describe(cache: Path) -> dict[str, dict]:
    """Najdluzszy opis ogloszenia per klucz, ta sama regula co w imporcie panelu."""
    best: dict[str, dict] = {}
    for path in sorted(cache.glob("leads-*.json"), key=lambda p: p.stat().st_mtime):
        try:
            payload = json.loads(path.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            continue
        for entry in payload if isinstance(payload, list) else []:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            key = str(entry["key"])
            if key not in best or len(str(entry.get("description") or "")) > len(
                str(best[key].get("description") or "")
            ):
                best[key] = entry
    return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backfill-drafts", description=__doc__)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    ledger = Ledger(CACHE / "ledger.json")
    records = ledger.load()
    details = describe(CACHE)
    cennik = load_cennik(CENNIK)

    todo = [
        key
        for key, record in records.items()
        if not record.draft and record.status in ("new", "seen")
    ]
    todo.sort(key=lambda key: (records[key].bucket != "TOP", -(records[key].score or 0)))
    todo = todo[: args.limit]

    print(f"ofert bez draftu do zrobienia: {len(todo)}")
    done = 0
    skipped = 0
    for index, key in enumerate(todo, start=1):
        record = records[key]
        detail = details.get(key, {})
        description = str(detail.get("description") or "")
        if len(description) < 20:
            print(f"[{index}] pomijam {key}: brak tresci ogloszenia")
            skipped += 1
            continue
        offer = OfferInput(
            key=key,
            title=record.title or str(detail.get("title") or ""),
            source=record.source,
            url=record.url,
            author=str(detail.get("author") or ""),
            description=description,
            bucket=record.bucket,
            score=int(record.score) if record.score is not None else None,
            budget=detail.get("budget"),
            currency=detail.get("currency"),
        )
        try:
            draft = ProposalWriter().write([offer])[0]
        except Exception as exc:  # noqa: BLE001 - jeden blad nie przerywa calosci
            print(f"[{index}] blad modelu {key}: {exc}")
            continue
        text = draft.text
        quote = cennik.quote(description, title=offer.title)
        brief = None
        if quote is not None:
            brief = cennik.as_price_brief(quote, key=key)
        else:
            # Poza cennikiem: kwote liczy model, tak samo jak w przyspieszonym przebiegu.
            model_briefs, _failures = price_best_effort([offer])
            brief = model_briefs[0] if model_briefs else None
        if brief is not None:
            priced = price_drafts(
                [draft],
                [brief],
                contexts=["\n\n".join([description, render_portfolio_block()])],
            )
            if priced:
                text = priced[0].text
        if args.dry_run:
            print(f"[{index}] {key}: {len(text)} znaków, kwota {'tak' if quote else 'brak'}")
            continue
        ledger.set_draft(key, text, rules_version=RULES_VERSION, now=datetime.now(UTC))
        done += 1
        print(f"[{index}] zapisane {key}: {len(text)} znaków")

    print(f"gotowe: zapisane {done}, pominiete {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
