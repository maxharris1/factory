#!/bin/sh
# contain.sh <stem> <command>...: exec the command at the OOM floor in a
# systemd user scope of its own, <stem>-<epoch>-<pid>.scope. With no systemd
# user scope it logs "<stem>: uncontained" and runs the command anyway.
# Every step execs, so the command keeps this pid.
stem=$1
shift
[ "$(uname -s)" = Linux ] || exec "$@"

probe_secs=3
oom="$(dirname "$0")/../lib/oom.py"
if command -v python3 >/dev/null 2>&1 && [ -r "$oom" ]; then
    python3 -I -S "$oom" floor $$ </dev/null || :
else
    echo "${stem}: oom_score_adj left as is (no python3 or ${oom})" >&2
fi

if u=$(id -u 2>/dev/null) &&
    [ "$(timeout -k 1 "$probe_secs" loginctl show-user "$u" -p Linger --value 2>/dev/null)" = yes ] &&
    x=${XDG_RUNTIME_DIR:-/run/user/$u} &&
    noexpand=--expand-environment=no &&
    if help=$(systemd-run --help 2>/dev/null); then
        case $help in *--expand-environment*) ;; *) noexpand= ;; esac
    fi &&
    XDG_RUNTIME_DIR="$x" timeout -k 1 "$probe_secs" \
        systemd-run --user --scope --quiet --collect ${noexpand:+"$noexpand"} \
        grep -q '^0::/.*[.]scope$' /proc/self/cgroup </dev/null >/dev/null 2>&1; then
    export XDG_RUNTIME_DIR="$x"
    exec systemd-run --user --scope --quiet --collect ${noexpand:+"$noexpand"} \
        --unit="${stem}-$(date +%s)-$$.scope" -- "$@"
fi
echo "${stem}: uncontained (no systemd user scope)" >&2
exec "$@"
