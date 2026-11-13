"""`orch wait <bot> --autostart`: the waiter starts claimed jobs itself (#80).

Plain `orch wait` claims a job, prints it and exits, and the job starts only
when the bot's model turn notices. A process exit does not wake a bot, so
claims sat until another bot pinged it. With `--autostart` the waiter, which
runs as the owning bot's own user and login, starts the job itself:

* it claims through the whole device pool (issue #85), so the least-loaded
  device takes the job: the box, or a remote device such as the PC or the
  Mac. A remote job runs over the bot's own SSH alias through `orch job
  run` (lib/placed_run.py, lib/remote.py), without `--worktree`;
* it renders the standard brief for the job (lib/brief.py), with the target
  repo's factory.md prose as additional context, read from the repo's
  trusted `factory_ref` in orch.yml (never a PR head) with `gh api` and every
  GitHub token variable unset;
* it writes the brief to `<state>/jobs/<job-id>/brief.md` (mode 600) and
  runs `orch job run <bot> <job-id> --db ... --machines ... [--worktree] --
  <command>` with the brief on stdin and its path in ORCH_JOB_BRIEF, in a new
  session (so a waiter restart never kills a job), output to run.log there;
* it goes straight back to waiting, and reaps each `orch job run` it started.

A remote device has no orch.db, so the harness there cannot run `orch job
done`. Its brief asks it to end its output with `ORCH-HANDOFF: <comment-id>`
(or `ORCH-HANDOFF: failed`); that output comes back over SSH into run.log,
and when the run ends the waiter runs `orch job done` on the box: done with
that comment id only when `orch job run` exited 0 and the marker is the last
line of this run's output (run.log from the offset where this run began,
skipping the runner's own `orch job run:` lines); failed (for T6) when the
exit is nonzero or the last line is missing, malformed or says failed. A
failed run whose remote worker was started is not failed here: its job stays
running until the poller's device check confirms the worker gone, so T6
never retries it beside a live worker. A job already finished by then is
left alone.

A remote run is recorded in `<job-dir>/remote.json` (pid, process start,
run.log offset) and started with `--status-file <job-dir>/exit`, so a
restarted waiter adopts every remote run still running for its bot and
finishes it from the status file when it ends (gone without one: failed).
The record is removed once the job is finished, so no run is finished twice.

`orch job run` sends the first heartbeat before the harness starts and
records itself on the job's slot, so the job reads `running` within seconds
of the claim. No model turn and no bot-to-bot message sit in between.

The harness command is the bot's own fixed config: a YAML file with `bot`
and `command` (an argv list), owned by the bot's user and writable by no one
else (`load_harness_command`). Nothing from a job row or a comment reaches an
argv: the only job value in the `orch job run` argv is the job id, which must
be a plain token. Everything else goes into the brief, on stdin.

The configured command is passed to `orch job run` as is, and one config
serves every device the bot runs on. On the box, `orch job run` itself adds
the orch state dir (the directory of ORCH_DB, where it exists) as a writable
root to a sandboxed `codex exec` (lib/codex_sandbox.py, issue #103), so its
`orch job done` can write orch.db. On a remote device it runs a codex
command by its bare name from the device's login PATH and keeps each
`--add-dir` only where the device has that directory (a `test -d` guard in
the start script), so no box path is run there.

A job that has no standard brief, or whose start fails, is failed at once
(T6 retries it) and its slot is freed, as `orch wait` does for a job it
could not print. A run that ends while its job is still running (the
harness exited without `orch job done`) hands the job to T6 at once too.

Each job gets exactly one handoff: the claim records this waiter on the
job's slot (DevicePool.claim `autostart`), so only this waiter's own `orch
job run` may run the job and only that run may `orch job done` it (lib/cli.py,
lib/placed_run.py). One waiter runs per bot: it holds the bot's waiter lock
for its whole life (`hold_waiter_lock`), and a plain `orch wait` for the bot
is refused while it does. A waiter that dies between a claim and the start leaves a
claimed job, which the poller requeues after release_claimed_minutes (#77).
"""

from __future__ import annotations

import base64
import errno
import fcntl
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import yaml

from lib.brief import BriefError, render_brief
from lib.device_pool import DevicePool, Placement
from lib.factory_md import FactoryContext, parse_factory_context
from lib.load import load_yaml
from lib.machines_yml import MachinesConfig
from lib.orch_yml import OrchConfig
from lib.queue import Job, QueueError, SQLiteJobQueue
from lib.wait import (
    OWNING_BOTS,
    POLL_SECONDS,
    Locked,
    claim_once,
    lock_backoff,
    retry_locked,
)
from lib.worktrees import pid_alive, process_start

