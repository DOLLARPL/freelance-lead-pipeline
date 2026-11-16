#!/bin/sh
set -eu

REPO=${SNAJPER_REPO:-/opt/data/projects/snajper-ofert}
STATE_DIR=${SNAJPER_STATE_DIR:-/opt/data/cache/snajper-v2}
ENV_FILE=${SNAJPER_ENV_FILE:-/opt/data/.env}
MODEL=google/gemini-2.5-flash-lite
SOURCES=useme,facebook_groups

if [ ! -r "$ENV_FILE" ]; then
    printf '%s\n' "snajper: cannot read environment file: $ENV_FILE" >&2
    exit 2
fi

OPENROUTER_API_KEY=$(python3 - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path

for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    if name.strip() == "OPENROUTER_API_KEY":
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[0] in "\"'":
            value = value[1:-1]
        print(value, end="")
        break
PY
)
export OPENROUTER_API_KEY
if [ -z "$OPENROUTER_API_KEY" ]; then
    printf '%s\n' "snajper: OPENROUTER_API_KEY is missing or empty in $ENV_FILE" >&2
    exit 2
fi

mkdir -p "$STATE_DIR/logs" "$STATE_DIR/reports"
cd "$REPO"
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)-$$
REPORT="$STATE_DIR/reports/report-$RUN_ID.txt"
TOP="$STATE_DIR/reports/top-$RUN_ID.txt"
MOZE="$STATE_DIR/reports/moze-$RUN_ID.txt"
AUDIT="$STATE_DIR/reports/rejected-$RUN_ID.txt"
ZAMIEN="$STATE_DIR/reports/zamien-$RUN_ID.txt"
POC="$STATE_DIR/poc-backlog.json"
LEADS="$STATE_DIR/leads-$RUN_ID.json"

set +e
uv run python -m freelance_pipeline.cli run \
    --state "$STATE_DIR/state.json" \
    --report "$REPORT" \
    --top-report "$TOP" \
    --moze-report "$MOZE" \
    --rejected-audit "$AUDIT" \
    --zamien-report "$ZAMIEN" \
    --poc-backlog "$POC" \
    --leads-json "$LEADS" \
    --sources "$SOURCES" \
    --model "$MODEL" \
    --collect-timeout 60 \
    --verbose
status=$?
set -e

# stdout carries every new offer from the TOP and MOŻE buckets, in full and
# uncapped (Jan's decision of 2026-09-22 — the 15-row and 5-rows-per-source
# ceilings had been hiding low-priority-but-real offers). No TOP and no MOŻE ⇒
# empty stdout ⇒ no Telegram message in the no_agent cron mode. "$TOP" keeps the
# TOP-only slice of the run, "$MOZE" keeps every MOŻE row, "$ZAMIEN" keeps the
# offers where we propose automation instead of the hire the client asked about;
# the separate cron job "Snajper ZAMIEN (osobna wiadomość)" sends that file as
# its own Telegram message.
if [ -s "$REPORT" ]; then
    cat "$REPORT"
fi
exit "$status"
