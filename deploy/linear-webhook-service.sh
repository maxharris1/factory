#!/usr/bin/env bash
# Keep the Linear webhook listener (`orch linear-webhook serve`, README
# "Linear webhook") and its reverse SSH tunnel up on a host without a systemd
# user session. deploy/install.sh installs this script to
# ~/.local/share/factory/linear-webhook-service.sh.
#
#   linear-webhook-service.sh start              start both loops unless running;
#                                                clears a stop and a held port
#   linear-webhook-service.sh stop               stop every loop and hold them stopped
#   linear-webhook-service.sh restart            stop, then start
#   linear-webhook-service.sh status             exit 0 if every loop runs, 3 if not
#   linear-webhook-service.sh ensure             start what is not running, unless
#                                                held stopped (what autostart runs;
#                                                prints nothing when all run)
#   linear-webhook-service.sh install-autostart  write the autostart flag (and
#                                                cron lines, see below)
#   linear-webhook-service.sh remove-autostart   remove them
#
# deploy/self-heal.sh restarts a down or stale-code listener or tunnel with
# `linear-webhook-service.sh restart` (issue #107). A restart whose stop
# fails drops the hold that stop just wrote, so ensure and self-heal still
# see a service to recover; a hold that was already there (an operator's
# stop) stays.
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
# Each serve or ssh child also holds a lock of its own
# (linear-webhook-listener-child.lock, linear-webhook-tunnel-child.lock),
# taken by its loop just before starting it. A child whose loop was killed
# keeps holding it, so stop (and restart, which self-heal runs) still finds
# and ends it, and a new loop starts no second child while it lives.
# Real OpenSSH closes inherited descriptors, so the lock alone cannot prove
# an ssh gone (issue #244). Each loop therefore also records its child as
# `PID STARTTIME PIDNS` in linear-webhook-{listener,tunnel}-child.pid
# (STARTTIME: field 22 of /proc/PID/stat). A recorded pid counts as the
# child only while it runs with that start time and its argv is still the
# child's (`-R 127.0.0.1:N:127.0.0.1:N`, or `linear-webhook serve`), so a
# reused pid is never signalled. Before starting a child, a loop ends the
# one recorded by an earlier loop (SIGTERM, SIGKILL after
# LINEAR_WEBHOOK_STOP_WAIT) and starts nothing if it survives. A record
# from another PID namespace cannot be checked, so a loop keeps it and
# starts nothing until the record is removed. The loops, start, stop, restart
# and ensure exit 4 outside the host PID namespace (deploy/host-pidns.sh).
#
# The listener loop starts serve only while nothing listens on
# 127.0.0.1:PORT. Both loops back off after a run shorter than 30 seconds:
# LINEAR_WEBHOOK_RETRY_SEC (default 5) doubles up to
# LINEAR_WEBHOOK_RETRY_MAX_SEC (default 60), and resets after a longer run.
# A tunnel refused with "remote port forwarding failed" is logged as such.
# After LINEAR_WEBHOOK_REFUSED_LIMIT refusals in a row (default 3) the loop
# writes linear-webhook-tunnel.held (`PORT HOST`) beside ORCH_DB (default
# STATE/orch.db), where orch health reads it: health reports "port held by
# another owner" and self-heal raises one alert instead of restarting (issue
# #427). While held, the loop starts no tunnel. At once and then every
# LINEAR_WEBHOOK_HELD_RETRY_SEC it reclaims and probes (issue #581):
#   reclaim: one ssh to HOST runs `ss -Hltnp "sport = :PORT"`, logs each
#     listener as the holder (kept in linear-webhook-tunnel.holder for
#     status), and sends SIGTERM to those named sshd or sshd-session. ss names
#     only the ssh user's own processes, so this ends only the tunnel's own
#     stale sessions (sshd there may keep a dead tunnel's forward for hours);
#     any other holder is left alone and logged as `held by <users>`.
#   probe: `ssh -o ExitOnForwardFailure=yes -R ... HOST true`. Exit 0 removes
#     the record and the loop starts the tunnel again; otherwise the record
#     stays as written.
# start removes the record and the waiting loop retries at once; restart
# keeps it. A loop started on another port or host removes it.
#
# stop finds every loop for this state directory: each process whose argv is
# `.../linear-webhook-service.sh _listener-loop|_tunnel-loop` (any copy of the
# script) and whose state directory (its --state argument, else its
# ORCH_STATE_DIR or HOME) is this one, plus every process holding a loop
# or child lock (a `lock:` line in its /proc/PID/fdinfo for the lock file; merely
# having the file open does not count), plus each recorded child that is
# still ours. It ends each with its children. If a lock is still held
# afterwards (a loop in a PID namespace this shell cannot see), a recorded
# child still runs (its forward may still be held), or a child record comes
# from another PID namespace, stop says so and exits 1, and so does restart.
#
# Settings, from the environment:
#   LINEAR_WEBHOOK_PORT          the port serve binds and the tunnel forwards,
#                                the same on both ends of the tunnel; default:
#                                the number in ~/.config/factory/linear-webhook.port
#                                (this device's setting), else 8787
#   LINEAR_WEBHOOK_REFUSED_LIMIT refusals in a row before the tunnel is held
#                                (default 3)
#   LINEAR_WEBHOOK_HELD_RETRY_SEC whole seconds between a held tunnel's
#                                reclaim and probe runs (default 300)
#   LINEAR_WEBHOOK_TUNNEL_HOST   ssh alias for the tunnel (default
#                                pc-wsl-cursor-bot; empty runs no tunnel)
#   LINEAR_WEBHOOK_RETRY_SEC, LINEAR_WEBHOOK_RETRY_MAX_SEC   backoff (above)
#   ORCH_STATE_DIR               state directory (default ~/.local/state/factory)
#   ORCH_DB                      default: the state directory's orch.db
#   ORCH_CONFIG                  orch.yml for serve (default: the checkout's)
#
# Files in the state directory: linear-webhook-{listener,tunnel}.{lock,pid,log},
# linear-webhook-{listener,tunnel}-child.{lock,pid},
# linear-webhook.stopped (the stop hold), linear-webhook-service.lock
# (serializes start/stop/ensure) and linear-webhook-service.log (autostart).
#
# Autostart: install-autostart writes ~/.config/factory/linear-webhook-autostart,
# which makes the profile written by deploy/install.sh run `ensure` in the
# background whenever a bot shell starts. When `crontab` exists it also
# installs an @reboot line and a 5-minute `ensure` watchdog, tagged
# "# factory-linear-webhook" and replaced, never duplicated, on every run
# (issue #107). ORCH_CRONTAB names the crontab command (default: crontab on
# PATH). If the current crontab cannot be read (anything but "no crontab for
# <user>"), install-autostart and remove-autostart change nothing and exit 1.
set -euo pipefail

