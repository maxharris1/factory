#!/usr/bin/env bash
# Run the dashboard sign-in broker (`orch accounts signin-broker`) as a
# service on a host without a systemd user session.
#
#   signin-broker-service.sh start             start unless running; clears a stop
#   signin-broker-service.sh stop              SIGTERM the broker and hold it stopped
#   signin-broker-service.sh restart           stop, then start
#   signin-broker-service.sh status            exit 0 if running, 3 if not
#   signin-broker-service.sh ensure            start unless running or held stopped
#                                              (what autostart runs; never clears a stop)
#   signin-broker-service.sh install-autostart enable autostart (see below)
#   signin-broker-service.sh remove-autostart  disable it
#
# start reports the broker started once it has run ORCH_SIGNIN_BROKER_SETTLE
# seconds (default 2), so a broker that exits at startup fails start.
# The broker writes to signin-broker.log beside the pidfile.
# On a host retired as the orch host (~/.config/factory/orch-retired),
# start, restart and ensure do nothing and exit 79; stop and status still work.
#
# Autostart: install-autostart writes ~/.config/factory/signin-broker-autostart,
# which makes the profile written by deploy/install.sh run `ensure` in the
# background whenever a bot shell starts, and makes self-heal keep the broker
# running.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"

# shellcheck source=deploy/host-pidns.sh
. "${HERE}/host-pidns.sh"

STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
PIDFILE="${STATE}/signin-broker.pid"
LOG="${STATE}/signin-broker.log"
RUN_LOCK="${STATE}/signin-broker.lock"
SERVICE_LOCK="${STATE}/signin-broker-service.lock"
STOPPED="${STATE}/signin-broker.stopped"
AUTOSTART="${CONFIG}/signin-broker-autostart"
ORCH="${HOME}/.local/bin/orch"
CONTAIN="${HERE}/contain.sh"
START_WAIT="${ORCH_SIGNIN_BROKER_START_WAIT:-15}"
STOP_WAIT="${ORCH_SIGNIN_BROKER_STOP_WAIT:-60}"
SETTLE="${ORCH_SIGNIN_BROKER_SETTLE:-2}"
PROC="${ORCH_PROC_DIR:-/proc}"
export PATH="${HOME}/.local/bin${PATH:+:${PATH}}:/usr/local/bin:/usr/bin:/bin"

say() { printf '%s signin-broker-service: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# orch is argv[0], or argv[1] when an interpreter runs it.
is_this_states_broker_argv() {
    local i j
    local -a argv=("$@")
    for i in 0 1; do
        [[ "${argv[i]:-}" =~ (^|/)orch$ && "${argv[i + 1]:-}" == accounts &&
            "${argv[i + 2]:-}" == signin-broker ]] || continue
        for ((j = i + 3; j + 1 < $#; j++)); do
            [[ "${argv[j]}" == --state && "${argv[j + 1]}" == "$STATE" ]] && return 0
        done
    done
    return 1
}

is_broker_pid() {
    local pid="$1" command
    local -a argv=()
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -d "$PROC" ]]; then
        mapfile -d '' -t argv 2>/dev/null <"${PROC}/${pid}/cmdline" || return 1
    else
        command="$(ps -ww -p "$pid" -o command= 2>/dev/null)" || return 1
        read -r -a argv <<<"$command"
    fi
    is_this_states_broker_argv "${argv[@]}" || return 1
    printf '%s\n' "$pid"
}

pidfile_pid() {
    is_broker_pid "$(head -n 1 "$PIDFILE" 2>/dev/null || true)"
}

lock_holder_pid() {
    local lock dir fd
    [[ -d "$PROC" ]] || return 1
    lock="$(readlink -f "$RUN_LOCK" 2>/dev/null)" || return 1
    [[ -e "$lock" ]] || return 1
    for dir in "$PROC"/[1-9]*; do
        is_broker_pid "${dir##*/}" >/dev/null || continue
        for fd in "${dir}"/fd/*; do
            [[ "$(readlink "$fd" 2>/dev/null)" == "$lock" ]] || continue
            printf '%s\n' "${dir##*/}"
            return 0
        done
    done
    return 1
}

