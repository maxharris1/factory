"""Remote job start over the box's SSH aliases (docs/SPEC.md F2-02).

A job the device pool placed on a remote device runs there as that
harness's own bot user. The box reaches the device through the Host alias
named by the device's `ssh_alias` in machines.yml, with `{bot}` resolved to
the bot user (`pc-wsl-{bot}` -> `pc-wsl-codex-bot`). The alias, its host
name and its key live only in the box user's own SSH client config; this
module never names, reads or passes a key, a user@ prefix or a config file.
`-l <bot user>` is always passed as well, so even a mistyped alias cannot
log a job in as another bot.

Each remote start gets a fresh random tag (`ORCH_WORKER_TAG`, kept on the
box in the slot row). The start runs under `exec env ORCH_WORKER_TAG=<tag>`,
so the login shell, the harness and every process they start carry it in
their environment; the tag, not a process number, is what names the job's
worker. The remote POSIX shell scripts sent as the SSH command:

* start: refuse (exit 125, nothing run) unless the device has Linux /proc
  and a grep that reads NUL-separated environments; make
  `<workdir>/jobs/<job-id>`, record `<process group> <boot id>` in
  `.orch-worker.pid` there, and exec the harness command with the job's
  environment under the bot's login shell (`bash -lc`, #94), so its own
  PATH (gh, claude, codex) is loaded. The job's environment and directory
  are applied inside that shell after the profile ran, so a profile cannot
  change them (#99 A6); just before the exec into the harness, once it is
  found on the login PATH, the start writes its tag to the launch record
  `<workdir>/jobs/<job-id>.launched` (#99 A2). Every step execs, so the
  pid stays the start's.
  sshd runs the command in a new session, so that process group is the
  job's. The record is never removed: deleting bookkeeping is not
  evidence the processes are gone. #817: when the bot's user manager
  lingers (`loginctl ... -p Linger` says yes) and starts a process in a
  scope of its own on the unified cgroup hierarchy, the start writes the
  scope record `.orch-worker-<tag>.scope` naming `orch-run-<tag>.scope`
  and execs the login shell under `systemd-run --user --scope` in that
  unit, with environment expansion off, so the run is contained: every
  process it starts stays in the scope, whatever its session or
  environment. Otherwise it writes UNCONTAINED to the run log and runs
  as above, uncontained. Before either,
  when it leads its session, the start leaves the keeper (KEEPER, #827)
  there and waits until the keeper ignores TERM, HUP and INT: tagged, and
  there until it is the session's last process, so the session holds a
  tagged member while any process of the start is in it;
* preflight (#94, `orch job run --worktree`): under `bash -lc`, exit 0 when
  git, gh and the harness binary are all found, else name the missing ones;
* checkout (#94): under `bash -lc`, clone (git init + fetch, so a job
  directory already holding a worker record still takes it) or reuse the
  job's checkout in `<workdir>/jobs/<job-id>` at the job's head, and copy
  the brief from stdin to `<workdir>/jobs/<job-id>.brief.md`. Job values
  are passed as quoted positional arguments, never spliced into the script.
  The checkout and the start first claim the job for `orch cleanup`
  (#953, `_CLAIM`);
* launched: exit 0 when the launch record holds this start's tag (it
  reached the harness), 1 when it does not, else unsure;
* check: exit 1 while any process of the run lives (wherever its group or
  session now is): one of this user's that carries the job's tag, one the
  tree record `.orch-worker-<tag>.tree.pid` from this boot names with the
  start time it has now, one in the run's scope (#817: the last component
  of its `0::` cgroup line is the unit the scope record names, and that
  is `orch-run-<tag>.scope`), one in the start's session while that
  session still holds one of those (its record from this boot names it;
  only for an uncontained run, one with no scope record), or a child of
  any of them (#722). The check rewrites
  the tree record with them (the boot id, then `<pid> <start time>` each,
  zombies left out), so a descendant that dropped the tag and left the
  session stays the run's after its parent exits; a number alone never
  names one, so a reused pid never joins. In a contained run such a
  descendant is the run's even when no check saw it before its parent
  exited, and a reused session number never names a process outside it.
  Each rewrite holds `flock` on the record's lock file, so a check never
  replaces a newer record with an older view; a device without `flock`,
  or a lock not taken in 10 seconds, is 3.
  The run's deadline (`ORCH_WORKER_DEADLINE`) is never the run's, nor its
  children. With none of them, exit 3 ("cannot tell") while the recorded
  group, from this boot, still has a live process (one that dropped the
  tag, or an unrelated group that reused the number), and exit 0 (gone)
  when it has none, the record is from an earlier boot, or no record was
  ever written. A malformed record (empty, not digits, a leading zero, 0 or 1)
  is 3. ssh itself exits 255 when the device cannot be reached;
* stop: send SIGTERM to every process of the run, as check finds and
  records them, and to each process group that holds a tagged one (a group
  with a tagged process is in the start's own session, or was made by one
  of its processes); in a contained run, every process in its scope. The
  stop that kills first sends each SIGSTOP, finding
  them again until no new one appears, so none forks a process that
  escapes, then SIGKILL. A stored number is never signalled on its own;
* probe: `exit 0`, the health check for a device whose slots need no
  worker check this poll.

check and stop run as `exec sh -c '<script>'`, so they are POSIX sh whatever
the bot's login shell is (bash on Linux, zsh on macOS).

#128: the start is not sent as the SSH command any more. The launch call
(`launch_script`) starts it detached under setsid, with a wrapper that
keeps its files in `<workdir>/jobs/<job-id>.run/<tag>/`, and the box follows
it with `poll_script` (lib/remote_follow.py). check and stop are unchanged:
they find the detached start by its tag or record as before; the stop of
a cancelled, exited or gone run escalates to SIGKILL. #679: the box's check
(`check_worker`) first makes the tag's run directory itself, so a launch
that reaches the device after it starts nothing (`fenced_check_script`).

On a device whose `uname -s` is Darwin (macOS: no /proc, and no way to read
another process's environment) the same contract holds through the record,
one file per start, `<workdir>/jobs/<job-id>/.orch-worker-<tag>.pid`:

* start: refuse (125) unless `sysctl -n kern.bootsessionuuid` gives the boot
  session id and the start leads its own process group (`ps -o pgid=`);
  create the record with noclobber (O_EXCL) holding
  `<process group> <boot session> <tag> <leader start time>` (the leader's
  `ps -o lstart=` in the C locale and UTC), refusing if it already exists,
  and exec as above. Before the record it starts the keeper in its group
  and writes the keeper's pid and start time as the first snapshot (below):
  the keeper ignores SIGTERM and stays until it is alone in the group, so
  the group is never empty, nor its number reused, while any process of
  the start lives (Codex PR #129 r3);
* check: with no record the start may still be on its way, so the check
  creates the record itself as the fence `fenced` (O_EXCL as well: the start
  or the check creates it, never both) and exits 3; the fenced start can
  never record itself and refuses, so a later check reads the fence as 0.
  0 too for a record from an earlier boot session or no process left in the
  recorded group, read from a `ps -A -o pgid=,pid=,uid=` that exited 0; 1
  while the group holds only this user's processes and is led by the
  recorded leader (same start time), or (Codex PR #129 r2) its leader has
  exited but it still holds a process from the snapshot with that
  process's recorded start time; 3 for a malformed record or one naming
  another tag, a group holding another user's process, a group whose leader
  has exited and holds no snapshot process (nothing ties the processes left
  to this start: the number may have been reused while the group was
  empty), a leader whose start time differs (the number was reused), a
  fence that could not be written, and when there is no boot session id or
  ps fails or lists nothing;
* stop: SIGTERM (SIGKILL when asked to kill) to the recorded group exactly
  when the check would exit 1; nothing else is signalled (with no record it
  writes the fence as well). While the leader is verified, it first writes
  the snapshot `.orch-worker-<tag>.members.pid` (`<pid> <start time>` per
  member of the group, from one listing, kept only when the leader is
  verified again after it), so a cancel that kills the leader but not a
  child still finds and kills the child. The keeper, in the snapshot from
  the start on, ties the group to the start after its leader exits, so a
  child born later (one the harness forks as it shuts down included) is
  signalled as well. A process that left the job's group (setsid) is never
  signalled here: macOS gives no way to find it by its tag;
* poll (#128): the check without the fence. The wrapper writes `pid` before
  its start writes the record, so no record is only unsure: a poll never
  fences the start it follows.

Every ssh call uses the `ssh` found on PATH with BatchMode (never a
password prompt), a connect timeout and server keepalives. `SshTransport`
sends each call through one ControlMaster per alias and bot user (#222,
lib/ssh_mux.py): a stale master is rebuilt inside the call, a device that
cannot be reached still ends 255, and `ORCH_SSH_MUX=off` turns reuse off.
Tests replace it, or put a fake `ssh` first on PATH; nothing here contacts
a real host on its own.
"""

from __future__ import annotations

import posixpath
import re
import secrets
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol, runtime_checkable

from lib.github_app_token import TOKEN_VARIABLES
from lib.handoff_marker import MARKER_LINE_LIMIT, MARKER_PREFIX, MARKER_SEARCH_LINES, RUNNER_LOG_PREFIX
from lib.harness_env import CPU_CAP
from lib.oom import raise_sh
from lib.ssh_mux import Multiplexer

SSH_BINARY = "ssh"
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=60",
    "-o",
    "ServerAliveCountMax=5",
)
PROBE_TIMEOUT_SECONDS = 60.0
CHECKOUT_TIMEOUT_SECONDS = 900.0
PID_FILE = ".orch-worker.pid"
# #94: every command orch runs for a job on a device (the preflight, the
# checkout and the harness) runs under the bot's login shell.
LOGIN_SHELL = "bash"
LOGIN_NAME = "orch-job"
PREFLIGHT_TOOLS = ("git", "gh")
_PREFLIGHT_MARK = "orch-preflight: missing"
_CHECKOUT_MARK = "orch-checkout:"
TAG_ENV = "ORCH_WORKER_TAG"
DEADLINE_ENV = "ORCH_WORKER_DEADLINE"
# #100 r5: with telemetry on, the start reports the harness's own
# environment (after the login profile) as one line just before the exec:
# WORKER_ENV_MARK, a space, then hex of NUL-separated NAME=value entries
# ending with WORKER_ENV_END. lib/telemetry.py scrubs that environment's
# secret values from a remote run's summary and never stores the line.
# The login profile's stdout goes to stderr (#100 r6), so the report is the
# first thing on stdout whatever the profile prints.
WORKER_ENV_MARK = "ORCH-WORKER-ENV"
WORKER_ENV_END = "ORCH-WORKER-ENV-END"
START_REFUSED = 125
# The harness's own exit status says nothing about whether it ran (#99 A2).
# The start writes its worker tag to the launch record (`launch_record`)
# just before the exec into the harness, once the harness is found under
# the login PATH; a start with no record of its tag never ran the harness.
# A login profile's tracing (set -x or -v, a DEBUG trap) would echo the
# job's and the worker's environment values to stderr, so the start turns it
# off first.
_UNTRACE = "set +xvT; trap - DEBUG; "


