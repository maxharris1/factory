"""#722: a remote run's processes are found by parentage, not only by its tag.

A worker's descendant can drop the tag (a clean environment) and leave the
start's process group (a new session). The device keeps the run's process
tree in the job directory, so check and stop still find that descendant
after the worker that started it is gone. The scripts run locally through
`sh` (tests/test_remote_start.py LocalShell); no test reaches a real host.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from lib import device_monitor, remote
from lib.worktrees import pid_alive, process_start
from tests.test_detached_remote import Box
from tests.test_remote_start import (
    LocalShell,
    config_of,
    disable_pc,
    kill_all,
    kill_quietly,
    lost_setup,
    machines_file,
    orphans,
    poll_at,
    wait_file,
    wait_gone,
)

linux_path = pytest.mark.skipif(
    sys.platform != "linux", reason="runs the Linux path's scripts against /proc"
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


@linux_path
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


@linux_path
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


@linux_path
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


@linux_path
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


@linux_path
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


def path_shim(directory: Path, name: str, body: str) -> None:
    real = shutil.which(name)
    assert real is not None
    directory.mkdir(exist_ok=True)
    shim = directory / name
    shim.write_text(f'#!/bin/sh\n{body}\nexec {real} "$@"\n')
    shim.chmod(0o755)


def pause_untagged_scans(shims: Path, paused: Path, release: Path) -> None:
    path_shim(
        shims,
        "grep",
        f'if [ -z "${remote.TAG_ENV}" ] && [ -z "${remote.DEADLINE_ENV}" ]; then\n'
        f": > {shlex.quote(str(paused))}\n"
        f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done\n"
        "fi",
    )


def kill_group(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@linux_path
def test_a_kill_before_the_wrapper_records_still_stops_the_untagged_child(
    tmp_path: Path,
) -> None:
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", uuid.uuid4().hex)
    assert target is not None
    shell = LocalShell()
    paused, release, child_pid = tmp_path / "paused", tmp_path / "release", tmp_path / "child.pid"
    shims = tmp_path / "shims"
    pause_untagged_scans(shims, paused, release)
    child = f"""import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(child_pid)!r}).write_text(str(os.getpid()))
while True:
    time.sleep(.05)
"""
    worker = (
        "import os, subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}], env={{'PATH': os.environ['PATH']}})\n"
    )
    start = remote.start_script(target, [sys.executable, "-c", worker], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=120, grace=1
    )
    exit_file = Path(remote.run_file(target, "exit"))
    wrapper = None
    try:
        result = subprocess.run(
            ["sh", "-c", launch],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env={**os.environ, "PATH": f"{shims}:{os.environ['PATH']}"},
        )
        assert result.returncode == 0, result.stderr
        wrapper = int(Path(remote.run_file(target, "pid")).read_text().split()[0])
        wait_until(child_pid.exists, "the worker started its untagged child")
        orphan = int(child_pid.read_text())
        wait_until(
            lambda: exit_file.exists() or paused.exists(),
            "the start ended and the wrapper wrote its exit or began an untagged record",
        )

        assert remote.stop_worker(shell, target, kill=True)
        kill_group(wrapper)
        release.touch()
        wait_until(
            lambda: not pid_alive(orphan, None),
            "the KILL stop reached the untagged child left in the start's session",
            5.0,
        )
        wait_until(
            lambda: remote.check_worker(shell, target) == remote.GONE,
            "the run is gone once its last process is",
            10.0,
        )
    finally:
        release.touch()
        remote.stop_worker(shell, target, kill=True)
        kill_group(wrapper)
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@linux_path
def test_a_term_before_the_keeper_ignores_it_leaves_no_untracked_child(
    tmp_path: Path,
) -> None:
    """#827 f-6: the keeper is held before its first instruction while the
    stop's TERM arrives; a worker that answers TERM by starting an untagged
    child in its session must not have run, or the KILL must reach that child."""
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", uuid.uuid4().hex)
    assert target is not None
    shell = LocalShell()
    held, worker_pid, child_pid = tmp_path / "held", tmp_path / "worker.pid", tmp_path / "child.pid"
    shims = tmp_path / "shims"
    path_shim(
        shims,
        "sh",
        'if [ "$3" = orch-keeper ]; then\n'
        f": > {shlex.quote(str(held))}\n"
        "while :; do sleep 0.05; done\n"
        "fi",
    )
    child = f"""import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(child_pid)!r}).write_text(str(os.getpid()))
