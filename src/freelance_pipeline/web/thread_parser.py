"""Parser wklejonego watku wiadomosci z portalu (UseMe, Oferteo, mail).

Jan kopiuje ze strony UseMe caly widok watku (Ctrl+A, Ctrl+C) albo sam ciag
wiadomosci. W takim tekscie siedzi menu, stopka, przyciski typu "Pokaż więcej",
godziny w nawiasach i naglowek nadawcy powtarzany przy kazdej wypowiedzi.

Modul jest czysty: zero bazy, zero HTTP, wiec da sie go testowac wprost.
Wyjscie to lista slownikow::

    {"kierunek": "klient"|"ja", "autor": str, "tresc": str, "czas": str}

Kolejnosc jest kolejnosc wklejenia. ``czas`` zostaje taki, jak w zrodle
("14:32", "2026-10-28 14:32"), bo panel tylko go pokazuje.
"""

from __future__ import annotations

import re
from typing import Iterable

#: Imiona/nick Jana i slowa, ktorymi portal opisuje jego wlasne wypowiedzi.
SELF_NAMES = frozenset({"jan", "jan noga", "ja", "ty", "you", "me", "mnie", "moja wiadomosc"})
#: Slowa, ktorymi portal opisuje rozmowce.
CLIENT_NAMES = frozenset(
    {"klient", "klientka", "zleceniodawca", "client", "on", "ona", "rozmowca", "useme"}
)

#: Maksymalna dlugosc jednej wiadomosci po parsowaniu. Dluzszy blok jest
#: rozbijany, zeby wklejenie calej strony nie robilo jednej sciany tekstu.
MAX_MESSAGE_CHARS = 1200

_MONTHS = (
    "stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|wrzesnia|"
    "października|pazdziernika|listopada|grudnia|sty|mar|kwi|cze|lip|sie|wrz|paz|lis|gru"
)
_DATE = (
    rf"(?:\d{{1,2}}\.\d{{1,2}}\.\d{{2,4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})(?:\s+20\d\d)?"
    rf"|20\d\d-\d\d-\d\d"
    rf"|dziś|dzis|dzisiaj|wczoraj|przedwczoraj|today|yesterday)"
)
_TIME = r"(?:\d{1,2}:\d{2}(?::\d{2})?)"
_STAMP_ALT = rf"(?:(?:{_DATE})(?:\s*,?\s*{_TIME})?|{_TIME})"

_STAMP_PAREN = re.compile(rf"[\(\[]\s*(?P<s>{_STAMP_ALT})\s*[\)\]]\s*$", re.I)
_STAMP_TAIL = re.compile(rf"(?:^|[\s·|,–—–-]+)(?P<s>{_STAMP_ALT})\s*$", re.I)
#: "Anna Kowalska: dzień dobry" albo "Jan: tak, robimy" - etykieta w linii.
_INLINE_LABEL = re.compile(r"^(?P<name>[^:]{2,32}?)\s*:\s+(?P<body>\S.*)$", re.UNICODE)

_STAMP_ONLY = re.compile(rf"^\s*[\(\[]?\s*(?P<s>{_STAMP_ALT})\s*[\)\]]?\s*$", re.I)

