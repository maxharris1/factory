#!/usr/bin/env bash
set -euo pipefail

# Weekly Monday eval report runner: reads the read-only backup (or live DB)
# for the preceding 7 UTC days and writes both plain text and JSON reports
# under ${ORCH_STATE_DIR:-~/.local/state/factory}/evals/. Each report is
# renamed into place whole, latest.* is swapped atomically, only the newest
# 12 weeks are kept, and the dashboard push then publishes them as evals.json.
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
TMP="${OUT_DIR}/.weekly-${STAMP}"
trap 'rm -f -- "${TMP}.txt.tmp" "${TMP}.json.tmp" "${OUT_DIR}/.latest.txt.tmp" "${OUT_DIR}/.latest.json.tmp"' EXIT

"$ORCH" eval-report --db "$SOURCE" --since "$SINCE" --until "$UNTIL" >"${TMP}.txt.tmp"
"$ORCH" eval-report --db "$SOURCE" --since "$SINCE" --until "$UNTIL" --json >"${TMP}.json.tmp"
for ext in txt json; do
    mv -f -- "${TMP}.${ext}.tmp" "${OUT_DIR}/weekly-${STAMP}.${ext}"
    ln -sfn "weekly-${STAMP}.${ext}" "${OUT_DIR}/.latest.${ext}.tmp"
    mv -Tf -- "${OUT_DIR}/.latest.${ext}.tmp" "${OUT_DIR}/latest.${ext}"
    find "$OUT_DIR" -maxdepth 1 -type f -name "weekly-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].${ext}" \
        | sort -r | tail -n +13 | xargs -r -d '\n' rm -f --
done

# The systemd unit sets no ORCH_DB, so the database is named. A failed or
# unconfigured push must not fail the week's report.
"$ORCH" dashboard push --once --db "$DB" --evals-dir "$OUT_DIR" || true
