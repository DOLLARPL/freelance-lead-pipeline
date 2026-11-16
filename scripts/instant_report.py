"""Snajper w trybie natychmiastowym: nowa oferta zaraz na Telegram, cisza gdy nic nie ma.

Użycie::

    # jedno sprawdzenie bez wysyłki (wypisze gotową wiadomość na stdout)
    .venv/bin/python scripts/instant_report.py --dry-run

    # produkcja: zbierz, oceń, wyceń z cennika, wyślij, oznacz jako seen
    .venv/bin/python scripts/instant_report.py --send

Kontrakt (decyzje Jana z 2026-10-28):
  * nowe zlecenie ma dojść od razu, więc ten skrypt chodzi na cronie co 10 minut,
  * brak nowych ofert ⇒ pusty stdout i zero wiadomości,
  * wycena idzie z ``config/cennik.yaml``; model wycenia tylko to, czego cennik nie zna,
  * w wiadomości jest numer, kubełek, ocena, brief, link, cena z terminem i gotowy draft,
  * statusy `new → seen` ustawiamy po potwierdzonej wysyłce, nigdy przed.

Zakres źródeł: domyślnie ``useme`` (od 2026-11-14 Oferteo usunięte ze wszystkich źródeł)
i zero HTTP do portalu) oraz ``facebook_groups``. Ten skrypt nie woła Apify: czyta plik,
który godzinowym cronem zapisuje transport FB. Dzięki temu post z grupy wchodzi do kolejki
w ciągu dziesięciu minut, a nie dopiero w porannym przebiegu.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(PROJECT / "scripts"))

from offer_context import fetch_full_text  # noqa: E402

from freelance_pipeline.adlink import with_ad_reference  # noqa: E402
from freelance_pipeline.brief import write_briefs  # noqa: E402
from freelance_pipeline.cennik import DEFAULT_PATH as CENNIK_PATH  # noqa: E402
from freelance_pipeline.cennik import load_cennik  # noqa: E402
from freelance_pipeline.instant import render_message, send_text  # noqa: E402
from freelance_pipeline.ledger import Ledger  # noqa: E402
from freelance_pipeline.pricing import (
    strip_price_marker,  # noqa: E402
    PriceBrief,
    price_best_effort,
    price_drafts,
)
from rolling_report import draft_context  # noqa: E402
from freelance_pipeline.proposal import ProposalWriter  # noqa: E402
from freelance_pipeline.rolling import (  # noqa: E402
    DEFAULT_CHAT_ID,
    DEFAULT_THREAD_ID,
    LeadRow,
    enrich_rows,
    load_pending_rows,
    select_rows,
    TelegramTarget,
)
from freelance_pipeline.portfolio import render_portfolio_block  # noqa: E402

DEFAULT_STATE_DIR = Path("/opt/data/cache/snajper-v2")
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
#: Ile godzin wstecz wolno wziąć ofertę, jeśli poprzednia wysyłka padła.
INSTANT_LOOKBACK_HOURS = 6
#: Podpowiedź dopisywana do wiadomości (jedno miejsce, żeby nie mnożyć wariantów).
HINT = "Odpisz tu: „1 odpisałem”, „1 odrzuć” albo „popraw 1: co zmienić”."


def collect(state_dir: Path, sources: str, model: str, *, log) -> Path | None:
    """Zbierz oferty i zostaw plik ``leads-*.json``; zwraca ścieżkę albo ``None``."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    leads = state_dir / f"leads-{stamp}.json"
    report = state_dir / "reports" / f"instant-report-{stamp}.txt"
    top = state_dir / "reports" / f"instant-top-{stamp}.txt"
    command = [
        sys.executable,
        "-m",
        "freelance_pipeline.cli",
        "run",
        "--state",
        str(state_dir / "state.json"),
        "--report",
        str(report),
        "--top-report",
        str(top),
        "--leads-json",
        str(leads),
        "--sources",
        sources,
        "--model",
        model,
        "--collect-timeout",
        "45",
    ]
    log("kolekcja: " + " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT / "src")},
        check=False,
    )
    if result.returncode != 0:
        print(
            "snajper instant: kolekcja padła, kod "
            f"{result.returncode}: {(result.stderr or result.stdout).strip()[:400]}",
            file=sys.stderr,
        )
        return None
    return leads if leads.is_file() else None


