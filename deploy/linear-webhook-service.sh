#!/usr/bin/env bash
# Keep the Linear webhook listener (`orch linear-webhook serve`, README
# "Linear webhook") and its reverse SSH tunnel up on a host without a systemd
# user session. deploy/install.sh installs this script to
# ~/.local/share/factory/linear-webhook-service.sh.
#
#   linear-webhook-service.sh start              start both loops unless running;
#                                                clears a stop
#   linear-webhook-service.sh stop               stop every loop and hold them stopped
#   linear-webhook-service.sh restart            stop, then start
#   linear-webhook-service.sh status             exit 0 if every loop runs, 3 if not
#   linear-webhook-service.sh ensure             start what is not running, unless
#                                                held stopped (what autostart runs;
#                                                prints nothing when all run)
#   linear-webhook-service.sh install-autostart  write the autostart flag
#   linear-webhook-service.sh remove-autostart   remove it
#
# Two loops run, each a singleton per state directory (issue #134):
#   _listener-loop runs `orch linear-webhook serve --bind 127.0.0.1:PORT`
#   (with `--orch "$ORCH_CONFIG"` when ORCH_CONFIG is set);
#   _tunnel-loop runs `ssh -N -R 127.0.0.1:PORT:127.0.0.1:PORT HOST`.
# Each loop holds an exclusive flock(1) on its own lock file for its whole
# life (linear-webhook-listener.lock, linear-webhook-tunnel.lock) and exits 0
# at once when the lock is already held. Locks belong to the open file, not
# to a pid, so they hold across PID namespaces (sandboxed bot shells share
# the state directory but not pids). ensure, start and status decide from
# the locks; the pidfiles are for people only. flock(1) is required.
#
# The listener loop starts serve only while nothing listens on
# 127.0.0.1:PORT. Both loops back off after a run shorter than 30 seconds:
# LINEAR_WEBHOOK_RETRY_SEC (default 5) doubles up to
# LINEAR_WEBHOOK_RETRY_MAX_SEC (default 60), and resets after a longer run.
# A tunnel refused with "remote port forwarding failed" is logged as such.
#
# stop finds every loop for this state directory: each process whose argv is
# `.../linear-webhook-service.sh _listener-loop|_tunnel-loop` (any copy of the
# script) and whose state directory (its --state argument, else its
# ORCH_STATE_DIR or HOME) is this one, plus every process holding a loop
# lock (a `lock:` line in its /proc/PID/fdinfo for the lock file; merely
# having the file open does not count). It ends each with its children. If a
# lock is still held afterwards (a loop in a PID namespace this shell cannot
# see), stop says so and exits 1.
#
# Settings, from the environment:
#   LINEAR_WEBHOOK_PORT          listener and forwarded port (default 8787)
#   LINEAR_WEBHOOK_TUNNEL_HOST   ssh alias for the tunnel (default
#                                pc-wsl-cursor-bot; empty runs no tunnel)
#   LINEAR_WEBHOOK_RETRY_SEC, LINEAR_WEBHOOK_RETRY_MAX_SEC   backoff (above)
#   ORCH_STATE_DIR               state directory (default ~/.local/state/factory)
#   ORCH_DB                      default: the state directory's orch.db
#   ORCH_CONFIG                  orch.yml for serve (default: the checkout's)
#
# Files in the state directory: linear-webhook-{listener,tunnel}.{lock,pid,log},
# linear-webhook.stopped (the stop hold), linear-webhook-service.lock
# (serializes start/stop/ensure) and linear-webhook-service.log (autostart).
#
# Autostart: install-autostart writes ~/.config/factory/linear-webhook-autostart,
# which makes the profile written by deploy/install.sh run `ensure` in the
# background whenever a bot shell starts.
set -euo pipefail

COMMAND="${1:-}"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
if [[ "$COMMAND" == _*-loop && "${2:-}" == --state && -n "${3:-}" ]]; then
    STATE="$3"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
CONFIG="${HOME}/.config/factory"
AUTOSTART="${CONFIG}/linear-webhook-autostart"
STOPPED="${STATE}/linear-webhook.stopped"
SERVICE_LOCK="${STATE}/linear-webhook-service.lock"
ORCH="${ORCH_BIN:-${HOME}/.local/bin/orch}"
PORT="${LINEAR_WEBHOOK_PORT:-8787}"
TUNNEL_HOST="${LINEAR_WEBHOOK_TUNNEL_HOST-pc-wsl-cursor-bot}"
RETRY="${LINEAR_WEBHOOK_RETRY_SEC:-5}"
RETRY_MAX="${LINEAR_WEBHOOK_RETRY_MAX_SEC:-60}"
HEALTHY_RUN=30
START_WAIT="${LINEAR_WEBHOOK_START_WAIT:-15}"
STOP_WAIT="${LINEAR_WEBHOOK_STOP_WAIT:-30}"
PROC="${ORCH_PROC_DIR:-/proc}"
export PATH="${HOME}/.local/bin${PATH:+:${PATH}}:/usr/local/bin:/usr/bin:/bin"