while True:
    time.sleep(.05)
"""
    worker = f"""import os, signal, subprocess, sys, time
from pathlib import Path
def answer(*_):
    subprocess.Popen([sys.executable, '-c', {child!r}], env={{'PATH': os.environ['PATH']}})
    os._exit(0)
signal.signal(signal.SIGTERM, answer)
Path({str(worker_pid)!r}).write_text(str(os.getpid()))
time.sleep(60)
"""
    start = remote.start_script(target, [sys.executable, "-c", worker], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=120, grace=1
    )
    exit_file = Path(remote.run_file(target, "exit"))
    wrapper = None
    try:
        result = subprocess.run(
            ["sh", "-c", launch],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env={**os.environ, "PATH": f"{shims}:{os.environ['PATH']}"},
        )
        assert result.returncode == 0, result.stderr
        wrapper = int(Path(remote.run_file(target, "pid")).read_text().split()[0])
        wait_until(held.exists, "the keeper is held before its first instruction")
        time.sleep(1.0)

        assert remote.stop_worker(shell, target)
        wait_until(exit_file.exists, "the run ended after the TERM")
        if child_pid.exists():
            wait_until(lambda: child_pid.read_text() != "", "the child wrote its pid")
        assert remote.stop_worker(shell, target, kill=True)
        if child_pid.exists():
            orphan = int(child_pid.read_text())
            wait_until(
                lambda: not pid_alive(orphan, None),
                "the KILL reached the untagged child the worker started on TERM",
                5.0,
            )
        assert not worker_pid.exists(), "the worker never runs before the keeper ignores TERM"
    finally:
        remote.stop_worker(shell, target, kill=True)
        kill_group(wrapper)
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


UNLOCKED_WRITE_SECONDS = 3


@linux_path
def test_a_concurrent_check_never_erases_a_newer_tree_record(tmp_path: Path) -> None:
    """#827 f-6: a check that scanned before a member's child was born must
    not replace the record a later check wrote with that child in it."""
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", uuid.uuid4().hex)
    assert target is not None
    paused, release = tmp_path / "paused", tmp_path / "release"
    go, child_pid = tmp_path / "go", tmp_path / "child.pid"
    shims = tmp_path / "shims"
    path_shim(
        shims,
        "mv",
        'if [ "$ORCH_TEST_PAUSE_MV" = 1 ]; then case "$*" in *.tree.pid)\n'
        f": > {shlex.quote(str(paused))}\n"
        f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done;;\n"
        "esac; fi",
    )
    member = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, time\nfrom pathlib import Path\n"
            f"while not Path({str(go)!r}).exists(): time.sleep(.02)\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
            "time.sleep(60)\n",
        ]
    )
    first = second = None
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        directory = Path(target.job_dir)
        directory.mkdir(parents=True)
        record = directory / remote.tree_record(target.require_tag())
        record.write_text(f"{boot}\n{member.pid} {process_start(member.pid)}\n", encoding="utf-8")
        check = ["sh", "-c", remote.check_script(target)]
        first = subprocess.Popen(
            check,
            env={
                **os.environ,
                "PATH": f"{shims}:{os.environ['PATH']}",
                "ORCH_TEST_PAUSE_MV": "1",
            },
        )
        wait_until(paused.exists, "the first check scanned and reached its rename")
        go.touch()
        wait_until(child_pid.exists, "the member started its child after that scan")
        child = int(child_pid.read_text())
        second = subprocess.Popen(check)
        try:
            second.wait(timeout=UNLOCKED_WRITE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        release.touch()

        assert first.wait(timeout=20) == 1, "the first check finds the member running"
        assert second.wait(timeout=20) == 1, "the second check finds the member running"
        lines = record.read_text(encoding="utf-8").splitlines()
        entries = dict(line.split() for line in lines[1:])
        assert entries.get(str(child)) == process_start(child), (
            f"the child the second check saw is still recorded: {lines}"
        )
    finally:
        release.touch()
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        member.kill()
        member.wait()


def stat_line(pid: int, session: int, start: int) -> str:
    return f"{pid} (sleep) S 1 {session} {session} " + "0 " * 15 + f"{start}\n"


SCOPE_SLICE = "/user.slice/user-1000.slice/user@1000.service/app.slice"
RECORDED_SESSION = 456789


class SyntheticDevice:
    def __init__(self, tmp_path: Path):
        self.proc = tmp_path / "proc"
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-current\n")
        (self.proc / "self").mkdir()
        (self.proc / "self/stat").write_text("readable\n")
        stubs = tmp_path / "bin"
        stubs.mkdir()
        for name, body in {
            "uname": "echo Linux",
            "grep": 'case "$*" in *"-qzxF x") exit 0;; *) exit 1;; esac',
        }.items():
            (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
            (stubs / name).chmod(0o755)
        self.env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
        self.target = remote.RemoteTarget(
            "pc", "pc", "claude-bot", str(tmp_path / "work"), "job", "a" * 32
        )
        self.job = Path(self.target.job_dir)
        self.job.mkdir(parents=True)
        (self.job / remote.start_record(self.target.require_tag())).write_text(
            f"{RECORDED_SESSION} boot-current {self.target.tag}\n"
        )
        (self.job / remote.PID_FILE).write_text(f"{RECORDED_SESSION} boot-current\n")

    def show(self, process: subprocess.Popen, session: int, cgroup: str | None = None) -> None:
        shown = self.proc / str(process.pid)
        shown.mkdir()
        (shown / "environ").write_bytes(b"PATH=/usr/bin\0")
        (shown / "stat").write_text(stat_line(process.pid, session, 9000))
        if cgroup is not None:
            (shown / "cgroup").write_text(f"0::{cgroup}\n")

    def run(self, script: str) -> int:
        return subprocess.run(
            ["sh", "-c", script.replace("/proc", str(self.proc))],
            capture_output=True,
            env=self.env,
        ).returncode

    def tree(self) -> list[str]:
        record = self.job / remote.tree_record(self.target.require_tag())
        return record.read_text().splitlines() if record.exists() else []


@linux_path
@pytest.mark.parametrize(("state", "expected"), [("Z", 0), ("S", 3)], ids=["zombie", "live"])
def test_the_recorded_group_holds_the_check_only_while_a_live_process_is_in_it(
    tmp_path: Path, state: str, expected: int
) -> None:
    """#827 f-7: an exited keeper its adopter has not reaped stays a zombie in
    the start's group; a zombie runs nothing, so it never makes the check unsure."""
    device = SyntheticDevice(tmp_path)
    left = device.proc / "4242"
    left.mkdir()
    (left / "stat").write_text(
        stat_line(4242, RECORDED_SESSION, 9000).replace(" S ", f" {state} ", 1)
    )
    assert device.run(remote.check_script(device.target)) == expected