#: Linie, ktore sa wylacznie elementem interfejsu portalu. Porownanie po
#: malej literze, bez interpunkcji na koncach, wiec "Pokaż więcej »" tez wpada.
NOISE_EXACT = frozenset(
    {
        # przyciski i akcje
        "pokaż więcej", "pokaz wiecej", "pokaż mniej", "pokaz mniej", "zobacz więcej",
        "zobacz wiecej", "wczytaj więcej", "wczytaj wiecej", "wczytaj starsze",
        "wczytaj starsze wiadomosci", "wczytaj starsze wiadomości", "show more",
        "load more", "read more", "odpowiedz", "reply", "odpowiedz na wiadomość",
        "odpowiedz na wiadomosc", "napisz wiadomość", "napisz wiadomosc", "napisz",
        "wyślij", "wyslij", "wyślij wiadomość", "wyslij wiadomosc", "wyślij ofertę",
        "wyslij oferte", "załącz plik", "zalacz plik", "załącz pliki", "zalacz pliki",
        "dodaj załącznik", "dodaj zalacznik", "usuń", "usun", "edytuj", "zapisz",
        "anuluj", "zamknij", "dalej", "wstecz", "kopiuj", "udostępnij", "udostepnij",
        "zgłoś", "zglos", "zgłoś nadużycie", "zgloś nadużycie", "oznacz jako przeczytane",
        "oznacz wszystkie jako przeczytane", "odznacz", "archiwizuj", "więcej", "wiecej",
        # naglowki portalu i sekcji
        "oferty", "oferta", "zlecenia", "zlecenie", "freelancerzy", "freelancer",
        "jak to działa", "jak to dziala", "cennik", "ceny", "pomoc", "centrum pomocy",
        "regulamin", "polityka prywatności", "polityka prywatnosci", "kontakt", "o nas",
        "blog", "kariera", "kategorie", "wszystkie kategorie", "szukaj", "menu",
        "strona główna", "strona glowna", "useme", "useme.com", "www.useme.com",
        "wiadomości", "wiadomosci", "skrzynka", "skrzynka odbiorcza", "wątek", "watek",
        "rozmowa", "historia wiadomości", "historia wiadomosci", "moje konto", "konto",
        "ustawienia", "powiadomienia", "profil", "wyloguj", "zaloguj", "zaloguj się",
        "zaloguj sie", "zarejestruj", "zarejestruj się", "zarejestruj sie", "rejestracja",
        "online", "offline", "aktywny", "aktywna", "ostatnio widziany", "ostatnio widziana",
        "zweryfikowany", "zweryfikowana", "premium", "status", "temat", "data", "godzina",
        "załączniki", "zalaczniki", "e-mail", "email", "telefon", "telefon komórkowy",
        "imię i nazwisko", "imie i nazwisko", "ocena", "opinie", "opinie o kliencie",
        "informacje", "szczegóły", "szczegoly", "podsumowanie", "podsumowanie zlecenia",
        "budżet", "budzet", "termin", "termin realizacji", "lokalizacja",
        # stopka i cookies
        "wszelkie prawa zastrzeżone", "wszelkie prawa zastrzezone", "wszelkie prawa zastrzeżone.",
        "akceptuję", "akceptuje", "akceptuj wszystkie", "zaakceptuj", "zaakceptuj wszystkie",
        "cookies", "pliki cookie", "polityka cookies", "zamknij baner", "zgoda",
        "regulamin serwisu", "mapa strony", "dla freelancerów", "dla freelancerow",
        "dla zleceniodawców", "dla zleceniodawcow", "jak zatrudniać", "jak zatrudniac",
    }
)

#: Etykiety metadanych przy naglowku rozmowcy ("Zleceniodawca · zweryfikowany").
META_TOKENS = frozenset(
    {
        "zleceniodawca", "zleceniodawczyni", "freelancer", "wykonawca", "klient", "klientka",
        "zweryfikowany", "zweryfikowana", "online", "offline", "aktywny", "aktywna",
        "premium", "nowy", "nowa", "useme", "rozmowca", "ty", "ja",
    }
)

#: Wzorce szumu, ktory ma zmienny ogon (licznika, copyright, adres strony).
NOISE_PATTERNS = (
    re.compile(r"^(?:©|\(c\)|copyright)\s*(?:20\d\d\s*[-–]?\s*)?.*$", re.I),
    re.compile(r"^©?\s*20\d\d\s+useme.*$", re.I),
    re.compile(r"^wszelkie prawa zastrze[zż]one.*$", re.I),
    re.compile(r"^©.*$", re.I),
    re.compile(r"^regulamin\s+polityka.*$", re.I),
    re.compile(r"^pokaż więcej.*$", re.I),
    re.compile(r"^pokaz wiecej.*$", re.I),
    re.compile(r"^wczytaj więcej.*$", re.I),
    re.compile(r"^wczytaj starsze.*$", re.I),
    re.compile(r"^załącznik[io]?:?.*$", re.I),
    re.compile(r"^zalacznik[io]?:?.*$", re.I),
    re.compile(r"^wiadomość \d+ z \d+$", re.I),
    re.compile(r"^wiadomosc \d+ z \d+$", re.I),
    re.compile(r"^\d+\s+(?:nowych?|nieprzeczytanych?)\s+wiadomo", re.I),
    re.compile(r"^(?:strona|page)\s+\d+\s*(?:z|/)\s*\d+$", re.I),
    re.compile(r"^[-_=*~•·.\s]{3,}$"),
    re.compile(r"^(?:www\.)?[a-z0-9-]+\.(?:com|pl|net|io|co|org)(?:/[\w\-./?=#%]*)?$", re.I),
    re.compile(r"^https?://\S+$", re.I),
)

