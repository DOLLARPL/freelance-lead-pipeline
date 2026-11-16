"""Draft the reply to each new offer, in Jan's own voice, without the price.

Why this exists (``PLAN-DOPRACOWANIE-2026-10.md`` §3.7): the rolling list has to
arrive with a ready message next to every offer, so Jan never opens a listing "empty".
The style comes from the same source of truth the chat uses — the section between
``<!-- kontrakt-start -->`` and ``<!-- kontrakt-end -->`` in the ``odpowiedz-na-oferte``
skill, i.e. Jan's own written rules — plus the live portfolio block and a layout
skeleton that says where each block goes.

An earlier revision fed 2–3 messages from ``ODPOWIEDZI-*.md`` in as "Jan's voice".
Jan corrected that on 2026-10-07: those files are drafts **this system** produced in
earlier runs, so imitating them means imitating a weaker predecessor. They stay useful
as history for the measurement loop (which rules led to replies), never as a style
reference — so no corpus is loaded into the prompt here.

The price is deliberately missing (decision D5). Jan prices every offer by hand from a
fresh market check, so the draft carries a single :data:`~freelance_pipeline.proposal_check.PRICE_MARKER`
line where the amount paragraph will go; nothing else in the draft may contain an amount.
This is why the model here is ``deepseek/deepseek-v4.1-flash`` — the same one Jan talks
to in the chat — and not the cheap ``gemini`` scorer: the draft written by the cron and
the message written on demand must have one style, or the two drift apart.

Every draft is validated by :func:`freelance_pipeline.proposal_check.check_draft` before
it leaves this module, with exactly one retry, and a draft that still fails is returned
with its ``problems`` filled in rather than silently shipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from freelance_pipeline.models import JobLead
from freelance_pipeline.openrouter import DEFAULT_COMPLETIONS_URL, ENV_API_KEY
from freelance_pipeline.portfolio import render_portfolio_block
from freelance_pipeline.proposal_check import (
    PRICE_MARKER,
    RULES_VERSION,
    check_draft,
    price_paragraph,
    summarise,
)

#: The chat's model — one style for cron drafts and on-demand messages (D5).
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

DEFAULT_SKILL_PATH = Path("/opt/data/skills/productivity/odpowiedz-na-oferte/SKILL.md")
CONTRACT_START = "<!-- kontrakt-start -->"
CONTRACT_END = "<!-- kontrakt-end -->"

#: Where messages Jan actually sent (after his own edits) live — the ONLY style
#: source besides the contract. One section per message, parsed by
#: :func:`load_approved_examples`; ``scripts/moj_styl.py`` appends to it.
APPROVED_STYLE_PATH = Path("/opt/data/projects/snajper-ofert/MOJ-STYL.md")
_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)
_KEY_RE = re.compile(r"^klucz:\s*(?P<key>\S+)\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```(?:text)?\n(?P<body>.*?)```", re.DOTALL)
_TOKEN_RE = re.compile(r"[a-ząćęłńóśźż0-9]{5,}")
_MAX_EXAMPLE_CHARS = 1800


#: The layout skeleton shown in the prompt: structure, not prose to imitate.
#: NOTE (2026-10-07, Jan's correction): the ``ODPOWIEDZI-*.md`` / ``wiadomosci-*.md``
#: files are drafts *this system* wrote, not text Jan wrote — imitating them would only
#: teach the model its own predecessor's habits, so no corpus goes into the prompt.
#: The only style sources are the skill contract (Jan's explicit rules) and this
#: layout, which states where each block goes.
_SHAPE_EXAMPLE = f"""=== SZABLON UKŁADU (to nie jest gotowa wiadomość, tylko układ bloków) ===
Cześć,

<1-2 zdania: konkretny problem klienta z ogłoszenia i pożądany efekt. Zero "nazywam się", zero streszczania ogłoszenia — te dwie linie decydują, czy klient w ogóle otworzy wiadomość>