running_pid() {
    pidfile_pid || lock_holder_pid
}

lock_held() {
    local status=0
    command -v flock >/dev/null 2>&1 || return 1
    [[ -e "$RUN_LOCK" ]] || return 1
    flock -n -E 75 "$RUN_LOCK" true 9>&- || status=$?
    ((status == 75))
}

write_pidfile() {
    printf '%s\n' "$1" >"${PIDFILE}.$$"
    mv -f "${PIDFILE}.$$" "$PIDFILE"
}

retired() {
    [[ -e "${CONFIG}/orch-retired" ]]
}

refuse_if_retired() {
    local retired="${CONFIG}/orch-retired"
    retired || return 0
    echo "signin-broker-service: refusing to $1 on a host retired as the orch host (${retired}: $(head -n 1 "$retired" 2>/dev/null || true))" >&2
    exit 79
}

service_lock() {
    command -v flock >/dev/null 2>&1 || return 0
    mkdir -p "$STATE"
    exec 9>>"$SERVICE_LOCK"
    flock 9
}

launch() {
    local pid child i
    mkdir -p "$STATE"
    if pid="$(running_pid)"; then
        say "sign-in broker already running (pid ${pid})"
        return 0
    fi
    if lock_held; then
        say "sign-in broker already running (${RUN_LOCK} held)"
        return 0
    fi
    refuse_if_retired start
    if command -v flock >/dev/null 2>&1; then
        factory_detach "$CONTAIN" signin-broker \
            bash -c 'exec 8>>"$1"; flock -n 8 || exit 75; shift; exec "$@"' _ "$RUN_LOCK" \
            "$ORCH" accounts signin-broker --state "$STATE" >>"$LOG" 2>&1 </dev/null 9>&-
    else
        factory_detach "$CONTAIN" signin-broker \
            "$ORCH" accounts signin-broker --state "$STATE" >>"$LOG" 2>&1 </dev/null 9>&-
    fi
    child=$!
    write_pidfile "$child"
    for ((i = 0; i < START_WAIT * 10; i++)); do
        if pid="$(running_pid)"; then
            if ((pid != child)); then
                write_pidfile "$pid"
                say "sign-in broker already running (pid ${pid})"
                return 0
            fi
            if ((i >= SETTLE * 10)); then
                say "started sign-in broker (pid ${pid})"
                return 0
            fi
        elif ! kill -0 "$child" 2>/dev/null; then
            rm -f "$PIDFILE"
            say "sign-in broker exited at startup; see ${LOG}" >&2
            return 1
        fi
        sleep 0.1
    done
    say "sign-in broker (pid ${child}) did not start within ${START_WAIT}s; see ${LOG}" >&2
    return 1
}

start() {
    mkdir -p "$STATE"
    rm -f "$STOPPED"
    launch
}

stop() {
    local pid i
    mkdir -p "$STATE"
    : >"$STOPPED"
    if ! pid="$(running_pid)"; then
        rm -f "$PIDFILE"
        say "sign-in broker is not running"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null || true
    for ((i = 0; i < STOP_WAIT * 10; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PIDFILE"
            say "stopped sign-in broker (pid ${pid})"
            return 0
        fi
        sleep 0.1
    done
    say "sign-in broker (pid ${pid}) did not stop within ${STOP_WAIT}s" >&2
    return 1
}

restart() {
    local operator_hold=""
    [[ -e "$STOPPED" ]] && operator_hold=1
    stop && start && return 0
    [[ -n "$operator_hold" ]] || rm -f "$STOPPED"
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
    [[ -e "$STOPPED" ]] || launch
}

install_autostart() {
    mkdir -p "$CONFIG"
    : >"$AUTOSTART"
    echo "sign-in broker autostart on (${AUTOSTART}): bot shells run '${SELF} ensure' and self-heal keeps it running"
}

remove_autostart() {
    rm -f "$AUTOSTART"
    echo "sign-in broker autostart off"
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
