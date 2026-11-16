"""Stopka z linkiem do ogłoszenia w draftach odpowiadanych na Facebooku.

Odpowiedź na post z grupy FB idzie na priv albo w komentarz, więc bez linku Jan po
kilku dniach nie wie, na które ogłoszenie odpisuje. Prefiks trzyma adres w treści,
w formacie, który sam podał: ``Odpowiadając na Państwa ogłoszenie: <url>``.
"""

from __future__ import annotations

AD_REFERENCE_PREFIX = "Odpowiadając na Państwa ogłoszenie:"

#: Źródła, w których odpowiedź nie niesie ze sobą wątku i link jest potrzebny.
AD_REFERENCE_SOURCES = ("facebook",)


def with_ad_reference(message: str, source: str | None, url: str | None) -> str:
    """Doklej link do ogłoszenia na początek wiadomości (tylko źródła facebookowe).

    Idempotentne: draft, który już ma prefiks, nie dostaje drugiego. Pusta wiadomość
    albo oferta bez URL-a wraca bez zmian, bo lepszy draft bez linku niż pusty.
    """
    body = (message or "").lstrip()
    if not body or not url:
        return message or ""
    if not str(source or "").lower().startswith(AD_REFERENCE_SOURCES):
        return message or ""
    if body.startswith(AD_REFERENCE_PREFIX) or f"{AD_REFERENCE_PREFIX} {url}" in body:
        return message or ""
    return f"{AD_REFERENCE_PREFIX} {url}\n\n{body}"


__all__ = ["AD_REFERENCE_PREFIX", "AD_REFERENCE_SOURCES", "with_ad_reference"]
