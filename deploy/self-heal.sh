#!/usr/bin/env bash
# Self-heal (issue #107): every two minutes, restart factory components that
# are down or run stale code, with no LLM and no tokens.
#
#   self-heal.sh once              one pass: `orch heal deploy`, then
#                                  `orch health --json | orch heal`, then
#                                  `orch heal devices` detached (#297)
#   self-heal.sh kick              start one pass now, detached, and return
#                                  (the poller runs it after a merge, #219)
#   self-heal.sh start             start the loop unless running; clears a stop
#   self-heal.sh stop              stop the loop and hold it stopped
#   self-heal.sh restart           stop, then start
#   self-heal.sh status            exit 0 if the loop runs, 3 if not
#   self-heal.sh ensure            start unless running or held stopped; prints
#                                  nothing when running (what autostart runs)
#   self-heal.sh install-autostart run `once` on a timer (see below)
#   self-heal.sh remove-autostart  turn that off
#
# `once` holds self-heal.lock, so passes never overlap; a pass that finds the
# lock held exits 0 at once. It reads ~/.config/factory/webhook.env into the
# environment when this user can read it (for the escalation POST; nothing
# from it is printed), runs `orch heal deploy --state <state dir>` (#219:
# the checkout owner fast-forwards the checkout to origin/main, keeping
# local edits to machines.yml, orch.yml and deploy/install.sh; see
# lib/self_deploy.py; a failure is logged and never blocks healing), then
# pipes `orch health --json` into
# `orch heal --health-json - --state <state dir>`, which decides and runs each
# restart through the component's own service script and writes
# self-heal.log. Self-heal itself never sends a signal, so it never stops a
# job's `orch job run`. Exits 0 and 3 from health are normal; anything else
# but 1 (an unreadable health file, still reported) fails the pass. Health
# runs after the deploy step in a new process, so a component still on the
# old commit shows stale code and is restarted in the same pass.
#
# Then (#297) `once` starts `orch heal devices --state <state dir>` detached
# in its own session (lib/device_deploy.py: each enabled remote device's
# factory checkout moves to this checkout's HEAD once the device has no job;
# it holds its own device-deploy.lock, so overlapping passes skip). The box
# pass never waits for it. When `orch heal deploy` moved this checkout's
# HEAD, `once` also starts `orch dashboard push --once` detached, so the
# dashboard files (the PC's factory-dashboard) are rendered by the new code
# at once rather than at the poller's next push.
#
# `kick` starts factory-self-heal.service with --no-block where the systemd
# user timer is installed, and otherwise a detached `once` in its own
# session; the lock keeps it from overlapping a timer pass.
#
# Where no systemd user timer runs the token broker (#171), each pass first
# runs deploy/app-token-service.sh once when ~/.config/factory/
# app-token-autostart exists.
#
# Files, in ~/.local/state/factory (ORCH_STATE_DIR): self-heal.lock (one
# pass at a time), self-heal.pid (the loop), self-heal.stopped (the stop
# hold), self-heal-service.lock, and self-heal-service.log (this script's
# lines and health's stderr). The loop runs `once` every ORCH_HEAL_SECONDS
# (default 120).
#
# Autostart: where `systemctl --user` works (ORCH_SYSTEMCTL names the
# command), install-autostart writes deploy/factory-self-heal.service and
# .timer, with this checkout's path, to ~/.config/systemd/user and enables
# the timer (every 2 minutes, from 1 minute after boot). Otherwise it writes
# ~/.config/factory/self-heal-autostart, which makes the profile written by
# deploy/install.sh run `ensure` whenever a bot shell starts, and, when
# `crontab` exists (ORCH_CRONTAB), an @reboot line and a 5-minute `ensure`
# watchdog tagged "# factory-self-heal", replaced and never duplicated. If
# the crontab cannot be read (anything but "no crontab for <user>"),
# install-autostart and remove-autostart change nothing and exit 1.
set -euo pipefail

COMMAND="${1:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"

# shellcheck source=deploy/host-pidns.sh
. "${HERE}/host-pidns.sh"

CHECKOUT="$(dirname "$HERE")"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
PIDFILE="${STATE}/self-heal.pid"
RUN_LOCK="${STATE}/self-heal.lock"
LOG="${STATE}/self-heal-service.log"
SERVICE_LOCK="${STATE}/self-heal-service.lock"
STOPPED="${STATE}/self-heal.stopped"
AUTOSTART="${CONFIG}/self-heal-autostart"
WEBHOOK_ENV="${CONFIG}/webhook.env"
ORCH="${ORCH_BIN:-${HOME}/.local/bin/orch}"
MARKER="# factory-self-heal"
INTERVAL="${ORCH_HEAL_SECONDS:-120}"
START_WAIT="${ORCH_HEAL_START_WAIT:-15}"
STOP_WAIT="${ORCH_HEAL_STOP_WAIT:-60}"
CRONTAB="${ORCH_CRONTAB:-crontab}"
SYSTEMCTL="${ORCH_SYSTEMCTL:-systemctl}"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
TIMER="factory-self-heal.timer"
PROC="${ORCH_PROC_DIR:-/proc}"
# cron, systemd and fresh shells have a minimal PATH; orch needs gh and git.
export PATH="${HOME}/.local/bin${PATH:+:${PATH}}:/usr/local/bin:/usr/bin:/bin"