say() { printf '%s linear-webhook-service: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

usage() {
    echo "usage: $(basename "$0") {start|stop|restart|status|ensure|install-autostart|remove-autostart}" >&2
    exit 2
}

lock_file() { printf '%s/linear-webhook-%s.lock' "$STATE" "$1"; }
pid_file() { printf '%s/linear-webhook-%s.pid' "$STATE" "$1"; }
log_file() { printf '%s/linear-webhook-%s.log' "$STATE" "$1"; }

modes() {
    echo listener
    [[ -z "$TUNNEL_HOST" ]] || echo tunnel
}

need_flock() {
    command -v flock >/dev/null 2>&1 && return 0
    say "flock(1) not found; the loops cannot run as singletons, so nothing was started" >&2
    return 1
}

check_settings() {
    local number='^[0-9]+([.][0-9]+)?$'
    if ! [[ "$PORT" =~ ^[1-9][0-9]{0,4}$ && "$RETRY" =~ $number && "$RETRY_MAX" =~ $number &&
        "$START_WAIT" =~ ^[0-9]+$ && "$STOP_WAIT" =~ ^[0-9]+$ ]]; then
        say "LINEAR_WEBHOOK_PORT, *_SEC and *_WAIT must be numbers" >&2
        exit 2
    fi
    if [[ -n "$TUNNEL_HOST" && ! "$TUNNEL_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ ]]; then
        say "LINEAR_WEBHOOK_TUNNEL_HOST must be an ssh alias" >&2
        exit 2
    fi
}

# Is the lock for loop $1 held by some process?
lock_held() {
    local file status=0
    file="$(lock_file "$1")"
    [[ -e "$file" ]] || return 1
    flock -n -E 75 "$file" true 8>&- 9>&- || status=$?
    ((status == 75))
}

service_lock() {
    mkdir -p "$STATE"
    exec 9>>"$SERVICE_LOCK"
    flock 9
}

# --- the loops ---------------------------------------------------------------

CHILD=""
STATUS=0
STOPPING=""

stopping() { [[ -n "$STOPPING" || -e "$STOPPED" ]]; }

# Take loop $1's lock for the life of this process (fd 8), or exit 0.
become_loop() {
    local mode="$1"
    mkdir -p "$STATE"
    exec 8>>"$(lock_file "$mode")"
    if ! flock -n 8; then
        say "_${mode}-loop already running (lock held); exiting"
        exit 0
    fi
    printf '%s\n' "$$" >"$(pid_file "$mode")"
    LOOP_PIDFILE="$(pid_file "$mode")"
    trap 'STOPPING=1; [[ -z "$CHILD" ]] || kill -TERM "$CHILD" 2>/dev/null || true' TERM INT HUP
    trap 'leave_loop' EXIT
}

leave_loop() {
    [[ -z "$CHILD" ]] || kill -TERM "$CHILD" 2>/dev/null || true
    if [[ "$(head -n 1 "$LOOP_PIDFILE" 2>/dev/null || true)" == "$$" ]]; then
        rm -f "$LOOP_PIDFILE"
    fi
}

# Run "$@" without the loop lock and wait for it; its exit status is STATUS.
run_child() {
    "$@" 8>&- &
    CHILD=$!
    STATUS=0
    wait "$CHILD" || STATUS=$?
    while kill -0 "$CHILD" 2>/dev/null; do
        STATUS=0
        wait "$CHILD" || STATUS=$?
    done
    CHILD=""
}

pause() {
    run_child sleep "$1"
}

doubled() {
    awk -v d="$1" -v m="$RETRY_MAX" 'BEGIN { d *= 2; if (d > m) d = m; print d }'
}

port_taken() {
    (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null
}

listener_loop() {
    local delay="$RETRY" started busy=""
    become_loop listener
    export ORCH_DB="${ORCH_DB:-${STATE}/orch.db}"
    export PYTHONUNBUFFERED=1
    while ! stopping; do
        if port_taken; then
            [[ -n "$busy" ]] || say "127.0.0.1:${PORT} already has a listener; not starting serve"
            busy=1
            pause "$delay"
            delay="$(doubled "$delay")"
            continue
        fi
        busy=""
        started="$SECONDS"
        say "starting orch linear-webhook serve on 127.0.0.1:${PORT}"
        run_child "$ORCH" linear-webhook serve --bind "127.0.0.1:${PORT}" \
            ${ORCH_CONFIG:+--orch "$ORCH_CONFIG"}
        stopping && break
        ((SECONDS - started < HEALTHY_RUN)) || delay="$RETRY"
        say "orch linear-webhook serve exited ${STATUS}; retry in ${delay}s"
        pause "$delay"
        delay="$(doubled "$delay")"
    done
}

tunnel_loop() {
    local delay="$RETRY" started errors
    [[ -n "$TUNNEL_HOST" ]] || {
        say "LINEAR_WEBHOOK_TUNNEL_HOST is empty; no tunnel" >&2
        exit 1
    }
    become_loop tunnel
    errors="${STATE}/linear-webhook-tunnel.stderr"
    while ! stopping; do
        started="$SECONDS"
        say "starting ssh -R 127.0.0.1:${PORT}:127.0.0.1:${PORT} ${TUNNEL_HOST}"
        run_child ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
            -R "127.0.0.1:${PORT}:127.0.0.1:${PORT}" "$TUNNEL_HOST" 2>"$errors"
        cat "$errors" >&2 2>/dev/null || true
        stopping && break
        ((SECONDS - started < HEALTHY_RUN)) || delay="$RETRY"
        if grep -q "remote port forwarding failed" "$errors" 2>/dev/null; then
            say "forward refused: port ${PORT} already forwarded on ${TUNNEL_HOST}; retry in ${delay}s"
        else
            say "ssh exited ${STATUS}; retry in ${delay}s"
        fi
        pause "$delay"
        delay="$(doubled "$delay")"
    done
}

# --- start, stop, status -------------------------------------------------------

# Start loop $1 unless its lock is held. With $2 = loud, say so when it is.
launch_one() {
    local mode="$1" loud="${2:-}" child i
    if lock_held "$mode"; then
        [[ -z "$loud" ]] || say "_${mode}-loop already running (lock held)"
        return 0
    fi
    nohup bash "$SELF" "_${mode}-loop" --state "$STATE" \
        >>"$(log_file "$mode")" 2>&1 </dev/null 8>&- 9>&- &
    child=$!
    for ((i = 0; i < START_WAIT * 10; i++)); do
        if lock_held "$mode"; then
            say "started _${mode}-loop (pid ${child})"
            return 0
        fi
        if ! kill -0 "$child" 2>/dev/null; then
            wait "$child" 2>/dev/null || true
            lock_held "$mode" && return 0
            say "_${mode}-loop exited at startup; see $(log_file "$mode")" >&2
            return 1
        fi
        sleep 0.1
    done
    say "_${mode}-loop did not take its lock within ${START_WAIT}s; see $(log_file "$mode")" >&2
    return 1
}

launch() {
    local mode status=0
    mkdir -p "$STATE"
    for mode in $(modes); do
        launch_one "$mode" "${1:-}" || status=1
    done
    return "$status"
}

start() {
    mkdir -p "$STATE"
    rm -f "$STOPPED"
    launch loud
}

ensure() {
    [[ -e "$STOPPED" ]] && return 0
    launch
}

# The state directory loop $1 serves, if pid $1 is a loop of this script.
loop_state_of() {
    local i kv home="" state=""
    local -a argv=() environ=()
    mapfile -d '' -t argv 2>/dev/null <"${PROC}/$1/cmdline" || return 1
    for i in 0 1; do
        ((${#argv[@]} > i + 1)) || continue
        [[ "${argv[i]}" =~ (^|/)linear-webhook-service\.sh$ ]] || continue
        [[ "${argv[i + 1]}" == _listener-loop || "${argv[i + 1]}" == _tunnel-loop ]] || continue
        if [[ "${argv[i + 2]:-}" == --state && -n "${argv[i + 3]:-}" ]]; then
            printf '%s\n' "${argv[i + 3]}"
            return 0
        fi
        mapfile -d '' -t environ 2>/dev/null <"${PROC}/$1/environ" || return 1
        for kv in "${environ[@]}"; do
            case "$kv" in
                ORCH_STATE_DIR=?*) state="${kv#*=}" ;;
                HOME=*) home="${kv#*=}" ;;
            esac
        done
        [[ -n "$state" || -n "$home" ]] || return 1
        printf '%s\n' "${state:-${home}/.local/state/factory}"
        return 0
    done
    return 1
}

loop_pids() {
    local entry pid
    for entry in "$PROC"/[0-9]*; do
        pid="${entry##*/}"
        [[ "$pid" != "$$" ]] || continue
        [[ "$(loop_state_of "$pid")" == "$STATE" ]] && printf '%s\n' "$pid"
    done
    return 0
}

lock_holders() {
    local mode file fd
    for mode in listener tunnel; do
        file="$(lock_file "$mode")"
        [[ -e "$file" ]] || continue
        file="$(readlink -f "$file")"
        while IFS= read -r fd; do
            grep -q '^lock:.*FLOCK' "${fd%/fd/*}/fdinfo/${fd##*/}" 2>/dev/null || continue
            fd="${fd%/fd/*}"
            printf '%s\n' "${fd##*/}"
        done < <(find "$PROC" -mindepth 3 -maxdepth 3 -path "${PROC}/[0-9]*/fd/*" -lname "$file" 2>/dev/null)
    done
    return 0
}

# Print the pids given and all their descendants.
with_descendants() {
    ps -A -o pid= -o ppid= 2>/dev/null | awk -v roots="$*" '
        BEGIN { n = split(roots, r, " "); for (i = 1; i <= n; i++) keep[r[i]] = 1 }
        { parent[$1] = $2 }
        END {
            for (grown = 1; grown;) {
                grown = 0
                for (p in parent) if (!(p in keep) && (parent[p] in keep)) { keep[p] = 1; grown = 1 }
            }
            for (p in keep) print p
        }'
}

alive() {
    local stat
    stat="$(cat "${PROC}/$1/stat" 2>/dev/null)" || return 1
    stat="${stat##*) }"
    [[ "${stat%% *}" != Z ]]
}

wait_gone() {
    local deadline=$((SECONDS + $1)) pid left
    while :; do
        left=""
        for pid in "${@:2}"; do
            alive "$pid" && left=1 && break
        done
        [[ -n "$left" ]] || return 0
        ((SECONDS < deadline)) || return 1
        sleep 0.1
    done
}

stop() {
    local mode status=0 pid
    local -a roots=() pids=() left=()
    mkdir -p "$STATE"
    : >"$STOPPED"
    mapfile -t roots < <({ loop_pids; lock_holders; } | grep -vx "$$" | sort -un)
    if ((${#roots[@]} == 0)); then
        say "no linear webhook loop is running"
    else
        mapfile -t pids < <(with_descendants "${roots[@]}" | grep -vx "$$")
        kill -TERM "${pids[@]}" 2>/dev/null || true
        if ! wait_gone "$STOP_WAIT" "${pids[@]}"; then
            for pid in "${pids[@]}"; do
                alive "$pid" && left+=("$pid")
            done
            say "killing ${left[*]}: still running ${STOP_WAIT}s after SIGTERM"
            kill -KILL "${left[@]}" 2>/dev/null || true
            wait_gone 5 "${left[@]}" || true
        fi
        say "stopped linear webhook loop(s) ${roots[*]}"
    fi
    for mode in listener tunnel; do
        if lock_held "$mode"; then
            say "$(lock_file "$mode") is still held by a process this shell cannot see (another PID namespace?); run stop from the host" >&2
            status=1
        fi
    done
    return "$status"
}

status() {
    local mode pid status=0
    for mode in $(modes); do
        pid="$(head -n 1 "$(pid_file "$mode")" 2>/dev/null || true)"
        if lock_held "$mode"; then
            echo "${mode}: running (lock held; pidfile names ${pid:-nothing})"
        else
            echo "${mode}: not running"
            status=3
        fi
    done
    [[ ! -e "$STOPPED" ]] || echo "held stopped by ${STOPPED}; run start to clear"
    return "$status"
}

case "$COMMAND" in
    _listener-loop) check_settings && need_flock && listener_loop ;;
    _tunnel-loop) check_settings && need_flock && tunnel_loop ;;
    start) check_settings && need_flock && service_lock && start ;;
    stop) need_flock && service_lock && stop ;;
    restart) check_settings && need_flock && service_lock && stop && start ;;
    status) need_flock && status ;;
    ensure) check_settings && need_flock && service_lock && ensure ;;
    install-autostart)
        mkdir -p "$CONFIG"
        : >"$AUTOSTART"
        echo "linear webhook autostart on: bot shells run '${SELF} ensure' (${AUTOSTART})"
        ;;
    remove-autostart)
        rm -f "$AUTOSTART"
        echo "linear webhook autostart off"
        ;;
    *) usage ;;
esac
