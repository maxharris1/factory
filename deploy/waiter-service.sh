#!/usr/bin/env bash
# Run `orch wait <bot> --autostart` under a restart supervisor (issue #80), on
# a host without a systemd user session. Follows deploy/poller-service.sh and
# the local Linear webhook service: a small supervisor loop owns the waiter
# and starts it again whenever it exits or is killed.
#
#   waiter-service.sh <bot> start             start unless running; clears a stop
#   waiter-service.sh <bot> stop              stop the supervisor and its waiter,
#                                             and hold them stopped
#   waiter-service.sh <bot> restart           stop, then start
#   waiter-service.sh <bot> status            exit 0 if running, 3 if not
#   waiter-service.sh <bot> ensure            start unless running or held stopped
#                                             (what autostart runs; never clears a stop)
#   waiter-service.sh <bot> install-autostart enable autostart (see below)
#   waiter-service.sh <bot> remove-autostart  disable it
#
# <bot> is claude-bot, codex-bot or cursor-bot. Run it as that bot's own user,
# with that bot's harness login: the waiter starts that bot's harness, with the
# command in ~/.config/factory/waiter-<bot>.yml (config/waiter.example.yml).
#
# The waiter starts each job's `orch job run` in its own session, so stopping
# or restarting the waiter never stops a running job. The waiter holds a
# per-bot lock beside the database (waiter-<bot>.lock, holding its pid) for
# its whole life, so a second waiter for the bot refuses to start. If only
# the supervisor is killed (SIGKILL), its waiter lives on holding the lock:
# a new supervisor's waiter then refuses and is retried with backoff, and
# stop also stops that waiter (the lock's pid, checked to be this bot's
# `orch wait <bot> --autostart`). stop also stops every other supervisor
# for this bot and state directory owned by this user, and a supervisor
# whose pid is no longer in the pidfile exits instead of starting another
# waiter (#295). A waiter that exits is
# started again after ORCH_WAITER_RESTART_SEC (default 5) seconds; when it
# keeps exiting within 30 seconds the pause doubles, up to 60 seconds.
#
# Drain (#219): self-heal writes waiter-<bot>.drain in the state directory
# when this bot's waiter runs stale code with jobs in flight; the waiter
# (told the path by ORCH_WAITER_DRAIN) then claims nothing, acknowledges in
# waiter-<bot>.drain.ack, and self-heal restarts it after its last job.
# start removes the marker and the ack.
#
# Environment: ~/.config/factory/waiter.env, if present, is read for the
# waiter (ORCH_DB and other ORCH_* paths only). ORCH_DB defaults to
# the shared state database, ~/.local/state/factory/orch.db.
#
# Autostart: install-autostart writes ~/.config/factory/waiter-autostart-<bot>,
# which makes the profile written by deploy/install.sh run `ensure` for that
# bot whenever a bot shell starts. When `crontab` exists it also installs an
# @reboot line and a 5-minute `ensure` watchdog, tagged
# "# factory-orch-waiter-<bot>" and replaced, never duplicated, on every run.
# ORCH_CRONTAB names the crontab command (default: crontab on PATH). If the
# current crontab cannot be read (anything but "no crontab for <user>"),
# install-autostart and remove-autostart change nothing and exit 1.
set -euo pipefail

usage() {
    echo "usage: $(basename "$0") {claude-bot|codex-bot|cursor-bot} {start|stop|restart|status|ensure|install-autostart|remove-autostart}" >&2
    exit 2
}

BOT="${1:-}"
case "$BOT" in
    claude-bot | codex-bot | cursor-bot) ;;
    *) usage ;;
