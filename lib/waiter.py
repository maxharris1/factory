"""`orch wait <bot> --autostart`: the waiter starts claimed jobs itself (#80).

Plain `orch wait` claims a job, prints it and exits, and the job starts only
when the bot's model turn notices. A process exit does not wake a bot, so
claims sat until another bot pinged it. With `--autostart` the waiter, which
runs as the owning bot's own user and login, starts the job itself:

* it claims through the whole device pool (issue #85), so the least-loaded
  device takes the job: the box, or a remote device such as the PC or the
  Mac. A remote job runs over the bot's own SSH alias through `orch job
  run` (lib/placed_run.py, lib/remote.py), without `--worktree`;
* before anything starts it checks every pinned skill file on the placed
  device against skills/skills.lock (lib/skill_preflight.py), and a missing
  or changed file fails the job. A remote factory checkout that is not on
  the box's commit, or missing, or a box checkout behind its fetched
  origin/main (#297) that self-deploy does not hold there, instead marks
  the checkout that lags (the box's own, for a box behind origin/main)
  lagging in device_sync and fails the job unhandled with a
  preflight-lagging event (#357). T6 requeues that same row
  after a backoff, charging no round, attempt or free requeue, and marks the
  ticket stuck device-lagging if it still lags past the bound. A device
  the preflight's ssh cannot reach (`preflight-unreachable`, #460) requeues
  the job at once with no round, attempt or free requeue charged, and backs
  that device off for UNREACHABLE_BACKOFF_SECONDS so the next claim goes to
  a reachable device. A harness login on the placed device that reads not
  logged in (#1334) never starts the harness: it holds that account@device
  for the doctor (lib/account_doctor.py) and requeues the job with no round,
  attempt or free requeue charged;
* it renders the standard brief for the job (lib/brief/), with the target
  repo's factory.md prose as additional context, read from the repo's
  trusted `factory_ref` in orch.yml (never a PR head) with `gh api` and every
  GitHub token variable unset;
* it writes the brief to `<state>/jobs/<job-id>/brief.md` (mode 600) and
  runs `orch job run <bot> <job-id> --db ... --machines ... [--worktree] --
  <command>` with the brief on stdin and its path in ORCH_JOB_BRIEF, in a new
  session (so a waiter restart never kills a job), output to run.log there;
* it goes straight back to waiting, and reaps each `orch job run` it started;
* on its first step and every CLEANUP_SWEEP_SECONDS after, it starts
  `orch cleanup run --bot <bot>` (lib/job_cleanup.py) in a new session,
  never while its last pass still runs. Its output goes to
  `<state>/cleanup.log` between a start line and an exit line, and the
  seven passes before it stay in cleanup.log.1 to cleanup.log.7. Once the
  last CLEANUP_ALERT_AFTER finished passes in those files failed, it raises
  one needs_attention row ("orch", "cleanup-<bot>", "cleanup-failing"), and
  deletes it when the newest finished pass exited 0 (#1764);
* before each claim, whatever jobs it runs (#366), it brings its own box
  factory_root to the deployed box checkout's HEAD with lib/device_deploy.py's
  git steps, then its home's installed skills to that pin with the preflight's
  own hash and install code (#345). It records the result as its (box, bot)
  device_sync row: synced, or refused
  with the reason (retried after HOME_RETRY_SECONDS). A sync that reaches the
  box HEAD while the box is not behind its origin/main clears this bot's
  lagging row. It syncs the checkout again after a box claim commits and
  before the preflight. It runs as this bot's account, so no other account
  writes into this checkout or home.

A remote device has no orch.db, so the harness there cannot run `orch job
done`. Its brief asks it to end its output with `ORCH-HANDOFF: <comment-id>`
(or `ORCH-HANDOFF: failed`); that output is relayed by orch job run's
follower (lib/remote_follow.py) into run.log,
and when the run ends the waiter runs `orch job done` on the box: done with
that comment id only when `orch job run` exited 0, the last marker in the
last 20 lines of this run's output (run.log from the offset where this run
began, skipping blank lines, code fences and the runner's own `orch job
run:` lines; bare or in backticks) names it, and the comment passes the
checks `orch job done --handoff` runs (lib/handoff_check.py, #191, #192);
failed (for T6) when the exit is nonzero, the marker is missing, malformed
or says failed, or the comment does not pass. A
failed run whose remote worker was started is not failed here: its job stays
running until the poller's device check confirms the worker gone, so T6
never retries it beside a live worker. A job already finished by then is
left alone.

A remote run is recorded in `<job-dir>/remote.json` (pid, process start,
run.log offset) and started with `--status-file <job-dir>/exit`, so a
restarted waiter adopts every remote run still running for its bot and
finishes it from the status file when it ends. The waiter locks
`<job-dir>/run.lock` and passes the locked fd to `orch job run`, which holds
it until it exits: an adopted run ends only once its status file exists or
that lock is free (free without a status file: failed) (#318).
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

Drain (#219): while the drain marker (`drain`, the state directory's
waiter-<bot>.drain, which self-heal writes when this waiter runs stale
code) names another commit than the one this waiter loaded, it reaps and
recovers as usual but claims nothing, and writes waiter-<bot>.drain.ack
naming that commit from a step that has already decided not to claim.
Self-heal restarts it once that ack exists; its runs keep going and the
replacement adopts the remote ones (#309). A waiter already on that commit
ignores the marker until an ack names it: then it too claims nothing, so
an ack stays true when the service respawns the waiter that wrote it,
until a service start or self-heal removes the marker (once the waiter no
longer runs stale code).
"""

from __future__ import annotations

import base64
import errno
import fcntl
import functools
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import yaml

