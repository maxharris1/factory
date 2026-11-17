#!/usr/bin/env bash
# Run `poller-service.sh ensure` (beside this script) every
# ORCH_POLL_ENSURE_SEC seconds (default 60), for hosts where neither cron nor
# a new bot shell comes often enough to restart a dead poller. Started by
# hand, e.g. `nohup deploy/poller-ensure-loop.sh >>poller-ensure-loop.log 2>&1 &`.
#
# One loop per state directory (issue #134): the loop holds an exclusive
# flock(1) on poller-ensure-loop.lock for its whole life and exits 0 at once
# when another loop holds it, so starting it again (from any PID namespace)
# is harmless. Started by `poller-service.sh ensure` (ORCH_POLL_AUTOSTART_LOOP=1),
# it exits once ~/.config/factory/poller-autostart is removed. It also exits
# when ensure exits 79 (a host retired as the orch host). The pidfile,
# poller-ensure-loop.pid, is written only after the lock is taken and is for
# people only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
LOCK="${STATE}/poller-ensure-loop.lock"
PIDFILE="${STATE}/poller-ensure-loop.pid"
INTERVAL="${ORCH_POLL_ENSURE_SEC:-60}"

say() { printf '%s poller-ensure-loop: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if ! command -v flock >/dev/null 2>&1; then
    say "flock(1) not found; not starting" >&2
    exit 1
fi
mkdir -p "$STATE"
exec 8>>"$LOCK"
if ! flock -n 8; then
    say "already running (lock held); exiting"
    exit 0
fi
printf '%s\n' "$$" >"$PIDFILE"

SLEEPER=""
leave() {
    [[ -z "$SLEEPER" ]] || kill "$SLEEPER" 2>/dev/null || true
    if [[ "$(head -n 1 "$PIDFILE" 2>/dev/null || true)" == "$$" ]]; then
        rm -f "$PIDFILE"
    fi
}
trap leave EXIT
trap 'exit 143' TERM INT HUP

while :; do
    status=0
    "${HERE}/poller-service.sh" ensure 8>&- || status=$?
    ((status != 76 && status != 79)) || exit 0
    ((status == 0)) || say "poller-service.sh ensure exited ${status}"
    sleep "$INTERVAL" 8>&- &
    SLEEPER=$!
    wait "$SLEEPER" || true
    SLEEPER=""
done
