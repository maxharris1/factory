#!/usr/bin/env bash
# Run the Linear webhook listener (`orch linear-webhook serve`, README
# "Linear webhook") under a restart supervisor, with its optional reverse SSH
# tunnel, on a host without a systemd user session (issue #107). Follows
# deploy/waiter-service.sh: a small supervisor owns each child and starts it
# again whenever it exits or is killed.
#
#   linear-webhook-service.sh start             start unless running; clears a stop
#   linear-webhook-service.sh stop              stop the supervisor and its children,
#                                               and hold them stopped
#   linear-webhook-service.sh restart           stop, then start
#   linear-webhook-service.sh status            exit 0 if running, 3 if not
#   linear-webhook-service.sh ensure            start unless running or held stopped
#                                               (what autostart runs; never clears a
#                                               stop, prints nothing when running)
#   linear-webhook-service.sh install-autostart enable autostart (see below)
#   linear-webhook-service.sh remove-autostart  disable it
#
# deploy/install.sh wires the profile hook; deploy/self-heal.sh restarts a
# down or stale-code listener or tunnel with `linear-webhook-service.sh restart`.
#
# Files, in ~/.local/state/factory (ORCH_STATE_DIR): linear-webhook.pid (the
# supervisor), linear-webhook.stopped (the stop hold),
# linear-webhook-service.lock, linear-webhook.log (the children's output and
# the supervisor's lines) and linear-webhook-service.log (autostart's output).
#
# Tunnel: when ~/.config/factory/linear-webhook.env sets both
# LINEAR_TUNNEL_SSH_ALIAS and LINEAR_TUNNEL_REMOTE_PORT, the supervisor also
# runs `ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
# -R <remote port>:<bind> <alias>`, where <bind> is LINEAR_TUNNEL_BIND
# (default 127.0.0.1:8787, linear_webhook.bind's usual value). Without them
# only the listener runs. No value from that file is ever printed.
#
# Each child that exits is started again after ORCH_LINEAR_RESTART_SEC
# (default 5) seconds; when it keeps exiting within 30 seconds the pause
# doubles, up to 60 seconds. ORCH_DB defaults to the shared state database,
# ~/.local/state/factory/orch.db.
#
# Autostart: install-autostart writes ~/.config/factory/linear-webhook-autostart,
# which makes the profile written by deploy/install.sh run `ensure` whenever
# a bot shell starts. When `crontab` exists it also installs an @reboot line
# and a 5-minute `ensure` watchdog, tagged "# factory-linear-webhook" and
# replaced, never duplicated, on every run. ORCH_CRONTAB names the crontab
# command (default: crontab on PATH). If the current crontab cannot be read
# (anything but "no crontab for <user>"), install-autostart and
# remove-autostart change nothing and exit 1.
set -euo pipefail

COMMAND="${1:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
PIDFILE="${STATE}/linear-webhook.pid"
LOG="${STATE}/linear-webhook.log"
SERVICE_LOG="${STATE}/linear-webhook-service.log"
SERVICE_LOCK="${STATE}/linear-webhook-service.lock"
STOPPED="${STATE}/linear-webhook.stopped"
AUTOSTART="${CONFIG}/linear-webhook-autostart"
ENV_FILE="${CONFIG}/linear-webhook.env"
ORCH="${ORCH_BIN:-${HOME}/.local/bin/orch}"
MARKER="# factory-linear-webhook"
RESTART_SEC="${ORCH_LINEAR_RESTART_SEC:-5}"
RESTART_MAX=60
START_WAIT="${ORCH_LINEAR_START_WAIT:-15}"
STOP_WAIT="${ORCH_LINEAR_STOP_WAIT:-60}"
CRONTAB="${ORCH_CRONTAB:-crontab}"
PROC="${ORCH_PROC_DIR:-/proc}"
export PATH="${HOME}/.local/bin${PATH:+:${PATH}}:/usr/local/bin:/usr/bin:/bin"

