"""#128: `orch job run` on a remote device rides out dropped SSH links.

End to end over tests/fake_ssh.py (#112's link faults): the waiter or
`orch job run` launches the harness detached on the "device" (this
machine), follows it with short calls, and finishes it. The harness is
tests/test_detached_remote.py's: it counts its starts, so a duplicate launch
shows.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import placed_run, remote
from lib.device_pool import DevicePool
from lib.machines_yml import load_machines_yml
from lib.queue import SQLiteJobQueue
from lib.remote_follow import EXITED, KILL_AFTER_SECONDS, STOPPED, FollowEnd
from lib.waiter import HarnessCommand, Waiter
from tests.fake_docker import Docker
from tests.handoff_store import serve_job
from tests.test_detached_remote import HARNESS
from tests.test_job_run import START_CONTAINER
from tests.test_job_state import Poller
from tests.test_liveness_requeue import _remote
from tests.test_remote_darwin import Mac, mac_env
from tests.test_remote_start import (
    Ssh,
    gone,
    job_dir,
    kill_quietly,
    orch,
    past_uncontained,
    wait_file,
    wait_gone,
)
from tests.test_waiter import FakeContext, enqueue, slots
from tests.test_waiter_remote import ORCH, remote_machines

CODEX = "fakepc-codex-bot"


def harness(count: Path, lines: int, pause: float) -> list[str]:
    return [sys.executable, "-c", HARNESS, str(count), str(lines), str(pause)]


def events(db: Path, job_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()


def run_dirs(tmp_path: Path, bot: str, job_id: str) -> list[Path]:
    run = job_dir(tmp_path, bot, job_id).with_name(f"{job_id}.run")
    return sorted(path for path in run.iterdir() if path.is_dir()) if run.exists() else []


def device_pids(tmp_path: Path, bot: str, job_id: str) -> tuple[int, int]:
    [rundir] = run_dirs(tmp_path, bot, job_id)
    wrapper, start = wait_file(rundir / "pid").split()
    return int(wrapper), int(start)


def kill_ssh_clients(ssh: Ssh) -> int:
    """SIGKILL every fake ssh client still running; how many were."""
    killed = 0
    for call in ssh.calls():
        pid = call["pid"]
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if b"fake_ssh.py" in command and not gone(pid):
            kill_quietly(pid)
            killed += 1
    return killed


def started_harness(count: Path, proc: subprocess.Popen | None = None) -> int:
    return int(wait_file(count, proc).split()[0])


def setup(tmp_path: Path):
    """A job placed on the pc, a database with the dispatcher's tables, the fake ssh."""
    poller, job_id, ssh = _remote(tmp_path)
    return poller, poller.path, job_id, ssh


def run(poller: Poller, job_id: str, ssh: Ssh, *args: str, **kwargs) -> subprocess.Popen:
    return orch(
        "job", "run", "codex-bot", job_id, "--db", str(poller.path),
        "--machines", str(poller.machines), *args,
        env={**ssh.env(), **kwargs.pop("env", {})}, **kwargs,
    )


def heartbeat(db: Path, job_id: str) -> float:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT last_activity_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]


