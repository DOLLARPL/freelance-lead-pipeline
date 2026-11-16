from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from typing import Any

from freelance_pipeline.models import JobLead
from freelance_pipeline.portfolio import render_portfolio_block
from freelance_pipeline.scoring import (
    MAX_GAP_LEN,
    MAX_RATIONALE_LEN,
    ScoreBucket,
    ScoredLead,
    apply_user_policy,
)

DEFAULT_MODEL = "openrouter/auto"  # model selection delegated to the account's defaults
DEFAULT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
ENV_API_KEY = "OPENROUTER_API_KEY"

_SYSTEM_PROMPT = (
    "Jesteś selektorem zleceń dla Jana, niezależnego specjalisty automatyzacji "
    "(n8n, Make, Python, API, integracje, vibe-coding, strony i aplikacje webowe, "
    "chatboty). Oceniasz, czy ogłoszenie jest REALNYM ZLECENIEM OD KLIENTA, które Jan "
    "wykona jako freelancer, a nie, czy technologia pasuje do jego stacku.\n"
    "ZAKRES ZLECENIA (definicja, nie przykłady): strony internetowe i landing page, "
    "aplikacje webowe, integracje i podłączanie narzędzi do siebie, chatboty i agenci AI, "
    "porządkowanie, migracja i deduplikacja danych, ręczna praca powtarzalna do "
    "zastąpienia, a także konfiguracja i wdrożenie narzędzi, z których klient już "
    "korzysta (hosting, domena, poczta, generator stron, publikowanie zmian na bieżąco). "
    "BRAK SŁOWA „automatyzacja” NIE OBNIŻA OCENY i nie jest podstawą odrzucenia: zlecenie "
    "napisane po polsku prostym językiem, które opisuje konkretny efekt, jest dokładnie "
    "tym, czego szukamy. Nie oceniaj po tytule kategorii ani po tym, jak zadanie nazwał "
    "klient („konfiguracja”, „wdrożenie”, „poprawki na stronie”, „pomoc z narzędziem” to "
    "normalne zlecenia, jeśli mają konkretny zakres i efekt).\n"
    "TOP: klient wie, co chce, opisuje konkretny proces lub efekt do osiągnięcia, "
    "a realizacja jest wykonalna narzędziami Jana.\n"
    "MOŻE: prawdopodobnie zlecenie od klienta, ale zakres jest mglisty, technologia "
    "niepewna albo skala/budżet poza zasięgiem.\n"
    "ZAMIEN: klient szuka CZŁOWIEKA (pracownika, asystenta, osoby na etat, na godziny "
    "albo na umowę zlecenie) do pracy powtarzalnej, którą da się zastąpić automatyzacją: "
    "przepisywanie danych z faktur lub z maili, przepisywanie danych między systemami, "
    "obsługa zgłoszeń i skrzynki mailowej, wystawianie dokumentów, wypełnianie arkuszy, "
    "aktualizacja cenników lub stanów magazynowych, raportowanie cykliczne. To nie jest "
    "zlecenie projektowe, więc nie daj TOP ani MOŻE: daj ZAMIEN i w uzasadnieniu napisz "
    "jednym zdaniem, co dokładnie da się zastąpić automatyzacją. Jeśli klient prosi "
    "o narzędzie, wdrożenie albo automatyzację, a nie o osobę, to zwykłe zlecenie: "
    "TOP/MOŻE, nigdy ZAMIEN.\n"
    "ODRZUĆ wszystko inne, w szczególności:\n"
    "1) Ogłoszenia pisane głosem wykonawcy lub agencji, które reklamują WŁASNE usługi "
    "automatyzacji: „[For Hire]”, „available for hire”, „I build…”, „we help businesses "
    "automate…”, „oferuję usługi”, „szukam zleceń”, cennik własnych prac, „certified / "
    "platinum partner”, software house, zaproszenia do partnerstwa. Zero tolerancji "
    "nawet gdy technologia pasuje idealnie.\n"
    "TEST GŁOSU (stosuj do każdego ogłoszenia): kto tu jest sprzedawcą? Jeśli autor "
    "przedstawia SIEBIE jako wykonawcę i oferuje czytelnikom swoje prace („I take on…”, "
    "„I run / I'm the founder of…”, „I build scenarios for clients…”, „jak pracuję”, "
    "pakiety, ceny, dostępność, „napisz do mnie”) to ODRZUĆ. Zlecenie od klienta "
    "brzmi odwrotnie: autor prosi o wykonanie czegoś DLA NIEGO („szukam kogoś”, "
    "„potrzebuję”, „we're looking for”, „I'm stuck and happy to pay”).\n"
    "WAŻNE: jeśli autor szuka kogoś do współpracy i ma budżet (agencja, software house, "
    "„konsulting szukający wykonawcy na projekty), to jest KLIENT, nie reklama. Nie "
    "odrzucaj tylko dlatego, że w ogłoszeniu pojawia się słowo „agencja”.\n"
    "WAŻNE: klient, który zleca AUDYT, PRZEGLĄD albo ANALIZĘ, to ZLECENIE — Jan ma audyt "
    "w cenniku. Sygnały: „sprawdźcie, gdzie tracimy pieniądze”, „przejrzyjcie nasze konto/"
    "dane/procesy i powiedzcie, co poprawić”, „audyt zakończony konkretnymi wnioskami”, "
    "„potrzebuję kogoś, kto to przeanalizuje”. To jednorazowa praca z wnioskami, wykonalna "
    "przez analizę danych i integracje. ODRZUCAMY tylko PRZEWLEKŁE PROWADZENIE cudzej "
    "sprzedaży, kampanii albo contentu (praca cykliczna za klienta), nie jednorazową "
    "analizę jego własnych danych.\n"
    "2) Etaty i stanowiska: Senior/Sr/Lead/Principal/Staff/Head/Director/Chief/Expert/"
    "Architect, full-time, DevOps, administracja IT, QA/testerzy, data engineering, "
    "ogólne programowanie i korporacje (Java/PHP/Laravel/mobile/Flutter/Kotlin).\n"
    "UWAGA na słowo „Hiring”: firma, która szuka KONTRAKTORA do płatnych wdrożeń u jej "
    "klientów („contract implementer”, „freelance engagement”, 1099, rozliczenie za "
    "instalację/projekt), to ZLECENIE, nie odrzucenie. ODRZUĆ tylko etat: pełny etat, "
    "stała pensja, „permanent”, benefity, wyłączność.\n"
    "3) Inne branże bez związku ze zleceniem: grafika, produkcja wideo i montaż, "
    "copywriting, PROWADZENIE kampanii reklamowych i social mediów (media buying, "
    "tworzenie contentu, optymalizacja adsów), sprzedaż, księgowość, Google Ads, "
    "testy penetracyjne, administracja serwerami produkcyjnymi (DevOps, bezpieczeństwo, "
    "utrzymanie infrastruktury). UWAGA: to nie dotyczy klienta, który prosi "
    "o SKONFIGUROWANIE, podłączenie albo naprawienie swojego narzędzia (konto Meta "
    "Business Suite, sklep, formularz, integracja, feed produktowy) — to jest zlecenie "
    "w zakresie („konfiguracja i wdrożenie narzędzi, z których klient już korzysta”), "
    "nawet gdy samo narzędzie służy marketingowi. Odrzucaj PRODUKCJĘ i PROWADZENIE, "
    "nie konfigurację.\n"
    "4) Praca ze sprzętem i natywną warstwą platformy: natywna aplikacja Android/iOS, "
    "drukarki fiskalne, terminale płatnicze, SDK producentów urządzeń. ODRZUĆ, nawet gdy "
    "webowa część systemu pasuje do zakresu; wspólny rdzeń w React/TypeScript tego nie "
    "zmienia.\n"
    "WYJĄTEK — ANALIZA OBRAZU (decyzja Jana 2026-09-21): zleceń z analizy obrazu, "
    "rozpoznawania z kamery, OCR, detekcji na zdjęciach lub pomiarów z wideo NIE "
    "odrzucaj za „poza zakres” — Jan chce się w tym rozwijać. Oceń je normalnie "
    "(TOP, gdy klient ma konkretny efekt do osiągnięcia; MOŻE, gdy zakres jest mglisty) "
    "i w uzasadnieniu napisz jednym zdaniem „analiza obrazu — kierunek rozwojowy”. "
    "Nadal ODRZUĆ, gdy zlecenie wymaga sterowania maszyną albo sprzętem (sterowniki, "
    "Modbus/PLC jako rdzeń, warstwa natywna): wtedy to praca urządzeniowa, nie wizja.\n"
    "TECHNOLOGIA NIE JEST KRYTERIUM. Klient, który opisuje swój problem i oczekiwany "
    "efekt, ale nie zna narzędzi (nie pisze „n8n”, „Make”, „Python”), jest NAJCENNIEJSZY: "
    "to klient, którego nikt inny nie obsłuży. Stack służy tylko do sprawdzenia, czy "
    "zadanie jest wykonalne dla Jana.\n"
    "PIENIĄDZE: TOP wymaga realnej kasy. Stawka godzinowa poniżej ~38 USD/h (≈150 zł/h) to "
    "nie priorytet, a jednorazowa drobna naprawa („fix my workflow” za 30–50 USD, „simple "
    "workflow”, „happy to pay for help”) to MOŻE, nie TOP, nawet gdy technologia pasuje "
    "idealnie. Budżet projektu rzędu 1500+ PLN albo 400+ USD z jasnym zakresem to TOP. "
    "JĘZYK: jeśli ogłoszenie wymaga języka innego niż polski lub angielski (np. „fluent "
    "Spanish is required”, „Deutschkenntnisse”, „biegły niemiecki”), wpisz ODRZUĆ, bo Jan "
    "pracuje po polsku i angielsku i takie zlecenie jest nieosiągalne. W razie wątpliwości, "
    "czy to w ogóle zlecenie, wybieraj ODRZUĆ; nie dawaj MOŻE za samo wystąpienie słowa "
    "Python, AI, API, Make czy automatyzacja. Każdą ofertę oceń merytorycznie i zwróć "
    "WYŁĄCZNIE tablicę JSON."
)