@linux_path
@pytest.mark.parametrize("member_in_session", [False, True])
def test_a_recorded_session_counts_only_while_a_proven_member_is_in_it(
    tmp_path: Path, member_in_session: bool
) -> None:
    """#722 f-5: the start's session number, from a record that outlived the
    session, is reused by an unrelated session whose leader then exits.
    """
    device = SyntheticDevice(tmp_path)
    target, job, session = device.target, device.job, RECORDED_SESSION
    stray = subprocess.Popen(["sleep", "60"])
    member = subprocess.Popen(["sleep", "60"])
    for process in [stray] + ([member] if member_in_session else []):
        device.show(process, session)
    if member_in_session:
        (job / remote.tree_record(target.tag)).write_text(f"boot-current\n{member.pid} 9000\n")

    try:
        check = device.run(remote.check_script(target))
        device.run(remote.stop_script(target, kill=True))
        if member_in_session:
            assert check == 1, "a recorded member keeps the run alive"
            assert stray.wait(timeout=5) == -signal.SIGKILL, (
                "a process in the session of a proven member is the run's"
            )
            return
        assert check != 1, "a session number alone never names the run"
        assert not (job / remote.tree_record(target.tag)).exists()
        time.sleep(0.2)
        assert stray.poll() is None, "a process outside the run is never signalled"
    finally:
        for process in (stray, member):
            process.kill()
            process.wait()


