"""Testy logowania panelu: hasla, sesje, zaproszenia, limit prob."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from freelance_pipeline.web import auth


@pytest.fixture()
def baza(tmp_path):
    sciezka = tmp_path / "panel.db"
    auth.init_schema(sciezka)
    return sciezka


def test_haslo_roundtrip(baza):
    zapis = auth.hash_password("bardzo-tajne-haslo")
    assert zapis.startswith("pbkdf2_sha256$600000$")
    assert auth.verify_password("bardzo-tajne-haslo", zapis)
    assert not auth.verify_password("inne-haslo", zapis)


def test_hash_jest_losowy_dla_tego_samego_hasla():
    a = auth.hash_password("to-samo-haslo")
    b = auth.hash_password("to-samo-haslo")
    assert a != b
    assert auth.verify_password("to-samo-haslo", a) and auth.verify_password("to-samo-haslo", b)


def test_uszkodzony_hash_nie_wali_wyjatkiem():
    assert not auth.verify_password("cokolwiek", "zepsuty-zapis")


def test_konto_i_sesja(baza):
    auth.create_user(baza, "jan@example.com", "haslo-jana-123", imie="Jan")
    token = auth.create_session(baza, "jan@example.com", ip="1.2.3.4")
    assert auth.session_login(baza, token) == "jan@example.com"
    auth.drop_session(baza, token)
    assert auth.session_login(baza, token) is None


def test_sesja_wygasla_nie_wpuszcza(baza):
    auth.create_user(baza, "ada@example.com", "haslo-ady-1234", imie="Ada")
    token = auth.create_session(baza, "ada@example.com")
    with auth.db(baza) as conn:
        conn.execute(
            "UPDATE sessions SET wygasa=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds"),),
        )
    assert auth.session_login(baza, token) is None


def test_wylaczone_konto_traci_sesje(baza):
    auth.create_user(baza, "ada@example.com", "haslo-ady-1234", imie="Ada")
    token = auth.create_session(baza, "ada@example.com")
    with auth.db(baza) as conn:
        conn.execute("UPDATE users SET aktywny=0 WHERE login='ada@example.com'")
    assert auth.session_login(baza, token) is None


def test_zmiana_hasla_zamyka_sesje(baza):
    auth.create_user(baza, "jan@example.com", "stare-haslo-123", imie="Jan")
    token = auth.create_session(baza, "jan@example.com")
    auth.set_password(baza, "jan@example.com", "nowe-haslo-4567")
    assert auth.session_login(baza, token) is None


def test_zaproszenie_jednorazowe(baza):
    auth.create_user(baza, "ada@example.com", "tymczasowe-haslo", imie="Ada")
    token = auth.create_invite(baza, "ada@example.com")
    assert auth.invite_login(baza, token) == "ada@example.com"
    with pytest.raises(ValueError):
        auth.use_invite(baza, token, "krotkie")
    assert auth.use_invite(baza, token, "haslo-ady-1234") == "ada@example.com"
    assert auth.invite_login(baza, token) is None
    with pytest.raises(ValueError):
        auth.use_invite(baza, token, "haslo-ady-1234")


def test_zaproszenie_wygasle(baza):
    auth.create_user(baza, "ada@example.com", "tymczasowe-haslo", imie="Ada")
    token = auth.create_invite(baza, "ada@example.com", godziny=-1)
    assert auth.invite_login(baza, token) is None


def test_limit_prob_po_pieciu(baza):
    auth.create_user(baza, "jan@example.com", "haslo-jana-123", imie="Jan")
    with auth.db(baza) as conn:
        for _ in range(5):
            auth._record(conn, "jan@example.com", "9.9.9.9", False)
        assert auth._too_many(conn, "jan@example.com", "9.9.9.9")
        assert not auth._too_many(conn, "inny@example.com", "8.8.8.8")


def test_audyt_zapisuje_wpis(baza):
    auth.audit(baza, "jan@example.com", "login-ok", "/login", 303, "1.2.3.4")
    with auth.db(baza) as conn:
        row = conn.execute("SELECT login, akcja, kod FROM audit_log").fetchone()
    assert tuple(row) == ("jan@example.com", "login-ok", 303)


def test_sciezki_publiczne():
    assert auth.is_public("/login")
    assert auth.is_public("/static/app.css")
    assert auth.is_public("/api/health")
    assert not auth.is_public("/")
    assert not auth.is_public("/lead/useme:1")