esac
COMMAND="${2:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
PIDFILE="${STATE}/waiter-${BOT}.pid"
LOG="${STATE}/waiter-${BOT}.log"
SERVICE_LOG="${STATE}/waiter-${BOT}-service.log"
SERVICE_LOCK="${STATE}/waiter-${BOT}-service.lock"
STOPPED="${STATE}/waiter-${BOT}.stopped"
DRAIN="${STATE}/waiter-${BOT}.drain"
DBFILE="${STATE}/waiter-${BOT}.db"
AUTOSTART="${CONFIG}/waiter-autostart-${BOT}"
ENV_FILE="${CONFIG}/waiter.env"
ORCH="${HOME}/.local/bin/orch"
MARKER="# factory-orch-waiter-${BOT}"
RESTART_SEC="${ORCH_WAITER_RESTART_SEC:-5}"
RESTART_MAX=60
START_WAIT="${ORCH_WAITER_START_WAIT:-15}"
STOP_WAIT="${ORCH_WAITER_STOP_WAIT:-60}"
CRONTAB="${ORCH_CRONTAB:-crontab}"
PROC="${ORCH_PROC_DIR:-/proc}"
# cron and fresh shells have a minimal PATH; the waiter needs orch, gh and git.
export PATH="${HOME}/.local/bin${PATH:+:${PATH}}:/usr/local/bin:/usr/bin:/bin"

