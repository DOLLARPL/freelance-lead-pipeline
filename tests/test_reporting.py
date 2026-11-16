"""Offline tests for the plain-text report: only TOP and MOŻE rows are shown
with source, title, company/author, URL, bucket, score and rationale."""

from __future__ import annotations

from datetime import UTC, datetime

from freelance_pipeline.models import JobLead
from freelance_pipeline.reporting import (
    render_chat_report,
    render_moze_report,
    render_report,
    render_top_report,
    render_zamien_report,
)
from freelance_pipeline.scoring import ScoreBucket, ScoredLead

FIXED_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def make_lead(**overrides) -> JobLead:
    base = {
        "source": "useme",
        "source_id": "1",
        "url": "https://useme.com/pl/jobs/example,1/",
        "canonical_url": "https://useme.com/pl/jobs/example,1/",
        "title": "Integracja API",
        "company_or_author": "Adam",
    }
    base.update(overrides)
    return JobLead(**base)


def scored(bucket: ScoreBucket, score: int, lead: JobLead, rationale: str = "krótki powód") -> ScoredLead:
    return ScoredLead(lead, score, bucket, rationale)


def test_empty_score_set_produces_empty_report():
    assert render_report([], now=FIXED_NOW) == ""


def test_top_entry_contains_all_mandatory_fields():
    text = render_report([scored(ScoreBucket.TOP, 95, make_lead())], now=FIXED_NOW)
    assert "[TOP] 95/100 — Integracja API" in text
    assert "source: useme" in text
    assert "company/author: Adam" in text
    assert "url: https://useme.com/pl/jobs/example,1/" in text
    assert "rationale: krótki powód" in text


def test_oferteo_row_says_the_contact_is_paid():
    """Punkt 1 decyzji 2026-09-25: raport musi jawnie oznaczyć płatny kontakt."""
    paid = make_lead(
        source="oferteo",
        source_id="18204894",
        url="https://www.oferteo.pl/zlecenia-na-tworzenie-stron-internetowych",
        title="Zapytanie o wykonanie strony www",
        raw_data={"contact_requires_points": True},
    )
    text = render_report([scored(ScoreBucket.TOP, 90, paid)], now=FIXED_NOW)
    assert "source: oferteo" in text
    assert "kontakt: płatny, punkty Oferteo, nie odblokowujemy automatycznie" in text

    # Useme i reszta źródeł nie dostają tej linii.
    free = render_report([scored(ScoreBucket.TOP, 90, make_lead())], now=FIXED_NOW)
    assert "kontakt:" not in free


def test_moze_is_included():
    text = render_report([scored(ScoreBucket.MOZE, 44, make_lead())], now=FIXED_NOW)
    assert "[MOŻE]" in text
    assert "44/100" in text


def test_odrzuc_is_never_in_report():
    entries = [
        scored(ScoreBucket.TOP, 90, make_lead(source_id="t")),
        scored(ScoreBucket.ODRZUC, 5, make_lead(source_id="r")),
    ]
    assert "ODRZUĆ" not in render_report(entries, now=FIXED_NOW)
    assert "[TOP]" in render_report(entries, now=FIXED_NOW)


def test_company_author_missing_is_rendered_as_none():
    text = render_report([scored(ScoreBucket.TOP, 95, make_lead(company_or_author=None))], now=FIXED_NOW)
    assert "company/author:" in text


def test_order_follows_scoring_input_order():
    first = scored(ScoreBucket.TOP, 90, make_lead(source_id="a", title="AAA"))
    second = scored(ScoreBucket.TOP, 80, make_lead(source_id="b", title="BBB"))
    report = render_report([first, second], now=FIXED_NOW)
    assert report.index("AAA") < report.index("BBB")


def test_report_is_reproducible_for_same_input():
    entries = [scored(ScoreBucket.TOP, 90, make_lead(source_id="a"))]
    assert render_report(entries, now=FIXED_NOW) == render_report(entries, now=FIXED_NOW)


# ---------- chat report (TOP + MOŻE, uncapped since 2026-09-22) versus the MOŻE file ----------


def test_chat_report_carries_moze_rows_too():
    entries = [
        scored(ScoreBucket.TOP, 90, make_lead(source_id="t", title="Prawdziwe zlecenie")),
        scored(ScoreBucket.MOZE, 44, make_lead(source_id="m", title="Słabe dopasowanie")),
    ]
    chat = render_chat_report(entries, now=FIXED_NOW)
    assert "[TOP] 90/100" in chat
    assert "[MOŻE] 44/100" in chat
    assert "Słabe dopasowanie" in chat
    assert "Liczba nowych ofert w kubełku: 2" in chat