@linux_path
@pytest.mark.parametrize("own_scope", [True, False])
def test_a_contained_runs_descendant_that_left_its_session_and_tag_is_kept_and_killed(
    tmp_path: Path, own_scope: bool
) -> None:
    """#817 f-2: a descendant that called setsid and dropped the tag, whose
    parent exited before any check saw it, is found by the run's scope."""
    device = SyntheticDevice(tmp_path)
    target = device.target
    unit = f"orch-run-{target.tag if own_scope else 'b' * 32}.scope"
    (device.job / remote.scope_record(target.require_tag())).write_text(f"{unit}\n")
    victim = subprocess.Popen(["sleep", "60"])
    device.show(victim, session=victim.pid, cgroup=f"{SCOPE_SLICE}/{unit}")
    try:
        check = device.run(remote.check_script(target))
        if own_scope:
            assert check == 1, "a process in the run's scope keeps the run alive"
            assert f"{victim.pid} 9000" in device.tree(), "the tree record names it"
            device.run(remote.stop_script(target, kill=True))
            assert victim.wait(timeout=5) == -signal.SIGKILL, "the kill reaches it"
            return
        assert check != 1, "a scope record naming another run's unit is ignored"
        device.run(remote.stop_script(target, kill=True))
        time.sleep(0.2)
        assert victim.poll() is None, "a process outside the run is never signalled"
    finally:
        victim.kill()
        victim.wait()


@linux_path
def test_a_contained_run_never_counts_its_recorded_session(tmp_path: Path) -> None:
    """#817 ac-3: a contained run's members are its scope's processes, so a
    process in the recorded session number but another cgroup is not one,
    even while a proven member is in that session."""
    device = SyntheticDevice(tmp_path)
    target, session = device.target, RECORDED_SESSION
    unit = f"orch-run-{target.tag}.scope"
    (device.job / remote.scope_record(target.require_tag())).write_text(f"{unit}\n")
    member = subprocess.Popen(["sleep", "60"])
    stray = subprocess.Popen(["sleep", "60"])
    device.show(member, session, cgroup=f"{SCOPE_SLICE}/{unit}")
    device.show(stray, session, cgroup="/user.slice/user-1000.slice/session-4.scope")
    (device.job / remote.tree_record(target.tag)).write_text(f"boot-current\n{member.pid} 9000\n")
    try:
        assert device.run(remote.check_script(target)) == 1
        assert f"{stray.pid} 9000" not in device.tree(), "the check does not count it"
        device.run(remote.stop_script(target, kill=True))
        assert member.wait(timeout=5) == -signal.SIGKILL
        time.sleep(0.2)
        assert stray.poll() is None, "a process outside the run is never signalled"
    finally:
        for process in (stray, member):
            process.kill()
            process.wait()


@linux_path
def test_an_uncontained_runs_reused_session_number_is_never_killed(tmp_path: Path) -> None:
    """#817 ac-3: the recorded session number now belongs to an unrelated
    real session whose leader has exited."""
    config = config_of(machines_file(tmp_path))
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", uuid.uuid4().hex)
    assert target is not None
    left = tmp_path / "sleep.pid"
    leader = subprocess.Popen(
        ["sh", "-c", f"sleep 60 > /dev/null 2>&1 & echo $! > {shlex.quote(str(left))}"],
        start_new_session=True,
    )
    assert leader.wait(timeout=5) == 0
    sleeper = int(left.read_text())
    try:
        assert Path(f"/proc/{sleeper}/stat").read_text().rsplit(") ", 1)[1].split()[3] == str(
            leader.pid
        ), "the sleep is in the session the record names"
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        job = Path(target.job_dir)
        job.mkdir(parents=True)
        (job / remote.start_record(target.require_tag())).write_text(
            f"{leader.pid} {boot} {target.tag}\n"
        )
        (job / remote.PID_FILE).write_text(f"{leader.pid} {boot}\n")
        shell = LocalShell()
        assert remote.check_worker(shell, target, fence=False) != remote.RUNNING
        assert remote.stop_worker(shell, target, kill=True)
        time.sleep(0.2)
        state = Path(f"/proc/{sleeper}/stat").read_text().rsplit(") ", 1)[1].split()[0]
        assert state == "S", "a process outside the run is never signalled"
    finally:
        kill_quietly(sleeper)


def scope_usable() -> bool:
    if sys.platform != "linux" or shutil.which("systemd-run") is None:
        return False
    probe = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"]
    return subprocess.run(probe, capture_output=True, timeout=30).returncode == 0