COMMAND="${1:-}"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
if [[ "$COMMAND" == _*-loop && "${2:-}" == --state && -n "${3:-}" ]]; then
    STATE="$3"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"

# shellcheck source=deploy/host-pidns.sh
. "${HERE}/host-pidns.sh"

CONFIG="${HOME}/.config/factory"
AUTOSTART="${CONFIG}/linear-webhook-autostart"
STOPPED="${STATE}/linear-webhook.stopped"
SERVICE_LOCK="${STATE}/linear-webhook-service.lock"
SERVICE_LOG="${STATE}/linear-webhook-service.log"
MARKER="# factory-linear-webhook"
CRONTAB="${ORCH_CRONTAB:-crontab}"
ORCH="${ORCH_BIN:-${HOME}/.local/bin/orch}"
PORT_FILE="${CONFIG}/linear-webhook.port"
PORT="${LINEAR_WEBHOOK_PORT:-}"
if [[ -z "$PORT" && -r "$PORT_FILE" ]]; then
    read -r PORT <"$PORT_FILE" || true
fi
PORT="${PORT:-8787}"
HELD="$(dirname "${ORCH_DB:-${STATE}/orch.db}")/linear-webhook-tunnel.held"
HOLDER="${STATE}/linear-webhook-tunnel.holder"
HELD_RETRY="${LINEAR_WEBHOOK_HELD_RETRY_SEC:-300}"
REFUSED_LIMIT="${LINEAR_WEBHOOK_REFUSED_LIMIT:-3}"
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
        "$START_WAIT" =~ ^[0-9]+$ && "$STOP_WAIT" =~ ^[0-9]+$ && "$REFUSED_LIMIT" =~ ^[1-9][0-9]*$ &&
        "$HELD_RETRY" =~ ^[1-9][0-9]*$ ]]; then
        say "LINEAR_WEBHOOK_PORT (or ${PORT_FILE}), *_SEC, *_WAIT and *_LIMIT must be numbers (LINEAR_WEBHOOK_HELD_RETRY_SEC whole seconds, at least 1)" >&2
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