def _report_env(env_file: str | None, never_read: Sequence[str] = ()) -> str:
    """The report on stdout, or (#128) written to env_file, mode 600, whole or not at all,
    with only its line break left on stdout, so a profile's unended output stays apart."""
    entry = 'printf "%s=%s\\0" "$n" "${!n}"; '
    if never_read:
        entry = f"case $n in {'|'.join(never_read)}) ;; *) {entry}esac; "
    report = (
        f'printf "%s " {WORKER_ENV_MARK}; '
        f"names=$(compgen -e) && {{ for n in $names; do {entry}done; "
        f'printf "%s\\0" {WORKER_ENV_END}; }} | od -An -v -tx1 | tr -d " \\n"; '
        "echo; "
    )
    if env_file is None:
        return report
    f = shlex.quote(env_file)
    part = shlex.quote(f"{env_file}.tmp")
    return (
        f"(umask 077 && {{ {report}}} > {part}) 2>/dev/null "
        f"&& mv -f -- {part} {f} || rm -f -- {part}; echo; "
    )


# Factory, Nov 13: a harness's `--add-dir` directories are given only where
# the device has them. The arguments start with k (where the kept
# `--add-dir <dir>` pairs go in the command), n, then n pairs of (path to
# test, directory as given), then the command and the job's environment.
# Each path is tested with `test -d` on the device at start time, after the
# profile ran. Paths and directories are positional arguments, never
# spliced into the script text.
_GUARD_DIRS = (
    'k=$1; n=$2; shift 2; a=(); '
    'while [ "$n" -gt 0 ]; do if test -d "$1"; then a+=(--add-dir "$2"); fi; '
    'shift 2; n=$((n-1)); done; '
    'c=("$@"); set -- "${c[@]:0:k}" "${a[@]}" "${c[@]:k}"; '
)
SSH_UNREACHABLE = 255

GONE = "gone"
RUNNING = "running"
UNKNOWN = "unknown"
UNREACHABLE = "unreachable"

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TAG = re.compile(r"^[0-9a-f]{32}$")
CGROUP_ROOT = "/sys/fs/cgroup"
WRAPPER_KILL_ROUNDS = 30

# Shell fragments shared by the scripts. NUL-separated environments are read
# with `grep -z`; a device whose grep cannot do that cannot check a worker.
_CAN_SCAN = "printf 'x\\000' | grep -qzxF x"
_TAGGED = f'grep -lzxsF -e "{TAG_ENV}=$t" /proc/[0-9]*/environ'
_BAD_GROUP = '""|0*|1|*[!0-9]*'

# #722: from lines `B <boot id>` (this boot), `T <pid>` (carries the tag),
# `X <pid>` (the run's deadline), `K ...` (the tree record: its boot id,
# then `<pid> <start time>` per process), `S <pid> <boot id> <tag>` (the
# start's record), `R` (#817: the run is contained in its scope), `C <pid>`
# (in the run's scope) and `P <pid> <ppid> <start time> <session>` (every
# live process), prints the tree record anew when the run has a member: a
# tagged process, a record entry from this boot whose pid has that start
# time now, a process in the run's scope, a process in the start's session
# (only for a run with no scope), or a process whose parent is a member.
# A session number is not reused while a process is in it, but a record
# outlives the session, so its number alone never names the run's session.
# The deadline is never a member, so neither are the checks and stops it runs.
_TREE_AWK = (
    '$1 == "B" { b = $2 } $1 == "K" && NF == 2 { r = $2 } '
    '$1 == "T" { t[$2] = 1 } $1 == "X" { x[$2] = 1 } '
    '$1 == "R" { sc = 1 } $1 == "C" { c[$2] = 1 } '
    '$1 == "S" && NF == 4 { q = $2; qb = $3 } '
    '$1 == "K" && NF == 3 { k[$2] = $3 } '
    '$1 == "P" && NF == 5 { u[$2] = $3; s[$2] = $4; g[$2] = $5 } '
    'END { if (r == "" || r != b) split("", k); '
    "for (p in u) if (!(p in x) && ((p in t) || (p in c) || ((p in k) && k[p] == s[p]))) m[p] = 1; "
    'if (sc || qb != b || q !~ /^[1-9][0-9]*$/ || q == 1 || ((q in u) && !(q in m))) q = ""; '
    'if (q != "") { v = 0; for (p in m) if (g[p] == q) v = 1; if (!v) q = "" } '
    'if (q != "") for (p in u) if (!(p in x) && g[p] == q) m[p] = 1; '
    "do { n = 0; for (p in u) if (!(p in m) && !(p in x) && (u[p] in m)) { m[p] = 1; n = 1 } } "
    "while (n); for (p in m) n++; if (n) { print b; for (p in m) print p, s[p] } }"
)


def _members(directory: str) -> str:
    """Define `members`: rewrite the tree record of tag $t, then print its members' pids.

    The read, scan and rewrite hold `flock` on the record's lock file, so a
    writer that read the record earlier never replaces a newer one (#827).
    The kernel drops the lock when its holder dies. `members` fails, printing
    nothing, when the lock cannot be taken in 10 seconds.

    The listing is its own function, so no `)` of its case pattern sits
    inside a command substitution: macOS's sh (bash 3.2) closes the
    substitution there and cannot parse the script (#722 f-4).
    """
    record = f"{directory}/{tree_record('$t')}"
    start = f"{directory}/{start_record('$t')}"
    scope = f"{directory}/{scope_record('$t')}"
    return (
        f"k={record}; listed() {{ "
        'b=; { read -r b < /proc/sys/kernel/random/boot_id; } 2>/dev/null; echo "B $b"; '
        'for e in $g; do e=${e#/proc/}; echo "T ${e%/environ}"; done; '
        'for e in $x; do e=${e#/proc/}; echo "X ${e%/environ}"; done; '
        "sed 's/^/K /' \"$k\" 2>/dev/null; "
        f"sed 's/^/S /' {start} 2>/dev/null; "
        f'c=; {{ read -r c < {scope}; }} 2>/dev/null; '
        'case "$c" in "orch-run-$t.scope") echo R;; *) c=;; esac; '
        "for e in /proc/[0-9]*; do "
        '{ read -r s < "$e/stat"; } 2>/dev/null || continue; '
        "set -- ${s##*) }; "
        'case "$1" in Z|X) continue;; esac; '
        'echo "P ${e#/proc/} $2 ${20} $4"; '
        '[ -z "$c" ] || { while read -r l; do case "$l" in 0::*) '
        '[ "${l##*/}" != "$c" ] || echo "C ${e#/proc/}"; break;; esac; '
        'done < "$e/cgroup"; } 2>/dev/null; '
        "done; }; tracked() { "
        f"g=$({_TAGGED}); "
        f'x=$(grep -lzxsF -e "{DEADLINE_ENV}=$t" /proc/[0-9]*/environ); '
        f"o=$(listed | awk {shlex.quote(_TREE_AWK)}); "
        'if [ -n "$o" ] || [ -e "$k" ]; then '
        '{ { [ -z "$o" ] || printf \'%s\\n\' "$o"; } > "$k.$$" '
        '&& mv -f -- "$k.$$" "$k"; } 2>/dev/null || rm -f -- "$k.$$"; fi; '
        "[ -z \"$o\" ] || printf '%s\\n' \"$o\" | sed -n 's/ .*//p'; }; "
        'members() { if [ -d "${k%/*}" ]; then '
        '{ flock -w 10 9 && tracked; } 9>>"$k.lock"; else tracked; fi; }; '
    )


