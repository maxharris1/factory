"""SSH connection reuse for calls to remote devices (issue #222).

Every `ssh_argv` call from `lib/remote.py` `SshTransport` goes through one
ControlMaster per (ssh alias, bot user), so the probes, preflight, checkout,
launch, polls and stops for a device share one login instead of opening a
new one each time.

* The control socket is `<control dir>/<16 hex of sha256(alias NUL user)>`.
  The control dir is `$ORCH_SSH_CONTROL_DIR`, else `/tmp/orch-ssh-<uid>`,
  made mode 0700. Before each use it must be a real directory (not a
  symlink) owned by this user with no group or other bits; otherwise the
  call runs exactly as it would with no multiplexing.
* Data calls carry `-o ControlMaster=no -o ControlPath=<socket>`. ssh uses a
  live master, and falls back to its own plain login when the socket is
  missing, stale or refuses the session, so a bad socket never fails a call.
* Before each call, under an flock on `<socket>.lock`, `ssh -O check` asks
  the master whether it is alive, then a no-op session (`PROBE_COMMAND`)
  through it, bounded by `PROBE_TIMEOUT_SECONDS`, proves its connection to
  the device still carries sessions: `-O check` only reaches the local
  master process, which outlives a TCP path lost across a box VM pause.
  When either fails (a pause, a reboot, a killed master, a deleted socket),
  the master is told to exit, a stale socket owned by this user is removed
  and one master is started with `-N -f` and `ControlPersist`. That rebuild
  happens inside the transport, before the command is sent: callers see one
  ordinary call, and the command itself runs at most once.
* When the master cannot be started (the device is down), the call runs
  once as a plain login, exactly as it did before multiplexing, so a real
  drop still ends 255. When a call through a master ends 255 or times out,
  its result is returned unchanged and the master is told to stop, so the
  next call builds a new one. Nothing is ever retried.
* `ORCH_SSH_MUX=off` turns all of this off.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

CONTROL_PERSIST_SECONDS = 600
CONTROL_TIMEOUT_SECONDS = 10.0
MASTER_TIMEOUT_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 10.0
RETIRE_WAIT_SECONDS = 2.0
PROBE_COMMAND = "exit 0 # orch-ssh-mux-probe"
UNREACHABLE = 255

Runner = Callable[..., subprocess.CompletedProcess]


def socket_path(directory: str, alias: str, user: str) -> str:
    """The control socket for one alias and user: short, and names neither."""
    return os.path.join(directory, hashlib.sha256(f"{alias}\0{user}".encode()).hexdigest()[:16])


@dataclass(frozen=True)
class Prepared:
    """The argv to run, and the socket of the master it goes through, if any."""

    argv: list[str]
    socket: str | None = None
    alias: str = ""
    user: str = ""


class Multiplexer:
    def __init__(
        self,
        *,
        binary: str,
        options: Sequence[str],
        environ: Mapping[str, str] | None = None,
        runner: Runner = subprocess.run,
    ):
        self.binary = binary
        self.options = list(options)
        self.environ = os.environ if environ is None else environ
        self.runner = runner

    def prepare(self, argv: Sequence[str]) -> Prepared:
        """argv through a live (or freshly built) master, else argv as given."""
        argv = list(argv)
        login = self._login(argv)
        if login is None or self.environ.get("ORCH_SSH_MUX", "").lower() == "off":
            return Prepared(argv)
        directory = self._control_dir()
        if directory is None:
            return Prepared(argv)
        alias, user = login
        socket = socket_path(directory, alias, user)
        try:
            with self._locked(socket):
                if not self._live(socket, alias, user) and not self._rebuild(socket, alias, user):
                    return Prepared(argv)
        except OSError:
            return Prepared(argv)
        return Prepared(
            [argv[0], "-o", "ControlMaster=no", "-o", f"ControlPath={socket}", *argv[1:]],
            socket,
            alias,
            user,
        )

    def settle(self, prepared: Prepared, code: int | None) -> None:
        """After a call: a 255 or a timeout (None) through a master stops that master."""
        if prepared.socket is not None and code in (None, UNREACHABLE):
            self._control(prepared.socket, "stop", prepared.alias, prepared.user)

    def _login(self, argv: list[str]) -> tuple[str, str] | None:
        """(alias, user) when argv is exactly `ssh_argv`'s shape."""
        head = [self.binary, *self.options]
        rest = argv[len(head):]
        if argv[: len(head)] != head or len(rest) != 5 or rest[0] != "-l" or rest[2] != "--":
            return None
        user, alias = rest[1], rest[3]
        if not user or not alias or alias.startswith("-"):
            return None
        return alias, user

    def _control_dir(self) -> str | None:
        directory = self.environ.get("ORCH_SSH_CONTROL_DIR") or f"/tmp/orch-ssh-{os.getuid()}"
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            info = os.lstat(directory)
        except OSError:
            return None
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            return None
        return directory

    @contextlib.contextmanager
    def _locked(self, socket: str) -> Iterator[None]:
        fd = os.open(f"{socket}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _live(self, socket: str, alias: str, user: str) -> bool:
        if self._control(socket, "check", alias, user) != 0:
            return False
        if self._probe(socket, alias, user) == 0:
            return True
        self._control(socket, "exit", alias, user)
        deadline = time.monotonic() + RETIRE_WAIT_SECONDS
        while os.path.lexists(socket) and time.monotonic() < deadline:
            time.sleep(0.05)
        return False

    def _probe(self, socket: str, alias: str, user: str) -> int | None:
        argv = [
            self.binary,
            *self.options,
            "-o", "ControlMaster=no",
            "-o", f"ControlPath={socket}",
            "-l", user, "--", alias, PROBE_COMMAND,
        ]
        return self._quiet(argv, PROBE_TIMEOUT_SECONDS)

    def _rebuild(self, socket: str, alias: str, user: str) -> bool:
        try:
            info = os.lstat(socket)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                return False
            os.unlink(socket)
        argv = [
            self.binary,
            *self.options,
            "-o", "ControlMaster=yes",
            "-o", f"ControlPersist={CONTROL_PERSIST_SECONDS}",
            "-o", f"ControlPath={socket}",
            "-N", "-f",
            "-l", user, "--", alias,
        ]
        return self._quiet(argv, MASTER_TIMEOUT_SECONDS) == 0

    def _control(self, socket: str, op: str, alias: str, user: str) -> int | None:
        argv = [self.binary, "-o", f"ControlPath={socket}", "-O", op, "-l", user, "--", alias]
        return self._quiet(argv, CONTROL_TIMEOUT_SECONDS)

    def _quiet(self, argv: list[str], timeout: float) -> int | None:
        try:
            return self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            ).returncode
        except (subprocess.TimeoutExpired, OSError):
            return None