contained_path = pytest.mark.skipif(
    not scope_usable(), reason="needs a Linux user manager that starts scopes"
)


@pytest.mark.parametrize(
    "linger", [pytest.param("yes", marks=contained_path), pytest.param("no", marks=linux_path)]
)
def test_a_linux_launch_runs_in_a_scope_only_when_lingering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linger: str
) -> None:
    """#817: with a lingering user manager, a descendant the worker starts
    in a new session with a clean environment as it exits on SIGTERM stays
    the run's, and the kill ends it. Without, the run starts uncontained."""
    box = Box(tmp_path, monkeypatch)
    tools = tmp_path / "device-bin"
    tools.mkdir()
    (tools / "loginctl").write_text(f"#!/bin/sh\necho {linger}\n")
    (tools / "loginctl").chmod(0o755)
    monkeypatch.setenv("FAKE_SSH_PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    ready, tick, leaf_pid = tmp_path / "worker.pid", tmp_path / "tick", tmp_path / "leaf.pid"
    leaf = f"""import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(leaf_pid)!r}).write_text(str(os.getpid()))
while True:
    Path({str(tick)!r}).write_text(str(time.monotonic_ns()))
    time.sleep(.02)
"""
    worker = f"""import os, signal, subprocess, sys, time
from pathlib import Path
def leave(*_):
    subprocess.Popen([sys.executable, '-c', {leaf!r}], env={{'PATH': os.environ['PATH']}},
                     start_new_session=True)
    os._exit(0)
signal.signal(signal.SIGTERM, leave)
Path({str(ready)!r}).write_text(str(os.getpid()))
time.sleep(120)
"""
    target, unit = box.target, f"orch-run-{box.target.tag}.scope"
    record = Path(target.job_dir) / remote.scope_record(target.require_tag())
    try:
        result = box.send(box.launch(command=[sys.executable, "-c", worker]))
        assert result.stdout.decode().strip() == f"{remote.LAUNCH_MARK} started", result.stderr
        worker_pid = int(wait_file(ready))
        cgroup = Path(f"/proc/{worker_pid}/cgroup").read_text().strip()
        said = (box.run_dir() / "log").read_text().splitlines()
        assert said.count("orch: the run is uncontained (no systemd scope)") == (
            1 if linger == "no" else 0
        ), said
        if linger == "no":
            assert not record.exists(), "no scope without a lingering user manager"
            assert not cgroup.endswith(unit)
            assert remote.check_worker(box.transport, target, fence=False) == remote.RUNNING
            return
        assert record.read_text() == f"{unit}\n"
        assert cgroup.startswith("0::/") and cgroup.endswith(f"/{unit}"), cgroup

        assert remote.stop_worker(box.transport, target)
        leaf = int(wait_file(leaf_pid))
        wait_file(tick)
        assert wait_gone(worker_pid), "the worker exited on SIGTERM"
        box.wait_exit()
        wrapper = int((box.run_dir() / "pid").read_text().split()[0])
        assert wait_gone(wrapper), "the wrapper exited after its start"
        assert remote.check_worker(box.transport, target, fence=False) == remote.RUNNING, (
            "an untagged descendant in its own session keeps the contained run alive"
        )
        assert remote.stop_worker(box.transport, target, kill=True)
        assert wait_gone(leaf, 5.0), "the kill reached the descendant"
        assert remote.check_worker(box.transport, target, fence=False) == remote.GONE
    finally:
        remote.stop_worker(box.transport, target, kill=True)
        box.cleanup()
        kill_leftover(tmp_path)


# Holds the keeper's session: forks the keeper and a companion `sleep` in a
# session of their own, then, as their child subreaper, adopts both and never
# reaps one unless it reads its pid on stdin. A dead companion can so stay a
# zombie whose parent is outside the keeper's session.
KEEPER_HOLDER = """import ctypes, os, subprocess, sys
assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
leader = subprocess.Popen(
    ["sh", "-c", 'sh -c "$K" orch-keeper </dev/null >/dev/null 2>&1 & k=$!; sleep 60 </dev/null >/dev/null 2>&1 & echo "$k $!"'],
    stdout=subprocess.PIPE, text=True, start_new_session=True,
)
print(leader.communicate()[0].strip(), flush=True)
for line in sys.stdin:
    os.waitpid(int(line), 0)
"""


def ignored_signals(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("SigIgn:"):
            return int(line.split()[1], 16)
    raise AssertionError(f"no SigIgn line for {pid}")


def zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] == "Z"
    except FileNotFoundError:
        return False


@linux_path
@pytest.mark.parametrize("reaped", [True, False], ids=["companion-reaped", "companion-a-zombie"])
def test_the_keeper_stays_through_stop_signals_until_it_is_alone_in_its_session(
    reaped: bool,
) -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", KEEPER_HOLDER],
        env={**os.environ, "K": remote.KEEPER},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdin is not None and holder.stdout is not None
    keeper, companion = (int(pid) for pid in holder.stdout.readline().split())
    stop_signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    mask = sum(1 << (signum - 1) for signum in stop_signals)
    try:
        wait_until(
            lambda: pid_alive(keeper, None) and ignored_signals(keeper) & mask == mask,
            "the keeper ignores TERM, HUP and INT",
        )
        for signum in stop_signals:
            os.kill(keeper, signum)
        time.sleep(0.6)
        assert pid_alive(keeper, None), "the keeper stays while another process is in its session"

        os.kill(companion, signal.SIGKILL)
        if reaped:
            holder.stdin.write(f"{companion}\n")
            holder.stdin.flush()
            wait_until(lambda: not Path(f"/proc/{companion}").exists(), "the companion was reaped")
        else:
            wait_until(
                lambda: zombie(companion), "the companion is a zombie in the keeper's session"
            )
        wait_until(
            lambda: not pid_alive(keeper, None),
            "the keeper exits once no live process shares its session",
            2.0,
        )
    finally:
        kill_all(keeper, companion)
        holder.kill()
        holder.wait()


def command_substitutions(script: str) -> list[str]:
    """Each `$(...)` body, ended where bash 3.2 ends it: at the `)` that
    balances its unquoted parentheses, so a case pattern's `)` ends it."""
    bodies, at = [], script.find("$(")
    while at >= 0:
        end, depth, quote = at + 2, 1, None
        while end < len(script) and depth:
            c = script[end]
            if quote:
                quote = None if c == quote else quote
            elif c in "'\"":
                quote = c
            else:
                depth += {"(": 1, ")": -1}.get(c, 0)
            end += 1
        if not script.startswith("$((", at):
            bodies.append(script[at + 2 : end - 1])
        at = script.find("$(", at + 2)
    return bodies


def generated_scripts(target: remote.RemoteTarget) -> dict[str, str]:
    return {
        "check": remote.check_script(target),
        "stop": remote.stop_script(target),
        "kill": remote.stop_script(target, kill=True),
        "fenced check": remote.fenced_check_script(target),
    }


def test_every_generated_remote_script_parses_under_the_native_sh() -> None:
    """#722 f-4: macOS's /bin/sh (bash 3.2) ends a `$(...)` at an unquoted
    `)` such as a case pattern's, so each body must parse cut there too."""
    target = remote.RemoteTarget("pc", "pc", "claude-bot", "/tmp/w", "job", "a" * 32)
    inners = {name: shlex.split(script)[3] for name, script in generated_scripts(target).items()}
    inners["start"] = shlex.split(remote.start_script(target, ["true"], {}))[5]
    inners["keeper"] = remote.KEEPER
    for name, inner in inners.items():
        for code in [inner, *command_substitutions(inner)]:
            result = subprocess.run(["/bin/sh", "-n", "-c", code], capture_output=True, text=True)
            assert result.returncode == 0, f"{name}: {result.stderr} in {code[:80]!r}"


def test_every_generated_remote_script_runs_under_the_native_sh(tmp_path: Path) -> None:
    gone, unsure = 0, 3
    target = remote.RemoteTarget("pc", "pc", "claude-bot", str(tmp_path / "work"), "job", "a" * 32)
    for name, script in generated_scripts(target).items():
        inner = shlex.split(script)[3]
        result = subprocess.run(
            ["/bin/sh", "-c", inner], capture_output=True, text=True, timeout=60
        )
        print(f"{name}: exit {result.returncode} {result.stderr!r}")
        assert result.returncode in (gone, unsure), (
            f"{name}: exit {result.returncode} {result.stderr}"
        )
