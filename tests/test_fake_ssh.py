"""tests/fake_ssh.py link faults (#106): delayed, late and dropped deliveries.

Each mode on its own, through the real fake ssh process. Later runner tests
build on these to replay a dropped link or a start delivered after its
runner died.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_remote_start import Ssh, wait_file

ALIAS = "fakepc-claude-bot"
USER = "claude-bot"


@pytest.fixture
def ssh(tmp_path: Path) -> Ssh:
    return Ssh(tmp_path)


def call(ssh: Ssh, script: str, **kwargs) -> subprocess.Popen:
    env = {**os.environ, **ssh.env()}
    return subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", "-l", USER, "--", ALIAS, script],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
    )


def test_no_fault_runs_the_script_and_returns_its_status(ssh: Ssh) -> None:
    proc = call(ssh, "echo hi; exit 3")
    out, _ = proc.communicate(timeout=20)
    assert (proc.returncode, out) == (3, b"hi\n")


def test_delay_holds_the_script_back_and_a_killed_client_cancels_it(
    ssh: Ssh, tmp_path: Path
) -> None:
    ssh.fault(ALIAS, "delay", 0.5)
    began = time.monotonic()
    proc = call(ssh, "echo late")
    out, _ = proc.communicate(timeout=20)
    assert out == b"late\n" and time.monotonic() - began >= 0.5

    marker = tmp_path / "ran"
    ssh.fault(ALIAS, "delay", 2)
    proc = call(ssh, f"touch {marker}")
    time.sleep(0.3)
    proc.kill()
    proc.wait()
    time.sleep(2.5)
    assert not marker.exists()


def test_delay_detached_runs_the_script_after_the_client_is_killed(
    ssh: Ssh, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    ssh.fault(ALIAS, "delay-detached", 1)
    proc = call(ssh, f"echo delivered > {marker}")
    time.sleep(0.3)
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert not marker.exists()
    assert wait_file(marker, seconds=10) == "delivered"


def test_drop_after_ends_the_client_with_255_while_the_script_runs_on(
    ssh: Ssh, tmp_path: Path
) -> None:
    marker = tmp_path / "finished"
    ssh.fault(ALIAS, "drop-after", 0.5)
    proc = call(ssh, f"echo before; sleep 1.5; echo done > {marker}")
    began = time.monotonic()
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 255
    assert out == b"before\n"
    assert time.monotonic() - began < 1.5
    assert wait_file(marker, seconds=10) == "done"


def test_offline_toggled_mid_test(ssh: Ssh) -> None:
    flag = ssh.offline(ALIAS)
    flag.touch()
    assert call(ssh, "exit 0").wait(timeout=20) == 255
    flag.unlink()
    assert call(ssh, "exit 0").wait(timeout=20) == 0
