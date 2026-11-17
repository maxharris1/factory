#!/usr/bin/env bash
# Run `orch poll` as a service on a host without a systemd user session.
#
#   poller-service.sh start             start unless already running; clears a stop
#   poller-service.sh stop              SIGTERM the poller and hold it stopped
#   poller-service.sh restart           stop, then start
#   poller-service.sh status            exit 0 if running, 3 if not
#   poller-service.sh ensure            start unless running or held stopped
#                                       (what autostart runs; never clears a stop)
#   poller-service.sh install-autostart enable autostart (see below)
#   poller-service.sh remove-autostart  disable it
#
# `orch poll` owns the pidfile and the single-poller lock itself; this script
# only reads the pidfile. Output goes to poller.log beside the pidfile.
# On a host retired as the orch host (~/.config/factory/orch-retired),
# start, restart and ensure do nothing and exit 79; stop and status still work.
#
# Dashboard push: when orch.yml sets dashboard.push (README "Dashboard"), the
# poller this script runs also starts `orch dashboard push --once` after each
# poll cycle, or every dashboard.push.every_minutes. Each push is a separate
# process with only PATH, HOME, locale, SSH_AUTH_SOCK and the ORCH_* paths in
# its environment (none of webhook.env), and the poll loop never waits on it:
# a failed push is one redacted line in poller.log and polling goes on.
# Without dashboard.push nothing is pushed and nothing changes here.
# start, stop, restart, ensure and remove-autostart take poller-service.lock
# (flock(1), where installed), so a stop cannot interleave with an ensure
# about to launch, and remove-autostart returns only after any ensure that
# still saw poller-autostart has finished.
# Before launching, start and ensure also probe orch.db.lock, the lock
# `orch poll` holds beside its database after resolving symlinks: the ORCH_DB
# run-poller.sh ends up with after sourcing its env files
# (`run-poller.sh --print-db`), else the caller's ORCH_DB, else
# ~/.local/state/factory/orch.db. While it is held, a
# poller runs even if its pid is not visible from here (another PID
# namespace), so nothing is launched (issue #134).
#
# Autostart: install-autostart writes ~/.config/factory/poller-autostart, which
# makes the profile written by deploy/install.sh run `ensure` in the
# background whenever a bot shell starts. When `crontab` exists it also
# installs an @reboot line and a 5-minute `ensure` watchdog, tagged
# "# factory-orch-poll" and replaced, never duplicated, on every run.
# With poller-autostart on and no `crontab` (issue #248), `ensure` logs that
# once per state directory and starts deploy/poller-ensure-loop.sh unless it
# already runs, so `ensure` still runs every ORCH_POLL_ENSURE_SEC seconds.
# That loop runs with ORCH_POLL_AUTOSTART_LOOP=1: once poller-autostart is
# gone, its `ensure` launches nothing and exits 76, and the loop exits.
#
# Waiters (issue #248): every `ensure`, held stopped or not, also runs
# `waiter-service.sh <bot> ensure` for each ~/.config/factory/waiter-autostart-<bot>,
# logging to waiter-<bot>-service.log, so whatever keeps the poller alive keeps
# the autostarted waiters alive too.
# ORCH_CRONTAB names the crontab command (default: crontab on PATH). If the
# current crontab cannot be read (anything but "no crontab for <user>"),
# install-autostart and remove-autostart change nothing and exit 1.
#
# State loss (#66): before a start, a missing ${ORCH_DB:-<state>/orch.db} is
# restored from the backup `orch poll` takes every cycle (`orch restore-db`),
# logging RESTORED. ORCH_DB and ORCH_DB_BACKUP are read with webhook.env and
# orch-poll.env loaded, as run-poller.sh loads them, so recovery uses the
# poller's own paths. With no backup it is created empty (`orch init-db`) with a
# WARNING; any other restore failure starts nothing. While a box copy-in is
# still restoring files (ORCH_COPY_IN_STATUS, default
# /tmp/sand-copy-in-status.json, exists and its "phase" is not "done"), the
# restore waits up to ORCH_COPY_IN_WAIT seconds (default 600) for it, checking
# every ORCH_COPY_IN_POLL_SEC (default 5); an orch.db the copy-in brings back
# is kept and nothing is restored.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"

# shellcheck source=deploy/host-pidns.sh
. "${HERE}/host-pidns.sh"

STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
PIDFILE="${STATE}/poller.pid"
LOG="${ORCH_POLL_LOG:-${STATE}/poller.log}"
SERVICE_LOG="${STATE}/poller-service.log"
SERVICE_LOCK="${STATE}/poller-service.lock"
STOPPED="${STATE}/poller.stopped"
AUTOSTART="${CONFIG}/poller-autostart"
RUNNER="${HERE}/run-poller.sh"
MARKER="# factory-orch-poll"
START_WAIT="${ORCH_POLL_START_WAIT:-15}"
STOP_WAIT="${ORCH_POLL_STOP_WAIT:-60}"
CRONTAB="${ORCH_CRONTAB:-crontab}"
PROC="${ORCH_PROC_DIR:-/proc}"
WAITER_SERVICE="${HERE}/waiter-service.sh"
ENSURE_LOOP="${HERE}/poller-ensure-loop.sh"
ENSURE_LOOP_LOCK="${STATE}/poller-ensure-loop.lock"
NO_CRONTAB_LOGGED="${STATE}/crontab-absent.logged"
COPY_IN_STATUS="${ORCH_COPY_IN_STATUS:-/tmp/sand-copy-in-status.json}"
COPY_IN_WAIT="${ORCH_COPY_IN_WAIT:-600}"
COPY_IN_POLL="${ORCH_COPY_IN_POLL_SEC:-5}"
AUTOSTART_LOOP="${ORCH_POLL_AUTOSTART_LOOP:-}"
unset ORCH_POLL_AUTOSTART_LOOP
# cron and fresh shells have a minimal PATH; the poller needs orch and gh.
# The caller's PATH keeps precedence; the standard directories only follow it.
export PATH="${HOME}/.local/bin${PATH:+:${PATH}}:/usr/local/bin:/usr/bin:/bin"

