"""Rolling lista: drafty z wycenami dla nowych ofert, plik i wysyłka na Telegram.

Użycie::

    cd /opt/data/projects/snajper-ofert
    # próba bez wysyłki (pliki powstają, statusy zostają 'new')
    uv run python scripts/rolling_report.py --state-dir /tmp/probe-rolling --dry-run

    # produkcja: plik + panel + sendDocument do tematu #Snajper
    uv run python scripts/rolling_report.py --send

Zasady (z planu §3.5 i D2/D6):
  * cisza, gdy nie ma nic nowego — pusty stdout, żadnej wiadomości,
  * oferta, na którą Jan już odpowiedział/odrzucił (status inny niż `new`), nigdy nie wraca,
  * statusy `new → seen` ustawiamy **po** potwierdzonej wysyłce, nigdy przed,
  * oferty z kubełka ZAMIEN jadą tą samą listą (osobny cron ZAMIEN został wstrzymany),
  * pełna treść ogłoszeń nie siedzi w pliku (jest w `leads-*.json`); w liście jest skrót.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(PROJECT / "scripts"))

import copy_widget
from offer_context import fetch_full_text

from freelance_pipeline.brief import write_briefs
from freelance_pipeline.cennik import DEFAULT_PATH as CENNIK_PATH
from freelance_pipeline.cennik import load_cennik, quote_row
from freelance_pipeline.ledger import DEFAULT_EXPIRY_DAYS, Ledger
from freelance_pipeline.portfolio import render_portfolio_block
from freelance_pipeline.pricing import PriceBrief, price_best_effort, price_drafts
from freelance_pipeline.proposal import ProposalWriter
from freelance_pipeline.proposal_check import RULES_VERSION
from freelance_pipeline.rolling import (
    RollingDeliveryError,
    TelegramTarget,
    build_entries,
    enrich_rows,
    load_pending_rows,
    mark_delivered,
    numbering,
    panel_items,
    render_file,
    render_markdown,
    select_rows,
    send_document,
    store_drafts,
)

DEFAULT_STATE_DIR = Path("/opt/data/cache/snajper-v2")
LEADS_KEEP_DAYS = 7
MAX_OFFERS = 25


GOOGLE_PYTHON = Path("/opt/data/.hermes-google-venv/bin/python")
GOOGLE_SCRIPT = Path("/opt/data/scripts/md_to_gdoc.py")


def export_to_docs(path: Path, *, title: str, timeout: float = 180.0) -> str:
    """Wyeksportuj dokument do Google Docs i zwróć link (albo pusty string).

    Eksport jest dodatkiem: brak venv, brak zgody w Google albo brak sieci nie może wywalić
    dostawy listy, więc każda porażka kończy się pustym linkiem i wpisem w logu.
    """
    if not GOOGLE_PYTHON.exists() or not GOOGLE_SCRIPT.exists():
        return ""
    try:
        result = subprocess.run(
            [str(GOOGLE_PYTHON), str(GOOGLE_SCRIPT), str(path), "--title", title],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    for token in result.stdout.split():
        if token.startswith("https://docs.google.com/"):
            return token
    return ""


def draft_context(context_parts: list[str]) -> str:
    return "\n\n".join(context_parts)


def prune_leads(state_dir: Path, *, days: int = LEADS_KEEP_DAYS) -> int:
    """Usuń stare pliki ofert, żeby katalog stanu nie rósł bez końca."""
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    removed = 0
    for path in state_dir.glob("leads-*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rolling_report", description=__doc__)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--ledger", default="", help="domyślnie <state-dir>/ledger.json")
    parser.add_argument("--out", default="", help="domyślnie <state-dir>/reports/rolling-<run>.md (dokument)")
    parser.add_argument("--plain", default="", help="kopia tekstowa do terminala (domyślnie obok .md)")
    parser.add_argument("--docs", action="store_true", help="wyeksportuj dokument do Google Docs")
    parser.add_argument("--panel", default="", help="domyślnie <state-dir>/reports/panel-<run>.html")
    parser.add_argument("--limit", type=int, default=MAX_OFFERS)
    parser.add_argument("--pending-days", type=int, default=3)
    parser.add_argument("--expire-days", type=int, default=DEFAULT_EXPIRY_DAYS)
    parser.add_argument(
        "--expire-hours",
        type=float,
        default=None,
        help="wygaś dostarczone i nieobsłużone oferty starsze niż N godzin (przed listą)",
    )
    parser.add_argument("--send", action="store_true", help="wyślij plik jako dokument na Telegram")
    parser.add_argument(
        "--mark-delivered",
        action="store_true",
        help="bez Telegrama: zapisz drafty i oznacz seen (dostawa przez panel PWA)",
    )
    parser.add_argument("--dry-run", action="store_true", help="nie zmieniaj statusów i nie wysyłaj")
    parser.add_argument("-v", "--verbose", action="store_true", help="raport techniczny na stderr")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    ledger = Ledger(args.ledger or state_dir / "ledger.json")
    now = datetime.now(UTC)
    run_id = f"{now:%Y%m%dT%H%M%SZ}"
    out = Path(args.out) if args.out else state_dir / "reports" / f"rolling-{run_id}.md"
    plain = Path(args.plain) if args.plain else out.with_suffix(".txt")
    panel = Path(args.panel) if args.panel else state_dir / "reports" / f"panel-{run_id}.html"

    def log(message: str) -> None:
        if args.verbose:
            print(message, file=sys.stderr)

    # Okno świeżości: oferta, która leżała bez decyzji dłużej niż próg, nie wchodzi
    # już na listę — po tym czasie w ogłoszeniu jest zwykle kilkunastu chętnych.
    # Tylko dostarczone: oferta, której Jan nigdy nie zobaczył (awaria wysyłki),
    # ma prawo trafić na listę przy następnym przebiegu.
    if args.expire_hours is not None and not args.dry_run:
        early = ledger.expire(max_age_hours=args.expire_hours, only_delivered=True, now=now)
        if early:
            log(f"wygaszonych przed listą (>{args.expire_hours:g}h): {len(early)}")

    rows = load_pending_rows(state_dir, ledger, now=now, days=args.pending_days)
    short = sum(1 for row in rows if len(row.description) < 900)
    if short:
        rows = enrich_rows(rows, lambda row: fetch_full_text(row.url))
        log(f"pełną treść dociągnięto dla {sum(1 for r in rows if len(r.description) >= 900)}/{short} ofert")
    log(f"ofert czekających: {len(rows)}")
    if not rows:
        return 0  # cisza

    # Rejestr: świeże oferty wchodzą jako 'new'; istniejące zachowują swój status.
    added = ledger.add([row.as_seed() for row in rows], now=now)
    log(f"nowych w rejestrze: {len(added)}")

    selection = select_rows(rows, ledger, limit=args.limit)
    if not selection:
        log("brak ofert ze statusem new")
        return 0
    offers = [row.as_offer() for row in selection]
    portfolio = render_portfolio_block()

    try:
        drafts = ProposalWriter().write(offers)
    except Exception as exc:  # noqa: BLE001 - brak draftu nie może wysłać pustej listy
        print(f"snajper rolling: nie udało się napisać listy: {exc}")
        print(f"snajper rolling: nie udało się napisać listy: {exc}", file=sys.stderr)
        return 3

    # Wycena idzie z cennika (decyzja Jana, 2026-10-28). Model dostaje tylko oferty, których
    # cennik nie zna, i wtedy jego kwota jest jawnie oznaczona jako „z researchu”. Bez tego
    # jedna i ta sama oferta wyceniana dwa razy dawała dwie różne kwoty (7 500 zł vs 4 500 zł).
    cennik = load_cennik(CENNIK_PATH)
    briefs_map: dict[str, PriceBrief] = {}
    cennik_notes: dict[str, str] = {}
    unmatched = []
    for row in selection:
        quote, price_brief = quote_row(cennik, row)
        if price_brief is None:
            unmatched.append(row)
            continue
        briefs_map[row.key] = price_brief
        cennik_notes[row.key] = f"cennik, {quote.service} {quote.level}" if quote else ""
    log(f"wycena z cennika: {len(briefs_map)}/{len(selection)} ofert, poza cennikiem: {len(unmatched)}")

    unpriced: dict[str, str] = {}
    if unmatched:
        model_briefs, unpriced = price_best_effort([row.as_offer() for row in unmatched])
        for brief in model_briefs:
            briefs_map[brief.key] = brief
            cennik_notes[brief.key] = "model, poza cennikiem"
    briefs = [briefs_map[key] for key in (offer.key for offer in offers) if key in briefs_map]
    if unpriced:
        # Oferta bez wyceny nie dostaje kwoty z sufitu: wypada z listy i zostaje `new`,
        # więc wróci przy kolejnym przebiegu (tak samo jak oferta bez draftu).
        for key, reason in sorted(unpriced.items()):
            log(f"bez wyceny (zostaje na jutro): {key} — {reason}")
        print(
            "snajper rolling: bez wyceny (zostają na kolejny przebieg): "
            + ", ".join(sorted(unpriced))
        )
        keep = [offer.key for offer in offers if offer.key not in unpriced]
        offers = [offer for offer in offers if offer.key not in unpriced]
        drafts = [draft for draft in drafts if draft.key in set(keep)]
        briefs = [brief for brief in briefs if brief.key in set(keep)]
    if not offers:
        print("snajper rolling: nie udało się wycenić żadnej oferty — listy nie wysyłam")
        print(
            "snajper rolling: nie udało się wycenić żadnej oferty — listy nie wysyłam",
            file=sys.stderr,
        )
        return 3

    try:
        priced = price_drafts(
            drafts, briefs, contexts=[draft_context([offer.text, portfolio]) for offer in offers]
        )
    except Exception as exc:  # noqa: BLE001 - j.w. kontrola wyjścia nie może zjeść listy
        print(f"snajper rolling: nie udało się napisać listy: {exc}")
        print(f"snajper rolling: nie udało się napisać listy: {exc}", file=sys.stderr)
        return 3

    brief_texts = write_briefs(offers)
    candidates = build_entries(selection, ledger, priced=priced, briefs=brief_texts)
    entries = [entry for entry in candidates if entry.draft_text]
    missing = [entry.key for entry in candidates if not entry.draft_text]
    if missing:
        log(f"bez draftu (zostają na jutro): {', '.join(missing)}")
    if not entries:
        log("żadna oferta nie ma draftu — nie wysyłam listy")
        return 0

    document = render_markdown(entries, now=now)
    text = render_file(entries, now=now)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")
    plain.write_text(text, encoding="utf-8")
    panel.parent.mkdir(parents=True, exist_ok=True)
    panel.write_text(copy_widget.render(panel_items(entries)), encoding="utf-8")
    keys_path = out.with_suffix(".keys.json")
    keys_path.write_text(
        json.dumps(numbering(entries), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log(f"plik: {out} ({out.stat().st_size} B), panel: {panel}")

    ok_count = sum(1 for entry in entries if entry.ok)
    log(f"draftów bez uwag: {ok_count}/{len(entries)}")

    docs_url = ""
    if args.docs:
        docs_url = export_to_docs(out, title=f"Snajper — rolling lista {now:%Y-%m-%d}")
        if docs_url:
            # Link w dokumencie wymaga drugiego zapisu pliku.
            out.write_text(render_markdown(entries, now=now, docs_url=docs_url), encoding="utf-8")
            log(f"Google Docs: {docs_url}")
        else:
            log("eksport do Google Docs nie udał się — zostaje sam plik")

    if args.dry_run:
        print(f"DRY-RUN: {out} ({len(entries)} ofert)", file=sys.stderr)
        return 0

    if args.send:
        try:
            target = TelegramTarget.from_env()
            caption = (
                f"Snajper — {len(entries)} nowych ofert z propozycjami wiadomości i wycenami. "
                "Odpisz w czacie „3 odpisałem”, „5 odrzuć”."
            )
            if docs_url:
                caption += f"\nWersja do czytania: {docs_url}"
            payload = send_document(target, out, caption=caption)
            message_id = (payload.get("result") or {}).get("message_id")
            log(f"wysłane jako message_id {message_id}")
        except RollingDeliveryError as exc:
            # Głośno na stdout, bo cron `no_agent` dostarcza właśnie stdout: awaria wysyłki
            # ma dotrzeć do Jana, a nie zniknąć w logu.
            print(f"snajper rolling: wysyłka nie udała się: {exc}")
            print(f"snajper rolling: wysyłka nie udała się: {exc}", file=sys.stderr)
            return 3

    # Dostawa przez panel (PWA): bez Telegrama lista i tak musi wejść do rejestru,
    # inaczej oferty zostają `new` i wracają na kolejną listę w nieskończoność.
    if args.send or args.mark_delivered:
        store_drafts(entries, ledger, rules_version=RULES_VERSION, now=now)
        mark_delivered([entry.key for entry in entries], ledger, now=now)
        log("statusy new -> seen")

    expired = ledger.expire(max_age_days=args.expire_days, now=now)
    log(f"wygaszonych bez decyzji: {len(expired)}")
    prune_leads(state_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
