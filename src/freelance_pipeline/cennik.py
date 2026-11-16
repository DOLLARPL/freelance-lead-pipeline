"""Cennik JNX: jedyne źródło prawdy o kwotach (decyzja Jana, 2026-10-28).

Ten moduł czyta ``config/cennik.yaml`` i zamienia treść ogłoszenia na konkretną pozycję
cennika: usługę, poziom, kwotę i termin. Zero modelu, zero wyszukiwania w sieci, zero
zgadywania: jeśli cennik nie ma dopasowania, zwracamy ``None`` i wołający decyduje, co dalej.

Dlaczego osobno od :mod:`freelance_pipeline.pricing`: tamten moduł wycenia z żywego researchu
rynkowego i wolno mu się mylić (to była decyzja z 2026-10-07). Tutaj jest odwrotnie: kwota
jest stała, wersjonowana i edytowalna przez Jana w YAML-u, a wycena jest powtarzalna. Cennik
wygrał z researchem 28.10, bo w liście z crona integracja wyszła 7 500 zł, gdy cennik mówi
4 500 zł za 2 do 3 procesów.

Kształt pliku opisuje ``config/cennik.yaml``; ten moduł zna tylko to, co tam jest:
``priced_as`` (``fixed``/``from``/``quote``), ``levels`` (start/standard/premium) z ``price``,
opcjonalnym ``days`` i listą ``includes``, oraz ``market`` z ceną rynkową netto.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from freelance_pipeline.pricing import CURRENCY_SYMBOL, PriceBrief

#: Domyślna ścieżka cennika (repo/config/cennik.yaml).
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "cennik.yaml"
#: Godziny pracy per usługa i poziom (repo/config/czas.yaml); opcjonalne, ale wtedy wycena
#: wie, ile realnie pracy stoi za kwotą.
DEFAULT_CZAS_PATH = Path(__file__).resolve().parents[2] / "config" / "czas.yaml"

#: Kolejność poziomów od najtańszego; ``base`` w YAML-u to cena poziomu ``start``.
LEVELS = ("start", "standard", "premium")

#: Godziny pracy w dniu roboczym, ta sama konwencja co w :mod:`pricing` (próg 90 zł/h).
DAILY_HOURS = 6.0
#: Próg opłacalności z kontraktu (skill ``odpowiedz-na-oferte``).
FLOOR_HOURLY_PLN = 90.0
#: Widełki stawek rynkowych używane w opisie wyceny (research 2026-10, sekcja 2).
MARKET_HOURLY = (90.0, 180.0)

#: Dopasowanie usługi po treści ogłoszenia. Kolejność ma znaczenie: pierwsze trafienie
#: wygrywa, dlatego wąskie produkty (audyt, szkolenie, sklep, onepager) stoją przed
#: ogólnymi (strona firmowa, integracja).
SERVICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("audyt_ai", re.compile(r"\baudyt\w*", re.IGNORECASE)),
    ("szkolenie_ai", re.compile(r"\bszkoleni\w*|\bwarsztat\w*|\bkurs\w*", re.IGNORECASE)),
    ("integracja", re.compile(
        r"\bintegracj\w*|\bapi\b|\bn8n\b|\bmake\.com\b|\bwebhook\w*|\bzapier\b|"
        r"\bautomatyzac\w*|\bpodłącz\w*|\bsynchronizac\w*",
        re.IGNORECASE,
    )),
    ("agent_ai", re.compile(
        r"\bchatbot\w*|\bbot\w*|\bagent\w* ai\b|\basystent\w* ai\b|\brag\b|"
        r"\bbaza wiedzy\b|\bsztuczn\w* inteligencj\w*",
        re.IGNORECASE,
    )),
    ("onepager", re.compile(r"\bonepager\w*|\bone[\s-]?page\w*|\blanding\w*", re.IGNORECASE)),
    ("wizytowka", re.compile(r"\bwizytówk\w*|\bwizytowk\w*", re.IGNORECASE)),
    ("sklep", re.compile(
        r"\bsklep\w*|\bwoocommerce\b|\bprestashop\b|\bshoper\b|\be-?commerce\b|"
        r"\bkoszyk\w*|\bprodukt\w* w sklepie",
        re.IGNORECASE,
    )),
    ("strona_firmowa", re.compile(
        r"\bstrona firmow\w*|\bstron\w* internetow\w*|\bwww\b|\bpodstron\w*|"
        r"\bwordpress\w*|\bstrona\b",
        re.IGNORECASE,
    )),
)

#: Drobna naprawa to nie pakiet. „Przywrócenie przejścia ze sklepu…” z budżetem 200 zł
#: dostawało w przebiegu kontrolnym 8 000 zł, czyli pakiet integracji premium. Takie zlecenia
#: zostają poza cennikiem (wycenia je model) do czasu, aż cennik dostanie pozycję na poprawki.
REPAIR_RE = re.compile(
    r"\bnapraw\w*|\bprzywróc\w*|\bpopraw\w*|\bbłęd\w*|\bnie działa\b|\bfix\b",
    re.IGNORECASE,
)
#: Słowa, które mimo naprawy znaczą jednak projekt integracyjny.
INTEGRATION_SCOPE_RE = re.compile(
    r"\bintegracj\w*|\bautomatyzac\w*|\bn8n\b|\bmake\.com\b|\bapi\b|\bwebhook\w*",
    re.IGNORECASE,
)

#: Budowa własnej aplikacji (panel, PWA, CRM, system rezerwacji) to nie pozycja cennika:
#: research nie ma na to obserwacji, a wpychanie jej w „integrację” dawało 2 500 zł za system
#: dla 15 monterów (przebieg kontrolny 28.10). Takie zlecenia idą do wyceny modelem.
APP_BUILD_RE = re.compile(
    r"\baplikacj\w*|\bpanel\w*|\bsystem\w*|\bprogram\w*|\bcrm\b|\bpwa\b|"
    r"\brezerwacj\w*|\bplatform\w*",
    re.IGNORECASE,
)

#: Tytuł mówiący wprost o integracji wygrywa z resztą dopasowań (patrz ``detect_service``).
INTEGRATION_TITLE = re.compile(r"\bintegracj\w*", re.IGNORECASE)

#: Sygnały wyższego poziomu. Świadomie zachowawcze: domyślnie bierzemy ``start``
#: (najniższą cenę z cennika), bo zaniżona wycena jest tańsza w naprawie niż zawyżona.
PREMIUM_SIGNS = re.compile(
    # Tylko słowa o ZAKRESIE produktu. „Panel administracyjny” i „analityka” wypadły, bo
    # pojawiają się w opisie funkcji zwykłej integracji i windowały ją do poziomu premium
    # (przebieg kontrolny 28.10: integracja Algolii wskoczyła na 8 000 zł).
    r"\bpremium\b|\bwiele kanałów\b|\bwielojęzyczn\w*|\brag\b|"
    r"\b2 rundy\b|\bdwie rundy\b",
    re.IGNORECASE,
)
STANDARD_SIGNS = re.compile(
    r"\bstandard\w*|\bkilka proces\w*|\b2 do 3 proces\w*|\bdwa proces\w*|"
    r"\b5 podstron\b|\bpięć podstron\b|\b6 do 8 sekcji\b|\bkilka sekcji\b|"
    r"\bseo\b|\bcrm\b|\bkalendarz\w*|\bkilka kanałów\b|\bautocomplete\b|"
    r"\bfiltr\w*|\bsortowan\w*|\bliterówk\w*|\bsynonim\w*|\bwebhook\w*",
    re.IGNORECASE,
)

#: Termin domyślny, gdy poziom w cenniku nie ma ``days`` (z researchu, sekcja 3).
DEFAULT_DAYS = {
    "wizytowka": 3,
    "onepager": 5,
    "strona_firmowa": 7,
    "sklep": 14,
    "integracja": 10,
    "agent_ai": 14,
    "audyt_ai": 5,
    "szkolenie_ai": 1,
}


#: „jak Standard”, „jak start”: skrót w cenniku do pozycji innego poziomu.
_LIKE_LEVEL = re.compile(r"^jak\s+(start|standard|premium)\b[\s,+i]*", re.IGNORECASE)


class CennikError(Exception):
    """Cennik nieczytelny albo niezgodny z kształtem opisanym w ``config/cennik.yaml``."""


@dataclass(frozen=True, slots=True)
class Quote:
    """Wycena jednej oferty prosto z cennika."""

    service: str
    level: str
    amount: float
    days: int
    includes: tuple[str, ...]
    priced_as: str
    version: str
    #: Realna praca w godzinach z ``config/czas.yaml`` (``None``, gdy pliku brak).
    hours: float | None = None

    @property
    def is_from(self) -> bool:
        """``True``, gdy cennik podaje dolną granicę („od”), a nie cenę sztywną."""
        return self.priced_as == "from"

    @property
    def hourly_equivalent(self) -> float:
        return self.amount / (self.days * DAILY_HOURS)


def _require_mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CennikError(f"{where} musi być mapą, jest {type(value).__name__}")
    return value


def _require_price(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise CennikError(f"{where}: cena musi być liczbą większą od zera, jest {value!r}")
    return float(value)


class Cennik:
    """Wczytany i zwalidowany cennik."""

    def __init__(
        self,
        path: str | Path = DEFAULT_PATH,
        *,
        data: dict[str, Any] | None = None,
        czas_path: str | Path | None = DEFAULT_CZAS_PATH,
        czas_data: dict[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        if data is None:
            data = self._load_file()
        self.data = data
        self.version = str(data.get("version") or "")
        if not self.version:
            raise CennikError("cennik bez pola 'version'")
        self.currency = str(data.get("currency") or "PLN").upper()
        self.services = _require_mapping(data.get("services"), "services")
        if not self.services:
            raise CennikError("cennik bez usług")
        for key, raw in self.services.items():
            service = _require_mapping(raw, f"services.{key}")
            levels = _require_mapping(service.get("levels"), f"services.{key}.levels")
            for name in LEVELS:
                if name not in levels:
                    raise CennikError(f"services.{key}.levels bez poziomu '{name}'")
                _require_price(
                    _require_mapping(levels[name], f"services.{key}.levels.{name}").get("price"),
                    f"services.{key}.levels.{name}",
                )
        self.hours_map = self._load_hours(czas_path, czas_data)

    def _load_hours(
        self, czas_path: str | Path | None, czas_data: dict[str, Any] | None
    ) -> dict[str, dict[str, float]]:
        """Godziny pracy z ``config/czas.yaml``; brak pliku nie jest błędem (tylko brak godzin)."""
        if czas_data is None:
            if czas_path is None:
                return {}
            path = Path(czas_path)
            if not path.is_file():
                return {}
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                return {}
            czas_data = loaded if isinstance(loaded, dict) else None
            if czas_data is None:
                return {}
        products = czas_data.get("produkty")
        if not isinstance(products, dict):
            return {}
        hours: dict[str, dict[str, float]] = {}
        for service, levels in products.items():
            if not isinstance(levels, dict):
                continue
            per_level: dict[str, float] = {}
            for level, raw in levels.items():
                if not isinstance(raw, dict):
                    continue
                value = raw.get("hours_total") or raw.get("hours_typical")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    per_level[str(level)] = float(value)
            if per_level:
                hours[str(service)] = per_level
        return hours

    def hours(self, service: str, level: str) -> float | None:
        """Realna praca w godzinach dla usługi i poziomu (``None``, gdy nie ma danych)."""
        return self.hours_map.get(service, {}).get(level)

    def _load_file(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise CennikError(f"brak pliku cennika: {self.path}")
        try:
            payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise CennikError(f"{self.path} nie jest poprawnym YAML-em: {exc}") from exc
        return _require_mapping(payload, str(self.path))

    # ------------------------------------------------------------------ dopasowanie

    @staticmethod
    def service_scores(title: str, description: str = "") -> dict[str, int]:
        """Punkty dopasowania usług: trafienie w tytule waży 3, w treści 1.

        Samo „pierwsze trafienie” myliło produkty: „Integracja wyszukiwarki Algolia ze sklepem
        Shoper” wpadało w ``sklep`` tylko dlatego, że słowo „sklep” stało w tytule. Waga tytułu
        i liczenie trafień rozwiązuje to bez listy wyjątków.
        """
        scores: dict[str, int] = {}
        for service, pattern in SERVICE_PATTERNS:
            hits = len(pattern.findall(title)) * 3 + len(pattern.findall(description))
            if hits:
                scores[service] = hits
        return scores

    def detect_service(self, title: str, description: str = "") -> str | None:
        """Nazwa usługi z cennika najlepiej pasująca do ogłoszenia (albo ``None``).

        Wyjątek od liczenia punktów: jeśli w tytule stoi słowo „integracja”, to zlecenie jest
        o integrację, nawet gdy w tym samym tytule jest „sklep”. Inaczej „Integracja Algolii
        ze sklepem Shoper” wpadało w pakiet sklepu za 8 900 zł (przebieg kontrolny 28.10).
        """
        if INTEGRATION_TITLE.search(title) and "integracja" in self.services:
            return "integracja"
        scores = self.service_scores(title, description)
        if not scores:
            return None
        order = {service: index for index, (service, _) in enumerate(SERVICE_PATTERNS)}
        return max(scores, key=lambda service: (scores[service], -order.get(service, 0)))

    @staticmethod
    def detect_level(text: str) -> str:
        """Poziom ceny: ``premium``/``standard`` po jawnych sygnałach, inaczej ``start``."""
        if PREMIUM_SIGNS.search(text):
            return "premium"
        if STANDARD_SIGNS.search(text):
            return "standard"
        return "start"

    def quote(
        self,
        text: str,
        *,
        title: str = "",
        service: str | None = None,
        level: str | None = None,
    ) -> Quote | None:
        """Wycena z cennika dla ogłoszenia; ``None`` gdy nie ma dopasowania.

        ``text`` to treść ogłoszenia, ``title`` tytuł (waży więcej przy dopasowaniu usługi).
        """
        # Naprawę rozpoznajemy po tytule, nie po całej stronie: w treści Useme siedzi menu
        # serwisu, a słowo „integracja” w stopce wystarczyło, żeby drobna poprawka dostała
        # pakiet integracji za 2 500 zł.
        if service is None and REPAIR_RE.search(title) and not INTEGRATION_SCOPE_RE.search(title):
            return None
        if service is None and APP_BUILD_RE.search(title):
            return None
        key = service or self.detect_service(title, text)
        if key is None or key not in self.services:
            return None
        service_data = _require_mapping(self.services[key], f"services.{key}")
        levels = _require_mapping(service_data.get("levels"), f"services.{key}.levels")
        # Poziom liczymy z tytułu i początku ogłoszenia: pełna strona Useme ma w treści menu
        # serwisu („Blog”, „Pomoc”), które windowało integrację do poziomu premium.
        level_text = f"{title}\n{text[:1500]}"
        chosen = level or self.detect_level(level_text)
        if chosen not in levels:
            chosen = "start"
        level_data = _require_mapping(levels[chosen], f"services.{key}.levels.{chosen}")
        amount = _require_price(level_data.get("price"), f"services.{key}.levels.{chosen}")
        hours = self.hours(key, chosen)
        days = level_data.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            # Termin kalendarzowy bierzemy z tabeli domyślnej, nie z godzin: godziny zawierają
            # przygotowanie, a klient czyta termin dostawy (szkolenie to 1 dzień, nie 3).
            days = DEFAULT_DAYS.get(key)
            if days is None:
                days = math.ceil(hours / DAILY_HOURS) if hours else 7
        includes = self._expand_includes(levels, level_data, key)
        return Quote(
            service=key,
            level=chosen,
            amount=amount,
            days=days,
            includes=tuple(str(item) for item in includes),
            priced_as=str(service_data.get("priced_as") or "fixed"),
            version=self.version,
            hours=hours,
        )

    @staticmethod
    def _expand_includes(levels: dict[str, Any], level_data: dict[str, Any], key: str) -> list[str]:
        """Zamień „jak Standard” na listę z poziomu standard, żeby akapit z ceną miał treść.

        Cennik używa skrótu „jak Standard plus …”, bo tak się go czyta w YAML-u, ale klient
        dostaje zdanie „w cenie: jak Standard, integracje dodatkowe”, które nic nie mówi.
        """
        raw = level_data.get("includes")
        if not isinstance(raw, list) or not raw:
            return []
        expanded: list[str] = []
        for item in raw:
            text = str(item)
            match = _LIKE_LEVEL.match(text)
            if match:
                parent = _require_mapping(
                    levels.get(match.group(1).lower(), {}), f"services.{key}.levels.{match.group(1)}"
                )
                parent_includes = parent.get("includes")
                if isinstance(parent_includes, list):
                    expanded.extend(str(part) for part in parent_includes)
                    rest = _LIKE_LEVEL.sub("", text).strip(" ,+i")
                    if rest:
                        expanded.append(rest)
                    continue
            expanded.append(text)
        return expanded or [key]

    def services_list(self) -> Iterable[str]:
        return tuple(self.services)

    # ------------------------------------------------------------------ most do cen

    def as_price_brief(self, quote: Quote, *, key: str) -> PriceBrief:
        """Zamień wycenę z cennika na :class:`PriceBrief`, żeby wstawić ją do draftu.

        ``includes`` idą do akapitu z ceną, ``rationale`` mówi wprost, że to cennik
        (a nie model), a ``sources`` wskazują plik i jego wersję, więc audyt wie,
        która wersja cennika dała tę kwotę.
        """
        poziom = "dolna granica" if quote.is_from else "cena stała"
        praca = f", {quote.hours:.0f} h pracy" if quote.hours else ""
        rationale = (
            f"Cennik JNX {quote.version}: {quote.service}, poziom {quote.level} "
            f"({poziom}, {quote.amount:.0f} zł brutto, {quote.days} dni{praca})."
        )
        return PriceBrief(
            key=key,
            amount=quote.amount,
            currency=self.currency,
            days=quote.days,
            hourly_min=MARKET_HOURLY[0],
            hourly_max=MARKET_HOURLY[1],
            includes=quote.includes,
            sources=(f"config/cennik.yaml ({quote.version})",),
            rationale=rationale,
            model="cennik",
            hours=quote.hours,
        )


def load_cennik(path: str | Path = DEFAULT_PATH) -> Cennik:
    """Wczytaj cennik z pliku (skrót dla wołających)."""
    return Cennik(path)


def quote_row(cennik: Cennik, row: Any) -> tuple[Quote | None, PriceBrief | None]:
    """Wycena jednej oferty (dowolny obiekt z ``key``, ``title``, ``description``)."""
    quote = cennik.quote(
        str(getattr(row, "description", "") or ""),
        title=str(getattr(row, "title", "") or ""),
    )
    if quote is None:
        return None, None
    return quote, cennik.as_price_brief(quote, key=str(getattr(row, "key", "")))


def currency_symbol(currency: str) -> str:
    return CURRENCY_SYMBOL.get(currency.upper(), currency)


__all__ = [
    "Cennik",
    "CennikError",
    "DEFAULT_CZAS_PATH",
    "DEFAULT_DAYS",
    "DEFAULT_PATH",
    "DAILY_HOURS",
    "FLOOR_HOURLY_PLN",
    "LEVELS",
    "Quote",
    "currency_symbol",
    "load_cennik",
    "quote_row",
]