# Few-shot calibration examples: real client commissions (keep) versus provider
# advertisements and employment ads (reject). They are deliberately distinct from
# the rows in ``tests/fixtures/golden_set.jsonl`` so the golden measurement stays
# an honest measurement rather than a memory test.
_FEW_SHOT: tuple[dict[str, str], ...] = (
    {
        "title": "Potrzebuję automatyzacji obiegu faktur w Make",
        "description": (
            "Prowadzę małe biuro rachunkowe. Faktury przychodzą mailem, a my ręcznie "
            "przepisujemy je do arkusza. Chcę, żeby Make robił to automatycznie i wrzucał "
            "dane do Google Sheets, a na koniec wysyłał podsumowanie."
        ),
        "bucket": "TOP",
        "reason": (
            "Zlecenie od klienta z konkretnym procesem (mail -> arkusz -> podsumowanie) "
            "i jasnym efektem; wykonalne w Make."
        ),
    },
    {
        "title": "Automate booking form into our calendar",
        "description": (
            "We run a small rental business. Bookings arrive through a form on our website "
            "and someone re-types them into Google Calendar. We would like the two tools "
            "connected so nothing has to be entered twice."
        ),
        "bucket": "TOP",
        "reason": "Klient opisuje własny problem i oczekiwany efekt; integracja przez API jest wykonalna.",
    },
    {
        "title": "Biuro rachunkowe: ręczne przepisywanie faktur — chcemy to zautomatyzować",
        "description": (
            "Nie znamy się na automatyzacjach i nie wiemy, jak to ugryźć. Codziennie ktoś "
            "przepisuje dane z faktur do arkusza, zajmuje to 2 godziny. Chcemy, żeby działo "
            "się to samo, a my mieli tylko kontrolę. Budżet mamy na to zaplanowany."
        ),
        "bucket": "TOP",
        "reason": (
            "Klient z realnym problemem i budżetem, bez nazw narzędzi — dokładnie taki "
            "klient jest najcenniejszy; wykonalne bez jego wiedzy technicznej."
        ),
    },
    {
        "title": "Looking for an n8n builder to work with our implementation agency, project by project",
        "description": (
            "We are a small AI implementation agency. n8n is core to our stack and our "
            "workload keeps growing. We are looking for an experienced n8n builder to work "
            "with us on an ongoing, project-by-project basis, with paid scoped tasks."
        ),
        "bucket": "TOP",
        "reason": (
            "Agencja jest tu KUPUJĄCYM — szuka wykonawcy i płaci za zadania. To zlecenie, "
            "nie reklama usług; słowo „agencja” samo w sobie niczego nie przekreśla."
        ),
    },
    {
        "title": "Chatbot na stronie gabinetu stomatologicznego",
        "description": (
            "Chciałbym chatbota, który odpowie pacjentom na powtarzające się pytania "
            "(cennik, godziny, przygotowanie do zabiegu) i zbierze dane do kontaktu, "
            "gdy gabinet jest zamknięty."
        ),
        "bucket": "TOP",
        "reason": "Konkretne zlecenie od klienta: zakres i cel są jasne, realne do wykonania jako chatbot.",
    },
    {
        "title": "Odświeżenie strony firmowej i formularz zgłoszeń do arkusza",
        "description": (
            "Mamy małą firmę remontową. Strona jest stara, a formularz wysyła zgłoszenia "
            "na maila, którego nikt nie pilnuje. Chcemy, żeby zgłoszenia trafiały do "
            "arkusza i dawały powiadomienie, i żebyśmy sami mogli dopisywać aktualności "
            "bez dzwonienia do znajomego, który robił stronę."
        ),
        "bucket": "TOP",
        "reason": (
            "Zlecenie od klienta z konkretnym efektem (formularz do arkusza, powiadomienia, "
            "samodzielna publikacja) i bez żargonu; strona z integracją jest w zakresie, "
            "słowo „automatyzacja” nie musi się pojawić."
        ),
    },
    {
        "title": "Potrzebuję pomocy z ChatGPT w firmie, nie znam się na tym",
        "description": (
            "Prowadzę biuro nieruchomości. Chcę, żeby pracownicy pytali o nasze procedury "
            "i cenniki w jednym miejscu, zamiast dzwonić do biura, i żeby ktoś to poskładał "
            "oraz pokazał nam, jak z tego korzystać. Budżet mamy zaplanowany."
        ),
        "bucket": "TOP",
        "reason": (
            "Klient nietechniczny z jasnym efektem, bez nazw narzędzi i bez wiedzy "
            "technicznej; wdrożenie i konfiguracja narzędzia są w zakresie."
        ),
    },
    {
        "title": "Szukam osoby do przepisywania danych z faktur do programu księgowego",
        "description": (
            "Poszukuję pracownika na kilka godzin dziennie. Codziennie przychodzi około "
            "40 faktur na maila i trzeba je przepisywać z faktur do naszego programu "
            "księgowego oraz do arkusza z rozliczeniami. Praca jest stała i powtarzalna, "
            "wcześniej zatrudniałem studenta na umowę zlecenie."
        ),
        "bucket": "ZAMIEN",
        "reason": (
            "Klient szuka człowieka do pracy powtarzalnej (przepisywanie danych z faktur), "
            "którą da się zastąpić automatyzacją; to nie jest zlecenie projektowe, więc "
            "nie TOP i nie MOŻE."
        ),
    },
    {
        "title": "[For Hire] I fix broken n8n workflows — $50 per fault",
        "description": (
            "Hi everyone, I am an n8n developer. I troubleshoot failing nodes, webhooks and "
            "data mappings. Fixed $50 per fault, you get the corrected workflow plus a change "
            "note. Reach out and send your scenario."
        ),
        "bucket": "ODRZUĆ",
        "reason": (
            "Wykonawca reklamuje własne usługi automatyzacji („[For Hire]”, cennik własnej "
            "pracy) — to nie jest zlecenie od klienta."
        ),
    },
    {
        "title": "Automatyzacje n8n i Make dla firm — kompleksowe wdrożenia",
        "description": (
            "Oferuję usługi automatyzacji procesów. Buduję workflow w n8n i Make, integruję "
            "CRM, arkusze i API. Szukam firm, które chcą uciec od ręcznej pracy."
        ),
        "bucket": "ODRZUĆ",
        "reason": "Ogłoszenie w głosie wykonawcy („oferuję usługi”, „szukam klientów”), a nie zlecenie.",
    },
    {
        "title": "Certified Make Partner — help with complex scenarios and API integrations",
        "description": (
            "We are an automation studio. We have been building on Make since the Integromat "
            "days and we help businesses tackle complex scenarios, custom apps and API work. "
            "Get in touch to discuss your automation."
        ),
        "bucket": "ODRZUĆ",
        "reason": "Agencja/partner reklamuje własne usługi; ogłoszenie nie zawiera zlecenia do wykonania.",
    },
    {
        "title": "Small Make workflow fix — US$30 fixed, I run a small automation lab",
        "description": (
            "I take one bounded Make workflow problem at a time: broken scenario, webhook "
            "or API connection, Google Sheets mapping. I run a small automation lab, so you "
            "know exactly who you are hiring. Message me with your scenario and I confirm "
            "scope and delivery time."
        ),
        "bucket": "ODRZUĆ",
        "reason": (
            "TEST GŁOSU: autorem jest sprzedawca („I run…”, „I take on…”, „message me”), "
            "który oferuje własne usługi — nie ma tu zlecenia dla Jana."
        ),
    },
    {
        "title": "Make.com automation for SMEs | Make, AI, APIs, Shopify",
        "description": (
            "I'm the founder of a small automation company near Zurich. I build and maintain "
            "Make scenarios for small and mid-sized businesses: order flows, Shopify "
            "integrations and API work. Availability and packages on request."
        ),
        "bucket": "ODRZUĆ",
        "reason": "Pierwszoosobowa oferta własnych usług („I build…”, founder, pakiety), nie zlecenie od klienta.",
    },
    {
        "title": "Small-business workflow automation, integrations & practical AI",
        "description": (
            "Hi everyone, I'm the founder of a small automation studio. I help small "
            "businesses untangle workflows that have grown into a mix of spreadsheets, "
            "mailboxes and half-finished automations. Packages and scope on request."
        ),
        "bucket": "ODRZUĆ",
        "reason": (
            "Pierwszoosobowa prezentacja własnej oferty („I help businesses”, „packages on "
            "request”) — wykonawca szuka klientów, to nie jest zlecenie."
        ),
    },
    {
        "title": "Hiring: Automation Developer (n8n, Make.com) — full-time, remote",
        "description": (
            "Our company is hiring a full-time Automation Developer to maintain internal "
            "workflows. Long-term engagement, monthly salary, required 3+ years of experience."
        ),
        "bucket": "ODRZUĆ",
        "reason": "Etaty i rekrutacja na stanowisko, a nie zlecenie dla freelancera.",
    },
    {
        "title": "Hiring a contract implementer: install our product for clients, paid per install",
        "description": (
            "We need a contract implementer (remote, 1099). You install our engine for each "
            "new client against a written runbook and keep it running, plus ongoing support "
            "inside a monthly cap. Paid per installation, invoiced monthly."
        ),
        "bucket": "TOP",
        "reason": (
            "To ZLECENIE, nie etat: płatność za instalację, rozliczenie kontraktowe, praca "
            "projektowa u klientów firmy. Samo słowo „Hiring” niczego nie przekreśla."
        ),
    },
)