<1-2 zdania: jak to rozwiążę technicznie, w tym dokładnie jeden projekt z portfolio z linkiem>
<opcjonalnie 2-4 punkty w wolnym kształcie: konkret, małą literą, bez etykiet>

{PRICE_MARKER}

<jedno krótkie zdanie: od czego zaczynamy (jeden pierwszy krok), jako osobne zdanie>

<jedno pytanie domykające, samo — nigdy zlepione z krokiem pierwszym w jedno zdanie przez przecinek>

Jan Noga
jnxautomation.pl
+48 600 000 000
"""


def mask_price(message: str) -> str:
    """A sent message with its price paragraph replaced by the marker.

    Jan's approved examples carry real amounts; the draft must not, and a live run
    showed that examples with amounts in them teach the model to drop the marker
    line altogether. So examples are always masked before they reach the prompt.
    """
    block = price_paragraph(message)
    if block is None:
        return message
    return message.replace(block, PRICE_MARKER, 1)


def load_contract(skill_path: Path | None = None) -> str:
    """The canonical writing rules: the marked section of the ``odpowiedz-na-oferte`` skill."""
    path = skill_path or DEFAULT_SKILL_PATH
    if not path.is_file():
        raise ProposalError(f"skill file with the contract is missing: {path}")
    text = path.read_text(encoding="utf-8")
    start = text.find(CONTRACT_START)
    end = text.find(CONTRACT_END)
    if start == -1 or end <= start:
        raise ProposalError(f"{path} has no {CONTRACT_START} / {CONTRACT_END} section")
    contract = text[start + len(CONTRACT_START) : end].strip()
    if not contract:
        raise ProposalError(f"{path} has an empty contract section")
    return contract


class ProposalError(Exception):
    """A proposal failure; nothing is persisted, so the caller can retry later."""


class InvalidProposalResponse(ProposalError):
    """The model's payload violated the strict output contract."""


@dataclass(frozen=True, slots=True)
class OfferInput:
    """The minimum an offer must expose for a draft to be written."""

    key: str
    title: str
    source: str
    url: str = ""
    author: str = ""
    description: str = ""
    bucket: str = ""
    score: int | None = None
    budget: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError("key must be a non-empty string")
        if not isinstance(self.title, str):
            raise TypeError("title must be a string")
        if self.score is not None and (isinstance(self.score, bool) or not isinstance(self.score, int)):
            raise TypeError("score must be an int or None")

    @classmethod
    def from_lead(cls, lead: JobLead, *, bucket: str = "", score: int | None = None) -> OfferInput:
        return cls(
            key=f"{lead.source}:{lead.source_id}",
            title=lead.title or "",
            source=lead.source,
            url=lead.url,
            author=lead.company_or_author or "",
            description=lead.description or "",
            bucket=bucket,
            score=score,
            budget=lead.budget,
            currency=lead.currency,
        )

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.description}".strip()


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """A draft plus the verdict of the outbound check."""

    key: str
    text: str
    problems: tuple[str, ...] = ()
    attempts: int = 1
    model: str = ""
    rules_version: str = RULES_VERSION

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def note(self) -> str:
        return summarise(self.problems)


#: Written once, when the style file is created; Jan pastes his edits under the sections.
STYLE_FILE_HEADER = """# Mój styl — wiadomości, które wysłałem po swojej redakcji

Ten plik jest **jedynym źródłem stylu** przy pisaniu draftów (oprócz kontraktu ze skilla
`odpowiedz-na-oferte`). Trafia tu finalna wersja wiadomości, którą Jan faktycznie wysłał —
a nie draft wygenerowany przez system. Pliki `ODPOWIEDZI-*.md` i `wiadomosci-*.md` są outputem
systemu, więc nigdy nie wchodzą do promptu.

Dopisanie sekcji z terminala:

```sh
cd /opt/data/projects/snajper-ofert
uv run python scripts/moj_styl.py dodaj --tytul "Subiekt i WooCommerce" --klucz useme:145473 --plik /tmp/wyslane.txt
```

albo po prostu wklej sekcję ręcznie w formacie:

```text
## 1. <tytuł zlecenia>
klucz: <source:source_id>   (opcjonalny)

```text
<treść wiadomości, którą wysłałem>
```
```
"""

