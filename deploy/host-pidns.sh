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
    [ "${depth:-0}" -gt 1 ] || hint="; a stale record refreshes only from the boot record ${ORCH_HOST_PIDNS_ATTESTATION:-/run/factory-host-pidns} that deploy/wsl-boot.sh sets up (see README)"
    echo "not the host PID namespace (NSpid: ${nspid:-none}; pid namespace: ${current:-unknown}; recorded host: ${recorded:-none})${hint}" >&2
    exit 1
)

factory_refresh_host_pidns() (
    [ -z "${ORCH_JOB_PROOF:-}" ] && [ -z "${CODEX_SANDBOX:-}" ] || exit 1
    proc="${ORCH_PROC_DIR:-/proc}"
    [ -r "${proc}/self/status" ] || exit 0
    depth="$(awk '/^NSpid:/ { print NF - 1 }' "${proc}/self/status")"
    [ "${depth:-0}" -le 1 ] || exit 1
    file="${1:-${HOME}/.config/factory/host-pidns}"
    recorded="$(cat "$file" 2>/dev/null)" || recorded=""
    current="$(readlink "${proc}/self/ns/pid" 2>/dev/null)" || current=""
    [ -n "$recorded" ] && [ "$recorded" != "$current" ] || exit 0
    holders="$(readlink "${proc}"/[0-9]*/ns/pid 2>/dev/null)" || :
    case "
${holders}
" in *"
${recorded}
"*) exit 4 ;; esac
    att="${ORCH_HOST_PIDNS_ATTESTATION:-/run/factory-host-pidns}"
    [ -n "$(find "$att" -prune -type f -user "${ORCH_HOST_PIDNS_ATTESTATION_UID:-0}" ! -perm -g+w ! -perm -o+w 2>/dev/null)" ] || exit 1
    attested="$(tr -d '\r' <"$att" | awk 'NR == 1 && /^pid:\[[0-9]+\]$/ { v = $0 } END { if (NR == 1) print v }')"
    [ -n "$attested" ] && [ "$attested" = "$current" ] || exit 1
    tmp="${file}.tmp.$$"
    printf '%s\n' "$current" >"$tmp" && mv -f "$tmp" "$file" || { rm -f "$tmp"; exit 1; }
)

factory_may_autostart() {
    [ -z "${ORCH_JOB_PROOF:-}" ] && [ -z "${CODEX_SANDBOX:-}" ] || return 1
    factory_refresh_host_pidns "$@" || :
    factory_in_host_pidns "$@"
}

factory_host_pidns_or_refuse() {
    case "$1" in
        start | ensure | restart) factory_refresh_host_pidns 2>/dev/null || : ;;
    esac
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