GH_ARGV_PREFIX = ("gh", "api", "-X", "GET")
GH_TIMEOUT_SECONDS = 30.0
GH_TOKEN_VARIABLES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
BRIEF_ENV = "ORCH_JOB_BRIEF"
MAX_COMMAND_PARTS = 64
MAX_CONFIG_BYTES = 64 * 1024

_CONFIG_KEYS = frozenset({"bot", "command"})
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
# The remote harness's last word: a GitHub comment id, a Linear comment id
# (a UUID), or failed. Read from the tail of run.log only.
_MARKER = re.compile(
    r"^ORCH-HANDOFF: ([0-9]{1,20}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|failed)$"
)
MARKER_TAIL_BYTES = 64 * 1024
# Lines `orch job run` itself writes to run.log (lib/cli.py _job_run log()).
RUNNER_LOG_PREFIX = "orch job run: "
# Per remote job, in its job directory: what a restarted waiter needs to
# finish the run (#87 F1.4), and the exit status `orch job run` leaves.
REMOTE_RECORD = "remote.json"
STATUS_FILE = "exit"

Log = Callable[[str], None]


class WaiterError(Exception):
    """The autostart waiter cannot run, or cannot start one job. One line."""


@dataclass(frozen=True)
class HarnessCommand:
    bot: str
    command: tuple[str, ...]


def default_harness_config(bot: str) -> Path:
    return Path.home() / ".config" / "factory" / f"waiter-{bot}.yml"


def default_state_dir(bot: str) -> Path:
    return Path.home() / ".local" / "state" / "factory" / f"waiter-{bot}"