@dataclass(frozen=True, slots=True)
class ApprovedExample:
    """A message Jan sent after editing it himself — his voice, verbatim."""

    title: str
    message: str
    key: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if not self.message.strip():
            raise ValueError("message must not be empty")


def load_approved_examples(path: Path | None = None) -> tuple[ApprovedExample, ...]:
    """Read :data:`APPROVED_STYLE_PATH`; a missing file means "no examples yet".

    The file holds only final versions Jan sent himself. The historical
    ``ODPOWIEDZI-*.md`` / ``wiadomosci-*.md`` files are *not* read here on purpose:
    those are drafts this system produced, and imitating them means imitating a
    weaker predecessor (Jan's correction, 2026-10-07).
    """
    source = path or APPROVED_STYLE_PATH
    if not source.is_file():
        return ()
    text = source.read_text(encoding="utf-8")
    marks = list(_SECTION_RE.finditer(text))
    examples: list[ApprovedExample] = []
    seen: set[str] = set()
    for index, mark in enumerate(marks):
        title = re.sub(r"^\d+\.\s*", "", mark.group("title")).strip()
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[mark.end() : end]
        match = _FENCE_RE.search(body)
        if match is None:
            continue
        message = match.group("body").strip()[:_MAX_EXAMPLE_CHARS]
        if len(message) < 80:
            continue
        fingerprint = hashlib.sha1(message.encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        key_match = _KEY_RE.search(body)
        examples.append(
            ApprovedExample(
                title=title,
                message=message,
                key=key_match.group("key") if key_match else "",
            )
        )
    return tuple(examples)


def record_approved_example(
    title: str,
    message: str,
    *,
    key: str = "",
    path: Path | None = None,
) -> ApprovedExample:
    """Append one sent message to the style file (the capture step of the loop)."""
    example = ApprovedExample(title=title.strip(), message=message.strip(), key=key.strip())
    source = path or APPROVED_STYLE_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    existing = load_approved_examples(source)
    if any(item.message == example.message for item in existing):
        raise ProposalError("ta wiadomość jest już w pliku stylu")
    number = len(existing) + 1
    lines = [f"## {number}. {example.title}"]
    if example.key:
        lines.append(f"klucz: {example.key}")
    lines += ["", "```text", example.message, "```", ""]
    prefix = "" if source.is_file() else STYLE_FILE_HEADER
    with source.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(lines) + "\n")
    return example


def pick_approved_examples(
    examples: Sequence[ApprovedExample],
    offer_text: str,
    *,
    limit: int = 2,
) -> tuple[ApprovedExample, ...]:
    """The ``limit`` sent messages closest to this offer (deterministic order)."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    tokens = set(_TOKEN_RE.findall(offer_text.lower()))
    ranked = sorted(
        (
            (len(tokens & set(_TOKEN_RE.findall(f"{item.title}\n{item.message}".lower()))), item.title, item)
            for item in examples
        ),
        key=lambda row: (-row[0], row[1]),
    )
    return tuple(row[2] for row in ranked[:limit])


_SYSTEM_PROMPT = (
    "Jesteś Janem Nogą, niezależnym specjalistą automatyzacji (n8n, Make, Python, API, "
    "integracje, strony i aplikacje webowe, chatboty, obróbka danych). Piszesz wiadomość "
    "do konkretnego zlecenia tak, żeby brzmiała jak napisana przez człowieka, który przeczytał "
    "ogłoszenie i wie, co zrobić. Nie sprzedajesz, nie chwalisz się, nie streszczasz klientowi "
    "jego własnego ogłoszenia i nie wymyślasz projektów ani metryk, których nie ma w portfolio. "
    "Najpierw odpowiadasz na pytania i prośby z ogłoszenia, potem proponujesz rozwiązanie. "
    "Odpowiadasz WYŁĄCZNIE tablicą JSON, bez komentarza i bez markdowna."
)

_EXTRA_RULES = """=== ZASADY DODATKOWE (research konwersji, 2026-10-07) ===
1. Pierwsze dwie linie (do ~200 znaków) to sedno problemu klienta i pożądany efekt: podgląd listy
   pokazuje klientowi tylko tyle, więc bez przedstawiania się i bez powtarzania jego ogłoszenia.
