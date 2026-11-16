"""Import Oferteo request notifications from the mailbox into a JSONL lead store.

Read-only on the Oferteo side: the only network calls go to the Gmail API.
Nothing here opens ``/odkryj``, buys points or sends anything.

    python scripts/oferteo_mail_ingest.py --since 7d --dry-run --print
    python scripts/oferteo_mail_ingest.py --since 30d --limit 20
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from freelance_pipeline.lead_identity import identity_keys, merge_leads
from freelance_pipeline.mailbox import GmailMailbox, MailboxError
from freelance_pipeline.sources.oferteo_mail import (
    OferteoMailError,
    parse_oferteo_mail,
)

DEFAULT_TOKEN = "/opt/data/google_token.json"
DEFAULT_STORE = "data/oferteo_leads.jsonl"


def _parse_since(value: str) -> timedelta:
    text = value.strip().lower()
    if text.endswith("d"):
        return timedelta(days=int(text[:-1]))
    if text.endswith("h"):
        return timedelta(hours=int(text[:-1]))
    return timedelta(days=int(text))


def _load(store: Path) -> tuple[list[dict], dict[str, int]]:
    rows: list[dict] = []
    index: dict[str, int] = {}
    if not store.exists():
        return rows, index
    for line in store.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows.append(row)
        for key in row.get("_keys", []):
            index[key] = len(rows) - 1
    return rows, index


def _plain(value):
    """Unfreeze nested FrozenDict/FrozenList so the row can be JSON-encoded."""
    from collections.abc import Mapping, Sequence

    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_plain(v) for v in value]
    return value


def _row(lead) -> dict:
    """Serialize a lead. ``dataclasses.asdict`` cannot be used: raw_data holds a
    FrozenDict, which deepcopy refuses to rebuild."""
    data = {
        "source": lead.source,
        "source_id": lead.source_id,
        "url": lead.url,
        "canonical_url": lead.canonical_url,
        "title": lead.title,
        "description": lead.description,
        "company_or_author": lead.company_or_author,
        "published_at": lead.published_at.isoformat() if lead.published_at else None,
        "collected_at": lead.collected_at.isoformat(),
        "budget": lead.budget,
        "currency": lead.currency,
        "location": lead.location,
        "status": lead.status,
        "raw_data": _plain(lead.raw_data or {}),
    }
    data["_keys"] = list(identity_keys(lead))
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="7d")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    store = Path(args.store)
    rows, index = _load(store)
    known = {row["raw_data"].get("mail_id") for row in rows if row.get("raw_data")}

    box = GmailMailbox(args.token)
    try:
        messages = box.fetch(
            since=_parse_since(args.since),
            limit=args.limit,
            known_ids=[m for m in known if m],
        )
    except MailboxError as exc:
        print(f"mailbox: {exc}", file=sys.stderr)
        return 2

    stats = {"mail": len(messages), "new": 0, "merged": 0, "skipped": 0, "error": 0}
    touched: list[dict] = []
    for msg in messages:
        try:
            lead = parse_oferteo_mail(msg)
        except OferteoMailError as exc:
            stats["error"] += 1
            print(f"parse: {exc}", file=sys.stderr)
            continue
        if lead is None:
            stats["skipped"] += 1
            continue
        row = _row(lead)
        hit = next((index[k] for k in row["_keys"] if k in index), None)
        if hit is None:
            rows.append(row)
            for key in row["_keys"]:
                index[key] = len(rows) - 1
            stats["new"] += 1
            touched.append(row)
        else:
            existing = rows[hit]
            merged = merge_leads(_row_to_lead(existing), lead)
            if merged is not lead:
                rows[hit] = _row(merged)
                stats["merged"] += 1
                touched.append(rows[hit])

    if not args.dry_run and touched:
        store.parent.mkdir(parents=True, exist_ok=True)
        with store.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.json:
        print(json.dumps({"stats": stats, "touched": touched}, ensure_ascii=False, indent=1))
    else:
        print(
            "mail={mail} new={new} merged={merged} skipped={skipped} error={error}".format(
                **stats
            )
        )
        if args.show:
            for row in touched:
                print(_line(row))
    return 0


def _line(row: dict) -> str:
    raw = row.get("raw_data") or {}
    return " | ".join(
        [
            (row.get("published_at") or "")[:16],
            (row.get("location") or "?")[:16],
            (row.get("title") or "?")[:46],
            "tel" if raw.get("has_phone") else "mail",
            row.get("status") or "",
        ]
    )


def _row_to_lead(row: dict):
    from datetime import datetime

    from freelance_pipeline.models import JobLead

    def dt(value):
        return datetime.fromisoformat(value) if value else None

    return JobLead(
        source=row["source"],
        source_id=row["source_id"],
        url=row["url"],
        canonical_url=row.get("canonical_url") or "",
        title=row.get("title") or "",
        description=row.get("description") or "",
        company_or_author=row.get("company_or_author"),
        published_at=dt(row.get("published_at")),
        budget=row.get("budget"),
        currency=row.get("currency"),
        location=row.get("location"),
        status=row.get("status"),
        raw_data=row.get("raw_data") or {},
    )


if __name__ == "__main__":
    raise SystemExit(main())