say() { printf '%s waiter-service %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BOT" "$*"; }

# Is this argv this bot's supervisor (`bash .../waiter-service.sh <bot> _supervise`)?
is_supervisor_argv() {
    local i
    for i in 0 1; do
        if (($# > i + 2)) && [[ "${@:i+1:1}" =~ (^|/)waiter-service\.sh$ &&
            "${@:i+2:1}" == "$BOT" && "${@:i+3:1}" == _supervise ]]; then
            return 0
        fi
    done
    return 1
}

# Is this argv a supervisor for this bot and state directory? One started
# before the state directory joined its argv counts when it runs this script.
is_own_supervisor_argv() {
    local i
    for i in 0 1; do
        if (($# > i + 2)) && [[ "${@:i+1:1}" =~ (^|/)waiter-service\.sh$ &&
            "${@:i+2:1}" == "$BOT" && "${@:i+3:1}" == _supervise ]]; then
            if (($# > i + 3)); then
                [[ "${@:i+4:1}" == "$STATE" ]]
            else
                [[ "${@:i+1:1}" == "$SELF" ]]
            fi
            return
        fi
    done
    return 1
}

# Print the pid of every supervisor for this bot and state directory that
# this user owns, the pidfile's included.
own_supervisors() {
    local dir pid command
    local -a argv=()
    if [[ -d "$PROC" ]]; then
        for dir in "$PROC"/[0-9]*; do
            [[ -O "$dir" ]] || continue
            mapfile -d '' -t argv 2>/dev/null <"${dir}/cmdline" || continue
            if is_own_supervisor_argv "${argv[@]}"; then
                printf '%s\n' "${dir##*/}"
            fi
        done
    else
        while read -r pid command; do
            read -r -a argv <<<"$command"
            if is_own_supervisor_argv "${argv[@]}"; then
                printf '%s\n' "$pid"
            fi
        done < <(ps -U "$(id -u)" -o pid= -o command=)
    fi
}

owns_pidfile() {
    [[ "$(head -n 1 "$PIDFILE" 2>/dev/null || true)" == "$$" ]]
}

# Print the pid of this bot's running supervisor, or fail.
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

# Is this argv this bot's autostart waiter (`... orch wait <bot> --autostart ...`)?
is_waiter_argv() {
    local i n=$#
    local -a argv=("$@")
    for ((i = 0; i + 1 < n; i++)); do
        if [[ "${argv[i]}" == wait && "${argv[i + 1]}" == "$BOT" ]]; then
            local rest
            for rest in "${argv[@]:i+2}"; do
                [[ "$rest" == --autostart ]] && return 0
            done
        fi
    done
    return 1
}

# Print the pid of the waiter holding this bot's lock (one a SIGKILLed
# supervisor left behind), or fail.
waiter_pid() {
    local db lock pid command
    local -a argv=()
    [[ -r "$DBFILE" ]] || return 1
    db="$(head -n 1 "$DBFILE" 2>/dev/null || true)"
    [[ "$db" == /* ]] || return 1
    lock="$(dirname "$db")/waiter-${BOT}.lock"
    [[ -r "$lock" ]] || return 1
    pid="$(head -n 1 "$lock" 2>/dev/null || true)"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -d "$PROC" ]]; then
        [[ -r "${PROC}/${pid}/cmdline" ]] || return 1
        mapfile -d '' -t argv <"${PROC}/${pid}/cmdline" || return 1
    else
        command="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
        read -r -a argv <<<"$command"
    fi
    is_waiter_argv "${argv[@]}" || return 1
    printf '%s\n' "$pid"
}

stop_waiter() {
    local pid i
    pid="$(waiter_pid)" || return 0
    kill -TERM "$pid" 2>/dev/null || return 0
    for ((i = 0; i < STOP_WAIT * 10; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            say "stopped orphaned waiter (pid ${pid})"
            return 0
        fi
        sleep 0.1
    done
    say "orphaned waiter (pid ${pid}) did not stop within ${STOP_WAIT}s" >&2
    return 1
}

service_lock() {
    mkdir -p "$STATE"
    exec 9>>"$SERVICE_LOCK"
    if command -v flock >/dev/null 2>&1; then
        flock 9
    elif command -v perl >/dev/null 2>&1; then
        LC_ALL=C perl -MFcntl=:flock -e 'open(my $fh, ">&=", 9) or die "$!\n"; flock($fh, LOCK_EX) or die "$!\n"'
    fi
}

# The supervisor loop. It owns the pidfile while its pid is there, and exits
# once another supervisor's pid replaces it; SIGTERM stops the waiter and it.
supervise() {
    local child="" status started delay="$RESTART_SEC" stopping=""
    trap 'stopping=1; [[ -n "$child" ]] && kill -TERM "$child" 2>/dev/null || true' TERM INT HUP
    mkdir -p "$STATE"
    printf '%s\n' "$$" >"$PIDFILE"
    if [[ -r "$ENV_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
    fi
    export ORCH_DB="${ORCH_DB:-${STATE}/orch.db}"
    [[ "$ORCH_DB" == /* ]] || ORCH_DB="${PWD}/${ORCH_DB}"
    printf '%s\n' "$ORCH_DB" >"$DBFILE"
    export ORCH_WAITER_DRAIN="$DRAIN"
    export PYTHONUNBUFFERED=1
    while [[ -z "$stopping" && ! -e "$STOPPED" ]]; do
        if ! owns_pidfile; then
            say "${PIDFILE} no longer names this supervisor (pid $$); exiting"
            break
        fi
        started="$SECONDS"
        say "starting orch wait ${BOT} --autostart"
        "$ORCH" wait "$BOT" --autostart 9>&- &
        child=$!
        status=0
        wait "$child" || status=$?
        # A trapped signal interrupts `wait`; wait again until the waiter is gone.
        while kill -0 "$child" 2>/dev/null; do
            status=0
            wait "$child" || status=$?
        done
        child=""
        [[ -z "$stopping" && ! -e "$STOPPED" ]] || break
        if ((SECONDS - started >= 30)); then
            delay="$RESTART_SEC"
        fi
        say "orch wait ${BOT} exited ${status}; restarting in ${delay}s"
        sleep "$delay" &
        wait $! || true
        delay="$(awk -v d="$delay" -v m="$RESTART_MAX" 'BEGIN { d *= 2; if (d > m) d = m; print d }')"
    done
    say "supervisor stopped"
    if owns_pidfile; then
        rm -f "$PIDFILE"
    fi
}

start() {
    mkdir -p "$STATE"
    rm -f "$STOPPED" "$DRAIN" "$DRAIN.ack"
    launch
}

launch() {
    local pid child i
    mkdir -p "$STATE"
    if pid="$(running_pid)"; then
        say "waiter supervisor already running (pid ${pid})"
        return 0
    fi
    nohup bash "$SELF" "$BOT" _supervise "$STATE" >>"$LOG" 2>&1 </dev/null 9>&- &
    child=$!
    for ((i = 0; i < START_WAIT * 10; i++)); do
        if pid="$(running_pid)"; then
            say "started waiter supervisor (pid ${pid})"
            return 0
        fi
        if ! kill -0 "$child" 2>/dev/null; then
            say "waiter supervisor exited at startup; see ${LOG}" >&2
            return 1
        fi
        sleep 0.1
    done
    say "waiter supervisor did not write ${PIDFILE} within ${START_WAIT}s; see ${LOG}" >&2
    return 1
}

stop() {
    local main="" pids="" left="" pid i
    mkdir -p "$STATE"
    : >"$STOPPED"
    main="$(running_pid)" || say "waiter supervisor is not running"
    for pid in $main $(own_supervisors); do
        [[ "${pids} " == *" ${pid} "* ]] || pids+=" ${pid}"
    done
    for pid in $pids; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    for ((i = 0; i < STOP_WAIT * 10; i++)); do
        left=""
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                left+=" ${pid}"
            fi
        done
        [[ -n "$left" ]] || break
        sleep 0.1
    done
    for pid in $pids; do
        if [[ "${left} " == *" ${pid} "* ]]; then
            say "waiter supervisor (pid ${pid}) did not stop within ${STOP_WAIT}s" >&2
        elif [[ "$pid" == "$main" ]]; then
            say "stopped waiter supervisor (pid ${pid})"
        else
            say "stopped stray waiter supervisor (pid ${pid})"
        fi
    done
    [[ -z "$left" ]] || return 1
    stop_waiter
}

# A restart whose stop times out drops the hold that stop just wrote, so
# ensure and self-heal still see a service to recover; a hold that was
# already there (an operator's stop) stays.
restart() {
    local held=""
    [[ -e "$STOPPED" ]] && held=1
    stop && start && return 0
    [[ -n "$held" ]] || rm -f "$STOPPED"
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
        echo "waiter-service: refusing to write ${1} into a crontab" >&2
        return 1
    }
}

crontab_without_marker() {
    local current errors status=0
    errors="$(mktemp "${TMPDIR:-/tmp}/waiter-service.XXXXXX")" || return 1
    current="$("$CRONTAB" -l 2>"$errors")" || status=$?
    if ((status != 0)); then
        if grep -q "no crontab for" "$errors"; then
            current=""
        else
            echo "waiter-service: cannot read the crontab (${CRONTAB} -l exited ${status}); changed nothing:" >&2
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
    local kept="" job="${SELF} ${BOT} ensure >>${SERVICE_LOG} 2>&1 ${MARKER}"
    if have_crontab; then
        plain_path "$SELF"
        plain_path "$SERVICE_LOG"
        kept="$(crontab_without_marker)" || exit 1
    fi
    mkdir -p "$CONFIG" "$STATE"
    : >"$AUTOSTART"
    echo "waiter autostart on for ${BOT}: bot shells run '${SELF} ${BOT} ensure' (${AUTOSTART})"
    if ! have_crontab; then
        echo "crontab not found; autostart uses the bot-shell profile hook only"
        return 0
    fi
    {
        [[ -z "$kept" ]] || printf '%s\n' "$kept"
        printf '@reboot %s\n' "$job"
        printf '*/5 * * * * %s\n' "$job"
    } | "$CRONTAB" -
    echo "installed crontab @reboot and 5-minute ensure lines for ${BOT}"
}

remove_autostart() {
    local kept=""
    if have_crontab; then
        kept="$(crontab_without_marker)" || exit 1
    fi
    rm -f "$AUTOSTART"
    echo "waiter autostart off for ${BOT}"
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
    restart) service_lock && restart ;;
    status) status ;;
    ensure) service_lock && ensure ;;
    install-autostart) install_autostart ;;
    remove-autostart) remove_autostart ;;
    *) usage ;;
esac