# Run "$@" as loop $1's child and wait for it. First end the child an earlier
# loop recorded, if it still runs (real OpenSSH closes inherited descriptors,
# so its child lock alone does not prove it gone). Returns 1, starting
# nothing, when that child survives, cannot be verified from this PID
# namespace, or still holds the child lock (fd 7).
run_locked() {
    end_child "$1" || return 1
    exec 7>>"$(lock_file "$1-child")"
    if ! flock -n 7; then
        exec 7>&-
        return 1
    fi
    spawn_child "${@:2}"
    record_child "$1" "$CHILD"
    wait_child
    forget_child "$1" "$LAST_CHILD"
    exec 7>&-
}

# Run "$@" without the loop lock and wait for it; its exit status is STATUS.
run_child() {
    spawn_child "$@"
    wait_child
}

spawn_child() {
    "$@" 8>&- &
    CHILD=$!
}

wait_child() {
    LAST_CHILD="$CHILD"
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
        if ! run_locked listener "$ORCH" linear-webhook serve --bind "127.0.0.1:${PORT}" \
            ${ORCH_CONFIG:+--orch "$ORCH_CONFIG"}; then
            say "serve from an earlier loop may still run; not starting serve"
            pause "$delay"
            delay="$(doubled "$delay")"
            continue
        fi
        stopping && break
        ((SECONDS - started < HEALTHY_RUN)) || delay="$RETRY"
        say "orch linear-webhook serve exited ${STATUS}; retry in ${delay}s"
        pause "$delay"
        delay="$(doubled "$delay")"
    done
}

held_here() {
    [[ "$(cat "$HELD" 2>/dev/null)" == "${PORT} ${TUNNEL_HOST}" ]]
}

read -r -d '' RECLAIM_ON_HOST <<'EOF' || true
listeners=$(ss -Hltnp "sport = :@PORT@") || exit 1
[ -n "$listeners" ] || exit 0
own='\("sshd(-session)?",pid=[0-9]+'
printf '%s\n' "$listeners" | sed 's/^/holder: /'
printf '%s\n' "$listeners" | grep -vE "$own" |
    sed -E -e 's/.*(users:.*)/held by \1/' -e t -e 's/.*/held by another user/'
pids=$(printf '%s\n' "$listeners" | grep -oE "$own" | sed 's/.*=//')
[ -n "$pids" ] || exit 0
echo "ending sshd" $pids
kill -TERM $pids
i=0
while [ "$i" -lt 10 ] && [ -n "$(ss -Hltn "sport = :@PORT@")" ]; do
    sleep 1
    i=$((i + 1))
done
EOF

reclaim() {
    local errors="$1" line
    run_child ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 "$TUNNEL_HOST" "${RECLAIM_ON_HOST//@PORT@/$PORT}" \
        >"${HOLDER}.tmp" 2>"$errors"
    cat "$errors" >&2 2>/dev/null || true
    while IFS= read -r line; do
        say "reclaim on ${TUNNEL_HOST}: ${line}"
    done <"${HOLDER}.tmp"
    if ((STATUS == 0)); then
        mv -f "${HOLDER}.tmp" "$HOLDER"
    else
        rm -f "${HOLDER}.tmp"
        stopping || say "reclaim on ${TUNNEL_HOST} exited ${STATUS}"
    fi
}

probe() {
    run_child ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ConnectTimeout=30 \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -R "127.0.0.1:${PORT}:127.0.0.1:${PORT}" "$TUNNEL_HOST" true 2>"$1"
    cat "$1" >&2 2>/dev/null || true
    ((STATUS == 0))
}

held_wait() {
    local until=$((SECONDS + HELD_RETRY))
    while held_here && ! stopping && ((SECONDS < until)); do
        pause "$RETRY_MAX"
    done
}

