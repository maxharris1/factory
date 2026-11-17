from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lib.dispatch.cycle_clock import CycleClock
from lib.github_actions import GhResult, GitHubAdapterError, run_gh
from lib.poll_source import LivePollSource
from tests.test_github_actions import HEAD, pr
from tests.test_live_poll import config

TIMEOUT = 1.0
BOUND = TIMEOUT + 5.0

FAKE_GH = """
import os, subprocess, sys, time

mode, gh_pid_file, grandchild_pid_file = sys.argv[1:4]
if mode == "fast":
    sys.stdout.write("out")
    sys.stderr.write("err")
    sys.exit(3)
grandchild = "import os, time\\n"
if mode == "escape":
    grandchild += "os.setsid()\\n"
grandchild += "time.sleep(120)\\n"
child = subprocess.Popen([sys.executable, "-c", grandchild])
with open(grandchild_pid_file, "w") as handle:
    handle.write(str(child.pid))
with open(gh_pid_file, "w") as handle:
    handle.write(str(os.getpid()))
if mode == "exit":
    sys.exit(0)
if mode == "partial":
    os.write(1, b"\\xe2")
    os.write(2, b"\\xe2")
time.sleep(120)
"""


class FakeGh:
    def __init__(self, tmp_path: Path):
        self.script = tmp_path / "gh.py"
        self.script.write_text(FAKE_GH)
        self.gh_pid_file = tmp_path / "gh.pid"
        self.grandchild_pid_file = tmp_path / "grandchild.pid"

    def argv(self, mode: str) -> list[str]:
        return [
            sys.executable,
            str(self.script),
            mode,
            str(self.gh_pid_file),
            str(self.grandchild_pid_file),
        ]

    def gh_pid(self) -> int:
        return int(self.gh_pid_file.read_text())

    def grandchild_pid(self) -> int:
        return int(self.grandchild_pid_file.read_text())


@pytest.fixture
def fake_gh(tmp_path: Path) -> Iterator[FakeGh]:
    gh = FakeGh(tmp_path)
    yield gh
    for pid_file in (gh.gh_pid_file, gh.grandchild_pid_file):
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def gone(probe, pid: int) -> bool:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            probe(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def timed_out_within_bound(argv: list[str]) -> None:
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_gh(argv, TIMEOUT)
    assert time.monotonic() - began < BOUND
    assert raised.value.timeout == TIMEOUT


def test_a_grandchild_holding_stdout_cannot_hold_run_gh_past_its_timeout(
    fake_gh: FakeGh,
) -> None:
    timed_out_within_bound(fake_gh.argv("hang"))

    assert gone(os.killpg, fake_gh.gh_pid()), "gh's process group outlived the timeout"
    assert gone(os.kill, fake_gh.grandchild_pid()), "the grandchild outlived the timeout"


def test_gh_exiting_while_its_grandchild_holds_stdout_still_times_out(
    fake_gh: FakeGh,
) -> None:
    timed_out_within_bound(fake_gh.argv("exit"))

    assert gone(os.killpg, fake_gh.gh_pid()), "gh's process group outlived the timeout"
    assert gone(os.kill, fake_gh.grandchild_pid()), "the grandchild outlived the timeout"


def test_a_grandchild_that_left_the_group_cannot_hold_run_gh_either(
    fake_gh: FakeGh,
) -> None:
    timed_out_within_bound(fake_gh.argv("escape"))

    assert gone(os.kill, fake_gh.gh_pid()), "gh outlived the timeout"


def test_a_timeout_mid_character_still_raises_the_timeout(fake_gh: FakeGh) -> None:
    timed_out_within_bound(fake_gh.argv("partial"))

    assert gone(os.killpg, fake_gh.gh_pid()), "gh's process group outlived the timeout"


def test_a_fast_gh_returns_its_exit_code_and_output(fake_gh: FakeGh) -> None:
    assert run_gh(fake_gh.argv("fast"), 30.0) == GhResult(3, "out", "err")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_a_timed_out_write_is_charged_to_its_source_and_ticket() -> None:
    clock = Clock()

    def gh(argv, timeout):
        call = list(argv)
        if "DELETE" in call:
            clock.now += 60.0
            raise subprocess.TimeoutExpired(call, 60.0)
        clock.now += 1.0
        if any(part.endswith("/labels?per_page=100") for part in call):
            return GhResult(0, json.dumps([{"name": "orch:state/intake"}]), "")
        return GhResult(0, "[]", "")

    source = LivePollSource(config(), gh_runner=gh, clock=clock)
    source.cycle_clock = CycleClock()
    source.cycle_clock.start()
    with pytest.raises(GitHubAdapterError, match="timed out after 60s"):
        with source.cycle_clock.phase("process"):
            source.review_actions_by_repo()["acme/widgets"].set_state(
                pr(repo="acme/widgets", ticket_id="664"), "refused"
            )

    line = source.cycle_clock.end()
    assert "[acme/widgets 2.0s, acme/widgets#664 60.0s]" in line, line


def test_a_timed_out_graphql_write_is_charged_to_its_source_and_ticket() -> None:
    clock = Clock()

    def gh(argv, timeout):
        call = list(argv)
        if "graphql" in call:
            clock.now += 60.0
            raise subprocess.TimeoutExpired(call, 60.0)
        clock.now += 1.0
        pull = {"head": {"sha": HEAD}, "state": "open", "draft": True, "merged": False}
        return GhResult(0, json.dumps({**pull, "node_id": "PR_kwDOKfac0s6CiTKq"}), "")

    source = LivePollSource(config(), gh_runner=gh, clock=clock)
    source.cycle_clock = CycleClock(wall=clock, monotonic=clock)
    source.cycle_clock.start()
    with pytest.raises(GitHubAdapterError, match="timed out after 60s"):
        with source.cycle_clock.phase("process"):
            source.review_actions_by_repo()["acme/widgets"].handoff(
                pr(repo="acme/widgets", ticket_id="664"), HEAD
            )

    line = source.cycle_clock.end()
    assert "[acme/widgets 1.0s, acme/widgets#664 60.0s]" in line, line
