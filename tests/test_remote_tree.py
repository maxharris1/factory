"""#722: a remote run's processes are found by parentage, not only by its tag.

A worker's descendant can drop the tag (a clean environment) and leave the
start's process group (a new session). The device keeps the run's process
tree in the job directory, so check and stop still find that descendant
after the worker that started it is gone. The scripts run locally through
`sh` (tests/test_remote_start.py LocalShell); no test reaches a real host.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from lib import device_monitor, remote
from lib.worktrees import pid_alive, process_start
from tests.test_remote_start import (
    LocalShell,
    config_of,
    disable_pc,
    lost_setup,
    machines_file,
    orphans,
    poll_at,
)


def wait_until(ready, what: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not ready():
        assert time.monotonic() < deadline, what
        time.sleep(0.02)


def launch_with_descendant(
    tmp_path: Path, target: remote.RemoteTarget, *, clean_environment: bool
) -> tuple[int, int]:
    """Launch a worker that starts a SIGTERM-proof descendant in its own session.

    With clean_environment the descendant's environment holds only PATH, so
    it does not carry the run's tag. Returns the worker's and the
    descendant's pids.
    """
    marker, tick, leaf_pid = tmp_path / "worker.pid", tmp_path / "tick", tmp_path / "leaf.pid"
    leaf = f"""import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(leaf_pid)!r}).write_text(str(os.getpid()))
while True:
    Path({str(tick)!r}).write_text(str(time.monotonic_ns()))
    time.sleep(.02)
"""
    child_env = "{'PATH': os.environ['PATH']}" if clean_environment else "None"
    worker = f"""import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_DFL)
