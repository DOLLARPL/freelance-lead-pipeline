#!/bin/sh
# Snajper v2: rolling lista — drafty z wycenami, plik i wysyłka jako dokument na Telegram.
#
# Osobny krok od kolekcji (05:30 UTC), bo:
#   * kolekcja ma zostawić `leads-*.json` z treścią ogłoszeń (bez ponownego pobierania),
#   * pisanie wiadomości i wycena to wywołania modelu, które nie mogą opóźniać przebiegu,
#   * lista jest wysyłką pliku (`sendDocument`), a nie tekstu w czacie (20-60 kB gubi numerację).
#
# Cisza: brak nowych ofert ⇒ pusty stdout ⇒ żadnej wiadomości (wzorzec z pozostałych cronów).
# Błąd wysyłki ⇒ komunikat na stderr i kod 3; statusy zostają `new`, więc oferta wróci rano.
#
# Test ręczny:
#   SNAJPER_STATE_DIR=/tmp/probe-rolling /opt/data/scripts/snajper-rolling.sh --dry-run
set -eu

REPO=${SNAJPER_REPO:-/opt/data/projects/snajper-ofert}
STATE_DIR=${SNAJPER_STATE_DIR:-/opt/data/cache/snajper-v2}
ENV_FILE=${SNAJPER_ENV_FILE:-/opt/data/.env}

if [ ! -r "$ENV_FILE" ]; then
    printf '%s\n' "snajper rolling: cannot read environment file: $ENV_FILE" >&2
    exit 2
fi

# Klucze wyciągamy bez wypisywania ich na stdout/stderr.
read_key() {
    python3 - "$ENV_FILE" "$1" <<'PY'
import sys
from pathlib import Path

for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() == sys.argv[2]:
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[0] in "\"'":
            value = value[1:-1]
        print(value, end="")
        break
PY
}

OPENROUTER_API_KEY=$(read_key OPENROUTER_API_KEY)
export OPENROUTER_API_KEY

if [ -z "$OPENROUTER_API_KEY" ]; then
    printf '%s\n' "snajper rolling: OPENROUTER_API_KEY missing in $ENV_FILE" >&2
    exit 2
fi

mkdir -p "$STATE_DIR/reports"
cd "$REPO"
# Dostawa tylko przez panel PWA (decyzja Jana, 2026-11-04): bez `--send`, ale z
# `--mark-delivered`, żeby drafty i statusy `seen` trafiły do rejestru. Bez tego oferty
# wracają na kolejną listę w nieskończoność.
# `--docs` dokłada wersję do czytania w Google Docs, a gdy Google nie odpowie, lista i tak leci.
# `--expire-hours` to okno świeżości: oferty widziane i nieobsłużone dłużej niż próg
# nie wchodzą na listę (SNAJPER_MAX_AGE_HOURS, domyślnie 12).
exec uv run python scripts/rolling_report.py --state-dir "$STATE_DIR" --mark-delivered --docs \
    --expire-hours "${SNAJPER_MAX_AGE_HOURS:-12}" "$@"
