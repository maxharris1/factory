#!/usr/bin/env bash
# The one poller launcher (#107): poller-service.sh and orch-poll.service both
# run this, so `orch poll` never starts without its environment. Exits 78
# naming a missing env file (never its contents), and 79 on a host retired as
# the orch host (~/.config/factory/orch-retired). Redirect its output to
# poller.log. --print-db prints the ORCH_DB orch poll would get, then exits.
set -euo pipefail

CONFIG="${HOME}/.config/factory"
RETIRED="${CONFIG}/orch-retired"
if [[ -e "$RETIRED" ]]; then
    echo "run-poller: this host is retired as the orch host (${RETIRED}: $(head -n 1 "$RETIRED" 2>/dev/null || true)); not starting orch poll" >&2
    exit 79
fi
for name in webhook.env orch-poll.env; do
    if [[ ! -r "${CONFIG}/${name}" ]]; then
        echo "run-poller: missing ~/.config/factory/${name}" >&2
        exit 78
    fi
done
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