say() { printf '%s linear-webhook-service: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

usage() {
    echo "usage: $(basename "$0") {start|stop|restart|status|ensure|install-autostart|remove-autostart}" >&2
    exit 2
}

# Is this argv the supervisor (`bash .../linear-webhook-service.sh _supervise`)?
is_supervisor_argv() {
    local i
    for i in 0 1; do
        if (($# > i + 1)) && [[ "${@:i+1:1}" =~ (^|/)linear-webhook-service\.sh$ &&
            "${@:i+2:1}" == _supervise ]]; then
            return 0
        fi
    done
    return 1
}

# Print the pid of the running supervisor, or fail.
running_pid() {
    local pid command
    local -a argv=()
    [[ -r "$PIDFILE" ]] || return 1
    pid="$(head -n 1 "$PIDFILE" 2>/dev/null || true)"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -d "$PROC" ]]; then
        [[ -r "${PROC}/${pid}/cmdline" ]] || return 1
        mapfile -d '' -t argv <"${PROC}/${pid}/cmdline" || return 1
        is_supervisor_argv "${argv[@]}" || return 1
    else
        command="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
        read -r -a argv <<<"$command"
        is_supervisor_argv "${argv[@]}" || return 1
    fi
    printf '%s\n' "$pid"
}

service_lock() {
    command -v flock >/dev/null 2>&1 || return 0
    mkdir -p "$STATE"
    exec 9>>"$SERVICE_LOCK"
    flock 9
}

# Run one child (`$2...`, named `$1`) until SIGTERM or the stop hold,
# starting it again with backoff whenever it exits.
child_loop() {
    local name="$1"
    shift
    local child="" status started delay="$RESTART_SEC" stopping=""
    trap 'stopping=1; [[ -n "$child" ]] && kill -TERM "$child" 2>/dev/null || true' TERM INT HUP
    while [[ -z "$stopping" && ! -e "$STOPPED" ]]; do
        started="$SECONDS"
        say "starting ${name}"
        "$@" 9>&- &
        child=$!
        status=0
        wait "$child" || status=$?
        while kill -0 "$child" 2>/dev/null; do
            status=0
            wait "$child" || status=$?
        done
        child=""
        [[ -z "$stopping" && ! -e "$STOPPED" ]] || break
        if ((SECONDS - started >= 30)); then
            delay="$RESTART_SEC"
        fi
        say "${name} exited ${status}; restarting in ${delay}s"
        sleep "$delay" &
        wait $! || true
        delay="$(awk -v d="$delay" -v m="$RESTART_MAX" 'BEGIN { d *= 2; if (d > m) d = m; print d }')"
    done
}

# The tunnel's ssh argv, or fail when linear-webhook.env does not set it.
tunnel_wanted() {
    [[ -n "${LINEAR_TUNNEL_SSH_ALIAS:-}" && -n "${LINEAR_TUNNEL_REMOTE_PORT:-}" ]] || return 1
    if ! [[ "$LINEAR_TUNNEL_SSH_ALIAS" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ &&
        "$LINEAR_TUNNEL_REMOTE_PORT" =~ ^[1-9][0-9]{0,4}$ &&
        "${LINEAR_TUNNEL_BIND:-127.0.0.1:8787}" =~ ^[A-Za-z0-9._-]+:[1-9][0-9]{0,4}$ ]]; then
        say "linear-webhook.env: LINEAR_TUNNEL_* is not an ssh alias, port and host:port; tunnel not started"
        return 1
    fi
}