2. Akapity po 1-2 zdania, nigdy ściana tekstu (klient czyta na telefonie).
3. Lista punktowana jest dozwolona wyłącznie w wolnym kształcie: 2-4 punkty, każdy w jednej linii
   (do ~60 znaków), małą literą, bez etykiety, bez numeracji, bez pogrubienia, bez kropki i
   dwukropka na końcu, o różnych długościach. Nigdy trzy punkty o identycznej budowie.
4. Zakaz długich myślników (—): tylko przecinek, dwukropek, kropka.
5. Zakaz fraz: "w dzisiejszym dynamicznym świecie", "kompleksowe rozwiązanie", "nie tylko... ale
   także", "szeroki wachlarz", "płynna integracja", "kluczowy element", "warto podkreślić",
   "podsumowując", "w dobie", "niesamowite".
6. Krok pierwszy i domknięcie to DWA osobne zdania: najpierw jedno krótkie zdanie "od czego
   zaczynamy", potem jedno pytanie domykające typu "czy taki podział na etapy ma sens u Was".
   Nigdy nie zlepiaj ich w jedno zdanie przecinkiem ("Zacznijmy od X, czy ma sens?"). Dokładnie
   jedno pytanie w całej wiadomości, bez żądania rozmowy czy telefonu.
7. Personalizacja wyłącznie z treści ogłoszenia. Zero faktów o kliencie z internetu (KRS, LinkedIn,
   prasa) i zero słów o tym, czego Jan jeszcze nie robił.
8. Dokładnie jeden link, wklejony znak w znak z listy portfolio powyżej. Nic do adresu nie dodawaj.
9. Zero zdjęć z siebie i zero podważania decyzji klienta: nie pisz, że to "prototyp, nie wdrożenie",
   nie tłumacz się z doświadczenia, nie porównuj ceny do pensji ani etatu ("mniej niż jedna pensja").
   Wycenę i jej zawartość niesie wyłącznie akapit z ceną — poza nim nie ma ani liczby, ani wartości.
10. Zdanie o orientacyjności wyceny ("Wycena i czas są orientacyjne...") wchodzi razem z akapitem
    ceny. Nie powtarzaj go nigdzie indziej (ani przy kroku pierwszym, ani przy pytaniu domykającym).
11. Jeśli ogłoszenie zawiera pytania albo prośby o informacje (wycena wariantów, termin, co zawiera
    cena, sposób pracy), odpowiedz na każde z nich zwięźle i po kolei, własnymi słowami, w akapicie
    przed wyceną. Nie streszczaj ogłoszenia i nie powtarzaj pytań dosłownie. To ma pierwszeństwo
    przed ogólnym opisem rozwiązania.
12. Długość: 700-1400 znaków bez linii wyceny. Przy wielu pytaniach idź do 1400, ale nie powyżej.
"""

_PRICE_RULE = f"""=== WYCENA (decyzja Jana: każdą kwotę liczy ręcznie) ===
Draft NIE zawiera żadnej kwoty, waluty ani widełek. Akapit z ceną zastępujesz DOKŁADNIE JEDNĄ
samodzielną linią, w tej dokładnej postaci:

{PRICE_MARKER}