# pylint-friendly text of the output contract embedded in every request.
_OUTPUT_CONTRACT = (
    "Odpowiedz wyłącznie tablicą JSON, bez komentarzy, bez markdowna: "
    '[{"lead_key": "<klucz_z_zapytania>", "score": <int 0-100>, '
    '"bucket": "TOP"|"MOŻE"|"ODRZUĆ"|"ZAMIEN", "reason": "<krótkie uzasadnienie, max ~60 słów, po polsku>", '
    '"portfolio_gap": "<nazwa brakującej kompetencji, max 8 słów, albo null>", ...] '
    "Dla każdego z podanych ogłoszeń; każdy obiekt musi zawierać lead_key z zapytania. "
    "ZAMIEN stosuj tylko wtedy, gdy klient szuka osoby do pracy powtarzalnej, którą da się "
    "zastąpić automatyzacją (nie jest to zlecenie projektowe); w reason napisz jednym zdaniem, "
    "co da się zastąpić. "
    "portfolio_gap to NAZWA BRAKUJĄCEJ KOMPETENCJI albo TECHNOLOGII, nie temat zlecenia: np. "
    '"SAP / ERP", "aplikacje mobilne (Flutter)", "telefonia głosowa (Twilio)". '
    "Wypełniaj TYLKO gdy klient wprost wymaga umiejętności, której nie ma w podanym portfolio "
    "(sam temat zlecenia, nazwy systemów klienta czy branża NIE są luką). Jeśli wszystko, czego "
    "wymaga zlecenie, pokrywa portfolio, wpisz null."
)