say() { printf '%s self-heal: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

usage() {
    echo "usage: $(basename "$0") {once|kick|start|stop|restart|status|ensure|install-autostart|remove-autostart}" >&2
    exit 2
}

# Is this argv the loop (`bash .../self-heal.sh _loop`)?
is_loop_argv() {
    local i
    for i in 0 1; do
        if (($# > i + 1)) && [[ "${@:i+1:1}" =~ (^|/)self-heal\.sh$ && "${@:i+2:1}" == _loop ]]; then
            return 0
        fi
    done
    return 1
}

# Print the pid of the running loop, or fail.
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
    else
        command="$(ps -p "$pid" -o command= 2>/dev/null)" || return 1
        read -r -a argv <<<"$command"
    fi
    is_loop_argv "${argv[@]}" || return 1
    printf '%s\n' "$pid"
}

service_lock() {
    command -v flock >/dev/null 2>&1 || return 0
    mkdir -p "$STATE"
    exec 9>>"$SERVICE_LOCK"
    flock 9
}

detached() {
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$@" >>"$LOG" 2>&1 </dev/null 8>&- 9>&- &
    else
        nohup "$@" >>"$LOG" 2>&1 </dev/null 8>&- 9>&- &
    fi
}

head_sha() {
    git -C "$CHECKOUT" rev-parse -q --verify HEAD 2>/dev/null || true
}

# One pass. Without flock(1) passes are not serialized.
once() {
    local statuses before after
    mkdir -p "$STATE"
    if command -v flock >/dev/null 2>&1; then
        exec 8>>"$RUN_LOCK"
        flock -n 8 || return 0
    fi
    if [[ -r "$WEBHOOK_ENV" ]]; then
        set -a
        # shellcheck disable=SC1090
        . "$WEBHOOK_ENV" >/dev/null 2>&1
        set +a
    fi
    export ORCH_DB="${ORCH_DB:-${STATE}/orch.db}"
    export PYTHONUNBUFFERED=1
    if [[ -f "${CONFIG}/app-token-autostart" && -x "${HERE}/app-token-service.sh" ]]; then
        "${HERE}/app-token-service.sh" once 8>&- || say "token broker pass exited $?" >>"$LOG"
    fi
    set +e
    before="$(head_sha)"
    "$ORCH" heal deploy --state "$STATE" >>"$LOG" 2>&1 8>&- </dev/null ||
        say "orch heal deploy exited $?; healing anyway" >>"$LOG"
    after="$(head_sha)"
    "$ORCH" health --json 2>>"$LOG" 8>&- | "$ORCH" heal --health-json - --state "$STATE" 8>&-
    statuses=("${PIPESTATUS[@]}")
    detached "$ORCH" heal devices --state "$STATE"
    if [[ -n "$after" && "$before" != "$after" ]]; then
        detached "$ORCH" dashboard push --once
    fi
    set -e
    case "${statuses[0]}" in
        0 | 1 | 3) ;;
        *)
            say "orch health exited ${statuses[0]}; nothing healed" >>"$LOG"
            return 1
            ;;
    esac
    return "${statuses[1]}"
}

# Start one pass now without waiting for it.
kick() {
    mkdir -p "$STATE"
    if have_systemd_user && [[ -f "${UNIT_DIR}/factory-self-heal.service" ]]; then
        if "$SYSTEMCTL" --user start --no-block factory-self-heal.service >>"$LOG" 2>&1; then
            say "kick: started factory-self-heal.service" >>"$LOG"
            return 0
        fi
    fi
    local detach=()
    command -v setsid >/dev/null 2>&1 && detach=(setsid)
    "${detach[@]}" nohup bash "$SELF" once >>"$LOG" 2>&1 </dev/null 8>&- 9>&- &
    say "kick: started a detached pass (pid $!)" >>"$LOG"
}

