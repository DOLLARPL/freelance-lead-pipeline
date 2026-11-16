from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_pipeline.lead_identity import identity_keys, merge_leads
from freelance_pipeline.mailbox import MailMessage
from freelance_pipeline.models import JobLead
from freelance_pipeline.sources.oferteo_mail import (
    OferteoMailError,
    classify,
    parse_label_table,
    parse_oferteo_mail,
)

FIXTURES = Path(__file__).parent / "fixtures"
NEW_SUBJECT = "Zapytanie o wykonanie strony www, Częstochowa - nr 18344970"
REVEALED_SUBJECT = "☎ Zapytanie o przebudowę strony www, Łódź 18316618 - Zadzwoń"


def _message(subject: str, fixture: str, html: str | None = None) -> MailMessage:
    return MailMessage(
        message_id="msg-1",
        thread_id="thread-1",
        received_at=datetime(2026, 10, 27, 11, 22, tzinfo=UTC),
        subject=subject,
        from_addr="powiadomienie@oferteo.pl",
        html=html if html is not None else (FIXTURES / fixture).read_text("utf-8"),
    )


def test_new_request_parses_the_brief() -> None:
    lead = parse_oferteo_mail(_message(NEW_SUBJECT, "oferteo_mail_new.html"))
    assert lead is not None
    assert lead.source == "oferteo_mail"
    assert lead.source_id == "oferteo:18344970"
    assert lead.location == "Częstochowa"
    assert lead.status == "new"
    assert lead.published_at is not None
    assert lead.published_at.year == 2026
    assert lead.published_at.hour == 11
    assert lead.url.startswith("https://www.oferteo.pl/")
    assert lead.company_or_author
    raw = lead.raw_data
    assert raw["has_phone"] is True
    assert raw["contact_confirmed"] is True
    assert raw["category"] == "Tworzenie stron internetowych"
    assert "Rodzaj strony" in lead.description
    assert len(raw["fields"]) >= 8


def test_label_table_keeps_scope_fields() -> None:
    html = (FIXTURES / "oferteo_mail_new.html").read_text("utf-8")
    pairs = parse_label_table(html)
    assert pairs["Kategoria"] == "Tworzenie stron internetowych"
    assert pairs["Liczba podstron/zakładek"] == "2-5"
    assert pairs["Termin skorzystania z oferty"] == "w ciągu miesiąca"
    assert pairs["Nr zapytania ofertowego"] == "18344970"


def test_revealed_mail_reuses_the_request_number_and_exposes_mail() -> None:
    lead = parse_oferteo_mail(_message(REVEALED_SUBJECT, "oferteo_mail_revealed.html"))
    assert lead is not None
    assert lead.status == "revealed"
    assert lead.source_id == "oferteo:18316618"
    assert lead.raw_data["client_mail"] == "klient@przyklad.pl"


def test_new_and_revealed_share_identity() -> None:
    first = parse_oferteo_mail(_message(NEW_SUBJECT, "oferteo_mail_new.html"))
    second = parse_oferteo_mail(_message(REVEALED_SUBJECT, "oferteo_mail_revealed.html"))
    assert first and second
    assert set(identity_keys(first)) != set(identity_keys(second))
    same = JobLead(
        source="oferteo_mail",
        source_id="oferteo:18344970",
        url="https://www.oferteo.pl/zlecenia",
        raw_data={"rfp_id": "18344970"},
    )
    assert "oferteo:18344970" in identity_keys(same)


def test_merge_fills_gaps_without_erasing() -> None:
    existing = JobLead(
        source="oferteo_mail",
        source_id="oferteo:1",
        url="https://www.oferteo.pl/zlecenia",
        title="Zapytanie o stronę",
        budget=2000.0,
    )
    incoming = JobLead(
        source="oferteo_mail",
        source_id="oferteo:1",
        url="https://www.oferteo.pl/konto/zapytanie/1_x/odkryj",
        description="Zakres zlecenia: wykonanie strony",
        location="Warszawa",
        status="revealed",
    )
    merged = merge_leads(existing, incoming)
    assert merged.budget == 2000.0
    assert merged.description.startswith("Zakres zlecenia")
    assert merged.status == "revealed"
    assert merged.location == "Warszawa"


def test_classify_other_for_account_manager_mail() -> None:
    assert classify("Polecam Panu Klientów z miejscowości Warszawa", "<p>oferta</p>") == "other"
    assert classify(NEW_SUBJECT, "<p>Nr zapytania ofertowego</p>") == "new"
    assert classify(REVEALED_SUBJECT, "") == "revealed"


def test_missing_request_number_raises() -> None:
    html = "<p>Nr zapytania ofertowego</p><p><b></b></p>"
    with pytest.raises(OferteoMailError):
        parse_oferteo_mail(_message("Zapytanie bez numeru", "", html=html))


def test_non_request_mail_is_ignored() -> None:
    assert parse_oferteo_mail(_message("Polecam Klientów", "", html="<p>x</p>")) is None
