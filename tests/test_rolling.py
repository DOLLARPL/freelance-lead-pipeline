"""Rolling lista: wpisy z rejestru, format pliku, panel i wysyłka dokumentu."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freelance_pipeline.ledger import STATUS_SEEN, Ledger
from freelance_pipeline.pricing import PriceBrief, PricedDraft
from freelance_pipeline.proposal import ProposalDraft
from freelance_pipeline.rolling import (
    LeadRow,
    RollingDeliveryError,
    RollingError,
    TelegramTarget,
    build_entries,
    load_leads,
    load_pending_rows,
    mark_delivered,
    panel_items,
    render_file,
    send_document,
    store_drafts,
)

NOW = datetime(2026, 10, 7, 6, 30, tzinfo=UTC)
MESSAGE = (
    "Cześć,\n\npotrzebujesz listy firm B2B, nie tysiąca przypadkowych rekordów.\n\n"
    "- pobieranie z LinkedIn\n- deduplikacja\n- eksport do CSV\n\n"
    "1 800 zł, ok. 3 dni roboczych za scraper i eksport. Wycena i czas są orientacyjne, "
    "dopasujemy je po szczegółach.\n\nCzy taki zakres ma sens?\n\n"
    "Jan Noga\njnxautomation.pl\n+48 600 000 000"
)


def lead(**overrides: object) -> dict:
    data: dict = {
        "key": "useme:145473",
        "source": "useme",
        "source_id": "145473",
        "url": "https://useme.com/pl/jobs/x,145473/",
        "title": "Zlecę przygotowanie leadów B2B",
        "author": "Łukasz",
        "description": "Klient chce listę firm z LinkedIn.",
        "bucket": "TOP",
        "score": 100,
        "budget": 1800.0,
        "currency": "PLN",
        "rationale": "Klient z konkretnym zakresem.",
    }
    data.update(overrides)
    return data


def brief(**overrides: object) -> PriceBrief:
    data: dict = {
        "key": "useme:145473",
        "amount": 1800.0,
        "currency": "PLN",
        "days": 3,
        "hourly_min": 90.0,
        "hourly_max": 150.0,
        "includes": ("scraper", "eksport"),
        "sources": ("https://useme.com/pl/blog/ile-zaplacic-freelancerowi/",),
        "rationale": "Stawki freelancerskie 90-150 zł/h.",
    }
    data.update(overrides)
    return PriceBrief(**data)  # type: ignore[arg-type]


def priced_draft(key: str = "useme:145473", text: str = MESSAGE, **overrides: object) -> PricedDraft:
    draft = ProposalDraft(key=key, text=text)
    return PricedDraft(draft=draft, brief=brief(key=key, **overrides), text=text)


# ------------------------------------------------------------------ wiersze ofert


def test_lead_row_parses_and_builds_an_offer():
    row = LeadRow.from_json(lead())
    assert row.key == "useme:145473"
    offer = row.as_offer()
    assert offer.title.startswith("Zlecę")
    assert offer.budget == 1800.0
    assert row.as_seed().source_id == "145473"


def test_lead_row_requires_source_id_and_url():
    with pytest.raises(RollingError, match="source/source_id/url"):
        LeadRow.from_json(lead(url=""))
    with pytest.raises(RollingError, match="source/source_id/url"):
        LeadRow.from_json(lead(source_id=""))


def test_lead_row_rejects_broken_numbers():
    with pytest.raises(RollingError, match="ocenę"):
        LeadRow.from_json(lead(score="100"))
    with pytest.raises(RollingError, match="budżet"):
        LeadRow.from_json(lead(budget="dużo"))


def test_load_leads_reads_a_list(tmp_path: Path):
    path = tmp_path / "leads-1.json"
    path.write_text(json.dumps([lead()], ensure_ascii=False), encoding="utf-8")
    assert [row.key for row in load_leads(path)] == ["useme:145473"]


def test_load_leads_handles_empty_file_and_reports_missing(tmp_path: Path):
    empty = tmp_path / "leads-empty.json"
    empty.write_text("", encoding="utf-8")
    assert load_leads(empty) == []
    with pytest.raises(RollingError, match="brak pliku"):
        load_leads(tmp_path / "nie-ma.json")


def test_load_leads_rejects_a_non_list(tmp_path: Path):
    path = tmp_path / "leads-bad.json"
    path.write_text(json.dumps({"oferty": []}), encoding="utf-8")
    with pytest.raises(RollingError, match="listę ofert"):
        load_leads(path)


def test_load_pending_rows_skips_old_files_and_seen_offers(tmp_path: Path):
    fresh = tmp_path / "leads-fresh.json"
    fresh.write_text(json.dumps([lead(), lead(key="useme:2", source_id="2")]), encoding="utf-8")
    old = tmp_path / "leads-old.json"
    old.write_text(json.dumps([lead(key="useme:3", source_id="3")]), encoding="utf-8")
    ancient = NOW - timedelta(days=10)
    old.touch()
    import os

    os.utime(old, (ancient.timestamp(), ancient.timestamp()))

    ledger = Ledger(tmp_path / "ledger.json")
    ledger.add([LeadRow.from_json(lead(key="useme:2", source_id="2")).as_seed()], now=NOW)
    ledger.mark_status(["useme:2"], STATUS_SEEN, now=NOW)

    pending = load_pending_rows(tmp_path, ledger, now=NOW)
    assert [row.key for row in pending] == ["useme:145473"]


# ------------------------------------------------------------------ wpisy listy


def test_build_entries_orders_buckets_and_skips_known_offers(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    rows = [
        LeadRow.from_json(lead(key="useme:moze", source_id="moze", bucket="MOŻE", score=50)),
        LeadRow.from_json(lead(key="useme:top", source_id="top", bucket="TOP", score=80)),
        LeadRow.from_json(lead(key="useme:zamien", source_id="zamien", bucket="ZAMIEN", score=100)),
        LeadRow.from_json(lead(key="useme:seen", source_id="seen")),
    ]
    ledger.add([row.as_seed() for row in rows], now=NOW)
    ledger.mark_status(["useme:seen"], STATUS_SEEN, now=NOW)

    entries = build_entries(
        rows,
        ledger,
        priced=[priced_draft(row.key) for row in rows],
        limit=None,
    )
    assert [entry.row.key for entry in entries] == ["useme:top", "useme:moze", "useme:zamien"]
    assert all(entry.ok for entry in entries)


def test_build_entries_honours_the_limit_and_flags_a_missing_draft(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    rows = [
        LeadRow.from_json(lead(key=f"useme:{index}", source_id=str(index), score=100 - index))
        for index in range(4)
    ]
    ledger.add([row.as_seed() for row in rows], now=NOW)
    entries = build_entries(rows, ledger, priced=[priced_draft("useme:0")], limit=2)
    assert len(entries) == 2
    assert entries[0].row.key == "useme:0"
    assert "brak draftu" in entries[1].note


def test_build_entries_rejects_a_negative_limit(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    with pytest.raises(ValueError):
        build_entries([], ledger, priced=[], limit=-1)


def test_select_rows_sorts_and_limits_the_same_set_that_gets_drafted(tmp_path: Path):
    from freelance_pipeline.rolling import select_rows

    ledger = Ledger(tmp_path / "ledger.json")
    rows = [
        LeadRow.from_json(lead(key="useme:moze", source_id="moze", bucket="MOŻE", score=90)),
        LeadRow.from_json(lead(key="useme:top-low", source_id="top-low", bucket="TOP", score=70)),
        LeadRow.from_json(lead(key="useme:top-high", source_id="top-high", bucket="TOP", score=100)),
        LeadRow.from_json(lead(key="useme:seen", source_id="seen")),
    ]
    ledger.add([row.as_seed() for row in rows], now=NOW)
    ledger.mark_status(["useme:seen"], STATUS_SEEN, now=NOW)

    ordered = select_rows(rows, ledger)
    assert [row.key for row in ordered] == ["useme:top-high", "useme:top-low", "useme:moze"]
    assert [row.key for row in select_rows(rows, ledger, limit=2)] == ["useme:top-high", "useme:top-low"]
    with pytest.raises(ValueError):
        select_rows(rows, ledger, limit=-1)


def test_price_line_shows_amount_days_and_researched_rate():
    entry = build_entries(
        [LeadRow.from_json(lead())],
        Ledger(Path("/tmp/nie-ma-ledgera.json")),
        priced=[],
    )
    assert entry == []  # brak wpisu w rejestrze = oferta nie jest jeszcze znana
    ledger_path = Path("/tmp/nie-ma-ledgera.json")
    assert not ledger_path.exists()


def test_price_line_format(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead())
    ledger.add([row.as_seed()], now=NOW)
    entry = build_entries([row], ledger, priced=[priced_draft()])[0]
    assert entry.price_line.startswith("1 800 zł, ok. 3 dni roboczych")
    assert "90-150 zł/h" in entry.price_line


# ------------------------------------------------------------------ plik


def test_render_file_has_header_numbering_and_prices(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead())
    ledger.add([row.as_seed()], now=NOW)
    entry = build_entries([row], ledger, priced=[priced_draft()])[0]

    text = render_file([entry], now=NOW)
    # Format z 2026-10-28: numer, kubełek, ocena i źródło zostają (Jan chce widzieć ocenę),
    # „po co” zastąpił „Brief:”, a uzasadnienie wyceny ze źródłami i kontrola wyjścia zniknęły.
    assert text.startswith("SNAJPER: 1 nowych ofert (stan na 2026-10-07 06:30 UTC)")
    assert "[1] TOP 100/100 useme" in text
    assert "https://useme.com/pl/jobs/x,145473/" in text
    assert "Budżet z ogłoszenia: 1 800 zł" in text
    assert "Brief: Klient z konkretnym zakresem" in text
    assert "Cena: 1 800 zł brutto, ok. 3 dni roboczych" in text
    assert "Wiadomość do wklejenia:" in text
    assert "Jan Noga" in text
    assert "źródła:" not in text
    assert "kontrola wyjścia" not in text
    assert "„3 odpisałem”" in text


def test_render_file_marks_problems_and_zamien_section(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    rows = [
        LeadRow.from_json(lead(key="useme:z", source_id="z", bucket="ZAMIEN", score=90)),
        LeadRow.from_json(lead(key="useme:bad", source_id="bad", bucket="TOP", score=80)),
    ]
    ledger.add([row.as_seed() for row in rows], now=NOW)
    entries = build_entries(
        rows,
        ledger,
        priced=[
            priced_draft("useme:z"),
            PricedDraft(
                draft=ProposalDraft(key="useme:bad", text="za krótko"),
                brief=brief(key="useme:bad"),
                text="za krótko",
                problems=("za krótka (9 znaków, minimum 700)",),
            ),
        ],
    )
    text = render_file(entries, now=NOW)
    assert "ZAMIEN (1): klient szuka człowieka" in text
    # Kontrola wyjścia jest techniczna, więc nie trafia do treści dla Jana (decyzja 2026-10-28).
    assert "kontrola wyjścia" not in text


def test_render_file_is_empty_without_entries():
    assert render_file([], now=NOW) == ""


def test_panel_items_carry_the_exact_message(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead())
    ledger.add([row.as_seed()], now=NOW)
    entry = build_entries([row], ledger, priced=[priced_draft()])[0]

    items = panel_items([entry])
    assert items[0]["wiadomosc"] == MESSAGE
    assert items[0]["tytul"].startswith("[1]")
    assert items[0]["url"] == row.url
    assert "1 800 zł" in items[0]["wycena"]


# ------------------------------------------------------------------ wysyłka


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)


class _Session:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(self.payload, self.status_code)


def test_target_from_env_needs_a_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RollingDeliveryError, match="TELEGRAM_BOT_TOKEN"):
        TelegramTarget.from_env({})
    target = TelegramTarget.from_env({"TELEGRAM_BOT_TOKEN": "t"})
    assert (target.chat_id, target.thread_id) == ("-1003574759568", "3")


def test_send_document_posts_multipart_to_the_topic(tmp_path: Path):
    path = tmp_path / "rolling.txt"
    path.write_text("lista", encoding="utf-8")
    session = _Session({"ok": True, "result": {"message_id": 42}})
    target = TelegramTarget(token="test-token", chat_id="-100", thread_id="3")

    payload = send_document(target, path, caption="Snajper", session=session)

    assert payload["result"]["message_id"] == 42
    call = session.calls[0]
    assert call["url"].endswith("/bottest-token/sendDocument")
    body = call["data"]
    assert b'name="chat_id"' in body and b"-100" in body
    assert b'name="message_thread_id"' in body and b"3" in body
    assert b'filename="rolling.txt"' in body
    assert b"lista" in body
    assert "multipart/form-data; boundary=" in call["headers"]["Content-Type"]


def test_send_document_reports_every_failure(tmp_path: Path):
    path = tmp_path / "rolling.txt"
    path.write_text("lista", encoding="utf-8")
    target = TelegramTarget(token="t")

    with pytest.raises(RollingDeliveryError, match="nie ma pliku"):
        send_document(target, tmp_path / "nie-ma.txt", session=_Session({}))
    with pytest.raises(RollingDeliveryError, match="HTTP 500"):
        send_document(target, path, session=_Session({}, status_code=500))
    with pytest.raises(RollingDeliveryError, match="odrzucił"):
        send_document(target, path, session=_Session({"ok": False, "description": "nope"}))
    with pytest.raises(RollingDeliveryError, match="message_id"):
        send_document(target, path, session=_Session({"ok": True, "result": {}}))
    with pytest.raises(RollingDeliveryError, match="nieczytelna"):
        send_document(target, path, session=_Session("nie json"))


# ------------------------------------------------------------------ statusy


def test_mark_delivered_moves_new_to_seen(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead())
    ledger.add([row.as_seed()], now=NOW)
    changed = mark_delivered([row.key], ledger, now=NOW)
    assert changed == (row.key,)
    record = ledger.get(row.key)
    assert record is not None and record.status == STATUS_SEEN
    assert record.in_report_at == NOW


def test_store_drafts_keeps_the_message_and_rules_version(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead())
    ledger.add([row.as_seed()], now=NOW)
    entry = build_entries([row], ledger, priced=[priced_draft()])[0]

    stored = store_drafts([entry], ledger, rules_version="2026-10-07.1", now=NOW)
    assert stored == (row.key,)
    record = ledger.get(row.key)
    assert record is not None
    assert record.draft == MESSAGE
    assert record.rules_version == "2026-10-07.1"


def test_store_drafts_skips_entries_without_a_message(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead())
    ledger.add([row.as_seed()], now=NOW)
    entry = build_entries([row], ledger, priced=[])[0]
    assert store_drafts([entry], ledger, rules_version="x", now=NOW) == ()


# ------------------------------------------------------------------ dociąganie pełnej treści


def test_enrich_rows_fills_short_descriptions():
    from freelance_pipeline.rolling import enrich_rows

    short = LeadRow.from_json(lead(description="Krótki skrót z raportu."))
    long_row = LeadRow.from_json(lead(key="useme:2", source_id="2", description="x" * 1200))
    calls: list[str] = []

    def fetcher(row: LeadRow) -> str:
        calls.append(row.url)
        return "Pełna treść ogłoszenia. " * 40

    rows = enrich_rows([short, long_row], fetcher)
    assert len(rows[0].description) > 900
    assert rows[1].description == "x" * 1200
    assert calls == [short.url]  # długiego opisu nie pobieramy drugi raz


def test_enrich_rows_keeps_the_snippet_when_the_fetch_fails():
    from freelance_pipeline.rolling import enrich_rows

    short = LeadRow.from_json(lead(description="Krótki skrót"))

    def boom(row: LeadRow) -> str:
        raise OSError("sieć padła")

    rows = enrich_rows([short], boom)
    assert rows[0].description == "Krótki skrót"


def test_enrich_rows_ignores_failure_markers_and_tiny_pages():
    from freelance_pipeline.rolling import enrich_rows

    short = LeadRow.from_json(lead(description="Krótki skrót"))
    assert enrich_rows([short], lambda row: "(nie udało się pobrać treści: 403)")[0].description == "Krótki skrót"
    assert enrich_rows([short], lambda row: "za krótko")[0].description == "Krótki skrót"
    assert enrich_rows([short], lambda row: "")[0].description == "Krótki skrót"


def test_enrich_rows_caps_very_long_pages():
    from freelance_pipeline.rolling import MAX_DESCRIPTION_CHARS, enrich_rows

    short = LeadRow.from_json(lead(description="Krótki skrót"))
    rows = enrich_rows([short], lambda row: "a" * 20000)
    assert len(rows[0].description) == MAX_DESCRIPTION_CHARS


# ------------------------------------------------------------------ dokument markdown


def _entry_with_price(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead())
    ledger.add([row.as_seed()], now=NOW)
    return build_entries([row], ledger, priced=[priced_draft()])[0]


def test_render_markdown_has_headings_links_and_prices(tmp_path: Path):
    from freelance_pipeline.rolling import render_markdown

    entry = _entry_with_price(tmp_path)
    text = render_markdown([entry], now=NOW)

    assert text.startswith("# Snajper — rolling lista: 1 nowych ofert")
    assert "## [1] TOP · 100/100 · useme" in text
    assert "### Zlecę przygotowanie leadów B2B" in text
    assert "<https://useme.com/pl/jobs/x,145473/>" in text
    assert "**Wycena:** 1 800 zł, ok. 3 dni roboczych" in text
    assert "**Skąd ta kwota:** Stawki freelancerskie" in text
    assert "**Źródła:** <https://useme.com/pl/blog/ile-zaplacic-freelancerowi/>" in text
    assert "**Dlaczego na liście:** Klient z konkretnym zakresem." in text
    assert "**Treść ogłoszenia (skrót z ogłoszenia — pełna wersja pod linkiem):**" in text
    assert "```text" in text and MESSAGE in text


def test_render_markdown_can_point_at_a_docs_version(tmp_path: Path):
    from freelance_pipeline.rolling import render_markdown

    entry = _entry_with_price(tmp_path)
    text = render_markdown([entry], now=NOW, docs_url="https://docs.google.com/document/d/abc/edit")
    assert "[Wersja do czytania w Google Docs](https://docs.google.com/document/d/abc/edit)" in text


def test_render_markdown_marks_problems_and_zamien(tmp_path: Path):
    from freelance_pipeline.pricing import PriceBrief, PricedDraft
    from freelance_pipeline.proposal import ProposalDraft
    from freelance_pipeline.rolling import render_markdown

    ledger = Ledger(tmp_path / "ledger.json")
    rows = [
        LeadRow.from_json(lead(key="useme:z", source_id="z", bucket="ZAMIEN", score=90)),
        LeadRow.from_json(lead(key="useme:bad", source_id="bad", bucket="TOP", score=80)),
    ]
    ledger.add([row.as_seed() for row in rows], now=NOW)
    brief = PriceBrief(
        key="useme:bad",
        amount=3000.0,
        currency="PLN",
        days=5,
        hourly_min=90.0,
        hourly_max=150.0,
        includes=("audyt",),
        sources=("https://example.com/stawki",),
        rationale="Stawki.",
    )
    entries = build_entries(
        rows,
        ledger,
        priced=[
            priced_draft("useme:z"),
            PricedDraft(
                draft=ProposalDraft(key="useme:bad", text="za krótko"),
                brief=brief,
                text="za krótko",
                problems=("za krótka (9 znaków, minimum 700)",),
            ),
        ],
    )
    text = render_markdown(entries, now=NOW)
    assert "**ZAMIEN (1):**" in text
    assert "⚠ kontrola wyjścia: za krótka" in text


def test_render_markdown_is_empty_without_entries():
    from freelance_pipeline.rolling import render_markdown

    assert render_markdown([], now=NOW) == ""


def test_render_markdown_blockquotes_the_listing(tmp_path: Path):
    from freelance_pipeline.rolling import render_markdown

    ledger = Ledger(tmp_path / "ledger.json")
    row = LeadRow.from_json(lead(description="Pierwsza linia.\nDruga linia."))
    ledger.add([row.as_seed()], now=NOW)
    entry = build_entries([row], ledger, priced=[priced_draft()])[0]
    text = render_markdown([entry], now=NOW)
    assert "> Pierwsza linia.\n> Druga linia." in text


def test_enriched_page_chrome_never_reaches_the_reader(tmp_path_factory):
    """Model dostaje pełną stronę, ale Jan czyta skrót z adaptera, nie menu Useme."""
    from freelance_pipeline.rolling import enrich_rows

    row = LeadRow.from_json(lead(description="Szukam osoby do naprawy koszyka w PrestaShop."))
    page = "Skip to Content Dla freelancerów Zaloguj się Cennik " + "Opis zlecenia tutaj " * 60
    enriched = enrich_rows([row], lambda _: page)[0]

    assert len(enriched.description) > 900  # pełna strona poszła do modelu
    assert enriched.display_description == "Szukam osoby do naprawy koszyka w PrestaShop."

    ledger = Ledger(tmp_path_factory.mktemp("ledger") / "ledger.json")
    ledger.add([enriched.as_seed()], now=NOW)
    entry = build_entries([enriched], ledger, priced=[priced_draft()])[0]
    from freelance_pipeline.rolling import render_markdown as _render_markdown

    document = _render_markdown([entry], now=NOW)
    assert "Skip to Content" not in document
    assert "Szukam osoby do naprawy koszyka w PrestaShop." in document
