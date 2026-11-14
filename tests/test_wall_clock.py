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
    cli._settle_box(poller.queue, job_id, "codex", lines.append, lambda: 4242)
    assert poller.row(job_id)["state"] == "running"
    assert "worker pid 4242 still runs" in lines[0]
    poller.dispatcher.reconcile_jobs()
    assert [(state, attempt) for _id, state, attempt in poller.jobs()] == [("running", 1)]

    cli._settle_box(poller.queue, job_id, "codex", lines.append, lambda: None)
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
        assert "killing it" in err, err
        assert gone(worker)
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
