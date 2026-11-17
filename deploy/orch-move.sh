#!/usr/bin/env bash
# Move the dispatcher database, orch.db, to another host with exactly one
# writer at a time (README "Move the orch host").
#
#   orch-move.sh export --to <host> [--dir <export>]
#
# export runs on the host giving orch.db up. It refuses (exit 79) on a host
# already retired as the orch host (~/.config/factory/orch-retired). It
# writes waiter-<bot>.drain ("orch-move") for claude-bot, codex-bot and
# cursor-bot, so no waiter claims a job, and every ORCH_MOVE_POLL_SEC seconds
# (default 30) writes back any drain file self-heal removed or replaced,
# copies orch.db to <export>/probe.db with `orch backup-db` and asks
# `orch move-db drained` whether a job still runs or a device slot is held.
# Once none is, it writes orch-retired ("<host> <UTC time>"), and only then
# stops the poller, the three waiters, self-heal and the installed Linear
# listener through their service scripts, which refuse to start again while
# orch-retired exists. Then it copies orch.db to <export>/orch.db, checks
# that copy is still drained, writes <export>/manifest.json
# (`orch move-db manifest`), copies the checkout's machines.yml to
# <export>/machines.<this host>.yml and prints <export>. <export> defaults
# to ~/.local/share/factory/orch-move/<UTC time>, outside the state
# directory, and must not exist yet.
#
# If the drain takes longer than ORCH_MOVE_DRAIN_TIMEOUT seconds (default
# 9000), or a probe fails, export removes the drain files, their acks and
# the probe, stops nothing, and exits 75 (1 for a failed probe): the host
# keeps running. A failure after orch-retired is written leaves the host
# retired and stopped and exits 1; to undo, remove orch-retired and start
# the services. The host name is ORCH_MOVE_HOST, else hostname(1).
# Usage errors exit 2.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
RETIRED="${CONFIG}/orch-retired"
MOVES="${HOME}/.local/share/factory/orch-move"
ORCH="${HOME}/.local/bin/orch"
LINEAR="${HOME}/.local/share/factory/linear-webhook-service.sh"
MACHINES="${HERE}/../machines.yml"
BOTS=(claude-bot codex-bot cursor-bot)
DRAIN_MARK=orch-move
POLL="${ORCH_MOVE_POLL_SEC:-30}"
DRAIN_TIMEOUT="${ORCH_MOVE_DRAIN_TIMEOUT:-9000}"
HOST="${ORCH_MOVE_HOST:-$(hostname)}"
FAILED=0

say() { printf '%s orch-move: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

usage() {
    echo "usage: $(basename "$0") export --to <host> [--dir <export>]" >&2
    exit 2
}

# The ORCH_DB the poller would get; run-poller.sh refuses on a retired host
# or without its env files, so fall back as poller-service.sh does.
db_path() {
    local db
    db="$("${HERE}/run-poller.sh" --print-db 2>/dev/null)" || db=""
    printf '%s\n' "${db:-${ORCH_DB:-${STATE}/orch.db}}"
}

drain_file() {
    printf '%s/waiter-%s.drain\n' "$STATE" "$1"
}

assert_drains() {
    local bot file
    for bot in "${BOTS[@]}"; do
        file="$(drain_file "$bot")"
        [[ "$(cat "$file" 2>/dev/null)" == "$DRAIN_MARK" ]] || printf '%s\n' "$DRAIN_MARK" >"$file"
    done
}

release_drains() {
    local bot file
    for bot in "${BOTS[@]}"; do
        file="$(drain_file "$bot")"
        rm -f "$file" "${file}.ack"
    done
    rm -f "${EXPORT}/probe.db"
    rmdir "$EXPORT" 2>/dev/null || true
}

undo_hint() {
    (($? == 0)) && return 0
    echo "orch-move: failed after retiring this host; it stays retired, and what stopped stays stopped. To undo: rm ${RETIRED}, then start the services (README \"Move the orch host\")." >&2
}

service() {
    local status=0
    "$@" || status=$?
    ((status == 0)) || {
        echo "orch-move: ${*} exited ${status}" >&2
        FAILED=1
    }
}

wait_for_drain() {
    local db="$1" deadline counts status
    deadline=$((SECONDS + DRAIN_TIMEOUT))
    while :; do
        assert_drains
        "$ORCH" backup-db --db "$db" --backup "${EXPORT}/probe.db" >/dev/null
        status=0
        counts="$("$ORCH" move-db drained --db "${EXPORT}/probe.db")" || status=$?
        ((status == 0)) && return 0
        ((status == 1)) || exit 1
        say "waiting for the drain: ${counts}"
        if ((SECONDS >= deadline)); then
            say "not drained after ${DRAIN_TIMEOUT}s; removed the drain files, stopped nothing"
            exit 75
        fi
        sleep "$POLL"
    done
}

export_db() {
    local to="" db line bot counts
    EXPORT=""
    while (($#)); do
        case "$1" in
            --to) (($# >= 2)) || usage; to="$2"; shift 2 ;;
            --dir) (($# >= 2)) || usage; EXPORT="$2"; shift 2 ;;
            *) usage ;;
        esac
    done
    [[ -n "$to" ]] || usage
    if [[ -e "$RETIRED" ]]; then
        echo "orch-move: this host is already retired as the orch host (${RETIRED}: $(head -n 1 "$RETIRED" 2>/dev/null || true))" >&2
        exit 79
    fi
    db="$(db_path)"
    EXPORT="${EXPORT:-${MOVES}/$(date -u +%Y%m%dT%H%M%SZ)}"
    mkdir -p "$(dirname "$EXPORT")" "$STATE"
    mkdir "$EXPORT"

    trap release_drains EXIT
    trap 'exit 130' INT TERM
    wait_for_drain "$db"

    line="${to} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    mkdir -p "$CONFIG"
    printf '%s\n' "$line" >"$RETIRED"
    trap undo_hint EXIT
    say "retired this host as the orch host (${line})"

    service "${HERE}/poller-service.sh" stop
    for bot in "${BOTS[@]}"; do
        service "${HERE}/waiter-service.sh" "$bot" stop
    done
    service "${HERE}/self-heal.sh" stop
    if [[ -x "$LINEAR" ]]; then
        service "$LINEAR" stop
    else
        say "no Linear listener installed at ${LINEAR}; nothing to stop"
    fi
    ((FAILED == 0)) || exit 1

    "$ORCH" backup-db --db "$db" --backup "${EXPORT}/orch.db" >/dev/null
    rm -f "${EXPORT}/probe.db"
    # A waiter self-heal restarted after the last probe may have claimed a
    # job before orch-retired held it off.
    if ! counts="$("$ORCH" move-db drained --db "${EXPORT}/orch.db")"; then
        echo "orch-move: the final copy is not drained (${counts}); wrote no manifest" >&2
        exit 1
    fi
    "$ORCH" move-db manifest --db "${EXPORT}/orch.db" --out "${EXPORT}/manifest.json" \
        --source-host "$HOST" --to "$to" --retired "$line" >/dev/null
    if [[ -f "$MACHINES" ]]; then
        cp "$MACHINES" "${EXPORT}/machines.${HOST}.yml"
    fi
    say "exported orch.db for ${to}"
    printf '%s\n' "$EXPORT"
}

case "${1:-}" in
    export) shift && export_db "$@" ;;
    *) usage ;;
esac
