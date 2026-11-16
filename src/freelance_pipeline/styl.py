"""Wysylka par draft -> wersja Jana do humanizera (reguly stylu).

Humanizer to osobna usluga (systemd ``humanizer``, ``127.0.0.1:8011``). Trzyma
karty stylu i zbiera korekty w ``poprawki.md``. Ten modul jest jedynym miejscem
w snajperze, ktore do niego pisze.

Zasada: nauka stylu nigdy nie moze zepsuc zapisu draftu. Kazda awaria konczy sie
logiem i zwrotem ``False``, bez wyjatku w gore.
"""

from __future__ import annotations

import logging
import os

import requests

DEFAULT_URL = "http://127.0.0.1:8011"
DEFAULT_REJESTR = "rejestr-klient"
TIMEOUT_S = 3.0

log = logging.getLogger(__name__)


def _url() -> str:
    return os.environ.get("SNAJPER_HUMANIZER_URL", DEFAULT_URL).rstrip("/")


def zapisz_pare(
    draft: str,
    moja: str,
    *,
    rejestr: str = DEFAULT_REJESTR,
    uwagi: str = "",
) -> bool:
    """Wysyla pare (draft agenta, wersja Jana) do ``/zapisz``.

    Zwraca ``True`` tylko gdy humanizer potwierdzil zapis. Pusta albo identyczna
    para nie jest wysylana, bo nic nie wnosi do karty stylu.
    """
    draft = (draft or "").strip()
    moja = (moja or "").strip()
    if not draft or not moja:
        return False
    if draft == moja:
        log.debug("styl: draft i wersja Jana identyczne, pomijam")
        return False
    payload = {"rejestr": rejestr, "draft": draft, "moja": moja, "uwagi": uwagi}
    try:
        response = requests.post(f"{_url()}/zapisz", json=payload, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        log.warning("styl: humanizer nieosiagalny (%s), para przepadla", exc)
        return False
    if response.status_code != 200:
        log.warning(
            "styl: humanizer odrzucil pare (%s): %s",
            response.status_code,
            (response.text or "")[:200],
        )
        return False
    return True


# --- wzorce odpowiedzi Ady (panel Styl) -------------------------------------


def zapisz_przyklad(
    oferta: str,
    odpowiedz: str,
    kategoria: str = "",
    lead_key: str = "",
    autor: str = "Ada",
    rejestr: str = DEFAULT_REJESTR,
) -> bool:
    """Dopisuje roboczy przykład Ady. Do promptów wchodzi dopiero po zatwierdzeniu."""
    payload = {
        "rejestr": rejestr,
        "oferta": (oferta or "")[:600],
        "odpowiedz": (odpowiedz or "").strip(),
        "kategoria": kategoria or "",
        "lead_key": lead_key or "",
        "autor": autor or "Ada",
    }
    if not payload["odpowiedz"]:
        return False
    try:
        response = requests.post(f"{_url()}/przyklad", json=payload, timeout=5)
    except requests.RequestException as exc:
        log.warning("styl: humanizer nieosiagalny (%s), przyklad przepadl", exc)
        return False
    if response.status_code != 200:
        log.warning("styl: humanizer odrzucil przyklad (%s)", response.status_code)
        return False
    return True


def lista_przykladow(rejestr: str = DEFAULT_REJESTR) -> list[dict]:
    try:
        response = requests.get(f"{_url()}/przyklady", params={"rejestr": rejestr}, timeout=3)
        if response.status_code != 200:
            return []
        dane = response.json()
    except (requests.RequestException, ValueError):
        return []
    return [p for p in (dane.get("przyklady") or []) if isinstance(p, dict)]


def zatwierdz_przyklad(idx: int, rejestr: str = DEFAULT_REJESTR) -> bool:
    try:
        response = requests.post(
            f"{_url()}/zatwierdz", json={"rejestr": rejestr, "id": int(idx)}, timeout=5
        )
    except requests.RequestException as exc:
        log.warning("styl: humanizer nieosiagalny (%s), zatwierdzenie przepadlo", exc)
        return False
    if response.status_code != 200:
        log.warning("styl: humanizer odrzucil zatwierdzenie (%s)", response.status_code)
        return False
    return True


def przywroc_przyklad(lead_key: str, rejestr: str = DEFAULT_REJESTR) -> bool:
    """Kasuje wpis warsztatu i kopię w puli, żeby oferta wróciła do zadań."""
    try:
        response = requests.post(
            f"{_url()}/przywroc", json={"rejestr": rejestr, "lead_key": lead_key}, timeout=5
        )
    except requests.RequestException as exc:
        log.warning("styl: humanizer nieosiagalny (%s), przywrocenie przepadlo", exc)
        return False
    if response.status_code != 200:
        log.warning("styl: humanizer odrzucil przywrocenie (%s)", response.status_code)
        return False
    return True
