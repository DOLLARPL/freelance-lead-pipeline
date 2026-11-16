"""Kontrola wyjścia wiadomości do zlecenia — reguły deterministyczne."""

from __future__ import annotations

import pytest

from freelance_pipeline.proposal_check import (
    AI_PHRASES,
    DRAFT_MAX_CHARS,
    DRAFT_MIN_CHARS,
    MAX_BULLET_CHARS,
    PRICE_MARKER,
    RULES_VERSION,
    SIGNATURE,
    check_draft,
    marker_count,
    price_paragraph,
    strip_price_marker,
    summarise,
)

LINK = "https://jnxautomation.pl/projekty/ksef-document-agent"
PORTFOLIO_CONTEXT = (
    "# Portfolio na żywo\n"
    "- KSeF Document Agent\n"
    f"  link: {LINK}\n"
    "- strona główna: https://jnxautomation.pl\n"
)
GREETING = "Cześć,"
INTRO = (
    "koszt przesyłki nie wchodzi do zamówienia, bo Sfera przyjmuje dokument pozycja po pozycji "
    "i to pole musi być wypełnione jawnie, inaczej Subiekt zapisuje zero. Skoro macie kod źródłowy, "
    "poprawka jest lokalna: dokładam osobny element dokumentu na koszt przesyłki z zamówienia "
    "WooCommerce, z kontrolą, czy Subiekt nie doliczy go drugi raz. Zgłoszenie bez tego kosztu "
    "przechodzi dziś bez błędu, tylko kwota na dokumencie jest zaniżona."
)
MIDDLE = (
    "Wystawianie pozycji dokumentu z mapowaniem danych mam tutaj: "
    f"{LINK}. Poprawka obejmuje też drugie wdrożenie, więc zostaje jedna wspólna ścieżka. "
    "Nie ruszam przy tym wystawiania pozostałych dokumentów ani samej wysyłki zamówień."
)
QUESTION = "Czy przy zamówieniu bez kosztu przesyłki ma lecieć pozycja zerowa?"
SIGNATURE_BLOCK = "\n".join(SIGNATURE)


def message(
    *,
    greeting: str = GREETING,
    intro: str = INTRO,
    middle: str = MIDDLE,
    bullets: str = "",
    tail: str = "",
    price: bool = True,
    question: str = QUESTION,
    signature: str = SIGNATURE_BLOCK,
    marker_times: int = 1,
) -> str:
    """Zbuduj wiadomość zgodną z kontraktem; testy psują w niej jedną rzecz naraz."""
    blocks = [greeting, intro, middle]
    if bullets:
        blocks.append(bullets)
    if tail:
        blocks.append(tail)
    if price:
        blocks.extend([PRICE_MARKER] * marker_times)
    if question:
        blocks.append(question)
    blocks.append(signature)
    return "\n\n".join(blocks)


def problems_of(text: str, *, context: str = "") -> tuple[str, ...]:
    return check_draft(text, context=context)


def has(problems: tuple[str, ...], needle: str) -> bool:
    return any(needle in problem for problem in problems)


# ------------------------------------------------------------------ poziom zerowy


def test_baseline_draft_is_clean():
    text = message()
    assert marker_count(text) == 1
    assert DRAFT_MIN_CHARS <= len(strip_price_marker(text)) <= DRAFT_MAX_CHARS
    assert problems_of(text, context=PORTFOLIO_CONTEXT) == ()


def test_check_draft_rejects_non_string():
    with pytest.raises(TypeError):
        check_draft(123)  # type: ignore[arg-type]


def test_rules_version_is_pinned():
    assert RULES_VERSION == "2026-10-09.1"


# ------------------------------------------------------------------ wycena


def test_missing_price_marker_is_reported():
    text = message(price=False)
    assert has(problems_of(text), "brak znacznika wyceny")


def test_duplicate_price_marker_is_reported():
    text = message(marker_times=2)
    assert has(problems_of(text), "znacznik wyceny występuje 2 razy")


def test_any_amount_in_a_draft_is_reported():
    text = message(tail="Całość zamknęłaby się w 1 800 zł, ok. 5 dni.")
    assert has(problems_of(text), "kwota w drafcie")


def test_marker_is_not_counted_towards_the_length_window():
    """Draft + wstawiona cena musi nadal być policzalny: marker nie wchodzi do limitu."""
    text = message()
    assert len(strip_price_marker(text)) < len(text)


