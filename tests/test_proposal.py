"""Generator draftów: prompt, walidacja, jedna poprawka.

Korpus `ODPOWIEDZI-*.md` / `wiadomosci-*.md` **nie** wchodzi już do promptu (korekta Jana,
2026-10-07: to drafty napisane przez ten system, nie przez Jana, więc naśladowanie ich
znaczyłoby naśladowanie słabszego poprzednika). Prompt bierze kontrakt ze skilla i szkielet
układu — i tego pilnują testy niżej.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freelance_pipeline.models import JobLead
from freelance_pipeline.proposal import (
    DEFAULT_MODEL,
    ApprovedExample,
    InvalidProposalResponse,
    OfferInput,
    ProposalError,
    ProposalWriter,
    draft_offers,
    load_approved_examples,
    load_contract,
    mask_price,
    pick_approved_examples,
    record_approved_example,
)
from freelance_pipeline.proposal_check import (
    MAX_BULLET_CHARS,
    PRICE_MARKER,
    RULES_VERSION,
)

LINK = "https://jnxautomation.pl/projekty/ksef-document-agent"
PORTFOLIO = f"PORTFOLIO JANA\n- KSeF Document Agent — link: {LINK}\n- strona główna: https://jnxautomation.pl\n"
CONTRACT = "=== KONTRAKT TESTOWY ===\n1. Długość 700-1100 znaków.\n2. Cena liczona ręcznie, w drafcie tylko znacznik.\n"

OFFER = OfferInput(
    key="useme:145473",
    title="Subiekt i WooCommerce, koszt przesyłki",
    source="useme",
    url="https://useme.com/pl/jobs/x,145473/",
    author="Marek",
    description="Koszt przesyłki nie wchodzi do zamówienia wystawianego przez Sferę.",
    bucket="TOP",
    score=95,
)

_INTRO = (
    "koszt przesyłki nie wchodzi do zamówienia, bo Sfera przyjmuje dokument pozycja po pozycji "
    "i to pole musi być wypełnione jawnie, inaczej Subiekt zapisuje zero. Skoro macie kod źródłowy, "
    "poprawka jest lokalna: dokładam osobny element dokumentu na koszt przesyłki z zamówienia "
    "WooCommerce, z kontrolą, czy Subiekt nie doliczy go drugi raz. Zgłoszenie bez tego kosztu "
    "przechodzi dziś bez błędu, tylko kwota na dokumencie jest zaniżona."
)
_MIDDLE = (
    "Wystawianie pozycji dokumentu z mapowaniem danych mam tutaj: "
    f"{LINK}. Poprawka obejmuje też drugie wdrożenie, więc zostaje jedna wspólna ścieżka."
)
_QUESTION = "Czy przy zamówieniu bez kosztu przesyłki ma lecieć pozycja zerowa?"
_SIGNATURE = "Jan Noga\njnxautomation.pl\n+48 600 000 000"


def clean_message(**overrides: object) -> str:
    """Wiadomość przechodząca walidator; testy nadpisują jeden element."""
    intro = overrides.pop("intro", _INTRO)
    middle = overrides.pop("middle", _MIDDLE)
    price = overrides.pop("price", PRICE_MARKER)
    blocks = ["Cześć,", str(intro), str(middle), str(price), _QUESTION, _SIGNATURE]
    return "\n\n".join(blocks)


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)


class _Post:
    """Minimalny zamiennik requests.Session, który oddaje przygotowane odpowiedzi."""

    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        content = self.contents.pop(0) if self.contents else ""
        return _Response({"choices": [{"message": {"content": content}}]})

    def prompt_of(self, index: int = 0) -> str:
        messages = self.calls[index]["json"]["messages"]  # type: ignore[index]
        return messages[1]["content"]


def drafts_payload(*texts: str) -> str:
    return json.dumps([{"offer_key": index, "message": text} for index, text in enumerate(texts)])


def writer(post: _Post, **kwargs: object) -> ProposalWriter:
    options = {
        "api_key": "test-key",
        "session": post,
        "contract": CONTRACT,
        "portfolio_block": PORTFOLIO,
        "approved_examples": (),  # testy nie czytają prawdziwego MOJ-STYL.md
    }
    options.update(kwargs)
    return ProposalWriter(**options)  # type: ignore[arg-type]


# ------------------------------------------------------------------ draftowanie


def test_writes_one_ok_draft_per_offer():
    post = _Post(drafts_payload(clean_message(), clean_message()))
    offers = [OFFER, OfferInput(key="facebook_groups:99", title="Chatbot dla gabinetu", source="facebook_groups")]
    result = writer(post).write(offers)
    assert [draft.key for draft in result] == ["useme:145473", "facebook_groups:99"]
    assert all(draft.ok for draft in result)
    assert all(draft.rules_version == RULES_VERSION for draft in result)
    assert all(draft.model == DEFAULT_MODEL for draft in result)
    assert all(draft.attempts == 1 for draft in result)
    assert result[0].note == "OK"


def test_drafts_are_split_into_batches():
    post = _Post(
        drafts_payload(clean_message(), clean_message()),
        drafts_payload(clean_message(), clean_message()),
        drafts_payload(clean_message()),
    )
    offers = [OfferInput(key=f"useme:{index}", title="Zlecenie", source="useme") for index in range(5)]
    result = writer(post, batch_size=2).write(offers)
    assert len(result) == 5
    assert len(post.calls) == 3
    assert [draft.key for draft in result] == [f"useme:{index}" for index in range(5)]


def test_prompt_carries_contract_marker_portfolio_and_offer():
    post = _Post(drafts_payload(clean_message()))
    writer(post).write([OFFER])
    prompt = post.prompt_of()
    assert CONTRACT.strip() in prompt
    assert LINK in prompt
    assert "Subiekt i WooCommerce, koszt przesyłki" in prompt
    payload = post.calls[0]["json"]  # type: ignore[index]
    assert payload["model"] == DEFAULT_MODEL
    assert payload["temperature"] == 0
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert post.calls[0]["headers"]["Authorization"] == "Bearer test-key"  # type: ignore[index]


def test_prompt_shows_the_layout_skeleton_with_the_marker():
    post = _Post(drafts_payload(clean_message()))
    writer(post).write([OFFER])
    prompt = post.prompt_of()
    assert "SZABLON UKŁADU" in prompt
    skeleton = prompt[prompt.index("SZABLON UKŁADU") : prompt.index("WYCENA (decyzja Jana")]
    assert PRICE_MARKER in skeleton
    assert "Jan Noga" in skeleton


def test_no_historical_drafts_go_into_the_prompt():
    """Korekta Jana 2026-10-07: ODPOWIEDZI-*.md to output tego systemu, nie wzór stylu."""
    post = _Post(drafts_payload(clean_message()))
    writer(post).write([OFFER])
    prompt = post.prompt_of()
    assert "PRZYKŁADY GŁOSU" not in prompt
    assert "ODPOWIEDZI-" not in prompt
    assert "few-shot" not in prompt.lower()


def test_price_rule_comes_after_the_layout_skeleton():
    post = _Post(drafts_payload(clean_message()))
    writer(post).write([OFFER])
    prompt = post.prompt_of()
    assert prompt.index("WYCENA (decyzja Jana") > prompt.index("SZABLON UKŁADU")
    assert prompt.index("WYCENA (decyzja Jana") < prompt.index("Odpowiedz wyłącznie tablicą JSON")


def test_empty_offer_list_needs_no_request():
    post = _Post()
    assert writer(post).write([]) == []
    assert post.calls == []


def test_missing_api_key_is_reported(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ProposalError, match="OPENROUTER_API_KEY"):
        ProposalWriter(session=_Post(), contract=CONTRACT, portfolio_block=PORTFOLIO).write([OFFER])


def test_offer_inputs_are_type_checked():
    with pytest.raises(TypeError):
        writer(_Post()).write(["useme:1"])  # type: ignore[list-item]


# ------------------------------------------------------------------ błędy modelu


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"not": "a list"}),
        json.dumps([{"offer_key": 0, "message": clean_message()}, {"offer_key": 0, "message": clean_message()}]),
        json.dumps([{"offer_key": 5, "message": clean_message()}]),
        json.dumps([{"offer_key": 0, "message": "   "}]),
        json.dumps([{"offer_key": "0", "message": clean_message()}]),
        "not json at all",
    ],
)
def test_invalid_payloads_are_rejected_after_every_attempt(payload: str):
    post = _Post(payload, payload)
    with pytest.raises(InvalidProposalResponse):
        writer(post).write([OFFER])
    assert len(post.calls) == 2


def test_markdown_fence_is_tolerated():
    post = _Post(f"```json\n{drafts_payload(clean_message())}\n```")
    assert writer(post).write([OFFER])[0].ok


def test_http_error_is_reported():
    class _Failing(_Post):
        def post(self, url: str, **kwargs: object) -> _Response:
            return _Response("{}", status_code=500)

    with pytest.raises(ProposalError, match="HTTP 500"):
        writer(_Failing()).write([OFFER])


def test_transport_error_is_reported():
    class _Broken(_Post):
        def post(self, url: str, **kwargs: object) -> _Response:
            raise OSError("connection reset")

    with pytest.raises(ProposalError, match="request failed"):
        writer(_Broken()).write([OFFER])


def test_empty_envelope_is_reported():
    class _Empty(_Post):
        def post(self, url: str, **kwargs: object) -> _Response:
            return _Response("{}", status_code=200)

    with pytest.raises(InvalidProposalResponse):
        writer(_Empty()).write([OFFER])


# ------------------------------------------------------------------ jedna poprawka


def test_a_problematic_draft_is_repaired_once():
    missing_marker = clean_message(price="")  # brak znacznika wyceny
    post = _Post(drafts_payload(missing_marker), drafts_payload(clean_message()))
    result = writer(post).write([OFFER])
    assert result[0].ok
    assert result[0].attempts == 2
    assert len(post.calls) == 2
    repair_prompt = post.prompt_of(1)
    assert "DO POPRAWY" in repair_prompt
    assert "brak znacznika wyceny" in repair_prompt
    assert '"poprzednia_wiadomość"' in repair_prompt
    assert "koszt przesyłki nie wchodzi" in repair_prompt
    assert PRICE_MARKER in repair_prompt  # instrukcja wstawienia znacznika


def test_the_original_is_kept_when_the_fix_is_worse():
    missing_marker = clean_message(price="")
    still_broken = clean_message(price="", intro="Krótko.", middle="Zrobię to.")
    post = _Post(drafts_payload(missing_marker), drafts_payload(still_broken))
    result = writer(post).write([OFFER])
    assert not result[0].ok
    assert result[0].attempts == 2  # druga próba była, ale nic nie poprawiła
    assert result[0].text == missing_marker


def test_a_failed_repair_returns_the_first_draft_with_its_problems():
    post = _Post(drafts_payload(clean_message(price="")), "broken")
    result = writer(post).write([OFFER])
    assert result[0].attempts == 1
    assert any("znacznika wyceny" in problem for problem in result[0].problems)


def test_max_attempts_one_skips_the_repair_round():
    post = _Post(drafts_payload(clean_message(price="")))
    result = writer(post, max_attempts=1).write([OFFER])
    assert not result[0].ok
    assert len(post.calls) == 1


def test_only_the_failing_offers_are_repaired():
    good = OfferInput(key="useme:1", title="Dobra integracja", source="useme")
    bad = OfferInput(key="useme:2", title="Zła integracja", source="useme")
    post = _Post(
        drafts_payload(clean_message(), clean_message(price="")),
        drafts_payload(clean_message()),
    )
    result = writer(post).write([good, bad])
    assert all(draft.ok for draft in result)
    assert [draft.attempts for draft in result] == [1, 2]
    repair_prompt = post.prompt_of(1)
    assert "Zła integracja" in repair_prompt
    assert "Dobra integracja" not in repair_prompt


def test_draft_offers_uses_a_given_writer(monkeypatch):
    post = _Post(drafts_payload(clean_message()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    result = draft_offers([OFFER], writer=writer(post))
    assert result[0].ok


# ------------------------------------------------------------------ kontrakt i model


def test_contract_is_read_from_the_skill_markers():
    contract = load_contract()
    assert "kontrakt wyjścia" in contract.lower()
    assert "700–1100" in contract


def test_missing_contract_file_is_reported(tmp_path: Path):
    with pytest.raises(ProposalError, match="skill file"):
        load_contract(tmp_path / "nie-ma.md")


def test_file_without_markers_is_reported(tmp_path: Path):
    path = tmp_path / "SKILL.md"
    path.write_text("# Skill bez znaczników\n", encoding="utf-8")
    with pytest.raises(ProposalError, match="kontrakt-start"):
        load_contract(path)


def test_default_writer_uses_the_chat_model():
    assert DEFAULT_MODEL == "deepseek/deepseek-v4.1-flash"
    assert ProposalWriter().model == DEFAULT_MODEL


def test_bullet_limit_is_shared_with_the_checker():
    assert MAX_BULLET_CHARS == 60


def test_offer_input_from_lead_and_validation():
    lead = JobLead(
        source="useme",
        source_id="145473",
        url="https://useme.com/pl/jobs/x,145473/",
        title="Subiekt i WooCommerce",
        company_or_author="Marek",
        description="Koszt przesyłki.",
        budget=1800.0,
        currency="PLN",
    )
    offer = OfferInput.from_lead(lead, bucket="TOP", score=95)
    assert offer.key == "useme:145473"
    assert offer.author == "Marek"
    assert offer.budget == 1800.0
    assert offer.text.startswith("Subiekt i WooCommerce")
    with pytest.raises(ValueError):
        OfferInput(key="  ", title="x", source="useme")
    with pytest.raises(TypeError):
        OfferInput(key="useme:1", title="x", source="useme", score=95.5)  # type: ignore[arg-type]


# ------------------------------------------------------------------ pętla stylu (MOJ-STYL.md)


STYLE_SECTION = """## 1. Subiekt i WooCommerce
klucz: useme:145473