class ScoreError(Exception):
    """A scoring failure; leads remain retryable (nothing is ever persisted)."""


class InvalidScoreResponse(ScoreError):
    """The model's payload violated the strict output contract."""


def _rate_row(lead: JobLead) -> dict[str, object] | None:
    """The source's budget pill, when it exists, so the model can judge money."""
    data = lead.raw_data or {}
    unit = data.get("rate_unit")
    if unit not in {"hour", "project"}:
        return None
    return {
        "min": data.get("rate_min"),
        "max": data.get("rate_max"),
        "unit": unit,
        "currency": lead.currency,
    }


def build_score_prompt(leads: Sequence[JobLead]) -> str:
    """One prompt carrying the few-shot examples and every lead.

    Each lead is keyed by its position (``lead_key``) so the model's response can
    be mapped back without trusting any model-supplied identifier.
    """
    rows = [
        {
            "lead_key": index,
            "source_id": lead.source_id,
            "source": lead.source,
            "title": lead.title,
            "company_or_author": lead.company_or_author,
            "budget": lead.budget,
            "currency": lead.currency,
            "rate": _rate_row(lead),
            "skills": list(lead.skills),
            "description": (lead.description or "")[:1200],
        }
        for index, lead in enumerate(leads)
    ]
    return (
        "Przykłady poprawnych ocen (few-shot — NIE oceniaj ich ponownie i NIE zwracaj "
        "ich w odpowiedzi, nie mają lead_key):\n"
        + json.dumps(list(_FEW_SHOT), ensure_ascii=False, indent=1)
        + "\n\n"
        + render_portfolio_block()
        + "\n\n"
        + _OUTPUT_CONTRACT
        + "\n\nZapytanie:\n"
        + json.dumps(rows, ensure_ascii=False)
    )


