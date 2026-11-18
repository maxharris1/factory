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
    hint=""
    [ "${depth:-0}" -gt 1 ] || hint="; if the record is stale, delete ${file} and rerun deploy/install.sh --update from a host shell"
    echo "not the host PID namespace (NSpid: ${nspid:-none}; pid namespace: ${current:-unknown}; recorded host: ${recorded:-none})${hint}" >&2
    exit 1
)

factory_may_autostart() {
    [ -z "${ORCH_JOB_PROOF:-}" ] && [ -z "${CODEX_SANDBOX:-}" ] && factory_in_host_pidns "$@"
}

factory_host_pidns_or_refuse() {
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