subprocess.Popen([sys.executable, '-c', {leaf!r}], env={child_env}, start_new_session=True)
Path({str(marker)!r}).write_text(str(os.getpid()))
time.sleep(120)
"""
    start = remote.start_script(target, [sys.executable, "-c", worker], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=120, grace=1
    )
    result = subprocess.run(
        ["sh", "-c", launch], stdin=subprocess.DEVNULL, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    wait_until(lambda: marker.exists() and tick.exists(), "the worker and its descendant started")
    return int(marker.read_text()), int(leaf_pid.read_text())


def kill_leftover(tmp_path: Path) -> None:
    leaf_pid = tmp_path / "leaf.pid"
    if leaf_pid.exists():
        try:
            os.kill(int(leaf_pid.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("clean_environment", [False, True])
def test_a_lost_remote_orphan_is_kept_while_a_descendant_of_its_run_executes(
    tmp_path: Path, clean_environment: bool
) -> None:
    tag = uuid.uuid4().hex
    now, queue, _, _, dispatcher, job = lost_setup(tmp_path, reload=True, tag=tag)
    monitor = dispatcher.devices
    monitor.transport = LocalShell()
    monitor.alive = lambda *_: False
    target = device_monitor.remote_target(monitor.config.device("pc"), "claude", job.id, tag)
    assert target is not None
    try:
        worker_pid, leaf = launch_with_descendant(
            tmp_path, target, clean_environment=clean_environment
        )
        disable_pc(tmp_path)
        poll_at(now, dispatcher, 0.0)
        assert orphans(queue), "the lost job leaves an orphan on the device"
        wait_until(lambda: not pid_alive(worker_pid, None), "the stop ended the worker", 5.0)

        poll_at(now, dispatcher, 60.0)
        assert orphans(queue) or not pid_alive(leaf, None), (
            "remote orphan forgotten while its descendant still executes"
        )
    finally:
        remote.stop_worker(monitor.transport, target, kill=True)
        kill_leftover(tmp_path)


def test_a_kill_reaches_a_descendant_that_left_the_runs_session_and_environment(
    tmp_path: Path,
) -> None:
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", uuid.uuid4().hex)
    assert target is not None
    shell = LocalShell()
    try:
        worker_pid, leaf = launch_with_descendant(tmp_path, target, clean_environment=True)
        assert remote.check_worker(shell, target) == remote.RUNNING
        os.kill(worker_pid, signal.SIGKILL)
        wait_until(lambda: not pid_alive(worker_pid, None), "the worker was killed", 5.0)
        assert remote.check_worker(shell, target) == remote.RUNNING, (
            "an untagged descendant in its own session keeps the run alive"
        )

        assert remote.stop_worker(shell, target, kill=True)
        wait_until(lambda: not pid_alive(leaf, None), "the kill reached the descendant", 5.0)
        assert remote.check_worker(shell, target) == remote.GONE, (
            "the run is gone once its last process is"
        )
    finally:
        remote.stop_worker(shell, target, kill=True)
        kill_leftover(tmp_path)


def test_a_run_whose_processes_all_ended_is_gone_while_its_deadline_still_waits(
    tmp_path: Path,
) -> None:
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", uuid.uuid4().hex)
    assert target is not None
    shell = LocalShell()
    release = tmp_path / "release"
    worker = (
        "import time; from pathlib import Path\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.02)\n"
    )
    start = remote.start_script(target, [sys.executable, "-c", worker], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=120, grace=1
    )
    try:
        result = subprocess.run(
            ["sh", "-c", launch], stdin=subprocess.DEVNULL, capture_output=True, timeout=30
        )
        assert result.stdout.decode().strip() == f"{remote.LAUNCH_MARK} started"
        assert remote.check_worker(shell, target) == remote.RUNNING
        release.touch()
        wait_until(lambda: Path(remote.run_file(target, "exit")).exists(), "the worker ended")
        wait_until(
            lambda: remote.check_worker(shell, target) == remote.GONE,
            "the deadline and the checks it runs are never the run's processes",
            5.0,
        )
    finally:
        remote.stop_worker(shell, target, kill=True)


@pytest.mark.parametrize(
    ("same_boot", "same_start"), [(True, True), (True, False), (False, True)]
)
def test_a_tree_entry_names_a_process_only_from_this_boot_with_its_start_time(
    tmp_path: Path, same_boot: bool, same_start: bool
) -> None:
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", uuid.uuid4().hex)
    assert target is not None
    shell = LocalShell()
    process = subprocess.Popen(["sleep", "60"])
    try:
        start = process_start(process.pid)
        assert start is not None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        recorded_boot = boot if same_boot else str(uuid.uuid4())
        recorded_start = start if same_start else str(int(start) + 1)
        directory = Path(target.job_dir)
        directory.mkdir(parents=True)
        (directory / remote.tree_record(target.require_tag())).write_text(
            f"{recorded_boot}\n{process.pid} {recorded_start}\n", encoding="utf-8"
        )

        if same_boot and same_start:
            assert remote.check_worker(shell, target) == remote.RUNNING
            assert remote.stop_worker(shell, target)
            assert process.wait(timeout=5) == -signal.SIGTERM
            return
        assert remote.check_worker(shell, target) == remote.GONE, (
            "a reused pid is never counted as the run's"
        )
        assert remote.stop_worker(shell, target)
        assert remote.stop_worker(shell, target, kill=True)
        time.sleep(0.1)
        state = Path(f"/proc/{process.pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
        assert process.poll() is None and state == "S", "a reused pid is never signalled"
    finally:
        process.kill()
        process.wait()


def test_a_lost_remote_runs_untagged_orphan_born_before_any_check_is_killed(
    tmp_path: Path,
) -> None:
    tag = uuid.uuid4().hex
    now, queue, _, _, dispatcher, job = lost_setup(tmp_path, reload=True, tag=tag)
    monitor = dispatcher.devices
    monitor.transport = LocalShell()
    monitor.alive = lambda *_: False
    target = device_monitor.remote_target(monitor.config.device("pc"), "claude", job.id, tag)
    assert target is not None
    tick, leaf_pid = tmp_path / "tick", tmp_path / "leaf.pid"
    leaf = f"""import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(leaf_pid)!r}).write_text(str(os.getpid()))
while True:
    Path({str(tick)!r}).write_text(str(time.monotonic_ns()))
    time.sleep(.02)
"""
    worker = (
        "import os, subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {leaf!r}], env={{'PATH': os.environ['PATH']}})\n"
    )
    start = remote.start_script(target, [sys.executable, "-c", worker], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=120, grace=1
    )
    unrelated = subprocess.Popen(["sleep", "120"], start_new_session=True)
    try:
        result = subprocess.run(
            ["sh", "-c", launch], stdin=subprocess.DEVNULL, capture_output=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        wait_until(lambda: Path(remote.run_file(target, "exit")).exists(), "the worker ended")
        wait_until(tick.exists, "the orphan started")
        orphan = int(leaf_pid.read_text())
        assert pid_alive(orphan, None)
        assert remote.check_worker(monitor.transport, target) == remote.RUNNING, (
            "an orphan in the start's session keeps the run alive"
        )

        disable_pc(tmp_path)
        poll_at(now, dispatcher, 0.0)
        assert orphans(queue), "the lost run keeps its orphan row"
        poll_at(now, dispatcher, 60.0)
        wait_until(lambda: not pid_alive(orphan, None), "the kill reached the orphan", 5.0)
        poll_at(now, dispatcher, 120.0)
        assert orphans(queue) == [], "the row is forgotten once the run is gone"
        assert unrelated.poll() is None, "a process outside the run is never signalled"
    finally:
        remote.stop_worker(monitor.transport, target, kill=True)
        kill_leftover(tmp_path)
        unrelated.kill()
        unrelated.wait()
