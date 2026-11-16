"""Testy read modelu panelu: import, idempotencja, filtry, zapis statusu i draftow.

Nie dotykaja sieci ani modelu. Kazdy test buduje wlasny katalog z minimalnym
zestawem plikow (ledger.json, leads-*.json, reports/*.keys|prices.json).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freelance_pipeline.ledger import Ledger, OfferSeed
from freelance_pipeline.web import store


def _ledger(tmp_path: Path, keys: list[tuple[str, str]], *, draft_for: str | None = None) -> Path:
    path = tmp_path / "cache" / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(path)
    seeds = [
        OfferSeed(
            source=source,
            source_id=source_id,
            url=f"https://example.com/{source_id}",
            title=f"Oferta {source_id}",
            bucket="TOP",
            score=90.0,
        )
        for source, source_id in keys
    ]
    ledger.add(seeds)
    if draft_for:
        ledger.set_draft(draft_for, "Czesc, robie takie rzeczy.")
    return path


def _leads_file(tmp_path: Path, entries: list[dict]) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "leads-20260101T000000Z.json").write_text(json.dumps(entries), encoding="utf-8")


def _report(tmp_path: Path, stamp: str, mapping: dict[str, str], prices: dict[str, dict]) -> None:
    reports = tmp_path / "cache" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"instant-{stamp}.keys.json").write_text(json.dumps(mapping), encoding="utf-8")
    (reports / f"instant-{stamp}.prices.json").write_text(json.dumps(prices), encoding="utf-8")


def _import(tmp_path: Path, conn):
    cache = tmp_path / "cache"
    return store.import_sources(
        conn, cache_dir=cache, reports_dir=cache / "reports", ledger_path=cache / "ledger.json"
    )


@pytest.fixture()
def conn(tmp_path: Path):
    connection = store.connect(tmp_path / "snajper.db")
    yield connection
    connection.close()


def test_import_tworzy_leady_drafty_i_wyceny(tmp_path: Path, conn) -> None:
    ledger_path = _ledger(tmp_path, [("useme", "1"), ("useme", "2")], draft_for="useme:1")
    _leads_file(
        tmp_path,
        [
            {
                "key": "useme:1",
                "source": "useme",
                "source_id": "1",
                "title": "Sklep na WooCommerce",
                "description": "Potrzebny sklep z platnosciami.",
                "author": "Anna",
                "bucket": "TOP",
                "score": 95,
                "rationale": "Dobre dopasowanie.",
            }
        ],
    )
    _report(
        tmp_path,
        "20260101T060000Z",
        {"1": "useme:1"},
        {"1": {"amount": 6500.0, "currency": "PLN", "days": 14, "note": "cennik, sklep standard"}},
    )
    assert ledger_path.exists()

    report = _import(tmp_path, conn)

    assert report["leads_new"] == 2
    assert report["drafts_new"] == 1
    assert report["quotes_new"] == 1
    lead = store.get_lead(conn, "useme:1")
    assert lead is not None
    assert lead.description.startswith("Potrzebny sklep")
    assert lead.draft.startswith("Czesc")
    assert lead.amount == 6500.0
    assert lead.last_number == 1


def test_drugi_import_jest_idempotentny(tmp_path: Path, conn) -> None:
    _ledger(tmp_path, [("useme", "1")], draft_for="useme:1")
    _leads_file(tmp_path, [{"key": "useme:1", "source": "useme", "source_id": "1", "description": "Opis"}])
    _import(tmp_path, conn)

    second = _import(tmp_path, conn)

    assert second["leads_new"] == 0
    assert second["leads_updated"] == 0
    assert second["drafts_new"] == 0
    assert second["quotes_new"] == 0
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_status_synchronizuje_sie_z_ledgerem(tmp_path: Path, conn) -> None:
    ledger_path = _ledger(tmp_path, [("useme", "1")])
    _import(tmp_path, conn)
    ledger = Ledger(ledger_path)

    lead = store.save_status(conn, ledger, "useme:1", "replied", note="z panelu")

    assert lead is not None and lead.status == "replied"
    assert ledger.get("useme:1").status == "replied"
    events = [e for e in store.get_lead(conn, "useme:1").events if e["actor"] == "web"]
    assert events and events[0]["to_status"] == "replied"
    assert events[0]["note"] == "z panelu"


def test_status_spoza_panelu_jest_odrzucany(tmp_path: Path, conn) -> None:
    ledger_path = _ledger(tmp_path, [("useme", "1")])
    _import(tmp_path, conn)
    with pytest.raises(ValueError):
        store.save_status(conn, Ledger(ledger_path), "useme:1", "new")


def test_draft_dostaje_nowa_wersje_i_zniknie_stara_flaga(tmp_path: Path, conn) -> None:
    ledger_path = _ledger(tmp_path, [("useme", "1")], draft_for="useme:1")
    _import(tmp_path, conn)
    ledger = Ledger(ledger_path)

    lead = store.save_draft(conn, ledger, "useme:1", "Wersja druga", instruction="krocej")

    assert lead is not None
    assert lead.draft == "Wersja druga"
    assert [d["version"] for d in lead.drafts] == [2, 1]
    current = conn.execute(
        "SELECT COUNT(*) FROM draft_versions WHERE lead_key = 'useme:1' AND is_current = 1"
    ).fetchone()[0]
    assert current == 1
    assert ledger.get("useme:1").draft == "Wersja druga"


def test_lista_filtruje_kolejke_i_ladnie_sortuje(tmp_path: Path, conn) -> None:
    _ledger(tmp_path, [("useme", "1"), ("useme", "2"), ("oferteo_mail", "3")])
    _import(tmp_path, conn)
    ledger = Ledger(tmp_path / "cache" / "ledger.json")
    store.save_status(conn, ledger, "useme:2", "replied")

    queue = store.list_leads(conn, view="queue")
    replied = store.list_leads(conn, view="replied")

    assert {row.key for row in queue} == {"useme:1", "oferteo_mail:3"}
    assert [row.key for row in replied] == ["useme:2"]
    assert store.counts(conn)["queue"] == 2
    assert store.counts(conn)["replied"] == 1


def test_lista_i_liczniki_respektuja_prog_wieku(conn) -> None:
    for key, hours in (("useme:1", 2), ("useme:2", 20)):
        conn.execute(
            "INSERT INTO leads(key, source, source_id, title, status, first_seen_at, "
            "imported_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (key, "useme", key.split(":")[1], "Oferta", "new",
             (datetime.now(UTC) - timedelta(hours=hours)).isoformat(sep=" "),
             store.now_iso(), store.now_iso()),
        )

    assert [r.key for r in store.list_leads(conn, view="queue", max_age_hours=12)] == ["useme:1"]
    assert store.counts(conn, max_age_hours=12)["queue"] == 1
    assert store.counts(conn)["queue"] == 2
    assert [r.key for r in store.list_leads(conn, view="queue", max_age_hours=0)] == [
        "useme:1",
        "useme:2",
    ]


def test_szukanie_i_filtr_zrodla(tmp_path: Path, conn) -> None:
    _ledger(tmp_path, [("useme", "1"), ("oferteo_mail", "2")])
    _leads_file(
        tmp_path,
        [
            {"key": "useme:1", "source": "useme", "source_id": "1", "title": "Sklep", "description": "WooCommerce"},
            {"key": "oferteo_mail:2", "source": "oferteo_mail", "source_id": "2", "title": "Wizytowka", "description": "Landing"},
        ],
    )
    _import(tmp_path, conn)

    assert [row.key for row in store.list_leads(conn, view="all", q="woocommerce")] == ["useme:1"]
    assert [row.key for row in store.list_leads(conn, view="all", source="oferteo_mail")] == ["oferteo_mail:2"]


def test_import_nie_wymysla_tresci_gdy_brak_pliku_leads(tmp_path: Path, conn) -> None:
    _ledger(tmp_path, [("useme", "9")])
    _import(tmp_path, conn)

    lead = store.get_lead(conn, "useme:9")

    assert lead is not None
    assert lead.description == ""
    assert lead.title == "Oferta 9"


def test_filtr_wielu_platform_jednym_zapytaniem(tmp_path: Path, conn) -> None:
    _ledger(tmp_path, [("useme", "1"), ("oferteo_mail", "2"), ("facebook_groups", "3")])
    _leads_file(
        tmp_path,
        [
            {"key": "useme:1", "source": "useme", "source_id": "1"},
            {"key": "oferteo_mail:2", "source": "oferteo_mail", "source_id": "2"},
            {"key": "facebook_groups:3", "source": "facebook_groups", "source_id": "3"},
        ],
    )
    _import(tmp_path, conn)

    wybrane = {row.key for row in store.list_leads(conn, view="all", sources=["useme", "facebook_groups"])}

    assert wybrane == {"useme:1", "facebook_groups:3"}
    # Pusta lista znaczy "wszystkie", tak samo jak brak parametru: czyszczenie filtra
    # nie moze udawac pustej kolejki.
    assert len(store.list_leads(conn, view="all", sources=[])) == 3


def test_preferencje_filtra_per_login(tmp_path: Path, conn) -> None:
    store.set_user_pref(conn, "jan@example.com", {"platforms": ["useme"]})
    store.set_user_pref(conn, "ada@example.com", {"platforms": []})

    assert store.get_user_pref(conn, "jan@example.com") == {"platforms": ["useme"]}
    assert store.get_user_pref(conn, "ada@example.com") == {"platforms": []}
    # Konto bez zapisu nie dostaje cudzego filtra.
    assert store.get_user_pref(conn, "ktos@example.com") == {}


def test_normalize_filters_odrzuca_nieznane_zrodla(tmp_path: Path, conn) -> None:
    _ledger(tmp_path, [("useme", "1")])
    _import(tmp_path, conn)

    wynik = store.normalize_filters(conn, {"platforms": ["useme", "olx", ""]})

    assert wynik == {"platforms": ["useme"]}
    # Smieci zamiast listy nie moga wysadzic zapisu.
    assert store.normalize_filters(conn, {"platforms": "useme"}) == {"platforms": []}
    assert store.normalize_filters(conn, None) == {"platforms": []}