```text
{message}
```
"""


def _style_file(tmp_path: Path, *messages: str) -> Path:
    path = tmp_path / "MOJ-STYL.md"
    path.write_text(STYLE_SECTION.format(message=messages[0]), encoding="utf-8")
    for index, message in enumerate(messages[1:], start=2):
        path.write_text(
            path.read_text(encoding="utf-8")
            + f"\n## {index}. Zlecenie {index}\n\n```text\n{message}\n```\n",
            encoding="utf-8",
        )
    return path


def test_missing_style_file_means_no_examples(tmp_path: Path):
    assert load_approved_examples(tmp_path / "nie-ma.md") == ()


def test_style_file_is_parsed_with_key_and_deduplicated(tmp_path: Path):
    message = clean_message()
    path = _style_file(tmp_path, message, message)
    examples = load_approved_examples(path)
    assert len(examples) == 1
    assert examples[0].title == "Subiekt i WooCommerce"
    assert examples[0].key == "useme:145473"


def test_tiny_blocks_are_ignored(tmp_path: Path):
    path = tmp_path / "MOJ-STYL.md"
    path.write_text("## 1. Krótko\n\n```text\nza krótkie\n```\n", encoding="utf-8")
    assert load_approved_examples(path) == ()


def test_record_approved_example_creates_the_file_and_numbers_sections(tmp_path: Path):
    path = tmp_path / "MOJ-STYL.md"
    first = record_approved_example("Subiekt", clean_message(), key="useme:1", path=path)
    assert first.key == "useme:1"
    assert path.read_text(encoding="utf-8").startswith("# Mój styl")
    record_approved_example("Chatbot", clean_message(intro="Inna treść " + _INTRO), path=path)
    text = path.read_text(encoding="utf-8")
    assert "## 1. Subiekt" in text
    assert "## 2. Chatbot" in text
    assert len(load_approved_examples(path)) == 2


def test_record_approved_example_refuses_a_duplicate(tmp_path: Path):
    path = tmp_path / "MOJ-STYL.md"
    record_approved_example("Subiekt", clean_message(), path=path)
    with pytest.raises(ProposalError, match="już w pliku stylu"):
        record_approved_example("Subiekt", clean_message(), path=path)


def test_approved_example_rejects_empty_fields():
    with pytest.raises(ValueError):
        ApprovedExample(title="  ", message="treść")
    with pytest.raises(ValueError):
        ApprovedExample(title="Tytuł", message="   ")


def test_pick_approved_examples_prefers_overlap_and_is_deterministic():
    subiekt = ApprovedExample("Subiekt i WooCommerce", "koszt przesyłki w Subiekcie, Sfera, dokument", "useme:1")
    chatbot = ApprovedExample("Chatbot gabinetu", "pacjentom odpowiada bot o cenniku i godzinach", "useme:2")
    text = "koszt przesyłki w Subiekcie i Sfera"
    assert [item.title for item in pick_approved_examples((chatbot, subiekt), text, limit=1)] == [
        "Subiekt i WooCommerce"
    ]
    assert pick_approved_examples((chatbot, subiekt), text, limit=2) == pick_approved_examples(
        (chatbot, subiekt), text, limit=2
    )
    with pytest.raises(ValueError):
        pick_approved_examples((), text, limit=-1)


def test_mask_price_replaces_the_amount_block_with_the_marker():
    priced = clean_message(price="1 800 zł, ok. 5 dni za poprawkę na obu platformach.")
    masked = mask_price(priced)
    assert "1 800 zł" not in masked
    assert masked.count(PRICE_MARKER) == 1


def test_approved_examples_go_into_the_prompt_masked():
    priced = clean_message(price="1 800 zł, ok. 5 dni za poprawkę na obu platformach.")
    example = ApprovedExample("Subiekt i WooCommerce", priced, "useme:145473")
    post = _Post(drafts_payload(clean_message()))
    writer(post, approved_examples=(example,)).write([OFFER])
    prompt = post.prompt_of()
    assert "PO SWOJEJ REDAKCJI" in prompt
    assert "1 800 zł" not in prompt
    assert PRICE_MARKER in prompt


def test_no_style_file_means_no_example_block(tmp_path: Path):
    post = _Post(drafts_payload(clean_message()))
    ProposalWriter(
        api_key="test-key",
        session=post,
        contract=CONTRACT,
        portfolio_block=PORTFOLIO,
        style_path=tmp_path / "pusto.md",
    ).write([OFFER])
    assert "PO SWOJEJ REDAKCJI" not in post.prompt_of()


def test_negative_approved_limit_is_rejected():
    with pytest.raises(ValueError):
        ProposalWriter(approved_example_limit=-1)
