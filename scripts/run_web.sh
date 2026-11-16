#!/usr/bin/env bash
# Panel webowy snajpera. Startuje uvicorna na loopbacku; dostep z telefonu i laptopa
# idzie przez `tailscale serve`, ktory dokłada naglowek tozsamosci.
#
# Recznie:   scripts/run_web.sh
# Usluga:    systemctl status snajper-web
set -euo pipefail

PROJECT=/opt/data/projects/snajper-ofert
CACHE=/opt/data/cache/snajper-v2

cd "$PROJECT"

export SNAJPER_CACHE_DIR="$CACHE"
export SNAJPER_REPORTS_DIR="$CACHE/reports"
export SNAJPER_LEDGER="$CACHE/ledger.json"
export SNAJPER_WEB_DB=/opt/data/cache/snajper-web/snajper.db
export SNAJPER_CENNIK="$PROJECT/config/cennik.yaml"
export SNAJPER_HOST=127.0.0.1
export SNAJPER_PORT="${SNAJPER_PORT:-8787}"
# Okno swiezosci kolejki w panelu: oferty starsze niz tyle godzin nie pokazuja sie
# w widoku "Do decyzji" (select w filtrach pozwala to obejsc). 0 = bez filtra.
export SNAJPER_MAX_AGE_HOURS="${SNAJPER_MAX_AGE_HOURS:-12}"
# Panel stoi za Caddy pod sniper.jnxautomation.pl. Naglowek Tailscale jest
# honorowany tylko z loopbacku i tylko gdy Host nie jest tym publicznym adresem.
export SNAJPER_OWNER_LOGIN="${SNAJPER_OWNER_LOGIN-kontakt@jnxautomation.pl}"
export SNAJPER_PUBLIC_HOST="${SNAJPER_PUBLIC_HOST:-snajper.jnxautomation.pl}"

# Klucz modelu tylko po to, zeby przycisk "Popraw" w karcie oferty dzialal.
# Czytany z pliku srodowiska Hermesa, nie wklejany do repo.
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -r /opt/data/.env ]; then
  OPENROUTER_API_KEY="$(grep -m1 '^OPENROUTER_API_KEY=' /opt/data/.env | cut -d= -f2- || true)"
  export OPENROUTER_API_KEY
fi

mkdir -p /opt/data/cache/snajper-web

exec .venv/bin/python -m uvicorn freelance_pipeline.web.app:app \
  --host "$SNAJPER_HOST" --port "$SNAJPER_PORT" --workers 1 --no-server-header
