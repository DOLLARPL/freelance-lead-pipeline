"""Pętla poprawek: „popraw 3: krócej, bez linku” i nowa wersja wiadomości.

Użycie::

    .venv/bin/python scripts/popraw.py --list
    .venv/bin/python scripts/popraw.py --nr 3 --instruction "skróć do 900 znaków, usuń link"
    .venv/bin/python scripts/popraw.py --nr 3 --instruction "..." --send

Numeracja jest ta sama co w wiadomościach i w rolling liście: ``[3]`` to ``3``. Mapa numerów
leży obok wysłanej wiadomości (``instant-*.keys.json``, ``rolling-*.keys.json``), więc poprawka
zawsze trafia w tę ofertę, którą Jan widział pod tym numerem.

Co robi: bierze ogłoszenie z ``leads-*.json``, dokleja do treści polecenie Jana jako wymóg
obowiązkowy, pisze draft od nowa (jeden model, ten sam kontrakt stylu), wstawia cenę z cennika
i wysyła nowy blok z dopiskiem ``wersja N``. Wyceny nie rusza, bo cena nie jest tekstem.

Czego nie robi: nie wysyła niczego do Oferteo ani Useme, nie zmienia statusu oferty w rejestrze
i nie nadpisuje poprzedniej wersji (każda poprawka ląduje w ``reports/popraw-<run>.txt``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(PROJECT / "scripts"))

from freelance_pipeline.cennik import DEFAULT_PATH as CENNIK_PATH  # noqa: E402
from freelance_pipeline.cennik import load_cennik  # noqa: E402
from freelance_pipeline.instant import render_offer_block, send_text  # noqa: E402
from freelance_pipeline.portfolio import render_portfolio_block  # noqa: E402
from freelance_pipeline.pricing import (  # noqa: E402
    PriceBrief,
    apply_price,
    price_best_effort,
    price_drafts,
)
from rolling_report import draft_context  # noqa: E402
from freelance_pipeline.proposal import OfferInput, ProposalWriter  # noqa: E402
from freelance_pipeline.rolling import (  # noqa: E402
    DEFAULT_CHAT_ID,
    DEFAULT_THREAD_ID,
    LeadRow,
    TelegramTarget,
)

DEFAULT_STATE_DIR = Path("/opt/data/cache/snajper-v2")
INSTRUCTION_PREFIX = (
    "\n\nUWAGA OD JANA (poprawka obowiązkowa, ważniejsza niż szablon): {instruction}\n"
    "Zachowaj wszystkie zasady kontraktu poza tym, co każe zmienić ta uwaga."
)


class PoprawError(Exception):
    """Nie da się wykonać poprawki: nieznany numer, brak ogłoszenia, brak draftu."""


def latest_numbering(state_dir: Path) -> tuple[Path, dict[str, str]]:
    """Najświeższa mapa numer → klucz (z wiadomości natychmiastowych albo rolling listy)."""
    candidates = sorted(
        list((state_dir / "reports").glob("instant-*.keys.json"))
        + list((state_dir / "reports").glob("rolling-*.keys.json")),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        raise PoprawError("brak mapy numerów: nie było jeszcze żadnej wysłanej listy")
    newest = candidates[-1]
    try:
        mapping = json.loads(newest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PoprawError(f"{newest} nie jest poprawnym JSON-em: {exc}") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise PoprawError(f"{newest} nie ma numerów ofert")
    return newest, {str(key): str(value) for key, value in mapping.items()}


def find_row(state_dir: Path, key: str) -> LeadRow:
    """Znajdź ogłoszenie po kluczu w najświeższych plikach ``leads-*.json``."""
    for path in sorted(state_dir.glob("leads-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            continue
        for entry in payload:
            if isinstance(entry, dict) and str(entry.get("key")) == key:
                return LeadRow.from_json(entry)
    raise PoprawError(f"nie ma ogłoszenia o kluczu {key} w plikach leads-*.json")


def stored_price(state_dir: Path, number: str) -> tuple[PriceBrief | None, str]:
    """Cena z ostatniej wysłanej wiadomości dla tego numeru (żeby poprawka jej nie zmieniała)."""
    candidates = sorted(
        list((state_dir / "reports").glob("instant-*.prices.json"))
        + list((state_dir / "reports").glob("rolling-*.prices.json")),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        return None, ""
    try:
        payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, ""
    entry = payload.get(number)
    if not isinstance(entry, dict) or not entry.get("amount"):
        return None, ""
    return (
        PriceBrief(
            key=str(entry.get("key") or ""),
            amount=float(entry["amount"]),
            currency=str(entry.get("currency") or "PLN"),
            days=int(entry.get("days") or 1),
            hourly_min=90.0,
            hourly_max=180.0,
            includes=(str(entry.get("note") or "poprawka wiadomości"),),
            sources=("poprzednia wiadomość snajpera",),
            rationale=str(entry.get("note") or ""),
            model="poprzednia wycena",
        ),
        str(entry.get("note") or ""),
    )


def quote_for(row: LeadRow) -> tuple[PriceBrief | None, str]:
    """Wycena z cennika, a przy braku dopasowania z modelu; notatka mówi skąd."""
    cennik = load_cennik(CENNIK_PATH)
    quote = cennik.quote(row.description, title=row.title)
    if quote is not None:
        return cennik.as_price_brief(quote, key=row.key), f"cennik, {quote.service} {quote.level}"
    briefs, failures = price_best_effort([row.as_offer()])
    if briefs:
        return briefs[0], "model, poza cennikiem"
    reason = next(iter(failures.values()), "nieznany powód")
    return None, f"brak wyceny: {reason}"


def build_version(
    row: LeadRow,
    instruction: str,
    *,
    state_dir: Path,
    number: str,
    version: int,
) -> tuple[str, PriceBrief | None, str]:
    """Nowa wersja wiadomości dla jednej oferty."""
    offer = row.as_offer()
    with_instruction = OfferInput(
        key=offer.key,
        title=offer.title,
        source=offer.source,
        url=offer.url,
        author=offer.author,
        description=(offer.description or "") + INSTRUCTION_PREFIX.format(instruction=instruction),
        bucket=offer.bucket,
        score=offer.score,
        budget=offer.budget,
        currency=offer.currency,
    )
    draft = ProposalWriter().write([with_instruction])[0]
    # Cena z poprzedniej wiadomości, chyba że Jan wprost prosi o przeliczenie.
    asks_about_price = re.search(r"\bcen|\bwycen|\bkoszt", instruction, re.IGNORECASE)
    brief, note = (None, "")
    if not asks_about_price:
        brief, note = stored_price(state_dir, number)
    if brief is None:
        brief, note = quote_for(row)
    elif brief.key != row.key:
        # price_drafts paruje wycenę z draftem po kluczu, a zapisana cena go nie niesie.
        brief = PriceBrief(
            key=row.key,
            amount=brief.amount,
            currency=brief.currency,
            days=brief.days,
            hourly_min=brief.hourly_min,
            hourly_max=brief.hourly_max,
            includes=brief.includes,
            sources=brief.sources,
            rationale=brief.rationale,
            model=brief.model,
            hours=brief.hours,
        )
    text = draft.text
    if brief is not None:
        priced = price_drafts([draft], [brief], contexts=[draft_context([offer.text, render_portfolio_block()])])
        if priced:
            text = priced[0].text
    return text, brief, note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="popraw", description=__doc__)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--nr", help="numer oferty z ostatniej listy, np. 3")
    parser.add_argument("--instruction", default="", help="co zmienić w wiadomości")
    parser.add_argument("--version", type=int, default=2, help="numer wersji (domyślnie 2)")
    parser.add_argument("--list", action="store_true", help="pokaż dostępne numery")
    parser.add_argument("--send", action="store_true", help="wyślij nową wersję na Telegram")
    parser.add_argument("--silent", action="store_true", help="wyślij bez powiadomienia")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    try:
        source, mapping = latest_numbering(state_dir)
    except PoprawError as exc:
        print(f"snajper popraw: {exc}", file=sys.stderr)
        return 2

    if args.list:
        print(f"numeracja z {source.name}:")
        for number, key in sorted(mapping.items(), key=lambda item: int(item[0])):
            print(f"  [{number}] {key}")
        return 0

    if not args.nr or not args.instruction.strip():
        print("snajper popraw: podaj --nr i --instruction (albo --list)", file=sys.stderr)
        return 2
    number = str(args.nr).strip()
    if number not in mapping:
        print(
            "snajper popraw: nie znam numeru "
            f"{number}; ostatnia lista ma {len(mapping)} pozycji: "
            + ", ".join(sorted(mapping, key=int)),
            file=sys.stderr,
        )
        return 2

    key = mapping[number]
    try:
        row = find_row(state_dir, key)
    except PoprawError as exc:
        print(f"snajper popraw: {exc}", file=sys.stderr)
        return 2

    try:
        text, brief, note = build_version(
            row,
            args.instruction.strip(),
            state_dir=state_dir,
            number=number,
            version=args.version,
        )
    except Exception as exc:  # noqa: BLE001 - model albo walidacja
        print(f"snajper popraw: nie udało się napisać wersji: {exc}", file=sys.stderr)
        return 3

    block = render_offer_block(
        index=int(number),
        bucket=row.bucket,
        score=row.score,
        source=row.source,
        title=row.title,
        url=row.url,
        brief="",
        draft_text=text,
        amount=brief.amount if brief else None,
        currency=brief.currency if brief else "PLN",
        days=brief.days if brief else None,
        budget=row.budget,
        budget_currency=row.currency,
        note=note,
        hint=f"wersja {args.version} · poprawione: {args.instruction.strip()}",
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = state_dir / "reports" / f"popraw-{stamp}-{number}.txt"
    out.write_text(block + "\n", encoding="utf-8")
    print(block)

    if not args.send:
        return 0
    target = TelegramTarget(
        token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("SNAJPER_TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID),
        thread_id=os.environ.get("SNAJPER_TELEGRAM_THREAD_ID", DEFAULT_THREAD_ID),
    )
    try:
        send_text(
            chat_id=target.chat_id,
            thread_id=target.thread_id,
            token=target.token,
            text=block,
            silent=args.silent,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"snajper popraw: wysyłka padła: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
