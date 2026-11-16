"""Wycena zlecenia: research, jedna liczba, wstawienie kwoty do draftu."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from freelance_pipeline.pricing import (
    MIN_HOURLY_PLN,
    InvalidPriceResponse,
    OpenRouterPricer,
    PriceBrief,
    PricedDraft,
    PriceError,
    apply_price,
    build_price_prompt,
    price_best_effort,
    price_drafts,
    price_paragraph,
    price_problems,
)
from freelance_pipeline.proposal import OfferInput, ProposalDraft
from freelance_pipeline.proposal_check import PRICE_MARKER, check_draft

LINK = "https://jnxautomation.pl/projekty/snajper-ofert"
PORTFOLIO = f"PORTFOLIO\n- Snajper Ofert — link: {LINK}\n- strona główna: https://jnxautomation.pl\n"
OFFER = OfferInput(
    key="useme:145473",
    title="Zlecę przygotowanie leadów na zakup bazy danych b2b",
    source="useme",
    url="https://useme.com/pl/jobs/x,145473/",
    author="Łukasz",
    description="Klient chce listę firm B2B z LinkedIn, z filtrami i eksportem.",
    bucket="TOP",
    score=100,
)


def brief(**overrides: object) -> PriceBrief:
    data: dict = {
        "key": "useme:145473",
        "amount": 3000.0,
        "currency": "PLN",
        "days": 5,
        "hourly_min": 90.0,
        "hourly_max": 150.0,
        "includes": ("scraper z filtrami", "deduplikacja", "eksport do CSV"),
        "sources": ("https://example.com/stawki-freelancer-2026",),
        "rationale": "Zakres 1 źródło danych + eksport; stawki krajowe 90-150 zł/h.",
    }
    data.update(overrides)
    return PriceBrief(**data)  # type: ignore[arg-type]


DRAFT_TEXT = (
    "Cześć,\n\n"
    "potrzebujesz listy firm B2B, do których da się zadzwonić, a nie tysiąca przypadkowych rekordów. "
    "Buduję automatyzacje, które zbierają i czyszczą dane bez ręcznej pracy.\n\n"
    "Scraper przechodzi kolejne strony wyników, odsiewa duplikaty i zapisuje rekord tam, gdzie ma "
    f"trafić, z kontrolą tempa zapytań, żeby konto zostało całe. Podobny mechanizm zbiera i triażuje "
    f"dane w Snajperze Ofert: {LINK}\n\n"
    "- pobieranie z LinkedIn i rejestrów\n"
    "- deduplikacja i czyszczenie\n"
    "- eksport do CSV\n\n"
    f"{PRICE_MARKER}\n\n"
    "Ile rekordów ma wejść do pierwszej partii?\n\n"
    "Jan Noga\njnxautomation.pl\n+48 600 000 000"
)


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)


class _Post:
    def __init__(self, *contents: str, citations: tuple[str, ...] = ()) -> None:
        self.contents = list(contents)
        self.citations = citations
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        content = self.contents.pop(0) if self.contents else ""
        annotations = [
            {"type": "url_citation", "url_citation": {"url": url_}} for url_ in self.citations
        ]
        return _Response({"choices": [{"message": {"content": content, "annotations": annotations}}]})

    def prompt_of(self, index: int = 0) -> str:
        return self.calls[index]["json"]["messages"][1]["content"]  # type: ignore[index]


def price_payload(**overrides: object) -> str:
    item: dict = {
        "offer_key": 0,
        "kwota": 3000,
        "waluta": "PLN",
        "dni": 5,
        "stawka_min": 90,
        "stawka_max": 150,
        "co_w_cenie": ["scraper z filtrami", "deduplikacja", "eksport do CSV"],
        "zrodla": ["https://example.com/stawki"],
        "uzasadnienie": "Zakres jedno źródło + eksport, stawki krajowe 90-150 zł/h.",
    }
    item.update(overrides)
    return json.dumps([item])


def pricer(post: _Post, **kwargs: object) -> OpenRouterPricer:
    options = {"api_key": "test-key", "session": post}
    options.update(kwargs)
    return OpenRouterPricer(**options)


# ------------------------------------------------------------------ wywołanie


def test_price_call_uses_web_search_and_strict_json():
    post = _Post(price_payload())
    briefs = pricer(post).price([OFFER])
    assert briefs[0].amount == 3000.0
    payload = post.calls[0]["json"]  # type: ignore[index]
    assert payload["plugins"] == [{"id": "web", "max_results": 4}]
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["temperature"] == 0
    assert post.calls[0]["headers"]["Authorization"] == "Bearer test-key"  # type: ignore[index]


def test_prompt_contains_the_offer_and_forbids_guessing():
    post = _Post(price_payload())
    pricer(post).price([OFFER])
    prompt = post.prompt_of()
    assert "Zlecę przygotowanie leadów" in prompt
    assert "90 zł/h" in prompt
    assert "nigdy widełki" in prompt
    assert "budżet" in prompt
    assert PRICE_MARKER not in prompt


def test_search_citations_are_attached_to_the_price():
    post = _Post(price_payload(zrodla=[]), citations=("https://realny-cennik.pl/x",))
    briefs = pricer(post).price([OFFER])
    assert briefs[0].sources == ("https://realny-cennik.pl/x",)


def test_empty_offer_list_needs_no_call():
    post = _Post()
    assert pricer(post).price([]) == []
    assert post.calls == []


def test_missing_api_key_is_reported(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(PriceError, match="OPENROUTER_API_KEY"):
        OpenRouterPricer(session=_Post()).price([OFFER])


# ------------------------------------------------------------------ kontrakt wyceny


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"nie": "lista"}),
        json.dumps([{"offer_key": 0}]),
        json.dumps([]),
        price_payload(waluta="CHF"),
        price_payload(dni=0),
        price_payload(kwota=0),
        price_payload(kwota="tysiąc"),
        price_payload(co_w_cenie=[]),
        price_payload(co_w_cenie=["ok", 5]),
        price_payload(zrodla="https://example.com"),
        price_payload(uzasadnienie="   "),
        "to nie jest json",
    ],
)
def test_broken_price_payloads_are_rejected(payload: str):
    with pytest.raises(InvalidPriceResponse):
        pricer(_Post(payload)).price([OFFER])


def test_duplicate_and_unknown_keys_are_rejected():
    two = json.dumps(
        [
            {
                "offer_key": 0,
                "kwota": 1000,
                "waluta": "PLN",
                "dni": 3,
                "stawka_min": 90,
                "stawka_max": 140,
                "co_w_cenie": ["a"],
                "zrodla": [],
                "uzasadnienie": "x",
            },
            {
                "offer_key": 0,
                "kwota": 1000,
                "waluta": "PLN",
                "dni": 3,
                "stawka_min": 90,
                "stawka_max": 140,
                "co_w_cenie": ["a"],
                "zrodla": [],
                "uzasadnienie": "x",
            },
        ]
    )
    with pytest.raises(InvalidPriceResponse, match="duplicates"):
        pricer(_Post(two)).price([OFFER, OfferInput(key="useme:2", title="Druga", source="useme")])


def test_http_error_is_reported():
    class _Failing(_Post):
        def post(self, url: str, **kwargs: object) -> _Response:
            return _Response("{}", status_code=502)

    with pytest.raises(PriceError, match="HTTP 502"):
        pricer(_Failing()).price([OFFER])


def test_brief_validation():
    with pytest.raises(ValueError):
        brief(days=0)
    with pytest.raises(ValueError):
        brief(includes=())
    with pytest.raises(ValueError):
        brief(rationale="  ")
    with pytest.raises(ValueError):
        brief(amount=-5)


def test_hourly_equivalent_flags_a_price_below_the_floor():
    cheap = brief(amount=1500, days=5)  # 1500 / 30 h = 50 zł/h
    rich = brief(amount=3000, days=5)
    assert cheap.hourly_equivalent < MIN_HOURLY_PLN
    assert rich.hourly_equivalent >= MIN_HOURLY_PLN


def test_build_price_prompt_keys_every_offer_by_position():
    prompt = build_price_prompt([OFFER, OFFER])
    assert '"offer_key": 0' in prompt
    assert '"offer_key": 1' in prompt


# ------------------------------------------------------------------ wstawienie kwoty


def test_price_paragraph_starts_with_the_amount_and_time():
    paragraph = price_paragraph(brief())
    assert paragraph.startswith("3 000 zł, ok. 5 dni roboczych, w cenie: ")
    assert paragraph.endswith("Wycena i czas są orientacyjne, dopasujemy je po szczegółach.")


def test_apply_price_replaces_the_marker_once():
    priced = apply_price(DRAFT_TEXT, brief())
    assert PRICE_MARKER not in priced
    assert priced.count("3 000 zł") == 1


def test_apply_price_appends_when_there_is_no_marker():
    priced = apply_price("Cześć,\n\ntreść bez znacznika", brief())
    assert priced.startswith("Cześć,\n\ntreść bez znacznika")
    assert "3 000 zł, ok. 5 dni roboczych" in priced


def test_priced_message_passes_the_checker():
    priced = apply_price(DRAFT_TEXT, brief())
    assert check_draft(priced, context=PORTFOLIO, priced=True) == ()
    assert price_problems(priced, brief(), context=PORTFOLIO) == ()


def test_apply_price_drops_a_disclaimer_the_model_repeated():
    # Model dokleja zdanie o orientacyjności do pytania domykającego, a akapit z ceną już je
    # niesie — w gotowej wiadomości ma zostać dokładnie raz.
    draft = DRAFT_TEXT.replace(
        "Ile rekordów ma wejść do pierwszej partii?",
        "Wycena i czas są orientacyjne, dopasujemy je po szczegółach. "
        "Ile rekordów ma wejść do pierwszej partii?",
    )
    priced = apply_price(draft, brief())
    assert priced.count("Wycena i czas są orientacyjne, dopasujemy je po szczegółach.") == 1
    assert "Ile rekordów ma wejść do pierwszej partii?" in priced
    assert price_problems(priced, brief(), context=PORTFOLIO) == ()


def test_double_disclaimer_is_reported_by_the_checker():
    priced = apply_price(DRAFT_TEXT, brief())
    doubled = priced.replace(
        "Ile rekordów ma wejść do pierwszej partii?",
        "Wycena i czas są orientacyjne, dopasujemy je po szczegółach. "
        "Ile rekordów ma wejść do pierwszej partii?",
    )
    problems = price_problems(doubled, brief(), context=PORTFOLIO)
    assert any("orientacyjności" in problem for problem in problems)


def test_price_below_the_hourly_floor_is_reported():
    cheap = brief(amount=900, days=5)
    problems = price_problems(apply_price(DRAFT_TEXT, cheap), cheap)
    assert any("90 zł/h" in problem for problem in problems)


def test_price_without_sources_is_reported():
    no_source = brief(sources=())
    problems = price_problems(apply_price(DRAFT_TEXT, no_source), no_source)
    assert any("bez źródła" in problem for problem in problems)


def test_price_drafts_pairs_briefs_with_drafts_and_flags_missing():
    draft = ProposalDraft(key="useme:145473", text=DRAFT_TEXT)
    other = ProposalDraft(key="useme:999", text=DRAFT_TEXT)
    priced = price_drafts([draft, other], [brief()], contexts=[PORTFOLIO, PORTFOLIO])
    assert isinstance(priced[0], PricedDraft)
    assert priced[0].ok
    assert priced[0].text.count("3 000 zł") == 1
    assert not priced[1].ok
    assert "brak wyceny" in priced[1].note
    assert priced[1].text == DRAFT_TEXT  # bez kwoty, znacznik zostaje


def test_price_drafts_keeps_problems_visible():
    # Draft, który sam nie przechodzi kontroli (za krótki) — po wycenie problem ma zostać widoczny.
    short = "Cześć,\n\nZrobię to.\n\n" + PRICE_MARKER + "\n\nJan Noga\njnxautomation.pl\n+48 600 000 000"
    priced = price_drafts([ProposalDraft(key="useme:145473", text=short)], [brief()], contexts=[PORTFOLIO])
    assert not priced[0].ok
    assert any("za krótka" in problem for problem in priced[0].problems)


# ------------------------------------------------------------------ próg 90 zł/h


def test_price_below_the_floor_triggers_one_correction():
    cheap = price_payload(kwota=900, dni=4)          # 900 / 24 h = 38 zł/h
    corrected = price_payload(kwota=2400, dni=4)     # 2400 / 24 h = 100 zł/h
    post = _Post(cheap, corrected)
    briefs = pricer(post).price([OFFER])
    assert briefs[0].amount == 2400.0
    assert briefs[0].hourly_equivalent >= MIN_HOURLY_PLN
    assert len(post.calls) == 2
    assert "POPRAW WYCENĘ" in post.prompt_of(1)
    assert "90 zł/h" in post.prompt_of(1)


def test_correction_that_does_not_improve_keeps_the_original():
    cheap = price_payload(kwota=900, dni=4)
    worse = price_payload(kwota=800, dni=4)
    post = _Post(cheap, worse)
    briefs = pricer(post).price([OFFER])
    assert briefs[0].amount == 900.0  # raportujemy to, co przyszło; problem widzi price_problems


def test_a_failed_correction_request_keeps_the_original():
    cheap = price_payload(kwota=900, dni=4)
    post = _Post(cheap, "nie json")
    briefs = pricer(post).price([OFFER])
    assert briefs[0].amount == 900.0


def test_floor_enforcement_can_be_switched_off():
    post = _Post(price_payload(kwota=900, dni=4))
    briefs = pricer(post, enforce_hourly_floor=False).price([OFFER])
    assert briefs[0].amount == 900.0
    assert len(post.calls) == 1


def test_prices_above_the_floor_are_not_retried():
    post = _Post(price_payload())
    pricer(post).price([OFFER])
    assert len(post.calls) == 1


def test_currency_symbols_and_lowercase_items():
    paragraph = price_paragraph(brief(currency="EUR", amount=900, days=1, includes=("Scraper", "Eksport")))
    assert paragraph.startswith("900 EUR, ok. 1 dzień roboczy, w cenie: scraper, eksport.")
    assert price_paragraph(brief()) == (
        "3 000 zł, ok. 5 dni roboczych, w cenie: scraper z filtrami, deduplikacja, eksport do CSV. "
        "Wycena i czas są orientacyjne, dopasujemy je po szczegółach."
    )


# ------------------------------------------------------------------ best effort (awaria 2026-10-09)


class _StubPricer:
    """Wycena bez sieci: pary „klucz → kwota”, z możliwością awarii partii."""

    def __init__(
        self,
        amounts: dict[str, float],
        *,
        batch_error: str = "",
        partial: bool = False,
    ) -> None:
        self.amounts = amounts
        self.batch_error = batch_error
        self.partial = partial
        self.calls: list[list[str]] = []

    def price(self, offers: Sequence[OfferInput]) -> list[PriceBrief]:
        keys = [offer.key for offer in offers]
        self.calls.append(keys)
        if len(keys) > 1 and self.batch_error:
            raise InvalidPriceResponse("kwota must be positive, got 0.0")
        out = []
        for key in keys:
            amount = self.amounts.get(key, 0.0)
            if amount <= 0:
                raise InvalidPriceResponse("kwota must be positive, got 0.0")
            out.append(brief(key=key, amount=amount))
        if self.partial and len(out) > 1:
            return out[:1]
        return out


SECOND = OfferInput(
    key="useme:145680",
    title="Wykonanie konfiguratora produktu",
    source="useme",
    url="https://useme.com/pl/jobs/x,145680/",
    author="publictoilets",
    description="Konfigurator z automatycznym wyliczaniem ceny.",
    bucket="TOP",
    score=100,
)


def test_healthy_batch_is_priced_in_one_call():
    stub = _StubPricer({OFFER.key: 3000.0, SECOND.key: 1800.0})
    briefs, failures = price_best_effort([OFFER, SECOND], pricer=stub)  # type: ignore[arg-type]
    assert failures == {}
    assert [b.key for b in briefs] == [OFFER.key, SECOND.key]
    assert stub.calls == [[OFFER.key, SECOND.key]]


def test_broken_batch_falls_back_to_single_offers():
    stub = _StubPricer(
        {OFFER.key: 3000.0, SECOND.key: 1800.0}, batch_error="kwota must be positive, got 0.0"
    )
    briefs, failures = price_best_effort([OFFER, SECOND], pricer=stub)  # type: ignore[arg-type]
    assert failures == {}
    assert [b.key for b in briefs] == [OFFER.key, SECOND.key]
    assert stub.calls == [[OFFER.key, SECOND.key], [OFFER.key], [SECOND.key]]


def test_offer_that_stays_unpriced_is_reported_not_invented():
    stub = _StubPricer(
        {OFFER.key: 3000.0}, batch_error="kwota must be positive, got 0.0"
    )
    briefs, failures = price_best_effort([OFFER, SECOND], pricer=stub)  # type: ignore[arg-type]
    assert [b.key for b in briefs] == [OFFER.key]
    assert list(failures) == [SECOND.key]
    assert "positive" in failures[SECOND.key]


def test_partial_batch_is_completed_by_single_offer_retry():
    stub = _StubPricer({OFFER.key: 3000.0, SECOND.key: 1800.0}, partial=True)
    briefs, failures = price_best_effort([OFFER, SECOND], pricer=stub)  # type: ignore[arg-type]
    assert failures == {}
    assert [b.key for b in briefs] == [OFFER.key, SECOND.key]
    assert stub.calls == [[OFFER.key, SECOND.key], [OFFER.key], [SECOND.key]]


def test_empty_batch_costs_nothing():
    stub = _StubPricer({OFFER.key: 3000.0})
    assert price_best_effort([], pricer=stub) == ([], {})  # type: ignore[arg-type]
    assert stub.calls == []