# macOS (Darwin) has no /proc and no way to read another process's
# environment, so there the worker is named by its record instead: the
# start's process group, the boot session, the tag and the group leader's
# start time. `uname -s` picks the path on the device; anything that is not
# Darwin takes the Linux path, which refuses a device without /proc itself.
_IS_DARWIN = '[ "$(uname -s 2>/dev/null)" = Darwin ]'
# A boot session id is a UUID: 8-4-4-4-12 hex digits. A case pattern for one.
_HEX = "[0-9A-Fa-f]"
_UUID = "-".join(_HEX * n for n in (8, 4, 4, 4, 12))
# The boot session id in $c, only when sysctl succeeds and prints a UUID.
_DARWIN_BOOT = (
    "c=$(sysctl -n kern.bootsessionuuid 2>/dev/null) && "
    f'case "$c" in {_UUID}) ;; *) false;; esac'
)
# A process's start time, one space between words, in a fixed locale and zone.
_LSTART = 'x=$(LC_ALL=C TZ=UTC0 ps -o lstart= -p "$p" 2>/dev/null); set -f; set -- $x; set +f'
# The process listing in $o, only when ps exits 0 and lists something.
_DARWIN_LIST = 'o=$(ps -A -o pgid= -o pid= -o uid= 2>/dev/null) && [ -n "$o" ]'
# Prints `<found> <foreign> <leader>` for group $p from the listing in $o:
# whether any process is in the group, whether one belongs to another user,
# and whether the leader (pid $p) is among them. awk exits 1, printing
# nothing, unless it read exactly $w lines (the shell's own count of the
# listing's fields, divided by 3), each three numbers: so a pipe that lost
# part of the listing cannot pass for a whole one. (macOS's awk cannot take
# the listing as a -v value: it rejects a newline there.)
_DARWIN_GROUP = (
    'printf \'%s\\n\' "$o" | awk -v p="$p" -v u="$u" -v w="$w" '
    "'NF != 3 || $1 !~ /^[0-9]+$/ || $2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ { e = 1; exit } "
    "$1 == p { n = 1; if ($3 != u) x = 1; if ($2 == p) k = 1 } "
    "END { if (e || NR != w) exit 1; print n + 0, x + 0, k + 0 }'"
)
# The stop's snapshot of the recorded group, in $m: one `<pid> <start time>`
# line per member, from one listing taken while the leader is verified
# alive, kept only when the leader is verified again after it (so the group
# was the start's for the whole listing).
_DARWIN_SNAPSHOT = (
    'LC_ALL=C TZ=UTC0 ps -A -o pgid= -o pid= -o lstart= 2>/dev/null | awk -v p="$p" '
    "'$1 == p && $2 ~ /^[0-9]+$/ { l = $2; for (i = 3; i <= NF; i++) l = l \" \" $i; print l }' "
    '> "$m.$$" 2>/dev/null; '
    f'{_LSTART}; if [ "$*" = "$s" ]; then mv -f -- "$m.$$" "$m"; else rm -f -- "$m.$$"; fi'
)
# Whether a member of the snapshot in $m is still in group $p with its
# recorded start time: then the group has not been empty since the snapshot,
# so its number was never reused and every process in it is the start's.
# Sets v=1 when one is.
_DARWIN_MEMBER = (
    'v=; if [ -f "$m" ]; then while read -r q a; do '
    'case "$q" in ""|*[!0-9]*) continue;; esac; '
    'y=$(LC_ALL=C TZ=UTC0 ps -o pgid= -o lstart= -p "$q" 2>/dev/null); '
    'set -f; set -- $y; set +f; '
    '[ "$#" -gt 1 ] && [ "$1" = "$p" ] || continue; shift; '
    '[ "$*" = "$a" ] && { v=1; break; }; '
    'done < "$m"; fi'
)
# Codex PR #129 r3: the keeper, which the start leaves in its own group ($1)
# so that the group is never empty while any process of the start lives: its
# number cannot be reused, and the keeper (a member of the snapshot) proves it
# to a check after the leader has exited. It ignores SIGTERM, SIGINT and
# SIGHUP (SIGKILL ends it with the group) and exits once a listing it is on
# shows no other member of the group but its own children. The listing goes
# to the file $2, so ps is the keeper's own child, never a grandchild. The
# start forks it from a subshell that exits at once, so it is not the
# harness's child: a harness that waits for all its children never waits
# on it. It prints its pid once it ignores those signals, then closes its
# output, so the start reads the pid only from a keeper a TERM can no longer
# end (#827 f-6).
_DARWIN_KEEPER = (
    'trap "" TERM INT HUP; echo "$$"; exec > /dev/null 2>&1; '
    "while sleep 1; do "
    'ps -A -o pgid= -o pid= -o ppid= > "$2" 2>/dev/null || continue; '
    '[ "$(awk -v p="$1" -v k="$$" '
    "'$2 == k { s = 1 } $1 == p && $2 != k && $3 != k { n = 1 } END { print s + 0, n + 0 }' "
    '"$2")" = "1 0" ] && break; '
    'done; rm -f -- "$2"'
)
# #827: the Linux counterpart of _DARWIN_KEEPER, for the start's session. Run
# it as `sh -c "$KEEPER" orch-keeper` with the run's tag. It ignores TERM,
# HUP and INT and exits once two scans in a row, 0.2 s apart, find no other
# live process in its session, so the session holds a tagged process for as
# long as anything else is in it: a session number proves the run only while
# a proven member is in it. One scan is not enough: a process listed in it
# can fork and exit before the scan reads it, and its child, born after the
# listing, is missed. Only builtins run during a scan, so no child of its own
# is listed. It prints its pid once it ignores those signals, then closes its
# output: whoever starts it reads that line before the worker runs, so a TERM
# sent once the worker runs never finds the keeper unprotected (#827 f-6).
KEEPER = (
    'trap "" TERM HUP INT; echo "$$"; exec > /dev/null 2>&1; '
    'read -r s < /proc/self/stat || exit 0; set -- ${s##*) }; q=$4; z=; '
    "while :; do n=; for e in /proc/[0-9]*; do "
    '[ "${e#/proc/}" = "$$" ] && continue; '
    '{ read -r s < "$e/stat"; } 2>/dev/null || continue; '
    "set -- ${s##*) }; "
    'case "$1" in Z|X) continue;; esac; '
    '[ "$4" = "$q" ] && { n=1; break; }; '
    'done; if [ -n "$n" ]; then z=; elif [ -n "$z" ]; then exit 0; else z=1; fi; '
    "sleep 0.2 || sleep 1; done"
)
# What a check or stop writes as the record of a start that has written none:
# the start's own record write (noclobber, so O_EXCL) then fails, and it
# refuses (125) without running anything.
_FENCE = "fenced"


def start_record(tag: str) -> str:
    """The start's record file name in the job directory: one per start."""
    return f".orch-worker-{tag}.pid"


def tree_record(tag: str) -> str:
    """Linux: the run's tree record in the job directory, `<pid> <start time>` per process."""
    return f".orch-worker-{tag}.tree.pid"


UNCONTAINED = "orch: the run is uncontained (no systemd scope)"


def scope_record(tag: str) -> str:
    """Linux: names the systemd user scope the run is contained in, when it is (#817)."""
    return f".orch-worker-{tag}.scope"


def _darwin_worker(
    job_dir: str,
    *,
    gone: str,
    unsure: str,
    running: str,
    blind: str,
    fence: bool = True,
    snapshot: bool = False,
) -> str:
    """Darwin: decide whether the record of tag $t names a live worker.

    A start for tag $t creates its record `$d/.orch-worker-$t.pid` with
    noclobber (O_EXCL) before it runs anything. With no record the start may
    still be on its way, so the check writes the fence record in its place
    (also O_EXCL: exactly one of the two creates the file) and is unsure this
    time; the start then cannot record itself and refuses. Without fence (a
    poll following that start, #128) no record is only unsure: nothing is
    written, so the start it follows still records itself.

    gone: the fence (that start can never run), a record from an earlier
    boot session, or no process left in the recorded group, all read from a
    ps that exited 0. unsure: no record yet (fence just written), a fence
    that could not be written, a malformed record or one naming another tag,
    a group holding another user's process, a group whose leader has exited
    and that holds no snapshot member (nothing ties the members left to
    this start: the number may have been reused while the group was empty),
    or a leader whose start time is not the recorded one, or a record whose
    boot session is not a UUID.
    running: the recorded group holds only this user's processes and is
    either led by the recorded leader or (#129 r2) still holds a member of
    the snapshot `$d/.orch-worker-$t.members.pid` (the start's keeper, then
    each stop's listing) with its recorded start time. With snapshot (a
    stop), a group led by its leader is snapshotted before running. blind:
    the device cannot tell (no boot session id, a user id that is not a
    number, ps failed or listed nothing, a listing line that is not three
    numbers, awk read less than the whole listing, failed, or printed
    anything but three fields of 0 or 1).
    """
    directory = shlex.quote(job_dir)
    empty_group_is_gone = (
        f"{_DARWIN_LIST} || {blind}; "
        'set -f; set -- $o; set +f; w=$(($# / 3)); '
        f"r=$({_DARWIN_GROUP}) || {blind}; "
        "set -f; set -- $r; set +f; "
        f'case "$#:$*" in "3:"[01]" "[01]" "[01]) ;; *) {blind};; esac; '
        f'[ "$1" = 1 ] || {gone}; '
    )
    no_record = (
        'if [ ! -e "$f" ]; then '
        'mkdir -p -- "$d" 2>/dev/null; '
        f'if (set -C; echo {_FENCE} > "$f") 2>/dev/null; then {unsure}; fi; '
        f'[ -e "$f" ] || {unsure}; '
        "fi; "
        if fence
        else f'[ -e "$f" ] || {unsure}; '
    )
    list_group = (
        f"{_DARWIN_LIST} || {blind}; "
        'set -f; set -- $o; set +f; w=$(($# / 3)); '
        f"r=$({_DARWIN_GROUP}) || {blind}; "
        "set -f; set -- $r; set +f; "
        f'case "$#:$*" in "3:"[01]" "[01]" "[01]) ;; *) {blind};; esac; '
        f'[ "$1" = 1 ] || {gone}; '
    )
    relist_or_unsure = f"{{ {list_group}{unsure}; }}"
    return (
        f"d={directory}; f=\"$d/.orch-worker-$t.pid\"; "
        'm="$d/.orch-worker-$t.members.pid"; '
        f"{_DARWIN_BOOT} || {blind}; "
        f"u=$(id -u) || {blind}; "
        f'case "$u" in ""|*[!0-9]*) {blind};; esac; '
        f"{no_record}"
        f'{{ read -r p b g s < "$f"; }} 2>/dev/null || {unsure}; '
        f'if [ "$p" = {_FENCE} ]; then {gone}; fi; '
        f'case "$p" in {_BAD_GROUP}) {unsure};; esac; '
        f'[ -n "$b" ] && [ -n "$g" ] && [ -n "$s" ] || {unsure}; '
        f'[ "$g" = "$t" ] || {unsure}; '
        f'case "$b" in {_UUID}) ;; *) {unsure};; esac; '
        f'[ "$b" = "$c" ] || {gone}; '
        f"{list_group}"
        f'[ "$2" = 0 ] || {unsure}; '
        f'if [ "$3" = 1 ]; then '
        f'{_LSTART}; [ "$*" = "$s" ] || {relist_or_unsure}; '
        f"{_DARWIN_SNAPSHOT + '; ' if snapshot else ''}"
        f"else {_DARWIN_MEMBER}; [ -n \"$v\" ] || {relist_or_unsure}; fi; "
        f"{running}"
    )


def _sh(script: str) -> str:
    """Run script under POSIX sh whatever the login shell (macOS bots use zsh)."""
    return f"exec sh -c {shlex.quote(script)}"


def new_tag() -> str:
    """A fresh worker tag for one remote start."""
    return secrets.token_hex(16)


class RemoteError(ValueError):
    """A remote target cannot be built. The message is one line."""


@dataclass(frozen=True)
class RemoteTarget:
    """Where one job's worker runs: device, alias, bot user and directory."""

    device: str
    alias: str
    user: str
    workdir_root: str
    job_id: str
    tag: str | None = None

    def __post_init__(self) -> None:
        if not _JOB_ID.fullmatch(self.job_id):
            raise RemoteError(f"job id {self.job_id!r} is not a plain token")
        if self.tag is not None and not _TAG.fullmatch(self.tag):
            raise RemoteError("worker tag is not 32 lowercase hex digits")
        if not self.alias or self.alias.startswith("-") or "@" in self.alias:
            raise RemoteError(f"ssh alias {self.alias!r} is not a bare Host alias")
        if not self.user or self.user.startswith("-"):
            raise RemoteError(f"bot user {self.user!r} is not a user name")
        if not self.workdir_root.startswith("/"):
            raise RemoteError("workdir_root must be absolute")

    @property
    def job_dir(self) -> str:
        return f"{self.workdir_root.rstrip('/')}/jobs/{self.job_id}"

    def require_tag(self) -> str:
        if self.tag is None:
            raise RemoteError(f"job {self.job_id} has no worker tag")
        return self.tag


