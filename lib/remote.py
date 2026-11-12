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
  environment. sshd runs the command in a new session, so that process
  group is the job's. The record is never removed: deleting bookkeeping
  is not evidence the processes are gone;
* check: exit 1 while any process of this user carries the job's tag
  (wherever its group or session now is); otherwise exit 3 ("cannot tell")
  while the recorded group, from this boot, still has a process (one that
  dropped the tag, or an unrelated group that reused the number), and exit
  0 (gone) when it has none, the record is from an earlier boot, or no
  record was ever written. A malformed record (empty, not digits, a
  leading zero, 0 or 1) is 3. ssh itself exits 255 when the device cannot
  be reached;
* stop: send SIGTERM to every process carrying the tag and to each process
  group that holds one (a group with a tagged process is in the start's
  own session, or was made by one of its processes). A stored number is
  never signalled on its own;
* probe: `exit 0`, the health check for a device whose slots need no
  worker check this poll.

check and stop run as `exec sh -c '<script>'`, so they are POSIX sh whatever
the bot's login shell is (bash on Linux, zsh on macOS).

On a device whose `uname -s` is Darwin (macOS: no /proc, and no way to read
another process's environment) the same contract holds through the record,
one file per start, `<workdir>/jobs/<job-id>/.orch-worker-<tag>.pid`:

* start: refuse (125) unless `sysctl -n kern.bootsessionuuid` gives the boot
  session id and the start leads its own process group (`ps -o pgid=`);
  create the record with noclobber (O_EXCL) holding
  `<process group> <boot session> <tag> <leader start time>` (the leader's
  `ps -o lstart=` in the C locale and UTC), refusing if it already exists,
  and exec as above;
* check: with no record the start may still be on its way, so the check
  creates the record itself as the fence `fenced` (O_EXCL as well: the start
  or the check creates it, never both) and exits 3; the fenced start can
  never record itself and refuses, so a later check reads the fence as 0.
  0 too for a record from an earlier boot session or no process left in the
  recorded group, read from a `ps -A -o pgid=,pid=,uid=` that exited 0; 1
  while the group holds only this user's processes and is led by the
  recorded leader (same start time); 3 for a malformed record or one naming
  another tag, a group holding another user's process, a group whose leader
  has exited (nothing ties the processes left to this start: the number may
  have been reused while the group was empty), a leader whose start time
  differs (the number was reused), a fence that could not be written, and
  when there is no boot session id or ps fails or lists nothing;
* stop: SIGTERM to the recorded group exactly when the check would exit 1;
  nothing else is signalled (with no record it writes the fence as well).
  A process that left the job's group (setsid), or that stays in it after
  the leader exits, is never signalled here: macOS gives no way to find it
  by its tag. A group left without its leader keeps the slot until it is
  empty.

Every ssh call uses the `ssh` found on PATH with BatchMode (never a
password prompt), a connect timeout and server keepalives. Tests replace
it, or put a fake `ssh` first on PATH; nothing here contacts a real host
on its own.
"""

from __future__ import annotations

import re
import secrets
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

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
PID_FILE = ".orch-worker.pid"
TAG_ENV = "ORCH_WORKER_TAG"
# The start script's own exit status when it cannot make or enter the job
# directory, or record the worker: the harness never ran.
START_REFUSED = 125
SSH_UNREACHABLE = 255

GONE = "gone"
RUNNING = "running"
UNKNOWN = "unknown"
UNREACHABLE = "unreachable"

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TAG = re.compile(r"^[0-9a-f]{32}$")

# Shell fragments shared by the scripts. NUL-separated environments are read
# with `grep -z`; a device whose grep cannot do that cannot check a worker.
_CAN_SCAN = "printf 'x\\000' | grep -qzxF x"
_TAGGED = 'grep -lzxsF -e "ORCH_WORKER_TAG=$t" /proc/[0-9]*/environ'
_BAD_GROUP = '""|0*|1|*[!0-9]*'

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
# What a check or stop writes as the record of a start that has written none:
# the start's own record write (noclobber, so O_EXCL) then fails, and it
# refuses (125) without running anything.
_FENCE = "fenced"


def darwin_record(tag: str) -> str:
    """The Darwin record's file name in the job directory: one per start."""
    return f".orch-worker-{tag}.pid"


def _darwin_worker(
    target: RemoteTarget, *, gone: str, unsure: str, running: str, blind: str
) -> str:
    """Darwin: decide whether the record of tag $t names a live worker.

    A start for tag $t creates its record `$d/.orch-worker-$t.pid` with
    noclobber (O_EXCL) before it runs anything. With no record the start may
    still be on its way, so the check writes the fence record in its place
    (also O_EXCL: exactly one of the two creates the file) and is unsure this
    time; the start then cannot record itself and refuses.

    gone: the fence (that start can never run), a record from an earlier
    boot session, or no process left in the recorded group, all read from a
    ps that exited 0. unsure: no record yet (fence just written), a fence
    that could not be written, a malformed record or one naming another tag,
    a group holding another user's process, a group whose leader has exited
    (nothing ties the members left to this start: the number may have been
    reused while the group was empty), or a leader whose start time is not
    the recorded one, or a record whose boot session is not a UUID.
    running: the recorded group holds only this user's processes and is led
    by the recorded leader. blind: the device cannot tell (no boot session
    id, a user id that is not a number, ps failed or listed nothing, a
    listing line that is not three numbers, awk read less than the whole
    listing, failed, or printed anything but three fields of 0 or 1).
    """
    directory = shlex.quote(target.job_dir)
    return (
        f"d={directory}; f=\"$d/.orch-worker-$t.pid\"; "
        f"{_DARWIN_BOOT} || {blind}; "
        f"u=$(id -u) || {blind}; "
        f'case "$u" in ""|*[!0-9]*) {blind};; esac; '
        'if [ ! -e "$f" ]; then '
        'mkdir -p -- "$d" 2>/dev/null; '
        f'if (set -C; echo {_FENCE} > "$f") 2>/dev/null; then {unsure}; fi; '
        f'[ -e "$f" ] || {unsure}; '
        "fi; "
        f'{{ read -r p b g s < "$f"; }} 2>/dev/null || {unsure}; '
        f'if [ "$p" = {_FENCE} ]; then {gone}; fi; '
        f'case "$p" in {_BAD_GROUP}) {unsure};; esac; '
        f'[ -n "$b" ] && [ -n "$g" ] && [ -n "$s" ] || {unsure}; '
        f'[ "$g" = "$t" ] || {unsure}; '
        f'case "$b" in {_UUID}) ;; *) {unsure};; esac; '
        f'[ "$b" = "$c" ] || {gone}; '
        f"{_DARWIN_LIST} || {blind}; "
        'set -f; set -- $o; set +f; w=$(($# / 3)); '
        f"r=$({_DARWIN_GROUP}) || {blind}; "
        "set -f; set -- $r; set +f; "
        f'case "$#:$*" in "3:"[01]" "[01]" "[01]) ;; *) {blind};; esac; '
        f'[ "$1" = 1 ] || {gone}; '
        f'[ "$2" = 0 ] || {unsure}; '
        f'[ "$3" = 1 ] || {unsure}; '
        f'{_LSTART}; [ "$*" = "$s" ] || {unsure}; '
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


def start_script(
    target: RemoteTarget,
    command: Sequence[str],
    env: Mapping[str, str],
) -> str:
    """The remote script that runs command as the job's tagged worker."""
    tag = target.require_tag()
    if not command:
        raise RemoteError("no command to run")
    for name in env:
        if not _ENV_NAME.fullmatch(name) or name == TAG_ENV:
            raise RemoteError(f"environment name {name!r} is not plain")
    assignments = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in sorted(env.items())
    )
    argv = " ".join(shlex.quote(part) for part in command)
    directory = shlex.quote(target.job_dir)
    pid_file = shlex.quote(PID_FILE)
    darwin_file = shlex.quote(darwin_record(tag))
    darwin = (
        f"{_DARWIN_BOOT} || exit {START_REFUSED}; "
        "set -- $(ps -o pgid= -p $$ 2>/dev/null); "
        f'[ "$1" = "$$" ] || exit {START_REFUSED}; '
        f'p=$$; {_LSTART}; s="$*"; [ -n "$s" ] || exit {START_REFUSED}; '
        f'mkdir -p -- "$d" && cd -- "$d" || exit {START_REFUSED}; '
        f'(set -C; echo "$$ $c {tag} $s" > {darwin_file}) 2>/dev/null '
        f"|| exit {START_REFUSED}; "
        f'exec env {assignments} ORCH_JOB_WORKDIR="$d" {argv}'
    )
    inner = (
        f"d={directory}; "
        f"if {_IS_DARWIN}; then {darwin}; fi; "
        f"{_CAN_SCAN} || exit {START_REFUSED}; "
        f"[ -r /proc/self/environ ] || exit {START_REFUSED}; "
        f"read -r b < /proc/sys/kernel/random/boot_id || exit {START_REFUSED}; "
        f'mkdir -p -- "$d" && cd -- "$d" || exit {START_REFUSED}; '
        f'echo "$$ $b" > {pid_file} || exit {START_REFUSED}; '
        f'exec env {assignments} ORCH_JOB_WORKDIR="$d" {argv}'
    )
    return f"exec env {TAG_ENV}={tag} sh -c {shlex.quote(inner)}"


def check_script(target: RemoteTarget) -> str:
    """Exit 1 while the job's worker runs, 0 once it is gone, 3 when unsure."""
    tag = target.require_tag()
    pid_file = shlex.quote(f"{target.job_dir}/{PID_FILE}")
    darwin = _darwin_worker(
        target, gone="exit 0", unsure="exit 3", running="exit 1", blind="exit 3"
    )
    return _sh(
        f"t={tag}; f={pid_file}; "
        f"if {_IS_DARWIN}; then {darwin}; fi; "
        f"{_CAN_SCAN} || exit 3; "
        "[ -r /proc/self/stat ] || exit 3; "
        f'[ -z "$({_TAGGED})" ] || exit 1; '
        '[ -e "$f" ] || exit 0; '
        '{ read -r p b < "$f"; } 2>/dev/null || exit 3; '
        f'case "$p" in {_BAD_GROUP}) exit 3;; esac; '
        '[ -n "$b" ] || exit 3; '
        "read -r c < /proc/sys/kernel/random/boot_id || exit 3; "
        '[ "$b" = "$c" ] || exit 0; '
        "for d in /proc/[0-9]*; do "
        '{ read -r s < "$d/stat"; } 2>/dev/null || continue; '
        "set -- ${s##*) }; "
        '[ "$3" = "$p" ] && exit 3; '
        "done; "
        "exit 0"
    )


def stop_script(target: RemoteTarget) -> str:
    """SIGTERM every process carrying the job's tag, and each group holding one."""
    tag = target.require_tag()
    pid_file = shlex.quote(f"{target.job_dir}/{PID_FILE}")
    darwin = _darwin_worker(
        target,
        gone="exit 0",
        unsure="exit 0",
        running='{ kill -TERM -"$p" 2>/dev/null; exit 0; }',
        blind="exit 3",
    )
    return _sh(
        f"t={tag}; f={pid_file}; "
        f"if {_IS_DARWIN}; then {darwin}; fi; "
        f"{_CAN_SCAN} || exit 3; "
        f"for e in $({_TAGGED}); do "
        "q=${e#/proc/}; q=${q%/environ}; "
        '{ read -r s < "/proc/$q/stat"; } 2>/dev/null || continue; '
        "set -- ${s##*) }; "
        f'case "$3" in {_BAD_GROUP}) ;; *) kill -TERM -"$3" 2>/dev/null;; esac; '
        'kill -TERM "$q" 2>/dev/null; '
        "done; "
        "exit 0"
    )


PROBE_SCRIPT = "exit 0"


class Transport(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> int: ...


class SshTransport:
    """The real transport for checks: the `ssh` on PATH, argv only, never a shell.

    The start itself is spawned by `orch job run` (lib/job_run.py) with the
    argv from `ssh_argv`, so it is heartbeated and gets forwarded signals.
    """

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SSH_UNREACHABLE
        except OSError:
            return SSH_UNREACHABLE
        return completed.returncode


def check_worker(transport: Transport, target: RemoteTarget) -> str:
    """GONE (0), RUNNING (1), UNREACHABLE (ssh's 255) or UNKNOWN (anything else).

    Only GONE lets a slot go. UNKNOWN still proves the device answered.
    The target must carry the start's worker tag.
    """
    code = transport.run(ssh_argv(target, check_script(target)), timeout=PROBE_TIMEOUT_SECONDS)
    if code == 0:
        return GONE
    if code == 1:
        return RUNNING
    if code == SSH_UNREACHABLE:
        return UNREACHABLE
    return UNKNOWN


def stop_worker(transport: Transport, target: RemoteTarget) -> bool:
    """Ask the device to SIGTERM the worker. True when the device answered."""
    code = transport.run(ssh_argv(target, stop_script(target)), timeout=PROBE_TIMEOUT_SECONDS)
    return code == 0


def probe_device(transport: Transport, target: RemoteTarget) -> bool:
    """One SSH round trip over target's alias. True when the device answered."""
    code = transport.run(ssh_argv(target, PROBE_SCRIPT), timeout=PROBE_TIMEOUT_SECONDS)
    return code != SSH_UNREACHABLE
