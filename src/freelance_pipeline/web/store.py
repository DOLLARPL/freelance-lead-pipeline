"""Read model panelu: SQLite obok ledger.json, ktory zostaje zrodlem prawdy.

Podzial odpowiedzialnosci:

* ``ledger.json`` decyduje o statusie oferty i trzyma biezacy draft,
* SQLite trzyma to, czego ledger nie ma: opisy ogloszen, wersje draftow,
  historie statusow i wyceny,
* kazdy zapis idzie najpierw przez ``Ledger`` (mark_status, set_draft),
  a potem jest odzwierciedlany w SQLite. Nigdy odwrotnie.

Dzieki temu Telegram, crony i panel widza ten sam stan, a panel nie psuje
pliku, ktory czyta rownolegle cron (Ledger pisze pod flock, atomowo).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from freelance_pipeline.cennik import Cennik, Quote
from freelance_pipeline.ledger import (
    STATUS_NEW,
    STATUS_REPLIED,
    STATUS_SEEN,
    STATUS_SKIPPED,
    Ledger,
)

SCHEMA_VERSION = 1

DEFAULT_CACHE_DIR = Path("/opt/data/cache/snajper-v2")
DEFAULT_DB_PATH = Path("/opt/data/cache/snajper-web/snajper.db")

#: Statusy, ktore panel moze ustawic. ``new`` i ``expired`` naleza do pipeline'u.
PANEL_STATUSES = (STATUS_SEEN, STATUS_REPLIED, STATUS_SKIPPED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
  key               TEXT PRIMARY KEY,
  source            TEXT NOT NULL,
  source_id         TEXT NOT NULL,
  title             TEXT NOT NULL DEFAULT '',
  url               TEXT NOT NULL DEFAULT '',
  author            TEXT NOT NULL DEFAULT '',
  description       TEXT NOT NULL DEFAULT '',
  bucket            TEXT NOT NULL DEFAULT '',
  score             INTEGER,
  budget            REAL,
  currency          TEXT,
  rationale         TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','seen','replied','skipped','expired')),
  first_seen_at     TEXT,
  in_report_at      TEXT,
  status_changed_at TEXT,
  rules_version     TEXT,
  contact_email     TEXT NOT NULL DEFAULT '',
  last_number       INTEGER,
  number_seen_at    TEXT,
  imported_at       TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS leads_queue  ON leads(status, bucket, score DESC, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS leads_source ON leads(source, status);

CREATE TABLE IF NOT EXISTS draft_versions (
  id           INTEGER PRIMARY KEY,
  lead_key     TEXT NOT NULL REFERENCES leads(key) ON DELETE CASCADE,
  version      INTEGER NOT NULL,
  body         TEXT NOT NULL,
  instruction  TEXT NOT NULL DEFAULT '',
  origin       TEXT NOT NULL DEFAULT 'web',
  is_current   INTEGER NOT NULL DEFAULT 0,
  body_sha256  TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE (lead_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS draft_one_current
  ON draft_versions(lead_key) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS draft_lead ON draft_versions(lead_key, version DESC);

CREATE TABLE IF NOT EXISTS status_events (
  id          INTEGER PRIMARY KEY,
  lead_key    TEXT NOT NULL REFERENCES leads(key) ON DELETE CASCADE,
  from_status TEXT,
  to_status   TEXT NOT NULL,
  actor       TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS status_events_lead ON status_events(lead_key, at DESC);
CREATE INDEX IF NOT EXISTS status_events_at   ON status_events(at DESC);

CREATE TABLE IF NOT EXISTS quotes (
  id             INTEGER PRIMARY KEY,
  lead_key       TEXT NOT NULL REFERENCES leads(key) ON DELETE CASCADE,
  cennik_version TEXT,
  service        TEXT,
  level          TEXT,
  amount         REAL NOT NULL,
  currency       TEXT NOT NULL DEFAULT 'PLN',
  days           INTEGER NOT NULL,
  hours          REAL,
  note           TEXT NOT NULL DEFAULT '',
  origin         TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quotes_lead ON quotes(lead_key, created_at DESC);

CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY,
  lead_key   TEXT NOT NULL REFERENCES leads(key) ON DELETE CASCADE,
  body       TEXT NOT NULL,
  author     TEXT NOT NULL DEFAULT 'web',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_lead ON notes(lead_key, created_at DESC);

-- Watek wiadomosci z klientem: Jan wkleja tekst z portalu, parser rozbija go
-- na wypowiedzi. ``przeczytane`` liczy sie tylko dla kierunku 'klient'.
CREATE TABLE IF NOT EXISTS wiadomosci (
  id          INTEGER PRIMARY KEY,
  lead_key    TEXT NOT NULL REFERENCES leads(key) ON DELETE CASCADE,
  kierunek    TEXT NOT NULL CHECK (kierunek IN ('klient','ja')),
  tresc       TEXT NOT NULL,
  czas        TEXT NOT NULL DEFAULT '',
  zrodlo      TEXT NOT NULL DEFAULT 'useme',
  przeczytane INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wiadomosci_lead ON wiadomosci(lead_key, id);

CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint    TEXT PRIMARY KEY,
  p256dh      TEXT NOT NULL,
  auth        TEXT NOT NULL,
  label       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  last_ok_at  TEXT
);

-- Preferencje per uzytkownik (login z naglowka tozsamosci): filtry listy ofert.
-- Klucz = login, pref = nazwa zestawu ("filters"). Wartosc to JSON, zeby nowy filtr
-- nie wymagal migracji schematu.
CREATE TABLE IF NOT EXISTS user_prefs (
  login      TEXT NOT NULL,
  pref       TEXT NOT NULL,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (login, pref)
);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Polaczenie z baza panelu; tworzy katalog i schemat przy pierwszym uzyciu."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Kolumny dodane po pierwszym wdrozeniu: CREATE TABLE IF NOT EXISTS ich nie doda.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    for name, ddl in (("contact_email", "TEXT NOT NULL DEFAULT ''"),):
        if name not in columns:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {ddl}")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )


# --------------------------------------------------------------------------- import


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null") or default
    except (OSError, json.JSONDecodeError):
        return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:-1] + "+00:00" if text.endswith("Z") else text


def _norm(value: Any) -> Any:
    """Puste napisy i NULL to ten sam brak danych, inaczej import nigdy nie jest idempotentny."""
    if value is None or value == "":
        return None
    return value


def _best_descriptions(cache_dir: Path) -> tuple[dict[str, dict], str | None]:
    """Najdluzszy opis ogloszenia per klucz, plus nazwa pliku, z ktorego pochodzi."""
    best: dict[str, dict] = {}
    source_file: str | None = None
    files = sorted(cache_dir.glob("leads-*.json"), key=lambda p: p.stat().st_mtime)
    for path in files:
        payload = _read_json(path, [])
        if not isinstance(payload, list):
            continue
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "")
            if not key:
                continue
            current = best.get(key)
            if current is None or len(str(entry.get("description") or "")) > len(
                str(current.get("description") or "")
            ):
                best[key] = entry
                source_file = path.name
    return best, source_file


def _numbering(reports_dir: Path) -> tuple[dict[str, str], str | None]:
    """Najswiezsza mapa numer -> klucz (rolling albo instant)."""
    files = [
        p for p in reports_dir.glob("rolling-*.keys.json")
    ] + [p for p in reports_dir.glob("instant-*.keys.json")]
    files.sort(key=lambda p: p.stat().st_mtime)
    if not files:
        return {}, None
    newest = files[-1]
    payload = _read_json(newest, {})
    if not isinstance(payload, dict):
        return {}, None
    return {str(k): str(v) for k, v in payload.items()}, newest.name


def _prices_for(reports_dir: Path, stamp: str) -> dict[str, dict]:
    payload = _read_json(reports_dir / f"{stamp}.prices.json", {})
    return payload if isinstance(payload, dict) else {}


def _stamps(reports_dir: Path) -> list[tuple[str, dict[str, str]]]:
    """Wszystkie pary (stamp, mapa numer->klucz), od najstarszej."""
    out: list[tuple[str, dict[str, str]]] = []
    for path in sorted(reports_dir.glob("*.keys.json"), key=lambda p: p.stat().st_mtime):
        stamp = path.name[: -len(".keys.json")]
        payload = _read_json(path, {})
        if isinstance(payload, dict) and payload:
            out.append((stamp, {str(k): str(v) for k, v in payload.items()}))
    return out


def import_sources(
    conn: sqlite3.Connection,
    *,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    reports_dir: str | Path | None = None,
    ledger_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Idempotentny import ledger.json + leads-*.json + reports/*.keys|prices.json.

    Drugi przebieg bez zmian w zrodlach daje same zera.
    """
    cache_dir = Path(cache_dir)
    reports_dir = Path(reports_dir) if reports_dir else cache_dir / "reports"
    ledger_path = Path(ledger_path) if ledger_path else cache_dir / "ledger.json"

    ledger = Ledger(ledger_path)
    records = ledger.load()
    descriptions, leads_file = _best_descriptions(cache_dir)
    numbering, number_file = _numbering(reports_dir)

    stamp = now_iso()
    report = {
        "leads_new": 0,
        "leads_updated": 0,
        "status_synced": 0,
        "drafts_new": 0,
        "quotes_new": 0,
        "skipped": 0,
        "at": stamp,
        "leads_file": leads_file,
        "number_file": number_file,
    }

    known = {row["key"]: row for row in conn.execute("SELECT * FROM leads")}

    if dry_run:
        conn.execute("BEGIN")

    try:
        for key, record in records.items():
            detail = descriptions.get(key, {})
            row = known.get(key)
            values = {
                "source": record.source,
                "source_id": record.source_id,
                "title": record.title or str(detail.get("title") or ""),
                "url": record.url or str(detail.get("url") or ""),
                "author": str(detail.get("author") or ""),
                "description": str(detail.get("description") or ""),
                "bucket": record.bucket or str(detail.get("bucket") or ""),
                "score": record.score if record.score is not None else detail.get("score"),
                "budget": detail.get("budget"),
                "currency": detail.get("currency"),
                "rationale": str(detail.get("rationale") or ""),
                "status": record.status,
                "first_seen_at": _iso(record.first_seen_at),
                "in_report_at": _iso(record.in_report_at),
                "status_changed_at": _iso(record.status_changed_at),
                "rules_version": record.rules_version,
                "imported_at": stamp,
                "updated_at": stamp,
            }
            if row is None:
                values["last_number"] = _number_for(numbering, key)
                values["number_seen_at"] = stamp if values["last_number"] else None
                cols = ", ".join(values)
                marks = ", ".join("?" for _ in values)
                conn.execute(
                    f"INSERT INTO leads(key, {cols}) VALUES(?, {marks})",
                    (key, *values.values()),
                )
                conn.execute(
                    "INSERT INTO status_events(lead_key, from_status, to_status, actor, note, at) "
                    "VALUES(?, NULL, ?, 'migracja', '', ?)",
                    (key, values["status"], values["status_changed_at"] or stamp),
                )
                report["leads_new"] += 1
            else:
                if row["status"] != record.status:
                    conn.execute(
                        "INSERT INTO status_events(lead_key, from_status, to_status, actor, note, at) "
                        "VALUES(?, ?, ?, 'ledger-sync', '', ?)",
                        (key, row["status"], record.status, stamp),
                    )
                    report["status_synced"] += 1
                changed = any(
                    _norm(row[field_name]) != _norm(value)
                    for field_name, value in values.items()
                    if field_name not in ("imported_at", "updated_at")
                )
                conn.execute(
                    f"UPDATE leads SET {', '.join(f'{c} = ?' for c in values)} WHERE key = ?",
                    (*values.values(), key),
                )
                if changed:
                    report["leads_updated"] += 1

            number = _number_for(numbering, key)
            if number:
                conn.execute(
                    "UPDATE leads SET last_number = ?, number_seen_at = ? "
                    "WHERE key = ? AND (last_number IS NULL OR number_seen_at < ?)",
                    (number, stamp, key, stamp),
                )

            # draft: ledger trzyma biezaca tresc
            if record.draft:
                report["drafts_new"] += _sync_draft(
                    conn, key, record.draft, origin="ledger", created_at=_iso(record.draft_at) or stamp
                )

        # wyceny z prices.json (kazda para ze swoja mapa numerow)
        for price_stamp, mapping in _stamps(reports_dir):
            prices = _prices_for(reports_dir, price_stamp)
            for number, entry in prices.items():
                key = mapping.get(number)
                if not key or not isinstance(entry, dict):
                    continue
                amount = entry.get("amount")
                if not amount:
                    continue
                note = str(entry.get("note") or "")
                exists = conn.execute(
                    "SELECT 1 FROM quotes WHERE lead_key = ? AND amount = ? AND days = ? "
                    "AND note = ? LIMIT 1",
                    (key, float(amount), int(entry.get("days") or 1), note),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO quotes(lead_key, cennik_version, service, level, amount, currency, "
                    "days, hours, note, origin, created_at) VALUES(?, NULL, NULL, NULL, ?, ?, ?, NULL, ?, "
                    "'prices_json', ?)",
                    (
                        key,
                        float(amount),
                        str(entry.get("currency") or "PLN"),
                        int(entry.get("days") or 1),
                        note,
                        stamp,
                    ),
                )
                report["quotes_new"] += 1

        conn.execute(
            "INSERT INTO meta(key, value) VALUES('imported_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (stamp,),
        )
        if dry_run:
            conn.execute("ROLLBACK")
    except Exception:
        if dry_run:
            conn.execute("ROLLBACK")
        raise

    report["total"] = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    return report


def _number_for(numbering: dict[str, str], key: str) -> int | None:
    for number, mapped in numbering.items():
        if mapped == key:
            try:
                return int(number)
            except ValueError:
                return None
    return None


def _sync_draft(
    conn: sqlite3.Connection,
    key: str,
    body: str,
    *,
    origin: str,
    instruction: str = "",
    created_at: str | None = None,
) -> int:
    """Wstaw nowa wersje, jesli tresc rozni sie od biezacej. Zwraca 1 przy zmianie."""
    body = body.strip()
    if not body:
        return 0
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    current = conn.execute(
        "SELECT version, body_sha256 FROM draft_versions WHERE lead_key = ? AND is_current = 1",
        (key,),
    ).fetchone()
    if current is not None and current["body_sha256"] == digest:
        return 0
    last = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM draft_versions WHERE lead_key = ?", (key,)
    ).fetchone()["v"]
    version = last + 1
    conn.execute("UPDATE draft_versions SET is_current = 0 WHERE lead_key = ?", (key,))
    conn.execute(
        "INSERT INTO draft_versions(lead_key, version, body, instruction, origin, is_current, "
        "body_sha256, created_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
        (key, version, body, instruction, origin, digest, created_at or now_iso()),
    )
    return 1


