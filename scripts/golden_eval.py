"""Golden-set precision measurement for the Snajper v2 selection layer.

This script is a measurement, not a feature: it feeds the labelled fixture
``tests/fixtures/golden_set.jsonl`` (37 real listings: 13 ``keep``, 24
``reject``) through the production scorer — model prompt plus the
deterministic ``apply_user_policy`` exclusions — and prints how many rows
landed in the right bucket. A row may carry ``published_at`` (ISO date); the
age rule then decides it deterministically, which is how Jan's 2026-09-21
verdict on the stale Perca landing-page offer is encoded.

Thresholds (HANDOFF, kryteria akceptacji):
  * every ``reject`` row must land in ODRZUĆ (100%),
  * at least 80% of the ``keep`` rows must stay in TOP/MOŻE.

Usage::

    uv run python scripts/golden_eval.py
    uv run python scripts/golden_eval.py --model google/gemini-2.5-flash-lite
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from freelance_pipeline.models import JobLead
from freelance_pipeline.openrouter import OpenRouterScorer
from freelance_pipeline.scoring import ScoreBucket

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = REPO_ROOT / "tests" / "fixtures" / "golden_set.jsonl"
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"

MIN_KEEP_RATIO = 0.8


def load_golden(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"golden_eval: missing golden set: {path}")
    rows: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"golden_eval: {path}:{lineno} is not valid JSON: {exc}") from exc
        if row.get("label") not in {"keep", "reject"}:
            raise SystemExit(f"golden_eval: {path}:{lineno} has an unknown label {row.get('label')!r}")
        rows.append(row)
    if not rows:
        raise SystemExit(f"golden_eval: golden set is empty: {path}")
    return rows


def to_lead(row: dict, index: int) -> JobLead:
    rate = row.get("rate") or {}
    published = row.get("published_at")
    published_at = None
    if published:
        try:
            published_at = datetime.fromisoformat(published)
        except ValueError as exc:
            raise SystemExit(
                f"golden_eval: row {row.get('id')!r} has an invalid published_at {published!r}"
            ) from exc
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
    return JobLead(
        source=row["source"],
        source_id=f"golden-{index}",
        url=row.get("url") or f"https://example.invalid/golden/{index}",
        title=row.get("title") or "",
        description=row.get("description") or "",
        published_at=published_at,
        company_or_author=row.get("company_or_author"),
        budget=row.get("budget"),
        currency=row.get("currency"),
        skills=tuple(row.get("skills") or ()),
        raw_data=(
            {
                "rate_min": rate.get("rate_min"),
                "rate_max": rate.get("rate_max"),
                "rate_unit": rate.get("rate_unit"),
            }
            if rate.get("rate_unit")
            else None
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="golden_eval", description=__doc__)
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="path to golden_set.jsonl")
    parser.add_argument("--model", default=os.environ.get("SNAJPER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args(argv)

    rows = load_golden(Path(args.golden))
    leads = [to_lead(row, index) for index, row in enumerate(rows)]
    scorer = OpenRouterScorer(model=args.model, batch_size=args.batch_size)
    scored = scorer.score(leads)

    rejected_ok = 0
    rejected_total = 0
    kept_ok = 0
    kept_total = 0
    misses: list[str] = []

    print(f"golden_eval: model={args.model} rows={len(rows)}")
    for row, entry in zip(rows, scored, strict=True):
        bucket = entry.bucket
        if row["label"] == "reject":
            rejected_total += 1
            ok = bucket is ScoreBucket.ODRZUC
            rejected_ok += int(ok)
        else:
            kept_total += 1
            ok = bucket in (ScoreBucket.TOP, ScoreBucket.MOZE)
            kept_ok += int(ok)
        mark = "OK " if ok else "MISS"
        print(f"  {mark} [{row['label']:<6}] {bucket.value:<7} {entry.score:>3}/100 — {row['id']}: {row['title'][:70]}")
        if not ok:
            misses.append(
                f"  {row['id']} ({row['label']}) -> {bucket.value} {entry.score}/100: {entry.rationale}"
            )

    print()
    required_kept = math.ceil(kept_total * MIN_KEEP_RATIO)
    print(f"reject: {rejected_ok}/{rejected_total} (wymagane {rejected_total}/{rejected_total})")
    print(f"keep:   {kept_ok}/{kept_total} (wymagane >= {required_kept}/{kept_total})")
    if misses:
        print("\nmisses:")
        for line in misses:
            print(line)

    passed = rejected_ok == rejected_total and kept_ok >= required_kept
    print()
    print("WYNIK: PASS" if passed else "WYNIK: FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
