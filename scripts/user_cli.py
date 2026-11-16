#!/usr/bin/env python3
"""Konta panelu snajpera: zakladanie, zaproszenia, hasla, wylaczanie.

Haslo nigdy nie idzie argumentem (zostaje w historii powloki), tylko przez
``getpass`` albo stdin. Nowe konto nie ma hasla: dostaje jednorazowy link
``/invite?token=...`` wazny 24 h, w ktorym osoba sama ustawia haslo.

Przyklady (na VPS, w katalogu repo):

    .venv/bin/python scripts/user_cli.py list
    .venv/bin/python scripts/user_cli.py add --login jan@jnxautomation.pl --imie Jan
    .venv/bin/python scripts/user_cli.py add --login ada@example.com --imie Ada
    .venv/bin/python scripts/user_cli.py invite --login ada@example.com
    .venv/bin/python scripts/user_cli.py passwd --login jan@jnxautomation.pl
    .venv/bin/python scripts/user_cli.py off --login ada@example.com
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from freelance_pipeline.web import auth  # noqa: E402

DB = Path(os.environ.get("SNAJPER_WEB_DB", "/opt/data/cache/snajper-web/snajper.db"))
BAZA = os.environ.get("SNAJPER_PUBLIC_URL", "https://snajper.jnxautomation.pl").rstrip("/")


def _haslo(z_potwierdzeniem: bool = True) -> str:
    if sys.stdin.isatty():
        haslo = getpass.getpass("Hasło: ")
        if z_potwierdzeniem and getpass.getpass("Powtórz: ") != haslo:
            raise SystemExit("hasła nie są takie same")
        return haslo
    haslo = sys.stdin.readline().rstrip("\n")
    if len(haslo) < 10:
        raise SystemExit("hasło musi mieć co najmniej 10 znaków")
    return haslo


def main() -> int:
    parser = argparse.ArgumentParser(description="Konta panelu snajpera")
    parser.add_argument("cmd", choices=["list", "add", "invite", "passwd", "off", "on", "del"])
    parser.add_argument("--login", default="")
    parser.add_argument("--imie", default="")
    parser.add_argument("--rola", default="owner")
    args = parser.parse_args()

    auth.init_schema(DB)

    if args.cmd == "list":
        konta = auth.list_users(DB)
        if not konta:
            print("brak kont")
            return 0
        for k in konta:
            stan = "aktywne" if k["aktywny"] else "wyłączone"
            print(f"{k['login']:38} {k['imie']:14} {k['rola']:8} {stan:10} ostatnie: {k['ostatnie_logowanie'] or '-'}")
        return 0

    login = args.login.strip().lower()
    if not login:
        raise SystemExit("podaj --login")

    if args.cmd == "add":
        # konto powstaje z haslem nie do uzycia, wlasciwe ustawia osoba z linku
        tymczasowe = secrets.token_urlsafe(32)
        auth.create_user(DB, login, tymczasowe, imie=args.imie, rola=args.rola)
        token = auth.create_invite(DB, login)
        print(f"konto: {login}")
        print(f"link (ważny 24 h, jednorazowy): {BAZA}/invite?token={token}")
        return 0

    if args.cmd == "invite":
        token = auth.create_invite(DB, login)
        print(f"{BAZA}/invite?token={token}")
        return 0

    if args.cmd == "passwd":
        auth.set_password(DB, login, _haslo())
        print(f"hasło zmienione: {login} (sesje tego konta zamknięte)")
        return 0

    if args.cmd in ("off", "on"):
        with auth.db(DB) as conn:
            cur = conn.execute(
                "UPDATE users SET aktywny=? WHERE login=?", (0 if args.cmd == "off" else 1, login)
            )
            if cur.rowcount == 0:
                raise SystemExit(f"brak konta {login}")
            if args.cmd == "off":
                conn.execute("DELETE FROM sessions WHERE login=?", (login,))
        print(f"{login}: {'wyłączone' if args.cmd == 'off' else 'włączone'}")
        return 0

    if args.cmd == "del":
        with auth.db(DB) as conn:
            cur = conn.execute("DELETE FROM users WHERE login=?", (login,))
            if cur.rowcount == 0:
                raise SystemExit(f"brak konta {login}")
            conn.execute("DELETE FROM sessions WHERE login=?", (login,))
            conn.execute("DELETE FROM invites WHERE login=?", (login,))
            conn.execute("DELETE FROM logowania WHERE login=?", (login,))
        print(f"konto usunięte: {login}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