def test_priced_message_passes_and_needs_the_amount_first():
    priced = message(price=False).replace(
        QUESTION, "1 800 zł, ok. 5 dni za poprawkę na obu platformach.\n\n" + QUESTION
    )
    assert check_draft(priced, context=PORTFOLIO_CONTEXT, priced=True) == ()


def test_priced_message_without_a_price_is_reported():
    priced = message(price=False)
    assert has(check_draft(priced, priced=True), "brak ceny")


def test_price_paragraph_must_start_with_the_amount():
    priced = message(price=False).replace(
        QUESTION, "Za tę poprawkę policzę 1 800 zł, ok. 5 dni.\n\n" + QUESTION
    )
    assert has(check_draft(priced, priced=True), "akapit z ceną nie zaczyna się od kwoty")


def test_two_amounts_in_the_price_paragraph_are_a_range():
    priced = message(price=False).replace(
        QUESTION, "1 500–2 500 zł, ok. 5 dni roboczych.\n\n" + QUESTION
    )
    assert has(check_draft(priced, priced=True), "widełki w akapicie z ceną")


def test_marker_in_a_priced_message_is_reported():
    priced = message(price=False) + "\n\n1 800 zł, ok. 5 dni." + f"\n\n{PRICE_MARKER}"
    assert has(check_draft(priced, priced=True), "znacznik wyceny")


def test_price_paragraph_helper_finds_the_block():
    priced = message(price=False).replace(QUESTION, "1 800 zł, ok. 5 dni.\n\n" + QUESTION)
    assert price_paragraph(priced) == "1 800 zł, ok. 5 dni."
    assert price_paragraph(message(price=False)) is None


# ------------------------------------------------------------------ długość


def test_too_short_draft_is_reported():
    text = message(intro="Wiem, o co chodzi.", middle="Zrobię to.")
    assert has(problems_of(text), "za krótka")


def test_too_long_draft_is_reported():
    filler = "Ta poprawka nie rusza pozostałych integracji, a mapowanie jest wspólne dla obu wdrożeń."
    text = message(tail=f"{filler} {filler} {filler} {filler} {filler} {filler}")
    assert has(problems_of(text), "za długa")


# ------------------------------------------------------------------ język i linki


def test_foreign_language_message_is_reported():
    text = message(tail="Hola, podemos empezar cuando quieras, gracias.")
    assert has(problems_of(text), "język inny niż polski")


def test_two_links_are_reported():
    text = message(tail="Drugi projekt: https://jnxautomation.pl/projekty/affiliate-matrix")
    assert has(problems_of(text), "więcej niż jeden link")


def test_link_outside_the_portfolio_is_reported():
    text = message(middle="Mam to tutaj: https://example.com/projekt. Reszta bez zmian.")
    assert has(problems_of(text, context=PORTFOLIO_CONTEXT), "link spoza portfolio")


def test_portfolio_link_is_allowed_with_context():
    assert problems_of(message(), context=PORTFOLIO_CONTEXT) == ()


def test_foreign_offer_needs_the_honesty_sentence_in_the_first_paragraph():
    spanish_context = "Busco freelancer, presupuesto y plazo. Hola."
    text = message()
    assert has(problems_of(text, context=spanish_context), "zdania o języku pracy")


def test_honesty_sentence_satisfies_the_foreign_rule():
    spanish_context = "Busco freelancer, presupuesto y plazo. Hola."
    text = message(intro="I work in English and Polish, so we can keep it written. " + INTRO)
    assert not has(problems_of(text, context=spanish_context), "zdania o języku pracy")


# ------------------------------------------------------------------ adresatywność i powitanie


def test_mixed_address_forms_are_reported():
    text = message(tail="Państwo dostaniecie gotowe mapowanie, a Ty dostaniesz listę testów.")
    assert has(problems_of(text), "mieszana forma")


def test_missing_greeting_is_reported():
    assert has(problems_of(message(greeting="Witam bez przecinka")), "brak powitania")


def test_greeting_with_exclamation_mark_is_not_a_greeting():
    assert has(problems_of(message(greeting="Cześć!")), "brak powitania")


# ------------------------------------------------------------------ podpis


def test_signature_must_be_exactly_three_lines():
    text = message(signature="Jan Noga\njnxautomation.pl\n+48 600 000 000\nPozdrawiam")
    assert has(problems_of(text), "podpis nie jest dokładnie w trzech wierszach")


def test_signature_without_phone_is_reported():
    assert has(problems_of(message(signature="Jan Noga\njnxautomation.pl")), "brak telefonu w podpisie")


