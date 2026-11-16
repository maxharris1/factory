"""#199: `orch job run`'s wall-clock budget.

A harness still running when the job's budget (its timeout_seconds) runs
out is stopped, SIGTERM then SIGKILL, and the job fails `wall-clock` only
once the harness is confirmed dead, so T6 never starts a retry beside it.
Every test uses a temp database and fakes (tests/test_job_state.py's Poller,
tests/fake_ssh.py for a remote device); nothing reaches a real harness.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

from lib import cli, job_run, telemetry
from lib.job_state import RUN_DIED
from lib.job_run import WALL_CLOCK, Budget
from tests.test_detached_remote_cli import harness, run_dirs, started_harness
from tests.test_job_state import Poller
from tests.test_liveness_requeue import _details, _job_run
from tests.test_remote_start import Ssh, gone, kill_quietly, orch, wait_gone

IGNORES_TERM = (
    "import os, signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open(sys.argv[1], 'w').write(str(os.getpid())); "
    "time.sleep(600)"
)


def _box(tmp_path: Path, timeout: int) -> tuple[Poller, str]:
    poller = Poller(tmp_path, now=time.time(), box_slots=3, pc=False)
    poller.queue.clock = time.time
    job_id = poller.review(timeout=timeout)
    assert poller.claim().device == "box"
    return poller, job_id


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --- job_run.run: the budget stops the child, and returns only once it is dead -------------


def test_a_child_past_its_budget_gets_sigterm_and_run_returns_once_it_is_reaped() -> None:
    budget = Budget(0.5, grace=5)
    lines: list[str] = []
    started = time.monotonic()
    code = job_run.run(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        beat=lambda: None, interval=0.1, log=lines.append, budget=budget,
    )
    assert code == 128 + signal.SIGTERM
    assert time.monotonic() - started < 5
    assert budget.expired and budget.signum == signal.SIGTERM
    assert any("ran past its" in line and "(SIGTERM)" in line for line in lines), lines


def test_a_child_that_ignores_sigterm_is_killed_and_confirmed_dead_before_run_returns(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pid"
    budget = Budget(1.0, grace=0.5)
    lines: list[str] = []
    code = job_run.run(
        [sys.executable, "-c", IGNORES_TERM, str(pid_file)],
        beat=lambda: None, interval=0.1, log=lines.append, budget=budget,
    )
    pid = int(pid_file.read_text())
    assert code == 128 + signal.SIGKILL
    assert budget.signum == signal.SIGKILL
    assert not _alive(pid)  # reaped: its death is confirmed when run returns
    assert any("ignored SIGTERM" in line and "SIGKILL" in line for line in lines), lines


def test_a_child_within_its_budget_is_left_alone() -> None:
    budget = Budget(30)
    code = job_run.run(
        [sys.executable, "-c", "import sys; sys.exit(4)"],
        beat=lambda: None, interval=0.1, log=lambda _m: None, budget=budget,
    )
    assert code == 4 and not budget.expired and budget.signum is None


def test_a_budget_reads_in_minutes_when_it_is_whole_minutes() -> None:
    assert Budget(3600).describe() == "60-minute wall-clock budget"
    assert Budget(90).describe() == "90-second wall-clock budget"


def test_a_budget_must_be_positive() -> None:
    for seconds in (0, -1, float("nan"), float("inf")):
        try:
            Budget(seconds)
        except ValueError:
            continue
        raise AssertionError(f"Budget({seconds}) was accepted")


# --- job_run.follow: the budget stops a remote run's Control -------------------------------


def test_a_remote_run_past_its_budget_has_its_control_stopped() -> None:
    budget = Budget(0.3)
    seen: list[int | None] = []

    def body(control: job_run.Control) -> int:
        deadline = time.monotonic() + 10
        while control.signum is None and time.monotonic() < deadline:
            control.wait(0.05)
        seen.append(control.signum)
        return 128 + (control.signum or 0)

    code = job_run.follow(body, beat=lambda: None, interval=0.1, log=lambda _m: None, budget=budget)
    assert seen == [signal.SIGTERM] and code == 128 + signal.SIGTERM
    assert budget.expired


def test_a_remote_run_that_ends_in_time_cancels_its_budget_timer() -> None:
    budget = Budget(0.3)
    code = job_run.follow(
        lambda _control: 0, beat=lambda: None, interval=0.1, log=lambda _m: None, budget=budget
    )
    time.sleep(0.5)
    assert code == 0 and not budget.expired


# --- orch job run on the box: killed, failed `wall-clock`, then (only then) retried --------


def test_a_fake_harness_past_its_budget_is_killed_and_fails_wall_clock(tmp_path: Path) -> None:
    poller, job_id = _box(tmp_path, timeout=2)
    pid_file = tmp_path / "pid"
    script = (
        "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(600)"
    )
    started = time.monotonic()
    code, err = _job_run(poller, job_id, sys.executable, "-c", script, str(pid_file))
    assert code == 128 + signal.SIGTERM, err
    assert time.monotonic() - started < 30
    assert not _alive(int(pid_file.read_text()))
    assert poller.row(job_id)["state"] == "failed"
    assert poller.pool.placement(job_id) is None
    events = _details(poller)
    assert [event for event, _d in events] == [WALL_CLOCK, RUN_DIED], events
    detail = events[0][1]
    assert "ran past its 2-second wall-clock budget" in detail
    assert "stopped with SIGTERM" in detail and "slot-hours lost" in detail
    assert "failed job" in err and "for T6" in err

    poller.now[0] = time.time()
    poller.queue.clock = lambda: poller.now[0]
    poller.dispatcher.reconcile_jobs()
    assert [(state, attempt) for _id, state, attempt in poller.jobs()] == [
        ("failed", 1), ("queued", 2)
    ]


def test_the_wall_clock_stop_and_its_slot_hours_go_into_telemetry(tmp_path: Path) -> None:
    poller, job_id = _box(tmp_path, timeout=2)
    code, err = _job_run(poller, job_id, sys.executable, "-c", "import time; time.sleep(600)")
    assert code == 128 + signal.SIGTERM, err
    with sqlite3.connect(poller.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT exit_reason, slot_hours_lost FROM {telemetry.TABLE} WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    assert row["exit_reason"].startswith("wall-clock (0.00 slot-hours lost)"), row["exit_reason"]
    assert 2 / 3600 <= row["slot_hours_lost"] < 30 / 3600


def test_a_run_within_its_budget_records_no_wall_clock(tmp_path: Path) -> None:
    poller, job_id = _box(tmp_path, timeout=60)
    code, err = _job_run(poller, job_id, sys.executable, "-c", "import sys; sys.exit(3)")
    assert code == 3, err
    assert poller.row(job_id)["state"] == "running"  # left to the bot's orch job done, as before
    assert [event for event, _d in _details(poller)] == [RUN_DIED]


def test_no_retry_starts_while_a_worker_of_the_job_still_runs(tmp_path: Path) -> None:
    """The wall-clock fail waits for every box worker of the job to be gone."""
    poller, job_id = _box(tmp_path, timeout=2)
    lines: list[str] = []
    cli._settle_box(poller.queue, job_id, "codex", lines.append, lambda: 4242, None)
    assert poller.row(job_id)["state"] == "running"
    assert "worker pid 4242 still runs" in lines[0]
    poller.dispatcher.reconcile_jobs()
    assert [(state, attempt) for _id, state, attempt in poller.jobs()] == [("running", 1)]

    cli._settle_box(poller.queue, job_id, "codex", lines.append, lambda: None, None)
    assert poller.row(job_id)["state"] == "failed"
    poller.dispatcher.reconcile_jobs()
    assert [(state, attempt) for _id, state, attempt in poller.jobs()] == [
        ("failed", 1), ("queued", 2)
    ]


# --- orch job run on a remote device: the worker is stopped and confirmed gone -------------


def _remote(tmp_path: Path, timeout: int) -> tuple[Poller, str, Ssh]:
    poller = Poller(tmp_path, box=False, now=time.time())
    poller.queue.clock = time.time
    job_id = poller.review(timeout=timeout)
    assert poller.claim().device == "pc"
    return poller, job_id, Ssh(tmp_path)


def test_a_remote_harness_past_its_budget_is_stopped_before_its_slot_is_freed(
    tmp_path: Path,
) -> None:
    poller, job_id, ssh = _remote(tmp_path, timeout=6)
    count = tmp_path / "count"
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(poller.path),
        "--machines", str(poller.machines), "--", *harness(count, 6000, 0.1),
        env=ssh.env(),
    )
    worker = None
    try:
        worker = started_harness(count, proc)
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 128 + signal.SIGTERM, err
        assert "passed its 6-second wall-clock budget" in err, err
        assert wait_gone(worker)
        [rundir] = run_dirs(tmp_path, "codex-bot", job_id)
        assert (rundir / "exit").read_text().strip() == str(128 + signal.SIGTERM)
        assert poller.pool.placement(job_id) is None  # freed once the device said gone
        events = _details(poller)
        assert [event for event, _d in events][:1] == [WALL_CLOCK], events
        # A remote run's job is failed by its waiter on the box, not here.
        assert poller.row(job_id)["state"] == "running"
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


def test_a_remote_worker_that_ignores_sigterm_is_killed_past_its_budget(tmp_path: Path) -> None:
    poller, job_id, ssh = _remote(tmp_path, timeout=6)
    count = tmp_path / "count"
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(poller.path),
        "--machines", str(poller.machines), "--", sys.executable, "-c", IGNORES_TERM, str(count),
        env=ssh.env(),
    )
    worker = None
    try:
        worker = started_harness(count, proc)
        _, err = proc.communicate(timeout=180)
        assert proc.returncode != 0, err
        assert gone(worker), err
        assert poller.pool.placement(job_id) is None
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


def test_threads_left_by_a_budget_do_not_outlive_follow() -> None:
    before = {thread.name for thread in threading.enumerate()}
    job_run.follow(
        lambda _control: 0, beat=lambda: None, interval=0.1, log=lambda _m: None, budget=Budget(60)
    )
    time.sleep(0.1)
    alive = [t for t in threading.enumerate() if t.name not in before and t.is_alive()]
    assert alive == []


# --- review round 1: every process of the job, the remote exit status, the detached deadline -


WRITER = (
    "import sys, time\n"
    "f = open(sys.argv[1], 'a')\n"
    "while True:\n"
    "    f.write('x'); f.flush(); time.sleep(0.05)\n"
)
LEAVES_WRITER = (
    "import os, subprocess, sys, time; "
    "env = None if sys.argv[4] == 'keep' else {'PATH': os.environ.get('PATH', '')}; "
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], env=env, "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
    "open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(float(sys.argv[5]))"
)


def _still_writes(ticks: Path) -> bool:
    before = ticks.stat().st_size
    time.sleep(0.3)
    return ticks.stat().st_size != before


def _leave_writer(
    tmp_path: Path, budget: Budget | None, *, env: str = "keep", sleep: float = 600
) -> tuple[int, int, Path]:
    pid_file, ticks = tmp_path / "writer.pid", tmp_path / "ticks"
    code = job_run.run(
        [sys.executable, "-c", LEAVES_WRITER, str(pid_file), str(ticks), WRITER, env, str(sleep)],
        beat=lambda: None, interval=0.1, log=lambda _m: None, budget=budget,
    )
    return code, int(pid_file.read_text()), ticks


def test_a_child_the_harness_left_is_dead_when_run_returns_past_the_budget(tmp_path: Path) -> None:
    writer = None
    try:
        code, writer, ticks = _leave_writer(tmp_path, Budget(1.0, grace=5))
        assert code == 128 + signal.SIGTERM
        assert not _alive(writer) and not _still_writes(ticks)
    finally:
        kill_quietly(writer)


def test_a_child_that_dropped_the_environment_is_dead_when_run_returns(tmp_path: Path) -> None:
    writer = None
    try:
        code, writer, ticks = _leave_writer(tmp_path, Budget(1.0, grace=5), env="scrub")
        assert code == 128 + signal.SIGTERM
        assert not _alive(writer) and not _still_writes(ticks)
    finally:
        kill_quietly(writer)


def test_a_child_left_by_a_harness_that_exited_in_time_is_dead_when_run_returns(
    tmp_path: Path,
) -> None:
    writer = None
    try:
        code, writer, ticks = _leave_writer(tmp_path, Budget(60), sleep=0.5)
        assert code == 0
        assert not _alive(writer) and not _still_writes(ticks)
    finally:
        kill_quietly(writer)


def test_an_old_child_does_not_run_beside_the_retry(tmp_path: Path) -> None:
    poller, job_id = _box(tmp_path, timeout=2)
    pid_file, ticks = tmp_path / "pid", tmp_path / "ticks"
    writer = None
    try:
        code, err = _job_run(
            poller, job_id, sys.executable, "-c", LEAVES_WRITER,
            str(pid_file), str(ticks), WRITER, "keep", "600",
        )
        writer = int(pid_file.read_text())
        assert code == 128 + signal.SIGTERM, err
        assert poller.row(job_id)["state"] == "failed"
        poller.now[0] = time.time()
        poller.queue.clock = lambda: poller.now[0]
        poller.dispatcher.reconcile_jobs()
        retry = poller.claim()
        observed = tmp_path / "observed"
        watch = (
            "from pathlib import Path; import sys, time; "
            "p = Path(sys.argv[1]); before = p.stat().st_size; time.sleep(0.3); "
            "Path(sys.argv[2]).write_text(f'{before} {p.stat().st_size}')"
        )
        _job_run(poller, retry.job_id, sys.executable, "-c", watch, str(ticks), str(observed))
        before, after = map(int, observed.read_text().split())
        assert after == before, "the old child still wrote during the retry"
    finally:
        kill_quietly(writer)


def test_a_remote_run_past_its_budget_fails_even_when_its_body_returns_0() -> None:
    def body(control: job_run.Control) -> int:
        deadline = time.monotonic() + 10
        while control.signum is None and time.monotonic() < deadline:
            control.wait(0.05)
        return 0

    budget = Budget(0.3)
    code = job_run.follow(body, beat=lambda: None, interval=0.1, log=lambda _m: None, budget=budget)
    assert budget.expired and code == 128 + signal.SIGTERM


def test_an_expired_remote_job_whose_harness_exits_0_on_sigterm_fails(tmp_path: Path) -> None:
    import subprocess

    from lib.device_pool import DevicePool
    from lib.machines_yml import load_machines_yml
    from lib.queue import SQLiteJobQueue
    from lib.waiter import HarnessCommand, Waiter
    from tests.test_waiter import FakeContext, enqueue
    from tests.test_waiter_remote import ORCH, remote_machines

    machines = remote_machines(tmp_path, box_enabled=False)
    ssh = Ssh(tmp_path)
    db = Poller(tmp_path / "schema").path
    queue = SQLiteJobQueue(db, clock=time.time)
    config, _ = load_machines_yml(machines, allow_placeholders=True)
    pid_file = tmp_path / "pid"
    script = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "print('ORCH-HANDOFF: 123456', flush=True); time.sleep(600)"
    )
    waiter = Waiter(
        bot="claude-bot", queue=queue, pool=DevicePool(queue, config),
        harness=HarnessCommand(bot="claude-bot", command=(sys.executable, "-c", script, str(pid_file))),
        context=FakeContext(), state_dir=tmp_path / "state", db=db, machines=machines,
        orch=(sys.executable, str(ORCH)), spawn=subprocess.Popen,
        environ={"HOME": str(tmp_path), **ssh.env()},
    )
    job_id = enqueue(queue, "timeout", timeout_seconds=3)
    try:
        assert waiter.step() == job_id
        deadline = time.monotonic() + 60
        while waiter.running() and time.monotonic() < deadline:
            time.sleep(0.1)
            waiter.step()
        assert not waiter.running()
        with queue.connect() as connection:
            row = connection.execute(
                "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert (row["state"], row["handoff_comment_id"]) == ("failed", None)
    finally:
        if pid_file.exists():
            kill_quietly(int(pid_file.read_text()))


def test_a_detached_remote_worker_keeps_its_deadline_when_its_runner_dies(tmp_path: Path) -> None:
    poller, job_id, ssh = _remote(tmp_path, timeout=3)
    count = tmp_path / "count"
    proc = orch(
        "job", "run", "codex-bot", job_id, "--db", str(poller.path),
        "--machines", str(poller.machines), "--", *harness(count, 6000, 0.1),
        env=ssh.env(),
    )
    worker = None
    try:
        worker = started_harness(count, proc)
        proc.kill()
        proc.communicate(timeout=10)
        deadline = time.monotonic() + 3 + job_run.KILL_GRACE_SECONDS + 30
        while not gone(worker) and time.monotonic() < deadline:
            time.sleep(0.5)
        assert gone(worker), "the detached worker outlived its budget with no runner"
        [rundir] = run_dirs(tmp_path, "codex-bot", job_id)
        exit_file = rundir / "exit"
        while not exit_file.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert exit_file.read_text().strip() == str(128 + signal.SIGTERM)
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()


PID_WRITER = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "f = open(sys.argv[2], 'a')\n"
    "while True:\n"
    "    f.write('x'); f.flush(); time.sleep(0.05)\n"
)
ORPHANS_WRITER = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]], env={}, "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
    "start_new_session=True)"
)
RUNS_ORPHANER = (
    "import subprocess, sys, time; time.sleep(0.2); "
    "subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]]); "
    "time.sleep(float(sys.argv[5]))"
)


def _orphan_writer(tmp_path: Path, budget: Budget, sleep: float) -> tuple[int, int, Path]:
    pid_file, ticks = tmp_path / "writer.pid", tmp_path / "ticks"
    code = job_run.run(
        [sys.executable, "-c", RUNS_ORPHANER, ORPHANS_WRITER, PID_WRITER, str(pid_file), str(ticks),
         str(sleep)],
        beat=lambda: None, interval=0.1, log=lambda _m: None, budget=budget,
    )
    return code, int(pid_file.read_text()), ticks


def test_an_untagged_orphan_in_a_new_session_is_dead_when_run_returns_past_the_budget(
    tmp_path: Path,
) -> None:
    writer = None
    try:
        code, writer, ticks = _orphan_writer(tmp_path, Budget(2.0, grace=5), 600)
        assert code == 128 + signal.SIGTERM
        assert not _alive(writer) and not _still_writes(ticks)
    finally:
        kill_quietly(writer)


def test_an_untagged_orphan_of_a_harness_that_exited_in_time_is_dead_when_run_returns(
    tmp_path: Path,
) -> None:
    writer = None
    try:
        code, writer, ticks = _orphan_writer(tmp_path, Budget(60), 1.5)
        assert code == 0
        assert not _alive(writer) and not _still_writes(ticks)
    finally:
        kill_quietly(writer)


def test_an_untagged_orphan_does_not_run_beside_the_retry(tmp_path: Path) -> None:
    poller, job_id = _box(tmp_path, timeout=2)
    pid_file, ticks = tmp_path / "pid", tmp_path / "ticks"
    try:
        code, err = _job_run(
            poller, job_id, sys.executable, "-c", RUNS_ORPHANER,
            ORPHANS_WRITER, PID_WRITER, str(pid_file), str(ticks), "600",
        )
        assert code == 128 + signal.SIGTERM, err
        assert poller.row(job_id)["state"] == "failed"
        poller.now[0] = time.time()
        poller.queue.clock = lambda: poller.now[0]
        poller.dispatcher.reconcile_jobs()
        retry = poller.claim()
        observed = tmp_path / "observed"
        watch = (
            "from pathlib import Path; import sys, time; "
            "p = Path(sys.argv[1]); before = p.stat().st_size; time.sleep(0.3); "
            "Path(sys.argv[2]).write_text(f'{before} {p.stat().st_size}')"
        )
        code, err = _job_run(
            poller, retry.job_id, sys.executable, "-c", watch, str(ticks), str(observed)
        )
        assert code == 0, err
        before, after = map(int, observed.read_text().split())
        assert after == before, "the old orphan still wrote during the retry"
    finally:
        if pid_file.exists():
            kill_quietly(int(pid_file.read_text()))


def _reap_children() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def test_a_remote_deadline_kills_tagged_children_left_after_the_wrapper_exits(
    tmp_path: Path,
) -> None:
    import ctypes
    import subprocess

    from lib import remote

    libc = ctypes.CDLL(None)
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    target = remote.RemoteTarget(
        device="pc", alias="fake", user="codex-bot", workdir_root=str(tmp_path),
        job_id="probe", tag=remote.new_tag(),
    )
    pid_file, ticks = tmp_path / "pid", tmp_path / "ticks"
    writer = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); " + PID_WRITER
    parent = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[1], sys.argv[2]], "
        "start_new_session=True); time.sleep(600)"
    )
    start = remote.start_script(
        target, [sys.executable, "-c", parent, str(pid_file), str(ticks), writer], {}
    )
    launch = remote.launch_script(
        target, start, size=1, not_after=int(time.time()) + 60, budget=2, grace=1
    )
    rundir = Path(remote.run_dir(target)) / target.tag
    try:
        subprocess.run(["sh", "-c", launch], input=b"x", capture_output=True, check=True)
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists()
        end = time.monotonic() + 8
        while time.monotonic() < end and not (
            (rundir / "exit").exists() and not _alive(int(pid_file.read_text()))
        ):
            _reap_children()
            time.sleep(0.05)
        _reap_children()
        assert (rundir / "exit").read_text().strip() == str(128 + signal.SIGTERM)
        assert not _still_writes(ticks), "a tagged child outlived the budget and its grace"
    finally:
        if pid_file.exists():
            kill_quietly(int(pid_file.read_text()))
        if (rundir / "pid").exists():
            for pid in map(int, (rundir / "pid").read_text().split()):
                kill_quietly(pid)
        time.sleep(0.2)
        _reap_children()
        libc.prctl(36, 0, 0, 0, 0)


def test_stops_that_reach_a_run_while_it_finishes_still_leave_its_exit_status(
    tmp_path: Path,
) -> None:
    import shutil
    import subprocess

    from lib import remote

    shims = tmp_path / "shims"
    shims.mkdir()
    for tool in ("awk", "mv"):
        real = shutil.which(tool)
        assert real is not None
        shim = shims / tool
        shim.write_text(f'#!/bin/sh\nsleep 0.5\nexec {real} "$@"\n')
        shim.chmod(0o755)
    target = remote.RemoteTarget(
        device="pc", alias="fake", user="codex-bot", workdir_root=str(tmp_path / "work"),
        job_id="finishing", tag=remote.new_tag(),
    )
    ended = tmp_path / "ended"
    harness = (
        "import signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').close(); sys.exit(3)"
    )
    start = remote.start_script(target, [sys.executable, "-c", harness, str(ended)], {})
    launch = remote.launch_script(target, start, size=1, not_after=int(time.time()) + 60)
    env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}
    rundir = Path(remote.run_dir(target)) / target.tag
    subprocess.run(["sh", "-c", launch], input=b"x", env=env, capture_output=True, check=True)
    deadline = time.monotonic() + 20
    while not ended.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ended.exists()
    check = ["sh", "-c", remote.check_script(target)]
    while subprocess.run(check).returncode != 0 and time.monotonic() < deadline:
        subprocess.run(["sh", "-c", remote.stop_script(target)], check=True)
        time.sleep(0.1)
    assert subprocess.run(check).returncode == 0
    assert (rundir / "exit").read_text().strip() == "3"
