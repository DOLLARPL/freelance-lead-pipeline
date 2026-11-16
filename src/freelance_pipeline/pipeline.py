from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from freelance_pipeline.dedup import DedupeDrop, PipelineState
from freelance_pipeline.delivery import Delivery
from freelance_pipeline.models import JobLead
from freelance_pipeline.orchestrator import CollectionRun, collect_all
from freelance_pipeline.poc import PocEntry, update_backlog
from freelance_pipeline.reporting import (
    render_chat_report,
    render_moze_report,
    render_top_report,
    render_zamien_report,
)
from freelance_pipeline.scoring import (
    ScoreBucket,
    ScoredLead,
    Scorer,
    render_rejected_audit,
)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Everything one pipeline run produced, in a single immutable bundle."""

    collection: CollectionRun
    candidates: tuple[JobLead, ...]
    dropped: tuple[DedupeDrop, ...]
    scored: tuple[ScoredLead, ...]
    report_text: str
    top_report: str
    maybe_report: str
    rejected_audit: str
    zamien_report: str
    reported_count: int
    committed: bool
    poc_entries: tuple[PocEntry, ...] = ()

    @property
    def zamien_count(self) -> int:
        """How many offers of this run landed in the ZAMIEN bucket."""
        return sum(1 for entry in self.scored if entry.bucket is ScoreBucket.ZAMIEN)

    @property
    def had_new_leads(self) -> bool:
        return bool(self.candidates)


def run_pipeline(
    *,
    adapters: Mapping[str, object],
    state: PipelineState,
    scorer: Scorer,
    delivery: Delivery,
    top_delivery: Delivery | None = None,
    maybe_delivery: Delivery | None = None,
    rejected_delivery: Delivery | None = None,
    zamien_delivery: Delivery | None = None,
    poc_backlog_path: Path | None = None,
    report_now: datetime | None = None,
    collector: Callable[[object], Sequence[JobLead]] | None = None,
) -> PipelineResult:
    """Run the full pipeline: collect, dedupe, score, report, deliver, commit.

    * Collection is isolated per source: a failing source never aborts the
      others and never blocks their candidates.
    * Dedup stages candidates against persisted + in-batch state and touches
      nothing.
    * Scoring must return one strictly validated ``ScoredLead`` per candidate
      or raises; any mismatch leaves every lead retryable (nothing committed).
    * The chat report (every TOP and MOŻE row, uncapped, Polish sources first)
      is rendered for ``delivery``; every MOŻE row also goes to
      ``maybe_delivery`` (the per-run file). ``top_delivery`` receives the
      TOP-only archive. The simple ODRZUĆ audit is rendered for
      ``rejected_delivery``, and ``zamien_delivery`` receives the ZAMIEN list
      (offers where Jan proposes automation instead of the hire the client asked
      about) — a separate file and, in production, a separate Telegram message,
      never a row inside the main report.
    * ``delivery.send_report`` is the confirmation callback: state is committed
      *only after* it returns normally. A scoring, delivery or collection-wide
      failure therefore leaves every candidate retryable and the state file
      byte-for-byte unchanged.

    ``poc_backlog_path`` (optional) collects the portfolio gaps the model spotted
    into a deduplicated file. It is never delivered anywhere by itself.
    """
    timestamp = report_now if report_now is not None else datetime.now(UTC)
    collection = collect_all(adapters, collector=collector)
    dedupe_result = state.dedupe(collection.all)
    candidates = dedupe_result.candidates

    scored = tuple(scorer.score(candidates)) if candidates else ()
    report_text = render_chat_report(scored, now=timestamp)
    top_report = render_top_report(scored, now=timestamp)
    maybe_report = render_moze_report(scored, now=timestamp)
    audit = render_rejected_audit(scored)
    zamien_report = render_zamien_report(scored, now=timestamp)
    poc_entries = (
        tuple(update_backlog(poc_backlog_path, scored, now=timestamp))
        if poc_backlog_path is not None
        else ()
    )

    # Delivery confirmation is the commit gate: these calls must return normally.
    # They also run for an empty run so their sinks reflect "zero new".
    delivery.send_report(report_text)
    if top_delivery is not None:
        top_delivery.send_report(top_report)
    if maybe_delivery is not None:
        maybe_delivery.send_report(maybe_report)
    if rejected_delivery is not None:
        rejected_delivery.send_report(audit)
    if zamien_delivery is not None:
        zamien_delivery.send_report(zamien_report)

    # State is written atomically and only now, only for the successfully
    # processed candidates of this run (scored or rejected, delivered or not).
    committed = bool(candidates)
    if committed:
        state.commit(candidates)

    return PipelineResult(
        collection=collection,
        candidates=candidates,
        dropped=dedupe_result.dropped,
        scored=scored,
        report_text=report_text,
        top_report=top_report,
        maybe_report=maybe_report,
        rejected_audit=audit,
        zamien_report=zamien_report,
        reported_count=sum(1 for s in scored if s.bucket in (ScoreBucket.TOP, ScoreBucket.MOZE)),
        committed=committed,
        poc_entries=poc_entries,
    )


__all__ = ["PipelineResult", "run_pipeline"]