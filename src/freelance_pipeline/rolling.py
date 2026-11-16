"""Rolling lista: co się nazbierało od ostatniej wysyłki, z draftem i wyceną przy każdej ofercie.

Dostawa jest **plikiem** (Telegram ``sendDocument``), nie tekstem w czacie: lista ma 20–60 kB,
a adapter czatu dzielił ją na kilka wiadomości, w których gubiła się numeracja. Numeracja
w pliku jest kontraktem z Janem — „3 odpisałem" znaczy wiersz ``[3]`` tego pliku.

Podział odpowiedzialności:

* :func:`build_entries` — bierze oferty z ``leads-*.json`` (świeży przebieg kolekcji, z treścią
  ogłoszeń), przechodzi przez rejestr (:mod:`freelance_pipeline.ledger`) i tworzy wpisy tylko dla
  ofert ze statusem ``new``,
* :func:`render_file` — tekst lista w formacie z planu (§3.4),
* :func:`panel_items` — dane dla panelu „kopiuj jednym kliknięciem" (`scripts/copy_widget.py`),
* :func:`send_document` — jedna wysyłka do tematu ``#Snajper``; statusy ``new → seen``
  ustawia wołający **po** potwierdzeniu wysyłki (nigdy przed).

Zero tabeli stawek i zero polityki: ten moduł nie decyduje, co jest warte odpowiedzi — dostaje
gotowe drafty i wyceny.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from freelance_pipeline.adlink import with_ad_reference
from freelance_pipeline.brief import fallback_brief
from freelance_pipeline.instant import render_offer_block
from freelance_pipeline.ledger import Ledger, OfferSeed
from freelance_pipeline.pricing import CURRENCY_SYMBOL, PriceBrief, PricedDraft
from freelance_pipeline.proposal import OfferInput
from freelance_pipeline.proposal_check import summarise

SNAJPER_TOPIC_LABEL = "#Snajper"
DEFAULT_CHAT_ID = "-1003574759568"
DEFAULT_THREAD_ID = "3"
ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "SNAJPER_TELEGRAM_CHAT_ID"
ENV_THREAD_ID = "SNAJPER_TELEGRAM_THREAD_ID"
TELEGRAM_API = "https://api.telegram.org"
#: Rozmiar podglądu treści ogłoszenia w pliku (pełny tekst jest w leads-*.json).
CONTENT_PREVIEW_CHARS = 1500
#: Maksymalna długość podpisu dokumentu w Telegramie.
CAPTION_LIMIT = 1024


class RollingError(Exception):
    """Błąd budowania lub wysyłki rolling listy."""


class RollingDeliveryError(RollingError):
    """Wysyłka dokumentu się nie udała; statusy zostają ``new`` (nic nie oznaczamy na ślepo)."""


@dataclass(frozen=True, slots=True)
class LeadRow:
    """Oferta z pliku ``leads-*.json`` — to, co przebieg kolekcji zostawił dla wyceny i draftu."""

    key: str
    source: str
    source_id: str
    url: str
    title: str
    author: str
    description: str
    bucket: str
    score: int
    budget: float | None = None
    currency: str | None = None
    rationale: str = ""
    #: Co pokazać w liście, gdy ``description`` to pełna strona ogłoszenia z menu serwisu.
    #: Model pisze draft z pełnej treści, ale Jan nie ma czytać nawigacji Useme.
    display_description: str = ""

    @classmethod
    def from_json(cls, data: object) -> LeadRow:
        if not isinstance(data, dict):
            raise RollingError(f"wpis leads-*.json musi być obiektem, jest {type(data).__name__}")
        source = str(data.get("source") or "")
        source_id = str(data.get("source_id") or "")
        key = str(data.get("key") or f"{source}:{source_id}")
        url = str(data.get("url") or "")
        if not source or not source_id or not url:
            raise RollingError(f"wpis bez source/source_id/url: {data!r}")
        score = data.get("score")
        if isinstance(score, bool) or not isinstance(score, int):
            raise RollingError(f"wpis {key} ma niecałkowitą ocenę: {score!r}")
        budget = data.get("budget")
        if budget is not None and not isinstance(budget, (int, float)):
            raise RollingError(f"wpis {key} ma niepoprawny budżet: {budget!r}")
        return cls(
            key=key,
            source=source,
            source_id=source_id,
            url=url,
            title=str(data.get("title") or ""),
            author=str(data.get("author") or ""),
            description=str(data.get("description") or ""),
            bucket=str(data.get("bucket") or ""),
            score=score,
            budget=float(budget) if budget is not None else None,
            currency=(str(data["currency"]) if data.get("currency") else None),
            rationale=str(data.get("rationale") or ""),
            display_description=str(data.get("display_description") or ""),
        )

    def as_offer(self) -> OfferInput:
        return OfferInput(
            key=self.key,
            title=self.title,
            source=self.source,
            url=self.url,
            author=self.author,
            description=self.description,
            bucket=self.bucket,
            score=self.score,
            budget=self.budget,
            currency=self.currency,
        )

    def as_seed(self) -> OfferSeed:
        return OfferSeed(
            source=self.source,
            source_id=self.source_id,
            url=self.url,
            title=self.title,
            bucket=self.bucket,
            score=float(self.score),
        )


def load_leads(path: str | Path) -> list[LeadRow]:
    """Wczytaj plik ``leads-*.json`` (pusty plik = brak nowych ofert, nie błąd)."""
    source = Path(path)
    if not source.is_file():
        raise RollingError(f"brak pliku ofert: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError as exc:
        raise RollingError(f"{source} nie jest poprawnym JSON-em: {exc}") from exc
    if not isinstance(payload, list):
        raise RollingError(f"{source} musi zawierać listę ofert")
    return [LeadRow.from_json(entry) for entry in payload]


def newest_leads(state_dir: str | Path) -> Path | None:
    """Najświeższy ``leads-*.json`` w katalogu stanu (albo ``None``)."""
    directory = Path(state_dir)
    found = sorted(directory.glob("leads-*.json"), key=lambda path: path.stat().st_mtime)
    return found[-1] if found else None


def load_pending_rows(
    state_dir: str | Path,
    ledger: Ledger,
    *,
    now: datetime | None = None,
    days: int = 3,
) -> list[LeadRow]:
    """Oferty czekające na listę: z plików ``leads-*.json`` z ostatnich ``days`` dni.

    Bierzemy kilka plików, nie tylko najświeższy, bo nieudana wysyłka nie może zgubić ofert:
    wczorajsze ``new`` nadal czeka, dopóki nie pójdzie na listę. Duplikaty po kluczu są usuwane,
    a status bierzemy z rejestru — oferta już ``seen`` nie wraca.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = stamp - timedelta(days=days)
    rows: dict[str, LeadRow] = {}
    for path in sorted(Path(state_dir).glob("leads-*.json")):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:  # pragma: no cover - plik zniknął między globem a stat
            continue
        if modified < cutoff:
            continue
        try:
            for row in load_leads(path):
                rows.setdefault(row.key, row)
        except RollingError:
            continue
    records = ledger.load()
    return [
        row
        for key, row in rows.items()
        if (record := records.get(key)) is None or record.status == "new"
    ]