def ssh_argv(target: RemoteTarget, script: str) -> list[str]:
    """`ssh <options> -l <bot user> -- <alias> <script>`: one argv, no local shell."""
    return [
        SSH_BINARY,
        *SSH_OPTIONS,
        "-l",
        target.user,
        "--",
        target.alias,
        script,
    ]


def _contain(tag: str) -> str:
    """Linux (#817): set "$@" to the systemd-run words that exec the run in its own user scope.

    Only on a device whose user manager lingers and puts a process it
    starts in a scope of its own on the unified cgroup hierarchy (the
    probe's `0::` line), and only once the scope record names the unit;
    otherwise "$@" is empty, UNCONTAINED goes to stderr and the run starts uncontained.
    systemd-run 254+ expands `${VAR}` in a scope's argv unless given
    `--expand-environment=no`, a flag older releases lack.
    XDG_RUNTIME_DIR, which systemd-run needs to reach the user manager, is
    the lingering user's runtime directory when the session gave none.
    """
    unit = f"orch-run-{tag}.scope"
    scope = "systemd-run --user --scope --quiet --collect $noexpand"
    return (
        'set --; if u=$(id -u 2>/dev/null) && '
        '[ "$(loginctl show-user "$u" -p Linger --value 2>/dev/null)" = yes ] && '
        "x=${XDG_RUNTIME_DIR:-/run/user/$u} && "
        "noexpand=--expand-environment=no && "
        "if help=$(systemd-run --help 2>/dev/null); then "
        "case $help in *--expand-environment*) ;; *) noexpand= ;; esac; fi && "
        f'XDG_RUNTIME_DIR="$x" {scope} '
        "grep -q '^0::/.*[.]scope$' /proc/self/cgroup < /dev/null > /dev/null 2>&1 && "
        f"echo {unit} > {shlex.quote(scope_record(tag))} && "
        'export XDG_RUNTIME_DIR="$x"; '
        f"then set -- {scope} --unit={unit} --; "
        f"else echo {shlex.quote(UNCONTAINED)} >&2; fi; "
    )


def _cap_cpus(tag: str, cap: int) -> str:
    """POSIX sh that pins the current shell to cap of its nproc CPUs, the
    window lib/harness_env.py cpu_window picks for tag, or says it could not."""
    skipped = f"orch: cpu cap: affinity skipped ($why); env cap ORCH_CPU_CAP={cap} still applies"
    return (
        "why=; cpus=$(nproc 2>/dev/null) || cpus=; "
        "case $cpus in ''|*[!0-9]*) why='no nproc' ;; "
        f'*) if [ "$cpus" -gt {cap} ]; then '
        f"i=0; list=; while [ $i -lt {cap} ]; do "
        f"list=${{list:+$list,}}$(( ({int(tag[:8], 16)} + i) % cpus )); i=$((i + 1)); done; "
        "if ! command -v taskset >/dev/null 2>&1; then why='no taskset'; "
        'elif ! taskset -pc "$list" $$ >/dev/null 2>&1; then why="taskset -pc $list failed"; fi; '
        "fi ;; esac; "
        f'if [ -n "$why" ]; then echo "{skipped}" >&2; fi; '
    )


def start_script(
    target: RemoteTarget,
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    guarded_dirs: Sequence[str] = (),
    insert_at: int = 0,
    cd: str | None = None,
    report_env: bool = False,
    env_file: str | None = None,
    unset: Sequence[str] = (),
    cpu_cap: int = CPU_CAP,
) -> str:
    """The remote script that runs command as the job's tagged worker.

    guarded_dirs (Factory, Nov 13): each is passed to the command as
    `--add-dir <dir>`, at index insert_at, only if `test -d` holds for it on
    the device when the job starts. A relative dir is tested under cd (the
    directory the command resolves it against, itself relative to the job
    directory) when one is given. With none, the command runs as given.
    With report_env, the start first prints the WORKER_ENV_MARK line, or
    writes it to env_file (mode 600) when one is given. Each name in unset
    is dropped after the profile, as TOKEN_VARIABLES are; one the profile
    cannot drop refuses the start. On Linux (#1222) the start pins itself to
    cpu_cap CPUs before it execs, so the harness and its descendants inherit
    the pin; where it cannot, one line on stderr says so.
    """
    tag = target.require_tag()
    if not command:
        raise RemoteError("no command to run")
    if guarded_dirs and not 1 <= insert_at <= len(command):
        raise RemoteError(f"cannot add directories at argument {insert_at}")
    for name in env:
        if not _ENV_NAME.fullmatch(name) or name == TAG_ENV:
            raise RemoteError(f"environment name {name!r} is not plain")
    for name in unset:
        if not _ENV_NAME.fullmatch(name) or name in {*env, "ORCH_JOB_WORKDIR", TAG_ENV}:
            raise RemoteError(f"cannot drop environment name {name!r}")
    dropped = (*TOKEN_VARIABLES, *unset)
    assignments = [f"{name}={shlex.quote(value)}" for name, value in sorted(env.items())]
    assignments += ['ORCH_JOB_WORKDIR="$d"', f"{TAG_ENV}={tag}"]
    count = len(assignments)
    argv = " ".join(shlex.quote(part) for part in command)
    # #94: the harness runs under the bot's login shell, so its own PATH (gh,
    # claude, codex) is loaded. #99 A6: the job's environment and directory
    # are applied after the profile ran, so a profile can change neither.
    # #509: the `sh` hop ends env's NAME=value words before the command,
    # whose first word may hold `=`.
    # Every step execs, so the start's pid and group are the harness's.
    guard = ""
    if guarded_dirs:
        # Factory, Nov 13: each --add-dir only where the device has it.
        pairs = " ".join(
            f"{shlex.quote(posixpath.join(cd or '', path))} {shlex.quote(path)}"
            for path in guarded_dirs
        )
        guard = f"{insert_at} {len(guarded_dirs)} {pairs} "
    script = (
        ("exec 1>&3 3>&-; " if report_env else "")
        + f'{_UNTRACE}cd -- "$1" || exit {START_REFUSED}; shift; '
        + f"unset {' '.join(dropped)} || exit {START_REFUSED}; "
        + f'export "${{@:1:{count}}}" || exit {START_REFUSED}; '
        + f'set -- "${{@:{count + 1}}}" "${{@:1:{count}}}"; '
        + (_GUARD_DIRS if guarded_dirs else "")
        + 'command -v "$1" >/dev/null 2>&1 '
        + '|| { printf "%s: %s: not found on the login PATH\\n" "$0" "$1" >&2; exit 127; }; '
        + f"echo {tag} > {shlex.quote(launch_record(target))} || exit {START_REFUSED}; "
        + (_report_env(env_file, dropped if unset else ()) if report_env else "")
        + "exec /usr/bin/env "
        + "".join(f"-u {name} " for name in dropped)
        + f'"${{@: -{count}}}" /bin/sh -c \'exec "$@"\' sh "${{@:1:$#-{count}}}"'
    )
    login = (
        f"{LOGIN_SHELL} -lc {shlex.quote(script)} {LOGIN_NAME} "
        f'"$d" {" ".join(assignments)} {guard}{argv}'
        + (" 3>&1 1>&2" if report_env else "")
    )
    directory = shlex.quote(target.job_dir)
    pid_file = shlex.quote(PID_FILE)
    record_file = shlex.quote(start_record(tag))
    darwin = (
        f"{_DARWIN_BOOT} || exit {START_REFUSED}; "
        "set -- $(ps -o pgid= -p $$ 2>/dev/null); "
        f'[ "$1" = "$$" ] || exit {START_REFUSED}; '
        f'p=$$; {_LSTART}; s="$*"; [ -n "$s" ] || exit {START_REFUSED}; '
        f'mkdir -p -- "$d" && cd -- "$d" || exit {START_REFUSED}; '
        f"k=$(sh -c {shlex.quote(_DARWIN_KEEPER)} orch-keeper $$ "
        f"{shlex.quote(f'.orch-worker-{tag}.keeper.pid')} "
        "< /dev/null 2> /dev/null &); "
        f'p=$k; {_LSTART}; [ -n "$k" ] && [ -n "$*" ] '
        f"&& echo \"$k $*\" > {shlex.quote(f'.orch-worker-{tag}.members.pid')} "
        f'&& (set -C; echo "$$ $c {tag} $s" > {record_file}) 2>/dev/null '
        f'|| {{ kill -KILL "$k"; exit {START_REFUSED}; }}; '
        f"exec {login}"
    )
    inner = (
        f"d={directory}; "
        f"lk=$({_FLOCK_ON_START_OR_LOGIN_PATH}); {_CLAIM} || exit {START_REFUSED}; "
        f"if {_IS_DARWIN}; then {darwin}; fi; "
        f"{_CAN_SCAN} || exit {START_REFUSED}; "
        f"[ -r /proc/self/environ ] || exit {START_REFUSED}; "
        f"read -r b < /proc/sys/kernel/random/boot_id || exit {START_REFUSED}; "
        f'mkdir -p -- "$d" && cd -- "$d" || exit {START_REFUSED}; '
        f'(set -C; echo "$$ $b {tag}" > {record_file}) 2>/dev/null || exit {START_REFUSED}; '
        f'echo "$$ $b" > {pid_file} || exit {START_REFUSED}; '
        f"read -r s < /proc/self/stat || exit {START_REFUSED}; "
        's=${s##*) }; s=${s#* }; s=${s#* }; s=${s#* }; '
        f'if [ "${{s%% *}}" = "$$" ]; then k=$(sh -c {shlex.quote(KEEPER)} orch-keeper '
        f'< /dev/null 2> /dev/null &); [ -n "$k" ] || exit {START_REFUSED}; fi; '
        f"{_contain(tag)}"
        f"{raise_sh()}"
        f"{_cap_cpus(tag, cpu_cap)}"
        f'exec "$@" {login}'
    )
    return f"exec env {TAG_ENV}={tag} sh -c {shlex.quote(inner)}"


def check_script(target: RemoteTarget) -> str:
    """Exit 1 while the job's worker runs, 0 once it is gone, 3 when unsure."""
    return _sh(f"t={target.require_tag()}; {_check_body(target.job_dir)}")


