"""Dostep do panelu: konta, sesje, audyt.

Panel stoi na 127.0.0.1 i jest wystawiony przez Caddy pod
``snajper.jnxautomation.pl``. Wlasnym logowaniem rzadzi ten modul:

* konto = login + hash hasla (PBKDF2-SHA256, 600 tys. iteracji, sol na konto),
* sesja = losowy token w ciasteczku ``HttpOnly; Secure; SameSite=Lax``;
  w bazie lezy tylko SHA-256 tokenu, wiec wyciek bazy nie daje sesji,
* ``Tailscale-User-Login`` jest honorowany **tylko** dla zadan z loopbacku
  i tylko gdy Host nie jest publicznym adresem (tailnet zostaje jako wejscie
  awaryjne dla Jana, publiczny adres nie ufa naglowkowi),
* kazdy POST laduje w ``audit_log`` (kto, sciezka, kod, ip).

Hasla nie przechodza przez czat ani przez pliki w repo: zaklada je
``scripts/user_cli.py`` uruchamiany na serwerze.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

COOKIE = "snajper_session"
SESSION_DAYS = 14
PBKDF2_ITER = 600_000
OWNER_HEADER = "Tailscale-User-Login"

PUBLIC_PATHS = ("/login", "/invite", "/static/", "/sw.js", "/api/health", "/favicon.ico")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
router = APIRouter()

#: Login biezacego zadania, zeby szablony mogly pokazac naglowek z wylogowaniem
#: bez przekazywania zmiennej w kazdym z 28 widokow.
_current_user: ContextVar[str] = ContextVar("snajper_current_user", default="")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  login              TEXT UNIQUE NOT NULL,
  imie               TEXT NOT NULL DEFAULT '',
  hash               TEXT NOT NULL,
  rola               TEXT NOT NULL DEFAULT 'owner',
  aktywny            INTEGER NOT NULL DEFAULT 1,
  utworzono          TEXT NOT NULL,
  ostatnie_logowanie TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  login      TEXT NOT NULL,
  utworzono  TEXT NOT NULL,
  wygasa     TEXT NOT NULL,
  ip         TEXT NOT NULL DEFAULT '',
  ua         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_login ON sessions(login);

CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  at      TEXT NOT NULL,
  login   TEXT NOT NULL DEFAULT '',
  akcja   TEXT NOT NULL,
  sciezka TEXT NOT NULL DEFAULT '',
  kod     INTEGER,
  ip      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at DESC);

CREATE TABLE IF NOT EXISTS logowania (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  at    TEXT NOT NULL,
  login TEXT NOT NULL DEFAULT '',
  ip    TEXT NOT NULL DEFAULT '',
  ok    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invites (
  token  TEXT PRIMARY KEY,
  login  TEXT NOT NULL,
  wygasa TEXT NOT NULL,
  uzyty  TEXT
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(path: str | Path) -> None:
    with db(path) as conn:
        conn.executescript(SCHEMA)


# --- hasla -----------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt.hex()}${digest.hex()}"


def verify_password(password: str, zapis: str) -> bool:
    try:
        alg, iteracje, salt_hex, digest_hex = zapis.split("$")
        if alg != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iteracje)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:  # noqa: BLE001 - uszkodzony hash ma dac "nie", nie 500
        return False


def create_user(path: str | Path, login: str, haslo: str, imie: str = "", rola: str = "owner") -> None:
    login = login.strip().lower()
    if not login or "@" not in login:
        raise SystemExit("login musi byc adresem e-mail")
    if len(haslo) < 10:
        raise SystemExit("haslo musi miec co najmniej 10 znakow")
    with db(path) as conn:
        conn.execute(
            "INSERT INTO users(login, imie, hash, rola, aktywny, utworzono) VALUES (?,?,?,?,1,?)",
            (login, imie, hash_password(haslo), rola, now_iso()),
        )


def set_password(path: str | Path, login: str, haslo: str) -> None:
    if len(haslo) < 10:
        raise SystemExit("haslo musi miec co najmniej 10 znakow")
    with db(path) as conn:
        cur = conn.execute(
            "UPDATE users SET hash=? WHERE login=?", (hash_password(haslo), login.strip().lower())
        )
        if cur.rowcount == 0:
            raise SystemExit(f"brak konta {login}")
        # zmiana hasla konczy wszystkie sesje tego konta
        conn.execute("DELETE FROM sessions WHERE login=?", (login.strip().lower(),))


def list_users(path: str | Path) -> list[dict]:
    with db(path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT login, imie, rola, aktywny, utworzono, ostatnie_logowanie FROM users ORDER BY id"
            )
        ]


# --- sesje -----------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(path: str | Path, login: str, ip: str = "", ua: str = "") -> str:
    token = secrets.token_urlsafe(32)
    wygasa = (datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    with db(path) as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, login, utworzono, wygasa, ip, ua) VALUES (?,?,?,?,?,?)",
            (_token_hash(token), login.strip().lower(), now_iso(), wygasa, ip[:64], ua[:200]),
        )
        conn.execute(
            "UPDATE users SET ostatnie_logowanie=? WHERE login=?", (now_iso(), login.strip().lower())
        )
    return token


def session_login(path: str | Path, token: str) -> str | None:
    if not token:
        return None
    with db(path) as conn:
        row = conn.execute(
            """SELECT s.login, s.wygasa, u.aktywny FROM sessions s
               JOIN users u ON u.login = s.login WHERE s.token_hash=?""",
            (_token_hash(token),),
        ).fetchone()
        if row is None or not row["aktywny"]:
            return None
        if row["wygasa"] < now_iso():
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            return None
        return row["login"]


def drop_session(path: str | Path, token: str) -> None:
    if not token:
        return
    with db(path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


# --- limit prob ------------------------------------------------------------


def _too_many(conn: sqlite3.Connection, login: str, ip: str) -> bool:
    prog = (datetime.now(UTC) - timedelta(minutes=15)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COUNT(*) FROM logowania WHERE ok=0 AND at>? AND (login=? OR ip=?)",
        (prog, login, ip),
    ).fetchone()
    return int(row[0]) >= 5


def _record(conn: sqlite3.Connection, login: str, ip: str, ok: bool) -> None:
    conn.execute(
        "INSERT INTO logowania(at, login, ip, ok) VALUES (?,?,?,?)",
        (now_iso(), login[:120], ip[:64], 1 if ok else 0),
    )
    conn.execute(
        "DELETE FROM logowania WHERE at < ?",
        ((datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds"),),
    )


def audit(
    path: str | Path, login: str, akcja: str, sciezka: str = "", kod: int | None = None, ip: str = ""
) -> None:
    try:
        with db(path) as conn:
            conn.execute(
                "INSERT INTO audit_log(at, login, akcja, sciezka, kod, ip) VALUES (?,?,?,?,?,?)",
                (now_iso(), login, akcja, sciezka[:300], kod, ip[:64]),
            )
    except Exception:  # noqa: BLE001 - audyt nie moze wywrocic zadania
        pass


# --- tozsamosc -------------------------------------------------------------


def client_ip(request: Request) -> str:
    # Caddy dokłada X-Forwarded-For; pierwszy adres to klient.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _public_host(request: Request) -> bool:
    settings = request.app.state.settings
    host = (request.headers.get("host") or "").split(":")[0].lower()
    public = (getattr(settings, "public_host", "") or "").lower()
    return bool(public) and host == public


def identity(request: Request) -> str | None:
    settings = request.app.state.settings
    token = request.cookies.get(COOKIE, "")
    if token:
        login = session_login(settings.db_path, token)
        if login:
            return login
    # Tailnet: naglowek ufamy tylko z loopbacku i tylko poza publicznym adresem.
    if not _public_host(request):
        claimed = (request.headers.get(OWNER_HEADER) or "").strip().lower()
        client = request.client.host if request.client else ""
        if claimed and client in ("127.0.0.1", "::1", "localhost"):
            if not settings.owner_login or claimed == settings.owner_login:
                return claimed
    if settings.allow_local:
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost") and not _public_host(request):
            return "local"
    return None


def current_user() -> str:
    """Login biezacego zadania dla szablonow (bez zaleznosci w 28 widokach)."""
    return _current_user.get()


def set_current_user(login: str) -> None:
    _current_user.set(login)


def require_owner(request: Request) -> str:
    login = identity(request)
    if login is None:
        raise HTTPException(status_code=401, detail="wymagane logowanie")
    return login


def check_origin(request: Request) -> None:
    """POST z obcej strony musi byc odrzucony, mimo poprawnego ciasteczka."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    host = request.headers.get("host", "")
    parsed = urlparse(source)
    if parsed.netloc and host and parsed.netloc != host:
        raise HTTPException(status_code=403, detail="obce origin")


