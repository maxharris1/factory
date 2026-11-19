# POSIX sh, because the bot profile that sources it may run under dash.
#
# A sandbox that mounts its own /proc shows a one-field NSpid, so only the
# host namespace recorded by deploy/install.sh catches it. macOS has no
# /proc/self/status.
factory_in_host_pidns() (
    proc="${ORCH_PROC_DIR:-/proc}"
    [ -r "${proc}/self/status" ] || exit 0
    nspid="$(sed -n 's/^NSpid:[[:space:]]*//p' "${proc}/self/status")"
    depth="$(awk '/^NSpid:/ { print NF - 1 }' "${proc}/self/status")"
    current="$(readlink "${proc}/self/ns/pid" 2>/dev/null)" || current=""
    file="${1:-${HOME}/.config/factory/host-pidns}"
    recorded=""
    if [ -r "$file" ]; then
        recorded="$(cat "$file")"
    fi
    if [ "${depth:-0}" -le 1 ] && { [ -z "$recorded" ] || [ "$recorded" = "$current" ]; }; then
        exit 0
    fi
    echo "not the host PID namespace (NSpid: ${nspid:-none}; pid namespace: ${current:-unknown}; recorded host: ${recorded:-none})" >&2
    exit 1
)

# A host or WSL restart leaves the record naming a dead namespace. Only the
# initial PID namespace, or one whose PID 1 is a system init (WSL's /init or
# systemd), is a host: a sandbox's PID 1 is the command its runtime started,
# and an unprivileged sandbox also has its own user namespace. Each retired
# namespace is appended to ${file}-retired, so a record naming it in any
# state directory is known dead.
factory_refresh_host_pidns() (
    proc="${ORCH_PROC_DIR:-/proc}"
    file="${1:-${HOME}/.config/factory/host-pidns}"
    state="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
    [ -z "${ORCH_JOB_PROOF:-}" ] && [ -z "${CODEX_SANDBOX:-}" ] || exit 1
    [ -r "${proc}/self/status" ] || exit 1
    depth="$(awk '/^NSpid:/ { print NF - 1 }' "${proc}/self/status")"
    [ "${depth:-0}" -le 1 ] || exit 1
    [ "$(awk '{ print $1, $2, $3 }' "${proc}/self/uid_map" 2>/dev/null)" = "0 0 4294967295" ] || exit 1
    recorded="$(cat "$file" 2>/dev/null)" || exit 1
    current="$(readlink "${proc}/self/ns/pid" 2>/dev/null)" || exit 1
    [ -n "$recorded" ] && [ -n "$current" ] && [ "$recorded" != "$current" ] || exit 1
    if [ "$current" != "pid:[4026531836]" ]; then
        init="$(tr '\000' '\n' <"${proc}/1/cmdline" 2>/dev/null | head -n 1)"
        case "${init##*/}" in
            init | systemd) ;;
            *) exit 1 ;;
        esac
    fi
    for pidfile in "$state"/*.pid; do
        [ -r "$pidfile" ] || continue
        pid="$(awk 'NR == 1 { print $1 }' "$pidfile")"
        case "$pid" in '' | *[!0-9]*) continue ;; esac
        if [ "$(readlink "${proc}/${pid}/ns/pid" 2>/dev/null)" = "$recorded" ]; then
            echo "kept ${file}: pid ${pid} in ${pidfile} still runs in ${recorded}" >&2
            exit 2
        fi
    done
    printf '%s\n' "$recorded" >>"${file}-retired" || exit 1
    printf '%s\n' "$current" >"$file" || exit 1
    echo "refreshed ${file}: pid namespace ${recorded} -> ${current}" >&2
)

factory_host_pidns_retired() {
    grep -qxF -- "$1" "${2:-${HOME}/.config/factory/host-pidns}-retired" 2>/dev/null
}

factory_may_autostart() {
    [ -z "${ORCH_JOB_PROOF:-}" ] && [ -z "${CODEX_SANDBOX:-}" ] && factory_in_host_pidns "$@"
}

factory_host_pidns_or_refuse() {
    factory_refresh_host_pidns || true
    factory_in_host_pidns && return 0
    echo "refusing to $1 from a non-host PID namespace" >&2
    exit 4
}

# A background child of a non-interactive shell does not lead its group, so
# setsid and perl exec in place and $! stays the service pid.
factory_detach() {
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$@" &
    elif command -v perl >/dev/null 2>&1; then
        nohup perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV or exit 127' "$@" &
    else
        nohup "$@" &
        return 0
    fi
    factory_detach_tries=0
    while [ "$factory_detach_tries" -lt 500 ]; do
        factory_detach_pgid="$(ps -o pgid= -p "$!" 2>/dev/null | tr -d ' ')"
        [ -n "$factory_detach_pgid" ] && [ "$factory_detach_pgid" != "$!" ] || return 0
        factory_detach_tries=$((factory_detach_tries + 1))
        sleep 0.01
    done
    echo "factory_detach: pid $! is still in the caller's process group" >&2
    return 1
}