#: Slowa, ktore nigdy nie sa imieniem, choc zaczynaja sie wielka litera.
_NOT_NAME = frozenset(
    {
        "dzień", "dzien", "witam", "cześć", "czesc", "hej", "hello", "cześć!", "ok",
        "okej", "tak", "nie", "proszę", "prosze", "dziękuję", "dziekuje", "pozdrawiam",
        "witaj", "dobry", "dobra", "dobrze", "thanks", "hi", "hey", "super", "jasne",
        "uwaga", "ps", "p.s.", "info", "re", "fw", "odp", "temat", "notatka",
    }
)

_WORD_RE = re.compile(r"^[^\W\d_][\w'’.\-]*$", re.UNICODE)

#: Slowa, z ktorych sklada sie pasek nawigacji portalu. Linia zlozona wylacznie
#: z nich (3+ slowa) to menu albo stopka, nie wypowiedz w watku.
_UI_WORDS = frozenset(
    [word for phrase in NOISE_EXACT for word in phrase.split()]
    + [word for phrase in META_TOKENS for word in phrase.split()]
    + [
        "jak", "to", "działa", "dziala", "moje", "konto", "się", "sie", "z", "w", "na",
        "dla", "o", "nas", "i", "oraz", "nowe", "wszystkie", "moich", "moje", "twoje",
        "główne", "glowne", "panel", "pulpit", "zleceń", "zlecen", "ofert", "wyszukaj",
    ]
)


def _squash(text: str) -> str:
    """Zwijanie bialych znakow, zeby porownania linii byly odporne na wciecia."""
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def _strip_line(text: str) -> str:
    """Usuwa znaki kontrolne i zerowej szerokosci, zostawia tresc i spacje."""
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\u00ad", "")
    text = text.replace("\t", " ").replace("\r", " ")
    return "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)


def _is_noise(line: str) -> bool:
    low = _squash(line).lower()
    if not low:
        return True
    probe = low.strip(" .,!?:;»«\"'()[]-–—*_~|")
    # liczniki przy przyciskach: "Pokaż więcej (12)" / "Odpowiedz 3"
    probe = re.sub(r"\s*\(\d+\)\s*$", "", probe)
    probe = re.sub(r"\s+\d{1,3}$", "", probe)
    if probe in NOISE_EXACT:
        return True
    if any(p.match(low) for p in NOISE_PATTERNS):
        return True
    # Wiele elementow menu w jednej linii: "Oferty  Zlecenia  Cennik  Kontakt".
    chunks = [c for c in re.split(r"\s{2,}|\s*[·|]\s*", low) if c]
    if len(chunks) >= 2:
        cleaned = [c.strip(" .,!?:;»«\"'()[]-–—") for c in chunks]
        if all(c in NOISE_EXACT or c in META_TOKENS or not c for c in cleaned):
            return True
        # "Zleceniodawca · zweryfikowany" - krotkie etykiety bez czasownika
        if len(cleaned) == 2 and all(c in META_TOKENS for c in cleaned):
            return True
    # Okruszki: "Strona główna > Wiadomości > Wątek".
    crumbs = [c.strip() for c in re.split(r"\s*[>›»]\s*", low) if c.strip()]
    if len(crumbs) >= 2:
        crumb_words = [w for c in crumbs for w in c.split()]
        if all(w.strip(".,!?") in _UI_WORDS for w in crumb_words):
            return True
    # Pasek nawigacji po skopiowaniu strony: same elementy menu, spacjami.
    words = [w.strip(" .,!?:;»«\"'()[]-–—").lower() for w in low.split()]
    if len(words) >= 3 and len(low) <= 80 and all(w in _UI_WORDS for w in words):
        return True
    return False


def _take_stamp(line: str) -> tuple[str, str]:
    """Oddziela znacznik czasu/daty od konca linii. Zwraca (reszta, czas)."""
    text = _squash(line)
    match = _STAMP_PAREN.search(text)
    if match:
        return text[: match.start()].strip(" ·|,–—-"), _clean_stamp(match.group("s"))
    match = _STAMP_TAIL.search(text)
    if match and match.start() > 0:
        return text[: match.start()].strip(" ·|,–—-"), _clean_stamp(match.group("s"))
    return text, ""


def _clean_stamp(stamp: str) -> str:
    stamp = _squash(stamp).strip("()[],.")
    # "dziś, 14:32" -> "dziś 14:32"; samo "14:32" zostaje
    return re.sub(r",\s*", " ", stamp)


