"""A project's `review.pre_ready` check, run before a builder's ready counts (#503).

The command comes from factory.md at the repo's trusted factory_ref, never
from the PR head. It runs with `sh -c` in a checkout at the ready's head,
with the job's own environment rules: token variables dropped, and the
job's proof and queue path dropped so the command cannot finish or touch
its own job. Its merged output is cut to a tail before anyone prints it.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lib.factory_md import PreReady
from lib.harness_env import harness_environ
from lib.job_token import PROOF_ENV

if TYPE_CHECKING:
    from lib.orch_yml import Approvals
    from lib.waiter import GhContextSource

TAIL_LINES = 40
TAIL_CHARS = 4000
DROPPED = frozenset({PROOF_ENV, "ORCH_DB"})


@dataclass(frozen=True)
class PreReadyResult:
    passed: bool
    reason: str
    tail: str


class ContextUnavailable(Exception):
    """The base factory.md could not be read, so nothing is decided."""


def spec_for(source: GhContextSource, repo: str) -> PreReady | None:
    """review.pre_ready from factory.md at the repo's factory_ref. A repo
    orch.yml gives no factory_ref has no base factory.md, so no check."""
    config = source.config
    project = None if config is None else next(
        (item for item in config.projects if item.repo.lower() == repo.lower()), None
    )
    if project is None or project.factory_ref is None:
        return None
    loaded = source.load(repo)
    if loaded.context is None:
        raise ContextUnavailable(loaded.note or f"{repo} factory.md could not be read")
    return loaded.context.config.pre_ready


def check_environ(environ: Mapping[str, str], approvals: Approvals | None) -> dict[str, str]:
    env = harness_environ(environ, approvals)
    return {name: value for name, value in env.items() if name not in DROPPED}


def cut(text: str) -> str:
    lines = text.splitlines()
    kept = lines[-TAIL_LINES:]
    tail = "\n".join(kept)[-TAIL_CHARS:]
    if len(kept) == len(lines) and len(tail) == len("\n".join(kept)):
        return tail
    count = len(tail.splitlines())
    return f"[pre_ready output cut to its last {count} lines]\n{tail}"


def run(spec: PreReady, cwd: Path, head: str, environ: Mapping[str, str]) -> PreReadyResult:
    """Run the check in cwd, which must be a clean checkout at head."""
    at = _git(cwd, "rev-parse", "HEAD")
    dirty = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    if at is None or at != head or dirty is None or dirty:
        return PreReadyResult(False, f"not at head {head}", "")
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
        return PreReadyResult(False, f"could not start: {type(exc).__name__}", "")
    try:
        output, _ = process.communicate(timeout=spec.timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_group(process.pid)
        output, _ = process.communicate()
        return PreReadyResult(False, f"timeout after {spec.timeout_seconds}s", _decoded(output))
    _kill_group(process.pid)
    if process.returncode != 0:
        return PreReadyResult(False, f"exit {process.returncode}", _decoded(output))
    return PreReadyResult(True, "exit 0", _decoded(output))


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
