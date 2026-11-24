#!/usr/bin/env bash
# Move the dispatcher database, orch.db, to another host with exactly one
# writer at a time (README "Move the orch host").
#
#   orch-move.sh export --to <host> [--dir <export>]
#   orch-move.sh import <export>
#
# export runs on the host giving orch.db up. It refuses (exit 79) on a host
# already retired as the orch host (~/.config/factory/orch-retired). It
# writes waiter-<bot>.drain ("orch-move") for claude-bot, codex-bot and
# cursor-bot, so no waiter claims a job; every self-heal pass skips while
# they read orch-move. It first runs `self-heal.sh wait-idle` once, so a
# pass already under way finishes, and writes the drain files again before
# that wait lets a new pass start. Then every ORCH_MOVE_POLL_SEC seconds
# (default 30) it writes the drain files again,
# copies orch.db to <export>/probe.db with `orch backup-db` and asks
# `orch move-db drained` whether a job still runs or a device slot is held.
# Once none is, it writes orch-retired ("<host> <UTC time>"), and only then
# stops the poller, the three waiters, self-heal and the installed Linear
# listener through their service scripts, which refuse to start again while
# orch-retired exists (exit 79; for self-heal also the timer's `once` pass).
# After stopping self-heal it runs `self-heal.sh wait-idle`, which waits up
# to ORCH_HEAL_IDLE_WAIT seconds (default 900) for a pass already running,
# and the device deploy it started detached, to finish writing to orch.db;
# on timeout that counts as a failed stop. Where /proc exists,
# poller-service.sh stop also finds a poller whose pidfile is elsewhere (as
# orch-poll.service's is) by the poll lock it holds open. Export then exits
# 1, leaving the host retired and stopped, unless `orch move-db unlocked`
# can take orch.db.lock and every waiter-<bot>.lock beside orch.db. Then it
# copies orch.db to <export>/orch.db and the job-detail/ beside it to
# <export>/job-detail, checks that copy is still drained, writes
# <export>/manifest.json (`orch move-db manifest`, which records each
# job-detail file's sha256 and refuses a spilled job row whose file is
# missing), copies the checkout's
# machines.yml to <export>/machines.<this host>.yml and prints <export>.
# <export> defaults to ~/.local/share/factory/orch-move/<UTC time>, outside
# the state directory, and must not exist yet.
#
# If the drain takes longer than ORCH_MOVE_DRAIN_TIMEOUT seconds (default
# 9000), a probe or that first idle wait fails, or export is interrupted, it
# removes the drain files, their acks and the probe, stops nothing, and exits
# 75 (1 for a failed probe or idle wait, 130 on interrupt): the host keeps
# running, and self-heal resumes on its next pass by itself. A failure after
# orch-retired is written leaves the host retired and stopped and exits 1;
# to undo, remove orch-retired and start the services. The host name is
# ORCH_MOVE_HOST, else hostname(1).
#
# Both read orch.db's path from `run-poller.sh --print-db`, which answers on a
# retired host too: the ORCH_DB the poller gets from its env files, else
# ${ORCH_DB:-<state dir>/orch.db}. When --print-db fails (a missing env file
# exits 78) they refuse (exit 1) and change nothing.
#
# import runs on the host taking orch.db over. <export> is a local path or
# [user@]host:path. It refuses (exit 1) when orch.db, or a -wal or -journal
# beside it, already exists here. It copies <export> to a fresh
# ~/.local/share/factory/orch-move/import-<UTC time> (cp -R, or
# ${ORCH_MOVE_SCP:-scp} -r for host:path) and refuses unless
# `orch move-db check` finds the copy's sha256 matches the manifest, the copy
# passes integrity_check, the manifest names a retired line, every job-detail
# file it names is staged unchanged and no spilled job row lacks its file; for
# host:path, also unless `${ORCH_MOVE_SSH:-ssh} host test -e
# .config/factory/orch-retired` succeeds. A refusal places nothing and
# starts nothing. Then it copies this checkout's machines.yml beside the
# snapshot as machines.<this host>.yml, merges the staged job-detail/ into
# the one beside orch.db (refusing, placing nothing, when a file there of
# the same name differs), places orch.db through orch.db.tmp
# and mv (never `orch restore-db`, which holds queued jobs), removes this
# host's orch-retired, and starts the poller first, then the waiter of each
# bot with ~/.config/factory/waiter-autostart-<bot>, self-heal and the
# installed Linear listener, each through its service script.
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
    echo "       $(basename "$0") import <export>" >&2
    exit 2
}

refuse() {
    echo "orch-move: $*" >&2
    exit 1
}