Ta linia jest stałym elementem wiadomości: stoi jako osobny akapit PO akapicie "jak to rozwiążę"
(a razem z listą punktowaną: po liście) i PRZED pytaniem domykającym. Zostaw ją, mimo że nie ma
w niej kwoty, i nie pisz wokół niej nic o wycenie ("wycena na priv", "do ustalenia").
Limit 700-1400 znaków liczy się dla wiadomości BEZ tej linii.
"""

_OUTPUT_CONTRACT = (
    "Odpowiedz wyłącznie tablicą JSON, bez markdowna, bez komentarzy: "
    '[{"offer_key": <int z zapytania>, "message": "<gotowa wiadomość>"}, ...] '
    "Dokładnie jeden obiekt na każde ogłoszenie z zapytania, każdy z własnym offer_key. "
    "Pole message zawiera pełną treść wiadomości (powitanie, treść, podpis w trzech wierszach), "
    "z linią znacznika wyceny w miejscu akapitu z ceną."
)


class ProposalWriter:
    """Batch writer for offer replies: prompt in, validated drafts out."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | Callable[[], str | None] | None = None,
        base_url: str = DEFAULT_COMPLETIONS_URL,
        timeout: float = 120.0,
        batch_size: int = 5,
        max_attempts: int = 2,
        session: Any | None = None,
        skill_path: Path | None = None,
        contract: str | None = None,
        portfolio_block: str | None = None,
        approved_examples: Sequence[ApprovedExample] | None = None,
        approved_example_limit: int = 3,
        style_path: Path | None = None,
    ) -> None:
        for name, value in (("batch_size", batch_size), ("max_attempts", max_attempts)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if approved_example_limit < 0:
            raise ValueError("approved_example_limit must not be negative")
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self._api_key = api_key
        self._session = session
        self._skill_path = skill_path
        self._contract = contract
        self._portfolio_block = portfolio_block
        self.approved_example_limit = approved_example_limit
        self._approved_examples = approved_examples
        self._style_path = style_path

    # ------------------------------------------------------------------ inputs

    def contract(self) -> str:
        if self._contract is None:
            self._contract = load_contract(self._skill_path)
        return self._contract

    def portfolio_block(self) -> str:
        if self._portfolio_block is None:
            self._portfolio_block = render_portfolio_block()
        return self._portfolio_block

    def approved_examples(self) -> tuple[ApprovedExample, ...]:
        """Messages Jan sent after editing them — the only style reference."""
        if self._approved_examples is None:
            self._approved_examples = load_approved_examples(self._style_path)
        return tuple(self._approved_examples)

    def _resolve_api_key(self) -> str | None:
        if callable(self._api_key):
            return self._api_key()
        if isinstance(self._api_key, str) and self._api_key:
            return self._api_key
        return os.environ.get(ENV_API_KEY)

    # ----------------------------------------------------------------- writing

    def write(self, offers: Sequence[OfferInput]) -> list[ProposalDraft]:
        """Draft a message for every offer, in input order."""
        prepared: list[OfferInput] = list(offers)
        for offer in prepared:
            if not isinstance(offer, OfferInput):
                raise TypeError(f"offers must be OfferInput instances, got {type(offer).__name__}")
        if not prepared:
            return []
        api_key = self._resolve_api_key()
        if not api_key:
            raise ProposalError(f"{ENV_API_KEY} is not configured; drafting skipped")
        session = self._session
        if session is None:  # pragma: no cover - exercised only in production
            import requests

            session = requests.Session()
        drafts: list[ProposalDraft] = []
        for start in range(0, len(prepared), self.batch_size):
            batch = prepared[start : start + self.batch_size]
            drafts.extend(self._write_batch(batch, session, api_key))
        return drafts

    def _write_batch(
        self, batch: Sequence[OfferInput], session: Any, api_key: str
    ) -> list[ProposalDraft]:
        last_error: InvalidProposalResponse | None = None
        texts: list[str] = []
        for _ in range(self.max_attempts):
            try:
                texts = self._request(batch, session, api_key, previous=None, problems=None)
                break
            except InvalidProposalResponse as exc:
                last_error = exc
        else:
            assert last_error is not None
            raise last_error
        drafts = [
            ProposalDraft(
                key=offer.key,
                text=text,
                problems=check_draft(text, context=self._context_for(offer)),
                attempts=1,
                model=self.model,
            )
            for offer, text in zip(batch, texts, strict=True)
        ]
        failing = [draft for draft in drafts if not draft.ok]
        if not failing or self.max_attempts < 2:
            return drafts
        return self._repair(batch, drafts, session, api_key)

    def _repair(
        self,
        batch: Sequence[OfferInput],
        drafts: Sequence[ProposalDraft],
        session: Any,
        api_key: str,
    ) -> list[ProposalDraft]:
        """One retry for the drafts that failed the check — improved or reported as is."""
        by_key = {offer.key: offer for offer in batch}
        failing = [draft for draft in drafts if not draft.ok]
        previous = [by_key[draft.key] for draft in failing]
        problems = [draft.problems for draft in failing]
        texts = [draft.text for draft in failing]
        try:
            fixed = self._request(previous, session, api_key, previous=texts, problems=problems)
        except InvalidProposalResponse:
            return list(drafts)
        repaired: dict[str, ProposalDraft] = {}
        for offer, text in zip(previous, fixed, strict=True):
            candidate = ProposalDraft(
                key=offer.key,
                text=text,
                problems=check_draft(text, context=self._context_for(offer)),
                attempts=2,
                model=self.model,
            )
            repaired[offer.key] = candidate
        out: list[ProposalDraft] = []
        for draft in drafts:
            replacement = repaired.get(draft.key)
            if replacement is None or (draft.ok and not replacement.ok):
                out.append(draft)
                continue
            if replacement.ok or len(replacement.problems) < len(draft.problems):
                out.append(replacement)
            else:
                # Druga próba była (kosztowała), ale nie poprawiła treści — raportujemy to wprost.
                out.append(replace(draft, attempts=2))
        return out

    def _context_for(self, offer: OfferInput) -> str:
        """Offer text plus the portfolio block — the allowlist the link check uses."""
        return f"{offer.text}\n\n{self.portfolio_block()}"

    # --------------------------------------------------------------- prompting

    def _prompt(
        self,
        batch: Sequence[OfferInput],
        *,
        previous: Sequence[str] | None,
        problems: Sequence[Sequence[str]] | None,
    ) -> str:
        parts: list[str] = [self.contract(), _EXTRA_RULES, self.portfolio_block()]
        offer_text = "\n".join(offer.text for offer in batch)
        examples = pick_approved_examples(
            self.approved_examples(), offer_text, limit=self.approved_example_limit
        )
        if examples:
            parts.append(
                "=== WIADOMOŚCI, KTÓRE JAN WYSŁAŁ PO SWOJEJ REDAKCJI (jedyny wzór stylu) ===\n"
                "To jego wersje po poprawkach. Trzymaj się ich rytmu i układu. Akapit z ceną jest tu "
                "zamieniony na znacznik, tak samo ma wyglądać Twój draft.\n"
                + json.dumps(
                    [
                        {"zlecenie": example.title, "wiadomość": mask_price(example.message)}
                        for example in examples
                    ],
                    ensure_ascii=False,
                    indent=1,
                )
            )
        parts.append(_SHAPE_EXAMPLE)
        rows = [
            {
                "offer_key": index,
                "tytuł": offer.title,
                "źródło": offer.source,
                "url": offer.url,
                "autor": offer.author,
                "kubełek": offer.bucket,
                "ocena": offer.score,
                "budżet": offer.budget,
                "waluta": offer.currency,
                "treść_ogłoszenia": offer.description[:6000],
            }
            for index, offer in enumerate(batch)
        ]
        parts.append(_PRICE_RULE)
        parts.append(_OUTPUT_CONTRACT)
        if previous is None:
            parts.append("Zapytanie:\n" + json.dumps(rows, ensure_ascii=False))
        else:
            parts.append(
                "=== DO POPRAWY (poprzednie drafty nie przeszły kontroli) ===\n"
                + json.dumps(
                    [
                        {
                            "offer_key": index,
                            "problemy": list(problem_list),
                            "poprzednia_wiadomość": text,
                            "ogłoszenie": rows[index],
                        }
                        for index, (text, problem_list) in enumerate(
                            zip(previous, problems or (), strict=False)
                        )
                    ],
                    ensure_ascii=False,
                )
                + "\nPopraw dokładnie to, na co wskazują problemy, i zwróć nowe wiadomości dla "
                "wszystkich podanych offer_key. Gdy problem to brak znacznika wyceny: wstaw "
                f"samodzielną linię {PRICE_MARKER} jako osobny akapit między treścią "
                "a pytaniem domykającym."
            )
        return "\n\n".join(parts)

    def _request(
        self,
        batch: Sequence[OfferInput],
        session: Any,
        api_key: str,
        *,
        previous: Sequence[str] | None,
        problems: Sequence[Sequence[str]] | None,
    ) -> list[str]:
        prompt = self._prompt(batch, previous=previous, problems=problems)
        try:
            response = session.post(
                self.base_url,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "offer_drafts",
                            "strict": True,
                            "schema": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "offer_key": {"type": "integer", "minimum": 0},
                                        "message": {"type": "string"},
                                    },
                                    "required": ["offer_key", "message"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                    },
                    "temperature": 0,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ProposalError(f"OpenRouter request failed: {exc}") from exc
        status_code = getattr(response, "status_code", None)
        if status_code is not None and status_code >= 400:
            raise ProposalError(f"OpenRouter HTTP {status_code}")
        try:
            payload_text = response.text
        except Exception as exc:
            raise ProposalError(f"OpenRouter response was unreadable: {exc}") from exc
        return _parse_content(_extract_content(payload_text), batch)


def _extract_content(payload_text: str) -> str:
    try:
        envelope = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise InvalidProposalResponse("OpenRouter envelope is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise InvalidProposalResponse("OpenRouter envelope must be an object")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InvalidProposalResponse("OpenRouter envelope has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise InvalidProposalResponse("OpenRouter choice has no message object")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise InvalidProposalResponse("OpenRouter message content is empty")
    return content


def _parse_content(content: str, batch: Sequence[OfferInput]) -> list[str]:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 : -3].strip()
    try:
        items = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InvalidProposalResponse("draft payload is not valid JSON") from exc
    if not isinstance(items, list):
        raise InvalidProposalResponse("draft payload must be a JSON array")
    if len(items) != len(batch):
        raise InvalidProposalResponse(
            f"draft payload has {len(items)} items for {len(batch)} offers"
        )
    by_key: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InvalidProposalResponse("draft item must be an object")
        offer_key = item.get("offer_key")
        if isinstance(offer_key, bool) or not isinstance(offer_key, int):
            raise InvalidProposalResponse("draft item missing integer offer_key")
        if not 0 <= offer_key < len(batch):
            raise InvalidProposalResponse(f"draft item references unknown offer_key {offer_key!r}")
        if offer_key in by_key:
            raise InvalidProposalResponse(f"draft item duplicates offer_key {offer_key!r}")
        message = item.get("message")
        if not isinstance(message, str) or not message.strip():
            raise InvalidProposalResponse("draft item has an empty message")
        by_key[offer_key] = message.strip()
    missing = [index for index in range(len(batch)) if index not in by_key]
    if missing:
        raise InvalidProposalResponse(f"draft payload missing offers: {missing}")
    return [by_key[index] for index in range(len(batch))]


def draft_offers(
    offers: Iterable[OfferInput],
    *,
    writer: ProposalWriter | None = None,
) -> list[ProposalDraft]:
    """Convenience wrapper: one call, default writer."""
    return (writer or ProposalWriter()).write(list(offers))


__all__ = [
    "APPROVED_STYLE_PATH",
    "CONTRACT_END",
    "CONTRACT_START",
    "DEFAULT_MODEL",
    "DEFAULT_SKILL_PATH",
    "ApprovedExample",
    "InvalidProposalResponse",
    "OfferInput",
    "ProposalDraft",
    "ProposalError",
    "ProposalWriter",
    "draft_offers",
    "load_approved_examples",
    "load_contract",
    "mask_price",
    "pick_approved_examples",
    "record_approved_example",
]
