#!/usr/bin/env bash
# Long-running poller launcher. Redirect its output to poller.log.
set -euo pipefail

CONFIG="${HOME}/.config/factory"
set -a
. "${CONFIG}/webhook.env"
. "${CONFIG}/orch-poll.env"
set +a
export PYTHONUNBUFFERED=1
exec "${HOME}/.local/bin/orch" poll "$@"