def test_signature_without_domain_is_reported():
    # Bez linku portfolio w treści, żeby domena mogła pochodzić tylko z podpisu.
    text = message(middle="Poprawka jest lokalna i nie rusza reszty integracji.", signature="Jan Noga\n+48 600 000 000")
    assert has(problems_of(text), "brak linku do strony w podpisie")


@pytest.mark.parametrize("bad", ["Jan Nogaj\njnxautomation.pl\n+48 600 000 000", "Jan Noga\njnxautomation.pl\n+48 600 000 000\njan@example.com"])
def test_swapped_signature_data_is_reported(bad: str):
    assert has(problems_of(message(signature=bad)), "podpis")


# ------------------------------------------------------------------ AI-izmy


@pytest.mark.parametrize("label", [label for _, label in AI_PHRASES])
def test_each_ai_phrase_is_caught(label: str):
    text = message(tail=f"To {label} dla Waszego procesu.")
    assert has(problems_of(text), "AI-izm")


def test_em_dash_is_reported():
    text = message(middle=f"Wystawianie dokumentu mam tu — {LINK}. To ten sam wzorzec.")
    assert has(problems_of(text), "długi myślnik")


def test_paragraph_longer_than_three_sentences_is_reported():
    long_paragraph = "Pierwszy krok to mapowanie. Drugi to test na zamówieniu. Trzeci to lista kontrolna. Czwarty to wdrożenie."
    assert has(problems_of(message(tail=long_paragraph)), "akapit dłuższy niż 3 zdania")


def test_too_many_sentences_in_the_preview_are_reported():
    opening = "Koszt przesyłki nie wchodzi do zamówienia. Sfera zapisuje zero. Widać to od razu. Poprawka jest mała. Zaczynamy od testu."
    text = message(intro=opening, middle="Mapowanie mam tutaj, w tym samym obiegu dokumentów.")
    assert has(problems_of(text), "pierwszych 200 znakach")


def test_more_than_two_questions_are_reported():
    text = message(tail="Czy macie kod źródłowy? A kto wystawia zamówienia? Kiedy startujemy?")
    assert has(problems_of(text), "więcej niż 2 pytania")


# ------------------------------------------------------------------ lista punktowana


def test_clean_bullets_are_allowed():
    bullets = "- mapowanie kosztu przesyłki z zamówienia\n- test na realnym zamówieniu WooCommerce\n- lista tego, co sprawdziliśmy"
    assert problems_of(message(bullets=bullets), context=PORTFOLIO_CONTEXT) == ()


@pytest.mark.parametrize(
    ("bullets", "needle"),
    [
        ("- **Krok 1:** mapowanie\n- **Krok 2:** test", "pogrubioną"),
        ("- 1. mapowanie kosztu\n- 2. test na zamówieniu", "pogrubioną"),
        ("- mapowanie: koszt przesyłki z zamówienia\n- test: na realnym zamówieniu", "etykieta: zdanie"),
        ("- mapowanie kosztu przesyłki.\n- test na zamówieniu", "kończy się kropką"),
        ("- mapowanie kosztu przesyłki:\n- test na zamówieniu", "kończy się kropką"),
        ("- Mapowanie kosztu przesyłki\n- test na zamówieniu", "wielką literą"),
        (
            "- mapowanie kosztu przesyłki z zamówienia WooCommerce oraz kontroli duplikatów\n- test",
            "dłuższy niż",
        ),
        (
            "- mapowanie kosztu zamowienia\n- kontrola danych zamowienia\n- testowanie obiegu faktury",
            "identycznej długości",
        ),
    ],
)
def test_machine_shaped_bullets_are_reported(bullets: str, needle: str):
    assert has(problems_of(message(bullets=bullets)), needle)


def test_bullet_length_limit_is_documented():
    assert MAX_BULLET_CHARS == 60


# ------------------------------------------------------------------ pomocnicze


def test_strip_price_marker_removes_the_line_and_keeps_one_blank_line():
    stripped = strip_price_marker(message())
    assert PRICE_MARKER not in stripped
    assert "\n\n\n" not in stripped
    assert stripped.endswith(SIGNATURE[-1])


def test_summarise_reports_ok_for_a_clean_list():
    assert summarise(()) == "OK"
    assert summarise(("brak ceny", "za długa (1500 znaków, limit 1100)")) == (
        "brak ceny; za długa (1500 znaków, limit 1100)"
    )
