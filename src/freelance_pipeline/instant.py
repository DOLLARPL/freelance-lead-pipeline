"""Tryb natychmiastowy: jedna oferta, jedna wiadomość, od razu po pojawieniu się.

Różnica względem rolling listy: tam lista je plikiem raz na dobę, bo 20 do 60 kB tekstu
w kilku wiadomościach gubi numerację. Tutaj każda nowa oferta idzie osobną wiadomością
w momencie wykrycia (`scripts/instant_report.py` na cronie co 10 minut). Kontrakt jest ten
sam co w liście: numer, kubełek, ocena, brief, link, cena z cennika i gotowa wiadomość.

Zero stanu w tym module: dostaje gotowe wartości, składa tekst, wysyła i nic nie zapisuje.
Statusy `new → seen` ustawia wołający dopiero po potwierdzonej wysyłce.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

TELEGRAM_API = "https://api.telegram.org"
#: Limit Telegrama to 4096 znaków na wiadomość; zostawiamy zapas na nagłówek.
MAX_TEXT_CHARS = 3800
#: Symbol waluty w wiadomości do klienta i do Jana.
CURRENCY_SYMBOL = {"PLN": "zł", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}


class InstantError(Exception):
    """Błąd budowania albo wysyłki wiadomości natychmiastowej."""


class InstantDeliveryError(InstantError):
    """Telegram nie potwierdził wysyłki; statusy ofert zostają ``new``."""


def _amount(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", " ")


def _days_text(days: int) -> str:
    if days == 1:
        return "1 dzień roboczy"
    return f"{days} dni roboczych"


def render_offer_block(
    *,
    index: int,
    bucket: str,
    score: int,
    source: str,
    title: str,
    url: str,
    brief: str = "",
    draft_text: str = "",
    amount: float | None = None,
    currency: str = "PLN",
    days: int | None = None,
    budget: float | None = None,
    budget_currency: str | None = None,
    note: str = "",
    hint: str = "",
) -> str:
    """Jedna oferta jako blok tekstu: do wiadomości na Telegramie i do rolling pliku."""
    lines = [f"[{index}] {bucket} {score}/100 {source}", title]
    if brief:
        lines.append(f"Brief: {brief}")
    if url:
        lines.append(url)
    if budget:
        symbol = CURRENCY_SYMBOL.get((budget_currency or "PLN").upper(), budget_currency or "PLN")
        lines.append(f"Budżet z ogłoszenia: {_amount(budget)} {symbol}")
    if amount:
        symbol = CURRENCY_SYMBOL.get(currency.upper(), currency)
        term = f", ok. {_days_text(days)}" if days else ""
        lines.append(f"Cena: {_amount(amount)} {symbol} brutto{term}")
    else:
        lines.append("Cena: brak (cennik nie dopasował zlecenia)")
    lines.append("")
    if draft_text.strip():
        lines.append("Wiadomość do wklejenia:")
        lines.extend(draft_text.strip().splitlines())
    else:
        lines.append("Wiadomość: brak, napisz w czacie „popraw " + str(index) + "”")
    if note:
        lines.append("")
        lines.append(f"Źródło ceny: {note}")
    if hint:
        lines.append("")
        lines.append(hint)
    return "\n".join(lines)


def render_message(
    entries: Sequence[dict[str, Any]],
    *,
    now: datetime | None = None,
    hint: str = "",
) -> str:
    """Wiadomość z jedną albo kilkoma ofertami (jedna wiadomość = jedna dostawa)."""
    if not entries:
        return ""
    blocks = [render_offer_block(**entry) for entry in entries]
    header = ""
    if len(blocks) > 1:
        stamp = f" (stan na {now:%Y-%m-%d %H:%M} UTC)" if now else ""
        header = f"SNAJPER: {len(blocks)} nowe oferty{stamp}\n\n"
    text = header + "\n\n".join(blocks)
    if hint and len(blocks) == 1:
        text = f"{text}\n\n{hint}"
    return text


def split_message(text: str, *, limit: int = MAX_TEXT_CHARS) -> list[str]:
    """Podziel długą wiadomość po pustych liniach, żeby nie ciąć draftu w środku zdania."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:  # pojedynczy blok dłuższy niż limit: tniemy twardo
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts


def send_text(
    *,
    chat_id: str,
    thread_id: str | None,
    token: str,
    text: str,
    session: Any | None = None,
    timeout: float = 60.0,
    silent: bool = False,
) -> list[dict[str, Any]]:
    """Wyślij tekst do tematu Telegrama; zwraca potwierdzenia, inaczej rzuca wyjątek.

    ``silent=True`` wysyła bez dźwięku (używa tego rolling lista). Tryb natychmiastowy
    wysyła z powiadomieniem, bo o to Janowi chodzi: nowe zlecenie ma go obudzić.
    """
    if not token:
        raise InstantError("brak tokenu bota Telegrama")
    if not text.strip():
        raise InstantError("pusta wiadomość")
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests jest w zależnościach
        raise InstantDeliveryError("brak biblioteki requests") from exc
    client = session or requests.Session()
    answers: list[dict[str, Any]] = []
    for part in split_message(text):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": part,
            "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else "false",
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        response = client.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage", data=payload, timeout=timeout
        )
        if response.status_code != 200:
            raise InstantDeliveryError(f"HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        if not body.get("ok") or "message_id" not in body.get("result", {}):
            raise InstantDeliveryError(f"Telegram odrzucił wiadomość: {body!r}"[:300])
        answers.append(body["result"])
    return answers


__all__ = [
    "CURRENCY_SYMBOL",
    "InstantDeliveryError",
    "InstantError",
    "MAX_TEXT_CHARS",
    "render_message",
    "render_offer_block",
    "send_text",
    "split_message",
]