def test_chat_report_is_empty_without_top_and_moze_so_cron_stays_silent():
    entries = [
        scored(ScoreBucket.ODRZUC, 10, make_lead(source_id="r")),
        scored(ScoreBucket.ZAMIEN, 61, make_lead(source_id="z")),
    ]
    assert render_chat_report(entries, now=FIXED_NOW) == ""


def test_chat_report_is_uncapped_and_drops_no_row():
    total = 25
    entries = [
        scored(ScoreBucket.TOP, 60 + index, make_lead(source_id=str(index), title=f"Zlecenie {index}"))
        for index in range(total)
    ]
    chat = render_chat_report(entries, now=FIXED_NOW)
    assert chat.count("[TOP]") == total
    for index in range(total):
        assert f"Zlecenie {index}" in chat
    assert "kolejnych TOP" not in chat


def test_moze_report_holds_every_moze_row_and_no_top():
    entries = [
        scored(ScoreBucket.TOP, 90, make_lead(source_id="t", title="TOP-owe")),
        scored(ScoreBucket.MOZE, 44, make_lead(source_id="m1", title="Może jeden")),
        scored(ScoreBucket.MOZE, 41, make_lead(source_id="m2", title="Może dwa")),
    ]
    moze = render_moze_report(entries, now=FIXED_NOW)
    assert "Może jeden" in moze and "Może dwa" in moze
    assert "TOP-owe" not in moze
    assert moze.count("[MOŻE]") == 2


def test_moze_report_is_empty_when_no_moze():
    assert render_moze_report([scored(ScoreBucket.TOP, 90, make_lead())], now=FIXED_NOW) == ""


# ---------- priorytet polskich źródeł w raporcie czatowym (decyzja Jana 2026-09-21) ----------


def test_chat_report_puts_polish_sources_first():
    entries = [
        scored(ScoreBucket.TOP, 95, make_lead(source="n8ndevs", source_id="n1", title="Zagraniczne zlecenie")),
        scored(ScoreBucket.TOP, 70, make_lead(source="useme", source_id="u1", title="Polskie zlecenie")),
    ]
    chat = render_chat_report(entries, now=FIXED_NOW)
    assert chat.index("Polskie zlecenie") < chat.index("Zagraniczne zlecenie")


def test_chat_report_ranks_by_score_inside_each_group():
    entries = [
        scored(ScoreBucket.TOP, 60, make_lead(source="useme", source_id="u1", title="Polskie słabsze")),
        scored(ScoreBucket.TOP, 90, make_lead(source="useme", source_id="u2", title="Polskie mocne")),
    ]
    chat = render_chat_report(entries, now=FIXED_NOW)
    assert chat.index("Polskie mocne") < chat.index("Polskie słabsze")


def test_polish_rows_are_not_pushed_out_by_a_flood_of_foreign_rows():
    entries = [
        scored(ScoreBucket.TOP, 90 - index, make_lead(source="n8ndevs", source_id=f"n{index}", title=f"Zagraniczne {index}"))
        for index in range(20)
    ]
    entries.append(scored(ScoreBucket.TOP, 40, make_lead(source="useme", source_id="u1", title="Polskie zlecenie")))
    chat = render_chat_report(entries, now=FIXED_NOW)
    assert chat.count("[TOP]") == 21
    assert "Polskie zlecenie" in chat
    assert "Zagraniczne 19" in chat  # nothing is capped away any more


def test_every_source_ships_all_its_rows_now():
    # The facebook_groups ceiling of five rows per report was removed on 2026-09-22.
    entries = [
        scored(ScoreBucket.TOP, 50, make_lead(source="facebook_groups", source_id=f"f{index}", title=f"Grupa {index}"))
        for index in range(8)
    ]
    chat = render_chat_report(entries, now=FIXED_NOW)
    assert chat.count("[TOP]") == 8
    assert "Grupa 7" in chat


def test_top_archive_keeps_the_scoring_order():
    entries = [
        scored(ScoreBucket.TOP, 50, make_lead(source="useme", source_id="u1", title="Polskie zlecenie")),
        scored(ScoreBucket.TOP, 90, make_lead(source="n8ndevs", source_id="n1", title="Zagraniczne zlecenie")),
    ]
    archive = render_top_report(entries, now=FIXED_NOW)
    assert archive.index("Polskie zlecenie") < archive.index("Zagraniczne zlecenie")


