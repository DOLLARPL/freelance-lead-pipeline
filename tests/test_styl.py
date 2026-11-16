"""Testy wysylki par stylu do humanizera. Bez sieci i bez modelu."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freelance_pipeline import styl
from freelance_pipeline.ledger import Ledger, OfferSeed
from freelance_pipeline.web import store


class FakePost:
    def __init__(self, status_code: int = 200, text: str = '{"ok": true}') -> None:
        self.status_code = status_code
        self.text = text
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, json: dict | None = None, timeout: float | None = None):
        self.calls.append((url, {"json": json, "timeout": timeout}))
        fake = self

        class _Resp:
            status_code = fake.status_code
            text = fake.text

        return _Resp()


@pytest.fixture
def fake_post(monkeypatch):
    fake = FakePost()
    monkeypatch.setattr(styl.requests, "post", fake)
    return fake


@pytest.fixture
def conn(tmp_path: Path):
    connection = store.connect(tmp_path / "snajper.db")
    yield connection
    connection.close()


def _setup(tmp_path: Path, conn, draft: str | None = "Czesc, zrobie to w tydzien.") -> None:
    """Ledger + plik leads, potem import do SQLite (draft dostaje origin 'ledger')."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(cache / "ledger.json")
    ledger.add(
        [
            OfferSeed(
                source="useme",
                source_id="1",
                url="https://example.com/1",
                title="Sklep na WooCommerce",
                bucket="TOP",
                score=90.0,
            )
        ]
    )
    if draft:
        ledger.set_draft("useme:1", draft)
    (cache / "leads-20260101T000000Z.json").write_text(
        json.dumps(
            [
                {
                    "key": "useme:1",
                    "source": "useme",
                    "source_id": "1",
                    "title": "Sklep na WooCommerce",
                    "description": "Potrzebny sklep z platnosciami.",
                    "bucket": "TOP",
                    "score": 95,
                }
            ]
        ),
        encoding="utf-8",
    )
    store.import_sources(
        conn, cache_dir=cache, reports_dir=cache / "reports", ledger_path=cache / "ledger.json"
    )


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "cache" / "ledger.json")


def test_zapisz_pare_wysyla_payload(fake_post, monkeypatch) -> None:
    monkeypatch.delenv("SNAJPER_HUMANIZER_URL", raising=False)
    assert styl.zapisz_pare("Draft agenta", "Moja wersja") is True
    url, kwargs = fake_post.calls[0]
    assert url == "http://127.0.0.1:8011/zapisz"
    assert kwargs["json"] == {
        "rejestr": "rejestr-klient",
        "draft": "Draft agenta",
        "moja": "Moja wersja",
        "uwagi": "",
    }
    assert kwargs["timeout"] == styl.TIMEOUT_S


def test_pusta_albo_identyczna_para_nie_leci(fake_post) -> None:
    assert styl.zapisz_pare("", "Moja") is False
    assert styl.zapisz_pare("Draft", "   ") is False
    assert styl.zapisz_pare("To samo", "To samo") is False
    assert fake_post.calls == []


def test_awaria_humanizera_nie_rzuca(fake_post, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise styl.requests.ConnectionError("connection refused")

    monkeypatch.setattr(styl.requests, "post", boom)
    assert styl.zapisz_pare("Draft", "Moja") is False


def test_odrzucenie_400_to_falsz(fake_post) -> None:
    fake_post.status_code = 400
    fake_post.text = '{"blad": "draft i moja musza byc niepuste"}'
    assert styl.zapisz_pare("Draft", "Moja") is False


def test_save_draft_bez_draftu_agenta_nie_wysyla(tmp_path: Path, conn, fake_post) -> None:
    _setup(tmp_path, conn, draft=None)
    ledger = _ledger(tmp_path)
    store.save_draft(conn, ledger, "useme:1", "Moja wersja bez draftu systemu.")
    assert fake_post.calls == []


def test_save_draft_model_nie_wysyla(tmp_path: Path, conn, fake_post) -> None:
    _setup(tmp_path, conn)
    ledger = _ledger(tmp_path)
    store.save_draft(conn, ledger, "useme:1", "Nowy draft modelu.", origin="model")
    assert fake_post.calls == []


