"""Adapter Oferteo: publiczna lista czytana bez logowania i bez płacenia.

Kontrakt, który te testy pilnują (2026-09-25):

1. Dane pochodzą wyłącznie z publicznej listy ``/zlecenia…``; ścieżki
   ``/rfp/`` (odblokowanie kontaktu) są zabronione w konfiguracji, bo robots.txt
   ich zakazuje, a kontakt jest płatny punktami.
2. Każdy lead niesie ``contact_requires_points=True`` i jawny opis, że kontaktu
   adapter nie odkrywa.
3. Ta sama zapytanie w dwóch kategoriach liczy się raz.
4. Zmiana układu strony kończy się błędem, nie pustą listą.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.oferteo import (
    OFERTEO_PRODUCTION_CATEGORY_PATHS,
    OferteoError,
    OferteoJobs,
    parse_list_page,
)
from tests.fakes import FakeResponse, FakeSession

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = FIXTURES / "oferteo_listing_page1.html"
EMPTY = FIXTURES / "oferteo_listing_empty.html"
BASE_URL = "https://www.oferteo.pl"
LANE = "/zlecenia-na-tworzenie-stron-internetowych"


def listing_html() -> str:
    return LISTING.read_text(encoding="utf-8")


def parse(html: str | None = None, *, category_path: str = LANE) -> list[JobLead]:
    return parse_list_page(
        listing_html() if html is None else html,
        base_url=BASE_URL,
        category_path=category_path,
    )


def test_parses_every_card_from_a_real_listing_page():
    leads = parse()
    assert len(leads) == 8
    assert all(isinstance(lead, JobLead) for lead in leads)
    assert [lead.source_id for lead in leads] == [
        "18204894",
        "18204655",
        "18204592",
        "18202229",
        "18200542",
        "18198777",
        "18197917",
        "18197815",
    ]


def test_first_lead_carries_title_city_buyer_and_date():
    lead = parse()[0]
    assert lead.source == "oferteo"
    assert lead.title == "Zapytanie o wykonanie strony www"
    assert lead.location == "Opole"
    assert lead.company_or_author == "Marco"
    assert lead.published_at == datetime(2026, 9, 25, tzinfo=UTC)
    assert lead.language == "pl"
    assert lead.raw_data["buyer_type"] == "osoba prywatna"
    assert lead.raw_data["deadline"] == "możliwe od zaraz"
    assert lead.raw_data["category_path"] == LANE
    assert lead.raw_data["rfp_id"] == "18204894"


def test_url_points_at_the_zapytanie_not_only_the_category():
    """Link musi prowadzić do konkretnego zapytania, nie do całej kategorii.

    Jan (2026-09-25): „URL-e są za mało dokładne, bo są tylko do kategorii, a
    później muszę ręcznie szukać konkretnego zapytania”. Adres pojedynczego
    zapytania to ten, który sam portal podstawia w oknie rejestracji:
    ``/konto/zapytanie/<rfp-id>_X`` (widoczny po darmowym zalogowaniu).
    """
    lead = parse()[0]
    assert lead.url == f"{BASE_URL}/konto/zapytanie/18204894_X"
    assert lead.raw_data["offer_url"] == lead.url
    # Druga, zawsze otwarta droga: lista kategorii, z której zapytanie przyszło.
    assert lead.raw_data["listing_url"] == f"{BASE_URL}{LANE}"
    assert lead.raw_data["listing_url"] != lead.url


def test_company_buyer_is_read_positionally_not_hinted():
    """„firma” i wolny tekst terminu muszą wejść bez zgadywania słów-kluczy."""
    leads = {lead.source_id: lead for lead in parse()}
    assert leads["18204592"].raw_data["buyer_type"] == "firma"
    # Termin bywa datą, nie frazą typu „możliwe od zaraz”.
    assert leads["18202229"].raw_data["deadline"] == "orientacyjna data: 04.01.2027"


def test_every_lead_marks_the_contact_as_paid_and_never_reveals_it():
    for lead in parse():
        assert lead.raw_data["contact_requires_points"] is True
        assert lead.raw_data["contact_masked"] is True
        assert "Kontakt płatny" in lead.description
        assert "+48●●" not in lead.description
        assert "uncover" not in lead.url


def test_empty_page_is_a_valid_empty_result_not_an_error():
    assert parse(EMPTY.read_text(encoding="utf-8")) == []


def test_changed_layout_raises_instead_of_returning_zero_leads():
    with pytest.raises(OferteoError):
        parse("<html><body><h1>Zlecenia</h1></body></html>")


def test_card_without_a_title_is_skipped():
    html = listing_html().replace(
        '<a href="" rel="nofollow" data-test="rfp_list__item__title"'
        ' class="rfp-tile_title | heading-xs w-full text-info !font-normal">\n\t\t\t\tZapytanie o wykonanie strony www</a>',
        '<a href="" rel="nofollow" data-test="rfp_list__item__title"'
        ' class="rfp-tile_title | heading-xs w-full text-info !font-normal"></a>',
        1,
    )
    leads = parse(html)
    assert len(leads) == 7
    assert "18204894" not in {lead.source_id for lead in leads}


def test_rfp_contact_reveal_paths_are_rejected_by_config():
    """Reguła prawna: robots.txt zabrania /rfp/ i /rfp/remote/uncover.ofer."""
    for path in ("/rfp/remote/uncover.ofer", "/rfp"):
        with pytest.raises(ValueError):
            OferteoJobs(category_paths=[path])


def test_collect_reads_each_lane_once_and_dedupes_ids_across_lanes():
    session = FakeSession(
        {
            f"{BASE_URL}/zlecenia-it": FakeResponse(listing_html()),
            f"{BASE_URL}{LANE}": FakeResponse(listing_html()),
        }
    )
    adapter = OferteoJobs(
        category_paths=["/zlecenia-it", LANE],
        session=session,
    )
    leads = adapter.collect()
    assert [call[0] for call in session.calls] == [
        f"{BASE_URL}/zlecenia-it",
        f"{BASE_URL}{LANE}",
    ]
    assert len(leads) == 8
    assert len({lead.source_id for lead in leads}) == 8
    # Pierwsza kategoria, która wystawiła zapytanie, decyduje o jego metadanych.
    assert all(lead.raw_data["category_path"] == "/zlecenia-it" for lead in leads)


def test_collect_reports_transport_failure_as_source_error():
    import requests

    session = FakeSession(
        {}, errors={f"{BASE_URL}/zlecenia-it": requests.ConnectionError("boom")}
    )
    adapter = OferteoJobs(session=session)
    with pytest.raises(OferteoError):
        adapter.collect()


def test_production_lanes_stay_off_marketing_only_and_audit_lists():
    """Kolejność i zakres kategorii: WWW, sklepy, obsługa stron, e-marketing, IT.

    Jan (2026-09-25) chciał węższych linków: „sklepy internetowe” i „obsługa
    stron” to osobne kategorie Oferteo, więc nie ma powodu wrzucać ich do jednej
    szerokiej listy. ``/zlecenia-it`` zostaje tylko dla integracji i outsourcingu;
    serwis sprzętu wycina deterministyczny filtr w scoring.py.
    """
    assert OFERTEO_PRODUCTION_CATEGORY_PATHS == (
        "/zlecenia-na-tworzenie-stron-internetowych",
        "/zlecenia-na-sklepy-internetowe",
        "/zlecenia-na-obsluge-stron-internetowych",
        "/zlecenia-e-marketingowe",
        "/zlecenia-it",
    )
    assert all(path.startswith("/zlecenia") for path in OFERTEO_PRODUCTION_CATEGORY_PATHS)


def test_week_old_leads_survive_the_adapter_and_let_policy_judge_age():
    """Data publikacji musi być świadoma strefy, inaczej filtr wieku odpadnie."""
    for lead in parse():
        assert lead.published_at is not None
        assert lead.published_at.utcoffset() is not None