#: Poniżej tylu znaków opis uznajemy za skrót i próbujemy dociągnąć pełną treść.
SHORT_DESCRIPTION_CHARS = 900
#: Ile pełnego tekstu trzymamy (dłuższe ogłoszenia i tak nie wnoszą nic do draftu).
MAX_DESCRIPTION_CHARS = 4000


def enrich_rows(
    rows: Sequence[LeadRow],
    fetcher: Callable[[LeadRow], str],
    *,
    min_chars: int = SHORT_DESCRIPTION_CHARS,
    limit_chars: int = MAX_DESCRIPTION_CHARS,
) -> list[LeadRow]:
    """Dociągnij pełną treść do ofert, których opis w raporcie jest skrótem.

    Adapter Useme oddaje ~250 znaków uciętych w połowie zdania. Draft z takiego skrawka
    bywa trafiony przypadkiem, więc krok listy dociąga stronę ogłoszenia. Każda porażka
    (sieć, blokada, odpowiedź-ostrzeżenie zaczynająca się od ``(``) zostawia oryginalny opis —
    brak sieci nie może wywalić całej listy.
    """
    out: list[LeadRow] = []
    for row in rows:
        if len(row.description) >= min_chars or not row.url.startswith("http"):
            out.append(row)
            continue
        try:
            full = (fetcher(row) or "").strip()
        except Exception:  # noqa: BLE001 - pobranie treści to dodatek, nie warunek listy
            out.append(row)
            continue
        if len(full) < min_chars or full.startswith("("):
            out.append(row)
            continue
        out.append(
            replace(
                row,
                description=full[:limit_chars],
                display_description=row.display_description or row.description,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class RollingEntry:
    """Jedna pozycja rolling listy: oferta, draft i wycena razem."""

    row: LeadRow
    first_seen_at: datetime
    draft_text: str
    price: PriceBrief | None = None
    problems: tuple[str, ...] = ()
    #: Krótkie streszczenie ogłoszenia (sekcja „Brief:” w liście), pisze :mod:`brief`.
    brief: str = ""

    @property
    def key(self) -> str:
        return self.row.key

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def note(self) -> str:
        return summarise(self.problems)

    @property
    def price_line(self) -> str:
        if self.price is None:
            return "wycena: brak"
        symbol = CURRENCY_SYMBOL.get(self.price.currency, self.price.currency)
        amount = f"{self.price.amount:,.0f}".replace(",", " ")
        return (
            f"{amount} {symbol}, ok. {self.price.days} dni roboczych"
            f" · {self.price.hourly_equivalent:.0f} zł/h"
            f" · stawka z researchu {self.price.hourly_min:.0f}-{self.price.hourly_max:.0f} zł/h"
        )


#: Kolejność kubełków na liście: najpierw zlecenia, potem MOŻE, na końcu ZAMIEN.
BUCKET_ORDER = {"TOP": 0, "MOŻE": 1, "ZAMIEN": 2}


def select_rows(
    rows: Sequence[LeadRow],
    ledger: Ledger,
    *,
    limit: int | None = None,
) -> list[LeadRow]:
    """Oferty ze statusem ``new``, posortowane i przycięte do ``limit``.

    Jedno miejsce decyduje, co wejdzie na listę — drafty i wyceny liczymy dokładnie dla tego
    zbioru, więc żadna pozycja nie może trafić do pliku bez wiadomości.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must not be negative")
    records = ledger.load()
    fresh = [
        row
        for row in rows
        if (record := records.get(row.key)) is not None and record.status == "new"
    ]
    fresh.sort(key=lambda row: (BUCKET_ORDER.get(row.bucket, 9), -row.score, row.key))
    return fresh[:limit] if limit is not None else fresh


def build_entries(
    rows: Sequence[LeadRow],
    ledger: Ledger,
    *,
    priced: Sequence[PricedDraft],
    limit: int | None = None,
    briefs: Mapping[str, str] | None = None,
) -> list[RollingEntry]:
    """Wpisy tylko dla ofert ze statusem ``new``, w kolejności TOP → MOŻE → ZAMIEN, potem ocena.

    Oferty już widziane (``seen``/``replied``/``skipped``/``expired``) nie wracają na listę.
    """
    records = ledger.load()
    by_key = {entry.key: entry for entry in priced}
    brief_map = dict(briefs or {})
    fresh = select_rows(rows, ledger, limit=limit)
    entries: list[RollingEntry] = []
    for row in fresh:
        record = records[row.key]
        draft = by_key.get(row.key)
        entries.append(
            RollingEntry(
                row=row,
                first_seen_at=record.first_seen_at or record.status_changed_at or datetime.now(UTC),
                draft_text=(
                    with_ad_reference(draft.text, row.source, row.url) if draft else ""
                ),
                price=draft.brief if draft and draft.brief.amount > 0 else None,
                problems=(draft.problems if draft else ("brak draftu",)),
                brief=brief_map.get(row.key, "")
                or fallback_brief(row.title, row.description, row.rationale),
            )
        )
    return entries


def render_file(
    entries: Sequence[RollingEntry],
    *,
    now: datetime,
    topic: str = SNAJPER_TOPIC_LABEL,
) -> str:
    """Treść pliku rolling listy.

    Format z 2026-10-28 (decyzja Jana): numer, kubełek, ocena i źródło zostają, bo ocenę
    chce widzieć; zamiast „po co” jest sekcja ``Brief:``; zniknęły rzeczy, które nie pomagają
    w decyzji: autor ogłoszenia, podgląd treści (brief go zastępuje), uzasadnienie wyceny
    ze źródłami i techniczna kontrola wyjścia. Blok jest ten sam co w trybie natychmiastowym,
    bo Jan kopiuje z obu miejsc tak samo.
    """
    if not entries:
        return ""
    lines = [
        f"SNAJPER: {len(entries)} nowych ofert (stan na {now:%Y-%m-%d %H:%M} UTC)",
        f"Do oznaczenia: „3 odpisałem”, „5 odrzuć”, „popraw 3: co zmienić” ({topic}).",
        "",
    ]
    zamien = [entry for entry in entries if entry.row.bucket == "ZAMIEN"]
    for index, entry in enumerate(entries, start=1):
        row = entry.row
        lines.append(
            render_offer_block(
                index=index,
                bucket=row.bucket,
                score=row.score,
                source=row.source,
                title=row.title,
                url=row.url,
                brief=entry.brief,
                draft_text=entry.draft_text,
                amount=entry.price.amount if entry.price else None,
                currency=entry.price.currency if entry.price else "PLN",
                days=entry.price.days if entry.price else None,
                budget=row.budget,
                budget_currency=row.currency,
            )
        )
        lines.append("")
    if zamien:
        lines += [
            (
                f"ZAMIEN ({len(zamien)}): klient szuka człowieka do pracy powtarzalnej — "
                "w wiadomości proponuj automatyzację zamiast zatrudnienia."
            ),
            "",
        ]
    return "\n".join(lines)


def numbering(entries: Sequence[RollingEntry]) -> dict[str, str]:
    """Mapa numeracji listy na klucze rejestru: ``{"1": "useme:145473", ...}``.

    Numeracja jest kontraktem z Janem („3 odpisałem"), więc mapa numer → klucz ląduje
    w pliku obok listy i to z niej korzysta ``scripts/ledger.py oznacz``.
    """
    return {str(index): entry.key for index, entry in enumerate(entries, start=1)}


def resolve_numbers(numbers: Sequence[str], mapping: dict[str, str]) -> list[str]:
    """Zamień numery z listy na klucze rejestru; nieznany numer to głośny błąd."""
    keys: list[str] = []
    unknown = [number for number in numbers if number not in mapping]
    if unknown:
        raise RollingError(
            "nie znam numeru/ów " + ", ".join(unknown) + f" — ostatnia lista ma {len(mapping)} pozycji"
        )
    for number in numbers:
        key = mapping[number]
        if key not in keys:
            keys.append(key)
    return keys


def render_markdown(
    entries: Sequence[RollingEntry],
    *,
    now: datetime,
    topic: str = SNAJPER_TOPIC_LABEL,
    docs_url: str = "",
) -> str:
    """Ta sama lista jako dokument Markdown: nagłówki, klikalne linki, wycena, blok do kopiowania.

    Wersja ``.txt`` jest czytana w terminalu, a ta idzie do ludzi: Telegram pokazuje ją jako
    dokument, a Google Docs renderuje nagłówki i linki, więc Jan czyta ją na telefonie bez
    przewijania surowego tekstu.
    """
    if not entries:
        return ""
    oldest = min(entry.first_seen_at for entry in entries)
    lines = [
        f"# Snajper — rolling lista: {len(entries)} nowych ofert",
        "",
        f"od {oldest:%Y-%m-%d %H:%M} UTC · stan na {now:%Y-%m-%d %H:%M} UTC",
        "",
        f"Oznaczanie: odpisz w czacie „3 odpisałem”, „5 odrzuć”, „1-2 odpisałem” (temat {topic}).",
    ]
    if docs_url:
        lines += ["", f"[Wersja do czytania w Google Docs]({docs_url})"]
    lines.append("")
    for index, entry in enumerate(entries, start=1):
        row = entry.row
        lines += [
            "---",
            "",
            f"## [{index}] {row.bucket} · {row.score}/100 · {row.source}",
            "",
            f"### {row.title}",
            "",
            f"<{row.url}>",
            "",
        ]
        meta = [f"zebrane {entry.first_seen_at:%Y-%m-%d}"]
        if row.author:
            meta.append(f"autor: {row.author}")
        if row.budget:
            currency = CURRENCY_SYMBOL.get((row.currency or "PLN").upper(), row.currency or "PLN")
            amount = f"{row.budget:,.0f}".replace(",", " ")
            meta.append(f"budżet z ogłoszenia: {amount} {currency}")
        lines.append(" · ".join(meta))
        lines.append("")
        lines.append(f"**Wycena:** {entry.price_line}")
        if entry.price and entry.price.rationale:
            lines.append("")
            lines.append(f"**Skąd ta kwota:** {entry.price.rationale}")
        if entry.price and entry.price.sources:
            lines.append("")
            lines.append("**Źródła:** " + " · ".join(f"<{url}>" for url in entry.price.sources[:4]))
        if row.rationale:
            lines += ["", f"**Dlaczego na liście:** {row.rationale}"]
        shown = (row.display_description or row.description).strip()
        if shown:
            lines += [
                "",
                "**Treść ogłoszenia (skrót z ogłoszenia — pełna wersja pod linkiem):**",
                "",
                "> " + shown[:CONTENT_PREVIEW_CHARS].replace("\n", "\n> "),
            ]
        lines += ["", "**Propozycja wiadomości** (do skopiowania, w miejscu ceny jest już kwota):", "", "```text", entry.draft_text or "(brak draftu)", "```"]
        if not entry.ok:
            lines += ["", f"> ⚠ kontrola wyjścia: {entry.note}"]
        lines.append("")
    zamien = [entry for entry in entries if entry.row.bucket == "ZAMIEN"]
    if zamien:
        lines += [
            "---",
            "",
            (
                f"**ZAMIEN ({len(zamien)}):** klient szuka człowieka do pracy powtarzalnej — w wiadomości "
                "proponuj automatyzację zamiast zatrudnienia."
            ),
            "",
        ]
    return "\n".join(lines)


def panel_items(entries: Sequence[RollingEntry]) -> list[dict[str, str]]:
    """Dane dla `scripts/copy_widget.py` — dokładnie te same wiadomości, bez renderowania."""
    return [
        {
            "tytul": f"[{index}] {entry.row.title}",
            "url": entry.row.url,
            "wycena": entry.price_line,
            "wiadomosc": entry.draft_text,
        }
        for index, entry in enumerate(entries, start=1)
    ]


@dataclass(frozen=True, slots=True)
class TelegramTarget:
    """Cel wysyłki: bot, czat i temat. Token nigdy nie trafia do logów."""

    token: str
    chat_id: str = DEFAULT_CHAT_ID
    thread_id: str = DEFAULT_THREAD_ID

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramTarget:
        source = env if env is not None else dict(os.environ)
        token = (source.get(ENV_BOT_TOKEN) or "").strip()
        if not token:
            raise RollingDeliveryError(f"brak {ENV_BOT_TOKEN} — nie mam czym wysłać")
        return cls(
            token=token,
            chat_id=(source.get(ENV_CHAT_ID) or DEFAULT_CHAT_ID).strip(),
            thread_id=(source.get(ENV_THREAD_ID) or DEFAULT_THREAD_ID).strip(),
        )


def send_document(
    target: TelegramTarget,
    path: str | Path,
    *,
    caption: str = "",
    session: Any | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Wyślij plik jako dokument do tematu; zwraca odpowiedź Telegrama.

    Multipart budujemy ręcznie, żeby nie dokładać zależności do produkcyjnej ścieżki crona.
    Każdy błąd (HTTP, ``ok: false``, brak ``message_id``) to :class:`RollingDeliveryError` —
    wołający nie oznacza wtedy niczego jako ``seen``.
    """
    source = Path(path)
    if not source.is_file():
        raise RollingDeliveryError(f"nie ma pliku do wysłania: {source}")
    boundary = f"----snajper{uuid.uuid4().hex}"
    fields = {
        "chat_id": target.chat_id,
        "message_thread_id": target.thread_id,
        "disable_notification": "true",
    }
    if caption:
        fields["caption"] = caption[:CAPTION_LIMIT]
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    chunks.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"{source.name}\"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        ).encode()
    )
    chunks.append(source.read_bytes())
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    url = f"{TELEGRAM_API}/bot{target.token}/sendDocument"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    if session is None:  # pragma: no cover - exercised only in production
        import urllib.request

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            raise RollingDeliveryError(f"wysyłka dokumentu nie udała się: {exc}") from exc
    else:
        response = session.post(url, data=body, headers=headers, timeout=timeout)
        status = getattr(response, "status_code", 200)
        try:
            payload = json.loads(response.text)
        except Exception as exc:
            raise RollingDeliveryError(f"odpowiedź Telegrama nieczytelna: {exc}") from exc

    if status is not None and status >= 400:
        raise RollingDeliveryError(f"Telegram HTTP {status}: {payload}")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RollingDeliveryError(f"Telegram odrzucił dokument: {payload}")
    message_id = (payload.get("result") or {}).get("message_id")
    if not isinstance(message_id, int):
        raise RollingDeliveryError(f"Telegram nie potwierdził message_id: {payload}")
    return payload


def mark_delivered(entry_keys: Iterable[str], ledger: Ledger, *, now: datetime | None = None) -> tuple[str, ...]:
    """``new → seen`` po potwierdzonej wysyłce (jedno wywołanie, jeden zapis)."""
    return ledger.mark_delivered(list(entry_keys), now=now)


def store_drafts(
    entries: Sequence[RollingEntry],
    ledger: Ledger,
    *,
    rules_version: str,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Zapisz drafty w rejestrze, żeby „napisz do 3" nie pisało od zera."""
    stored: list[str] = []
    for entry in entries:
        if not entry.draft_text:
            continue
        ledger.set_draft(entry.key, entry.draft_text, rules_version=rules_version, now=now)
        stored.append(entry.key)
    return tuple(stored)


__all__ = [
    "BUCKET_ORDER",
    "DEFAULT_CHAT_ID",
    "DEFAULT_THREAD_ID",
    "ENV_BOT_TOKEN",
    "ENV_CHAT_ID",
    "ENV_THREAD_ID",
    "MAX_DESCRIPTION_CHARS",
    "SHORT_DESCRIPTION_CHARS",
    "LeadRow",
    "RollingDeliveryError",
    "RollingEntry",
    "RollingError",
    "TelegramTarget",
    "build_entries",
    "enrich_rows",
    "load_leads",
    "load_pending_rows",
    "mark_delivered",
    "newest_leads",
    "numbering",
    "panel_items",
    "render_file",
    "render_markdown",
    "resolve_numbers",
    "select_rows",
    "send_document",
    "store_drafts",
]
