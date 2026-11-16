"""Zakładka Outreach w panelu JNX (snajper-web, 8787).

Podgląd maili, uwagi, zatwierdzenie i wysyłka przez endpoint home.pl.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("OUTREACH_DB", "/opt/data/panel/outreach.db")
os.environ.setdefault("OUTREACH_TOKEN_FILE", "/opt/data/panel/outreach-token")
sys.path.insert(0, "/opt/data/projects/outreach")

import outreach  # noqa: E402
from fastapi import APIRouter, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from . import store  # noqa: E402
from .routes import _nav_counts, templates  # noqa: E402

router = APIRouter()


def _ensure_note_column() -> None:
    with outreach.db() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(leads)")]
        if "note" not in cols:
            conn.execute("ALTER TABLE leads ADD COLUMN note TEXT")


def _ready(limit: int = 30) -> list[dict]:
    with outreach.db() as conn:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE status='new' AND email IS NOT NULL AND priority IN ('A','B')
               ORDER BY priority, signal_date DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


@router.get("/outreach", response_class=HTMLResponse)
def outreach_page(request: Request) -> HTMLResponse:
    _ensure_note_column()
    rows = _ready()
    items = []
    for row in rows:
        result = outreach.compose(row["id"], "a" if row.get("person") else "b", "M1")
        items.append({
            "id": row["id"],
            "company": row["company"],
            "email": row["email"],
            "signal_date": row["signal_date"],
            "signal_text": row["signal_text"],
            "priority": row["priority"],
            "subject": result["subject"],
            "body": result["body"],
            "note": row.get("note") or "",
            "problems": result["problems"],
        })
    with outreach.db() as conn:
        sent = conn.execute("SELECT COUNT(*) c FROM sends WHERE status='sent'").fetchone()["c"]
        contacted = conn.execute("SELECT COUNT(*) c FROM leads WHERE status='contacted'").fetchone()["c"]
        replied = conn.execute("SELECT COUNT(*) c FROM leads WHERE status IN ('replied','negative')").fetchone()["c"]
    conn = request.app.state.conn
    counts = _nav_counts(request)
    return templates.TemplateResponse(request, "outreach.html", {
        "items": items,
        "sent": sent,
        "contacted": contacted,
        "replied": replied,
        "counts": counts,
        "imported_at": store.last_import(conn),
    })


@router.post("/api/outreach/uwaga")
def outreach_note(payload: dict) -> JSONResponse:
    lead_id = int(payload.get("lead_id", 0))
    note = str(payload.get("note", ""))[:2000]
    with outreach.db() as conn:
        conn.execute("UPDATE leads SET note=? WHERE id=?", (note, lead_id))
    return JSONResponse({"ok": True})


@router.post("/api/outreach/start")
def outreach_start(payload: dict) -> JSONResponse:
    ids = [int(x) for x in (payload.get("ids") or [])]
    results = []
    for lead_id in ids:
        try:
            lead = outreach.get_lead(lead_id)
            result = outreach.compose(lead_id, "a" if lead.get("person") else "b", "M1")
            if result["problems"]:
                results.append({"company": lead["company"], "ok": False,
                                "error": "; ".join(result["problems"][:2])})
                continue
            if not outreach.send_http(lead["email"], result["subject"], result["body"]):
                results.append({"company": lead["company"], "ok": False, "error": "endpoint odrzucił"})
                continue
            with outreach.db() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO sends (lead_id, step, variant, subject, body, status, scheduled_for, sent_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (lead_id, "M1", "a" if lead.get("person") else "b", result["subject"],
                     result["body"], "sent", date.today().isoformat(),
                     datetime.now().isoformat(timespec="seconds")),
                )
                conn.execute("UPDATE leads SET status='contacted' WHERE id=? AND status='new'", (lead_id,))
            results.append({"company": lead["company"], "ok": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"company": f"lead {lead_id}", "ok": False, "error": type(exc).__name__})
    outreach.set_setting("send_enabled", "1")
    outreach.set_setting("approved_at", datetime.now().isoformat(timespec="seconds"))
    return JSONResponse({"ok": all(r["ok"] for r in results), "results": results})
