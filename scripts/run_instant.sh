#!/bin/sh
# Snajper v2: tryb natychmiastowy. Nowa oferta = powiadomienie push w panelu (docelowo zamiast Telegrama).
#
# Dlaczego co 10 minut: Jan uznał (2026-10-28), że szybkość odpowiedzi jest najważniejsza,
# a brak nowych ofert ma oznaczać ciszę, nie pusty komunikat.
#
# Dlaczego Useme i Oferteo z maila: Useme to zwykłe HTTP, Oferteo to odczyt skrzynki Gmaila
# (adapter nie otwiera /odkryj i nie kupuje punktów). Facebook idzie osobnym cronem na Apify
# i kosztuje kredyty, więc nie wolno go wołać co kwadrans.
#
# Test ręczny:
#   SNAJPER_STATE_DIR=/tmp/probe-instant /opt/data/scripts/snajper-instant.sh --dry-run
set -eu

REPO=${SNAJPER_REPO:-/opt/data/projects/snajper-ofert}
STATE_DIR=${SNAJPER_STATE_DIR:-/opt/data/cache/snajper-v2}
ENV_FILE=${SNAJPER_ENV_FILE:-/opt/data/.env}
PYTHON=${SNAJPER_PYTHON:-$REPO/.venv/bin/python}
# 1 = wyślij też na Telegram, 0 = tylko panel PWA (ustawienie Jana, 2026-11-04).
TELEGRAM=${SNAJPER_TELEGRAM:-0}
DRY_RUN=0
for arg in "$@"; do
    [ "$arg" = "--dry-run" ] && DRY_RUN=1
done

if [ ! -r "$ENV_FILE" ]; then
    printf '%s\n' "snajper instant: cannot read environment file: $ENV_FILE" >&2
    exit 2
fi

# Klucze wyciągamy bez wypisywania ich na stdout/stderr.
read_key() {
    "$PYTHON" - "$ENV_FILE" "$1" <<'PY'
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
TELEGRAM_BOT_TOKEN=$(read_key TELEGRAM_BOT_TOKEN)
export OPENROUTER_API_KEY TELEGRAM_BOT_TOKEN

if [ -z "$OPENROUTER_API_KEY" ]; then
    printf '%s\n' "snajper instant: OPENROUTER_API_KEY missing in $ENV_FILE" >&2
    exit 2
fi
if [ "$TELEGRAM" = "1" ] && [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    printf '%s\n' "snajper instant: TELEGRAM_BOT_TOKEN missing in $ENV_FILE" >&2
    exit 2
fi

mkdir -p "$STATE_DIR/reports"
cd "$REPO"

STATUS=0
if [ "$TELEGRAM" = "1" ]; then
    # `--send` obowiązkowo: bez niego skrypt tylko wypisze wiadomość i cron nic nie dostarczy.
    "$PYTHON" scripts/instant_report.py --state-dir "$STATE_DIR" --send "$@" || STATUS=$?
else
    # `--mark-only`: bez Telegrama oferta i tak musi wejść do rejestru jako `seen`
    # (z draftem), a sygnał do Jana idzie osobno przez push_notify.py.
    "$PYTHON" scripts/instant_report.py --state-dir "$STATE_DIR" --mark-only "$@" || STATUS=$?
fi

# Push niezależnie od Telegrama: gdy w rejestrze są oferty do decyzji, leci powiadomienie do panelu.
if [ "$DRY_RUN" = "0" ]; then
    "$PYTHON" scripts/push_notify.py --new-leads --title "Snajper: nowe oferty" || true
fi

exit "$STATUS"