def _secure_cookie(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if (request.headers.get("x-forwarded-proto") or "").lower() == "https":
        return True
    return _public_host(request)


# --- trasy logowania -------------------------------------------------------


def _redirect_target(raw: str) -> str:
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> HTMLResponse:
    if identity(request):
        return RedirectResponse(_redirect_target(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _redirect_target(next), "blad": "", "login": ""},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    login: str = Form(""),
    haslo: str = Form(""),
    next: str = Form("/"),
):
    settings = request.app.state.settings
    ip = client_ip(request)
    login_norm = login.strip().lower()
    with db(settings.db_path) as conn:
        if _too_many(conn, login_norm, ip):
            audit(settings.db_path, login_norm, "login-zablokowany", "/login", 429, ip)
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "next": _redirect_target(next),
                    "blad": "Za duzo prob. Odczekaj 15 minut.",
                    "login": login,
                },
                status_code=429,
            )
        row = conn.execute(
            "SELECT login, hash, aktywny FROM users WHERE login=?", (login_norm,)
        ).fetchone()
        ok = bool(row) and bool(row["aktywny"]) and verify_password(haslo, row["hash"])
        _record(conn, login_norm, ip, ok)
    if not ok:
        audit(settings.db_path, login_norm, "login-nieudany", "/login", 401, ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _redirect_target(next), "blad": "Zly e-mail albo haslo.", "login": login},
            status_code=401,
        )
    token = create_session(settings.db_path, login_norm, ip=ip, ua=request.headers.get("user-agent", ""))
    audit(settings.db_path, login_norm, "login-ok", "/login", 303, ip)
    response = RedirectResponse(_redirect_target(next), status_code=303)
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path="/",
    )
    return response


