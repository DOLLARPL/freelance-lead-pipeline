"""Wiadomość natychmiastowa: co Jan widzi, gdy pojawi się nowe zlecenie."""

from __future__ import annotations

from datetime import UTC, datetime

from freelance_pipeline.instant import (
    MAX_TEXT_CHARS,
    render_message,
    render_offer_block,
    split_message,
)

NOW = datetime(2026, 10, 28, 14, 40, tzinfo=UTC)
DRAFT = (
    "Cześć,\n\nwyszukiwarka nie łapie tego, czego szuka klient.\n\n"
    "2 500 zł, ok. 10 dni roboczych. W cenie: jeden proces, jeden system. "
    "Wycena i czas są orientacyjne, dopasujemy je po szczegółach.\n\n"
    "Od czego zaczynamy?\n\nJan Noga\njnxautomation.pl\n+48 600 000 000"
)


def block(**overrides):
    data = dict(
        index=1,
        bucket="TOP",
        score=80,
        source="useme",
        title="Integracja wyszukiwarki Algolia ze sklepem Shoper",
        url="https://useme.com/pl/jobs/x,146640/",
        brief="Klient chce podpowiedzi w wyszukiwarce sklepu.",
        draft_text=DRAFT,
        amount=2500.0,
        currency="PLN",
        days=10,
        budget=None,
        budget_currency=None,
        note="cennik 2026-10-28.1: integracja/start",
    )
    data.update(overrides)
    return data


def test_block_keeps_the_score_and_has_a_brief_section():
    text = render_offer_block(**block())
    assert text.startswith("[1] TOP 80/100 useme")
    assert "Brief: Klient chce podpowiedzi" in text
    assert "https://useme.com/pl/jobs/x,146640/" in text
    assert "Cena: 2 500 zł brutto, ok. 10 dni roboczych" in text
    assert "Wiadomość do wklejenia:" in text
    assert "Jan Noga" in text


def test_block_drops_what_jan_called_noise():
    text = render_offer_block(**block())
    # Autor, podgląd ogłoszenia, źródła wyceny i kontrola wyjścia nie wchodzą do wiadomości.
    for noise in ("autor:", "treść ogłoszenia:", "źródła:", "kontrola wyjścia"):
        assert noise not in text


def test_missing_price_is_stated_plainly_instead_of_invented():
    text = render_offer_block(**block(amount=None, days=None))
    assert "Cena: brak (cennik nie dopasował zlecenia)" in text
    assert "zł brutto" not in text


def test_missing_draft_tells_him_which_command_to_use():
    text = render_offer_block(**block(draft_text=""))
    assert "popraw 1" in text


def test_budget_from_the_listing_is_shown_once():
    text = render_offer_block(**block(budget=200.0, budget_currency="PLN"))
    assert "Budżet z ogłoszenia: 200 zł" in text


def test_message_header_only_when_more_than_one_offer():
    single = render_message([block()], now=NOW)
    assert not single.startswith("SNAJPER:")
    double = render_message([block(), block(index=2, score=70)], now=NOW)
    assert double.startswith("SNAJPER: 2 nowe oferty")


def test_hint_goes_only_to_single_offer_messages():
    hinted = render_message([block()], now=NOW, hint="Odpisz tu: „1 odpisałem”.")
    assert "„1 odpisałem”" in hinted


def test_long_message_is_split_on_blank_lines():
    text = render_message([block(), block(index=2, draft_text="x" * 5000)], now=NOW)
    assert len(text) > MAX_TEXT_CHARS
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(part) <= MAX_TEXT_CHARS for part in parts)
    assert "".join(parts).count("Jan Noga") >= 1