# The loop. It owns the pidfile; SIGTERM stops it between passes.
run_loop() {
    local stopping="" pause=""
    trap 'stopping=1; [[ -n "$pause" ]] && kill -TERM "$pause" 2>/dev/null || true' TERM INT HUP
    mkdir -p "$STATE"
    printf '%s\n' "$$" >"$PIDFILE"
    while [[ -z "$stopping" && ! -e "$STOPPED" ]]; do
        bash "$SELF" once 9>&- || say "pass exited $?"
        [[ -z "$stopping" && ! -e "$STOPPED" ]] || break
        sleep "$INTERVAL" &
        pause=$!
        wait "$pause" || true
        pause=""
    done
    say "loop stopped"
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
        say "self-heal loop already running (pid ${pid})"
        return 0
    fi
    nohup bash "$SELF" _loop >>"$LOG" 2>&1 </dev/null 9>&- &
    child=$!
    for ((i = 0; i < START_WAIT * 10; i++)); do
        if pid="$(running_pid)"; then
            say "started self-heal loop (pid ${pid})"
            return 0
        fi
        if ! kill -0 "$child" 2>/dev/null; then
            say "self-heal loop exited at startup; see ${LOG}" >&2
            return 1
        fi
        sleep 0.1
    done
    say "self-heal loop did not write ${PIDFILE} within ${START_WAIT}s; see ${LOG}" >&2
    return 1
}

stop() {
    local pid i
    mkdir -p "$STATE"
    : >"$STOPPED"
    if ! pid="$(running_pid)"; then
        say "self-heal loop is not running"
        return 0
    fi
    kill -TERM "$pid"
    for ((i = 0; i < STOP_WAIT * 10; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            say "stopped self-heal loop (pid ${pid})"
            return 0
        fi
        sleep 0.1
    done
    say "self-heal loop (pid ${pid}) did not stop within ${STOP_WAIT}s" >&2
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
        echo "self-heal: refusing to write ${1} into a crontab" >&2
        return 1
    }
}

crontab_without_marker() {
    local current errors status=0
    errors="$(mktemp "${TMPDIR:-/tmp}/self-heal.XXXXXX")" || return 1
    current="$("$CRONTAB" -l 2>"$errors")" || status=$?
    if ((status != 0)); then
        if grep -q "no crontab for" "$errors"; then
            current=""
        else
            echo "self-heal: cannot read the crontab (${CRONTAB} -l exited ${status}); changed nothing:" >&2
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

have_systemd_user() {
    command -v "$SYSTEMCTL" >/dev/null 2>&1 && "$SYSTEMCTL" --user show-environment >/dev/null 2>&1
}

write_crontab() {
    local kept="$1"
    if [[ -n "$kept" ]]; then
        printf '%s\n' "$kept" | "$CRONTAB" -
    else
        "$CRONTAB" - </dev/null
    fi
}

install_autostart() {
    local kept="" unit job="${SELF} ensure >>${LOG} 2>&1 ${MARKER}"
    if have_crontab; then
        plain_path "$SELF"
        plain_path "$LOG"
        kept="$(crontab_without_marker)" || exit 1
    fi
    mkdir -p "$CONFIG" "$STATE"
    if have_systemd_user; then
        mkdir -p "$UNIT_DIR"
        for unit in factory-self-heal.service factory-self-heal.timer; do
            sed "s|%h/factory/|${CHECKOUT}/|g" "${HERE}/${unit}" >"${UNIT_DIR}/${unit}"
        done
        "$SYSTEMCTL" --user daemon-reload
        "$SYSTEMCTL" --user enable --now "$TIMER"
        rm -f "$AUTOSTART"
        echo "self-heal autostart on: systemd user timer ${TIMER} runs '${SELF} once' every 2 minutes"
        if have_crontab; then
            write_crontab "$kept"
            echo "removed crontab lines tagged ${MARKER}"
        fi
        return 0
    fi
    : >"$AUTOSTART"
    echo "self-heal autostart on: bot shells run '${SELF} ensure' (${AUTOSTART})"
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
    if have_systemd_user; then
        "$SYSTEMCTL" --user disable --now "$TIMER" >/dev/null 2>&1 || true
        echo "disabled ${TIMER}"
    fi
    rm -f "$AUTOSTART"
    echo "self-heal autostart off"
    if have_crontab; then
        write_crontab "$kept"
        echo "removed crontab lines tagged ${MARKER}"
    fi
}

case "$COMMAND" in
    once) once ;;
    kick) kick ;;
    _loop) factory_host_pidns_or_refuse _loop && run_loop ;;
    start) factory_host_pidns_or_refuse start && service_lock && start ;;
    stop) factory_host_pidns_or_refuse stop && service_lock && stop ;;
    restart) factory_host_pidns_or_refuse restart && service_lock && stop && start ;;
    status) status ;;
    ensure) factory_host_pidns_or_refuse ensure && service_lock && ensure ;;
    install-autostart) install_autostart ;;
    remove-autostart) remove_autostart ;;
    *) usage ;;
esac