def quote_briefs(
    rows: list[LeadRow], *, log
) -> tuple[dict[str, PriceBrief], dict[str, str]]:
    """Wyceny z cennika dla wszystkich ofert; zwraca ``(briefs, notatki)``.

    Oferta bez dopasowania w cenniku idzie do modelu (jeden research na partię), a gdy i to
    zawiedzie, ląduje bez ceny z notatką: lepiej pokazać draft bez kwoty niż zmyśloną kwotę.
    """
    cennik = load_cennik(CENNIK_PATH)
    briefs: dict[str, PriceBrief] = {}
    notes: dict[str, str] = {}
    unmatched: list[LeadRow] = []
    for row in rows:
        quote = cennik.quote(row.description, title=row.title)
        if quote is None:
            unmatched.append(row)
            continue
        briefs[row.key] = cennik.as_price_brief(quote, key=row.key)
        notes[row.key] = f"cennik, {quote.service} {quote.level}"
    if unmatched:
        log(f"poza cennikiem: {', '.join(row.key for row in unmatched)}")
        model_briefs, failures = price_best_effort([row.as_offer() for row in unmatched])
        for brief in model_briefs:
            briefs[brief.key] = brief
            notes[brief.key] = "model, poza cennikiem"
        for key, reason in failures.items():
            notes[key] = f"brak wyceny: {reason}"
    return briefs, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="instant_report", description=__doc__)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--sources", default="useme")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki i bez statusów")
    parser.add_argument("--send", action="store_true", help="wyślij na Telegram i oznacz seen")
    parser.add_argument(
        "--mark-only",
        action="store_true",
        help="zapisz drafty i oznacz seen bez wysyłki na Telegram (kanał PWA)",
    )
    parser.add_argument("--no-collect", action="store_true", help="nie zbieraj, weź gotowe leads-*.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    (state_dir / "reports").mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        if args.verbose:
            print(message, file=sys.stderr)

    now = datetime.now(UTC)
    ledger = Ledger(state_dir / "ledger.json")

    if not args.no_collect:
        collected = collect(state_dir, args.sources, args.model, log=log)
        if collected is None:
            return 0 if not args.verbose else 3

    rows = load_pending_rows(state_dir, ledger, now=now, days=1)
    # Tylko zrodla z tego przebiegu: stary plik leads-*.json nie moze przywrocic
    # ofert ze zrodla, ktore zostalo wylaczone (tak wracalo Oferteo z cache).
    allowed = {name.strip() for name in args.sources.split(",") if name.strip()}
    rows = [row for row in rows if row.source in allowed]
    short = [row for row in rows if len(row.description) < 900]
    if short:
        rows = enrich_rows(rows, lambda row: fetch_full_text(row.url))
    if not rows:
        log("brak nowych ofert, cisza")
        return 0

    ledger.add([row.as_seed() for row in rows], now=now)
    selection = select_rows(rows, ledger, limit=args.limit)
    if not selection:
        log("brak ofert ze statusem new")
        return 0

    offers = [row.as_offer() for row in selection]
    portfolio = render_portfolio_block()
    try:
        drafts = ProposalWriter().write(offers)
    except Exception as exc:  # noqa: BLE001 - nie wysyłamy pustej wiadomości
        print(f"snajper instant: nie udało się napisać draftów: {exc}", file=sys.stderr)
        return 3

    briefs_text = write_briefs(offers)
    briefs, notes = quote_briefs(selection, log=log)
    priced = price_drafts(
        [draft for draft in drafts if draft.key in briefs],
        [briefs[offer.key] for offer in offers if offer.key in briefs],
        contexts=[draft_context([offer.text, portfolio]) for offer in offers if offer.key in briefs],
    )
    by_key = {item.key: item for item in priced}

    entries: list[dict] = []
    keys: dict[str, str] = {}
    for index, (row, offer) in enumerate(zip(selection, offers, strict=True), start=1):
        draft = by_key.get(row.key)
        draft_text = draft.text if draft else ""
        if draft is not None:
            # Oferta poza cennikiem bez wyceny modelu nie moze wyslac znacznika
            # [WYCENA: ...] do klienta (lista 2026-10-30).
            draft_text, had_marker = strip_price_marker(draft_text)
            draft_text = with_ad_reference(draft_text, row.source, row.url)
            if had_marker:
                notes[row.key] = notes.get(row.key, "") + " (bez ceny)"
        brief_data = briefs.get(row.key)
        entries.append(
            {
                "index": index,
                "bucket": row.bucket,
                "score": row.score,
                "source": row.source,
                "title": row.title,
                "url": row.url,
                "brief": briefs_text.get(row.key, ""),
                "draft_text": draft_text,
                "amount": brief_data.amount if brief_data else None,
                "currency": brief_data.currency if brief_data else "PLN",
                "days": brief_data.days if brief_data else None,
                "budget": row.budget,
                "budget_currency": row.currency,
                "note": notes.get(row.key, ""),
            }
        )
        keys[str(index)] = row.key

    text = render_message(entries, now=now, hint=HINT if len(entries) == 1 else "")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = state_dir / "reports" / f"instant-{stamp}.txt"
    out.write_text(text + "\n", encoding="utf-8")
    (state_dir / "reports" / f"instant-{stamp}.keys.json").write_text(
        __import__("json").dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Ceny zapisujemy obok numeracji, żeby „popraw 3” nie wyceniało oferty drugi raz:
    # przy ofertach poza cennikiem model za każdym razem dawał inną kwotę (550 → 600 zł).
    prices = {
        str(index): {
            "amount": entry["amount"],
            "currency": entry["currency"],
            "days": entry["days"],
            "note": entry["note"],
        }
        for index, entry in enumerate(entries, start=1)
    }
    (state_dir / "reports" / f"instant-{stamp}.prices.json").write_text(
        __import__("json").dumps(prices, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.dry_run or (not args.send and not args.mark_only):
        print(text)
        return 0

    if args.send:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        target = TelegramTarget(
            token=token,
            chat_id=os.environ.get("SNAJPER_TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID),
            thread_id=os.environ.get("SNAJPER_TELEGRAM_THREAD_ID", DEFAULT_THREAD_ID),
        )
        try:
            send_text(
                chat_id=target.chat_id, thread_id=target.thread_id, token=target.token, text=text
            )
        except Exception as exc:  # noqa: BLE001 - statusy zostają new, oferta wróci
            print(f"snajper instant: wysyłka padła: {exc}", file=sys.stderr)
            return 3
    ledger.mark_delivered(keys.values(), now=now)
    # Draft musi wyladowac w rejestrze, tak samo jak w rolling liscie. Bez tego oferty
    # dostarczone trybem natychmiastowym nie maja wiadomosci w panelu ani dla "popraw 3".
    from freelance_pipeline.proposal_check import RULES_VERSION

    stored = 0
    for entry in entries:
        text = str(entry.get("draft_text") or "").strip()
        if text:
            ledger.set_draft(keys[str(entry["index"])], text, rules_version=RULES_VERSION, now=now)
            stored += 1
    log(f"drafty zapisane w rejestrze: {stored}")
    log(f"wysłane i oznaczone jako seen: {len(keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