def worker_check_script(job_dir: str, tag: str) -> str:
    """Exit 1 while tag's worker in job_dir runs, 0 once it is gone, 3 when unsure."""
    if not _TAG.fullmatch(tag):
        raise RemoteError("worker tag is not 32 lowercase hex digits")
    return _sh(f"t={tag}; {_check_body(job_dir, fence=False)}")


def _check_body(job_dir: str, *, fence: bool = True, claim: bool = False) -> str:
    """check_script's test for the worker whose tag is in $t (fence: _darwin_worker).

    claim (Linux): first take the start's record as _darwin_worker's fence
    does, or be unsure, so a start of $t not yet recorded refuses and never
    runs after this check says gone, even one the process scan missed.
    """
    directory = shlex.quote(job_dir)
    pid_file = shlex.quote(f"{job_dir}/{PID_FILE}")
    darwin = _darwin_worker(
        job_dir, gone="exit 0", unsure="exit 3", running="exit 1", blind="exit 3", fence=fence
    )
    record = (
        f'r={directory}/{start_record("$t")}; if [ ! -e "$r" ]; then '
        f"mkdir -p -- {directory} 2>/dev/null; "
        f'(set -C; echo {_FENCE} > "$r") 2>/dev/null; [ -e "$r" ] || exit 3; fi; '
        if claim
        else ""
    )
    return (
        f"f={pid_file}; "
        f"if {_IS_DARWIN}; then {darwin}; fi; "
        f"{record}"
        f"{_CAN_SCAN} || exit 3; "
        "[ -r /proc/self/stat ] || exit 3; "
        "command -v flock >/dev/null 2>&1 || exit 3; "
        f"{_members(directory)}"
        'o=$(members) || exit 3; [ -z "$o" ] || exit 1; '
        '[ -e "$f" ] || exit 0; '
        '{ read -r p b < "$f"; } 2>/dev/null || exit 3; '
        f'case "$p" in {_BAD_GROUP}) exit 3;; esac; '
        '[ -n "$b" ] || exit 3; '
        "read -r c < /proc/sys/kernel/random/boot_id || exit 3; "
        '[ "$b" = "$c" ] || exit 0; '
        "for d in /proc/[0-9]*; do "
        '{ read -r s < "$d/stat"; } 2>/dev/null || continue; '
        "set -- ${s##*) }; "
        'case "$1" in Z|X) continue;; esac; '
        '[ "$3" = "$p" ] && exit 3; '
        "done; "
        "exit 0"
    )


def fenced_check_script(target: RemoteTarget) -> str:
    """check_script, after taking the tag's run directory (as launch_script's mkdir
    would) and, on Linux, the start's record.
    """
    tag = target.require_tag()
    return _sh(
        f"t={tag}; R={shlex.quote(run_dir(target))}; "
        'mkdir -p -- "$R" 2>/dev/null; '
        'if (umask 077 && mkdir -- "$R/$t") 2>/dev/null; then '
        f'echo {START_REFUSED} > "$R/$t/exit" 2>/dev/null || :; '
        'elif [ ! -d "$R/$t" ]; then exit 3; fi; '
        f"{_check_body(target.job_dir, claim=True)}"
    )


def stop_script(target: RemoteTarget, *, kill: bool = False) -> str:
    """SIGTERM (SIGKILL with kill) each process of the run, and each tagged one's group.

    With kill, the scope's cgroup.kill is written first when the scope record
    names it (#1210), then every member is stopped (SIGSTOP), tracking again
    until no new one appears, so none forks a process that escapes the SIGKILL.
    """
    tag = target.require_tag()
    sig = "KILL" if kill else "TERM"
    pid_file = shlex.quote(f"{target.job_dir}/{PID_FILE}")
    scope = f"{shlex.quote(target.job_dir)}/{scope_record('$t')}"
    cgroup_kill = (
        f'u=; {{ read -r u < {scope}; }} 2>/dev/null; '
        'if [ "$u" = "orch-run-$t.scope" ]; then for w in /proc/[0-9]*; do y=; '
        '{ while read -r y; do case "$y" in 0::*) break;; esac; y=; done < "$w/cgroup"; } 2>/dev/null; '
        f'case "$y" in 0::*/"$u") z="{CGROUP_ROOT}${{y#0::}}/cgroup.kill"; '
        '[ ! -e "$z" ] || { echo 1 > "$z"; } 2>/dev/null; break;; esac; '
        "done; fi; "
        if kill
        else ""
    )
    halt = (
        'a=; n=1; while [ -n "$n" ]; do n=; '
        'm=$(members) || { for q in $a; do kill -CONT "$q" 2>/dev/null; done; exit 3; }; '
        "for q in $m; do "
        'case " $a " in *" $q "*) ;; *) kill -STOP "$q" 2>/dev/null; a="$a $q"; n=1;; esac; '
        "done; done; "
        if kill
        else "m=$(members) || exit 3; "
    )
    resume = 'for q in $a; do kill -CONT "$q" 2>/dev/null; done; ' if kill else ""
    darwin = _darwin_worker(
        target.job_dir,
        gone="exit 0",
        unsure="exit 0",
        running=f'{{ kill -{sig} -"$p" 2>/dev/null; exit 0; }}',
        blind="exit 3",
        snapshot=True,
    )
    return _sh(
        f"t={tag}; f={pid_file}; "
        f"if {_IS_DARWIN}; then {darwin}; fi; "
        f"{_CAN_SCAN} || exit 3; "
        "command -v flock >/dev/null 2>&1 || exit 3; "
        f"{_members(shlex.quote(target.job_dir))}"
        f"{cgroup_kill}"
        f"{halt}"
        f"for e in $({_TAGGED}); do "
        "q=${e#/proc/}; q=${q%/environ}; "
        '{ read -r s < "/proc/$q/stat"; } 2>/dev/null || continue; '
        "set -- ${s##*) }; "
        f'case "$3" in {_BAD_GROUP}) ;; *) kill -{sig} -"$3" 2>/dev/null;; esac; '
        f'kill -{sig} "$q" 2>/dev/null; '
        "done; "
        f'for q in $m; do kill -{sig} "$q" 2>/dev/null; done; '
        f"{resume}"
        "exit 0"
    )


# --- #128: a detached start, followed with short calls -----------------------
#
# The launch call starts the harness under setsid, apart from its own SSH
# session, so a dropped link costs the box a retry, not the job. The run's
# files live in `<workdir>/jobs/<job-id>.run/<tag>/` (mode 700): `stdin` (the
# brief), `pid` (`<wrapper pid> <start pid>`), `log` (the harness's output),
# `handoff` (HANDOFF_AWK's line), `exit` (its exit status, moved into place
# once it has ended) and, with telemetry, `env` (the start's
# WORKER_ENV_MARK report, mode 600, which the follower relays once and
# removes: it never enters the log).
LAUNCH_MARK = "orch-launch:"
FOLLOW_MARK = "orch-follow:"
LAUNCH_STARTED = ("started", "already", "pending")
LAUNCH_BUSY = "busy"
LAUNCH_SHORT = "short"
ENV_FILE = "env"

# The run's handoff, by the box's rule for what it relays: a line that is a
# JSON object is a stream event (lib/telemetry.py), whose final message
# (`result`, or a completed `agent_message`) gives its deciding marker line
# at the next end event (`result`, `turn.completed`) or the end of the log;
# any other line is relayed as is. Prints the marker line that decides the
# relayed lines.
HANDOFF_AWK = (
    f'BEGIN {{ lines = {MARKER_SEARCH_LINES}; limit = {MARKER_LINE_LIMIT}; '
    f'runner = "{RUNNER_LOG_PREFIX}"; prefix = "{MARKER_PREFIX}" }}'
) + r"""
function value(line, key,    i, n, c, h, out) {
    found = 0
    if (!match(line, "\"" key "\"[ \t]*:[ \t]*\"")) return ""
    n = length(line); out = ""
    for (i = RSTART + RLENGTH; i <= n; i++) {
        c = substr(line, i, 1)
        if (c == "\"") { found = 1; return out }
        if (c == "\\") {
            c = substr(line, ++i, 1)
            if (c == "n") c = "\n"
            else if (c == "r") c = "\r"
            else if (c == "t" || c == "b" || c == "f") c = " "
            else if (c == "u") {
                h = tolower(substr(line, i + 1, 4)); i += 4
                c = (h == "000a" || h == "000d") ? "\n" : (h == "0009" ? " " : "?")
            }
        }
        out = out c
    }
    return ""
}
function strip(line) { sub(/^[ \t\r]+/, "", line); sub(/[ \t\r]+$/, "", line); return line }
function kept(line) { return line != "" && index(line, runner) != 1 && line !~ /^```[A-Za-z0-9_-]*$/ }
function candidate(line) { sub(/^`+/, "", line); return index(line, prefix) == 1 }
function handoff(text,    parts, k, n, line) {
    gsub(/\r/, "\n", text)
    n = 0
    for (k = split(text, parts, "\n"); k > 0 && n < lines; k--) {
        line = strip(parts[k])
        if (!kept(line)) continue
        n++
        if (candidate(line)) return substr(line, 1, limit)
    }
    return ""
}
function relay(line) { line = strip(line); if (kept(line)) seen[count++ % lines] = line }
/^[ \t]*\{.*\}[ \t\r]*$/ {
    if ($0 ~ /"type"[ \t]*:[ \t]*"result"/) text = value($0, "result")
    else if ($0 ~ /"type"[ \t]*:[ \t]*"item\.completed"/ && $0 ~ /"type"[ \t]*:[ \t]*"agent_message"/) text = value($0, "text")
    else found = 0
    if (found) { final = text; pending = 1 }
    if (pending && $0 ~ /"type"[ \t]*:[ \t]*"(result|turn\.completed)"/) { relay(handoff(final)); pending = 0 }
    next
}
{ relay($0) }
END {
    if (pending) relay(handoff(final))
    for (k = count - 1; k >= 0 && k >= count - lines; k--) if (candidate(seen[k % lines])) { print seen[k % lines]; exit }
}
"""

# Detach "$@" from the session (setsid, else perl's POSIX::setsid on a Mac
# without one) with the worker tag in its environment, by exec: the caller
# runs it as an asynchronous list, so $! is the detached process.
_DETACH = (
    'detach() { if [ "$S" = setsid ]; then exec env "ORCH_WORKER_TAG=$t" setsid "$@"; fi; '
    'exec env "ORCH_WORKER_TAG=$t" perl -MPOSIX -e "POSIX::setsid(); exec @ARGV or exit 127" "$@"; }; '
)