tunnel_loop() {
    local delay="$RETRY" started errors refused=0
    [[ -n "$TUNNEL_HOST" ]] || {
        say "LINEAR_WEBHOOK_TUNNEL_HOST is empty; no tunnel" >&2
        exit 1
    }
    become_loop tunnel
    errors="${STATE}/linear-webhook-tunnel.stderr"
    held_here || rm -f "$HELD"
    while ! stopping; do
        if held_here; then
            reclaim "$errors"
            stopping && break
            if ! probe "$errors"; then
                stopping && break
                say "port ${PORT} on ${TUNNEL_HOST} is held by another owner; next reclaim and probe in ${HELD_RETRY}s (start retries at once)"
                held_wait
                continue
            fi
            rm -f "$HELD" "$HOLDER"
            say "port ${PORT} on ${TUNNEL_HOST} is free again; resuming the tunnel"
            delay="$RETRY"
        fi
        started="$SECONDS"
        say "starting ssh -R 127.0.0.1:${PORT}:127.0.0.1:${PORT} ${TUNNEL_HOST}"
        if ! run_locked tunnel ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
            -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
            -R "127.0.0.1:${PORT}:127.0.0.1:${PORT}" "$TUNNEL_HOST" 2>"$errors"; then
            cat "$errors" >&2 2>/dev/null || true
            say "ssh from an earlier loop may still run; not starting ssh"
            pause "$delay"
            delay="$(doubled "$delay")"
            continue
        fi
        cat "$errors" >&2 2>/dev/null || true
        stopping && break
        ((SECONDS - started < HEALTHY_RUN)) || delay="$RETRY"
        if grep -q "remote port forwarding failed" "$errors" 2>/dev/null; then
            refused=$((refused + 1))
            if ((refused >= REFUSED_LIMIT)); then
                printf '%s %s\n' "$PORT" "$TUNNEL_HOST" >"$HELD"
                say "forward refused ${refused} times: port ${PORT} on ${TUNNEL_HOST} is held by another owner; reclaiming it, then probing every ${HELD_RETRY}s"
                refused=0
                continue
            fi
            say "forward refused: port ${PORT} already forwarded on ${TUNNEL_HOST}; retry in ${delay}s"
        else
            refused=0
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
    for mode in listener tunnel listener-child tunnel-child; do
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

# --- owned children ------------------------------------------------------------

child_file() { printf '%s/linear-webhook-%s-child.pid' "$STATE" "$1"; }

pid_ns() { readlink "${PROC}/self/ns/pid" 2>/dev/null || true; }

start_time() {
    local stat
    local -a fields=()
    stat="$(cat "${PROC}/$1/stat" 2>/dev/null)" || return 1
    read -r -a fields <<<"${stat##*) }"
    [[ -n "${fields[19]:-}" ]] || return 1
    printf '%s\n' "${fields[19]}"
}

# Is pid $2's argv still loop $1's child: `-R 127.0.0.1:N:127.0.0.1:N` for
# the tunnel, `linear-webhook serve` for the listener?
child_argv() {
    local i
    local -a argv=()
    mapfile -d '' -t argv 2>/dev/null <"${PROC}/$2/cmdline" || return 1
    for ((i = 0; i + 1 < ${#argv[@]}; i++)); do
        case "$1" in
            tunnel)
                [[ "${argv[i]}" == -R && "${argv[i + 1]}" =~ ^127\.0\.0\.1:[0-9]+:127\.0\.0\.1:[0-9]+$ ]] &&
                    return 0
                ;;
            listener)
                [[ "${argv[i]}" == linear-webhook && "${argv[i + 1]}" == serve ]] && return 0
                ;;
        esac
    done
    return 1
}

record_child() {
    local file start
    file="$(child_file "$1")"
    start="$(start_time "$2")" || return 0
    printf '%s %s %s\n' "$2" "$start" "$(pid_ns)" >"${file}.tmp"
    mv -f "${file}.tmp" "$file"
}

forget_child() {
    local file pid rest
    file="$(child_file "$1")"
    read -r pid rest 2>/dev/null <"$file" || return 0
    [[ "$pid" != "$2" ]] || rm -f "$file"
}

# Print the pid loop $1 recorded for its child while that process still runs
# and is still that child (same start time and argv). Returns 1 when there is
# none (removing a stale record), 2 when the record is from another PID
# namespace and cannot be checked here.
owned_child() {
    local file pid start ns
    file="$(child_file "$1")"
    [[ -e "$file" ]] || return 1
    read -r pid start ns 2>/dev/null <"$file" || true
    if [[ -n "$ns" && "$ns" != "$(pid_ns)" ]]; then
        return 2
    fi
    if [[ "$pid" =~ ^[0-9]+$ ]] && alive "$pid" &&
        [[ "$(start_time "$pid")" == "$start" ]] && child_argv "$1" "$pid"; then
        printf '%s\n' "$pid"
        return 0
    fi
    rm -f "$file"
    return 1
}

owned_children() {
    local mode
    for mode in listener tunnel; do
        owned_child "$mode" || true
    done
}

# End the child loop $1 recorded, if it still runs: SIGTERM, SIGKILL after
# STOP_WAIT, and return 1 unless it is verifiably gone. A record from another
# PID namespace cannot be checked here, so it stays and this returns 1.
end_child() {
    local pid status=0
    pid="$(owned_child "$1")" || status=$?
    if ((status == 2)); then
        say "$(child_file "$1") names a process in another PID namespace; cannot verify it is gone; not starting another" >&2
        return 1
    fi
    ((status == 0)) || return 0
    say "ending $1 child pid ${pid} left by an earlier loop"
    kill -TERM "$pid" 2>/dev/null || true
    if ! wait_gone "$STOP_WAIT" "$pid"; then
        kill -KILL "$pid" 2>/dev/null || true
        if ! wait_gone 5 "$pid"; then
            say "$1 child pid ${pid} is still running after SIGKILL; not starting another" >&2
            return 1
        fi
    fi
    rm -f "$(child_file "$1")"
}

stop() {
    local mode status=0 pid found
    local -a roots=() pids=() left=()
    mkdir -p "$STATE"
    : >"$STOPPED"
    mapfile -t roots < <({ loop_pids; lock_holders; owned_children; } | grep -vx "$$" | sort -un)
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
        found=0
        pid="$(owned_child "$mode")" || found=$?
        if ((found == 0)); then
            say "${mode} child pid ${pid} is still running after SIGKILL; its forward or port may still be held" >&2
            status=1
        elif ((found == 2)); then
            say "$(child_file "$mode") names a process in another PID namespace; cannot verify it is gone; remove the record once that sandbox has exited" >&2
            status=1
        fi
    done
    for mode in listener tunnel listener-child tunnel-child; do
        if lock_held "$mode"; then
            say "$(lock_file "$mode") is still held by a process this shell cannot see (another PID namespace?); run stop from the host" >&2
            status=1
        fi
    done
    return "$status"
}

# See the header: a failed restart leaves no stop hold of its own.
restart() {
    local held=""
    [[ -e "$STOPPED" ]] && held=1
    stop && start && return 0
    [[ -n "$held" ]] || rm -f "$STOPPED"
    return 1
}

status() {
    local mode pid status=0
    for mode in $(modes); do
        pid="$(head -n 1 "$(pid_file "$mode")" 2>/dev/null || true)"
        if lock_held "$mode"; then
            echo "${mode}: running (lock held; pidfile names ${pid:-nothing})"
            if [[ "$mode" == tunnel ]] && held_here; then
                echo "tunnel: port ${PORT} on ${TUNNEL_HOST} held by another owner; reclaim and probe every ${HELD_RETRY}s, or run start to retry now"
                [[ ! -s "$HOLDER" ]] || sed 's/^/tunnel: last reclaim: /' "$HOLDER"
            fi
        else
            echo "${mode}: not running"
            status=3
        fi
    done
    [[ ! -e "$STOPPED" ]] || echo "held stopped by ${STOPPED}; run start to clear"
    return "$status"
}

# --- autostart ---------------------------------------------------------------

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
    _listener-loop) factory_host_pidns_or_refuse _listener-loop && check_settings && need_flock && listener_loop ;;
    _tunnel-loop) factory_host_pidns_or_refuse _tunnel-loop && check_settings && need_flock && tunnel_loop ;;
    start) factory_host_pidns_or_refuse start && check_settings && need_flock && service_lock && rm -f "$HELD" && start ;;
    stop) factory_host_pidns_or_refuse stop && need_flock && service_lock && stop ;;
    restart) factory_host_pidns_or_refuse restart && check_settings && need_flock && service_lock && restart ;;
    status) need_flock && status ;;
    ensure) factory_host_pidns_or_refuse ensure && check_settings && need_flock && service_lock && ensure ;;
    install-autostart) install_autostart ;;
    remove-autostart) remove_autostart ;;
    *) usage ;;
esac
