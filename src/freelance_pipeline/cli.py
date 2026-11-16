"""Production CLI entrypoint wiring collection, isolation, dedup, scoring,
reporting, delivery confirmation and the atomic state commit.

Usage examples::

    # offline, fixture-driven (safe double-run E2E, no network, no secrets)
    uv run python -m freelance_pipeline.cli run --state /tmp/s.json --report /tmp/r.txt \\
        --fixtures tests/fixtures

    # production live mode (scoring uses OPENROUTER_API_KEY from the env);
    # without --sources only the active production set is collected
    uv run python -m freelance_pipeline.cli run --state /opt/snajper/state.json \\
        --report /opt/snajper/report.txt

Telegram flags/env are accepted and validated, but **no message is ever sent**:
this build never performs any Telegram request and never reads a credential
file.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from freelance_pipeline.dedup import PipelineState
from freelance_pipeline.delivery import (
    ReportDelivery,
    TelegramConfigError,
    TelegramDelivery,
)
from freelance_pipeline.openrouter import OpenRouterScorer
from freelance_pipeline.registry import (
    ACTIVE_SOURCE_KWARGS,
    ACTIVE_SOURCES,
    AdapterRegistry,
    build_active_adapters,
    build_adapter,
)
from freelance_pipeline.scoring import DeterministicScorer, ScoreBucket

ENV_STATE = "SNAJPER_STATE"
ENV_REPORT = "SNAJPER_REPORT"
ENV_SOURCES = "SNAJPER_SOURCES"
ENV_FIXTURES = "SNAJPER_FIXTURES"
ENV_MODEL = "SNAJPER_MODEL"
ENV_REJECTED = "SNAJPER_REJECTED_AUDIT"
ENV_MOZE = "SNAJPER_MOZE_REPORT"
ENV_TOP = "SNAJPER_TOP_REPORT"
ENV_POC = "SNAJPER_POC_BACKLOG"
ENV_ZAMIEN = "SNAJPER_ZAMIEN_REPORT"
ENV_LEADS = "SNAJPER_LEADS_JSON"
ENV_TG_TOKEN = "SNAJPER_TELEGRAM_TOKEN"
ENV_TG_CHAT = "SNAJPER_TELEGRAM_CHAT_ID"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RUNTIME = 3

DEFAULT_REPORT = "-"


class CollectTimeoutError(TimeoutError):
    """A source's ``collect()`` exceeded its whole-collection wall clock."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snajper",
        description="Snajper v2: collect -> dedup -> score -> report -> deliver -> commit.",
    )
    parser.add_argument("run", nargs="?", default="run", help="pipeline command (only 'run')")
    parser.add_argument(
        "--state",
        default=os.environ.get(ENV_STATE),
        help=f"path of the durable seen-state JSON (env {ENV_STATE})",
    )
    parser.add_argument(
        "--report",
        default=os.environ.get(ENV_REPORT, DEFAULT_REPORT),
        help="report sink: file path or '-' for stdout (env {ENV_REPORT})",
    )
    parser.add_argument(
        "--top-report",
        dest="top_report",
        default=os.environ.get(ENV_TOP),
        help=(
            "write the UNCAPPED TOP archive here (the chat gets the 15 highest, "
            f"Polish sources first); omit to skip (env {ENV_TOP})"
        ),
    )
    parser.add_argument(
        "--poc-backlog",
        dest="poc_backlog",
        default=os.environ.get(ENV_POC),
        help=(
            "collect the portfolio gaps the model spotted into this JSON file "
            f"(never delivered on its own; env {ENV_POC})"
        ),
    )
    parser.add_argument(
        "--moze-report",
        dest="moze_report",
        default=os.environ.get(ENV_MOZE),
        help=(
            "write MOŻE rows here (never sent to the chat); '-' for stdout; "
            f"omit to skip (env {ENV_MOZE})"
        ),
    )
    parser.add_argument(
        "--zamien-report",
        dest="zamien_report",
        default=os.environ.get(ENV_ZAMIEN),
        help=(
            "write the ZAMIEN list here (offers to answer with automation instead of a "
            "hire; delivered as its own Telegram message, never inside the TOP report); "
            f"omit to skip (env {ENV_ZAMIEN})"
        ),
    )
    parser.add_argument(
        "--leads-json",
        dest="leads_json",
        default=os.environ.get(ENV_LEADS),
        help=(
            "write the new offers of this run (TOP/MOŻE/ZAMIEN, with full descriptions) here; "
            "the rolling-list step drafts and prices out of this file, so descriptions never "
            f"have to be re-fetched (env {ENV_LEADS})"
        ),
    )
    parser.add_argument(
        "--rejected-audit",
        dest="rejected_audit",
        default=os.environ.get(ENV_REJECTED),
        help="write the simple ODRZUĆ audit here; '-' for stdout; omit to skip (env {ENV_REJECTED})",
    )
    parser.add_argument(
        "--sources",
        default=os.environ.get(ENV_SOURCES),
        help=(
            "comma-separated adapter names; live default: the active production set "
            f"({','.join(ACTIVE_SOURCES)}); offline default: every fixture source "
            f"(env {ENV_SOURCES})"
        ),
    )
    parser.add_argument(
        "--fixtures",
        default=os.environ.get(ENV_FIXTURES),
        help="fixture dir -> offline deterministic mode, no network (env {ENV_FIXTURES})",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get(ENV_MODEL, "openrouter/auto"),
        help=f"OpenRouter model; ignored in --fixtures mode (env {ENV_MODEL})",
    )
    parser.add_argument(
        "--collect-timeout",
        type=float,
        default=60.0,
        help="per-source collect() wall-clock budget in seconds",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print per-source accounting")
    telegram = parser.add_argument_group("telegram config (validated here; never sent)")
    telegram.add_argument(
        "--telegram-token",
        default=os.environ.get(ENV_TG_TOKEN),
        help=f"bot token; only validated (env {ENV_TG_TOKEN})",
    )
    telegram.add_argument(
        "--telegram-chat-id",
        default=os.environ.get(ENV_TG_CHAT),
        help=f"chat id; only validated (env {ENV_TG_CHAT})",
    )
    return parser


def collect_with_timeout(source: object, timeout: float) -> list:
    """Run ``source.collect()`` under a wall-clock budget on a daemon thread."""
    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["leads"] = source.collect()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    thread = threading.Thread(
        target=_run, name=f"collect-{getattr(source, 'source', '?')}", daemon=True
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise CollectTimeoutError(
            f"[{getattr(source, 'source', '?')}] collect() exceeded {timeout:g}s timeout"
        )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["leads"]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.state:
        state_path = Path(args.state)
    else:
        print(f"snajper: --state is required (or set {ENV_STATE})", file=sys.stderr)
        return EXIT_USAGE

    if args.telegram_token or args.telegram_chat_id:
        try:
            TelegramDelivery(token=args.telegram_token, chat_id=args.telegram_chat_id).validate()
        except TelegramConfigError as exc:
            print(f"snajper: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if args.verbose:
            print("telegram: config validated as dry-run; no message will be sent", file=sys.stderr)

    state = PipelineState(state_path)
    try:
        return _run_pipeline(state, args)
    except KeyboardInterrupt:
        return EXIT_RUNTIME


def _run_pipeline(state: PipelineState, args: argparse.Namespace) -> int:
    from freelance_pipeline import pipeline

    if args.fixtures:
        from freelance_pipeline.offline import build_offline_adapters

        adapters = build_offline_adapters(args.fixtures)
        scorer = DeterministicScorer()
        mode_name = f"offline ({args.fixtures})"
    else:
        registry = AdapterRegistry()
        adapters = build_active_adapters(registry)
        scorer = OpenRouterScorer(model=args.model)
        mode_name = "live"

    names = [name for name in (args.sources or "").split(",") if name] if args.sources else None
    if names is not None:
        selected: dict[str, object] = {}
        for name in names:
            if name in adapters:
                selected[name] = adapters[name]
                continue
            # Registered-but-inactive adapters stay individually buildable so a
            # diagnostic run can exercise one source (e.g. the Oferteo radar,
            # switched off until Jan approves its live rows) without turning it
            # on for the production cron. Production options still apply.
            try:
                selected[name] = build_adapter(name, **ACTIVE_SOURCE_KWARGS.get(name, {}))
            except KeyError:
                print(f"snajper: unknown source {name!r}", file=sys.stderr)
                return 2
        adapters = selected

    delivery = ReportDelivery(path=args.report)

    try:
        result = pipeline.run_pipeline(
            adapters=adapters,
            state=state,
            scorer=scorer,
            delivery=delivery,
            top_delivery=(
                ReportDelivery(path=args.top_report) if args.top_report else None
            ),
            maybe_delivery=(
                ReportDelivery(path=args.moze_report) if args.moze_report else None
            ),
            rejected_delivery=(
                ReportDelivery(path=args.rejected_audit) if args.rejected_audit else None
            ),
            zamien_delivery=(
                ReportDelivery(path=args.zamien_report) if args.zamien_report else None
            ),
            poc_backlog_path=Path(args.poc_backlog) if args.poc_backlog else None,
            collector=lambda source: collect_with_timeout(source, args.collect_timeout),
        )
    except Exception as exc:  # noqa: BLE001 - scorer/delivery: nothing was committed
        print(f"snajper: pipeline failed (nothing committed, leads retryable): {exc}", file=sys.stderr)
        return 3

    if args.leads_json:
        _write_leads(Path(args.leads_json), result)

    _report_summary(result, mode_name, args.verbose)
    return 3 if result.collection.failures else 0


def _write_leads(path: Path, result) -> None:
    """Zapisz oferty do odpowiedzi (TOP/MOŻE/ZAMIEN) razem z treścią ogłoszenia.

    Kubełek ODRZUĆ nie jest zapisywany: na te ogłoszenia nie odpowiadamy. Zapis jest atomowy,
    a plik jest tym, z którego krok rolling listy bierze drafty i wyceny — dzięki temu nie
    pobieramy treści ogłoszeń drugi raz.
    """
    import json

    rows = [
        {
            "key": f"{scored.lead.source}:{scored.lead.source_id}",
            "source": scored.lead.source,
            "source_id": scored.lead.source_id,
            "url": scored.lead.url,
            "canonical_url": scored.lead.canonical_url,
            "title": scored.lead.title,
            "author": scored.lead.company_or_author or "",
            "description": scored.lead.description or "",
            "bucket": scored.bucket.value,
            "score": scored.score,
            "budget": scored.lead.budget,
            "currency": scored.lead.currency,
            "rationale": scored.rationale,
        }
        for scored in result.scored
        if scored.bucket is not ScoreBucket.ODRZUC
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _report_summary(result, mode_name: str, verbose: bool) -> None:
    print(f"snajper: mode={mode_name}", file=sys.stderr)
    for entry in result.collection.results:
        if entry.ok:
            line = f"  OK  {entry.source}: {len(entry.leads)} leads"
        else:
            line = (
                f"  ERR {entry.source}: {entry.error_message}"
                if verbose
                else f"  ERR {entry.source}: collection failed"
            )
        print(line, file=sys.stderr)
    print(
        f"  new candidates={len(result.candidates)} scored={len(result.scored)}"
        f" reported={result.reported_count} zamien={result.zamien_count}"
        f" committed={result.committed}",
        file=sys.stderr,
    )
    if not result.candidates:
        print("  zero new candidates (nothing awaited)", file=sys.stderr)


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())