#!/usr/bin/env bash
# Run `orch poll` as a service on a host without a systemd user session.
#
#   poller-service.sh start             start unless already running; clears a stop
#   poller-service.sh stop              SIGTERM the poller and hold it stopped
#   poller-service.sh restart           stop, then start
#   poller-service.sh status            exit 0 if running, 3 if not
#   poller-service.sh ensure            start unless running or held stopped
#                                       (what autostart runs)
#   poller-service.sh install-autostart enable autostart (see below)
#   poller-service.sh remove-autostart  disable it
#
# `orch poll` owns the pidfile and the single-poller lock itself; this script
# only reads the pidfile. Output goes to poller.log beside the pidfile.
#
# Autostart: install-autostart writes ~/.config/factory/poller-autostart, which
# makes the profile written by deploy/install.sh run `ensure` in the
# background whenever a bot shell starts. When `crontab` exists it also
# installs an @reboot line and a 5-minute `ensure` watchdog, tagged
# "# factory-orch-poll" and replaced, never duplicated, on every run.
# ORCH_CRONTAB names the crontab command (default: crontab on PATH).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
PIDFILE="${STATE}/poller.pid"
LOG="${ORCH_POLL_LOG:-${STATE}/poller.log}"
SERVICE_LOG="${STATE}/poller-service.log"
STOPPED="${STATE}/poller.stopped"
AUTOSTART="${CONFIG}/poller-autostart"
RUNNER="${HERE}/run-poller.sh"
MARKER="# factory-orch-poll"
START_WAIT="${ORCH_POLL_START_WAIT:-15}"
STOP_WAIT="${ORCH_POLL_STOP_WAIT:-60}"
CRONTAB="${ORCH_CRONTAB:-crontab}"
# cron and fresh shells have a minimal PATH; the poller needs orch and gh.
# The caller's PATH keeps precedence; the standard directories only follow it.
export PATH="${HOME}/.local/bin${PATH:+:${PATH}}:/usr/local/bin:/usr/bin:/bin"

say() { printf '%s poller-service: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Print the pid of the running poller, or fail. A pidfile naming a dead
# process, or a live process that is not `orch poll`, does not count.
running_pid() {
    local pid cmdline
    [[ -r "$PIDFILE" ]] || return 1
    pid="$(head -n 1 "$PIDFILE" 2>/dev/null || true)"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -r "/proc/${pid}/cmdline" ]]; then
        cmdline="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
        [[ "$cmdline" == *"orch poll"* ]] || return 1
    fi
    printf '%s\n' "$pid"
}

start() {
    local pid child i
    mkdir -p "$STATE"
    rm -f "$STOPPED"
    if pid="$(running_pid)"; then
        say "orch poll already running (pid ${pid})"
        return 0
    fi
    nohup "$RUNNER" --pidfile "$PIDFILE" >>"$LOG" 2>&1 </dev/null &
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
    start
}

# cron runs lines through /bin/sh and treats % specially, so only plain
# paths are written into the crontab.
plain_path() {
    [[ "$1" =~ ^/[A-Za-z0-9/._-]+$ ]] || {
        echo "poller-service: refusing to write ${1} into a crontab" >&2
        return 1
    }
}

crontab_without_marker() {
    local current
    current="$("$CRONTAB" -l 2>/dev/null || true)"
    [[ -n "$current" ]] || return 0
    grep -vF -- "$MARKER" <<<"$current" || true
}

install_autostart() {
    mkdir -p "$CONFIG" "$STATE"
    : >"$AUTOSTART"
    echo "poller autostart on: bot shells run '${SELF} ensure' (${AUTOSTART})"
    if ! command -v "$CRONTAB" >/dev/null 2>&1; then
        echo "crontab not found; autostart uses the bot-shell profile hook only"
        return 0
    fi
    plain_path "$SELF"
    plain_path "$SERVICE_LOG"
    local job="${SELF} ensure >>${SERVICE_LOG} 2>&1 ${MARKER}"
    {
        crontab_without_marker
        printf '@reboot %s\n' "$job"
        printf '*/5 * * * * %s\n' "$job"
    } | "$CRONTAB" -
    echo "installed crontab @reboot and 5-minute ensure lines"
}

remove_autostart() {
    rm -f "$AUTOSTART"
    echo "poller autostart off"
    if command -v "$CRONTAB" >/dev/null 2>&1; then
        crontab_without_marker | "$CRONTAB" -
        echo "removed crontab lines tagged ${MARKER}"
    fi
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop && start ;;
    status) status ;;
    ensure) ensure ;;
    install-autostart) install_autostart ;;
    remove-autostart) remove_autostart ;;
    *)
        echo "usage: $(basename "$0") {start|stop|restart|status|ensure|install-autostart|remove-autostart}" >&2
        exit 2
        ;;
esac