# --------------------------------------------------------------------------- odczyt

QUEUE_STATUSES = (STATUS_NEW, STATUS_SEEN)


@dataclass
class LeadRow:
    key: str
    source: str
    title: str
    url: str
    bucket: str
    score: float | None
    status: str
    first_seen_at: str | None
    last_number: int | None
    author: str = ""
    budget: float | None = None
    currency: str | None = None
    version: int | None = None
    amount: float | None = None
    days: int | None = None
    quote_origin: str = ""
    notes_count: int = 0


@dataclass
class LeadDetail(LeadRow):
    description: str = ""
    rationale: str = ""
    rules_version: str | None = None
    status_changed_at: str | None = None
    draft: str | None = None
    draft_at: str | None = None
    quote_note: str = ""
    quote_hours: float | None = None
    contact_email: str = ""
    drafts: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)


def _quote_join() -> str:
    return (
        "LEFT JOIN (SELECT q.lead_key, q.amount, q.days, q.note, q.hours, q.id, q.origin FROM quotes q "
        "JOIN (SELECT lead_key, MAX(id) AS id FROM quotes GROUP BY lead_key) last ON last.id = q.id) "
        "q ON q.lead_key = l.key"
    )


def _age_cutoff(max_age_hours: float | None) -> str | None:
    """Granica wieku jako napis porownywalny w SQL.

    ``first_seen_at`` jest w bazie w formacie ISO ze spacja i offsetem, wiec
    porownujemy przez ``datetime()`` (SQLite rozumie oba warianty i sprowadza
    do UTC). Zero i wartosci ujemne znacza "bez filtra".
    """
    if not max_age_hours or max_age_hours <= 0:
        return None
    return (datetime.now(UTC) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")


def list_leads(
    conn: sqlite3.Connection,
    *,
    view: str = "queue",
    bucket: str | None = None,
    source: str | None = None,
    sources: Iterable[str] | None = None,
    q: str | None = None,
    min_score: int | None = None,
    without_quote: bool = False,
    max_age_hours: float | None = None,
    only_delivered: bool = False,
    sort: str = "queue",
    limit: int = 200,
    offset: int = 0,
) -> list[LeadRow]:
    where: list[str] = []
    params: list[Any] = []
    if view == "queue":
        where.append("l.status IN ('new','seen')")
    elif view == "replied":
        where.append("l.status = 'replied'")
    elif view == "skipped":
        where.append("l.status = 'skipped'")
    if bucket:
        where.append("l.bucket = ?")
        params.append(bucket)
    if source:
        where.append("l.source = ?")
        params.append(source)
    # Filtr platformy: pusta lista znaczy "wszystkie", tak jak brak parametru.
    wybrane = [s for s in (sources or []) if s]
    if wybrane:
        where.append(f"l.source IN ({', '.join('?' for _ in wybrane)})")
        params.extend(wybrane)
    if q:
        where.append("(l.title LIKE ? OR l.description LIKE ? OR l.author LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if min_score:
        where.append("COALESCE(l.score, 0) >= ?")
        params.append(min_score)
    if without_quote:
        where.append("q.amount IS NULL")
    cutoff = _age_cutoff(max_age_hours)
    if cutoff:
        where.append("datetime(l.first_seen_at) >= ?")
        params.append(cutoff)
    if only_delivered:
        where.append("l.in_report_at IS NOT NULL")
    order = {
        "queue": "CASE l.status WHEN 'seen' THEN 0 ELSE 1 END, "
        "CASE l.bucket WHEN 'TOP' THEN 0 WHEN 'MOŻE' THEN 1 ELSE 2 END, "
        "COALESCE(l.score, 0) DESC, l.first_seen_at DESC",
        "newest": "l.first_seen_at DESC",
        "oldest": "l.first_seen_at ASC",
        "score": "COALESCE(l.score, 0) DESC",
        "amount": "COALESCE(q.amount, 0) DESC",
    }.get(sort, "COALESCE(l.score, 0) DESC")
    sql = (
        "SELECT l.*, q.amount, q.days, q.origin AS quote_origin, q.note AS quote_note, "
        "(SELECT MAX(version) FROM draft_versions d WHERE d.lead_key = l.key) AS version, "
        "(SELECT COUNT(*) FROM notes n WHERE n.lead_key = l.key) AS notes_count "
        f"FROM leads l {_quote_join()} "
        + (f"WHERE {' AND '.join(where)} " if where else "")
        + f"ORDER BY {order} LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    return [
        LeadRow(
            key=row["key"],
            source=row["source"],
            title=row["title"],
            url=row["url"],
            bucket=row["bucket"],
            score=row["score"],
            status=row["status"],
            first_seen_at=row["first_seen_at"],
            last_number=row["last_number"],
            author=row["author"],
            budget=row["budget"],
            currency=row["currency"],
            version=row["version"],
            amount=row["amount"],
            days=row["days"],
            quote_origin=row["quote_origin"] or "",
            notes_count=row["notes_count"] or 0,
        )
        for row in conn.execute(sql, params)
    ]


def get_lead(conn: sqlite3.Connection, key: str) -> LeadDetail | None:
    row = conn.execute(
        "SELECT l.*, q.amount, q.days, q.origin AS quote_origin, q.note AS quote_note, q.hours AS quote_hours, "
        "(SELECT MAX(version) FROM draft_versions d WHERE d.lead_key = l.key) AS version, "
        "(SELECT COUNT(*) FROM notes n WHERE n.lead_key = l.key) AS notes_count "
        f"FROM leads l {_quote_join()} WHERE l.key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    drafts = [
        dict(r)
        for r in conn.execute(
            "SELECT version, body, instruction, origin, is_current, created_at "
            "FROM draft_versions WHERE lead_key = ? ORDER BY version DESC",
            (key,),
        )
    ]
    events = [
        dict(r)
        for r in conn.execute(
            "SELECT from_status, to_status, actor, note, at FROM status_events "
            "WHERE lead_key = ? ORDER BY id DESC LIMIT 20",
            (key,),
        )
    ]
    current = next((d for d in drafts if d["is_current"]), None)
    return LeadDetail(
        key=row["key"],
        source=row["source"],
        title=row["title"],
        url=row["url"],
        bucket=row["bucket"],
        score=row["score"],
        status=row["status"],
        first_seen_at=row["first_seen_at"],
        last_number=row["last_number"],
        author=row["author"],
        budget=row["budget"],
        currency=row["currency"],
        version=row["version"],
        amount=row["amount"],
        days=row["days"],
        quote_origin=row["quote_origin"] or "",
        description=row["description"],
        rationale=row["rationale"],
        rules_version=row["rules_version"],
        status_changed_at=row["status_changed_at"],
        draft=current["body"] if current else None,
        draft_at=current["created_at"] if current else None,
        quote_note=row["quote_note"] or "",
        quote_hours=row["quote_hours"],
        contact_email=row["contact_email"] or "",
        notes_count=row["notes_count"] or 0,
        drafts=drafts,
        events=events,
        notes=notes_for(conn, key),
    )


def counts(
    conn: sqlite3.Connection,
    *,
    max_age_hours: float | None = None,
    only_delivered: bool = False,
) -> dict[str, int]:
    """Liczniki statusow; opcjonalnie tylko oferty w oknie wieku.

    Panel podaje ten sam prog, co lista, zeby badge w naglowku nie klamal.
    Bez argumentow zachowanie jest globalne (cron, statystyki, push).
    """
    where: list[str] = []
    params: list[Any] = []
    cutoff = _age_cutoff(max_age_hours)
    if cutoff:
        where.append("datetime(first_seen_at) >= ?")
        params.append(cutoff)
    if only_delivered:
        where.append("in_report_at IS NOT NULL")
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    out = {"all": 0, "new": 0, "seen": 0, "replied": 0, "skipped": 0, "expired": 0, "no_quote": 0}
    for row in conn.execute(f"SELECT status, COUNT(*) AS n FROM leads{clause} GROUP BY status", params):
        out[row["status"]] = row["n"]
        out["all"] += row["n"]
    out["queue"] = out["new"] + out["seen"]
    no_quote_sql = (
        "SELECT COUNT(*) FROM leads l"
        + clause
        + (" AND " if where else " WHERE ")
        + "NOT EXISTS (SELECT 1 FROM quotes q WHERE q.lead_key = l.key)"
    )
    out["no_quote"] = conn.execute(no_quote_sql, params).fetchone()[0]
    return out


def sources(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT source FROM leads ORDER BY source")]


# --------------------------------------------------------------------------- preferencje

PREFS_FILTERS = "filters"


def get_user_pref(
    conn: sqlite3.Connection, login: str, pref: str = PREFS_FILTERS
) -> dict[str, Any]:
    """Zapisane filtry uzytkownika; brak wiersza albo smieci w JSON to pusty slownik."""
    row = conn.execute(
        "SELECT value FROM user_prefs WHERE login = ? AND pref = ?", (login, pref)
    ).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_user_pref(
    conn: sqlite3.Connection, login: str, data: dict[str, Any], pref: str = PREFS_FILTERS
) -> None:
    conn.execute(
        "INSERT INTO user_prefs(login, pref, value, updated_at) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(login, pref) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (login, pref, json.dumps(data, ensure_ascii=False, sort_keys=True), now_iso()),
    )


def normalize_filters(conn: sqlite3.Connection, raw: Any) -> dict[str, list[str]]:
    """Filtry z zewnatrz sprowadzone do znanych wartosci.

    Nieznana platforma (literowka, stare zrodlo, ktore zniklo z bazy) jest
    odrzucana zamiast zapisywana: inaczej filtr wraca po tygodniu i pokazuje
    pusta liste bez powodu.
    """
    znane = set(sources(conn))
    platformy = raw.get("platforms") if isinstance(raw, dict) else None
    if not isinstance(platformy, list):
        platformy = []
    return {"platforms": sorted({str(p) for p in platformy if str(p) in znane})}


def last_import(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'imported_at'").fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------- zapis


def save_status(
    conn: sqlite3.Connection,
    ledger: Ledger,
    key: str,
    status: str,
    *,
    note: str = "",
) -> LeadDetail | None:
    """Zmiana statusu: najpierw ledger (zrodlo prawdy), potem read model."""
    if status not in PANEL_STATUSES:
        raise ValueError(f"status {status!r} nie jest ustawialny z panelu")
    ledger.mark_status([key], status)
    record = ledger.get(key)
    if record is None:
        return None
    previous = conn.execute("SELECT status FROM leads WHERE key = ?", (key,)).fetchone()
    stamp = now_iso()
    conn.execute(
        "UPDATE leads SET status = ?, status_changed_at = ?, updated_at = ? WHERE key = ?",
        (record.status, _iso(record.status_changed_at) or stamp, stamp, key),
    )
    conn.execute(
        "INSERT INTO status_events(lead_key, from_status, to_status, actor, note, at) "
        "VALUES(?, ?, ?, 'web', ?, ?)",
        (key, previous["status"] if previous else None, record.status, note, stamp),
    )
    return get_lead(conn, key)


def save_draft(
    conn: sqlite3.Connection,
    ledger: Ledger,
    key: str,
    body: str,
    *,
    instruction: str = "",
    origin: str = "web",
) -> LeadDetail | None:
    body = (body or "").strip()
    if not body:
        raise ValueError("draft jest pusty")
    ledger.set_draft(key, body)
    _sync_draft(conn, key, body, origin=origin, instruction=instruction)
    return get_lead(conn, key)


def quote_for(
    conn: sqlite3.Connection, cennik: "Cennik | None", key: str, *, refresh: bool = False
) -> dict | None:
    """Wycena z cennika (nigdy z modelu); zapisuje nowa pozycje przy ``refresh``."""
    lead = get_lead(conn, key)
    if lead is None or cennik is None:
        return None
    if not refresh:
        stored = conn.execute(
            "SELECT amount, currency, days, note, hours, service, level, cennik_version "
            "FROM quotes WHERE lead_key = ? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if stored is not None:
            return dict(stored)
    quote: Quote | None = cennik.quote(lead.description, title=lead.title)
    if quote is None:
        return None
    conn.execute(
        "INSERT INTO quotes(lead_key, cennik_version, service, level, amount, currency, days, hours, "
        "note, origin, created_at) VALUES(?, ?, ?, ?, ?, 'PLN', ?, ?, ?, 'cennik', ?)",
        (
            key,
            quote.version,
            quote.service,
            quote.level,
            quote.amount,
            quote.days,
            quote.hours,
            f"cennik, {quote.service} {quote.level}",
            now_iso(),
        ),
    )
    return {
        "amount": quote.amount,
        "currency": "PLN",
        "days": quote.days,
        "hours": quote.hours,
        "service": quote.service,
        "level": quote.level,
        "cennik_version": quote.version,
        "note": f"cennik, {quote.service} {quote.level}",
    }


def latest_quote(conn: sqlite3.Connection, key: str) -> dict | None:
    """Ostatnia wycena dowolnego pochodzenia: cennik albo ręczna Jana."""
    row = conn.execute(
        "SELECT amount, currency, days, note, hours, service, level, cennik_version, origin "
        "FROM quotes WHERE lead_key = ? ORDER BY id DESC LIMIT 1",
        (key,),
    ).fetchone()
    return dict(row) if row is not None else None


def save_manual_quote(
    conn: sqlite3.Connection,
    key: str,
    amount: float,
    days: int,
    note: str = "",
    *,
    author: str = "jan",
) -> dict | None:
    """Wycena Jana: kwota i termin wpisane ręcznie. Zostaje w historii jako origin='jan'."""
    if amount is None or float(amount) <= 0:
        raise ValueError("kwota musi być większa od zera")
    if days is None or int(days) < 0:
        raise ValueError("termin nie może być ujemny")
    if get_lead(conn, key) is None:
        return None
    conn.execute(
        "INSERT INTO quotes(lead_key, cennik_version, service, level, amount, currency, days, hours, "
        "note, origin, created_at) VALUES(?, 'manual', 'manual', '', ?, 'PLN', ?, NULL, ?, 'jan', ?)",
        (key, float(amount), int(days), (note or "").strip() or "moja wycena", now_iso()),
    )
    conn.commit()
    return {
        "amount": float(amount),
        "currency": "PLN",
        "days": int(days),
        "hours": None,
        "service": "manual",
        "level": "",
        "cennik_version": "manual",
        "note": (note or "").strip() or "moja wycena",
        "origin": "jan",
        "author": author,
    }


def add_note(conn: sqlite3.Connection, key: str, body: str, *, author: str = "web") -> dict | None:
    """Notatka przy ofercie: telefon, SMS, ustalenia z klientem. Historia zostaje."""
    body = (body or "").strip()
    if not body:
        raise ValueError("notatka jest pusta")
    if conn.execute("SELECT 1 FROM leads WHERE key = ?", (key,)).fetchone() is None:
        return None
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO notes(lead_key, body, author, created_at) VALUES(?, ?, ?, ?)",
        (key, body[:2000], author, stamp),
    )
    return {"id": cursor.lastrowid, "body": body[:2000], "created_at": stamp, "author": author}


def notes_for(conn: sqlite3.Connection, key: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, body, author, created_at FROM notes WHERE lead_key = ? ORDER BY id DESC",
            (key,),
        )
    ]


# --------------------------------------------------------------------------- watek


def _plain(text: str) -> str:
    """Klucz porownania tresci: biale znaki i wielkosc liter bez znaczenia."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def add_messages(
    conn: sqlite3.Connection,
    key: str,
    items: Iterable[dict],
    *,
    zrodlo: str = "useme",
) -> dict[str, int] | None:
    """Zapisuje rozpoznane wiadomosci watku; duplikaty pomija.

    Jan wkleja ten sam watek po kazdej nowej odpowiedzi klienta, wiec bez
    odsiewu duplikatow historia roslaby o kopie. Klucz to kierunek + tresc.
    """
    if conn.execute("SELECT 1 FROM leads WHERE key = ?", (key,)).fetchone() is None:
        return None
    existing = {
        (row["kierunek"], _plain(row["tresc"]))
        for row in conn.execute("SELECT kierunek, tresc FROM wiadomosci WHERE lead_key = ?", (key,))
    }
    stamp = now_iso()
    dodane = 0
    pominiete = 0
    for item in items:
        kierunek = "ja" if str(item.get("kierunek")) == "ja" else "klient"
        tresc = (item.get("tresc") or "").strip()[:8000]
        if not tresc:
            pominiete += 1
            continue
        fingerprint = (kierunek, _plain(tresc))
        if fingerprint in existing:
            pominiete += 1
            continue
        existing.add(fingerprint)
        conn.execute(
            "INSERT INTO wiadomosci(lead_key, kierunek, tresc, czas, zrodlo, przeczytane, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                kierunek,
                tresc,
                (item.get("czas") or "").strip()[:40],
                (zrodlo or "useme").strip()[:24],
                0 if kierunek == "klient" else 1,
                stamp,
            ),
        )
        dodane += 1
    return {"dodane": dodane, "pominiete": pominiete}


def messages_for(conn: sqlite3.Connection, key: str) -> list[dict]:
    """Historia watku w kolejnosci wklejenia (najstarsze pierwsze)."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT id, kierunek, tresc, czas, zrodlo, przeczytane, created_at "
            "FROM wiadomosci WHERE lead_key = ? ORDER BY id",
            (key,),
        )
    ]


def unread_count(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM wiadomosci WHERE lead_key = ? AND kierunek = 'klient' "
        "AND przeczytane = 0",
        (key,),
    ).fetchone()
    return int(row[0] or 0)


def mark_read(conn: sqlite3.Connection, key: str) -> int:
    cursor = conn.execute(
        "UPDATE wiadomosci SET przeczytane = 1 WHERE lead_key = ? AND przeczytane = 0",
        (key,),
    )
    return int(cursor.rowcount or 0)


def lead_flags(conn: sqlite3.Connection) -> dict[str, dict]:
    """Ostatnia notatka i licznik notatek per oferta, do znacznika na liscie."""
    out: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT lead_key, COUNT(*) AS n, MAX(created_at) AS last_at FROM notes GROUP BY lead_key"
    ):
        out[row["lead_key"]] = {"count": row["n"], "last_at": row["last_at"]}
    return out


def daily_counts(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    out = []
    for row in conn.execute(
        "SELECT at, to_status FROM status_events WHERE actor = 'web' ORDER BY at DESC"
    ):
        try:
            stamp = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if stamp >= cutoff:
            out.append(dict(row))
    return out