@router.get("/logout")
@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    token = request.cookies.get(COOKIE, "")
    login = session_login(settings.db_path, token) or ""
    drop_session(settings.db_path, token)
    audit(settings.db_path, login, "logout", "/logout", 303, client_ip(request))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/invite", response_class=HTMLResponse)
def invite_page(request: Request, token: str = "") -> HTMLResponse:
    settings = request.app.state.settings
    login = invite_login(settings.db_path, token)
    if login is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": "/", "blad": "Zaproszenie wygasło albo zostało już użyte.", "login": ""},
            status_code=410,
        )
    return templates.TemplateResponse(request, "invite.html", {"token": token, "login": login, "blad": ""})


@router.post("/invite", response_class=HTMLResponse)
def invite_submit(
    request: Request,
    token: str = Form(""),
    haslo: str = Form(""),
    haslo2: str = Form(""),
):
    settings = request.app.state.settings
    login = invite_login(settings.db_path, token)
    if login is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": "/", "blad": "Zaproszenie wygasło albo zostało już użyte.", "login": ""},
            status_code=410,
        )
    if haslo != haslo2:
        return templates.TemplateResponse(
            request,
            "invite.html",
            {"token": token, "login": login, "blad": "Hasła nie są takie same."},
            status_code=400,
        )
    if len(haslo) < 10:
        return templates.TemplateResponse(
            request,
            "invite.html",
            {"token": token, "login": login, "blad": "Hasło musi mieć co najmniej 10 znaków."},
            status_code=400,
        )
    login = use_invite(settings.db_path, token, haslo)
    token_sesji = create_session(
        settings.db_path, login, ip=client_ip(request), ua=request.headers.get("user-agent", "")
    )
    audit(settings.db_path, login, "konto-ustawione", "/invite", 303, client_ip(request))
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE,
        token_sesji,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path="/",
    )
    return response


def is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PATHS)


# --- zaproszenia -----------------------------------------------------------

INVITE_HOURS = 24


def create_invite(path: str | Path, login: str, godziny: int = INVITE_HOURS) -> str:
    """Jednorazowy link do ustawienia hasla. Wygasa po dobie."""
    login = login.strip().lower()
    token = secrets.token_urlsafe(24)
    wygasa = (datetime.now(UTC) + timedelta(hours=godziny)).isoformat(timespec="seconds")
    with db(path) as conn:
        if not conn.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            raise SystemExit(f"brak konta {login}")
        conn.execute("INSERT INTO invites(token, login, wygasa) VALUES (?,?,?)", (token, login, wygasa))
    return token


def invite_login(path: str | Path, token: str) -> str | None:
    """Login, do ktorego nalezy wazne zaproszenie, albo None."""
    if not token:
        return None
    with db(path) as conn:
        row = conn.execute("SELECT login, wygasa, uzyty FROM invites WHERE token=?", (token,)).fetchone()
        if row is None or row["uzyty"] or row["wygasa"] < now_iso():
            return None
        return row["login"]


def use_invite(path: str | Path, token: str, haslo: str) -> str:
    login = invite_login(path, token)
    if login is None:
        raise ValueError("zaproszenie wygaslo albo zostalo uzyte")
    if len(haslo) < 10:
        raise ValueError("haslo musi miec co najmniej 10 znakow")
    with db(path) as conn:
        conn.execute("UPDATE users SET hash=?, aktywny=1 WHERE login=?", (hash_password(haslo), login))
        conn.execute("UPDATE invites SET uzyty=? WHERE token=?", (now_iso(), token))
        conn.execute("DELETE FROM sessions WHERE login=?", (login,))
    return login


def cleanup_sessions(path: str | Path) -> int:
    with db(path) as conn:
        a = conn.execute("DELETE FROM sessions WHERE wygasa < ?", (now_iso(),)).rowcount
        conn.execute(
            "DELETE FROM audit_log WHERE at < ?",
            ((datetime.now(UTC) - timedelta(days=180)).isoformat(timespec="seconds"),),
        )
    return a