say() { printf '%s poller-service: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Is this argv `orch poll ...`? Either orch itself (argv[0]) or orch run by
# an interpreter (argv[1], as deploy/install.sh's wrapper execs it) must be
# a file named orch, and the very next argument must be `poll`.
is_poller_argv() {
    local i
    for i in 0 1; do
        if (($# > i + 1)) && [[ "${@:i+1:1}" =~ (^|/)orch$ && "${@:i+2:1}" == poll ]]; then
            return 0
        fi
    done
    return 1
}

# Print the pid of the running poller, or fail. A pidfile naming a dead
# process, or a live process that is not `orch poll`, does not count. With
# no /proc (macOS), ps(1) names the command; if it cannot, the pid does not
# count either (orch poll's own lock still refuses a second poller).
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
        is_poller_argv "${argv[@]}" || return 1
    else
        # ps joins argv with spaces; the poller's paths have none.
        command="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
        read -r -a argv <<<"$command"
        is_poller_argv "${argv[@]}" || return 1
    fi
    printf '%s\n' "$pid"
}

# Is orch poll's own lock held? Without flock(1) or the lock file, no.
poll_lock_held() {
    local db lock status=0
    command -v flock >/dev/null 2>&1 || return 1
    db="$("$RUNNER" --print-db 2>/dev/null 9>&-)" || db=""
    db="${db:-${ORCH_DB:-${STATE}/orch.db}}"
    lock="$(readlink -f -- "$db" || printf '%s' "$db").lock"
    [[ -e "$lock" ]] || return 1
    flock -n -E 75 "$lock" true 9>&- || status=$?
    ((status == 75))
}

retired() {
    [[ -e "${CONFIG}/orch-retired" ]]
}

refuse_if_retired() {
    local retired="${CONFIG}/orch-retired"
    retired || return 0
    echo "poller-service: refusing to $1 on a host retired as the orch host (${retired}: $(head -n 1 "$retired" 2>/dev/null || true))" >&2
    exit 79
}

service_lock() {
    command -v flock >/dev/null 2>&1 || return 0
    mkdir -p "$STATE"
    exec 9>>"$SERVICE_LOCK"
    flock 9
}

start() {
    mkdir -p "$STATE"
    rm -f "$STOPPED"
    launch
}

# Both to stderr (the service log under cron) and to the poller log.
loud() {
    say "$@" | tee -a "$LOG" >&2
}

# The copy-in status file's "phase", or nothing when it has none.
copy_in_phase() {
    local text
    text="$(cat "$COPY_IN_STATUS" 2>/dev/null)" || return 0
    [[ "$text" =~ \"phase\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] || return 0
    printf '%s\n' "${BASH_REMATCH[1]}"
}

# #248: a box copy-in may still be restoring orch.db; wait (bounded) for it
# to finish or for the database to come back.
wait_for_copy_in() {
    local db="$1" phase deadline
    [[ -e "$COPY_IN_STATUS" ]] || return 0
    phase="$(copy_in_phase)"
    [[ "$phase" == done ]] && return 0
    loud "box copy-in is still running (phase ${phase:-unknown}); waiting up to ${COPY_IN_WAIT}s before restoring orch.db"
    deadline=$((SECONDS + COPY_IN_WAIT))
    while ((SECONDS <= deadline)); do
        sleep "$COPY_IN_POLL"
        [[ -e "$db" ]] && return 0
        [[ -e "$COPY_IN_STATUS" ]] || return 0
        [[ "$(copy_in_phase)" == done ]] && return 0
    done
    loud "box copy-in did not finish within ${COPY_IN_WAIT}s; restoring orch.db anyway"
}

# #66: never start the poller on a silently empty database. A subshell, so
# the poller's environment stays out of this script.
ensure_db() (
    local db status=0
    set -a
    . "${CONFIG}/webhook.env"
    . "${CONFIG}/orch-poll.env"
    set +a
    db="${ORCH_DB:-${STATE}/orch.db}"
    mkdir -p "$STATE" "$(dirname "$db")"
    [[ -e "$db" ]] && return 0
    wait_for_copy_in "$db"
    if [[ -e "$db" ]]; then
        loud "orch.db came back with the box copy-in; nothing restored"
        return 0
    fi
    retired && return 79
    orch restore-db --db "$db" >&2 || status=$?
    case "$status" in
        0) loud "RESTORED orch.db from backup" ;;
        3)
            orch init-db --db "$db" >&2 || return 1
            loud "WARNING orch.db was missing and no backup exists; created an empty database (dedupe now relies on thread markers)"
            ;;
        *)
            loud "orch.db is missing and orch restore-db exited ${status}; not starting the poller"
            return 1
            ;;
    esac
)

launch() {
    local pid child i
    mkdir -p "$STATE"
    if pid="$(running_pid)"; then
        say "orch poll already running (pid ${pid})"
        return 0
    fi
    if poll_lock_held; then
        say "orch poll already running (poll lock held)"
        return 0
    fi
    refuse_if_retired start
    ensure_db || {
        refuse_if_retired start
        return 1
    }
    refuse_if_retired start
    # fd 9 is the service lock; the poller must not inherit it.
    nohup "$RUNNER" --pidfile "$PIDFILE" >>"$LOG" 2>&1 </dev/null 9>&- &
    child=$!
    for ((i = 0; i < START_WAIT * 10; i++)); do
        if pid="$(running_pid)"; then
            if ((pid == child)); then
                say "started orch poll (pid ${pid})"
            else
                say "orch poll already running (pid ${pid}); stopping launch ${child}"
                stop_launch "$child"
            fi
            return 0
        fi
        if ! kill -0 "$child" 2>/dev/null; then
            say "orch poll exited at startup; see ${LOG}" >&2
            return 1
        fi
        sleep 0.1
    done
    say "orch poll did not write ${PIDFILE} within ${START_WAIT}s; stopping it; see ${LOG}" >&2
    stop_launch "$child"
    return 1
}

stop_launch() {
    local i
    kill -TERM "$1" 2>/dev/null || true
    for ((i = 0; i < STOP_WAIT * 10; i++)); do
        kill -0 "$1" 2>/dev/null || return 0
        sleep 0.1
    done
    kill -KILL "$1" 2>/dev/null || true
    wait "$1" 2>/dev/null || true
}

stop() {
    local pid i
    mkdir -p "$STATE"
    : >"$STOPPED"
    if ! pid="$(running_pid)"; then
        say "orch poll is not running"
        return 0
    fi
    kill -TERM "$pid"
    for ((i = 0; i < STOP_WAIT * 10; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            say "stopped orch poll (pid ${pid})"
            return 0
        fi
        sleep 0.1
    done
    say "orch poll (pid ${pid}) did not stop within ${STOP_WAIT}s" >&2
    return 1
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

ensure_poller() {
    [[ -e "$STOPPED" ]] && return 0
    running_pid >/dev/null && return 0
    poll_lock_held && return 0
    # Re-check right before launching: a stop may have just landed.
    [[ -e "$STOPPED" ]] && return 0
    launch
}

ensure_waiters() {
    local flag bot status
    [[ -x "$WAITER_SERVICE" ]] || return 0
    for flag in "${CONFIG}"/waiter-autostart-*; do
        [[ -f "$flag" ]] || continue
        bot="${flag##*/waiter-autostart-}"
        case "$bot" in claude-bot | codex-bot | cursor-bot) ;; *) continue ;; esac
        status=0
        "$WAITER_SERVICE" "$bot" ensure >>"${STATE}/waiter-${bot}-service.log" 2>&1 </dev/null 9>&- || status=$?
        ((status == 0)) || say "waiter-service.sh ${bot} ensure exited ${status}" >&2
    done
}

# #248: with no crontab, poller-ensure-loop.sh is the watchdog.
ensure_watchdog() {
    local status=0
    have_crontab && return 0
    [[ -e "$AUTOSTART" ]] || return 0
    if [[ ! -e "$NO_CRONTAB_LOGGED" ]]; then
        loud "crontab not found; ${ENSURE_LOOP} runs ensure instead"
        : >"$NO_CRONTAB_LOGGED"
    fi
    [[ -x "$ENSURE_LOOP" ]] || return 0
    command -v flock >/dev/null 2>&1 || return 0
    if [[ -e "$ENSURE_LOOP_LOCK" ]]; then
        flock -n -E 75 "$ENSURE_LOOP_LOCK" true 9>&- || status=$?
        ((status == 75)) && return 0
    fi
    ORCH_POLL_AUTOSTART_LOOP=1 nohup "$ENSURE_LOOP" >>"${STATE}/poller-ensure-loop.log" 2>&1 </dev/null 9>&- &
}

ensure() {
    local status=0
    mkdir -p "$STATE"
    if [[ -n "$AUTOSTART_LOOP" && ! -e "$AUTOSTART" ]]; then
        say "poller autostart is off; the ensure loop it started stops here"
        return 76
    fi
    ensure_poller || status=$?
    ensure_waiters
    ensure_watchdog
    return "$status"
}

# cron runs lines through /bin/sh and treats % specially, so only plain
# paths are written into the crontab.
plain_path() {
    [[ "$1" =~ ^/[A-Za-z0-9/._-]+$ ]] || {
        echo "poller-service: refusing to write ${1} into a crontab" >&2
        return 1
    }
}

# Print the current crontab without our tagged lines. "no crontab for
# <user>" is an empty table; any other read failure fails, so the caller
# writes nothing rather than replacing the table with what it could read.
crontab_without_marker() {
    local current errors status=0
    errors="$(mktemp "${TMPDIR:-/tmp}/poller-service.XXXXXX")" || return 1
    current="$("$CRONTAB" -l 2>"$errors")" || status=$?
    if ((status != 0)); then
        if grep -q "no crontab for" "$errors"; then
            current=""
        else
            echo "poller-service: cannot read the crontab (${CRONTAB} -l exited ${status}); changed nothing:" >&2
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
    echo "poller autostart on: bot shells run '${SELF} ensure' (${AUTOSTART})"
    if ! have_crontab; then
        echo "crontab not found; autostart uses the bot-shell profile hook, and ensure starts deploy/poller-ensure-loop.sh"
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
    echo "poller autostart off"
    if have_crontab; then
        if [[ -n "$kept" ]]; then
            printf '%s\n' "$kept" | "$CRONTAB" -
        else
            "$CRONTAB" - </dev/null
        fi
        echo "removed crontab lines tagged ${MARKER}"
    fi
}

case "${1:-}" in
    start) factory_host_pidns_or_refuse start && refuse_if_retired start && service_lock && refuse_if_retired start && start ;;
    stop) factory_host_pidns_or_refuse stop && service_lock && stop ;;
    restart) factory_host_pidns_or_refuse restart && refuse_if_retired restart && service_lock && refuse_if_retired restart && restart ;;
    status) status ;;
    ensure) factory_host_pidns_or_refuse ensure && refuse_if_retired ensure && service_lock && refuse_if_retired ensure && ensure ;;
    install-autostart) install_autostart ;;
    remove-autostart) service_lock && remove_autostart ;;
    *)
        echo "usage: $(basename "$0") {start|stop|restart|status|ensure|install-autostart|remove-autostart}" >&2
        exit 2
        ;;
esac
