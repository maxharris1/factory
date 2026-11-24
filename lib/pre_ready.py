"""The checks a builder's ready must pass at its head before it counts:
factory.md's `review.pre_ready` (#503), then `ci.local_gate` on a PR into a
base CI never runs on (#1463)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from lib.factory_md import CheckCommand, FactoryConfig, no_ci_base
from lib.harness_env import harness_environ
from lib.job_token import PROOF_ENV
from lib.queue import (
    LOCAL_GATE_FAILED,
    LOCAL_GATE_PASSED,
    LOCAL_GATE_UNAVAILABLE,
    PRE_READY_FAILED,
    PRE_READY_PASSED,
)
from lib.redact_output import redact_output

if TYPE_CHECKING:
    from lib.orch_yml import Approvals
    from lib.waiter import GhContextSource

TAIL_LINES = 40
TAIL_CHARS = 4000
DRAIN_SECONDS = 1
JOB_CONTROL_VARIABLES = frozenset({PROOF_ENV, "ORCH_DB"})
ORCH_INTERPRETER_ENV = "ORCH_PYTHON"
UNAVAILABLE_EXIT = os.EX_TEMPFAIL


@dataclass(frozen=True)
class PreReadyResult:
    outcome: Literal["passed", "failed", "unavailable"]
    reason: str
    tail: str
    head: str

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"


@dataclass(frozen=True)
class HeadCheck:
    name: str
    spec: CheckCommand
    passed_event: str
    failed_event: str
    unavailable_event: str | None


class ContextUnavailable(Exception):
    """The base factory.md could not be read, so nothing is decided."""


def planned(factory: FactoryConfig, pr_base: str | None) -> tuple[HeadCheck, ...]:
    checks: list[HeadCheck] = []
    if factory.pre_ready is not None:
        checks.append(
            HeadCheck("pre_ready", factory.pre_ready, PRE_READY_PASSED, PRE_READY_FAILED, None)
        )
    if factory.ci_local_gate is not None and no_ci_base(factory, pr_base) is not None:
        checks.append(
            HeadCheck(
                "local_gate",
                factory.ci_local_gate,
                LOCAL_GATE_PASSED,
                LOCAL_GATE_FAILED,
                LOCAL_GATE_UNAVAILABLE,
            )
        )
    return tuple(checks)


def checks_for(source: GhContextSource, repo: str, pr_base: str | None) -> tuple[HeadCheck, ...]:
    """The planned checks from factory.md at the repo's factory_ref, never the PR head."""
    config = source.config
    project = None if config is None else next(
        (item for item in config.projects if item.repo.lower() == repo.lower()), None
    )
    if project is None or project.factory_ref is None:
        return ()
    loaded = source.load(repo)
    if loaded.context is None:
        raise ContextUnavailable(loaded.note or f"{repo} factory.md could not be read")
    return planned(loaded.context.config, pr_base)


def check_environ(environ: Mapping[str, str], approvals: Approvals | None) -> dict[str, str]:
    env = harness_environ(environ, approvals)
    kept = {name: value for name, value in env.items() if name not in JOB_CONTROL_VARIABLES}
    return {**kept, ORCH_INTERPRETER_ENV: sys.executable}


def cut(text: str) -> str:
    lines = redact_output(text).splitlines()
    kept = lines[-TAIL_LINES:]
    tail = "\n".join(kept)[-TAIL_CHARS:]
    if len(kept) == len(lines) and len(tail) == len("\n".join(kept)):
        return tail
    count = len(tail.splitlines())
    return f"[pre_ready output cut to its last {count} lines]\n{tail}"


def run(spec: CheckCommand, cwd: Path, head: str, environ: Mapping[str, str]) -> PreReadyResult:
    """Run the check in cwd, which must be a clean checkout at head."""
    at = _git(cwd, "rev-parse", "HEAD")
    dirty = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    if at is None or at != head or dirty is None or dirty:
        return PreReadyResult("failed", f"not at head {head}", "", head)
    try:
        process = subprocess.Popen(
            ["/bin/sh", "-c", spec.command],
            cwd=cwd,
            env=dict(environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return PreReadyResult("unavailable", f"could not start: {type(exc).__name__}", "", head)
    try:
        output, _ = process.communicate(timeout=spec.timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_group(process.pid)
        output = _drain(process)
        return PreReadyResult(
            "failed", f"timeout after {spec.timeout_seconds}s", _decoded(output), head
        )
    _kill_group(process.pid)
    if process.returncode == 0:
        return PreReadyResult("passed", "exit 0", _decoded(output), head)
    outcome = "unavailable" if process.returncode == UNAVAILABLE_EXIT else "failed"
    return PreReadyResult(outcome, f"exit {process.returncode}", _decoded(output), head)


@contextmanager
def committed_checkout(worktree: Path, head: str) -> Iterator[Path]:
    """A throwaway detached worktree of head made from the job's worktree, so
    a check sees the committed source and nothing the builder left uncommitted."""
    with tempfile.TemporaryDirectory(prefix="orch-checkout-") as scratch:
        path = Path(scratch) / "checkout"
        _git(worktree, "worktree", "add", "--detach", "--quiet", str(path), head)
        try:
            yield path
        finally:
            _git(worktree, "worktree", "remove", "--force", str(path))


def first_failure(
    checks: tuple[HeadCheck, ...],
    cwd: Path,
    head: str,
    environ: Mapping[str, str],
    passed: Callable[[HeadCheck], None],
) -> tuple[HeadCheck, PreReadyResult] | None:
    for check in checks:
        result = run(check.spec, cwd, head, environ)
        if result.outcome == "unavailable" and check.unavailable_event is None:
            return check, replace(result, outcome="failed")
        if not result.passed:
            return check, result
        passed(check)
    return None


def _drain(process: subprocess.Popen[bytes]) -> bytes | None:
    """What the killed check wrote, without waiting on a descendant that
    left its group and still holds the pipe."""
    try:
        output, _ = process.communicate(timeout=DRAIN_SECONDS)
    except subprocess.TimeoutExpired as exc:
        output = exc.output
        if process.stdout is not None:
            process.stdout.close()
        process.wait()
    return output


def _decoded(output: bytes | None) -> str:
    return cut((output or b"").decode("utf-8", errors="replace"))


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None