# The supervisor. It owns the pidfile; SIGTERM stops its children and it.
supervise() {
    local stopping="" pid
    local -a loops=()
    trap 'stopping=1; ((${#loops[@]})) && kill -TERM "${loops[@]}" 2>/dev/null || true' TERM INT HUP
    mkdir -p "$STATE"
    printf '%s\n' "$$" >"$PIDFILE"
    if [[ -r "$ENV_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
    fi
    export ORCH_DB="${ORCH_DB:-${STATE}/orch.db}"
    export PYTHONUNBUFFERED=1
    child_loop "orch linear-webhook serve" "$ORCH" linear-webhook serve &
    loops+=("$!")
    if tunnel_wanted; then
        child_loop "reverse tunnel" ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
            -R "${LINEAR_TUNNEL_REMOTE_PORT}:${LINEAR_TUNNEL_BIND:-127.0.0.1:8787}" \
            "$LINEAR_TUNNEL_SSH_ALIAS" &
        loops+=("$!")
    fi
    [[ -z "$stopping" ]] || kill -TERM "${loops[@]}" 2>/dev/null || true
    for pid in "${loops[@]}"; do
        while kill -0 "$pid" 2>/dev/null; do
            wait "$pid" || true
        done
    done
    say "supervisor stopped"
    if [[ "$(head -n 1 "$PIDFILE" 2>/dev/null || true)" == "$$" ]]; then
        rm -f "$PIDFILE"
    fi
}

start() {
    mkdir -p "$STATE"
    rm -f "$STOPPED"
    launch
}

launch() {
    local pid child i
    mkdir -p "$STATE"
    if pid="$(running_pid)"; then
        say "linear webhook supervisor already running (pid ${pid})"
        return 0
    fi
    nohup bash "$SELF" _supervise >>"$LOG" 2>&1 </dev/null 9>&- &
    child=$!
    for ((i = 0; i < START_WAIT * 10; i++)); do
        if pid="$(running_pid)"; then
            say "started linear webhook supervisor (pid ${pid})"
            return 0
        fi
        if ! kill -0 "$child" 2>/dev/null; then
            say "linear webhook supervisor exited at startup; see ${LOG}" >&2
            return 1
        fi
        sleep 0.1
    done
    say "linear webhook supervisor did not write ${PIDFILE} within ${START_WAIT}s; see ${LOG}" >&2
    return 1
}

stop() {
    local pid i
    mkdir -p "$STATE"
    : >"$STOPPED"
    if ! pid="$(running_pid)"; then
        say "linear webhook supervisor is not running"
        return 0
    fi
    kill -TERM "$pid"
    for ((i = 0; i < STOP_WAIT * 10; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            say "stopped linear webhook supervisor (pid ${pid})"
            return 0
        fi
        sleep 0.1
    done
    say "linear webhook supervisor (pid ${pid}) did not stop within ${STOP_WAIT}s" >&2
    return 1
}

status() {
    local pid
    if pid="$(running_pid)"; then
        echo "running (pid ${pid})"
        return 0
    fi
    if [[ -e "$STOPPED" ]]; then
        echo "stopped (held by ${STOPPED}; run start to clear)"
    else
        echo "not running"
    fi
    return 3
}

ensure() {
    [[ -e "$STOPPED" ]] && return 0
    running_pid >/dev/null && return 0
    [[ -e "$STOPPED" ]] && return 0
    launch
}

plain_path() {
    [[ "$1" =~ ^/[A-Za-z0-9/._-]+$ ]] || {
        echo "linear-webhook-service: refusing to write ${1} into a crontab" >&2
        return 1
    }
}

crontab_without_marker() {
    local current errors status=0
    errors="$(mktemp "${TMPDIR:-/tmp}/linear-webhook-service.XXXXXX")" || return 1
    current="$("$CRONTAB" -l 2>"$errors")" || status=$?
    if ((status != 0)); then
        if grep -q "no crontab for" "$errors"; then
            current=""
        else
            echo "linear-webhook-service: cannot read the crontab (${CRONTAB} -l exited ${status}); changed nothing:" >&2
            cat "$errors" >&2
            rm -f "$errors"
            return 1
        fi
    fi
    rm -f "$errors"
    [[ -n "$current" ]] || return 0
    grep -vF -- "$MARKER" <<<"$current" || true
}

have_crontab() {
    command -v "$CRONTAB" >/dev/null 2>&1
}

install_autostart() {
    local kept="" job="${SELF} ensure >>${SERVICE_LOG} 2>&1 ${MARKER}"
    if have_crontab; then
        plain_path "$SELF"
        plain_path "$SERVICE_LOG"
        kept="$(crontab_without_marker)" || exit 1
    fi
    mkdir -p "$CONFIG" "$STATE"
    : >"$AUTOSTART"
    echo "linear webhook autostart on: bot shells run '${SELF} ensure' (${AUTOSTART})"
    if ! have_crontab; then
        echo "crontab not found; autostart uses the bot-shell profile hook only"
        return 0
    fi
    {
        [[ -z "$kept" ]] || printf '%s\n' "$kept"
        printf '@reboot %s\n' "$job"
        printf '*/5 * * * * %s\n' "$job"
    } | "$CRONTAB" -
    echo "installed crontab @reboot and 5-minute ensure lines"
}

remove_autostart() {
    local kept=""
    if have_crontab; then
        kept="$(crontab_without_marker)" || exit 1
    fi
    rm -f "$AUTOSTART"
    echo "linear webhook autostart off"
    if have_crontab; then
        if [[ -n "$kept" ]]; then
            printf '%s\n' "$kept" | "$CRONTAB" -
        else
            "$CRONTAB" - </dev/null
        fi
        echo "removed crontab lines tagged ${MARKER}"
    fi
}

case "$COMMAND" in
    _supervise) supervise ;;
    start) service_lock && start ;;
    stop) service_lock && stop ;;
    restart) service_lock && stop && start ;;
    status) status ;;
    ensure) service_lock && ensure ;;
    install-autostart) install_autostart ;;
    remove-autostart) remove_autostart ;;
    *) usage ;;
esac