# ---------- ZAMIEN: osobny plik, nigdy czat ----------


def test_zamien_report_lists_only_zamien_rows():
    entries = [
        scored(ScoreBucket.TOP, 90, make_lead(source_id="t", title="Prawdziwe zlecenie")),
        scored(ScoreBucket.MOZE, 44, make_lead(source_id="m", title="Może")),
        scored(ScoreBucket.ZAMIEN, 61, make_lead(source_id="z", title="Szukam osoby do faktur")),
    ]
    report = render_zamien_report(entries, now=FIXED_NOW)
    assert "[ZAMIEN] 61/100 — Szukam osoby do faktur" in report
    assert "Liczba nowych ofert w kubełku: 1" in report
    assert "Prawdziwe zlecenie" not in report
    assert "Może" not in report


def test_zamien_rows_never_reach_the_chat_report():
    entries = [scored(ScoreBucket.ZAMIEN, 61, make_lead(source_id="z", title="Szukam osoby do faktur"))]
    assert render_chat_report(entries, now=FIXED_NOW) == ""
    assert render_moze_report(entries, now=FIXED_NOW) == ""


def test_zamien_report_is_empty_when_there_is_no_such_row():
    assert render_zamien_report([scored(ScoreBucket.TOP, 90, make_lead())], now=FIXED_NOW) == ""


def test_oferteo_row_carries_zapytanie_details():
    """Punkt 2 decyzji 2026-09-25: miasto, typ zlecającego i data w wierszu.

    24 zapytania o stronę www wyglądały identycznie, choć adapter zbiera miasto,
    typ zlecającego i datę publikacji. Teraz widać je w jednej linii.
    """
    paid = make_lead(
        source="oferteo",
        source_id="18204894",
        url="https://www.oferteo.pl/zlecenia-na-tworzenie-stron-internetowych",
        title="Zapytanie o wykonanie strony www",
        location="Kraków",
        published_at=datetime(2026, 9, 24, 9, 30, tzinfo=UTC),
        raw_data={
            "contact_requires_points": True,
            "buyer_type": "firma",
            "deadline": "w ciągu miesiąca",
            "buyer_verified": True,
        },
    )
    text = render_report([scored(ScoreBucket.TOP, 90, paid)], now=FIXED_NOW)
    assert "  zapytanie: Kraków | firma | opublikowane 2026-09-24 | termin: w ciągu miesiąca" in text
    assert "zlecający zweryfikowany" in text


def test_oferteo_row_without_metadata_skips_the_details_line():
    """Brak metadanych nie tworzy pustej linii ani myślników-zapchajdziur."""
    paid = make_lead(
        source="oferteo",
        source_id="1",
        title="Zapytanie o sklep internetowy",
        raw_data={"contact_requires_points": True},
    )
    text = render_report([scored(ScoreBucket.TOP, 90, paid)], now=FIXED_NOW)
    assert "kontakt: płatny" in text
    assert "zapytanie:" not in text


def test_oferteo_row_carries_the_exact_link_and_the_list_it_came_from():
    """Wiersz musi mieć link do konkretnego zapytania, a nie tylko do kategorii.

    Jan (2026-09-25): „URL-e są za mało dokładne, bo są tylko do kategorii”.
    Link do zapytania działa po darmowym zalogowaniu, więc obok niego jedzie
    lista kategorii jako droga działająca bez logowania.
    """
    paid = make_lead(
        source="oferteo",
        source_id="18204894",
        url="https://www.oferteo.pl/konto/zapytanie/18204894_X",
        title="Zapytanie o wykonanie strony www",
        raw_data={
            "contact_requires_points": True,
            "listing_url": "https://www.oferteo.pl/zlecenia-na-tworzenie-stron-internetowych",
        },
    )
    text = render_report([scored(ScoreBucket.TOP, 90, paid)], now=FIXED_NOW)
    assert "  url: https://www.oferteo.pl/konto/zapytanie/18204894_X" in text
    assert "  lista: https://www.oferteo.pl/zlecenia-na-tworzenie-stron-internetowych" in text


def test_zapytanie_details_line_never_touches_other_sources():
    """Źródła inne niż Oferteo zostają przy pięciu liniach — bez nowych pól."""
    free = make_lead(
        location="Kraków",
        published_at=datetime(2026, 9, 24, 9, 30, tzinfo=UTC),
        raw_data={"buyer_type": "firma"},
    )
    text = render_report([scored(ScoreBucket.TOP, 90, free)], now=FIXED_NOW)
    assert "zapytanie:" not in text
    assert "Kraków" not in text
