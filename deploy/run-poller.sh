#!/usr/bin/env bash
# Long-running poller launcher. Redirect its output to poller.log.
# --print-db prints the ORCH_DB orch poll would get, then exits.
set -euo pipefail

CONFIG="${HOME}/.config/factory"
set -a
. "${CONFIG}/webhook.env"
. "${CONFIG}/orch-poll.env"
set +a
if [[ "${1:-}" == --print-db ]]; then
    printf '%s\n' "${ORCH_DB:-}"
    exit 0
fi
export PYTHONUNBUFFERED=1
exec "${HOME}/.local/bin/orch" poll "$@"
