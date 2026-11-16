"""Wycenianie zlecenia: osobny etap, z własnym researchem i własnymi źródłami.

Dlaczego osobno od pisania (decyzja Jana, 2026-10-07): agent piszący wiadomość nie ma
w tym wywołaniu żadnego wyszukiwania, więc kwota pochodziłaby z pamięci modelu — czyli
dokładnie z tego, czego zabrania zasada wyceny w skillu ``odpowiedz-na-oferte`` („sprawdź
aktualne stawki rynkowe w internecie, nie zgaduj z pamięci”). Drugi powód: rozdzielenie
zostawia tekst niezakotwiczony liczbą i daje wycenie własne uzasadnienie oraz listę źródeł,
którą da się sprawdzić.

Ten moduł nie ma tabeli stawek i nie wolno mu jej dodać (zakaz stałego cennika): każde
wywołanie to świeży research. Zwraca jedną liczbę za pierwszy etap plus czas, nigdy widełki,
i twarde dane do audytu: stawkę rynkową, źródła i uzasadnienie.

Wstawianie kwoty do draftu robi :func:`apply_price` — draft z crona nosi znacznik
``PRICE_MARKER``, a tu zamieniamy go na akapit zgodny z kontraktem (kwota i czas na początku,
potem „co w tym jest” i zdanie o orientacyjności), po czym całość przechodzi kontrolę
w trybie ``priced=True``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from freelance_pipeline.openrouter import DEFAULT_COMPLETIONS_URL, ENV_API_KEY
from freelance_pipeline.proposal import OfferInput, ProposalDraft
from freelance_pipeline.proposal_check import (
    PRICE_DISCLAIMER,
    PRICE_MARKER,
    check_draft,
    summarise,
)

DEFAULT_PRICER_MODEL = "deepseek/deepseek-v4.1-flash"
#: Ile wyników wyszukiwania dostaje model w jednym wywołaniu.
DEFAULT_WEB_RESULTS = 4
#: Twardy próg opłacalności (skill: 90 zł/h to granica, poniżej nie bierzemy).
MIN_HOURLY_PLN = 90.0
#: Konwersje na złote, gdy klient płaci w obcej walucie (skill: USD × 3,9; EUR × 4,25).
FX_TO_PLN = {"PLN": 1.0, "USD": 3.9, "EUR": 4.25, "GBP": 4.6}
#: Sensowne granice na czas realizacji pierwszego etapu.
MIN_DAYS, MAX_DAYS = 1, 40

_URL_RE = re.compile(r"https?://[^\s<>)\(\]\"]+")


class PriceError(Exception):
    """Pricing failed; the draft keeps its marker instead of getting an invented amount."""


class InvalidPriceResponse(PriceError):
    """The pricer's payload violated the output contract."""


@dataclass(frozen=True, slots=True)
class PriceBrief:
    """One offer's price, with the evidence behind it."""

    key: str
    amount: float
    currency: str
    days: int
    hourly_min: float
    hourly_max: float
    includes: tuple[str, ...]
    sources: tuple[str, ...]
    rationale: str
    model: str = ""
    #: Realna praca w godzinach (config/czas.yaml). Gdy jest, próg 90 zł/h liczy się z godzin,
    #: a nie z dni kalendarzowych, bo dni zawierają czekanie na materiały od klienta.
    hours: float | None = None

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("amount must not be negative")
        if not MIN_DAYS <= self.days <= MAX_DAYS:
            raise ValueError(f"days must be between {MIN_DAYS} and {MAX_DAYS}")
        if not self.includes:
            raise ValueError("includes must list what the amount covers")
        if not self.rationale.strip():
            raise ValueError("rationale must not be empty")

    @property
    def hourly_equivalent(self) -> float:
        """Złote na godzinę: z realnych godzin, a bez nich przy 6 h pracy dziennie."""
        work_hours = self.hours if self.hours else self.days * 6
        return self.amount_pln / work_hours

    @property
    def amount_pln(self) -> float:
        return self.amount * FX_TO_PLN.get(self.currency.upper(), 1.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "amount": self.amount,
            "currency": self.currency,
            "days": self.days,
            "hourly_min": self.hourly_min,
            "hourly_max": self.hourly_max,
            "includes": list(self.includes),
            "sources": list(self.sources),
            "rationale": self.rationale,
            "model": self.model,
            "hours": self.hours,
        }


