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
that comment id, or failed (for T6) when the line is missing, malformed or
says failed. A job already finished by then is left alone.

`orch job run` sends the first heartbeat before the harness starts and
records itself on the job's slot, so the job reads `running` within seconds
of the claim. No model turn and no bot-to-bot message sit in between.

The harness command is the bot's own fixed config: a YAML file with `bot`
and `command` (an argv list), owned by the bot's user and writable by no one
else (`load_harness_command`). Nothing from a job row or a comment reaches an
argv: the only job value in the `orch job run` argv is the job id, which must
be a plain token. Everything else goes into the brief, on stdin.

A job that has no standard brief, or whose start fails, is failed at once
(T6 retries it) and its slot is freed, as `orch wait` does for a job it
could not print. A waiter that dies between a claim and the start leaves a
claimed job, which the poller requeues after release_claimed_minutes (#77).
"""

from __future__ import annotations

import base64
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
    """The bot's own harness argv, from a file only that bot's user can write."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise WaiterError(f"harness config {path} not found") from None
    except OSError as exc:
        raise WaiterError(f"cannot read harness config {path}: {_one_line(exc)}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise WaiterError(f"harness config {path} is not a regular file")
    if info.st_uid != os.getuid():
        raise WaiterError(f"harness config {path} is not owned by this user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise WaiterError(f"harness config {path} is writable by group or others")
    try:
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
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
) -> list[str]:
    """`orch job run` for one job: fixed flags, the job id, then the bot's command."""
    if bot not in OWNING_BOTS:
        raise WaiterError(f"{bot!r} is not a harness bot")
    if not _JOB_ID.fullmatch(job_id):
        raise WaiterError(f"job id {job_id!r} is not a plain token")
    argv = [*orch, "job", "run", bot, job_id, "--db", str(db), "--machines", str(machines)]
    if worktree:
        argv.append("--worktree")
    return [*argv, "--", *command]


def handoff_marker(text: str) -> str | None:
    """The last valid `ORCH-HANDOFF: <id|failed>` line in text, or None."""
    found = None
    for line in text.splitlines():
        match = _MARKER.fullmatch(line.strip())
        if match is not None:
            found = match.group(1)
    return found


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
        self._children: dict[str, Child] = {}
        # job id -> its job directory, for runs placed on a remote device
        self._remote: dict[str, Path] = {}
        self._locked = 0

    def running(self) -> set[str]:
        return set(self._children)

    def step(self) -> str | None:
        """Reap finished runs, then claim and start at most one job."""
        self._reap()
        try:
            claimed = claim_once(self.queue, self.recipient, self.pool)
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
            argv = job_run_argv(
                self.orch,
                bot=self.bot,
                job_id=job.id,
                db=self.db,
                machines=self.machines,
                command=self.harness.command,
                worktree=not remote,  # worktrees are box-only
            )
            brief = self._brief(job, remote_device=device.name if remote else None)
            job_dir = self._job_dir(job.id)
            brief_path = job_dir / "brief.md"
            _write_private(brief_path, brief.encode("utf-8"))
            child = self._spawn(argv, job_dir, brief_path)
        except (WaiterError, BriefError, OSError, ValueError) as exc:
            self.log(f"{label}: not started: {_one_line(exc)}; failed it for T6")
            self._fail(job)
            return None
        self._children[job.id] = child
        if remote:
            self._remote[job.id] = job_dir
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

    def _reap(self) -> None:
        for job_id, child in list(self._children.items()):
            code = child.poll()
            if code is not None:
                del self._children[job_id]
                self.log(f"job {job_id}: orch job run exited {code}")
                job_dir = self._remote.pop(job_id, None)
                if job_dir is not None:
                    self._finish_remote(job_id, job_dir)

    def _finish_remote(self, job_id: str, job_dir: Path) -> None:
        """`orch job done` on the box for a remote run, from its marker line."""
        try:
            with open(job_dir / "run.log", "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - MARKER_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            tail = ""
        marker = handoff_marker(tail)
        handoff = None if marker in (None, "failed") else marker
        try:
            job = retry_locked(
                lambda: self.queue.finish(job_id, owner=self.recipient, handoff_comment_id=handoff)
            )
        except QueueError as exc:
            self.log(f"job {job_id}: not finished from the box ({_one_line(exc)})")
            return
        except (sqlite3.Error, OSError) as exc:
            self.log(f"job {job_id}: orch job done failed ({_one_line(exc)}); T6 times it out")
            return
        how = f"handoff {handoff}" if handoff else f"failed ({marker or 'no ORCH-HANDOFF line'})"
        self.log(f"job {job_id}: ran orch job done on the box: {job.state}, {how}")


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o600)


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
