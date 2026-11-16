"""Oferteo notification e-mail -> JobLead.

The client posts a request; Oferteo pushes it to every contractor whose
categories match, from ``powiadomienie@oferteo.pl``, usually within a minute.
That e-mail already carries the structured brief (scope, site type, industry,
number of subpages, functions, deadline), so a lead can be scored **without**
buying the contact and without touching the logged-in panel.

Two shapes exist and both are handled:

* ``new``      the request itself, subject ``"Zapytanie o …, Miasto - nr 123"``,
               full brief, masked phone and e-mail;
* ``revealed`` subject prefixed with ``☎`` and ending in ``Zadzwoń``, sent after
               the contact was unlocked: same request, full text, **plain-text
               client e-mail**, phone still partially masked.

The module never issues an HTTP request. The ``/odkryj`` link found in the body
is recorded as ``url`` and must stay closed: opening it spends points.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from freelance_pipeline.mailbox import MailMessage
from freelance_pipeline.models import JobLead

OFERTEO_MAIL_SOURCE = "oferteo_mail"
OFERTEO_LIST_URL = "https://www.oferteo.pl/zlecenia"
WARSAW = ZoneInfo("Europe/Warsaw")

#: Fields that belong to the brief itself; everything else is metadata.
BRIEF_FIELDS = (
    "Zakres zlecenia",
    "Zakres obsługi strony",
    "Rodzaj strony",
    "Rodzaj współpracy",
    "Branża",
    "Branża sklepu",
    "Projekt graficzny",
    "Liczba podstron/zakładek",
    "Liczba podstron",
    "Funkcje strony",
    "Funkcje sklepu",
    "Integracje",
    "Orientacyjna liczba produktów",
    "Responsywność",
    "Potrzebne usługi",
    "Technologia",
    "Charakter zlecenia",
    "Opis projektu",
    "Informacje dodatkowe",
    "Inne potrzeby Klienta",
    "Termin skorzystania z oferty",
    "Termin realizacji usługi",
    "Możliwość współpracy zdalnej",
    "Projekt",
    "Zakres",
    "Przeznaczenie projektu",
    "Rodzaj outsourcingu IT",
    "Klient korzysta obecnie z obsługi informatycznej",
    "Budżet",
)

#: Marketing strings that sit on the same line as a label in some templates.
LABEL_NOISE = frozenset({"Jestem zainteresowany", "Zobacz więcej", "Pokaż"})

CORE_FIELDS = ("Kategoria", "Miejsce", "Dodano", "Nr zapytania ofertowego")
KNOWN_LABELS = frozenset(BRIEF_FIELDS) | frozenset(CORE_FIELDS)

_RFP_IN_SUBJECT = re.compile(r"nr\s*(\d{5,10})", re.IGNORECASE)
_RFP_BARE = re.compile(r"\b(\d{7,9})\b")
_PHONE_MASK = re.compile(r"\+48\s*[•*]")
_MAIL_MASK = re.compile(r"[a-z][•*]{2,}@")
_MAIL_PLAIN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_OFFER_LINK = re.compile(r'href="([^"]*(?:/odkryj|/konto/zapytanie/)[^"]*)"')
_AMOUNT = re.compile(r"(\d[\d\s]{2,})\s*zł")


class OferteoMailError(RuntimeError):
    """The message does not look like an Oferteo request notification."""


_BLOCK = re.compile(
    r"(?is)</?(?:p|div|tr|td|th|table|tbody|li|h[1-6])\b[^>]*>|<br\s*/?>"
)
_TAG = re.compile(r"<[^>]+>")


def _lines(html: str) -> list[tuple[str, bool]]:
    """Split the document into visual lines, keeping a "contains bold" flag.

    Oferteo mixes two markups for the same table: ``label</p><p><b>value</b></p>``
    and ``label<br/><b>value</b><br/>``. Splitting on block boundaries first and
    checking for ``<b>`` inside the line handles both, and stops labels from
    gluing onto neighbouring prose.
    """
    out: list[tuple[str, bool]] = []
    for chunk in _BLOCK.split(html):
        text = html_lib.unescape(_TAG.sub(" ", chunk)).replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append((text, "<b>" in chunk.lower() or "<strong" in chunk.lower()))
    return out


def parse_label_table(html: str) -> dict[str, str]:
    """Return every known ``label -> value`` pair the e-mail exposes."""
    lines = _lines(html)
    pairs: dict[str, str] = {}
    for i, (text, bold) in enumerate(lines[:-1]):
        if bold or len(text) > 48:
            continue
        value, value_bold = lines[i + 1]
        if not value_bold or len(value) > 300:
            continue
        label = text.rstrip(":").strip()
        if label in LABEL_NOISE:
            continue
        if label in KNOWN_LABELS and label not in pairs:
            pairs[label] = value
    return pairs


def unknown_labels(html: str) -> list[str]:
    """Labels the e-mail uses that this parser does not know yet."""
    found: list[str] = []
    lines = _lines(html)
    for i, (text, bold) in enumerate(lines[:-1]):
        if bold or len(text) > 48 or "," in text or "?" in text:
            continue
        if not lines[i + 1][1]:
            continue
        label = text.rstrip(":").strip()
        if label and label not in KNOWN_LABELS and label not in found:
            found.append(label)
    return found[:8]


def classify(subject: str, html: str) -> str:
    """``new``, ``revealed`` or ``other``."""
    head = subject.strip()
    if head.startswith("☎") or "zadzwoń" in head.lower():
        return "revealed"
    if "Nr zapytania ofertowego" in html:
        return "new"
    return "other"


def _title_from_subject(subject: str) -> str:
    text = subject.replace("☎", " ")
    text = re.sub(r"\s*-\s*nr\s*\d+\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\b\d{6,9}\b\s*-\s*Zadzwoń\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*-\s*Zadzwoń\s*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" ,-")


def _published_at(pairs: dict[str, str]) -> datetime | None:
    raw = pairs.get("Dodano")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=WARSAW)
        except ValueError:
            continue
    return None


def _budget(pairs: dict[str, str]) -> float | None:
    raw = pairs.get("Budżet", "")
    hit = _AMOUNT.search(raw)
    if not hit:
        return None
    return float(hit.group(1).replace(" ", ""))


def _client_name(soup: BeautifulSoup) -> str | None:
    heading = soup.find("h3")
    if heading:
        text = heading.get_text(" ", strip=True)
        if 1 < len(text) <= 40:
            return text
    return None


def _description(pairs: dict[str, str]) -> str:
    parts = [f"{k}: {pairs[k]}" for k in BRIEF_FIELDS if pairs.get(k)]
    return " | ".join(parts)


def parse_oferteo_mail(msg: MailMessage) -> JobLead | None:
    """Parse one notification. Returns ``None`` for non-request mail."""
    kind = classify(msg.subject, msg.html)
    if kind == "other":
        return None
    soup = BeautifulSoup(msg.html, "lxml")
    pairs = parse_label_table(msg.html)
    rfp = pairs.get("Nr zapytania ofertowego")
    if not rfp:
        hit = _RFP_IN_SUBJECT.search(msg.subject) or _RFP_BARE.search(msg.subject)
        rfp = hit.group(1) if hit else None
    if not rfp:
        raise OferteoMailError(f"brak numeru zapytania w: {msg.subject!r}")

    city_raw = pairs.get("Miejsce", "")
    city = city_raw.split(",")[0].strip() or None
    # The /odkryj link carries a one-time token and opening it spends points.
    # It is never stored verbatim: a chat client that unfurls the link (Telegram,
    # WhatsApp) would trigger the reveal. Only the neutral request path is kept.
    hint = None
    link = _OFFER_LINK.search(msg.html)
    if link:
        hint = re.sub(r"/odkryj.*$", "", link.group(1)).split("?")[0]
        if hint.startswith("/"):
            hint = "https://www.oferteo.pl" + hint
    # Adres musi być unikalny per zapytanie. Wcześniej każdy lead dostawał tę samą listę
    # (``/zlecenia``), więc dedupe po canonical_url zwijał 23 maile do jednego kandydata
    # (przebieg kontrolny 28.10). Bierzemy neutralną ścieżkę zapytania z maila (bez
    # jednorazowego tokenu /odkryj), a gdy jej nie ma, doklejamy numer jako fragment.
    url = hint or f"{OFERTEO_LIST_URL}#{rfp}"

    text = soup.get_text(" ", strip=True)
    plain_mail = _MAIL_PLAIN.search(text)
    attributes: dict[str, Any] = {
        "rfp_id": rfp,
        "kind": kind,
        "category": pairs.get("Kategoria"),
        "region": city_raw or None,
        "has_phone": bool(_PHONE_MASK.search(text)),
        "has_mail": bool(_MAIL_MASK.search(text)),
        "contact_confirmed": "Potwierdzony" in text,
        "client_mail": plain_mail.group(0) if plain_mail and kind == "revealed" else None,
        "offer_hint": hint,
        "mail_received_at": msg.received_at.isoformat(),
        "unknown_labels": unknown_labels(msg.html),
        "mail_subject": msg.subject,
        "mail_id": msg.message_id,
        "fields": dict(pairs),
    }
    return JobLead(
        source=OFERTEO_MAIL_SOURCE,
        source_id=f"oferteo:{rfp}",
        url=url,
        canonical_url=url,
        title=_title_from_subject(msg.subject) or pairs.get("Zakres zlecenia", "Oferteo"),
        description=_description(pairs),
        company_or_author=_client_name(soup),
        published_at=_published_at(pairs) or msg.received_at,
        budget=_budget(pairs),
        currency="PLN" if _budget(pairs) is not None else None,
        location=city,
        status=kind,
        raw_data=attributes,
    )


#: Gdzie leży token OAuth Google (Drive, Gmail readonly/modify, Calendar).
DEFAULT_TOKEN_PATH = "/opt/data/google_token.json"
#: Ile dni wstecz adapter czyta skrzynkę przy jednym przebiegu. Krótko, bo cron chodzi
#: co 10 minut, a powtórki i tak wycina dedupe po numerze zapytania.
DEFAULT_SINCE_DAYS = 2


class OferteoMailJobs:
    """Źródło w interfejsie pipeline'u: skrzynka Oferteo zamieniona na leady.

    Ten sam kontrakt co ``sources.oferteo.OferteoJobs`` (``source``, ``base_url``,
    ``collect()``), więc wpina się w ``--sources`` bez zmian w orkiestratorze. Różnica:
    treść zapytania pochodzi z maila, nie z publicznej listy, więc lead ma pełny brief,
    kategorię, miasto, termin i informację, czy kontakt jest już odsłonięty.

    Zasady, których ten adapter nie łamie:
    * nie otwiera ``/odkryj`` i nie kupuje punktów, cała praca to odczyt skrzynki,
    * dwa maile o tym samym numerze zapytania (zwykły i ``☎ … Zadzwoń``) to jeden lead,
      scalany przez :func:`freelance_pipeline.lead_identity.merge_leads`,
    * brak tokenu albo błąd Gmaila to ``OferteoMailError``, nie cicha pustka.
    """

    source = OFERTEO_MAIL_SOURCE
    base_url = "https://www.oferteo.pl"

    def __init__(
        self,
        *,
        token_path: str = DEFAULT_TOKEN_PATH,
        since_days: int = DEFAULT_SINCE_DAYS,
        limit: int | None = None,
        mailbox: Any | None = None,
        **_ignored: Any,
    ) -> None:
        self.token_path = token_path
        self.since_days = max(1, int(since_days))
        self.limit = limit
        self._mailbox = mailbox

    def collect(self) -> list[JobLead]:
        """Pobierz maile z okna czasowego i zwróć leady po jednym na numer zapytania."""
        from datetime import timedelta

        from freelance_pipeline.lead_identity import merge_leads
        from freelance_pipeline.mailbox import GmailMailbox, MailboxError

        box = self._mailbox
        if box is None:
            try:
                box = GmailMailbox(self.token_path)
            except MailboxError as exc:
                raise OferteoMailError(f"skrzynka Gmaila: {exc}") from exc
        try:
            messages = box.fetch(since=timedelta(days=self.since_days), limit=self.limit)
        except MailboxError as exc:
            raise OferteoMailError(f"odczyt skrzynki: {exc}") from exc

        leads: dict[str, JobLead] = {}
        for msg in messages:
            try:
                lead = parse_oferteo_mail(msg)
            except OferteoMailError:
                # Jeden zepsuty mail nie może wyciąć całego źródła: reszta jest dobra.
                continue
            if lead is None:
                continue
            previous = leads.get(lead.source_id)
            leads[lead.source_id] = merge_leads(previous, lead) if previous else lead
        return list(leads.values())
