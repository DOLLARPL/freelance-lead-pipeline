#!/bin/sh
# Snajper FB — wrapper transportu (etap 2b planu FACEBOOK-GROUPS.md).
#
# Uruchamia jeden run Apify na grupach z enabled: true i zapisuje surowe itemy do
# cache/snajper-v2/facebook/posts-<data>.json, czyli w formacie, który czyta
# adapter freelance_pipeline.sources.facebook. Adapter nigdy nie woła API sam.
#
# Token: APIFY_API_TOKEN z pliku środowiska (domyślnie /opt/data/.env) — czyta go
# skrypt Pythona, nigdy nie przechodzi przez argv ani przez stdout.
#
# Stdout jest pusty, gdy wszystko gra (cron no_agent nic nie wyśle), a przy awarii
# mówi jednym zdaniem, co się stało (brak danych / zero postów z tekstem).
#
# Kody wyjścia: 0 = plik zapisany z danymi, 2 = brak danych z transportu,
# 3 = run doszedł, ale zero postów z tekstem, 4 = za mało kredytu Apify (żadnego
# runu nie startujemy; komunikat na stdout dostarczy cron, adapter zgłosi brak danych). Niezerowy kod to nie awaria crona,
# a widoczny komunikat — nie potwierdzamy tu sukcesu, którego nie było.
#
# Uruchomienie ręczne: /opt/data/projects/snajper-ofert/scripts/fb_fetch.sh --dry-run
set -eu

REPO=${SNAJPER_REPO:-/opt/data/projects/snajper-ofert}
ENV_FILE=${SNAJPER_ENV_FILE:-/opt/data/.env}
POSTS_DIR=${SNAJPER_FB_POSTS_DIR:-/opt/data/cache/snajper-v2/facebook}
GROUPS_CONFIG=${SNAJPER_FB_GROUPS_CONFIG:-$REPO/config/facebook_groups.yaml}

if [ ! -r "$ENV_FILE" ]; then
    printf '%s\n' "snajper-fb: nie mogę przeczytać pliku środowiska: $ENV_FILE" >&2
    exit 2
fi

cd "$REPO"
# Okno świeżości: od 14.11.2026 przebieg jest raz dziennie o 9:00, więc pierwsze
# uruchomienie dnia musi objąć całą dobę od poprzedniego przebiegu (26 h z zapasem).
# Gdy plik dzisiejszej daty już istnieje (ręczny przebieg), wystarczy 1 h.
# Nadpisanie: SNAJPER_FB_NEWER_THAN.
TODAY=$(date -u +%Y%m%d)
if [ -f "$POSTS_DIR/posts-$TODAY.json" ]; then
    NEWER=${SNAJPER_FB_NEWER_THAN:-1 hour}
else
    NEWER=${SNAJPER_FB_NEWER_THAN_FIRST:-26 hours}
fi

exec uv run python scripts/fb_fetch.py \
    --env-file "$ENV_FILE" \
    --posts-dir "$POSTS_DIR" \
    --groups-config "$GROUPS_CONFIG" \
    --newer-than "$NEWER" \
    --quiet-empty \
    "$@"
