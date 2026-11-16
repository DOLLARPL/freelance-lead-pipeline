"""Deterministic outbound check for a reply to an offer.

This is the old dashboard-plugin validator (``_draft_problems`` in
``archive/plugins-snajper-20261007/snajper/dashboard/plugin_api.py``) moved into
the package, because the plugin was withdrawn on 2026-10-07 while the cron still
needs the same control over what a model drafts. Extended with the rules that came
out of ``RESEARCH-KONWERSJA.md`` (§3 AI-isms, §7 rules to the prompt): the shape of
a bulleted list, paragraph length, the AI lexicon, sentence count in the first 200
characters and the number of closing questions.

Two modes, one function:

* ``priced=False`` (the cron's draft): the price paragraph is replaced by a single
  :data:`PRICE_MARKER` line, because Jan prices every offer by hand (decision D5,
  ``PLAN-DOPRACOWANIE-2026-10.md`` §3.7). No amount may appear anywhere, and the
  700–1100 character window is measured on the text *without* that marker line.
* ``priced=True`` (the message sent after Jan prices it): the amount paragraph has
  to be there and has to start with the amount.

The check never judges style and never calls a model — it only catches what Jan
recognises as a mistake, cheaply and repeatably, so a draft with problems never
leaves the system silently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Bumped whenever the rules below change, so the ledger can tell which draft was
#: written under which rules (see ``RESEARCH-KONWERSJA.md`` §6).
RULES_VERSION = "2026-10-09.1"

#: Stands in for the price paragraph in a cron draft.
PRICE_MARKER = "[WYCENA: napisz „napisz do N”, żeby policzyć]"

#: The sign-off sentence the price paragraph carries (``pricing.price_paragraph``). The model
#: sometimes appends it to the closing question as well, so the message ends up with it twice;
#: ``pricing.apply_price`` strips the stray copy and this rule catches a regression.
PRICE_DISCLAIMER = "Wycena i czas są orientacyjne, dopasujemy je po szczegółach."

DRAFT_MIN_CHARS = 700
DRAFT_MAX_CHARS = 1100
#: A bullet has to fit one line on a phone.
MAX_BULLET_CHARS = 60
#: Blocks longer than this are a wall of text on a phone.
MAX_SENTENCES_PER_PARAGRAPH = 3
#: The list preview shows ~200–250 characters, so the opening must be tight.
PREVIEW_CHARS = 200
MAX_PREVIEW_SENTENCES = 3
MAX_QUESTIONS = 2

GREETING_RE = re.compile(
    r"^(Cześć|Dzień dobry|Dobry wieczór|Witam|Hello|Hi|Hey)\b[^\n]{0,30},",
    re.IGNORECASE,
)
#: Mixing „Ty” and „Państwo” in one message reads as a glued template.
TY_RE = re.compile(
    r"\b(ty|Tobie|Ciebie|Cię|Twój|Twoja|Twoje|Twoich|Twoim|Twoją|masz|potrzebujesz)\b",
    re.IGNORECASE,
)
PANSTWO_RE = re.compile(
    r"\b(Państwo|Państwa|Państwu|Państwem|Wam|Wasz|Wasza|Wasze|Waszych|Waszej"
    r"|macie|chcecie|robicie|rezerwujecie|piszecie|potrzebujecie|zlecacie|wysyłacie|używacie)\b"
)
NOT_PL_EN_RE = re.compile(
    r"\b(hola|estáis|estais|vuestra?|vuestros|presupuesto|tarifa|plazo|gracias|podemos|"
    r"buenos días|estimado|saludos|cordialmente|sehr geehrte|grüße|danke|können wir|"
    r"bonjour|cordialement|merci)\b",
    re.IGNORECASE,
)
#: The honest sentence about working languages has to be in the FIRST paragraph.
HONESTY_RE = re.compile(
    r"(English and Polish|English or Polish|po angielsku|in English|work in English)", re.IGNORECASE
)
URL_IN_MSG_RE = re.compile(r"https?://[^\s<>()\"'\]]+")
AMOUNT_RE = re.compile(
    r"(?P<cur1>USD|EUR|PLN|\$|€)\s*(?P<num1>\d[\d\s.,]*)"
    r"|(?P<num2>\d[\d\s.,]*)\s*(?P<cur2>zł|USD|EUR|PLN)",
    re.IGNORECASE,
)
PRICE_RE = re.compile(r"\d[\d\s.,]*\s*(zł|pln|usd|eur|gbp|\$|€)|(\$|€)\s*\d", re.IGNORECASE)
#: A paragraph that opens with an amount ("1 800 zł, ok. 5 dni") — the contract's price block.
PRICE_AT_START_RE = re.compile(r"^\s*(?:\$|€|\d)")
#: Widełki: two numbers joined by a dash, with a currency after the pair ("1 500–2 500 zł").
#: A dash inside "ok. 5-7 dni" is not a price range, so the currency has to be there.
PRICE_RANGE_RE = re.compile(
    r"\d[\d\s.,]*\s*[-–—]\s*\d[\d\s.,]*\s*(?:zł|pln|usd|eur|gbp|\$|€)", re.IGNORECASE
)
BULLET_RE = re.compile(r"^\s*[-*•]\s+(?P<body>.+)$")
SENTENCE_END_RE = re.compile(r"[.!?…](?:\s|$)")

SIGNATURE = ("Jan Noga", "jnxautomation.pl", "+48 600 000 000")
SIGNATURE_DOMAIN = SIGNATURE[1]
SIGNATURE_PHONE = SIGNATURE[2]
PHONE_IN_SIGNATURE_MAX_INDEX = 1

#: Phrases from ``RESEARCH-KONWERSJA.md`` §3 — cheap to check, typical of machine text.
AI_PHRASES: tuple[tuple[str, str], ...] = (
    (r"w dzisiejszym dynamicznym świecie", "w dzisiejszym dynamicznym świecie"),
    (r"kompleksow\w+ rozwiązani\w+", "kompleksowe rozwiązanie"),
    (r"nie tylko[^.!?\n]{0,60}?ale także", "nie tylko… ale także"),
    (r"szerok\w+ wachlarz", "szeroki wachlarz"),
    (r"płynn\w+ integracj\w+", "płynna integracja"),
    (r"kluczow\w+ element", "kluczowy element"),
    (r"warto podkreślić", "warto podkreślić"),
    (r"podsumowując", "podsumowując"),
    (r"w dobie\b", "w dobie"),
    (r"niesamowit\w+", "niesamowity"),
)


def strip_price_marker(message: str) -> str:
    """The message as it will read after Jan inserts the price in place of the marker."""
    lines = [line for line in message.splitlines() if line.strip() != PRICE_MARKER]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def marker_count(message: str) -> int:
    return sum(1 for line in message.splitlines() if line.strip() == PRICE_MARKER)


def _amounts(message: str) -> list[str]:
    return [match.group(0).strip() for match in AMOUNT_RE.finditer(message)]


def price_paragraph(message: str) -> str | None:
    """The block that starts with an amount, or ``None`` when there is none."""
    for block in _blocks(message):
        if PRICE_AT_START_RE.match(block.strip()):
            return block
    return None


def _blocks(message: str) -> list[str]:
    return [block for block in re.split(r"\n\s*\n", message.strip()) if block.strip()]


def _bullets(message: str) -> list[str]:
    bodies: list[str] = []
    for line in message.splitlines():
        match = BULLET_RE.match(line)
        if match:
            bodies.append(match.group("body").strip())
    return bodies


def _content_blocks(message: str) -> list[str]:
    """Message blocks minus the greeting and the three-line signature."""
    blocks = _blocks(message)
    if not blocks:
        return []
    if GREETING_RE.match(blocks[0].strip().splitlines()[0].strip()):
        first_lines = blocks[0].strip().splitlines()
        if len(first_lines) <= 2:
            blocks = blocks[1:]
        else:
            blocks[0] = "\n".join(first_lines[1:])
    if blocks and _is_signature_block(blocks[-1]):
        blocks = blocks[:-1]
    return blocks


def _is_signature_block(block: str) -> bool:
    lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
    return lines[-3:] == list(SIGNATURE) if len(lines) >= 3 else lines == list(SIGNATURE)


def _sentences(text: str) -> int:
    return len(SENTENCE_END_RE.findall(text))


def _bullet_problems(message: str) -> list[str]:
    bullets = _bullets(message)
    if not bullets:
        return []
    problems: list[str] = []
    if any(re.match(r"\*\*|^\d+[.)]\s", body) for body in bullets):
        problems.append("punkt z pogrubioną albo numerowaną etykietą")
    if any(re.match(r"^[^\s:][^:.!?\n]{0,38}:\s", body) for body in bullets):
        problems.append("punkt zbudowany jako „etykieta: zdanie”")
    if any(body.endswith((".", ":", ";")) for body in bullets):
        problems.append("punkt kończy się kropką albo dwukropkiem")
    if any(body[:1].isupper() for body in bullets):
        problems.append("punkt zaczyna się wielką literą")
    if any(len(body) > MAX_BULLET_CHARS for body in bullets):
        problems.append(f"punkt dłuższy niż {MAX_BULLET_CHARS} znaków")
    if len(bullets) == 3:
        lengths = {len(body) for body in bullets}
        words = {len(body.split()) for body in bullets}
        widest, narrowest = max(lengths), min(lengths)
        if len(words) == 1 and widest - narrowest <= max(2, widest // 20):
            problems.append("trzy punkty o identycznej długości")
    return problems


def _ai_smell_problems(message: str) -> list[str]:
    problems: list[str] = []
    lowered = message.lower()
    for pattern, label in AI_PHRASES:
        if re.search(pattern, lowered):
            problems.append(f"AI-izm: „{label}”")
    if re.search(r"\s—\s", message):
        problems.append("długi myślnik (—) zamiast przecinka")
    for block in _content_blocks(message):
        if _sentences(block) > MAX_SENTENCES_PER_PARAGRAPH:
            problems.append("akapit dłuższy niż 3 zdania")
            break
    if _sentences(message[:PREVIEW_CHARS]) > MAX_PREVIEW_SENTENCES:
        problems.append(f"więcej niż {MAX_PREVIEW_SENTENCES} zdania w pierwszych {PREVIEW_CHARS} znakach")
    if message.count("?") > MAX_QUESTIONS:
        problems.append(f"więcej niż {MAX_QUESTIONS} pytania (maszynowy wzorzec)")
    problems.extend(_bullet_problems(message))
    return problems


def _signature_problems(message: str) -> list[str]:
    problems: list[str] = []
    lines = [line.strip() for line in message.rstrip().splitlines() if line.strip()]
    if lines[-3:] != list(SIGNATURE):
        problems.append("podpis nie jest dokładnie w trzech wierszach (Jan Noga / jnxautomation.pl / telefon)")
    if SIGNATURE_PHONE not in message:
        problems.append("brak telefonu w podpisie")
    if SIGNATURE_DOMAIN not in message:
        problems.append("brak linku do strony w podpisie")
    if re.search(r"Nogaj|@gmail|@wp\.pl|@o2\.pl|@onet", message, re.IGNORECASE):
        problems.append("podpis z podmienionymi danymi")
    return problems


def _price_problems(message: str, *, priced: bool) -> tuple[list[str], int]:
    """Price problems plus the length the character window should be measured on."""
    problems: list[str] = []
    markers = marker_count(message)
    if priced:
        if markers:
            problems.append("znacznik wyceny w wiadomości z ceną")
        if not PRICE_RE.search(message):
            problems.append("brak ceny")
        else:
            block = price_paragraph(message)
            if block is None:
                problems.append("akapit z ceną nie zaczyna się od kwoty")
            else:
                amounts = _amounts(block)
                if len(amounts) > 1 or PRICE_RANGE_RE.search(block):
                    problems.append("widełki w akapicie z ceną (jedna liczba na pierwszy krok)")
        if message.count(PRICE_DISCLAIMER) > 1:
            problems.append("zdanie o orientacyjności wyceny występuje więcej niż raz")
        return problems, len(message)
    if markers == 0:
        problems.append("brak znacznika wyceny")
    elif markers > 1:
        problems.append(f"znacznik wyceny występuje {markers} razy")
    stray = _amounts(strip_price_marker(message))
    if stray:
        problems.append("kwota w drafcie (drafty idą bez ceny): " + ", ".join(stray[:3]))
    return problems, len(strip_price_marker(message))


def check_draft(message: str, *, context: str = "", priced: bool = False) -> tuple[str, ...]:
    """Every problem with this message, in a fixed order (empty ⇒ it may go out).

    ``context`` is the full offer text plus the portfolio block, when available:
    it turns "the link must come from the portfolio" into a real check instead of
    a promise, and it enables the foreign-language rule.
    """
    if not isinstance(message, str):
        raise TypeError(f"message must be a str, got {type(message).__name__}")
    text = message.strip()
    problems: list[str] = []
    price_problems, measured_chars = _price_problems(text, priced=priced)
    problems.extend(price_problems)
    if measured_chars > DRAFT_MAX_CHARS:
        problems.append(f"za długa ({measured_chars} znaków, limit {DRAFT_MAX_CHARS})")
    elif measured_chars < DRAFT_MIN_CHARS:
        problems.append(f"za krótka ({measured_chars} znaków, minimum {DRAFT_MIN_CHARS})")
    if NOT_PL_EN_RE.search(text):
        problems.append("język inny niż polski albo angielski")
    if text.count("http") > 1:
        problems.append("więcej niż jeden link")
    if TY_RE.search(text) and PANSTWO_RE.search(text):
        problems.append('mieszana forma: „Ty” i „Państwo” w jednej wiadomości')
    if context:
        allowed = {url.rstrip(".,;") for url in URL_IN_MSG_RE.findall(context)}
        allowed.add("https://jnxautomation.pl")
        foreign = [
            url
            for url in URL_IN_MSG_RE.findall(text)
            if url.rstrip(".,;)") not in allowed
        ]
        if foreign:
            problems.append("link spoza portfolio (" + ", ".join(foreign) + ")")
        opening = " ".join(_blocks(text)[:2])
        if NOT_PL_EN_RE.search(context) and not HONESTY_RE.search(opening):
            problems.append("brak zdania o języku pracy w pierwszym akapicie (ogłoszenie obcojęzyczne)")
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    if not GREETING_RE.match(first_line):
        problems.append("brak powitania w pierwszej linii")
    problems.extend(_signature_problems(text))
    problems.extend(_ai_smell_problems(text))
    return tuple(problems)


def summarise(problems: Iterable[str]) -> str:
    """One line for a log or a cron report."""
    collected = list(problems)
    if not collected:
        return "OK"
    return "; ".join(collected)


__all__ = [
    "AI_PHRASES",
    "DRAFT_MAX_CHARS",
    "DRAFT_MIN_CHARS",
    "MAX_BULLET_CHARS",
    "PRICE_DISCLAIMER",
    "PRICE_MARKER",
    "RULES_VERSION",
    "SIGNATURE",
    "check_draft",
    "marker_count",
    "price_paragraph",
    "strip_price_marker",
    "summarise",
]