class OpenRouterScorer:
    """Batch scorer calling an OpenRouter chat-completions model.

    The model must return a JSON array (one object per input lead) whose only
    legal fields are ``source_id``, ``score`` (int 0-100), ``bucket``
    (TOP/MOŻE/ODRZUĆ) and ``reason`` (non-empty, bounded). Anything else —
    markdown fences, wrong count, unknown ids, out-of-range/non-integral scores,
    unknown buckets, blank reasons — is rejected loudly via ``InvalidScoreResponse``
    so a misbehaving model can never silently drop or fabricate a lead. On any
    failure the caller simply leaves the candidates uncommitted (retryable).

    ``api_key`` may be a literal string or a zero-arg callable; otherwise the
    ``OPENROUTER_API_KEY`` environment variable is used. The key is never
    logged or echoed in errors.
    """

    def __init__(
        self,
        *,
        model: str = "openrouter/auto",
        api_key: str | Callable[[], str | None] | None = None,
        base_url: str = DEFAULT_COMPLETIONS_URL,
        timeout: float = 60.0,
        batch_size: int = 10,
        max_attempts: int = 2,
        session: Any | None = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self._api_key = api_key
        self._session = session

    def _resolve_api_key(self) -> str | None:
        if callable(self._api_key):
            return self._api_key()
        if isinstance(self._api_key, str) and self._api_key:
            return self._api_key
        return os.environ.get(ENV_API_KEY)

    def score(self, leads: Sequence[JobLead]) -> list[ScoredLead]:
        if not leads:
            return []
        api_key = self._resolve_api_key()
        if not api_key:
            raise ScoreError(f"{ENV_API_KEY} is not configured; scoring skipped")
        session = self._session
        if session is None:  # pragma: no cover - exercised only in production
            import requests

            session = requests.Session()
        scored: list[ScoredLead] = []
        for start in range(0, len(leads), self.batch_size):
            batch = list(leads[start : start + self.batch_size])
            scored.extend(self._score_batch(batch, session, api_key))
        return apply_user_policy(scored)

    def _score_batch(
        self, batch: Sequence[JobLead], session: Any, api_key: str
    ) -> list[ScoredLead]:
        """Score one batch, retrying a malformed payload before giving up.

        A single truncated response used to lose the whole run (observed live
        2026-09-19). The payload is never accepted unless it matches this batch
        exactly, so a retry can only help; every attempt re-validates strictly.
        """
        last_error: InvalidScoreResponse | None = None
        for _ in range(self.max_attempts):
            try:
                return self._request_batch(batch, session, api_key)
            except InvalidScoreResponse as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _request_batch(
        self, batch: Sequence[JobLead], session: Any, api_key: str
    ) -> list[ScoredLead]:
        prompt = build_score_prompt(batch)
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
                            "name": "lead_scores",
                            "strict": True,
                            "schema": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "lead_key": {"type": "integer", "minimum": 0},
                                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                                        "bucket": {"type": "string", "enum": ["TOP", "MOŻE", "ODRZUĆ", "ZAMIEN"]},
                                        "reason": {"type": "string"},
                                        "portfolio_gap": {"type": ["string", "null"]},
                                    },
                                    "required": ["lead_key", "score", "bucket", "reason", "portfolio_gap"],
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
            raise ScoreError(f"OpenRouter request failed: {exc}") from exc
        status_code = getattr(response, "status_code", None)
        if status_code is not None and status_code >= 400:
            raise ScoreError(f"OpenRouter HTTP {status_code}")
        try:
            payload_text = response.text
        except Exception as exc:
            raise ScoreError(f"OpenRouter response was unreadable: {exc}") from exc
        content = _extract_content(payload_text)
        return _parse_content(content, batch)


