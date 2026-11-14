"""The shared liveness probe the service tests poll while processes come and go."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.procs import alive, open_files

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")


def _child() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_a_running_process_is_alive() -> None:
    child = _child()
    try:
        assert alive(child.pid)
    finally:
        child.kill()
        child.wait()


def test_an_unreaped_zombie_is_not_alive() -> None:
    child = _child()
    child.kill()
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
    try:
        assert not alive(child.pid)
    finally:
        child.wait()


def test_a_reaped_process_is_not_alive() -> None:
    child = _child()
    child.kill()
    child.wait()
    assert not alive(child.pid)


def test_a_process_reaped_right_after_the_signal_probe_is_not_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _child()
    real_kill = os.kill

    def kill_then_reap(pid: int, sig: int) -> None:
        real_kill(pid, sig)
        if pid == child.pid and sig == 0:
            real_kill(pid, signal.SIGKILL)
            child.wait()

    monkeypatch.setattr(os, "kill", kill_then_reap)
    assert not alive(child.pid)


def test_open_files_skips_an_fd_that_closes_mid_scan(tmp_path: Path, monkeypatch) -> None:
    held = tmp_path / "held"
    closing = tmp_path / "closing"
    held.touch()
    closing.touch()
    real_readlink = os.readlink
    with open(held), open(closing) as gone:
        vanished = f"/fd/{gone.fileno()}"

        def readlink(path, *args, **kwargs):
            if str(path).endswith(vanished):
                raise FileNotFoundError(path)
            return real_readlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "readlink", readlink)
        found = open_files(os.getpid())
    assert str(held.resolve()) in found
    assert str(closing.resolve()) not in found