def test_a_remote_job_survives_a_forced_ssh_kill_and_completes(tmp_path: Path) -> None:
    """Done-when: SSH clients killed and the device offline mid-run; one harness, done."""
    machines = remote_machines(tmp_path, box_enabled=False)
    ssh = Ssh(tmp_path)
    alias = "fakepc-claude-bot"
    count = tmp_path / "count"
    db = Poller(tmp_path / "schema").path  # the dispatcher's tables, for job_events
    queue = SQLiteJobQueue(db, clock=time.time)
    config, _ = load_machines_yml(machines)
    log: list[str] = []
    waiter = Waiter(
        bot="claude-bot",
        queue=queue,
        pool=DevicePool(queue, config),
        harness=HarnessCommand(bot="claude-bot", command=tuple(harness(count, 40, 0.1))),
        context=FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=machines,
        orch=(sys.executable, str(ORCH)),
        spawn=subprocess.Popen,
        log=log.append,
        environ={"HOME": str(tmp_path), **ssh.env()},
    )
    job_id = enqueue(queue, "a")
    serve_job(db, job_id, "123456")
    assert waiter.step() == job_id
    first = started_harness(count)
    try:
        # A call in flight when the link dies: hold the next one in the client.
        delay = ssh.fault(alias, "delay", 1)
        deadline = time.monotonic() + 10
        while not kill_ssh_clients(ssh):
            assert time.monotonic() < deadline, "no ssh call was in flight"
            time.sleep(0.05)
        offline = ssh.offline(alias)
        offline.touch()
        kill_ssh_clients(ssh)
        time.sleep(2.5)
        assert not gone(first)  # the harness never noticed
        offline.unlink()
        delay.unlink()
        deadline = time.monotonic() + 60
        while waiter.running() and time.monotonic() < deadline:
            time.sleep(0.1)
            waiter.step()
        assert not waiter.running(), log
    finally:
        kill_quietly(first)
    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    run_log = (tmp_path / "state" / "jobs" / job_id / "run.log").read_bytes()
    assert row == ("done", "123456"), run_log.decode()
    assert count.read_text().split() == [str(first)]  # one harness, ever
    [rundir] = run_dirs(tmp_path, "claude-bot", job_id)
    device_log = (rundir / "log").read_bytes()
    assert device_log.endswith(b"ORCH-HANDOFF: 123456\n")
    # #93: the start's worker-environment report is in neither log, and its
    # private `env` file is gone once the follower relayed it; its line
    # break is what stays in the device log.
    report, relayed = past_uncontained(device_log).split(b"\n", 1)
    assert report == b"" and not (rundir / "env").exists()
    assert b"ORCH-WORKER-ENV" not in device_log and b"ORCH-WORKER-ENV" not in run_log
    assert run_log.count(relayed) == 1 and run_log.count(b"line 0 ") == 1
    assert b"reattached" in run_log  # the runner's own stderr line, in run.log too
    assert "run-died" not in [event for event, _ in events(db, job_id)]
    assert slots(db) == {}
    brief = (tmp_path / "state" / "jobs" / job_id / "brief.md").read_bytes()
    assert Path(f"{count}.brief").read_bytes() == brief == (rundir / "stdin").read_bytes()


