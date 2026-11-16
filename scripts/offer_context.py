"""Context for writing one reply to one offer: the offer itself + the live portfolio.

Usage::

    uv run python scripts/offer_context.py --kind top --index 3
    uv run python scripts/offer_context.py --kind moze --index M5
    uv run python scripts/offer_context.py --portfolio

Prints plain text: the full offer (title, source, url, budget pill, description)
followed by the case studies that are actually on jnxautomation.pl right now, so a
proposal can only cite projects that exist.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from html import unescape
from pathlib import Path

REPORT_DIRS = (
    Path("/opt/data/cache/snajper-v2/reports"),
    Path("/opt/data/cache/snajper-v2/verify/reports"),
)
PATTERNS = {"top": "top-*.txt", "moze": "moze-*.txt", "zamien": "zamien-*.txt"}
SITE_PROJECTS = Path("/opt/data/work/jnx-automation-copy/src/content/projects")
SITE_URL = "https://jnxautomation.pl"

FRONT_RE = re.compile(r"^(title|slug|result):\s*(.+)$", re.MULTILINE)
ROW_RE = re.compile(r"^\[(TOP|MOŻE|ODRZUĆ|ZAMIEN)\]\s+(\d+)/100\s+—\s+(.*)$")


def newest(kind: str) -> Path | None:
    found: list[Path] = []
    for directory in REPORT_DIRS:
        if directory.is_dir():
            found.extend(directory.glob(PATTERNS[kind]))
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def offers(kind: str) -> list[dict[str, str]]:
    path = newest(kind)
    if path is None:
        return []
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ROW_RE.match(line)
        if match:
            current = {"bucket": match.group(1), "score": match.group(2), "title": match.group(3).strip()}
            items.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        for key, field in (("source:", "source"), ("company/author:", "author"), ("url:", "url"), ("rationale:", "rationale")):
            if stripped.startswith(key):
                current[field] = stripped[len(key) :].strip()
    return items


def portfolio() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    if SITE_PROJECTS.is_dir():
        for path in sorted(SITE_PROJECTS.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            fields = {k: v.strip() for k, v in FRONT_RE.findall(text)}
            slug = fields.get("slug") or path.stem
            if path.name.endswith(".en.md"):
                # Wersja EN nie ma frontmattera, a jej adres to /en/projects/<slug>
                # (sprawdzone 2026-10-04: /projekty/<slug>.en zwraca 404).
                base = path.name[: -len(".en.md")]
                url = f"{SITE_URL}/en/projects/{base}"
                title = fields.get("title", base)
            else:
                url = f"{SITE_URL}/projekty/{slug}"
                title = fields.get("title", slug)
            entries.append(
                {
                    "title": title,
                    "slug": slug,
                    "result": fields.get("result", ""),
                    "url": url,
                }
            )
    return entries


def render_portfolio() -> str:
    lines = [f"# Portfolio na żywo ({SITE_URL}) — cytuj TYLKO te projekty"]
    for entry in portfolio():
        lines.append(f"- {entry['title']}")
        lines.append(f"  efekt: {entry['result']}")
        lines.append(f"  link: {entry['url']}")
    if not portfolio():
        lines.append("- (nie znaleziono plików projektów — nie cytuj żadnego case study)")
    lines.append(f"- strona główna: {SITE_URL}")
    return "\n".join(lines)


def render_offer(kind: str, index: str) -> str:
    items = offers(kind)
    if not items:
        return f"Brak plików raportu dla {kind!r} — nie było jeszcze przebiegu."
    normalized = index.strip().lstrip("Mm")
    try:
        position = int(normalized)
    except ValueError:
        return f"Nie rozumiem numeru {index!r}; podaj np. 3 albo M5."
    lookup = items[position - 1 : position] if kind == "top" else _pick_moze(items, position)
    if not lookup:
        return f"Nie ma pozycji {position} w {kind} — lista ma {len(items)} pozycji."
    entry = lookup[0]
    lines = [
        f"# Oferta [{entry['bucket']} {entry['score']}/100] {entry['title']}",
        f"źródło: {entry.get('source', '?')} · autor: {entry.get('author', '?') or '—'}",
        f"url: {entry.get('url', '?')}",
        f"ocena selektora: {entry.get('rationale', '')}",
        "",
        "## Pełna treść ogłoszenia",
        _full_description(entry.get("url", "")),
    ]
    return "\n".join(lines)


def _pick_moze(items: list[dict[str, str]], position: int) -> list[dict[str, str]]:
    return items[position - 1 : position]


def _full_description(url: str) -> str:
    """Fetch one offer's real text on demand (no scraping infrastructure).

    Discourse communities (n8n, Make) answer ``<topic>.json``; everything else is
    fetched as HTML and reduced to readable text. Failures are reported honestly
    instead of inventing content.
    """
    if not url.startswith("http"):
        return "(brak adresu — nie mogę pobrać treści)"
    try:
        if "community.n8n.io" in url or "community.make.com" in url:
            raw = _http(url.rstrip("/") + ".json")
            payload = json.loads(raw)
            posts = payload.get("post_stream", {}).get("posts", [])
            if not posts:
                return "(brak treści w JSON-ie tematu)"
            return _html_to_text(posts[0].get("cooked", ""))[:4000]
        return _html_to_text(_http(url))[:4000]
    except Exception as exc:  # noqa: BLE001 - network/parse problems are reported, not hidden
        return f"(nie udało się pobrać treści: {exc}) — otwórz {url}"


def fetch_full_text(url: str) -> str:
    """Pełna treść ogłoszenia z jego strony (publiczny wrapper na ``_full_description``).

    Potrzebne, bo adapter Useme oddaje w raporcie tylko skrót (~250 znaków, ucięty w połowie
    zdania), a draft pisany z takiego skrawka jest loterią. Wołający ma traktować każdą
    odpowiedź zaczynającą się od ``(`` jako porażkę pobrania.
    """
    return _full_description(url)


def _http(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "pl,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", "replace")


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="offer_context", description=__doc__)
    parser.add_argument("--kind", choices=sorted(PATTERNS), default="top")
    parser.add_argument("--index")
    parser.add_argument("--portfolio", action="store_true", help="tylko portfolio")
    args = parser.parse_args(argv)

    if args.portfolio:
        print(render_portfolio())
        return 0
    if args.index:
        print(render_offer(args.kind, args.index))
        print()
    print(render_portfolio())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
