"""Powiadomienia push (VAPID + Web Push) dla panelu snajpera.

Klucze VAPID leza na dysku obok bazy panelu: prywatny PEM (600, uid 10000) i publiczny
base64 w pliku tekstowym. Subskrypcje przegladarek trzyma SQLite (tabela push_subscriptions),
zeby cron mogl wyslac powiadomienie bez otwierania panelu.

Wysylka idzie przez usluge push przegladarki (Apple, Mozilla, Google). Endpoint zwraca
201 przy przyjeciu, 404/410 gdy subskrypcja wygasla: takie wpisy usuwamy, zeby nie rosly w nieskonczonosc.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .store import now_iso

DEFAULT_SUBJECT = "mailto:jan@jnxautomation.pl"


@dataclass
class PushResult:
    sent: int
    failed: int
    removed: int
    errors: list[str]


def public_key(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def ensure_keys(private_path: Path, public_path: Path) -> str:
    """Generuje pare VAPID, jesli jej nie ma. Zwraca klucz publiczny (base64url)."""
    if private_path.exists() and public_path.exists():
        return public_key(public_path)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid import Vapid

    private_path.parent.mkdir(parents=True, exist_ok=True)
    vapid = Vapid()
    vapid.generate_keys()
    vapid.save_key(str(private_path))
    raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    public_path.write_text(encoded + "\n", encoding="utf-8")
    private_path.chmod(0o600)
    return encoded


def save_subscription(conn: sqlite3.Connection, payload: dict, *, label: str = "") -> str:
    """Zapisuje subskrypcje (idempotentnie po endpointcie). Zwraca endpoint."""
    endpoint = str(payload.get("endpoint") or "").strip()
    keys = payload.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise ValueError("subskrypcja bez endpointu albo kluczy")
    conn.execute(
        "INSERT INTO push_subscriptions(endpoint, p256dh, auth, label, created_at, last_ok_at) "
        "VALUES(?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, "
        "label = excluded.label",
        (endpoint, p256dh, auth, label[:60], now_iso()),
    )
    return endpoint


def delete_subscription(conn: sqlite3.Connection, endpoint: str) -> int:
    cursor = conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    return cursor.rowcount


def subscriptions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM push_subscriptions ORDER BY created_at")]


def send(
    conn: sqlite3.Connection,
    *,
    private_path: Path,
    subject: str = DEFAULT_SUBJECT,
    title: str,
    body: str,
    url: str = "/",
    tag: str = "snajper",
) -> PushResult:
    """Wysyla powiadomienie do wszystkich zapisanych urzadzen.

    Wygaśniete subskrypcje (404, 410) usuwa. Kazdy inny blad tylko raportuje, bo jedno
    zepsute urzadzenie nie moze blokowac pozostalych.
    """
    from pywebpush import WebPushException, webpush

    data = json.dumps({"title": title, "body": body, "url": url, "tag": tag}, ensure_ascii=False)
    sent = failed = removed = 0
    errors: list[str] = []

    for row in subscriptions(conn):
        info = {
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        try:
            webpush(
                subscription_info=info,
                data=data,
                vapid_private_key=str(private_path),
                vapid_claims={"sub": subject},
                ttl=3600,
                headers={"Urgency": "high"},
            )
            conn.execute(
                "UPDATE push_subscriptions SET last_ok_at = ? WHERE endpoint = ?",
                (now_iso(), row["endpoint"]),
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                delete_subscription(conn, row["endpoint"])
                removed += 1
                continue
            failed += 1
            errors.append(f"{status or 'brak'}: {str(exc)[:120]}")
        except Exception as exc:  # noqa: BLE001 - jedno urzadzenie nie blokuje reszty
            failed += 1
            errors.append(str(exc)[:120])

    return PushResult(sent=sent, failed=failed, removed=removed, errors=errors[:5])
