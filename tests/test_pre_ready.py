"""The review.pre_ready runner (#503): head check, timeout, environment, cut."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from lib.factory_md import PreReady
from lib.pre_ready import TAIL_CHARS, TAIL_LINES, check_environ, cut, run


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    return repo, git(repo, "rev-parse", "HEAD")


def test_a_passing_command_passes(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)

    result = run(PreReady("echo fine", 10), repo, head, dict(os.environ))

    assert (result.passed, result.reason, result.tail) == (True, "exit 0", "fine")


def test_a_failing_command_fails_with_its_exit_and_output(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)

    result = run(PreReady("echo broke; exit 3", 10), repo, head, dict(os.environ))

    assert (result.passed, result.reason, result.tail) == (False, "exit 3", "broke")


def test_a_timeout_fails_and_kills_the_whole_group(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    marker = tmp_path / "child-lived"

    started = time.monotonic()
    result = run(
        PreReady(f"(sleep 3; touch {marker}) & sleep 30", 1), repo, head, dict(os.environ)
    )

    assert (result.passed, result.reason) == (False, "timeout after 1s")
    assert time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists()


def test_another_head_or_a_dirty_tree_runs_nothing(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    marker = tmp_path / "ran"
    spec = PreReady(f"touch {marker}", 10)

    other = run(spec, repo, "b" * 40, dict(os.environ))
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    dirty = run(spec, repo, head, dict(os.environ))

    assert (other.passed, other.reason) == (False, f"not at head {'b' * 40}")
    assert (dirty.passed, dirty.reason) == (False, f"not at head {head}")
    assert not marker.exists()


def test_the_command_sees_no_token_proof_or_queue_variables(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)
    caller = {
        "PATH": os.environ["PATH"],
        "GH_TOKEN": "x",
        "GITHUB_TOKEN": "x",
        "ORCH_JOB_PROOF": "x",
        "ORCH_DB": "x",
        "KEPT_VARIABLE": "kept",
    }

    result = run(PreReady("env", 10), repo, head, check_environ(caller, None))

    names = {line.partition("=")[0] for line in result.tail.splitlines()}
    assert names.isdisjoint({"GH_TOKEN", "GITHUB_TOKEN", "ORCH_JOB_PROOF", "ORCH_DB"})
    assert "KEPT_VARIABLE" in names


def test_long_output_is_cut_to_its_last_lines(tmp_path: Path) -> None:
    repo, head = checkout(tmp_path)

    result = run(PreReady("seq 1 10000", 10), repo, head, dict(os.environ))
    body = result.tail.splitlines()

    assert body[0] == f"[pre_ready output cut to its last {TAIL_LINES} lines]"
    assert len(body) == TAIL_LINES + 1
    assert body[-1] == "10000"


def test_wide_lines_are_cut_to_the_character_limit() -> None:
    tail = cut("\n".join("x" * 500 for _ in range(30)) + "\nlast")

    assert len(tail.partition("\n")[2]) <= TAIL_CHARS
    assert tail.startswith("[pre_ready output cut to its last ")
    assert tail.endswith("\nlast")


def test_short_output_is_not_cut() -> None:
    assert cut("one\ntwo\n") == "one\ntwo"
