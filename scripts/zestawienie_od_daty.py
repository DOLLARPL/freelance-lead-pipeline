"""Zestawienie ofert, które wpłynęły od piątku (2026-10-17) — źródło: rejestr + leads-*.json."""

from __future__ import annotations

import glob
import json
from pathlib import Path

LEDGER = Path("/opt/data/cache/snajper-v2/ledger.json")
CUTOFF = "2026-10-17"
OUT = Path("/opt/data/projects/snajper-ofert/OFERTY-OD-2026-10-17.md")

BUCKET_ORDER = {"TOP": 0, "MOŻE": 1, "ZAMIEN": 2}
SOURCE_ORDER = {"useme": 0, "facebook_groups": 1}
DESC_LIMIT = 1400


def load_offers() -> dict[str, dict]:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    offers = {
        key: record
        for key, record in ledger.items()
        if (record.get("first_seen_at") or "")[:10] >= CUTOFF
    }
    for path in sorted(glob.glob("/opt/data/cache/snajper-v2/leads-*.json")):
        try:
            rows = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            key = row.get("key")
            if key in offers and not offers[key].get("description"):
                offers[key]["description"] = row.get("description") or ""
                offers[key]["author"] = offers[key].get("author") or row.get("author") or ""
    return offers


def main() -> None:
    offers = load_offers()
    ordered = sorted(
        offers.items(),
        key=lambda item: (
            item[1].get("first_seen_at") or "",
            BUCKET_ORDER.get(item[1].get("bucket", ""), 9),
            -(float(item[1].get("score") or 0)),
        ),
    )
    days: dict[str, int] = {}
    for _, record in ordered:
        day = (record.get("first_seen_at") or "")[:10]
        days[day] = days.get(day, 0) + 1

    lines = [
        f"# Zlecenia od piątku {CUTOFF} ({len(ordered)} ofert)",
        "",
        " · ".join(f"{day}: {count}" for day, count in sorted(days.items())),
        "",
        "Każda pozycja: data wpływu, źródło, ocena, link do ogłoszenia, skrót treści i gotowa "
        "wiadomość z wyceną (do skopiowania). Brak wiadomości = oferta nie miała jeszcze draftu.",
        "",
    ]
    for index, (_, record) in enumerate(ordered, start=1):
        day = (record.get("first_seen_at") or "")[:10]
        title = (record.get("title") or "").strip()
        source = record.get("source") or ""
        lines += [
            "---",
            "",
            f"## [{index}] {day} · {record.get('bucket') or '?'} · {record.get('score') or '?'}/100 · {source}",
            "",
            f"### {title}",
            "",
            f"<{record.get('url') or ''}>",
            "",
        ]
        meta = []
        if record.get("author"):
            meta.append(f"autor: {record['author']}")
        if record.get("draft_at"):
            meta.append(f"lista i wiadomość: {(record.get('draft_at') or '')[:10]}")
        if meta:
            lines += [" · ".join(meta), ""]
        description = (record.get("description") or "").strip()
        if description:
            lines += [
                "**Treść ogłoszenia (skrót):**",
                "",
                "> " + description[:DESC_LIMIT].replace("\n", "\n> "),
                "",
            ]
        draft = (record.get("draft") or "").strip()
        lines += ["**Wiadomość do wysłania:**", ""]
        if draft:
            lines += ["```text", draft, "```", ""]
        else:
            lines += ["(brak draftu — poproś w czacie o wiadomość do tej oferty)", ""]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"{OUT} — {len(ordered)} ofert, {OUT.stat().st_size} B")
    for day, count in sorted(days.items()):
        print(day, count)


if __name__ == "__main__":
    main()
