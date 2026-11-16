#!/bin/sh
# Read-only smoke test of the Oferteo adapter: one live pass over the PUBLIC
# category lists with the production scorer, on a throwaway state file.
#
# It never touches the production state, never sends Telegram, never logs in and
# never buys points: the adapter only parses public listings and marks every lead
# with contact_requires_points=True.
#
# Usage: scripts/smoke_oferteo.sh [model]        (default: google/gemini-2.5-flash-lite)
set -eu

REPO=${SNAJPER_REPO:-/opt/data/projects/snajper-ofert}
ENV_FILE=${SNAJPER_ENV_FILE:-/opt/data/.env}
MODEL=${1:-google/gemini-2.5-flash-lite}
OUT_DIR=${OFERTEO_SMOKE_DIR:-/tmp/oferteo-smoke}

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
    printf '%s\n' "smoke_oferteo: OPENROUTER_API_KEY missing in $ENV_FILE" >&2
    exit 2
fi

mkdir -p "$OUT_DIR"
cd "$REPO"
uv run python -m freelance_pipeline.cli run \
    --state "$OUT_DIR/state.json" \
    --report "$OUT_DIR/report.txt" \
    --top-report "$OUT_DIR/top.txt" \
    --moze-report "$OUT_DIR/moze.txt" \
    --rejected-audit "$OUT_DIR/rejected.txt" \
    --sources oferteo \
    --model "$MODEL" \
    --collect-timeout 60 \
    --verbose

printf '%s\n' "--- raport ($OUT_DIR/report.txt) ---"
cat "$OUT_DIR/report.txt"
printf '%s\n' "--- ODRZUĆ: $(wc -l < "$OUT_DIR/rejected.txt") wierszy ---"
