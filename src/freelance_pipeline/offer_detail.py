"""Pobiera pełny opis oferty z Useme na żądanie, z cache.

Scraper listy oddaje skrót (~250 znaków, ucięty w połowie zdania). Dopiero strona
szczegółów ma pełny zakres. Pobieramy ją tylko wtedy, gdy draft ma powstać, a nie
przy każdym przebiegu listy, bo Useme odpowiada 429 na serię żądań.

Gdy bezpośrednie żądanie dostanie 429 (blokada IP VPS), drugą drogą jest
r.jina.ai: publiczny czytnik, który oddaje markdown strony z pominięciem blokady.
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

CACHE_DIR = Path("/opt/data/cache/snajper-v2/detail-cache")
TTL_SECONDS = 7 * 24 * 3600
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.txt"


def _http_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept-Language": "pl,en;q=0.8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _from_html(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
        from freelance_pipeline.sources.useme import UsemeJobs

        text = UsemeJobs._detail_description(soup) or ""
        if len(text) < 120:
            text = UsemeJobs._detail_text(soup)[:4000]
        return text.strip()
    except Exception:  # noqa: BLE001 - parser nie może wywalić draftu
        return ""


def _from_jina(url: str, timeout: float) -> str:
    try:
        text = _http_text(f"https://r.jina.ai/{url}", timeout)
    except Exception:  # noqa: BLE001 - fallback nie może wywalić draftu
        return ""
    match = re.search(r"\nOpis\n\n(.+?)\n\n(?:Opublikowano|Budżet)\b", text, re.S)
    if not match:
        return ""
    body = re.sub(r"\n{3,}", "\n\n", match.group(1)).strip()
    # usuń markdownowe linki i obrazy, zostaw treść
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)
    return body.strip()


def fetch_useme_description(url: str, timeout: float = 20.0) -> str:
    """Zwraca pełny opis albo pusty string. Nie rzuca wyjątkiem na 429."""
    if not url:
        return ""
    cached = _cache_path(url)
    try:
        if cached.is_file() and time.time() - cached.stat().st_mtime < TTL_SECONDS:
            return cached.read_text(encoding="utf-8").strip()
    except OSError:
        pass

    text = ""
    try:
        text = _from_html(_http_text(url, timeout))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        text = ""
    if len(text) < 120:
        text = _from_jina(url, max(timeout, 30.0))

    text = text.strip()
    if len(text) > 4000:
        text = text[:4000].rsplit(" ", 1)[0] + "..."
    if text:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text