#: Jak zapisać walutę w wiadomości (klient polski czyta „zł”, nie „PLN”).
CURRENCY_SYMBOL = {"PLN": "zł", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}


def _amount_text(amount: float) -> str:
    text = f"{amount:,.0f}".replace(",", " ")
    return text


def _lower_first(text: str) -> str:
    stripped = text.strip().rstrip(".")
    return stripped[:1].lower() + stripped[1:] if stripped else stripped


def price_paragraph(brief: PriceBrief) -> str:
    """Akapit z ceną dokładnie w kształcie z kontraktu."""
    amount = _amount_text(brief.amount)
    symbol = CURRENCY_SYMBOL.get(brief.currency.upper(), brief.currency)
    days = "1 dzień roboczy" if brief.days == 1 else f"{brief.days} dni roboczych"
    items = ", ".join(_lower_first(item) for item in brief.includes[:3])
    # „, w cenie: …” zamiast „za …”, bo pozycje cennika są w mianowniku i po „za” wychodziły
    # kwiatki typu „za integracja API” (lista z 2026-10-28). Przecinek, nie kropka, bo kontrola
    # wyjścia liczy zdania w akapicie i kropka po terminie robiła z niego cztery zdania.
    return (
        f"{amount} {symbol}, ok. {days}, w cenie: {items}. "
        f"{PRICE_DISCLAIMER}"
    )


PRICE_FALLBACK = "Wycena i termin po doprecyzowaniu zakresu, w ciągu 24 godzin od odpowiedzi."