from lib.brief import BriefError, render_brief
from lib.device_pool import UNREACHABLE_BACKOFF_SECONDS, DevicePool, Placement, run_account
from lib.dispatch.constants import LOCAL_GATE_WAITING
from lib.factory_md import FactoryContext, parse_factory_context
from lib.load import load_yaml
from lib.machines_yml import Device, MachinesConfig, orch_host
from lib import account_doctor, attention, codex_sandbox, containers, device_deploy, job_token, leftovers
from lib import stacks
from lib.device_monitor import remote_target
from lib.harness_env import ApproverProfileExposed, harness_environ
from lib.handoff_check import (
    SERVER_ERROR,
    Expected,
    Read,
    ReadError,
    expected_for,
    gh_reader,
    ready_at,
    retrying,
)
from lib.handoff_check import problems as handoff_problems
from lib.handoff_marker import handoff_marker
from lib.heal import _webhook
from lib.job_cleanup import ALREADY_RUNNING
from lib.health import LOADED_VERSION, checkout_sha
from lib.orch_yml import Approvals, OrchConfig, approvals, load_orch_yml
from lib.pre_ready import ContextUnavailable, check_environ, checks_for, first_failure
from lib.pre_ready import run as run_check
from lib.pstack_pin import LOCK_PATH, ROOT as FACTORY_CHECKOUT
from lib.queue import (
    LOCAL_GATE_EVENTS,
    LOCAL_GATE_FAILED,
    LOCAL_GATE_PASSED,
    LOCAL_GATE_RETRY,
    LOCAL_GATE_UNAVAILABLE,
    ClaimProofError,
    Job,
    Proof,
    QueueError,
    SQLiteJobQueue,
    job_from_row,
    require_proof,
)
from lib.remote import ByteTransport, CaptureTransport, SshTransport
from lib.scratch import remove as remove_scratch
from lib.self_deploy import git_env, own_repo
from lib import skill_preflight
from lib.skill_preflight import CheckoutLagging, DeviceUnreachable, PreflightError
from lib import device_sync
from lib.job_state import fail_lagging, requeue_signed_out, requeue_unreachable
from lib.telemetry import STATE_DIR_ENV
from lib.wait import (
    OWNING_BOTS,
    POLL_SECONDS,
    Locked,
    claim_once,
    lock_backoff,
    retry_locked,
)
from lib.worktrees import (
    WorktreeError,
    detached_checkout,
    pid_alive,
    pid_gone,
    process_start,
    url_template,
    work_root,
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
MAX_CONFIG_BYTES = 64 * 1024

_CONFIG_KEYS = frozenset({"bot", "command"})
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
MARKER_TAIL_BYTES = 64 * 1024
# Per remote job, in its job directory: what a restarted waiter needs to
# finish the run (#87 F1.4), and the exit status `orch job run` leaves.
REMOTE_RECORD = "remote.json"
REMOTE_PROOF = "remote.proof"
STATUS_FILE = "exit"
RUN_LOCK = "run.lock"
HOME_RETRY_SECONDS = 60.0
CONTAINER_SWEEP_SECONDS = 600.0
CLEANUP_SWEEP_SECONDS = 900.0
CLEANUP_LOG = "cleanup.log"
CLEANUP_LOGS = 8
CLEANUP_STARTED = "cleanup run started "
CLEANUP_EXITED = "cleanup run exited "
CLEANUP_ALERT_AFTER = 4
CLEANUP_ALERT_RETRY_SECONDS = 60.0
_CLEANUP_EXIT = re.compile(rf"{CLEANUP_EXITED}(-?\d+)$")
LOCAL_GATE_RETRY_SECONDS = 600
UNSTARTED_CLAIM = (
    "harness = ? AND autostart_pid = ? AND runner_pid IS NULL "
    "AND worker_pid IS NULL AND COALESCE(remote_started, 0) = 0"
)

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


DRAIN_ENV = "ORCH_WAITER_DRAIN"


def _first_line(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def drain_marker(bot: str, environ: Mapping[str, str] | None = None) -> Path:
    """Where self-heal's drain marker for this bot lives: ORCH_WAITER_DRAIN
    (deploy/waiter-service.sh sets it), else waiter-<bot>.drain in
    ORCH_STATE_DIR or ~/.local/state/factory, self-heal's state directory."""
    env = os.environ if environ is None else environ
    if env.get(DRAIN_ENV):
        return Path(env[DRAIN_ENV])
    state = env.get("ORCH_STATE_DIR") or str(Path.home() / ".local" / "state" / "factory")
    return Path(state) / f"waiter-{bot}.drain"


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
        retry_log: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.run = run
        self.environ = environ if environ is not None else os.environ
        self.note = note
        self.retry_log = retry_log
        self.sleep = sleep

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
        env = {key: value for key, value in self.environ.items() if key not in GH_TOKEN_VARIABLES}

        def read(path: str) -> Mapping[str, object]:
            try:
                done = self.run(
                    [*GH_ARGV_PREFIX, path, "-f", f"ref={ref}"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=GH_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ReadError(
                    f"gh api failed: {type(exc).__name__}",
                    retryable=isinstance(exc, subprocess.TimeoutExpired),
                ) from exc
            if done.returncode != 0:
                raise ReadError(
                    f"gh api exited {done.returncode}",
                    retryable=SERVER_ERROR.search(f"{done.stderr} {done.stdout}") is not None,
                )
            try:
                payload = json.loads(done.stdout)
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}

        if self.retry_log is not None:
            read = retrying(read, self.retry_log, self.sleep, label="factory.md read")
        try:
            payload = read(f"repos/{project.repo}/contents/factory.md")
        except ReadError as exc:
            return ContextResult(None, ref, str(exc))
        try:
            if payload.get("encoding") != "base64":
                raise ValueError("not base64")
            encoded = payload.get("content")
            if not isinstance(encoded, str):
                raise ValueError("not base64")
            text = base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return ContextResult(None, ref, f"{repo} factory.md is not base64 content")
        context, findings = parse_factory_context(text, strict=False)
        if context is None:
            errors = [item.message for item in findings if item.severity == "error"]
            return ContextResult(None, ref, f"{repo} factory.md: {errors[0] if errors else 'invalid'}")
        if context.config.base_branch != ref:
            return ContextResult(
                None,
                ref,
                f"{repo} factory.md base_branch is {context.config.base_branch}, "
                f"not factory_ref {ref}",
            )
        return ContextResult(context, ref, None)


class WaiterTransport(CaptureTransport, ByteTransport, Protocol):
    pass


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
        job_dir: Path,
        gone: Callable[[int, str | None], bool],
    ):
        self.pid = pid
        self.start = start
        self.status_path = job_dir / STATUS_FILE
        self.lock_path = job_dir / RUN_LOCK
        self.gone = gone

    def poll(self) -> int | None:
        code = read_status(self.status_path)
        if code is not None:
            return code
        if self._running():
            return None
        code = read_status(self.status_path)  # it may have ended just now
        return 255 if code is None else code

    def _running(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return not self.gone(self.pid, self.start)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return not self.gone(self.pid, self.start)
        finally:
            os.close(fd)
        return False


def _lock_for_runner(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise WaiterError(f"{path} is held by an earlier run of this job") from exc
    return fd


@dataclass(frozen=True)
class CleanupStreak:
    """failed: finished passes in a row that failed, newest first; 0 when
    the newest finished pass exited 0. error: the last non-blank output line
    of the newest failed pass, "" when failed is 0 or it printed nothing."""

    failed: int
    error: str


def cleanup_streak(passes: Iterable[str]) -> CleanupStreak | None:
    """The streak in the kept cleanup pass files, newest first, or None when
    no pass finished. A pass with no exit line (running, not started, or its
    waiter died) and a pass that exited ALREADY_RUNNING are skipped."""
    failed, error = 0, ""
    for text in passes:
        lines = text.splitlines()
        if lines and lines[0].startswith(CLEANUP_STARTED):
            lines = lines[1:]
        output = [line.strip() for line in lines if line.strip()]
        exited = _CLEANUP_EXIT.search(output[-1]) if output else None
        if exited is None:
            continue
        code = int(exited.group(1))
        if code == ALREADY_RUNNING:
            continue
        if code == 0:
            return CleanupStreak(failed, error)
        if not failed:
            output[-1] = output[-1][:exited.start()].strip()
            error = next((line for line in reversed(output) if line), "")
        failed += 1
    return CleanupStreak(failed, error) if failed else None


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
        gone: Callable[[int, str | None], bool] = pid_gone,
        approvals: Approvals | None = None,
        drain: Path | None = None,
        version: str = LOADED_VERSION,
        orch_yml: Path = FACTORY_CHECKOUT / "orch.yml",
        home: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        transport: WaiterTransport | None = None,
        sweep_cleanup: bool = False,
        cleanup_spawn: Callable[..., Child] | None = None,
        cleanup_alert_after: int = CLEANUP_ALERT_AFTER,
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
        try:
            self.environ = harness_environ(
                environ if environ is not None else os.environ, approvals
            )
        except ApproverProfileExposed as exc:
            raise WaiterError(str(exc)) from exc
        self.alive = alive
        self.gone = gone
        self._children: dict[str, tuple[Child, Proof | None]] = {}
        # job id -> (its job directory, run.log size when the run started),
        # for runs placed on a remote device
        self._remote: dict[str, tuple[Path, int]] = {}
        self._scratch_failures: dict[str, str] = {}
        self.transport = transport if transport is not None else SshTransport()
        self._containers_swept_at = clock()
        self._container_lines: set[str] = set()
        self._leftover_tries: dict[str, dict[str, int]] = {}
        self._locked = 0
        self._recovered = False
        self._claimer = (os.getpid(), process_start(os.getpid()))
        self.drain = drain
        self.version = version
        self.orch_yml = Path(orch_yml)
        self._draining_for: str | None = None
        self.home = home
        self.clock = clock
        self._home_key: tuple[object, ...] | None = None
        self._home_retry_at = 0.0
        self.sweep_cleanup = sweep_cleanup
        self.cleanup_spawn = cleanup_spawn if cleanup_spawn is not None else spawn
        self._cleanup: Child | None = None
        self._cleanup_started_at: float | None = None
        self.cleanup_alert_after = cleanup_alert_after
        self._cleanup_alert_due: float | None = clock()

    def running(self) -> set[str]:
        return set(self._children)

    def step(self) -> str | None:
        """Reap finished runs, then claim and start at most one job."""
        if not self._recovered:
            self._recover()
        self._reap()
        self._sweep_job_folders()
        self._publish_cleanup_alert()
        self._reconcile_home()
        if self._draining():
            return None
        self._retry_waiting_gates()
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
        job, placement, proof = claimed
        return self._start(job, placement, proof)

    def _sweep_job_folders(self) -> None:
        if not self.sweep_cleanup:
            return
        log_path = self.state_dir / CLEANUP_LOG
        if self._cleanup is not None:
            code = self._cleanup.poll()
            if code is None:
                return
            self._cleanup = None
            self._append_cleanup_log(log_path, f"{CLEANUP_EXITED}{code}")
            self.log(f"{CLEANUP_EXITED}{code} ({log_path})")
            self._cleanup_alert_due = self.clock()
        now = self.clock()
        if self._cleanup_started_at is not None and now - self._cleanup_started_at < CLEANUP_SWEEP_SECONDS:
            return
        self._cleanup_started_at = now
        argv = [*self.orch, "cleanup", "run", "--bot", self.bot,
                "--db", str(self.db), "--machines", str(self.machines)]
        names = self._cleanup_logs()
        started = datetime.fromtimestamp(self.queue.now(), timezone.utc).isoformat(timespec="seconds")
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for older, newer in reversed(list(zip(names, names[1:]))):
                try:
                    os.replace(older, newer)
                except FileNotFoundError:
                    pass
            output = open(log_path, "xb", opener=_private_nofollow)
        except OSError as exc:
            self.log(f"cleanup run not started: {_one_line(exc)}")
            return
        try:
            with output:
                output.write(f"{CLEANUP_STARTED}{started}\n".encode())
                output.flush()
                self._cleanup = self.cleanup_spawn(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    cwd=str(self.state_dir),
                    start_new_session=True,
                    close_fds=True,
                    shell=False,
                )
        except OSError as exc:
            self._append_cleanup_log(log_path, f"cleanup run not started: {_one_line(exc)}")
            self.log(f"cleanup run not started: {_one_line(exc)}")

    def _cleanup_logs(self) -> list[Path]:
        log_path = self.state_dir / CLEANUP_LOG
        return [log_path, *(log_path.with_name(f"{CLEANUP_LOG}.{n}") for n in range(1, CLEANUP_LOGS))]

    def _cleanup_pass_tails(self) -> Iterator[str]:
        for path in self._cleanup_logs():
            try:
                with open(path, "rb", opener=_private_nofollow) as handle:
                    handle.seek(0, os.SEEK_END)
                    handle.seek(max(0, handle.tell() - MARKER_TAIL_BYTES))
                    tail = handle.read()
            except OSError:
                continue
            yield tail.decode("utf-8", errors="replace")

    def _publish_cleanup_alert(self) -> None:
        """Raise, update or delete this bot's cleanup-failing row from the
        kept pass files. Never raises: it runs in the loop that claims jobs."""
        due = self._cleanup_alert_due
        if not self.sweep_cleanup or due is None or self.clock() < due:
            return
        self._cleanup_alert_due = None
        streak = cleanup_streak(self._cleanup_pass_tails())
        if streak is None or 0 < streak.failed < self.cleanup_alert_after:
            return
        key = ("orch", f"cleanup-{self.bot}", attention.CLEANUP_FAILING)
        detail = f"the last {streak.failed} cleanup passes failed" + (
            f": {streak.error}" if streak.error else " with no output")
        try:
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if streak.failed:
                    changed = attention.raise_flag(connection, key, detail, self.queue.now())
                else:
                    changed = attention.clear_flag(connection, key)
        except (QueueError, sqlite3.Error, OSError) as exc:
            self._cleanup_alert_due = self.clock() + CLEANUP_ALERT_RETRY_SECONDS
            self.log(f"{attention.CLEANUP_FAILING} not written: {_one_line(exc)}")
            return
        if changed and streak.failed:
            self.log(f"{attention.CLEANUP_FAILING} raised: {detail}")
        elif changed:
            self.log(f"{attention.CLEANUP_FAILING} cleared: a cleanup pass exited 0")

    def _append_cleanup_log(self, log_path: Path, line: str) -> None:
        try:
            with open(log_path, "ab", opener=_private_nofollow) as output:
                output.write(f"{line}\n".encode())
        except OSError as exc:
            self.log(f"{log_path}: could not add {line!r} ({_one_line(exc)})")

    def _reconcile_home(self) -> None:
        if self.home is None:
            return
        device = next((d for d in self.pool.config.devices if d.local), None)
        name = orch_host(self.pool.config)
        checkout = self._factory_root(device)
        if checkout is None:
            return
        root = Path(checkout)
        target = checkout_sha(FACTORY_CHECKOUT)
        if self._home_state(root, target) == self._home_key or self.clock() < self._home_retry_at:
            return
        detail = self._sync_checkout(checkout, target)
        head = checkout_sha(root)
        if not detail:
            detail = self._install_home(root, checkout)
        state = device_sync.REFUSED if detail else device_sync.SYNCED
        clears_lagging = (head == checkout_sha(FACTORY_CHECKOUT)
                          and skill_preflight.box_behind_origin(
                              name, self.bot, str(FACTORY_CHECKOUT), self._box_held()) is None)

        def write() -> None:
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = next((row.state for row in device_sync.read(connection)
                                if (row.device, row.bot) == (name, self.bot)), None)
                if not (state == device_sync.SYNCED and current == device_sync.LAGGING
                        and not clears_lagging):
                    device_sync.upsert(
                        connection, device=name, bot=self.bot, harness=self.recipient,
                        checkout=checkout, target=target, sha=head, state=state,
                        detail=detail, now=self.queue.now(),
                    )
                connection.commit()

        try:
            retry_locked(write)
        except (QueueError, sqlite3.Error, OSError) as exc:
            self.log(f"skills in {self.home}: sync state not written ({_one_line(exc)})")
            self._home_retry_at = self.clock() + HOME_RETRY_SECONDS
            return
        if detail:
            self.log(f"skills in {self.home}: {detail}; retry in {HOME_RETRY_SECONDS:.0f}s")
            self._home_retry_at = self.clock() + HOME_RETRY_SECONDS
        else:
            self.log(f"skills in {self.home}: at the pin of {checkout} {(head or '?')[:12]}")
            self._home_key = self._home_state(root, target)

    @staticmethod
    def _home_state(root: Path, target: str | None) -> tuple[object, ...]:
        head = checkout_sha(root)
        try:
            lock = (root / LOCK_PATH).stat()
        except OSError as exc:
            return (target, head, type(exc).__name__)
        return (target, head, lock.st_mtime_ns, lock.st_size)

    def _sync_checkout(self, checkout: str, target: str | None) -> str:
        if target is None:
            return f"cannot read the HEAD of {FACTORY_CHECKOUT}"
        if checkout_sha(Path(checkout)) == target:
            return ""
        refused = device_deploy.sync_local(
            checkout, url=url_template(None).format(repo=own_repo(FACTORY_CHECKOUT) or ""),
            sha=target, environ=git_env(self.environ, _webhook(self.orch_yml)),
        )
        return f"checkout: {refused}" if refused else ""

    def _sync_checkout_to_box_head(self, checkout: str | None, label: str) -> None:
        if self.home is None or checkout is None:
            return
        refused = self._sync_checkout(checkout, checkout_sha(FACTORY_CHECKOUT))
        if refused:
            self.log(f"{label}: {checkout} not at the box HEAD ({refused}); the preflight decides")

    def _install_home(self, root: Path, checkout: str) -> str:
        try:
            pinned = skill_preflight.pinned_files(root)
            expected = skill_preflight.wanted(skill_preflight.skills_dir(self.bot), checkout, pinned)
            found = skill_preflight.local_hashes(self.home, sorted(expected))
            errors: list[str] = []
            if skill_preflight.home_install_stale(expected, found):
                errors = skill_preflight.install_into_home(root, self.home)
                found = skill_preflight.local_hashes(self.home, sorted(expected))
            error = skill_preflight.compare(expected, found)
        except (OSError, ValueError, UnicodeError) as exc:
            return f"install-skills: {_one_line(exc)}"
        if error is None:
            return ""
        return f"install-skills: {errors[0]}" if errors else error

    def _draining(self) -> bool:
        """The drain marker names another commit than this waiter's, or an
        ack names the marker's commit: a waiter that took over from the one
        that acknowledged keeps the drain until the marker goes."""
        target = _first_line(self.drain)
        if target == self.version and _first_line(self._ack_path()) != target:
            target = None
        if target is not None and target != self._draining_for:
            self.log(f"draining for {target}: no new claims until restarted "
                     f"({len(self._children) + len(self._remote)} running jobs)")
        self._draining_for = target
        if target is not None:
            self._acknowledge(target)
        return target is not None

    def _ack_path(self) -> Path | None:
        return None if self.drain is None else self.drain.with_name(self.drain.name + ".ack")

    def _acknowledge(self, target: str) -> None:
        """Tell self-heal this waiter claims nothing more for `target`:
        written only from a step that has already decided not to claim."""
        ack = self._ack_path()
        assert ack is not None
        if _first_line(ack) == target:
            return
        try:
            temp = ack.with_name(f".{ack.name}.{os.getpid()}.tmp")
            temp.write_text(target + "\n", encoding="utf-8")
            os.replace(temp, ack)
        except OSError as exc:
            self.log(f"drain ack not written ({type(exc).__name__})")

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

    def _box_held(self) -> device_sync.Row | None:
        try:
            with self.queue.connect() as connection:
                return device_sync.held(connection, orch_host(self.pool.config))
        except (QueueError, sqlite3.Error, OSError):
            return None

    def _start(self, job: Job, placement: Placement | None, proof: str) -> str | None:
        request = job.request
        label = f"job {job.id} ({request.repo}#{request.ticket_id} {request.action})"
        claim = Proof(proof)
        device = (
            self.pool.config.device(placement.device) if placement is not None else None
        )
        remote = device is not None and not device.local
        factory_root = self._factory_root(device)
        if not remote:
            self._sync_checkout_to_box_head(factory_root, label)
        placed = placement is not None and device is not None
        try:
            account, account_home = run_account(self.pool.config, placement) if placed else (None, None)
            skill_preflight.preflight(
                self.queue, job, bot=self.bot, placement=placement, device=device,
                checkout=factory_root, held=self._box_held(), host=orch_host(self.pool.config),
                account_home=account_home,
            )
            if placed:
                account_doctor.check_login(
                    self.transport, self.pool.config, device, placement.harness, account, self.environ
                )
            marker_finish = remote or codex_sandbox.is_sandboxed_exec(self.harness.command)
            job_dir = self._job_dir(job.id)
            argv = job_run_argv(
                self.orch,
                bot=self.bot,
                job_id=job.id,
                db=self.db,
                machines=self.machines,
                command=self.harness.command,
                worktree=not remote,  # worktrees are box-only
                status_file=job_dir / STATUS_FILE if marker_finish else None,
            )
            brief = self._brief(
                job,
                remote_device=device.name if remote else None,
                factory_root=factory_root,
                handoff_via_marker=marker_finish,
            )
            brief_path = job_dir / "brief.md"
            _write_private(brief_path, brief.encode("utf-8"))
            offset = 0
            lock = -1
            if marker_finish:
                # Only this run's output counts: whatever run.log already
                # holds is an earlier run's (#87 F1.5).
                (job_dir / STATUS_FILE).unlink(missing_ok=True)
                (job_dir / REMOTE_RECORD).unlink(missing_ok=True)
                offset = _size(job_dir / "run.log")
                lock = _lock_for_runner(job_dir / RUN_LOCK)
            try:
                child = self._spawn(argv, job_dir, brief_path, lock, proof)
            finally:
                if lock >= 0:
                    os.close(lock)
        except CheckoutLagging as exc:
            self._fail_lagging(job, placement, factory_root, exc, label, claim)
            return None
        except DeviceUnreachable as exc:
            self._requeue_unreachable(job, exc, label, claim)
            return None
        except account_doctor.SignedOut as exc:
            self._requeue_signed_out(job, exc, label, claim)
            return None
        except (WaiterError, BriefError, PreflightError, OSError, ValueError) as exc:
            self.log(f"{label}: not started: {_one_line(exc)}; failed it for T6")
            self._fail(job, claim)
            return None
        self._children[job.id] = (child, claim)
        if marker_finish:
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
                if claim.token is not None:
                    _write_private(
                        job_dir / REMOTE_PROOF,
                        claim.token.encode("utf-8"),
                    )
            except OSError as exc:
                self.log(
                    f"{label}: could not record the remote run ({_one_line(exc)}); "
                    "a waiter restart before it ends leaves it to T6"
                )
        where = placement.device if placement is not None else "no device"
        self.log(f"{label}: started orch job run (pid {child.pid}) on {where}")
        return job.id

    def _fail_lagging(
        self,
        job: Job,
        placement: Placement | None,
        checkout: str | None,
        exc: CheckoutLagging,
        label: str,
        proof: Proof,
    ) -> None:
        reason = str(exc)
        if placement is None or checkout is None:
            self.log(f"{label}: not started: {reason}; failed it for T6")
            self._fail(job, proof)
            return
        claimer, _ = self._claimer
        lagging = placement.device
        if exc.box_behind_origin:
            lagging = orch_host(self.pool.config)
            if placement.device != lagging:
                checkout = str(skill_preflight.ROOT.resolve())

        def write() -> tuple[str, str] | None:
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                require_proof(connection, job.id, proof)
                now = self.queue.now()
                device_sync.upsert(
                    connection,
                    device=lagging,
                    bot=self.bot,
                    harness=self.recipient,
                    checkout=checkout,
                    target=None,
                    sha=None,
                    state=device_sync.LAGGING,
                    detail=reason,
                    now=now,
                )
                result = fail_lagging(
                    connection,
                    job.id,
                    now=now,
                    reason=reason,
                    slot_guard=UNSTARTED_CLAIM,
                    slot_params=(self.recipient, claimer),
                )
                connection.commit()
                return result

        try:
            result = retry_locked(write)
        except ClaimProofError:
            result = None
        except (QueueError, sqlite3.Error, OSError) as error:
            self.log(f"{label}: not started: {reason}; could not record it ({_one_line(error)})")
            self._fail(job, proof)
            return
        if result is None:
            self.log(f"{label}: not started: {reason}; its claim changed, left alone")
            return
        self.log(f"{label}: not started: {reason}; failed for T6 to requeue after a backoff")

    def _requeue_unreachable(
        self, job: Job, exc: DeviceUnreachable, label: str, proof: Proof
    ) -> None:
        reason = str(exc)
        claimer, _ = self._claimer

        def write() -> tuple[str, str] | None:
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                require_proof(connection, job.id, proof)
                result = requeue_unreachable(
                    connection, job.id, device=exc.device, now=self.queue.now(), reason=reason,
                    slot_guard=UNSTARTED_CLAIM, slot_params=(self.recipient, claimer),
                )
                connection.commit()
                return result

        try:
            result = retry_locked(write)
        except ClaimProofError:
            result = None
        except (QueueError, sqlite3.Error, OSError) as error:
            self.log(f"{label}: not started: {reason}; could not record it ({_one_line(error)})")
            self._fail(job, proof)
            return
        if result is None:
            self.log(f"{label}: not started: {reason}; its claim changed, left alone")
            return
        self.log(f"{label}: not started: {reason}; requeued, {exc.device} backed off "
                 f"for {UNREACHABLE_BACKOFF_SECONDS}s")

    def _requeue_signed_out(
        self, job: Job, exc: account_doctor.SignedOut, label: str, proof: Proof
    ) -> None:
        reason = str(exc)
        claimer, _ = self._claimer

        def write() -> tuple[str, str] | None:
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                require_proof(connection, job.id, proof)
                result = requeue_signed_out(
                    connection, job.id, account=exc.account, device=exc.device, now=self.queue.now(),
                    reason=reason, slot_guard=UNSTARTED_CLAIM, slot_params=(self.recipient, claimer),
                )
                connection.commit()
                return result

        try:
            result = retry_locked(write)
        except ClaimProofError:
            result = None
        except (QueueError, sqlite3.Error, OSError) as error:
            self.log(f"{label}: not started: {reason}; could not record it ({_one_line(error)})")
            self._fail(job, proof)
            return
        if result is None:
            self.log(f"{label}: not started: {reason}; its claim changed, left alone")
            return
        self.log(f"{label}: not started: {reason}; requeued, {exc.account}@{exc.device} signed out, "
                 "held until its login reads signed in")

    def _factory_root(self, device: Device | None) -> str | None:
        """The placed device's deployed factory checkout, never the job's worktree.

        A device names its own; the box falls back to this checkout. The
        skill preflight checks this same checkout."""
        return skill_preflight.checkout_root(device, self.recipient, FACTORY_CHECKOUT)

    def _brief(
        self,
        job: Job,
        *,
        remote_device: str | None = None,
        factory_root: str | None = None,
        handoff_via_marker: bool = False,
    ) -> str:
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
            factory_root=factory_root,
            handoff_via_marker=handoff_via_marker,
        )

    def _job_dir(self, job_id: str) -> Path:
        jobs = self.state_dir / "jobs"
        jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = jobs / job_id
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def _spawn(
        self, argv: list[str], job_dir: Path, brief_path: Path, lock: int, proof: str
    ) -> Child:
        env = dict(self.environ)
        env[job_token.PROOF_ENV] = proof
        env[BRIEF_ENV] = str(brief_path)
        env[STATE_DIR_ENV] = str(job_dir)  # the run's raw telemetry log goes here (#93)
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
                    pass_fds=(lock,) if lock >= 0 else (),
                    shell=False,
                )
        finally:
            if log_fd >= 0:
                os.close(log_fd)

    def _fail(self, job: Job, proof: Proof) -> None:
        try:
            retry_locked(
                lambda: self.queue.finish(
                    job.id, owner=self.recipient, handoff_comment_id=None, proof=proof
                )
            )
            retry_locked(lambda: self.pool.release(job.id, owner=self.recipient, proof=proof))
        except ClaimProofError:
            self.log(
                f"job {job.id}: its claim was released and claimed again; left to the later claim"
            )
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
                (job_dir / REMOTE_PROOF).unlink(missing_ok=True)
                continue
            adopted_proof = _read_private_proof(job_dir / REMOTE_PROOF)
            self._children[job_id] = (AdoptedRun(pid, start, job_dir, self.gone), adopted_proof)
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
        for job_id, (child, proof) in list(self._children.items()):
            code = child.poll()
            if code is not None:
                del self._children[job_id]
                self.log(f"job {job_id}: orch job run exited {code}")
                remote = self._remote.pop(job_id, None)
                if remote is not None:
                    self._finish_remote(
                        job_id, remote[0], code, proof, offset=remote[1], runner=child.pid
                    )
                else:
                    self._settle(job_id, child.pid, code, proof)
        self._remove_scratch_once_workers_exit()
        self._sweep_containers()

    def _sweep_containers(self) -> None:
        now = self.clock()
        if now - self._containers_swept_at < CONTAINER_SWEEP_SECONDS:
            return
        self._containers_swept_at = now
        runners = [(orch_host(self.pool.config), containers.local_runner(), [str(work_root(None))], None)]
        for device in self.pool.config.devices:
            target = remote_target(device, self.recipient, "containers")
            if target is not None:
                roots = [root for harness in device.harnesses if (root := device.workdir_for(harness))]
                runners.append((device.name, containers.remote_runner(self.transport, target), roots, target))
        lines: set[str] = set()

        def note(line: str) -> None:
            if line not in self._container_lines:
                self.log(line)
            lines.add(line)

        ledger = functools.cache(lambda: self._stack_ledger(note))
        held_tags = functools.cache(lambda: self._held_tags(note))
        for name, runner, roots, target in runners:
            for line in containers.sweep(runner, self._job_state_name, name):
                note(line)
            for line in stacks.sweep(runner, ledger, name, roots):
                note(line)
            if target is not None:
                for line in leftovers.sweep(
                    runner, target, self._job_state_name, held_tags, self._leftover_tries.setdefault(name, {})
                ):
                    note(line)
        self._container_lines = lines

    def _held_tags(self, note: Log) -> frozenset[str] | None:
        try:
            held = retry_locked(self.pool.held)
        except (QueueError, sqlite3.Error, OSError) as exc:
            note(f"runs kept, orch.db is unreadable ({_one_line(exc)})")
            return None
        return frozenset(slot.placement.worker_tag for slot in held if slot.placement.worker_tag)

    def _stack_ledger(self, note: Log) -> stacks.Ledger | None:
        def read() -> stacks.Ledger:
            with self.queue.connect() as connection:
                return stacks.read_ledger(connection, live=set(self._children))

        try:
            return retry_locked(read)
        except (QueueError, sqlite3.Error, OSError) as exc:
            note(f"stacks kept, orch.db is unreadable ({_one_line(exc)})")
            return None

    def _job_state_name(self, job_id: str) -> str | None:
        try:
            state = retry_locked(lambda: self._job_state(job_id))
        except (QueueError, sqlite3.Error, OSError) as exc:
            self.log(f"job {job_id}: what it left is kept, its state is unreadable ({_one_line(exc)})")
            return "unreadable"
        return None if state is None else state[0]

    def _remove_scratch_once_workers_exit(self) -> None:
        jobs = self.state_dir / "jobs"
        failures: dict[str, str] = {}
        for path in sorted(jobs.glob("*/tmp")) if jobs.is_dir() else []:
            job_id = path.parent.name
            if not _JOB_ID.fullmatch(job_id) or job_id in self._children:
                continue
            try:
                placement = self.pool.placement(job_id)
            except (QueueError, sqlite3.Error, OSError) as exc:
                self.log(f"job {job_id}: scratch kept, its placement is unreadable ({_one_line(exc)})")
                continue
            if placement is not None and not placement.remote_started and any(
                pid is not None and self.alive(pid, start)
                for pid, start in ((placement.runner_pid, placement.runner_start),
                                   (placement.worker_pid, placement.worker_start))
            ):
                continue
            failure = remove_scratch(path)
            if failure is not None:
                if failure != self._scratch_failures.get(job_id):
                    self.log(f"job {job_id}: {failure}")
                failures[job_id] = failure
        self._scratch_failures = failures

    def _finish_remote(
        self,
        job_id: str,
        job_dir: Path,
        code: int,
        proof: Proof | None,
        *,
        offset: int = 0,
        runner: int | None = None,
    ) -> None:
        """`orch job done` on the box for a remote run, from its marker line.

        Done needs both: `orch job run` exited 0, and the marker that decides
        this run's output (after `offset` in run.log, lib/handoff_marker.py)
        names a comment id.
        Anything else fails the job for T6 (#87 F1.5), but only when no
        worker is still running: once a remote worker was started, or while a
        local runner/worker is still alive (sandboxed local Codex runs settle
        via marker too), the job stays running until the poller confirms the
        worker gone and fails it then, so a retry never runs beside it (#87 r2 F2.1).
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
        refused = None if handoff is None else self._refused_handoff(job_id, handoff, proof)
        if refused is not None:
            handoff = None
        placement: Placement | None = None
        if handoff is None:
            try:
                placement = self.pool.placement(job_id)
            except (QueueError, sqlite3.Error, OSError) as exc:
                self.log(f"job {job_id}: could not read its slot ({_one_line(exc)}); left to T6")
                self._forget_remote(job_dir)
                return
            if (
                placement is not None
                and placement.runner_pid is not None
                and (runner is None or placement.runner_pid != runner)
                and self.alive(placement.runner_pid, placement.runner_start)
            ):
                self.log(f"job {job_id}: still run by pid {placement.runner_pid}; left to it")
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
                self._forget_remote(job_dir)
                return
        try:
            job = retry_locked(
                lambda: self.queue.finish(
                    job_id, owner=self.recipient, handoff_comment_id=handoff, proof=proof
                )
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
            try:
                retry_locked(lambda: self.queue.clear_invalid_handoff(job_id))
            except (sqlite3.Error, OSError) as exc:
                self.log(f"job {job_id}: its submitted-invalid flag stays ({_one_line(exc)})")
            how = f"handoff {handoff}"
        elif refused is not None:
            how = f"failed ({refused})"
        elif code != 0:
            how = f"failed (orch job run exited {code})"
        else:
            how = f"failed ({marker or 'no final ORCH-HANDOFF line'})"
        self.log(f"job {job_id}: ran orch job done on the box: {job.state}, {how}")

    def _refused_handoff(self, job_id: str, comment_id: str, proof: Proof | None) -> str | None:
        """Why a remote marker's handoff does not complete its job, or None
        when it fits (#192): T17a's checks (lib/handoff_check.py), as `orch
        job done --handoff` runs them on the box. A handoff that does not fit
        is recorded submitted-invalid; a check that cannot run (GitHub,
        orch.yml) records nothing. Either way the harness has exited and
        cannot post another, so the job is not done. A job no longer running
        is not checked: finishing it again is a no-op or refused anyway."""
        try:
            with self.queue.connect() as connection:
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None or row["state"] != "running":
                    return None
                job = job_from_row(row, connection)
            config, findings = load_orch_yml(self.orch_yml, allow_placeholders=True)
            errors = [item.message for item in findings if item.severity == "error"]
            if config is None or errors:
                raise ValueError(f"{self.orch_yml}: {errors[0] if errors else 'invalid configuration'}")
            expected = expected_for(job, self.bot)
            read = retrying(gh_reader(), lambda line: self.log(f"job {job_id}: {line}"))
            found = handoff_problems(expected, comment_id, read, config.trusted_logins)
            if not found:
                return self._refused_by_head_checks(job_id, comment_id, proof, config, expected, read)
            detail = "; ".join(found)
            retry_locked(
                lambda: self.queue.record_invalid_handoff(
                    job_id, owner=self.recipient, comment_id=comment_id, detail=detail,
                    proof=proof,
                )
            )
        except (
            ReadError, ContextUnavailable, WorktreeError, ApproverProfileExposed, ValueError,
            QueueError, sqlite3.Error, OSError,
        ) as exc:
            reason = f"could not check handoff {comment_id}: {_one_line(exc)}"
            self.log(f"job {job_id}: {reason}")
            return reason
        reason = f"submitted-invalid {comment_id}: {detail}"
        self.log(f"job {job_id}: {reason}")
        return reason

    def _refused_by_head_checks(
        self,
        job_id: str,
        comment_id: str,
        proof: Proof | None,
        config: OrchConfig,
        expected: Expected,
        read: Read,
    ) -> str | None:
        """#503, #1463: a fitting ready runs the base factory.md's head checks
        at the ready's head, in a throwaway worktree of the shared clone (the
        remote run's checkout is on its device)."""
        ready = ready_at(expected, comment_id, read)
        if ready is None:
            return None
        source = GhContextSource(config, retry_log=lambda line: self.log(f"job {job_id}: {line}"))
        checks = checks_for(source, expected.repo, ready.base)
        if not checks:
            return None
        with detached_checkout(
            work_root(None), expected.repo, ready.head, url_template(None)
        ) as checkout:
            failed = first_failure(
                checks,
                checkout,
                ready.head,
                check_environ(os.environ, approvals(config)),
                lambda check: retry_locked(
                    lambda: self.queue.record_head_check(
                        job_id, event=check.passed_event, comment_id=comment_id,
                        head=ready.head, proof=proof,
                    )
                ),
            )
        if failed is None:
            return None
        check, result = failed
        if result.outcome == "unavailable":
            retry_locked(
                lambda: self.queue.record_head_check(
                    job_id, event=check.unavailable_event, comment_id=comment_id,
                    head=result.head, reason=result.reason, proof=proof,
                )
            )
            self.log(
                f"job {job_id}: {check.name} unavailable {comment_id}: {result.reason}; "
                "the ticket waits for a device that can run it"
            )
            if result.tail:
                self.log(f"job {job_id}: {check.name} output:\n{result.tail}")
            return None
        detail = f"{check.name} failed: {result.reason}"
        retry_locked(
            lambda: self.queue.record_invalid_handoff(
                job_id, owner=self.recipient, comment_id=comment_id, detail=detail,
                proof=proof, event=check.failed_event,
            )
        )
        reason = f"{check.name} failed {comment_id}: {result.reason}"
        self.log(f"job {job_id}: {reason}")
        if result.tail:
            self.log(f"job {job_id}: {check.name} output:\n{result.tail}")
        return reason

    def _retry_waiting_gates(self) -> None:
        """#1464: rerun ci.local_gate for a local-gate-waiting ready. Records a
        gate event on the job that posted it; the job's state never changes."""
        try:
            with self.queue.connect() as connection:
                tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ('ci_waits', 'job_events')"
                ).fetchone()[0]
                rows = [] if tables < 2 else connection.execute(
                    """
                    SELECT waits.comment_id, events.id, events.job_id, jobs.repo,
                        events.event, events.detail, events.recorded_at
                    FROM ci_waits AS waits
                    JOIN job_events AS events
                      ON events.repo = waits.repo AND events.ticket_id = waits.ticket_id
                     AND substr(events.detail, 1, length('handoff ' || waits.comment_id || ':'))
                         = 'handoff ' || waits.comment_id || ':'
                    JOIN jobs ON jobs.id = events.job_id
                    WHERE waits.reason = ? AND jobs.recipient = ? AND events.event IN (?, ?, ?, ?)
                    ORDER BY events.id
                    """,
                    (LOCAL_GATE_WAITING, self.recipient, *LOCAL_GATE_EVENTS),
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            self.log(f"local_gate retry: waiting gates not read ({_one_line(exc)})")
            return
        newest = {row["comment_id"]: row for row in rows}
        for comment_id, row in newest.items():
            age = self.queue.now() - float(row["recorded_at"])
            if (
                row["event"] not in (LOCAL_GATE_UNAVAILABLE, LOCAL_GATE_RETRY)
                or age < LOCAL_GATE_RETRY_SECONDS
            ):
                continue
            job_id, repo = str(row["job_id"]), str(row["repo"])
            head = str(row["detail"]).removeprefix(f"handoff {comment_id}: at ").partition(":")[0]
            try:
                if not retry_locked(
                    lambda: self.queue.reserve_gate_retry(
                        job_id, comment_id=comment_id, head=head,
                        expected_newest_event_id=int(row["id"]),
                    )
                ):
                    continue
                config, findings = load_orch_yml(self.orch_yml, allow_placeholders=True)
                errors = [item.message for item in findings if item.severity == "error"]
                if config is None or errors:
                    raise ValueError(
                        f"{self.orch_yml}: {errors[0] if errors else 'invalid configuration'}"
                    )
                source = GhContextSource(
                    config, retry_log=lambda line: self.log(f"job {job_id}: {line}")
                )
                loaded = source.load(repo)
                if loaded.context is None:
                    raise ContextUnavailable(loaded.note or f"{repo} factory.md could not be read")
                spec = loaded.context.config.ci_local_gate
                if spec is None:
                    continue
                with detached_checkout(work_root(None), repo, head, url_template(None)) as checkout:
                    result = run_check(
                        spec, checkout, head, check_environ(os.environ, approvals(config))
                    )
                if result.passed:
                    event, reason = LOCAL_GATE_PASSED, None
                elif result.outcome == "unavailable":
                    event, reason = LOCAL_GATE_UNAVAILABLE, result.reason
                else:
                    event, reason = LOCAL_GATE_FAILED, f"local_gate failed: {result.reason}"
                retry_locked(
                    lambda: self.queue.record_head_check(
                        job_id, event=event, comment_id=comment_id, head=head, reason=reason
                    )
                )
            except (
                ReadError, ContextUnavailable, WorktreeError, ApproverProfileExposed, ValueError,
                QueueError, sqlite3.Error, OSError,
            ) as exc:
                self.log(f"job {job_id}: local_gate retry {comment_id} not run ({_one_line(exc)})")
                continue
            self.log(
                f"job {job_id}: local_gate retry {comment_id} at {head}: {event} ({result.reason})"
            )

    def _forget_remote(self, job_dir: Path) -> None:
        for name in (REMOTE_RECORD, REMOTE_PROOF):
            try:
                (job_dir / name).unlink(missing_ok=True)
            except OSError as exc:
                self.log(f"could not remove {job_dir / name}: {_one_line(exc)}")

    def _settle(self, job_id: str, runner: int, code: int, proof: Proof | None) -> None:
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
                lambda: self.queue.finish(
                    job_id, owner=self.recipient, handoff_comment_id=None, proof=proof
                )
            )
        except ClaimProofError:
            self.log(
                f"job {job_id}: its claim was released and claimed again; left to the later claim"
            )
            return
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
            retry_locked(lambda: self.pool.release(job_id, owner=self.recipient, proof=proof))
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


def _read_private_proof(path: Path) -> Proof | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or (info.st_mode & (stat.S_IRWXG | stat.S_IRWXO))
        ):
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            token = handle.read(4096).decode("utf-8", errors="strict").strip()
        return Proof(token) if token else None
    except (OSError, UnicodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _private_nofollow(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
