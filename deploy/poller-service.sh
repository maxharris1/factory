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
#
# Dashboard push: when orch.yml sets dashboard.push (README "Dashboard"), the
# poller this script runs also starts `orch dashboard push --once` after each
# poll cycle, or every dashboard.push.every_minutes. Each push is a separate
# process with only PATH, HOME, locale, SSH_AUTH_SOCK and the ORCH_* paths in
# its environment (none of webhook.env), and the poll loop never waits on it:
# a failed push is one redacted line in poller.log and polling goes on.
# Without dashboard.push nothing is pushed and nothing changes here.
# start, stop, restart and ensure take poller-service.lock (flock(1), where
# installed), so a stop cannot interleave with an ensure about to launch.
#
# Autostart: install-autostart writes ~/.config/factory/poller-autostart, which
# makes the profile written by deploy/install.sh run `ensure` in the
# background whenever a bot shell starts. When `crontab` exists it also
# installs an @reboot line and a 5-minute `ensure` watchdog, tagged
# "# factory-orch-poll" and replaced, never duplicated, on every run.
# ORCH_CRONTAB names the crontab command (default: crontab on PATH). If the
# current crontab cannot be read (anything but "no crontab for <user>"),
# install-autostart and remove-autostart change nothing and exit 1.
#
# State loss (#66): before a start, a missing ${ORCH_DB:-<state>/orch.db} is
# restored from the backup `orch poll` takes every cycle (`orch restore-db`),
# logging RESTORED. With no backup it is created empty (`orch init-db`) with a
# WARNING; any other restore failure starts nothing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
PIDFILE="${STATE}/poller.pid"
LOG="${ORCH_POLL_LOG:-${STATE}/poller.log}"
SERVICE_LOG="${STATE}/poller-service.log"
SERVICE_LOCK="${STATE}/poller-service.lock"
DB="${ORCH_DB:-${STATE}/orch.db}"
STOPPED="${STATE}/poller.stopped"
AUTOSTART="${CONFIG}/poller-autostart"
RUNNER="${HERE}/run-poller.sh"
MARKER="# factory-orch-poll"
START_WAIT="${ORCH_POLL_START_WAIT:-15}"
STOP_WAIT="${ORCH_POLL_STOP_WAIT:-60}"
CRONTAB="${ORCH_CRONTAB:-crontab}"
PROC="${ORCH_PROC_DIR:-/proc}"
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

# Serialize start/stop/restart/ensure. Without flock(1) (macOS) this is a
# no-op and ensure's re-check of the stop marker is the only guard.
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

# #66: never start the poller on a silently empty database.
ensure_db() {
    local status=0
    mkdir -p "$STATE" "$(dirname "$DB")"
    [[ -e "$DB" ]] && return 0
    orch restore-db --db "$DB" >&2 || status=$?
    case "$status" in
        0) loud "RESTORED orch.db from backup" ;;
        3)
            orch init-db --db "$DB" >&2 || return 1
            loud "WARNING orch.db was missing and no backup exists; created an empty database (dedupe now relies on thread markers)"
            ;;
        *)
            loud "orch.db is missing and orch restore-db exited ${status}; not starting the poller"
            return 1
            ;;
    esac
}

# Start the poller unless it is running. Only `start` clears a stop.
launch() {
    local pid child i
    mkdir -p "$STATE"
    if pid="$(running_pid)"; then
        say "orch poll already running (pid ${pid})"
        return 0
    fi
    ensure_db || return 1
    # fd 9 is the service lock; the poller must not inherit it.
    nohup "$RUNNER" --pidfile "$PIDFILE" >>"$LOG" 2>&1 </dev/null 9>&- &
    child=$!
    for ((i = 0; i < START_WAIT * 10; i++)); do
        if pid="$(running_pid)"; then
            say "started orch poll (pid ${pid})"
            return 0
        fi
        if ! kill -0 "$child" 2>/dev/null; then
            say "orch poll exited at startup; see ${LOG}" >&2
            return 1
        fi
        sleep 0.1
    done
    say "orch poll did not write ${PIDFILE} within ${START_WAIT}s; see ${LOG}" >&2
    return 1
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
    # Re-check right before launching: a stop may have just landed.
    [[ -e "$STOPPED" ]] && return 0
    launch
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
    start) service_lock && start ;;
    stop) service_lock && stop ;;
    restart) service_lock && stop && start ;;
    status) status ;;
    ensure) service_lock && ensure ;;
    install-autostart) install_autostart ;;
    remove-autostart) remove_autostart ;;
    *)
        echo "usage: $(basename "$0") {start|stop|restart|status|ensure|install-autostart|remove-autostart}" >&2
        exit 2
        ;;
esac