def test_dropped_polls_mid_run_relay_the_device_log_exactly(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--", *harness(count, 40, 0.1))
    try:
        started_harness(count, proc)
        drop = ssh.fault(CODEX, "drop-after", 0)
        time.sleep(2)
        drop.unlink()
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert "lost the link to pc" in err and "reattached" in err
    [rundir] = run_dirs(tmp_path, "codex-bot", job_id)
    # #93: the worker-environment report never entered the device log.
    device_log = (rundir / "log").read_bytes()
    assert b"ORCH-WORKER-ENV" not in device_log and not (rundir / "env").exists()
    assert out.encode() == device_log
    assert len(count.read_text().split()) == 1
    assert "run-died" not in [event for event, _ in events(db, job_id)]
    assert poller.pool.placement(job_id) is None


def test_heartbeats_keep_beating_while_the_device_is_offline(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--interval", "0.3", "--", *harness(count, 40, 0.1))
    try:
        started_harness(count, proc)
        offline = ssh.offline(CODEX)
        offline.touch()
        before = heartbeat(db, job_id)
        time.sleep(1.5)
        during = heartbeat(db, job_id)
        offline.unlink()
        _, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert during > before
    assert proc.returncode == 0, err
    assert poller.row(job_id)["state"] == "running"


def test_a_signal_to_the_runner_stops_the_remote_worker(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--", *harness(count, 600, 0.1))
    worker = None
    try:
        worker = started_harness(count, proc)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 128 + signal.SIGTERM, err
        assert "stopping it" in err
        assert wait_gone(worker)
        [rundir] = run_dirs(tmp_path, "codex-bot", job_id)
        assert (rundir / "exit").read_text().strip() == str(128 + signal.SIGTERM)
        assert poller.pool.placement(job_id) is None
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


def test_a_cancel_kills_a_remote_worker_that_ignores_sigterm(tmp_path: Path) -> None:
    """Codex PR #129 r1 F2, with the real timings: SIGTERM, then SIGKILL; gone, slot freed."""
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    command = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(count)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(600)"
    )
    proc = run(poller, job_id, ssh, "--", sys.executable, "-c", command)
    worker = None
    try:
        worker = started_harness(count, proc)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=KILL_AFTER_SECONDS + 60)
        assert proc.returncode == 128 + signal.SIGTERM, err
        assert "stopping it" in err and "killing it" in err, err
        assert gone(worker)
        assert poller.pool.placement(job_id) is None
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize("darwin", [False, True], ids=["linux", "darwin"])
def test_a_cancel_stops_the_whole_unit_when_only_a_child_ignores_sigterm(
    tmp_path: Path, darwin: bool
) -> None:
    """Codex PR #129 r2: SIGTERM ends the harness (the group leader) but not its
    child; the cancel still kills the child, then frees the slot."""
    poller, db, job_id, ssh = setup(tmp_path)
    env = mac_env(ssh, Mac(tmp_path)) if darwin else {}
    child_file, parent_file = tmp_path / "child.pid", tmp_path / "parent.pid"
    child_command = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(child_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(600)"
    )
    parent_command = (
        "import os, subprocess, sys; "
        f"open({str(parent_file)!r}, 'w').write(str(os.getpid())); "
        f"subprocess.Popen([sys.executable, '-c', {child_command!r}]).wait()"
    )
    proc = run(poller, job_id, ssh, "--", sys.executable, "-c", parent_command, env=env)
    parent = child = None
    try:
        parent = int(wait_file(parent_file, proc))
        child = int(wait_file(child_file, proc))
        assert os.getpgid(child) == os.getpgid(parent) == parent
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=KILL_AFTER_SECONDS + 60)
        assert proc.returncode == 128 + signal.SIGTERM, err
        assert wait_gone(child), err
        assert gone(parent)
        assert poller.pool.placement(job_id) is None, err
    finally:
        kill_quietly(child)
        kill_quietly(parent)
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("darwin", [False, True], ids=["linux", "darwin"])
def test_a_cancel_stops_a_child_the_harness_forks_while_it_shuts_down(
    tmp_path: Path, darwin: bool
) -> None:
    """Codex PR #129 r3: the harness's SIGTERM handler forks a child that
    ignores SIGTERM, then exits. The child was born after any stop looked at
    the group; the cancel still kills it, then frees the slot."""
    poller, db, job_id, ssh = setup(tmp_path)
    env = mac_env(ssh, Mac(tmp_path)) if darwin else {}
    child_file, parent_file = tmp_path / "child.pid", tmp_path / "parent.pid"
    command = (
        "import os, signal, time\n"
        "def stop(signum, frame):\n"
        "    if os.fork() == 0:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"        open({str(child_file)!r}, 'w').write(str(os.getpid()))\n"
        "        time.sleep(600)\n"
        "        os._exit(0)\n"
        "    os._exit(143)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"open({str(parent_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(600)\n"
    )
    proc = run(poller, job_id, ssh, "--", sys.executable, "-c", command, env=env)
    parent = child = None
    try:
        parent = int(wait_file(parent_file, proc))
        proc.send_signal(signal.SIGTERM)
        child = int(wait_file(child_file, proc))
        assert os.getpgid(child) == parent
        _, err = proc.communicate(timeout=KILL_AFTER_SECONDS + 60)
        assert wait_gone(child), err
        assert gone(parent)
        assert poller.pool.placement(job_id) is None, err
    finally:
        if child is None and child_file.exists():
            child = int(child_file.read_text())
        kill_quietly(child)
        kill_quietly(parent)
        if proc.poll() is None:
            proc.kill()
            proc.wait()


class StopsOnlyOnKill:
    """The device: the worker runs on until a SIGKILL stop; records each stop sent."""

    def __init__(self) -> None:
        self.stops: list[str] = []
        self.check = ""

    def run(self, argv, *, timeout: float) -> int:
        script = argv[-1]
        for sig in ("TERM", "KILL"):
            if f"kill -{sig}" in script:
                self.stops.append(sig)
                return 0
        if script == self.check:
            return 0 if "KILL" in self.stops else 1
        return 0


def placed_for_finish(tmp_path: Path, end: FollowEnd):
    poller, _, job_id, _ = setup(tmp_path)
    transport = StopsOnlyOnKill()
    placed = placed_run.prepare(
        poller.pool, poller.config, job_id=job_id, bot="codex-bot", owner="codex",
        worktree=False, log=lambda _: None, transport=transport, alive=lambda *_: True,
    )
    assert placed.target is not None
    transport.check = remote.fenced_check_script(placed.target)
    placed.before_start()
    placed.follow_end = end
    placed.sleep = lambda _: None
    return poller, job_id, placed, transport


def test_finish_kills_a_cancelled_worker_that_ignores_sigterm(tmp_path: Path) -> None:
    """Codex PR #129 r1 F2: the follower gave up unconfirmed; finish escalates too."""
    poller, job_id, placed, transport = placed_for_finish(
        tmp_path, FollowEnd(STOPPED, 143, "not confirmed stopped")
    )
    assert placed.finish(143) is True
    assert transport.stops == ["TERM", "KILL"]
    assert poller.pool.placement(job_id) is None


def test_finish_never_kills_a_leftover_of_a_run_that_was_not_cancelled(tmp_path: Path) -> None:
    poller, job_id, placed, transport = placed_for_finish(tmp_path, FollowEnd(EXITED, 0))
    assert placed.finish(0) is False
    assert transport.stops == ["TERM"]
    assert poller.pool.placement(job_id) is not None


def test_a_job_ended_elsewhere_stops_its_remote_worker(tmp_path: Path) -> None:
    """A timeout (or a fail) ends the job; the heartbeat sees it and the unit is stopped."""
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--interval", "0.3", "--", *harness(count, 600, 0.1))
    worker = None
    try:
        worker = started_harness(count, proc)
        poller.queue.finish(job_id, owner="codex", handoff_comment_id=None)
        _, err = proc.communicate(timeout=60)
        assert "heartbeat stopped" in err and "stopping it" in err, err
        assert wait_gone(worker)
        [rundir] = run_dirs(tmp_path, "codex-bot", job_id)
        assert (rundir / "exit").read_text().strip() == str(128 + signal.SIGTERM)
        assert poller.row(job_id)["state"] == "failed"
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


def test_a_long_outage_is_run_died_lost_and_leaves_the_worker_alone(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--follow-grace", "2", "--", *harness(count, 600, 0.1))
    worker = None
    try:
        worker = started_harness(count, proc)
        ssh.offline(CODEX).touch()
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 255, err
        [(event, detail)] = [e for e in events(db, job_id) if e[0] == "run-died"]
        assert "lost contact with pc" in detail and "not confirmed gone" in detail
        assert not gone(worker)  # never stopped: it may still finish
        assert poller.pool.placement(job_id) is not None  # held for the device check
        assert poller.row(job_id)["state"] == "running"
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


def test_a_worker_killed_on_the_device_is_run_died_gone(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--", *harness(count, 600, 0.1))
    try:
        started_harness(count, proc)
        wrapper, start = device_pids(tmp_path, "codex-bot", job_id)
        os.kill(wrapper, signal.SIGKILL)
        os.killpg(start, signal.SIGKILL)
        _, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 255, err
    died = [d for e, d in events(db, job_id) if e == "run-died"]
    assert died == ["worker gone on pc without an exit status"]
    assert poller.pool.placement(job_id) is None


def test_the_brief_file_is_the_harness_stdin_on_the_device(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    count = tmp_path / "count"
    brief = tmp_path / "brief.md"
    brief.write_bytes(b"# Brief\n\nbuild it \xe2\x9c\x93\n")
    with brief.open("rb") as stdin:
        proc = run(
            poller, job_id, ssh, "--", *harness(count, 1, 0),
            env={"ORCH_JOB_BRIEF": str(brief)}, stdin=stdin,
        )
        _, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert Path(f"{count}.brief").read_bytes() == brief.read_bytes()


def test_a_remote_runs_teardown_removes_its_labelled_containers_on_the_device(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    docker = Docker(tmp_path)
    docker.start(app="web")
    proc = run(poller, job_id, ssh, "--", sys.executable, "-c", START_CONTAINER,
               env={"PATH": docker.path(ssh.env()["PATH"]), "ORCH_TELEMETRY": "off"})
    try:
        _, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert any(call[:4] == ["run", "-d", "--label", f"factory.job={job_id}"] for call in docker.calls())
    assert docker.labelled() == [""]
    removals = [call for call in ssh.calls()
                if f"label=factory.job={job_id}" in (call["script"] or "")]
    assert len(removals) == 1 and removals[0]["alias"] == CODEX