def _extract_content(payload_text: str) -> str:
    try:
        envelope = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise InvalidScoreResponse("OpenRouter envelope is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise InvalidScoreResponse("OpenRouter envelope must be an object")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InvalidScoreResponse("OpenRouter envelope has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise InvalidScoreResponse("OpenRouter choice has no message object")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise InvalidScoreResponse("OpenRouter message content is empty")
    return content


def _parse_content(content: str, leads: Sequence[JobLead]) -> list[ScoredLead]:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 : -3].strip()
    try:
        items = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InvalidScoreResponse("scored payload is not valid JSON") from exc
    if not isinstance(items, list):
        raise InvalidScoreResponse("scored payload must be a JSON array")
    if len(items) != len(leads):
        raise InvalidScoreResponse(
            f"scored payload has {len(items)} items for {len(leads)} leads"
        )
    by_key: dict[int, ScoredLead] = {}
    for item in items:
        if not isinstance(item, dict):
            raise InvalidScoreResponse("scored item must be an object")
        lead_key = item.get("lead_key")
        if isinstance(lead_key, bool) or not isinstance(lead_key, int):
            raise InvalidScoreResponse("scored item missing integer lead_key")
        if not 0 <= lead_key < len(leads):
            raise InvalidScoreResponse(f"scored item references unknown lead_key {lead_key!r}")
        if lead_key in by_key:
            raise InvalidScoreResponse(f"scored item duplicates lead_key {lead_key!r}")
        by_key[lead_key] = _scored_item(leads[lead_key], item)
    missing = [index for index in range(len(leads)) if index not in by_key]
    if missing:
        raise InvalidScoreResponse(f"scored payload missing leads: {missing}")
    return [by_key[index] for index in range(len(leads))]