def _tidy(message: str) -> str:
    """Sprząta po usuniętym zdaniu: nadmiar spacji w linii i zbędne puste linie."""
    lines = [re.sub(r" {2,}", " ", line).rstrip() for line in message.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def apply_price(message: str, brief: PriceBrief) -> str:
    """Wstaw akapit z ceną w miejsce znacznika (albo na jego koniec, gdy go brak)."""
    paragraph = price_paragraph(brief)
    if PRICE_MARKER in message:
        # Akapit z ceną już niesie zdanie o orientacyjności. Model bywa, że dokleja je także
        # w treści (typowo zlepione z pytaniem domykającym) — wtedy wiadomość ma je dwa razy,
        # więc usuwamy kopie z treści, a akapit wnosi je raz.
        body = message.replace(PRICE_DISCLAIMER, "")
        return _tidy(body.replace(PRICE_MARKER, paragraph, 1))
    return f"{message.rstrip()}\n\n{paragraph}"


def strip_price_marker(message: str) -> tuple[str, bool]:
    """Usuwa znacznik ceny. Zwraca ``(tekst, czy_znacznik_byl)``.

    Gdy kwoty nie da sie policzyc (poza cennikiem i model nie odpowiedzial), wiadomosc
    nie moze wyjsc ze znacznikiem: zamiast tego dostaje zdanie o wycenie po szczegolach.
    """
    if PRICE_MARKER not in message:
        return message, False
    body = message.replace(PRICE_DISCLAIMER, "").replace(PRICE_MARKER, PRICE_FALLBACK, 1)
    return _tidy(body), True


def price_problems(message: str, brief: PriceBrief, *, context: str = "") -> tuple[str, ...]:
    """Kontrola wiadomości z wstawioną ceną — z dodatkowym progiem 90 zł/h."""
    problems = list(check_draft(message, context=context, priced=True))
    if brief.hourly_equivalent < MIN_HOURLY_PLN:
        problems.append(
            f"wycena poniżej progu 90 zł/h (równowartość {brief.hourly_equivalent:.0f} zł/h)"
        )
    if not brief.sources:
        problems.append("wycena bez źródła (brak researchu stawek)")
    return tuple(problems)


def _MISSING_BRIEF(key: str) -> PriceBrief:
    """Wycena zastępcza dla oferty, której wyceniający nie obsłużył (kwota 0, bez źródeł)."""
    return PriceBrief(
        key=key,
        amount=0.0,
        currency="PLN",
        days=1,
        hourly_min=0.0,
        hourly_max=0.0,
        includes=("-",),
        sources=(),
        rationale="brak wyceny — wyceniający nie zwrócił odpowiedzi dla tej oferty",
    )


_PRICE_SYSTEM = (
    "Jesteś wyceniającym (estimator), nie copywriterem. Wyceniasz jedno zlecenie dla Jana Nogi, "
    "niezależnego specjalisty automatyzacji (n8n, Make, Python, API, integracje, strony i aplikacje "
    "webowe, chatboty, obróbka danych), pracującego z Polski, fakturującego w złotych, gdy klient "
    "pisze po polsku. Zanim podasz kwotę, sprawdź aktualne stawki rynkowe dla tego typu pracy. "
    "Nie ma żadnego cennika ani tabeli stawek — każdą kwotę budujesz od zera z zakresu i z danych, "
    "które znalazłeś. Odpowiadasz WYŁĄCZNIE tablicą JSON."
)

_PRICE_CONTRACT = """Zasady wyceny (twarde):
1. Jedna liczba za pierwszy etap, nigdy widełki, nigdy "do ustalenia".
2. Kwota liczona z realnego zakresu (moduły, integracje, źródła danych, wdrożenie) i z aktualnych
   stawek rynkowych, które sprawdziłeś. Podaj w "zrodla" wyłącznie adresy, które faktycznie
   dotyczą stawek lub wycen tego typu pracy: najlepiej stawki freelancerskie i wyceny projektów
   (Useme, Upwork, blogi o stawkach, cenniki wdrożeń), a nie artykuły o płacy minimalnej, podatkach
   ani strony software house'ów. Jeśli
   w wynikach wyszukiwania nie ma nic trafnego, napisz to w "uzasadnienie" i oprzyj kwotę na
   najbliższej znalezionej analogii — nie zmyślaj źródła.
3. Próg opłacalności: równowartość nigdy poniżej 90 zł/h (przy ~6 h pracy dziennie).
4. Jeśli ogłoszenie ma budżet, uszanuj go: kwota startowa to jego dolna-środkowa część i jedna
   liczba, a duże zlecenie dzieli się na etapy (wyceniasz pierwszy).
5. Waluta: polskie ogłoszenie → PLN; obcojęzyczne → waluta klienta (USD/EUR/GBP).
6. "co_w_cenie": 2-3 krótkie elementy (do ~60 znaków każdy), rzeczowniki, nie zdania.
7. "dni": realny czas realizacji pierwszego etapu w dniach roboczych.

Format odpowiedzi (wyłącznie JSON, bez markdowna):
[{"offer_key": <int z zapytania>, "kwota": <liczba>, "waluta": "PLN"|"USD"|"EUR"|"GBP",
  "dni": <int>, "stawka_min": <liczba zł/h>, "stawka_max": <liczba zł/h>,
  "co_w_cenie": ["...", "..."], "zrodla": ["https://..."], "uzasadnienie": "<do 3 zdań>"}, ...]
Dokładnie jeden obiekt na każde ogłoszenie z zapytania, każdy z własnym offer_key."""


def build_fix_prompt(offers: Sequence[OfferInput], previous: Sequence[PriceBrief]) -> str:
    """Druga próba dla wycen, które wyszły poniżej progu 90 zł/h."""
    rows = []
    for index, (offer, brief) in enumerate(zip(offers, previous, strict=True)):
        rows.append(
            {
                "offer_key": index,
                "tytuł": offer.title,
                "budżet": offer.budget,
                "waluta_budżetu": offer.currency,
                "poprzednia_wycena": {
                    "kwota": brief.amount,
                    "waluta": brief.currency,
                    "dni": brief.days,
                    "rownowartosc_pln_h": round(brief.hourly_equivalent),
                },
                "treść_ogłoszenia": offer.description[:2000],
            }
        )
    return (
        _PRICE_CONTRACT
        + "\n\n=== POPRAW WYCENĘ ===\n"
        + json.dumps(rows, ensure_ascii=False)
        + "\nPowód poprawki: równowartość poprzedniej wyceny (przy ~6 h pracy dziennie) wyszła poniżej "
        f"{MIN_HOURLY_PLN:.0f} zł/h. Podnieś kwotę i/lub skróć czas realizacji tak, żeby równowartość "
        "wynosiła co najmniej 90 zł/h, ale nie wymyślaj zakresu ponad to, co jest w ogłoszeniu: "
        "jeśli zlecenie jest małe, to znaczy że jest krótkie (mniej dni), a nie że jest warte mniej "
        "za godzinę. Zwróć nowe obiekty dla wszystkich podanych offer_key."
    )


def build_price_prompt(offers: Sequence[OfferInput]) -> str:
    rows = [
        {
            "offer_key": index,
            "tytuł": offer.title,
            "źródło": offer.source,
            "autor": offer.author,
            "budżet": offer.budget,
            "waluta_budżetu": offer.currency,
            "ocena_selektora": offer.score,
            "treść_ogłoszenia": offer.description[:2500],
        }
        for index, offer in enumerate(offers)
    ]
    return _PRICE_CONTRACT + "\n\nZapytanie:\n" + json.dumps(rows, ensure_ascii=False)


class OpenRouterPricer:
    """Wycenia partię ofert jednym wywołaniem, z włączonym wyszukiwaniem w sieci.

    Wyszukiwanie jest tu obowiązkowe: bez niego kwota byłaby pamięciowa. Adnotacje
    ``url_citation`` z odpowiedzi są zapisywane obok kwoty, żeby Jan mógł sprawdzić,
    na czym wycena stoi.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_PRICER_MODEL,
        api_key: str | Callable[[], str | None] | None = None,
        base_url: str = DEFAULT_COMPLETIONS_URL,
        timeout: float = 180.0,
        batch_size: int = 5,
        web_results: int = DEFAULT_WEB_RESULTS,
        enforce_hourly_floor: bool = True,
        session: Any | None = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if isinstance(web_results, bool) or not isinstance(web_results, int) or web_results < 1:
            raise ValueError("web_results must be a positive integer")
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.batch_size = batch_size
        self.web_results = web_results
        self.enforce_hourly_floor = enforce_hourly_floor
        self._api_key = api_key
        self._session = session

    def _resolve_api_key(self) -> str | None:
        if callable(self._api_key):
            return self._api_key()
        if isinstance(self._api_key, str) and self._api_key:
            return self._api_key
        return os.environ.get(ENV_API_KEY)

    def price(self, offers: Sequence[OfferInput]) -> list[PriceBrief]:
        batch = list(offers)
        for offer in batch:
            if not isinstance(offer, OfferInput):
                raise TypeError(f"offers must be OfferInput instances, got {type(offer).__name__}")
        if not batch:
            return []
        api_key = self._resolve_api_key()
        if not api_key:
            raise PriceError(f"{ENV_API_KEY} is not configured; pricing skipped")
        session = self._session
        if session is None:  # pragma: no cover - exercised only in production
            import requests

            session = requests.Session()
        briefs: list[PriceBrief] = []
        for start in range(0, len(batch), self.batch_size):
            part = batch[start : start + self.batch_size]
            briefs.extend(self._price_batch(part, session, api_key))
        if not self.enforce_hourly_floor:
            return briefs
        return self._lift_below_floor(batch, briefs, session, api_key)

    def _lift_below_floor(
        self,
        batch: Sequence[OfferInput],
        briefs: Sequence[PriceBrief],
        session: Any,
        api_key: str,
    ) -> list[PriceBrief]:
        """Jedna poprawka dla wycen poniżej progu 90 zł/h; brak poprawy = raport, nie ciche 90 zł/h."""
        by_key = {offer.key: offer for offer in batch}
        weak = [brief for brief in briefs if brief.hourly_equivalent < MIN_HOURLY_PLN]
        if not weak:
            return list(briefs)
        fixed: dict[str, PriceBrief] = {}
        for start in range(0, len(weak), self.batch_size):
            part = weak[start : start + self.batch_size]
            try:
                answers = self._price_batch(
                    [by_key[brief.key] for brief in part], session, api_key, previous=part
                )
            except (PriceError, InvalidPriceResponse):
                continue
            for old, new in zip(part, answers, strict=True):
                if new.hourly_equivalent > old.hourly_equivalent:
                    fixed[old.key] = new
        return [fixed.get(brief.key, brief) for brief in briefs]

    def _price_batch(
        self,
        batch: Sequence[OfferInput],
        session: Any,
        api_key: str,
        *,
        previous: Sequence[PriceBrief] | None = None,
    ) -> list[PriceBrief]:
        prompt = (
            build_price_prompt(batch)
            if previous is None
            else build_fix_prompt(batch, previous)
        )
        try:
            response = session.post(
                self.base_url,
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _PRICE_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "plugins": [{"id": "web", "max_results": self.web_results}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "offer_prices",
                            "strict": True,
                            "schema": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "offer_key": {"type": "integer", "minimum": 0},
                                        "kwota": {"type": "number"},
                                        "waluta": {"type": "string", "enum": ["PLN", "USD", "EUR", "GBP"]},
                                        "dni": {"type": "integer", "minimum": MIN_DAYS, "maximum": MAX_DAYS},
                                        "stawka_min": {"type": "number"},
                                        "stawka_max": {"type": "number"},
                                        "co_w_cenie": {"type": "array", "items": {"type": "string"}},
                                        "zrodla": {"type": "array", "items": {"type": "string"}},
                                        "uzasadnienie": {"type": "string"},
                                    },
                                    "required": [
                                        "offer_key",
                                        "kwota",
                                        "waluta",
                                        "dni",
                                        "stawka_min",
                                        "stawka_max",
                                        "co_w_cenie",
                                        "zrodla",
                                        "uzasadnienie",
                                    ],
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
            raise PriceError(f"OpenRouter request failed: {exc}") from exc
        status_code = getattr(response, "status_code", None)
        if status_code is not None and status_code >= 400:
            raise PriceError(f"OpenRouter HTTP {status_code}")
        content, citations = _envelope_payload(response)
        return _parse_prices(content, batch, citations=citations, model=self.model)


def _envelope_payload(response: Any) -> tuple[str, tuple[str, ...]]:
    """Zwraca treść odpowiedzi i adresy z adnotacji wyszukiwania."""
    try:
        envelope = json.loads(response.text)
    except Exception as exc:
        raise InvalidPriceResponse(f"OpenRouter envelope was unreadable: {exc}") from exc
    choices = envelope.get("choices") if isinstance(envelope, dict) else None
    if not isinstance(choices, list) or not choices:
        raise InvalidPriceResponse("OpenRouter envelope has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise InvalidPriceResponse("OpenRouter message content is empty")
    citations: list[str] = []
    for annotation in message.get("annotations") or []:
        if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
            continue
        url = (annotation.get("url_citation") or {}).get("url")
        if isinstance(url, str) and url.startswith("http") and url not in citations:
            citations.append(url)
    return content, tuple(citations)


def _parse_prices(
    content: str,
    batch: Sequence[OfferInput],
    *,
    citations: Sequence[str] = (),
    model: str = "",
) -> list[PriceBrief]:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 : -3].strip()
    try:
        items = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InvalidPriceResponse("price payload is not valid JSON") from exc
    if not isinstance(items, list):
        raise InvalidPriceResponse("price payload must be a JSON array")
    if len(items) != len(batch):
        raise InvalidPriceResponse(f"price payload has {len(items)} items for {len(batch)} offers")
    by_key: dict[int, PriceBrief] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InvalidPriceResponse("price item must be an object")
        offer_key = item.get("offer_key")
        if isinstance(offer_key, bool) or not isinstance(offer_key, int):
            raise InvalidPriceResponse("price item missing integer offer_key")
        if not 0 <= offer_key < len(batch):
            raise InvalidPriceResponse(f"price item references unknown offer_key {offer_key!r}")
        if offer_key in by_key:
            raise InvalidPriceResponse(f"price item duplicates offer_key {offer_key!r}")
        by_key[offer_key] = _price_item(batch[offer_key], item, citations, model=model)
    missing = [index for index in range(len(batch)) if index not in by_key]
    if missing:
        raise InvalidPriceResponse(f"price payload missing offers: {missing}")
    return [by_key[index] for index in range(len(batch))]


def _positive(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPriceResponse(f"{name} must be a number, got {value!r}")
    number = float(value)
    if number <= 0:
        raise InvalidPriceResponse(f"{name} must be positive, got {number!r}")
    return number


def _price_item(
    offer: OfferInput,
    item: dict[str, Any],
    citations: Sequence[str],
    *,
    model: str,
) -> PriceBrief:
    currency = item.get("waluta")
    if currency not in FX_TO_PLN:
        raise InvalidPriceResponse(f"unknown currency {currency!r}")
    days = item.get("dni")
    if isinstance(days, bool) or not isinstance(days, int) or not MIN_DAYS <= days <= MAX_DAYS:
        raise InvalidPriceResponse(f"dni must be an int in {MIN_DAYS}..{MAX_DAYS}, got {days!r}")
    includes = item.get("co_w_cenie")
    if not isinstance(includes, list) or not 1 <= len(includes) <= 4:
        raise InvalidPriceResponse(f"co_w_cenie must be a list of 1-4 items, got {includes!r}")
    if not all(isinstance(entry, str) and entry.strip() for entry in includes):
        raise InvalidPriceResponse(f"co_w_cenie entries must be non-empty strings: {includes!r}")
    sources = item.get("zrodla")
    if not isinstance(sources, list) or not all(isinstance(entry, str) for entry in sources):
        raise InvalidPriceResponse(f"zrodla must be a list of strings, got {sources!r}")
    found = [url for url in sources if _URL_RE.match(url)]
    for url in citations:
        if url not in found:
            found.append(url)
    rationale = item.get("uzasadnienie")
    if not isinstance(rationale, str) or not rationale.strip():
        raise InvalidPriceResponse("uzasadnienie must be a non-empty string")
    return PriceBrief(
        key=offer.key,
        amount=_positive("kwota", item.get("kwota")),
        currency=currency,
        days=days,
        hourly_min=_positive("stawka_min", item.get("stawka_min")),
        hourly_max=_positive("stawka_max", item.get("stawka_max")),
        includes=tuple(entry.strip() for entry in includes),
        sources=tuple(found),
        rationale=rationale.strip()[:600],
        model=model,
    )


def price_offers(
    offers: Sequence[OfferInput],
    *,
    pricer: OpenRouterPricer | None = None,
) -> list[PriceBrief]:
    """Wygodny skrót: jedna partia, domyślny wyceniający."""
    return (pricer or OpenRouterPricer()).price(list(offers))


def price_best_effort(
    offers: Sequence[OfferInput],
    *,
    pricer: OpenRouterPricer | None = None,
) -> tuple[list[PriceBrief], dict[str, str]]:
    """Wycena partii, która nie wywala całej listy przez jedną ofertę.

    Powód (awaria 2026-10-09): model zwrócił w jednej partii ``kwota: 0`` i walidacja
    podniosła :class:`InvalidPriceResponse`, więc :func:`price` rzucił wyjątek i cała
    rolling lista nie powstała — Jan nie dostał nic, choć pozostałe oferty były wycenione.
    Tutaj nieudana partia idzie jeszcze raz pojedynczo (mniejszy kontekst = mniej okazji
    do zepsucia JSON-a), a oferty, których mimo to nie da się wycenić, są zwracane jako
    ``failures`` (klucz oferty → powód). Takiej ofercie nie wpisujemy kwoty z sufitu:
    zostaje ze statusem ``new`` na kolejny przebieg.

    Zwraca ``(briefs, failures)``; ``briefs`` pokrywa wyłącznie oferty wycenione.
    """
    batch = list(offers)
    if not batch:
        return [], {}
    worker = pricer or OpenRouterPricer()
    try:
        briefs = worker.price(batch)
    except PriceError as exc:
        first_error = str(exc)
    else:
        missing = [offer.key for offer in batch if offer.key not in {b.key for b in briefs}]
        if not missing:
            return list(briefs), {}
        first_error = f"brak wyceny w odpowiedzi dla: {', '.join(missing)}"

    briefs = []
    failures: dict[str, str] = {}
    for offer in batch:
        try:
            briefs.extend(worker.price([offer]))
        except PriceError as exc:
            failures[offer.key] = str(exc)
    for key in [offer.key for offer in batch if offer.key not in {b.key for b in briefs}]:
        failures.setdefault(key, first_error)
    return briefs, failures


@dataclass(frozen=True, slots=True)
class PricedDraft:
    """Draft po wstawieniu kwoty: gotowa wiadomość plus dowód wyceny."""

    draft: ProposalDraft
    brief: PriceBrief
    text: str
    problems: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.draft.key

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def note(self) -> str:
        return summarise(self.problems)


def price_drafts(
    drafts: Sequence[ProposalDraft],
    briefs: Sequence[PriceBrief],
    *,
    contexts: Sequence[str] = (),
) -> list[PricedDraft]:
    """Wstaw każdemu draftowi jego kwotę i sprawdź całość w trybie z ceną.

    Drafty bez wyceny zostają nietknięte (zwracane jako ``PricedDraft`` z markerem i problemem
    „brak wyceny” — nigdy nie zgadujemy kwoty). Kolejność wyniku = kolejność draftów.
    """
    by_key = {brief.key: brief for brief in briefs}
    out: list[PricedDraft] = []
    for index, draft in enumerate(drafts):
        brief = by_key.get(draft.key)
        context = contexts[index] if index < len(contexts) else ""
        if brief is None:
            out.append(
                PricedDraft(
                    draft=draft,
                    brief=_MISSING_BRIEF(draft.key),
                    text=draft.text,
                    problems=tuple(draft.problems) + ("brak wyceny dla tej oferty",),
                )
            )
            continue
        text = apply_price(draft.text, brief)
        out.append(
            PricedDraft(
                draft=draft,
                brief=brief,
                text=text,
                problems=price_problems(text, brief, context=context),
            )
        )
    return out


__all__ = [
    "CURRENCY_SYMBOL",
    "DEFAULT_PRICER_MODEL",
    "FX_TO_PLN",
    "MIN_HOURLY_PLN",
    "InvalidPriceResponse",
    "OpenRouterPricer",
    "PriceBrief",
    "PriceError",
    "PricedDraft",
    "apply_price",
    "build_fix_prompt",
    "build_price_prompt",
    "price_best_effort",
    "price_drafts",
    "price_offers",
    "price_paragraph",
    "price_problems",
]