def load_harness_command(path: Path, bot: str) -> HarnessCommand:
    """The bot's own harness argv, from a file only that bot's user can write.

    The file is opened once with O_NOFOLLOW and every check is made on that
    open descriptor (fstat), so the file read is the file checked. Its
    directory must be this user's (or root's) and writable by no one else,
    so nobody else can swap the file between runs.
    """
    try:
        parent = os.stat(path.parent)
    except OSError as exc:
        raise WaiterError(
            f"cannot read harness config directory {path.parent}: {_one_line(exc)}"
        ) from exc
    if not stat.S_ISDIR(parent.st_mode):
        raise WaiterError(f"harness config directory {path.parent} is not a directory")
    if parent.st_uid not in (os.getuid(), 0):
        raise WaiterError(f"harness config directory {path.parent} is not owned by this user")
    if parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise WaiterError(
            f"harness config directory {path.parent} is writable by group or others"
        )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise WaiterError(f"harness config {path} not found") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise WaiterError(f"harness config {path} is a symlink, not a regular file") from None
        raise WaiterError(f"cannot read harness config {path}: {_one_line(exc)}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WaiterError(f"harness config {path} is not a regular file")
        if info.st_uid != os.getuid():
            raise WaiterError(f"harness config {path} is not owned by this user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise WaiterError(f"harness config {path} is writable by group or others")
        if info.st_size > MAX_CONFIG_BYTES:
            raise WaiterError(f"harness config {path} is over {MAX_CONFIG_BYTES} bytes")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise WaiterError(f"cannot read harness config {path}: {_one_line(exc)}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        raise WaiterError(f"harness config {path} is over {MAX_CONFIG_BYTES} bytes")
    try:
        data = load_yaml(raw.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as exc:
        raise WaiterError(f"harness config {path}: invalid YAML: {_one_line(exc)}") from exc
    if not isinstance(data, dict):
        raise WaiterError(f"harness config {path} must be a mapping")
    unknown = sorted(str(key) for key in data if key not in _CONFIG_KEYS)
    if unknown:
        raise WaiterError(f"harness config {path}: unknown field {unknown[0]!r}")
    if "bot" not in data:
        raise WaiterError(f"harness config {path}: missing field 'bot'")
    if data["bot"] != bot:
        raise WaiterError(f"harness config {path} is for {data['bot']!r}, not {bot}")
    command = data.get("command")
    if not isinstance(command, list):
        raise WaiterError(f"harness config {path}: command must be a list (an argv)")
    if not command:
        raise WaiterError(f"harness config {path}: command is empty")
    if len(command) > MAX_COMMAND_PARTS:
        raise WaiterError(f"harness config {path}: command has over {MAX_COMMAND_PARTS} parts")
    for index, part in enumerate(command):
        if not isinstance(part, str):
            raise WaiterError(f"harness config {path}: command[{index}] must be a string")
        if part == "":
            raise WaiterError(f"harness config {path}: command[{index}] is empty")
        if any(ord(character) < 32 or ord(character) == 127 for character in part):
            raise WaiterError(f"harness config {path}: command[{index}] has a control character")
    if command[0].startswith("-"):
        raise WaiterError(f"harness config {path}: command[0] looks like an option")
    return HarnessCommand(bot=bot, command=tuple(command))


def job_run_argv(
    orch: Sequence[str],
    *,
    bot: str,
    job_id: str,
    db: Path,
    machines: Path,
    command: Sequence[str],
    worktree: bool = True,
    status_file: Path | None = None,
) -> list[str]:
    """`orch job run` for one job: fixed flags, the job id, then the bot's command."""
    if bot not in OWNING_BOTS:
        raise WaiterError(f"{bot!r} is not a harness bot")
    if not _JOB_ID.fullmatch(job_id):
        raise WaiterError(f"job id {job_id!r} is not a plain token")
    argv = [*orch, "job", "run", bot, job_id, "--db", str(db), "--machines", str(machines)]
    if worktree:
        argv.append("--worktree")
    if status_file is not None:
        argv += ["--status-file", str(status_file)]
    return [*argv, "--", *command]


def handoff_marker(text: str) -> str | None:
    """The run's final `ORCH-HANDOFF: <id|failed>` line, or None (#87 F1.5).

    The marker counts only as the last line of the harness's output. Blank
    lines and the box runner's own `orch job run: ...` lines (it logs after
    the harness exits) are skipped; any other line after a marker, or a
    malformed last marker, means there is no completion.
    """
    last = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(RUNNER_LOG_PREFIX):
            continue
        last = stripped
    if last is None:
        return None
    match = _MARKER.fullmatch(last)
    return match.group(1) if match is not None else None


def local_only(config: MachinesConfig) -> MachinesConfig:
    """machines.yml with only the box (the `local: true` device)."""
    return replace(config, devices=tuple(device for device in config.devices if device.local))


@dataclass(frozen=True)
class ContextResult:
    context: FactoryContext | None
    ref: str | None
    note: str | None


class ContextSource(Protocol):
    def load(self, repo: str) -> ContextResult: ...


class GhContextSource:
    """factory.md from the repo's trusted factory_ref, read with `gh api`."""

    def __init__(
        self,
        config: OrchConfig | None,
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        environ: Mapping[str, str] | None = None,
        note: str | None = None,
    ):
        self.config = config
        self.run = run
        self.environ = environ if environ is not None else os.environ
        self.note = note

    def load(self, repo: str) -> ContextResult:
        owner, _, name = repo.partition("/")
        if not _REPO.fullmatch(repo) or name in (".", ".."):
            return ContextResult(None, None, f"repo {repo!r} is not owner/name")
        if self.config is None:
            return ContextResult(None, None, self.note or "orch.yml not loaded")
        project = next(
            (item for item in self.config.projects if item.repo.lower() == repo.lower()),
            None,
        )
        if project is None:
            return ContextResult(None, None, f"{repo} is not in orch.yml")
        ref = project.factory_ref
        if ref is None or not _REF.fullmatch(ref):
            return ContextResult(None, None, f"{repo} has no usable factory_ref in orch.yml")
        if not _REPO.fullmatch(project.repo):
            return ContextResult(None, None, f"repo {project.repo!r} is not owner/name")
        argv = [*GH_ARGV_PREFIX, f"repos/{project.repo}/contents/factory.md", "-f", f"ref={ref}"]
        env = {key: value for key, value in self.environ.items() if key not in GH_TOKEN_VARIABLES}
        try:
            done = self.run(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ContextResult(None, None, f"gh api failed: {type(exc).__name__}")
        if done.returncode != 0:
            return ContextResult(None, None, f"gh api exited {done.returncode}")
        try:
            payload = json.loads(done.stdout)
            if not isinstance(payload, dict) or payload.get("encoding") != "base64":
                raise ValueError("not base64")
            encoded = payload.get("content")
            if not isinstance(encoded, str):
                raise ValueError("not base64")
            text = base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return ContextResult(None, None, f"{repo} factory.md is not base64 content")
        context, findings = parse_factory_context(text)
        if context is None:
            errors = [item.message for item in findings if item.severity == "error"]
            return ContextResult(None, None, f"{repo} factory.md: {errors[0] if errors else 'invalid'}")
        if context.config.base_branch != ref:
            return ContextResult(
                None,
                None,
                f"{repo} factory.md base_branch is {context.config.base_branch}, "
                f"not factory_ref {ref}",
            )
        return ContextResult(context, ref, None)


class Child(Protocol):
    pid: int

    def poll(self) -> int | None: ...


class Stop(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


class StopFlag:
    """Set from a signal handler; nothing here takes a lock."""

    def __init__(self) -> None:
        self._set = False

    def set(self, *_args: object) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self._set:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(0.1, left))
        return self._set


class AdoptedRun:
    """An `orch job run` an earlier waiter started, found again after a restart.

    It is not this process's child, so its exit status comes from the status
    file it writes when it ends. Gone with no status file (killed) reads as
    a failure (255).
    """

    def __init__(
        self,
        pid: int,
        start: str | None,
        status_path: Path,
        alive: Callable[[int, str | None], bool],
    ):
        self.pid = pid
        self.start = start
        self.status_path = status_path
        self.alive = alive

    def poll(self) -> int | None:
        code = read_status(self.status_path)
        if code is not None:
            return code
        if self.alive(self.pid, self.start):
            return None
        code = read_status(self.status_path)  # it may have ended just now
        return 255 if code is None else code


def read_status(path: Path) -> int | None:
    """The exit status `orch job run --status-file` wrote, or None."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    try:
        return int(text)
    except ValueError:
        return 255


class Waiter:
    def __init__(
        self,
        *,
        bot: str,
        queue: SQLiteJobQueue,
        pool: DevicePool,
        harness: HarnessCommand,
        context: ContextSource,
        state_dir: Path,
        db: Path,
        machines: Path,
        orch: Sequence[str] = (sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "orch")),
        spawn: Callable[..., Child] = subprocess.Popen,
        log: Log = lambda line: print(line, file=sys.stderr, flush=True),
        environ: Mapping[str, str] | None = None,
        alive: Callable[[int | None, str | None], bool] = pid_alive,
    ):
        if bot not in OWNING_BOTS:
            raise WaiterError(f"{bot!r} is not a harness bot")
        if harness.bot != bot:
            raise WaiterError(f"harness command is for {harness.bot}, not {bot}")
        self.bot = bot
        self.recipient = OWNING_BOTS[bot]
        self.queue = queue
        self.pool = pool
        self.harness = harness
        self.context = context
        self.state_dir = Path(state_dir).absolute()
        self.db = Path(db).absolute()
        self.machines = Path(machines).absolute()
        self.orch = tuple(orch)
        self.spawn = spawn
        self.log = log
        self.environ = dict(environ if environ is not None else os.environ)
        self.alive = alive
        self._children: dict[str, Child] = {}
        # job id -> (its job directory, run.log size when the run started),
        # for runs placed on a remote device
        self._remote: dict[str, tuple[Path, int]] = {}
        self._locked = 0
        self._recovered = False
        self._claimer = (os.getpid(), process_start(os.getpid()))

    def running(self) -> set[str]:
        return set(self._children)

    def step(self) -> str | None:
        """Reap finished runs, then claim and start at most one job."""
        if not self._recovered:
            self._recover()
        self._reap()
        try:
            claimed = claim_once(
                self.queue, self.recipient, self.pool, autostart=self._claimer
            )
        except Locked:
            self._locked += 1
            return None
        self._locked = 0
        if claimed is None:
            return None
        job, placement = claimed
        return self._start(job, placement)

    def serve(self, stop: Stop, *, poll: float = POLL_SECONDS) -> int:
        self.log(
            f"waiting for {self.bot} jobs; each claim starts "
            f"`orch job run ... -- {' '.join(self.harness.command)}`"
        )
        while not stop.is_set():
            if self.step() is not None:
                continue
            stop.wait(lock_backoff(self._locked) if self._locked else poll)
        self._reap()
        if self._children:
            self.log(
                f"stopping; leaving {len(self._children)} running jobs to their orch job run"
            )
        return 0

    def _start(self, job: Job, placement: Placement | None) -> str | None:
        request = job.request
        label = f"job {job.id} ({request.repo}#{request.ticket_id} {request.action})"
        device = (
            self.pool.config.device(placement.device) if placement is not None else None
        )
        remote = device is not None and not device.local
        try:
            job_dir = self._job_dir(job.id)
            argv = job_run_argv(
                self.orch,
                bot=self.bot,
                job_id=job.id,
                db=self.db,
                machines=self.machines,
                command=self.harness.command,
                worktree=not remote,  # worktrees are box-only
                status_file=job_dir / STATUS_FILE if remote else None,
            )
            brief = self._brief(job, remote_device=device.name if remote else None)
            brief_path = job_dir / "brief.md"
            _write_private(brief_path, brief.encode("utf-8"))
            offset = 0
            if remote:
                # Only this run's output counts: whatever run.log already
                # holds is an earlier run's (#87 F1.5).
                (job_dir / STATUS_FILE).unlink(missing_ok=True)
                (job_dir / REMOTE_RECORD).unlink(missing_ok=True)
                offset = _size(job_dir / "run.log")
            child = self._spawn(argv, job_dir, brief_path)
        except (WaiterError, BriefError, OSError, ValueError) as exc:
            self.log(f"{label}: not started: {_one_line(exc)}; failed it for T6")
            self._fail(job)
            return None
        self._children[job.id] = child
        if remote:
            self._remote[job.id] = (job_dir, offset)
            try:
                _write_private(
                    job_dir / REMOTE_RECORD,
                    json.dumps(
                        {
                            "job_id": job.id,
                            "pid": child.pid,
                            "start": process_start(child.pid),
                            "offset": offset,
                        },
                        sort_keys=True,
                    ).encode("utf-8"),
                )
            except OSError as exc:
                self.log(
                    f"{label}: could not record the remote run ({_one_line(exc)}); "
                    "a waiter restart before it ends leaves it to T6"
                )
        where = placement.device if placement is not None else "no device"
        self.log(f"{label}: started orch job run (pid {child.pid}) on {where}")
        return job.id

    def _brief(self, job: Job, *, remote_device: str | None = None) -> str:
        try:
            result = self.context.load(job.request.repo)
        except Exception as exc:  # a context failure never blocks the start
            result = ContextResult(None, None, f"factory.md lookup failed: {type(exc).__name__}")
        return render_brief(
            job,
            bot=self.bot,
            context=result.context,
            context_ref=result.ref,
            context_note=result.note,
            remote_device=remote_device,
        )

    def _job_dir(self, job_id: str) -> Path:
        jobs = self.state_dir / "jobs"
        jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = jobs / job_id
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def _spawn(self, argv: list[str], job_dir: Path, brief_path: Path) -> Child:
        env = dict(self.environ)
        env[BRIEF_ENV] = str(brief_path)
        log_fd = os.open(
            job_dir / "run.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with open(brief_path, "rb") as stdin, os.fdopen(log_fd, "ab") as output:
                log_fd = -1
                return self.spawn(
                    argv,
                    stdin=stdin,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=str(job_dir),
                    start_new_session=True,
                    close_fds=True,
                    shell=False,
                )
        finally:
            if log_fd >= 0:
                os.close(log_fd)

    def _fail(self, job: Job) -> None:
        try:
            retry_locked(
                lambda: self.queue.finish(job.id, owner=self.recipient, handoff_comment_id=None)
            )
            retry_locked(lambda: self.pool.release(job.id, owner=self.recipient))
        except (QueueError, sqlite3.Error, OSError) as exc:
            self.log(
                f"job {job.id}: could not fail it ({_one_line(exc)}); "
                "the poller requeues an unstarted claim"
            )

    def _recover(self) -> None:
        """Adopt remote runs an earlier waiter started and did not finish (#87 F1.4).

        Each remote run leaves remote.json in its job directory until the
        box finishes it. A job still running for this bot is watched again
        (its status file gives the exit status); any other record is stale
        and is removed.
        """
        jobs = self.state_dir / "jobs"
        try:
            records = sorted(jobs.glob(f"*/{REMOTE_RECORD}")) if jobs.is_dir() else []
        except OSError as exc:
            self.log(f"cannot look for remote runs to adopt: {_one_line(exc)}")
            return
        for path in records:
            job_dir = path.parent
            job_id = job_dir.name
            if job_id in self._children or not _JOB_ID.fullmatch(job_id):
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                pid = record["pid"]
                start = record.get("start")
                offset = record.get("offset", 0)
                if (
                    record.get("job_id") != job_id
                    or not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or pid < 1
                    or not isinstance(offset, int)
                    or offset < 0
                    or (start is not None and not isinstance(start, str))
                ):
                    raise ValueError("bad record")
            except (OSError, UnicodeError, ValueError, KeyError, TypeError):
                self.log(f"job {job_id}: unreadable {REMOTE_RECORD}; left to T6")
                path.unlink(missing_ok=True)
                continue
            try:
                state = retry_locked(lambda: self._job_state(job_id))
            except (QueueError, sqlite3.Error, OSError) as exc:
                self.log(f"cannot read job {job_id} to adopt it: {_one_line(exc)}")
                return  # try again at the next step
            if state != ("running", self.recipient):
                path.unlink(missing_ok=True)
                continue
            self._children[job_id] = AdoptedRun(
                pid, start, job_dir / STATUS_FILE, self.alive
            )
            self._remote[job_id] = (job_dir, offset)
            self.log(f"job {job_id}: adopted the remote run (pid {pid}) of an earlier waiter")
        self._recovered = True

    def _job_state(self, job_id: str) -> tuple[str, str] | None:
        with self.queue.connect() as connection:
            row = connection.execute(
                "SELECT state, recipient FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def _reap(self) -> None:
        for job_id, child in list(self._children.items()):
            code = child.poll()
            if code is not None:
                del self._children[job_id]
                self.log(f"job {job_id}: orch job run exited {code}")
                remote = self._remote.pop(job_id, None)
                if remote is not None:
                    self._finish_remote(job_id, remote[0], code, offset=remote[1])
                else:
                    self._settle(job_id, child.pid, code)

    def _finish_remote(self, job_id: str, job_dir: Path, code: int, *, offset: int = 0) -> None:
        """`orch job done` on the box for a remote run, from its marker line.

        Done needs both: `orch job run` exited 0, and the last line of this
        run's output (after `offset` in run.log) is a comment-id marker.
        Anything else fails the job for T6 (#87 F1.5), but only when no
        remote worker was started: once one was, the job stays running
        until the poller's device check confirms the worker gone and fails
        it then, so a retry never runs beside it (#87 r2 F2.1).
        """
        try:
            with open(job_dir / "run.log", "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(offset, size - MARKER_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            tail = ""
        marker = handoff_marker(tail)
        handoff = None if code != 0 or marker in (None, "failed") else marker
        if handoff is None:
            try:
                placement = self.pool.placement(job_id)
            except (QueueError, sqlite3.Error, OSError) as exc:
                self.log(f"job {job_id}: could not read its slot ({_one_line(exc)}); left to T6")
                self._forget_remote(job_dir)
                return
            if placement is not None and placement.remote_started:
                self.log(
                    f"job {job_id}: remote run ended without a handoff (exit {code}); "
                    "its worker may still run, so the poller fails it once the device "
                    "confirms it gone"
                )
                self._forget_remote(job_dir)
                return
        try:
            job = retry_locked(
                lambda: self.queue.finish(job_id, owner=self.recipient, handoff_comment_id=handoff)
            )
        except QueueError as exc:
            self.log(f"job {job_id}: not finished from the box ({_one_line(exc)})")
            self._forget_remote(job_dir)
            return
        except (sqlite3.Error, OSError) as exc:
            self.log(f"job {job_id}: orch job done failed ({_one_line(exc)}); T6 times it out")
            self._forget_remote(job_dir)
            return
        self._forget_remote(job_dir)
        if handoff:
            how = f"handoff {handoff}"
        elif code != 0:
            how = f"failed (orch job run exited {code})"
        else:
            how = f"failed ({marker or 'no final ORCH-HANDOFF line'})"
        self.log(f"job {job_id}: ran orch job done on the box: {job.state}, {how}")

    def _forget_remote(self, job_dir: Path) -> None:
        try:
            (job_dir / REMOTE_RECORD).unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"could not remove {job_dir / REMOTE_RECORD}: {_one_line(exc)}")

    def _settle(self, job_id: str, runner: int, code: int) -> None:
        """A run that ended while its job still runs hands the job to T6 now.

        `orch job run` does this itself when its command exits without `orch
        job done`; this catches a runner that died first (killed, crashed).
        A job that another live runner still runs is left to it.

        Codex PR #82 r2 F2.1: the job becomes retryable only once its runner
        and its worker are both gone. A runner SIGKILLed while its harness
        still runs leaves the job running: failing it now would let T6 start
        attempt 2 beside the live harness. The poller's DEAD path (runner
        gone and worker confirmed gone, lib/job_state.py) fails it once the
        harness ends, so the retry comes then, exactly once.
        """
        try:
            with self.queue.connect() as connection:
                row = connection.execute(
                    "SELECT state, recipient FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
            placement = self.pool.placement(job_id)
        except (QueueError, sqlite3.Error, OSError) as exc:
            self.log(f"job {job_id}: could not read it after its run ({_one_line(exc)})")
            return
        if row is None or row["state"] != "running" or row["recipient"] != self.recipient:
            return
        if (
            placement is not None
            and placement.runner_pid is not None
            and placement.runner_pid != runner
            and self.alive(placement.runner_pid, placement.runner_start)
        ):
            self.log(f"job {job_id}: still run by pid {placement.runner_pid}; left to it")
            return
        if placement is not None and placement.remote_started:
            self.log(
                f"job {job_id}: orch job run exited {code}; its remote worker may still "
                "run, so the poller fails it once the device confirms it gone"
            )
            return
        if (
            placement is not None
            and placement.worker_pid is not None
            and self.alive(placement.worker_pid, placement.worker_start)
        ):
            self.log(
                f"job {job_id}: orch job run exited {code} but its worker (pid "
                f"{placement.worker_pid}) still runs; not failed while it runs, "
                "the poller's dead-job check fails it for T6 once both are gone"
            )
            return
        try:
            retry_locked(
                lambda: self.queue.finish(job_id, owner=self.recipient, handoff_comment_id=None)
            )
        except QueueError:
            return  # it was finished between the read and the write
        except (sqlite3.Error, OSError) as exc:
            self.log(f"job {job_id}: could not fail it ({_one_line(exc)})")
            return
        self.log(
            f"job {job_id}: orch job run exited {code} without orch job done; "
            "failed it for T6"
        )
        if placement is None:
            return
        workers = (
            (placement.runner_pid, placement.runner_start),
            (placement.worker_pid, placement.worker_start),
        )
        if any(pid is not None and self.alive(pid, start) for pid, start in workers):
            return  # the DeviceMonitor frees it once the worker is gone
        try:
            retry_locked(lambda: self.pool.release(job_id, owner=self.recipient))
        except (QueueError, sqlite3.Error, OSError) as exc:
            self.log(f"job {job_id}: could not free its slot ({_one_line(exc)})")


def _size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except FileNotFoundError:
        return 0


def lock_path(db: Path, bot: str) -> Path:
    """The bot's waiter lock, beside the queue it claims from."""
    return Path(db).absolute().parent / f"waiter-{bot}.lock"


def hold_waiter_lock(db: Path, bot: str) -> int:
    """Take the bot's waiter lock for this process's whole life; return its fd.

    One autostart waiter per bot and queue: a second one (say, left by a
    supervisor that was SIGKILLed while its waiter lived on) is refused
    here. A plain `orch wait` for the bot holds the lock shared, so the two
    never claim the same bot's jobs at once. The fd is close-on-exec, so no
    `orch job run` child inherits it.
    """
    path = lock_path(db, bot)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WaiterError(_lock_holder(fd, path, bot)) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except BaseException:
        os.close(fd)
        raise
    return fd


def share_waiter_lock(db: Path, bot: str) -> int:
    """Plain `orch wait`: hold the bot's waiter lock shared, or refuse.

    Refused while the bot's autostart waiter runs: it claims and starts the
    bot's jobs itself, so a bot turn must not claim them too.
    """
    path = lock_path(db, bot)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _pid_in(fd)
        os.close(fd)
        raise WaiterError(
            f"{bot}'s autostart waiter (pid {holder or 'unknown'}) holds {path}: it claims "
            f"and starts {bot}'s jobs itself, so a second claim is refused"
        ) from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock_holder(fd: int, path: Path, bot: str) -> str:
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _pid_in(fd)
        return (
            f"another autostart waiter for {bot} (pid {holder or 'unknown'}) holds {path}; "
            "refusing to start a second one"
        )
    fcntl.flock(fd, fcntl.LOCK_UN)
    return f"a plain orch wait for {bot} holds {path}; refusing to autostart beside it"


def _pid_in(fd: int) -> int | None:
    try:
        text = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