# #199 r1: the run's deadline on the device, so it holds when the box's
# runner dies. $1 the wrapper's pid, $2 the budget and $3 the grace (whole
# seconds), $4 and $5 the stop and kill scripts (stop_script), $6 the check
# script (check_script). It runs in a session of its own, without the tag
# and with ORCH_WORKER_DEADLINE=<tag> (#722: the wrapper is its parent, and
# that marks it as never a process of the run, so no check counts it or the
# checks it runs, and no stop reaches it). The run lives while the wrapper
# does or (#199 r2) the check finds a process of it, so a worker the
# wrapper left behind keeps the deadline going. Past the budget it runs the
# stop script; past the grace after that, the kill script, again each
# second until the run is gone.
_DEADLINE = (
    'W=$1; B=$2; G=$3; C=$6; trap "" TERM INT HUP; '
    'late() { [ "$(date +%s)" -ge "$1" ]; }; '
    'live() { kill -0 "$W" 2>/dev/null && return 0; sh -c "$C" > /dev/null 2>&1; [ $? -eq 1 ]; }; '
    'e=$(( $(date +%s) + B + 1 )); '
    'while live; do late "$e" && break; sleep 1; done; '
    'live || exit 0; '
    'sh -c "$4"; e=$(( $(date +%s) + G )); '
    'while live; do late "$e" && break; sleep 1; done; '
    'while live; do sh -c "$5"; sleep 1; done'
)

# #1210: the wrapper's last step. $1 the run's directory, $2 the start's
# status, $3 and $4 the kill and check scripts, $5 the launch call's pid.
# The launch call carries the tag, so the kill would end it: it waits for
# that call first. TERM and INT stay ignored across the wrapper's exec.
_FINISH = (
    'T=$1; r=$2; K=$3; C=$4; L=$5; k=0; '
    'while [ -n "$L" ] && kill -0 "$L" 2>/dev/null && [ "$k" -lt 100 ]; do '
    'k=$((k + 1)); sleep 0.1 2>/dev/null || sleep 1; done; k=0; '
    'while :; do sh -c "$K" > /dev/null 2>&1; sh -c "$C" > /dev/null 2>&1; '
    f'[ $? -ne 0 ] && [ "$k" -lt {WRAPPER_KILL_ROUNDS} ] || break; '
    "k=$((k + 1)); sleep 1; done; "
    f"awk {shlex.quote(HANDOFF_AWK)} "
    '"$T/log" > "$T/handoff.tmp" 2>/dev/null; mv -f -- "$T/handoff.tmp" "$T/handoff"; '
    'printf "%s\\n" "$r" > "$T/exit.tmp" && mv -f -- "$T/exit.tmp" "$T/exit"'
)

# The wrapper: the run's own monitor, detached by the launch. $1 the run's
# directory, $2 the detacher, $3 the start script, then (#199 r1) $4 the
# budget in whole seconds (empty: none), $5 the grace, $6 and $7 the stop and
# kill scripts, $8 the check script, $9 the launch call's pid. It starts
# the start (start_script, which records the worker and execs the harness) in a session of its own with
# the brief as its stdin and its output in `log`, records both pids, starts
# its deadline (_DEADLINE) when it has a budget, waits for the start (again
# after a SIGTERM, which it catches so the start keeps default handling),
# then execs into _FINISH.
_WRAPPER = (
    'T=$1; S=$2; x=$3; K=$7; C=$8; L=$9; t=$ORCH_WORKER_TAG; s=; trap "s=1" TERM INT; '
    + _DETACH
    + 'detach sh -c "$x" < "$T/stdin" > "$T/log" 2>&1 & c=$!; '
    'printf "%s %s\\n" "$$" "$c" > "$T/pid.tmp" && mv -f -- "$T/pid.tmp" "$T/pid"; '
    'if [ -n "$4" ]; then '
    f'set -- sh -c {shlex.quote(_DEADLINE)} orch-deadline "$$" "$4" "$5" "$6" "$7" "$8"; '
    f'if [ "$S" = setsid ]; then env -u {TAG_ENV} "{DEADLINE_ENV}=$t" setsid "$@" '
    '< /dev/null > /dev/null 2>&1 & '
    f'else env -u {TAG_ENV} "{DEADLINE_ENV}=$t" '
    'perl -MPOSIX -e "POSIX::setsid(); exec @ARGV or exit 127" "$@" '
    '< /dev/null > /dev/null 2>&1 & fi; fi; '
    'while :; do s=; wait "$c"; r=$?; '
    'if [ -n "$s" ] && kill -0 "$c" 2>/dev/null; then continue; fi; break; done; '
    f'trap "" TERM INT; exec env -u {TAG_ENV} "{DEADLINE_ENV}=$t" '
    f'sh -c {shlex.quote(_FINISH)} orch-finish "$T" "$r" "$K" "$C" "$L"'
)


def run_dir(target: RemoteTarget) -> str:
    """Beside the job directory: one directory per start, named by its tag."""
    return f"{target.job_dir}.run"


def run_file(target: RemoteTarget, name: str) -> str:
    """One of this start's files in its run directory."""
    return f"{run_dir(target)}/{target.require_tag()}/{name}"


def env_clear_script(target: RemoteTarget) -> str:
    """Remove the start's environment report once the box has it."""
    return _sh(f"rm -f -- {shlex.quote(run_file(target, ENV_FILE))}")


SWEPT_FILE = "swept"
LEFTOVER_MARK = "orch-leftover:"
REAP_MARK = "orch-reap:"


@dataclass(frozen=True)
class ListedRun:
    job_id: str
    tag: str
    ended: bool


def leftovers_script(jobs_dir: str) -> str:
    return _sh(
        f"cd -- {shlex.quote(jobs_dir)} 2>/dev/null || exit 0; "
        'for r in *.run/*/; do r=${r%/}; j=${r%%.run/*}; t=${r#*.run/}; '
        '[ -d "$r" ] && [ ! -L "$r" ] && [ ! -L "$j.run" ] || continue; '
        'case "$j" in ""|*[!A-Za-z0-9_-]*) continue;; esac; '
        'case "$t" in *[!0-9a-f]*) continue;; esac; '
        f'[ ${{#t}} = 32 ] && [ ! -e "$r/{SWEPT_FILE}" ] || continue; '
        'e=1; if [ ! -e "$r/exit" ]; then p=; { read -r p < "$r/pid"; } 2>/dev/null; '
        'for q in $p; do kill -0 "$q" 2>/dev/null && e=0; done; fi; '
        f'echo "{LEFTOVER_MARK} $j $t $e"; done'
    )


class Listing(NamedTuple):
    runs: list[ListedRun]
    unreadable: int


def read_leftovers(stdout: str) -> Listing:
    runs: list[ListedRun] = []
    unreadable = 0
    for line in stdout.splitlines():
        if not line.startswith(LEFTOVER_MARK):
            continue
        match line[len(LEFTOVER_MARK):].split():
            case [job_id, tag, ("0" | "1") as ended] if _JOB_ID.fullmatch(job_id) and _TAG.fullmatch(tag):
                runs.append(ListedRun(job_id, tag, ended == "1"))
            case _:
                unreadable += 1
    return Listing(runs, unreadable)


def reap_script(target: RemoteTarget) -> str:
    check = worker_check_script(target.job_dir, target.require_tag())
    swept = shlex.quote(run_file(target, SWEPT_FILE))
    return _sh(
        f"({check}); b=$?; a=-; "
        f'if [ "$b" != 0 ]; then ({stop_script(target, kill=True)}); ({check}); a=$?; fi; '
        f'if [ "$b" = 0 ] || [ "$a" = 0 ]; then {{ : > {swept}; }} 2>/dev/null; fi; '
        f'echo "{REAP_MARK} $b $a"'
    )


def read_reap(stdout: str) -> tuple[str, str | None] | None:
    fields = (_marked(stdout, REAP_MARK) or "").split()
    if len(fields) != 2 or not fields[0].isdigit() or not (fields[1].isdigit() or fields[1] == "-"):
        return None
    return worker_state(int(fields[0])), None if fields[1] == "-" else worker_state(int(fields[1]))


def launch_script(
    target: RemoteTarget,
    start: str,
    *,
    size: int,
    not_after: int,
    budget: int | None = None,
    grace: int = 30,
) -> str:
    """The short call that starts `start` detached, once per tag.

    The brief (exactly `size` bytes) comes on stdin. Prints one
    `orch-launch: <word>` line: `started` (the wrapper recorded itself),
    `already` (this tag was launched before: nothing is started twice),
    `pending` (no record within 10 seconds), `short` (the brief came in
    short, nothing done: send it again), `busy` (another tag of the job is
    the current run and not confirmed gone) or `refused <why>` (nothing
    ran). A launch that reaches the device after not_after (the box's epoch
    seconds) is refused unless its tag already ran. Like the start, it runs
    under `exec env ORCH_WORKER_TAG=<tag>`, so a check finds it running.

    #199 r1: with a budget (whole seconds), the device stops the run once it
    is past it (stop_script), and kills it `grace` seconds later
    (stop_script with kill), whether or not the box still follows it.
    #1210: with or without one, once the start exits (a SIGKILL included)
    the device kills every process the run left before it writes `exit`.
    """
    tag = target.require_tag()
    if size < 0 or not_after <= 0:
        raise RemoteError("a launch needs the brief's size and a deadline")
    if (budget is not None and budget <= 0) or grace < 0:
        raise RemoteError("a launch's budget must be above 0 and its grace not below 0")
    limits = (
        f"{shlex.quote('' if budget is None else str(budget))} {grace} "
        f"{shlex.quote(stop_script(target))} "
        f"{shlex.quote(stop_script(target, kill=True))} {shlex.quote(check_script(target))}"
    )
    mark = LAUNCH_MARK
    launcher = (
        f"t={tag}; R={shlex.quote(run_dir(target))}; T=\"$R/$t\"; "
        f"say() {{ printf '{mark} %s\\n' \"$*\"; }}; "
        '[ -d "$T" ] && { say already; exit 0; }; '
        'n=$(date +%s 2>/dev/null); case "$n" in ""|*[!0-9]*) say refused clock; exit 0;; esac; '
        f'[ "$n" -le {not_after} ] || {{ say refused late; exit 0; }}; '
        "if command -v setsid >/dev/null 2>&1; then S=setsid; "
        "elif command -v perl >/dev/null 2>&1; then S=perl; "
        "else say refused setsid; exit 0; fi; "
        'mkdir -p -- "$R" || { say refused rundir; exit 0; }; '
        'i="$R/.$t.$$"; '
        '(umask 077 && cat > "$i") 2>/dev/null || { rm -f -- "$i"; say short; exit 0; }; '
        f'w=$(wc -c < "$i"); [ "$((w + 0))" -eq {size} ] || {{ rm -f -- "$i"; say short; exit 0; }}; '
        'if ! (umask 077 && mkdir -- "$T") 2>/dev/null; then rm -f -- "$i"; '
        '[ -d "$T" ] && { say already; exit 0; }; say refused tagdir; exit 0; fi; '
        f'mv -f -- "$i" "$T/stdin" || {{ echo {START_REFUSED} > "$T/exit"; say refused stdin; exit 0; }}; '
        'c="$R/current"; '
        'if { read -r o < "$c"; } 2>/dev/null && [ -n "$o" ] && [ "$o" != "$t" ] '
        '&& [ ! -e "$R/$o/exit" ]; then '
        f'(t=$o; {_check_body(target.job_dir)}) || {{ echo {START_REFUSED} > "$T/exit"; say busy; exit 0; }}; '
        "fi; "
        'printf "%s\\n" "$t" > "$c.$$" && mv -f -- "$c.$$" "$c" '
        f'|| {{ echo {START_REFUSED} > "$T/exit"; say refused current; exit 0; }}; '
        + _DETACH
        + f"detach sh -c {shlex.quote(_WRAPPER)} orch-run \"$T\" \"$S\" {shlex.quote(start)} "
        f"{limits} \"$$\" < /dev/null > /dev/null 2>&1 & "
        "k=0; while [ ! -e \"$T/pid\" ] && [ ! -e \"$T/exit\" ] && [ \"$k\" -lt 100 ]; do "
        "sleep 0.1 2>/dev/null || sleep 1; k=$((k+1)); done; "
        'if [ -e "$T/pid" ]; then say started; '
        'elif [ -e "$T/exit" ]; then say refused "exit $(cat "$T/exit")"; '
        "else say pending; fi"
    )
    return f"exec env {TAG_ENV}={tag} sh -c {shlex.quote(launcher)}"


