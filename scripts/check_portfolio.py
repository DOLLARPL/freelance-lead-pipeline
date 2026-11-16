#!/usr/bin/env python3
"""Sprawdza, czy każdy projekt z portfolio odpowiada 200 na jnxautomation.pl.

Uruchomienie:
    cd /opt/data/projects/snajper-ofert && PYTHONPATH=src .venv/bin/python scripts/check_portfolio.py

Kod wyjścia 1, gdy którykolwiek link jest martwy. Dzięki temu draft nie linkuje
do projektu, którego nie ma na stronie.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request

from freelance_pipeline.portfolio import load_portfolio

UA = "Mozilla/5.0 (compatible; JNX-portfolio-check/1.0)"


def status(url: str) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return 0


def main() -> int:
    items = load_portfolio()
    if not items:
        print("Brak portfolio do sprawdzenia.")
        return 1
    bad = 0
    for item in items:
        code = status(item.url)
        flag = "ok" if code == 200 else "MARTWY"
        print(f"{code:3} {flag:7} {item.url}")
        if code != 200:
            bad += 1
    print(f"\nSprawdzone: {len(items)}, martwe: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
