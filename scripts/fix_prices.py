"""Przelicza drafty, ktore wyszly bez ceny (zdanie awaryjne zamiast kwoty).

Powod: oferta poza cennikiem, ktorej model w danym przebiegu nie wycenil. Teraz liczymy
jeszcze raz, tym razem z fallbackiem na model, i zapisujemy nowa wersje w rejestrze.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from freelance_pipeline.cennik import load_cennik  # noqa: E402
from freelance_pipeline.ledger import Ledger  # noqa: E402
from freelance_pipeline.portfolio import render_portfolio_block  # noqa: E402
from freelance_pipeline.pricing import (  # noqa: E402
    PRICE_FALLBACK,
    price_best_effort,
    price_drafts,
)
from freelance_pipeline.proposal import OfferInput, ProposalWriter  # noqa: E402
from freelance_pipeline.proposal_check import RULES_VERSION  # noqa: E402

CACHE = Path("/opt/data/cache/snajper-v2")
CENNIK = PROJECT / "config" / "cennik.yaml"


def descriptions(cache: Path) -> dict[str, dict]:
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


def main() -> int:
    ledger = Ledger(CACHE / "ledger.json")
    records = ledger.load()
    details = descriptions(CACHE)
    cennik = load_cennik(CENNIK)

    todo = [key for key, rec in records.items() if rec.draft and PRICE_FALLBACK in rec.draft]
    print("draftow bez ceny:", len(todo))
    done = 0
    for key in todo:
        record = records[key]
        detail = details.get(key, {})
        description = str(detail.get("description") or "")
        if len(description) < 20:
            print("pomijam", key, "brak tresci")
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
        draft = ProposalWriter().write([offer])[0]
        quote = cennik.quote(description, title=offer.title)
        brief = None
        if quote is not None:
            brief = cennik.as_price_brief(quote, key=key)
        else:
            model_briefs, _ = price_best_effort([offer])
            brief = model_briefs[0] if model_briefs else None
        text = draft.text
        if brief is not None:
            priced = price_drafts(
                [draft],
                [brief],
                contexts=["\n\n".join([description, render_portfolio_block()])],
            )
            if priced:
                text = priced[0].text
        if PRICE_FALLBACK in text:
            print("nadal bez ceny:", key)
            continue
        ledger.set_draft(key, text, rules_version=RULES_VERSION, now=datetime.now(UTC))
        done += 1
        print("uzupelnione:", key, "|", brief.amount if brief else "?", "|", len(text), "znakow")
    print("gotowe:", done)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