def poll_script(target: RemoteTarget, offset: int, chunk: int) -> str:
    """Read-only: the run's state, and up to chunk bytes of its log from offset.

    Prints `orch-follow: <log size> <state> [<exit>]` on stderr, then on
    stdout exactly min(chunk, size - offset) bytes of the log (none when
    offset is past it). The state is `exited` (with its status), `running`,
    `gone` (no exit status and the worker confirmed gone), `unsure` (not
    yet recorded, or the check cannot tell) or `none` (no such start).
    `exit` is read before and after the worker check, so a run that ends in
    between reads as exited. The start's `env` report, when there is one,
    comes on stderr too, read after the log's size: a log that holds the
    harness's output holds no byte the report did not precede. Never starts or signals anything, and
    writes only the Linux tree record (as check does, so each poll tracks the run's processes while
    it lives): on Darwin a start not yet recorded is unsure, never fenced.
    """
    tag = target.require_tag()
    if offset < 0 or chunk <= 0:
        raise RemoteError("a poll needs an offset and a chunk size")
    return _sh(
        f"t={tag}; T={shlex.quote(run_dir(target))}/$t; "
        f"say() {{ printf '{FOLLOW_MARK} %s\\n' \"$*\" >&2; }}; "
        '[ -d "$T" ] || { say 0 none; exit 0; }; '
        'e=; [ -f "$T/exit" ] && { read -r e < "$T/exit"; } 2>/dev/null; '
        'if [ -n "$e" ]; then st=exited; '
        'elif [ ! -f "$T/pid" ]; then st=unsure; '
        f'else ({_check_body(target.job_dir, fence=False)}); '
        'case $? in 0) st=gone;; 1) st=running;; *) st=unsure;; esac; '
        '[ -f "$T/exit" ] && { read -r e < "$T/exit"; } 2>/dev/null; '
        'if [ -n "$e" ]; then st=exited; '
        'elif [ "$st" = gone ] && { read -r w _ < "$T/pid"; } 2>/dev/null '
        '&& kill -0 "$w" 2>/dev/null; then st=unsure; fi; '
        "fi; "
        'z=0; [ -f "$T/log" ] && z=$(wc -c < "$T/log"); z=$((z + 0)); '
        f'[ -f "$T/{ENV_FILE}" ] && cat -- "$T/{ENV_FILE}" >&2; '
        f"n=$((z - {offset})); [ \"$n\" -gt {chunk} ] && n={chunk}; "
        'say "$z" "$st" $e; '
        f'[ "$n" -gt 0 ] && tail -c +{offset + 1} "$T/log" | head -c "$n"; '
        "exit 0"
    )


PROBE_SCRIPT = "exit 0"


# --- #94: preflight and job checkout on the placed device --------------------
#
# Both run under the bot's login shell (`bash -lc`), as the harness does, so
# they see the PATH the harness will. Job values never reach the script text:
# each script is a constant, and the values are its quoted positional
# arguments (`bash -lc '<script>' <name> <arg>...`).

# Prints `orch-preflight: missing <tool>...` on stderr and exits 3 when any
# tool is not found under the login PATH.
_PREFLIGHT = (
    'm=; for t in "$@"; do command -v "$t" >/dev/null 2>&1 || m="$m $t"; done; '
    '[ -z "$m" ] && exit 0; '
    f"printf '{_PREFLIGHT_MARK}%s\\n' \"$m\" >&2; exit 3"
)

# $1 job dir, $2 git URL, $3 head (or empty), $4 base branch (or empty),
# $5 the job's branch (or empty), $6 the brief's path (or empty: no brief).
# #953: every runner entry (checkout and start) claims the job under
# `flock` on `<job-dir>.lock`, the lock `orch cleanup device` takes around
# each move or empty of the folder. The claim touches `<job-dir>.claimed`,
# which cleanup reads under the lock, and moves `<job-dir>.cleanup` back to
# `<job-dir>` when cleanup set the folder aside first. With no `flock` ($lk
# empty) the claim runs unlocked; cleanup then leaves every folder alone.
# `ino` compares inode numbers: `-ef` against /dev/fd/9 never matches on
# macOS, whose devfs gives /dev/fd/9 another st_dev (#1298).
_RELOCK = 75
_CLAIM = (
    '{ claim() { touch -- "$d.claimed" && { [ -e "$d" ] || [ -L "$d" ] '
    '|| [ ! -d "$d.cleanup" ] || [ -L "$d.cleanup" ] || mv -- "$d.cleanup" "$d"; }; }; '
    'ino() { ls -iL -- "$1" 2>/dev/null | { read -r i _ && echo "$i"; }; }; '
    'held() { "$lk" -w 600 9 || return 1; held_ino=$(ino /dev/fd/9); '
    f'[ -z "$held_ino" ] || [ "$(ino "$d.lock")" = "$held_ino" ] || return {_RELOCK}; claim; }}; '
    f'locked() {{ while {{ held; }} 9>>"$d.lock"; lr=$?; [ "$lr" -eq {_RELOCK} ]; do :; done; '
    'return "$lr"; }; '
    'mkdir -p -- "${d%/*}" && if [ -n "$lk" ]; then locked; else claim; fi; }'
)
_FLOCK_ON_START_OR_LOGIN_PATH = (
    f"command -v flock 2>/dev/null || {LOGIN_SHELL} -lc 'command -v flock | sed s/^/orch-flock:/' "
    "< /dev/null 2> /dev/null | sed -n 's/^orch-flock://p' | tail -n 1"
)
# Clones (git init + fetch, so a job directory that already holds orch's
# worker record still takes the checkout) or reuses the job's checkout, puts
# it at the head (on the job's branch when that branch's tip is exactly the
# head, else detached; with no head, the base branch or origin's default),
# keeps orch's own files out of `git status`, and writes the brief (stdin)
# beside the checkout. Prints `orch-checkout: ok <sha> <new|reused>` on
# success, `orch-checkout: <reason>` on stderr and exit 3 on failure.
_CHECKOUT = (
    'd=$1; u=$2; h=$3; m=$4; n=$5; b=$6; '
    f"fail() {{ printf '{_CHECKOUT_MARK} %s\\n' \"$*\" >&2; exit 3; }}; "
    "export GIT_TERMINAL_PROMPT=0; "
    'lk=$(command -v flock 2>/dev/null); '
    f'{_CLAIM} || fail "cannot claim $d"; '
    'mkdir -p -- "$d" || fail "cannot make $d"; '
    'cd -- "$d" || fail "cannot enter $d"; '
    'if [ -n "$b" ]; then (umask 077 && cat > "$b") || fail "cannot write the brief $b"; fi; '
    "exclude() { mkdir -p .git/info && for p in "
    + " ".join(
        shlex.quote(f"/{name}")
        for name in (PID_FILE, start_record("*"), f"{tree_record('*')}.lock", scope_record("*"))
    )
    + '; do grep -qxF -- "$p" .git/info/exclude 2>/dev/null '
    '|| printf \'%s\\n\' "$p" >> .git/info/exclude || return 1; done; }; '
    "if [ -d .git ] && t=$(git rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null); then "
    'exclude || fail "cannot write .git/info/exclude"; '
    f"printf '{_CHECKOUT_MARK} ok %s reused\\n' \"$t\"; exit 0; fi; "
    'git init -q . >/dev/null || fail "git init failed"; '
    "git remote remove origin >/dev/null 2>&1; "
    'git remote add origin "$u" || fail "git remote add failed"; '
    'git fetch -q --prune origin || fail "git fetch failed"; '
    'if [ -n "$h" ]; then '
    'if ! git cat-file -e "$h^{commit}" 2>/dev/null; then '
    'git fetch -q origin "$h" || fail "cannot fetch $h"; '
    'git cat-file -e "$h^{commit}" 2>/dev/null || fail "cannot fetch $h"; '
    "fi; "
    't=$(git rev-parse "$h^{commit}") || fail "cannot resolve $h"; '
    'elif [ -n "$m" ] && t=$(git rev-parse -q --verify "refs/remotes/origin/$m^{commit}"); then :; '
    "else "
    'git remote set-head origin --auto >/dev/null 2>&1 || fail "cannot find the default branch"; '
    "t=$(git rev-parse -q --verify 'refs/remotes/origin/HEAD^{commit}') "
    '|| fail "cannot find the default branch"; '
    "fi; "
    'if [ -n "$n" ] && [ -n "$h" ] '
    '&& [ "$(git rev-parse -q --verify "refs/remotes/origin/$n^{commit}")" = "$t" ]; then '
    'git checkout -q -B "$n" "$t" && git branch -q --set-upstream-to="origin/$n" "$n" '
    '|| fail "git checkout failed"; '
    "else "
    'git checkout -q --detach "$t" || fail "git checkout failed"; '
    "fi; "
    'exclude || fail "cannot write .git/info/exclude"; '
    f"printf '{_CHECKOUT_MARK} ok %s new\\n' \"$t\""
)