def _looks_like_name(base: str) -> bool:
    """Krotka linia z samymi wielka litera: kandydat na naglowek nadawcy."""
    if not base or len(base) > 42:
        return False
    if base[-1] in ".!?:;,…":
        return False
    if any(ch.isdigit() for ch in base):
        return False
    words = base.split()
    if not 1 <= len(words) <= 4:
        return False
    if sum(len(word) for word in words) < 2:
        return False
    for word in words:
        clean = word.strip(",.()[]")
        if not clean or not _WORD_RE.match(clean):
            return False
        if clean.lower() in _NOT_NAME:
            return False
        if not clean[0].isupper():
            return False
    return True


def _role_for(name: str) -> str:
    key = _squash(name).lower().strip(" .,:;")
    if key in SELF_NAMES:
        return "ja"
    if key in CLIENT_NAMES:
        return "klient"
    return "klient"


def _chunk(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Dzieli dlugi blok na kawalki po akapitach, potem po zdaniach."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    buffer = ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        candidate = f"{buffer}\n\n{block}".strip() if buffer else block
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
            buffer = ""
        if len(block) <= limit:
            buffer = block
            continue
        for sentence in re.split(r"(?<=[.!?…])\s+", block):
            sentence = sentence.strip()
            if not sentence:
                continue
            candidate = f"{buffer} {sentence}".strip() if buffer else sentence
            if len(candidate) <= limit:
                buffer = candidate
            else:
                if buffer:
                    parts.append(buffer)
                # pojedyncze zdanie dluzsze niz limit: tniemy po spacji
                while len(sentence) > limit:
                    cut = sentence.rfind(" ", 0, limit)
                    cut = cut if cut > limit // 2 else limit
                    parts.append(sentence[:cut].strip())
                    sentence = sentence[cut:].strip()
                buffer = sentence
    if buffer:
        parts.append(buffer)
    return [part for part in parts if part]


def _finish(messages: list[dict]) -> list[dict]:
    """Sprzata wynik: puste wiadomosci, powtorzenia, dlugosc, biale znaki."""
    out: list[dict] = []
    for message in messages:
        body = (message.get("tresc") or "").strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        body = "\n".join(line.rstrip() for line in body.split("\n")).strip()
        body = re.sub(r"^(?:pokaż więcej|pokaz wiecej|zobacz więcej)\s*$", "", body, flags=re.I | re.M)
        body = body.strip()
        if not body:
            continue
        direction = "ja" if message.get("kierunek") == "ja" else "klient"
        for piece in _chunk(body):
            if out and out[-1]["tresc"] == piece and out[-1]["kierunek"] == direction:
                continue  # sklejone duplikaty po skopiowaniu strony dwa razy
            out.append(
                {
                    "kierunek": direction,
                    "autor": _squash(match_author(message.get("autor", ""))),
                    "tresc": piece,
                    "czas": _squash(message.get("czas", "")),
                }
            )
    return out


def match_author(name: str) -> str:
    return name or ""


def parse_thread(text: str, self_names: Iterable[str] | None = None) -> list[dict]:
    """Zamienia wklejony tekst watku na liste wiadomosci.

    Kolejne proby, od najbardziej pewnej:

    1. naglowki nadawcy (imie/avatar, opcjonalnie z godzina) powtarzane w tekscie,
    2. naprzemienne bloki akapitow (klient, ja, klient, ...),
    3. jeden blok: dzielony na kawalki, zeby nie bylo jednej sciany tekstu.
    """
    if not text or not text.strip():
        return []

    self_set = {_squash(name).lower() for name in (self_names or SELF_NAMES)}

    raw_lines = _strip_line(text.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
    records: list[tuple[str, str, str]] = []  # (typ, tresc, czas)
    for raw in raw_lines:
        line = _squash(raw)
        if not line:
            if records and records[-1][0] != "blank":
                records.append(("blank", "", ""))
            continue
        if _is_noise(line):
            continue
        stamp_only = _STAMP_ONLY.match(line)
        if stamp_only:
            records.append(("stamp", "", _clean_stamp(stamp_only.group("s"))))
            continue
        base, stamp = _take_stamp(line)
        if not base and stamp:
            records.append(("stamp", "", stamp))
            continue
        if _STAMP_ONLY.match(line) and not base:
            records.append(("stamp", "", _clean_stamp(line)))
            continue
        records.append(("text", line, stamp))
    while records and records[-1][0] == "blank":
        records.pop()

    # --- 1. naglowki nadawcy -------------------------------------------------
    counts: dict[str, int] = {}
    stamps: dict[str, str] = {}
    for kind, line, stamp in records:
        if kind != "text":
            continue
        inline = _INLINE_LABEL.match(line)
        if inline and _looks_like_name(inline.group("name").strip()):
            key = inline.group("name").strip().lower()
            counts[key] = counts.get(key, 0) + 1
            continue
        base, _ = _take_stamp(line)
        if not _looks_like_name(base):
            continue
        key = base.lower()
        counts[key] = counts.get(key, 0) + 1
        if stamp and key not in stamps:
            stamps[key] = stamp
    headers = {
        key
        for key, count in counts.items()
        if count >= 2 or key in stamps or key in self_set
    }

    messages: list[dict] = []
    current: dict | None = None
    for kind, line, stamp in (records if headers else []):
        if kind == "blank":
            if current is not None:
                current["tresc"] += "\n\n"
            continue
        if kind == "stamp":
            if current is not None and not current["czas"]:
                current["czas"] = stamp
            continue
        inline = _INLINE_LABEL.match(line)
        if inline:
            label = inline.group("name").strip()
            if label.lower() in headers:
                body = inline.group("body").strip()
                direction = "ja" if label.lower() in self_set else _role_for(label)
                current = {"kierunek": direction, "autor": label, "tresc": body, "czas": ""}
                messages.append(current)
                continue
        base, inline_stamp = _take_stamp(line)
        key = base.lower()
        if key in headers:
            # Naglowek od razu po naglowku (imie, pod nim imie z godzina) to
            # jeden nadawca, nie dwie puste wiadomosci.
            if current is not None and not current["tresc"].strip():
                current["autor"] = base
                current["kierunek"] = "ja" if key in self_set else _role_for(base)
                current["czas"] = current["czas"] or inline_stamp or stamps.get(key, "")
                continue
            autor = base
            if key in self_set or autor.lower() in self_set:
                direction = "ja"
            else:
                direction = _role_for(autor)
            current = {
                "kierunek": direction,
                "autor": autor,
                "tresc": "",
                "czas": inline_stamp or stamps.get(key, ""),
            }
            messages.append(current)
            continue
        if current is None:
            current = {"kierunek": "klient", "autor": "", "tresc": "", "czas": stamp}
            messages.append(current)
        if current["tresc"]:
            current["tresc"] += "\n" + line
        else:
            current["tresc"] = line

    parsed = _finish([m for m in messages if m["tresc"].strip()])

    # --- 2. naprzemienne bloki ---------------------------------------------
    if not parsed:
        blocks: list[str] = []
        buffer: list[str] = []
        for kind, line, _stamp in records:
            if kind == "blank":
                if buffer:
                    blocks.append("\n".join(buffer).strip())
                    buffer = []
                continue
            if kind == "stamp":
                continue
            buffer.append(line)
        if buffer:
            blocks.append("\n".join(buffer).strip())
        blocks = [b for b in blocks if b and not _is_noise(b)]
        # Naprzemiennosc ma sens tylko dla krotkich, rozmownych blokow. Jeden
        # dlugi tekst z akapitami to nadal jedna wiadomosc, nie czat.
        long_blocks = [b for b in blocks if len(b) > 600]
        if len(blocks) > 1 and len(long_blocks) * 2 <= len(blocks):
            parsed = _finish(
                [
                    {
                        "kierunek": "klient" if index % 2 == 0 else "ja",
                        "autor": "",
                        "tresc": block,
                        "czas": "",
                    }
                    for index, block in enumerate(blocks)
                ]
            )
        elif blocks:
            # --- 3. jeden blok: dzielimy, ale kierunek zostaje klienta ------
            parsed = _finish([{"kierunek": "klient", "autor": "", "tresc": blocks[0], "czas": ""}])

    if not parsed:
        parsed = _salvage(text)
    return parsed


def _salvage(text: str) -> list[dict]:
    """Ostatnia deska ratunku: filtr zjadl wszystko, a tekst jest dlugi.

    Lepiej pokazac Janowi brzydki podglad z mozliwoscia korekty niz zero
    wiadomosci po wklejeniu dlugiego watku, w ktorym trafila sie stopka.
    """
    body = _strip_line(text)
    blocks = [_squash_block(part) for part in re.split(r"\n\s*\n", body)]
    blocks = [block for block in blocks if len(block) > 40]
    if not blocks:
        return []
    return _finish(
        [
            {
                "kierunek": "klient" if index % 2 == 0 else "ja",
                "autor": "",
                "tresc": block,
                "czas": "",
            }
            for index, block in enumerate(blocks)
        ]
    )


def _squash_block(text: str) -> str:
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()
