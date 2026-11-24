#!/usr/bin/env bash
set -euo pipefail

# Weekly Monday eval report runner: reads the read-only backup (or live DB)
# for the preceding 7 UTC days and writes both plain text and JSON reports
# under ~/.local/state/factory/evals/.
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
OUT_DIR="${STATE}/evals"
BACKUP="${ORCH_BACKUP_DB:-${HOME}/.local/share/factory/backup/orch.db}"
DB="${ORCH_DB:-${STATE}/orch.db}"
SOURCE="$BACKUP"
if [[ ! -f "$SOURCE" ]]; then
    SOURCE="$DB"
fi
ORCH="${HOME}/.local/bin/orch"
if [[ ! -x "$ORCH" ]]; then
    ORCH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/bin/orch"
fi

mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y-%m-%d)"
UNTIL="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SINCE="$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)"

"$ORCH" eval-report --db "$SOURCE" --since "$SINCE" --until "$UNTIL" >"${OUT_DIR}/weekly-${STAMP}.txt"
"$ORCH" eval-report --db "$SOURCE" --since "$SINCE" --until "$UNTIL" --json >"${OUT_DIR}/weekly-${STAMP}.json"
ln -sfn "weekly-${STAMP}.txt" "${OUT_DIR}/latest.txt"
ln -sfn "weekly-${STAMP}.json" "${OUT_DIR}/latest.json"