class CheckoutError(Exception):
    """The job cannot start on its device: preflight or checkout failed. One line."""


def brief_path(target: RemoteTarget) -> str:
    """Where the job's brief is written on the device: beside its checkout."""
    return f"{target.job_dir}.brief.md"


def _login(script: str, name: str, args: Sequence[str]) -> str:
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return f"exec {LOGIN_SHELL} -lc {shlex.quote(script)} {name} {quoted}".rstrip()


def preflight_script(harness: str) -> str:
    """Exit 0 when git, gh and harness are found under the bot's login PATH."""
    if not harness:
        raise RemoteError("no harness binary to check")
    return _login(_PREFLIGHT, "orch-preflight", (*PREFLIGHT_TOOLS, harness))


def plain_branch(name: str | None, what: str = "branch") -> str:
    if name is None:
        return ""
    if (
        not _BRANCH.fullmatch(name)
        or ".." in name
        or name.endswith((".lock", "/", "."))
        or "//" in name
        or "@{" in name
    ):
        raise RemoteError(f"{what} {name!r} is not a plain branch name")
    return name


def checkout_script(
    target: RemoteTarget,
    *,
    url: str,
    head: str | None,
    base_branch: str | None,
    branch: str | None,
    brief: bool,
) -> str:
    """The remote script that makes (or reuses) the job's checkout at head."""
    if head is not None and not _SHA.fullmatch(head):
        raise RemoteError(f"head {head!r} is not a commit SHA")
    if not url or url.startswith("-") or any(ord(c) < 32 for c in url):
        raise RemoteError("the git URL is not usable")
    args = (
        target.job_dir,
        url,
        head or "",
        plain_branch(base_branch, "base branch"),
        plain_branch(branch, "branch"),
        brief_path(target) if brief else "",
    )
    return _login(_CHECKOUT, "orch-checkout", args)


@dataclass(frozen=True)
class Called:
    code: int
    stdout: str
    stderr: str


def _marked(text: str, mark: str) -> str | None:
    """The last line starting with mark, without it; None if there is none."""
    found = None
    for line in text.splitlines():
        if line.startswith(mark):
            found = line[len(mark):].strip()
    return found


def _git_detail(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("fatal:", "error:")):
            return " ".join(line.split())[:200]
    return ""


def preflight(transport: CaptureTransport, target: RemoteTarget, harness: str) -> None:
    """Check over ssh that git, gh and harness exist under bash -lc. Raises CheckoutError."""
    result = transport.call(
        ssh_argv(target, preflight_script(harness)), timeout=PROBE_TIMEOUT_SECONDS
    )
    if result.code == 0:
        return
    if result.code == SSH_UNREACHABLE:
        raise CheckoutError(f"{target.device} is unreachable over ssh ({target.alias})")
    missing = _marked(result.stderr, _PREFLIGHT_MARK)
    if missing:
        raise CheckoutError(f"missing under bash -lc on {target.device}: {missing}")
    if result.code == 127:
        raise CheckoutError(f"{LOGIN_SHELL} not found on {target.device}")
    raise CheckoutError(f"preflight on {target.device} exited {result.code}")


def checkout(
    transport: CaptureTransport,
    target: RemoteTarget,
    *,
    url: str,
    head: str | None,
    base_branch: str | None,
    branch: str | None,
    brief: bytes | None,
) -> tuple[str, bool]:
    """Make the job's checkout on the device. Returns (HEAD sha, reused)."""
    script = checkout_script(
        target, url=url, head=head, base_branch=base_branch, branch=branch,
        brief=brief is not None,
    )
    result = transport.call(
        ssh_argv(target, script), timeout=CHECKOUT_TIMEOUT_SECONDS, input=brief
    )
    done = _marked(result.stdout, _CHECKOUT_MARK) if result.code == 0 else None
    if done is not None:
        parts = done.split()
        if len(parts) == 3 and parts[0] == "ok" and _SHA.fullmatch(parts[1]):
            return parts[1], parts[2] == "reused"
    if result.code == SSH_UNREACHABLE:
        raise CheckoutError(f"{target.device} is unreachable over ssh ({target.alias})")
    reason = _marked(result.stderr, _CHECKOUT_MARK) or f"exited {result.code}"
    detail = _git_detail(result.stderr)
    suffix = f": {detail}" if detail else ""
    raise CheckoutError(f"checkout failed on {target.device}: {reason}{suffix}")


class Transport(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> int: ...


class CaptureTransport(Transport, Protocol):
    def call(
        self, argv: Sequence[str], *, timeout: float, input: bytes | None = None
    ) -> Called: ...


@dataclass(frozen=True)
class Exchanged:
    """One call's exit status, its stdout as bytes and its stderr as text."""

    code: int
    stdout: bytes
    stderr: str
    timed_out: bool = False


@runtime_checkable
class ByteTransport(Transport, Protocol):
    def exchange(
        self, argv: Sequence[str], *, timeout: float, input: bytes = b""
    ) -> Exchanged: ...


class SshTransport:
    """The real transport: the `ssh` on PATH, argv only, never a shell.

    Calls built by `ssh_argv` go through one ControlMaster per alias and
    bot user (`lib/ssh_mux.py`, issue #222); any other argv runs as given.
    """

    def __init__(self, mux: Multiplexer | None = None):
        self.mux = mux or Multiplexer(binary=SSH_BINARY, options=SSH_OPTIONS)

    def _spawn(
        self, argv: Sequence[str], *, timeout: float, input: bytes | None, capture: bool
    ) -> subprocess.CompletedProcess:
        """Run argv; a timeout or a failed spawn raises, after the mux is told."""
        prepared = self.mux.prepare(argv)
        code: int | None = None
        try:
            if capture:
                completed = subprocess.run(
                    prepared.argv, input=input or b"", capture_output=True,
                    timeout=timeout, check=False,
                )
            else:
                completed = subprocess.run(
                    prepared.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                )
            code = completed.returncode
            return completed
        finally:
            self.mux.settle(prepared, code)

    def exchange(
        self, argv: Sequence[str], *, timeout: float, input: bytes = b""
    ) -> Exchanged:
        """Run argv with input on stdin; a timeout or a failed spawn is unreachable."""
        try:
            completed = self._spawn(argv, timeout=timeout, input=input, capture=True)
        except subprocess.TimeoutExpired:
            return Exchanged(SSH_UNREACHABLE, b"", f"timed out after {timeout:g} seconds", timed_out=True)
        except OSError as exc:
            return Exchanged(SSH_UNREACHABLE, b"", type(exc).__name__)
        return Exchanged(
            completed.returncode, completed.stdout, completed.stderr.decode("utf-8", "replace")
        )

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        try:
            completed = self._spawn(argv, timeout=timeout, input=None, capture=False)
        except subprocess.TimeoutExpired:
            return SSH_UNREACHABLE
        except OSError:
            return SSH_UNREACHABLE
        return completed.returncode

    def call(
        self, argv: Sequence[str], *, timeout: float, input: bytes | None = None
    ) -> Called:
        """Run argv, feeding input (else /dev/null) and capturing its output."""
        try:
            completed = self._spawn(argv, timeout=timeout, input=input, capture=True)
        except subprocess.TimeoutExpired:
            return Called(124, "", f"{_CHECKOUT_MARK} timed out after {timeout:g} seconds")
        except OSError as exc:
            return Called(SSH_UNREACHABLE, "", type(exc).__name__)
        return Called(
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )


def launch_record(target: RemoteTarget) -> str:
    """Beside the job directory: the tag of the last start that reached its harness."""
    return f"{target.job_dir}.launched"


def launched_script(target: RemoteTarget) -> str:
    """Exit 0 when this start reached its harness, 1 when it did not, else unsure."""
    tag = target.require_tag()
    record = shlex.quote(launch_record(target))
    return _sh(
        f"[ -e {record} ] || exit 1; "
        f"{{ read -r t < {record}; }} 2>/dev/null || exit 3; "
        f'[ "$t" = {tag} ] && exit 0; exit 1'
    )


def harness_launched(transport: Transport, target: RemoteTarget) -> bool | None:
    """True or False once the device answers whether the start reached its harness."""
    code = transport.run(
        ssh_argv(target, launched_script(target)), timeout=PROBE_TIMEOUT_SECONDS
    )
    return {0: True, 1: False}.get(code)


def check_worker(transport: Transport, target: RemoteTarget, *, fence: bool = True) -> str:
    """GONE (0), RUNNING (1), UNREACHABLE (ssh's 255) or UNKNOWN (anything else).

    Only GONE lets a slot go. UNKNOWN still proves the device answered.
    The target must carry the start's worker tag. The check first fences
    that tag's launch (fenced_check_script), so call it only once done with
    the start; GONE then holds for good. Pass fence=False only when no launch
    of the tag can still reach the device (it answered and none went unanswered).
    """
    script = fenced_check_script(target) if fence else check_script(target)
    return worker_state(transport.run(ssh_argv(target, script), timeout=PROBE_TIMEOUT_SECONDS))


def worker_state(code: int) -> str:
    """A worker check's exit code as GONE, RUNNING, UNREACHABLE (ssh's 255) or UNKNOWN."""
    return {0: GONE, 1: RUNNING, SSH_UNREACHABLE: UNREACHABLE}.get(code, UNKNOWN)


def stop_worker(transport: Transport, target: RemoteTarget, *, kill: bool = False) -> bool:
    """Ask the device to SIGTERM (SIGKILL with kill) the worker. True when it answered."""
    code = transport.run(
        ssh_argv(target, stop_script(target, kill=kill)), timeout=PROBE_TIMEOUT_SECONDS
    )
    return code == 0


def probe_device(transport: Transport, target: RemoteTarget) -> bool:
    """One SSH round trip over target's alias. True when the device answered."""
    code = transport.run(ssh_argv(target, PROBE_SCRIPT), timeout=PROBE_TIMEOUT_SECONDS)
    return code != SSH_UNREACHABLE
