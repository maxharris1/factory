#!/usr/bin/env bash
# The one poller launcher (#107): poller-service.sh and orch-poll.service both
# run this, so `orch poll` never starts without its environment. Exits 78
# naming a missing env file (never its contents). Redirect its output to
# poller.log.
set -euo pipefail

CONFIG="${HOME}/.config/factory"
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
export PYTHONUNBUFFERED=1
exec "${HOME}/.local/bin/orch" poll "$@"
