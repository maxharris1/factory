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
another process's environment) the same contract holds through the record:

* start: refuse (125) unless `sysctl -n kern.bootsessionuuid` gives the boot
  session id and the start leads its own process group (`ps -o pgid=`);
  record `<process group> <boot session> <tag> <leader start time>` (the
  leader's `ps -o lstart=` in the C locale and UTC) and exec as above;
* check: 0 with no record, a record from an earlier boot session or written
  by another start (another tag), or no process left in the recorded group
  (`ps -A -o pgid=,pid=,uid=`); 1 while the group holds only this user's
  processes and its leader is the recorded one or has exited (a group number
  is never reused while the group has members, so a leaderless group is the
  job's children); 3 for a malformed record, a group holding another user's
  process, or a leader whose start time differs (the number was reused), and
  3 when there is no boot session id or ps lists nothing;
* stop: SIGTERM to the recorded group exactly when the check would exit 1;
  nothing else is signalled. A process that left the job's group (setsid) is
  invisible here: macOS gives no way to find it by its tag.

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
_DARWIN_BOOT = 'c=$(sysctl -n kern.bootsessionuuid 2>/dev/null) && [ -n "$c" ]'
# A process's start time, one space between words, in a fixed locale and zone.
_LSTART = 'x=$(LC_ALL=C TZ=UTC0 ps -o lstart= -p "$p" 2>/dev/null); set -f; set -- $x; set +f'
# Prints `<lines> <found> <foreign> <leader>` for group $p: how many lines ps
# listed, whether any process is in the group, whether one belongs to another
# user, and whether the leader (pid $p) is among them.
_DARWIN_GROUP = (
    'ps -A -o pgid= -o pid= -o uid= 2>/dev/null | awk -v p="$p" -v u="$u" '
    "'$1 == p { n = 1; if ($3 != u) o = 1; if ($2 == p) k = 1 } "
    "END { print NR, n + 0, o + 0, k + 0 }'"
)


def _darwin_worker(*, gone: str, unsure: str, running: str, blind: str) -> str:
    """Darwin: decide whether the record in $f names a live worker of tag $t.

    gone: no record, a record from an earlier boot session or for another
    start, or no process left in the recorded group. unsure: a malformed
    record, a group holding another user's process, or a leader whose start
    time is not the recorded one (the number was reused). running: the
    recorded group still holds only this user's processes and either its
    leader is the recorded one or the leader has exited (a group number is
    never reused while the group has members). blind: the device cannot
    tell (no boot session id, ps listed nothing).
    """
    return (
        f"{_DARWIN_BOOT} || {blind}; "
        f"u=$(id -u) || {blind}; "
        f'[ -e "$f" ] || {gone}; '
        f'{{ read -r p b g s < "$f"; }} 2>/dev/null || {unsure}; '
        f'case "$p" in {_BAD_GROUP}) {unsure};; esac; '
        f'[ -n "$b" ] && [ -n "$g" ] && [ -n "$s" ] || {unsure}; '
        f'[ "$b" = "$c" ] || {gone}; '
        f'[ "$g" = "$t" ] || {gone}; '
        f"r=$({_DARWIN_GROUP}); set -- $r; "
        f'[ "${{1:-0}}" -gt 0 ] || {blind}; '
        f'[ "$2" = 1 ] || {gone}; '
        f'[ "$3" = 0 ] || {unsure}; '
        f'if [ "$4" = 1 ]; then {_LSTART}; [ "$*" = "$s" ] || {unsure}; fi; '
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
    darwin = (
        f"{_DARWIN_BOOT} || exit {START_REFUSED}; "
        "set -- $(ps -o pgid= -p $$ 2>/dev/null); "
        f'[ "$1" = "$$" ] || exit {START_REFUSED}; '
        f'p=$$; {_LSTART}; s="$*"; [ -n "$s" ] || exit {START_REFUSED}; '
        f'mkdir -p -- "$d" && cd -- "$d" || exit {START_REFUSED}; '
        f'echo "$$ $c {tag} $s" > {pid_file} || exit {START_REFUSED}; '
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
    darwin = _darwin_worker(gone="exit 0", unsure="exit 3", running="exit 1", blind="exit 3")
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