def _scored_item(lead: JobLead, item: dict[str, Any]) -> ScoredLead:
    raw_score = item.get("score")
    if isinstance(raw_score, bool):
        raise InvalidScoreResponse(f"score must be a number, got {raw_score!r}")
    if not isinstance(raw_score, (int, float)):
        raise InvalidScoreResponse(f"score must be an int, got {raw_score!r}")
    if isinstance(raw_score, float):
        if not raw_score.is_integer():
            raise InvalidScoreResponse(f"score must be an integer, got {raw_score!r}")
        score = int(raw_score)
    else:
        score = raw_score
    if not 0 <= score <= 100:
        raise InvalidScoreResponse(f"score {score} out of 0..100")
    bucket_raw = item.get("bucket")
    bucket = {b.value: b for b in ScoreBucket}.get(bucket_raw)
    if bucket is None:
        raise InvalidScoreResponse(f"unknown bucket {bucket_raw!r}")
    rationale = item.get("reason")
    if not isinstance(rationale, str) or not rationale.strip():
        raise InvalidScoreResponse("reason must be a non-empty string")
    rationale = rationale.strip()
    if len(rationale) > MAX_RATIONALE_LEN:
        raise InvalidScoreResponse(
            f"reason exceeds {MAX_RATIONALE_LEN} chars ({len(rationale)})"
        )
    gap_raw = item.get("portfolio_gap")
    gap: str | None = None
    if gap_raw is not None:
        if not isinstance(gap_raw, str):
            raise InvalidScoreResponse(f"portfolio_gap must be a string or null, got {gap_raw!r}")
        gap = gap_raw.strip() or None
        if gap is not None and len(gap) > MAX_GAP_LEN:
            raise InvalidScoreResponse(
                f"portfolio_gap exceeds {MAX_GAP_LEN} chars ({len(gap)})"
            )
    return ScoredLead(lead, score, bucket, rationale, gap)


__all__ = [
    "DEFAULT_COMPLETIONS_URL",
    "InvalidScoreResponse",
    "OpenRouterScorer",
    "ScoreError",
]