db_path() {
    local db status=0
    db="$("${HERE}/run-poller.sh" --print-db)" || status=$?
    ((status == 0)) || refuse "cannot read the poller's database path (run-poller.sh --print-db exited ${status}); changed nothing"
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

linear() {
    if [[ -x "$LINEAR" ]]; then
        service "$LINEAR" "$1"
    else
        say "no Linear listener installed at ${LINEAR}; skipped its $1"
    fi
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

assert_final_copy_drained() {
    local counts
    if ! counts="$("$ORCH" move-db drained --db "${EXPORT}/orch.db")"; then
        echo "orch-move: the final copy is not drained (${counts}); a waiter restarted after the last probe claimed a job; wrote no manifest" >&2
        exit 1
    fi
}

export_db() {
    local to="" db line bot status=0 drains=()
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
    assert_drains
    for bot in "${BOTS[@]}"; do
        drains+=("$(drain_file "$bot")")
    done
    "${HERE}/self-heal.sh" wait-idle tee "${drains[@]}" <<<"$DRAIN_MARK" >/dev/null || status=$?
    if ((status != 0)); then
        echo "orch-move: self-heal.sh wait-idle exited ${status}; removed the drain files, stopped nothing" >&2
        exit 1
    fi
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
    service "${HERE}/self-heal.sh" wait-idle
    linear stop
    ((FAILED == 0)) || exit 1
    "$ORCH" move-db unlocked --db "$db" || refuse "a poller or waiter still holds its lock beside ${db} after the stops; wrote no export"

    "$ORCH" backup-db --db "$db" --backup "${EXPORT}/orch.db" >/dev/null
    if [[ -d "$(dirname "$db")/job-detail" ]]; then
        cp -R "$(dirname "$db")/job-detail" "${EXPORT}/job-detail"
    fi
    rm -f "${EXPORT}/probe.db"
    assert_final_copy_drained
    "$ORCH" move-db manifest --db "${EXPORT}/orch.db" --out "${EXPORT}/manifest.json" \
        --source-host "$HOST" --to "$to" --retired "$line" >/dev/null
    if [[ -f "$MACHINES" ]]; then
        cp "$MACHINES" "${EXPORT}/machines.${HOST}.yml"
    fi
    say "exported orch.db for ${to}"
    printf '%s\n' "$EXPORT"
}

merge_job_detail() {
    local from="$1" to="$2" file
    [[ -d "$from" ]] || return 0
    for file in "$from"/*.json; do
        [[ -e "$file" && -e "${to}/${file##*/}" ]] || continue
        cmp -s "$file" "${to}/${file##*/}" \
            || refuse "${to}/${file##*/} differs from the export's copy; placed nothing"
    done
    mkdir -p "$to"
    cp -R "${from}/." "${to}/"
}

import_db() {
    local source db file stage remote="" bot
    (($# == 1)) || usage
    source="$1"
    [[ "$source" =~ ^[^/]+: ]] && remote="${source%%:*}"
    db="$(db_path)"
    for file in "$db" "${db}-wal" "${db}-journal"; do
        [[ ! -e "$file" ]] || refuse "${file} exists; refusing to replace this host's database (move it aside first)"
    done
    stage="${MOVES}/import-$(date -u +%Y%m%dT%H%M%SZ)"
    [[ ! -e "$stage" ]] || refuse "${stage} exists"
    mkdir -p "$MOVES"
    if [[ -n "$remote" ]]; then
        "${ORCH_MOVE_SCP:-scp}" -r "$source" "$stage"
    else
        cp -R "$source" "$stage"
    fi
    "$ORCH" move-db check --db "${stage}/orch.db" --manifest "${stage}/manifest.json" \
        || refuse "the export staged in ${stage} failed its check; placed nothing"
    if [[ -n "$remote" ]] && ! "${ORCH_MOVE_SSH:-ssh}" "$remote" test -e .config/factory/orch-retired; then
        refuse "${remote} is not retired as the orch host (no ~/.config/factory/orch-retired there); placed nothing"
    fi
    if [[ -f "$MACHINES" ]]; then
        cp "$MACHINES" "${stage}/machines.${HOST}.yml"
    fi

    merge_job_detail "${stage}/job-detail" "$(dirname "$db")/job-detail"
    mkdir -p "$(dirname "$db")"
    cp "${stage}/orch.db" "${db}.tmp"
    mv "${db}.tmp" "$db"
    rm -f "$RETIRED"
    say "placed ${db} from ${stage}"

    service "${HERE}/poller-service.sh" start
    for bot in "${BOTS[@]}"; do
        [[ -e "${CONFIG}/waiter-autostart-${bot}" ]] || continue
        service "${HERE}/waiter-service.sh" "$bot" start
    done
    service "${HERE}/self-heal.sh" start
    linear start
    ((FAILED == 0)) || refuse "placed ${db}; start what failed above by hand"
}

case "${1:-}" in
    export) shift && export_db "$@" ;;
    import) shift && import_db "$@" ;;
    *) usage ;;
esac
