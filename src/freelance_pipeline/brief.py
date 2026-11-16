"""Brief: dwa zdania o tym, o co klientowi chodzi (decyzja Jana, 2026-10-28).

W liście snajpera ma być sekcja ``Brief:`` z krótkim streszczeniem ogłoszenia, zamiast
modelowego uzasadnienia oceny („po co”). Brief pisze model jednym wywołaniem na partię
ofert, ale treść jest tylko pomocnicza: gdy model zawiedzie, wracamy do uzasadnienia
selektora, a gdy i tego nie ma, do pierwszych zdań ogłoszenia. Lista ofert nigdy nie może
paść przez brief.

Kontrakt wyjścia: 2 do 3 zdań, maksymalnie 320 znaków, bez ceny i bez terminów (te niesie
akapit z cennikiem), bez zwrotów do klienta i bez powtarzania tytułu.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from freelance_pipeline.openrouter import DEFAULT_COMPLETIONS_URL, ENV_API_KEY
from freelance_pipeline.proposal import OfferInput

#: Model do briefów: ten sam, którym pisze się drafty z crona (jeden styl, jedna jakość).
DEFAULT_BRIEF_MODEL = "deepseek/deepseek-v4.1-flash"
MAX_BRIEF_CHARS = 320
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_PRICE_OR_DAYS = re.compile(r"\d[\d\s]*\s*(zł|pln|dni|tygodni|godzin)", re.IGNORECASE)

_SYSTEM = (
    "Streszczasz ogłoszenie o zlecenie dla Jana, jednoosobowego specjalisty automatyzacji. "
    "Piszesz po polsku, 2 do 3 zdań, maksymalnie 320 znaków. Podajesz: co klient ma teraz, "
    "co ma być efektem, i jaki jest zakres po jego stronie (systemy, liczba elementów), "
    "jeśli ogłoszenie to mówi. Zero ceny, zero terminów, zero zwrotów do klienta, zero "
    "powtarzania tytułu, zero oceniania zlecenia. Tylko fakty z treści ogłoszenia; jeśli "
    "czegoś nie ma, pomiń to, nie zgaduj. Bez myślnika em dash."
)


class BriefError(Exception):
    """Nie udało się napisać briefu (wołający bierze wtedy fallback)."""


_TRAILING = " ,;.()[]"


def _trim(text: str) -> str:
    """Odetnij końcowe spacje i interpunkcję (bez ``strip`` z wieloma znakami, reguła B005)."""
    return text.rstrip(_TRAILING)


def _clean(text: str) -> str:
    """Zbij białe znaki i utnij do limitu, ale po granicy zdania, nie w środku wyliczanki."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= MAX_BRIEF_CHARS:
        return _trim(flat)
    cut = flat[:MAX_BRIEF_CHARS]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end >= 120:
        return _trim(cut[: sentence_end + 1])
    # Bez granicy zdania tniemy po przecinku albo po nawiasie, żeby brief nie kończył się
    # w połowie wyliczanki („...dostępność produktów)”).
    for separator in (", ", " (", " i "):
        index = cut.rfind(separator)
        if index > 150:
            return _trim(cut[:index])
    space = cut.rfind(" ")
    return _trim(cut[:space] if space > 0 else cut)


def fallback_brief(title: str, description: str, rationale: str = "") -> str:
    """Brief bez modelu: uzasadnienie selektora, a gdy go brak, pierwsze zdania ogłoszenia."""
    if rationale.strip():
        return _clean(rationale)
    body = re.sub(r"\s+", " ", (description or "").strip())
    if not body:
        return _clean(title)
    sentences = _SENTENCE_END.split(body)
    picked: list[str] = []
    length = 0
    for sentence in sentences:
        if not sentence.strip():
            continue
        if _PRICE_OR_DAYS.search(sentence) and picked:
            continue
        picked.append(sentence.strip())
        length += len(sentence)
        if length > 180 or len(picked) == 2:
            break
    return _clean(" ".join(picked) or body)


def _validate(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    clean = _clean(text)
    if len(clean) < 40:
        return None
    return clean


class BriefWriter:
    """Pisze briefy dla partii ofert; nigdy nie rzuca wyjątkiem na zewnątrz ``write``."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_BRIEF_MODEL,
        api_key: str | Callable[[], str | None] | None = None,
        base_url: str = DEFAULT_COMPLETIONS_URL,
        timeout: float = 90.0,
        batch_size: int = 8,
        session: Any | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.batch_size = max(1, batch_size)
        self._api_key = api_key
        self._session = session

    def _key(self) -> str | None:
        if callable(self._api_key):
            return self._api_key()
        if isinstance(self._api_key, str) and self._api_key:
            return self._api_key
        import os

        return os.environ.get(ENV_API_KEY) or None

    def write(self, offers: Sequence[OfferInput]) -> dict[str, str]:
        """Mapa ``{klucz oferty: brief}``; oferty bez odpowiedzi modelu dostają fallback."""
        batch = list(offers)
        result = {offer.key: fallback_brief(offer.title, offer.description) for offer in batch}
        if not batch:
            return result
        api_key = self._key()
        if not api_key:
            return result
        try:
            import requests
        except ImportError:  # pragma: no cover - requests jest w zależnościach
            return result
        session = self._session or requests.Session()
        for start in range(0, len(batch), self.batch_size):
            part = batch[start : start + self.batch_size]
            answers: dict[str, str] = {}
            try:
                answers = self._write_batch(part, session, api_key)
            except Exception:  # noqa: BLE001 - partia bez modelu idzie na fallback
                answers = {}
            if answers:
                result.update({key: value for key, value in answers.items() if key in result})
        return result

    def _write_batch(
        self, part: Sequence[OfferInput], session: Any, api_key: str
    ) -> dict[str, str]:
        rows = [
            {
                "offer_key": index,
                "tytul": offer.title,
                "tresc": (offer.description or "")[:2500],
                "budzet": offer.budget,
            }
            for index, offer in enumerate(part)
        ]
        response = session.post(
            self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=self.timeout,
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": "Ogłoszenia:\n"
                        + json.dumps(rows, ensure_ascii=False)
                        + "\n\nZwróć JSON: {\"briefs\": [{\"offer_key\": <int>, \"brief\": "
                        "\"<2-3 zdania>\"}]}",
                    },
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "offer_briefs",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "briefs": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "offer_key": {"type": "integer"},
                                            "brief": {"type": "string"},
                                        },
                                        "required": ["offer_key", "brief"],
                                        "additionalProperties": False,
                                    },
                                }
                            },
                            "required": ["briefs"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        )
        if response.status_code != 200:
            raise BriefError(f"HTTP {response.status_code}: {response.text[:200]}")
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        data = json.loads(content)
        answers: dict[str, str] = {}
        for item in data.get("briefs", []):
            index = item.get("offer_key")
            if not isinstance(index, int) or not 0 <= index < len(part):
                continue
            clean = _validate(item.get("brief"))
            if clean:
                answers[part[index].key] = clean
        return answers


def write_briefs(offers: Sequence[OfferInput], **kwargs: Any) -> dict[str, str]:
    """Skrót: briefy dla partii ofert (z fallbackiem, bez wyjątków)."""
    return BriefWriter(**kwargs).write(offers)


__all__ = [
    "DEFAULT_BRIEF_MODEL",
    "MAX_BRIEF_CHARS",
    "BriefError",
    "BriefWriter",
    "fallback_brief",
    "write_briefs",
]
