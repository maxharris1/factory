"""#75: four job states (claimed, running, stale, dead) derived in one place.

lib/job_state.py derives them; the poller (lib/dispatcher.py) requeues a job
claimed too long without charging a round and fails a dead one into T6;
the dashboard and `orch status` show them. Every test uses a temp database
and fakes (a scripted pid check, tests/test_remote_start.py's FakeTransport
for SSH); nothing reaches a real host or the live orch.db.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot, render_pages
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool, Placement
from lib.dispatcher import Dispatcher
from lib.job_state import (
    CLAIMED,
    DEAD,
    RUNNER_GONE,
    RUNNING,
    STALE,
    JobStateConfig,
    Liveness,
    derive,
    liveness,
    read_placements,
    stale_cause,
)
from lib.orch_yml import load_orch_yml
from lib.queue import JobRequest, QueueError, SQLiteJobQueue
from tests.test_dashboard_layout import BAR, _run_page, attention_count, needs_node
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, TRUSTED_LOGINS
from tests.test_remote_start import FakeTransport, config_of, machines_file

REVIEW_72 = "ticket.html#t=maxharris1%2Ffactory%2372"

ROOT = Path(__file__).resolve().parents[1]
ET = timezone(timedelta(hours=-5))
# bbfa3217 (issue #75): Codex's `orch wait` claimed this review of #72 at
# 3:47 PM ET on Nov 12; Codex never started it.
CLAIMED_AT = datetime(2026, 11, 12, 15, 47, tzinfo=ET).timestamp()


def placement(**overrides) -> Placement:
    values = dict(
        job_id="j1",
        device="box",
        harness="codex",
        os_user="codex-bot",
        placed_at=100.0,
    )
    values.update(overrides)
    return Placement(**values)  # type: ignore[arg-type]


# --- derive: one function, four states ---------------------------------------


def test_a_slot_with_no_runner_and_no_heartbeat_since_the_claim_is_claimed() -> None:
    assert derive(
        last_activity_at=100.0, now=1000.0, placement=placement(), stale_seconds=120
    ) == CLAIMED


def test_a_fresh_heartbeat_is_running_and_an_old_one_is_stale() -> None:
    started = placement(runner_pid=11)
    assert derive(last_activity_at=900.0, now=1000.0, placement=started, stale_seconds=120) == RUNNING
    assert derive(last_activity_at=880.0, now=1000.0, placement=started, stale_seconds=120) == RUNNING
    assert derive(last_activity_at=879.0, now=1000.0, placement=started, stale_seconds=120) == STALE


def test_a_job_with_no_slot_is_running_or_stale_by_heartbeat_alone() -> None:
    assert derive(last_activity_at=990.0, now=1000.0, placement=None, stale_seconds=120) == RUNNING
    assert derive(last_activity_at=10.0, now=1000.0, placement=None, stale_seconds=120) == STALE


def test_a_heartbeat_after_the_claim_without_a_runner_is_not_claimed() -> None:
    # `orch job heartbeat` by hand, or `orch job run` between its first beat
    # and recording itself: work may have started, so never "claimed".
    unstarted = placement()
    assert derive(last_activity_at=150.0, now=200.0, placement=unstarted, stale_seconds=120) == RUNNING
    assert derive(last_activity_at=150.0, now=900.0, placement=unstarted, stale_seconds=120) == STALE


def test_dead_needs_the_runner_gone_and_the_worker_confirmed_gone() -> None:
    started = placement(runner_pid=11, worker_pid=22)
    gone = Liveness(runner_alive=False, worker_gone=True)
    assert derive(
        last_activity_at=990.0, now=1000.0, placement=started, stale_seconds=120, liveness=gone
    ) == DEAD
    # A worker that may still run is not dead: its heartbeat stopped, so stale.
    # (#91: a runner confirmed alive keeps it running instead.)
    assert derive(
        last_activity_at=500.0, now=1000.0, placement=started, stale_seconds=120,
        liveness=Liveness(runner_alive=True),
    ) == RUNNING
    for facts in (
        Liveness(runner_alive=False, worker_gone=False),
        Liveness(runner_alive=False, worker_gone=None),
        Liveness(),
    ):
        assert derive(
            last_activity_at=500.0, now=1000.0, placement=started, stale_seconds=120,
            liveness=facts,
        ) == STALE


def test_a_stale_job_whose_runner_is_gone_shows_the_runner_gone() -> None:
    # #1211: display only; a gone runner with its worker confirmed gone is DEAD.
    assert stale_cause(STALE, Liveness(runner_alive=False, worker_gone=False)) == RUNNER_GONE
    assert stale_cause(STALE, Liveness(runner_alive=False, worker_gone=None)) == RUNNER_GONE
    assert RUNNER_GONE == "runner-gone"
    assert stale_cause(STALE, Liveness()) is None
    for state in (RUNNING, CLAIMED, DEAD):
        assert stale_cause(state, Liveness(runner_alive=False, worker_gone=False)) is None
    assert stale_cause(RUNNING, Liveness(runner_alive=True)) is None


def test_liveness_checks_the_runner_then_a_local_worker_by_pid() -> None:
    started = placement(runner_pid=11, runner_start="r", worker_pid=22, worker_start="w")
    live: set[int] = {11, 22}

    def alive(pid, _start) -> bool:
        return pid in live

    assert liveness(started, alive) == Liveness(runner_alive=True)
    live.discard(11)
    assert liveness(started, alive) == Liveness(runner_alive=False, worker_gone=False)
    live.discard(22)
    assert liveness(started, alive) == Liveness(runner_alive=False, worker_gone=True)
    # No runner recorded, or no check supplied: nothing is known.
    assert liveness(placement(), alive) == Liveness()
    assert liveness(started, None) == Liveness()
    assert liveness(None, alive) == Liveness()


def test_a_remote_worker_is_only_gone_when_the_remote_check_says_so() -> None:
    remote = placement(device="pc", runner_pid=11, remote_started=True, worker_tag="t")
    dead_runner = lambda *_: False  # noqa: E731
    assert liveness(remote, dead_runner) == Liveness(runner_alive=False, worker_gone=None)
    assert liveness(remote, dead_runner, lambda _p: True).worker_gone is True
    assert liveness(remote, dead_runner, lambda _p: False).worker_gone is False


def test_the_device_monitor_asks_the_device_by_worker_tag(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 1000.0)
    config = config_of(machines_file(tmp_path, box_enabled=False))
    pool = DevicePool(queue, config)
    transport = FakeTransport(check=0)
    monitor = DeviceMonitor(pool, config, transport, alive=lambda *_: False)
    remote = placement(device="pc", runner_pid=11, remote_started=True, worker_tag="a" * 32)
    assert monitor.liveness(remote) == Liveness(runner_alive=False, worker_gone=True)
    assert transport.calls == [("check", "codex-bot", "fakepc-codex-bot")]
    transport.check = 1
    assert monitor.liveness(remote).worker_gone is False
    transport.check = 255
    assert monitor.liveness(remote).worker_gone is None
    # No tag to name the worker: never asked, never gone.
    calls = len(transport.calls)
    assert monitor.liveness(
        placement(device="pc", runner_pid=11, remote_started=True)
    ).worker_gone is None
    assert len(transport.calls) == calls


# --- the poller ----------------------------------------------------------------


class Poller:
    """A dispatcher on a temp database with the device pool and a fake clock."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        now: float = CLAIMED_AT,
        box: bool = True,
        box_slots: int = 1,
        pc: bool = True,
        check: int = 0,
        job_states: JobStateConfig | None = None,
    ):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.now = [now]
        self.path = tmp_path / "orch.db"
        self.queue = SQLiteJobQueue(self.path, clock=lambda: self.now[0])
        self.machines = machines_file(tmp_path, box_enabled=box, box_slots=box_slots, pc=pc)
        self.config = config_of(self.machines)
        self.pool = DevicePool(self.queue, self.config)
        self.live: set[int] = set()
        self.transport = FakeTransport(check=check)
        self.lines: list[str] = []
        self.dispatcher = Dispatcher(
            self.path,
            self.queue,
            trusted_logins=TRUSTED_LOGINS,
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
            devices=DeviceMonitor(
                self.pool,
                self.config,
                self.transport,
                alive=lambda pid, _start: pid in self.live,
            ),
            job_states=job_states,
            log=self.lines.append,
        )

    def review(self, *, timeout: int = 3600) -> str:
        """Queue the review of #72 at round 1/1, as T2 would."""
        detail = json.dumps({"round": "1/1", "action": "review", "pr_number": 72})
        return self.queue.enqueue(
            JobRequest(
                repo="maxharris1/factory",
                ticket_id="72",
                action="review",
                recipient="codex",
                source_comment_id="ready-1",
                timeout_seconds=timeout,
                head="a" * 40,
                detail=detail,
            ),
            dedupe_key="review:72:r1",
        ).id

    def claim(self) -> Placement:
        claimed = self.pool.claim("codex")
        assert claimed is not None
        return claimed[1]

    def at(self, seconds: float) -> None:
        self.now[0] = CLAIMED_AT + seconds
        self.dispatcher.reconcile_jobs()

    def row(self, job_id: str) -> sqlite3.Row:
        with self.queue.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row is not None
        return row

    def jobs(self) -> list[tuple[str, str, int]]:
        with self.queue.connect() as connection:
            return [
                (str(row["id"]), str(row["state"]), int(row["attempt"]))
                for row in connection.execute("SELECT * FROM jobs ORDER BY created_at, id")
            ]

    def events(self) -> list[tuple[str, str]]:
        with self.queue.connect() as connection:
            return [
                (str(row["job_id"]), str(row["event"]))
                for row in connection.execute("SELECT * FROM job_events ORDER BY id")
            ]


def test_bbfa3217_a_claim_never_started_is_requeued_after_5_minutes_without_a_round(
    tmp_path: Path,
) -> None:
    """Regression: claimed at 3:47 PM ET, never started, shown as running."""
    poller = Poller(tmp_path)
    job_id = poller.review()
    placed = poller.claim()
    assert placed.runner_pid is None

    poller.at(4 * 60)
    poller.at(5 * 60)  # exactly 5 minutes: not yet
    assert poller.row(job_id)["state"] == "running"
    assert poller.pool.placement(job_id) is not None
    assert poller.events() == []

    poller.at(5 * 60 + 1)
    row = poller.row(job_id)
    assert row["state"] == "queued"
    assert poller.pool.placement(job_id) is None  # the slot is free again
    # The same job row: same round, same T6 attempt, nothing new charged.
    assert poller.jobs() == [(job_id, "queued", 1)]
    assert (row["generation"], row["attempt"]) == (1, 1)
    assert json.loads(row["detail"])["round"] == "1/1"
    assert row["finished_at"] is None
    with poller.queue.connect() as connection:
        failures = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE state = 'failed'"
        ).fetchone()[0]
    assert failures == 0
    assert poller.events() == [(job_id, "released-claimed")]
    assert len(poller.lines) == 1
    assert "released-claimed" in poller.lines[0] and job_id[:8] in poller.lines[0]
    assert "no round charged" in poller.lines[0]

    # Another `orch wait` can claim it again, as the same job.
    again = poller.claim()
    assert again.job_id == job_id


def test_a_late_orch_job_run_finds_the_released_job_queued_and_starts_nothing(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path)
    job_id = poller.review()
    poller.claim()
    poller.at(6 * 60)
    assert poller.row(job_id)["state"] == "queued"
    # `orch job run` beats before anything else; a queued job refuses it.
    with pytest.raises(QueueError, match="requires a running job"):
        poller.queue.heartbeat(job_id, owner="codex")


def test_a_job_that_started_is_never_requeued(tmp_path: Path) -> None:
    # Heartbeat since the claim (orch job run's first beat), no runner yet.
    # (#91: once nothing has beaten for release_claimed_minutes with no
    # runner and no worker, it is released as unstarted:
    # tests/test_liveness_requeue.py.)
    poller = Poller(tmp_path)
    beaten = poller.review()
    poller.claim()
    poller.now[0] = CLAIMED_AT + 1
    poller.queue.heartbeat(beaten, owner="codex")
    poller.at(1 + 5 * 60)
    assert poller.row(beaten)["state"] == "running"

    # A recorded runner that is still alive.
    other = Poller(tmp_path / "other")
    run = other.review()
    other.claim()
    other.live.add(4242)
    other.pool.attach_runner(run, owner="codex", pid=4242, start=None, alive=lambda *_: False)
    other.at(10 * 60)
    assert other.row(run)["state"] == "running"
    assert other.events() == []


def test_the_release_is_guarded_against_a_runner_that_starts_in_between(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path)
    job_id = poller.review()
    placed = poller.claim()
    job = next(item for item in poller.queue.outstanding() if item.id == job_id)
    # orch job run beats after the poller read the job as claimed.
    poller.now[0] = CLAIMED_AT + 301
    poller.queue.heartbeat(job_id, owner="codex")
    poller.dispatcher._release_claimed(job, placed.placed_at, poller.now[0])
    assert poller.row(job_id)["state"] == "running"
    assert poller.pool.placement(job_id) is not None
    assert poller.events() == []


def _failed_and_retries(poller: Poller) -> tuple[int, int]:
    with poller.queue.connect() as connection:
        failed = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE state = 'failed'"
        ).fetchone()[0]
        retries = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE attempt > 1 OR dedupe_key LIKE 't6-retry:%'"
        ).fetchone()[0]
    return int(failed), int(retries)


def test_a_released_job_restarts_its_queue_clock_and_is_not_timed_out(
    tmp_path: Path,
) -> None:
    """PR #77 review F1.2: queued 55 minutes, claimed, released at 60:01.

    The release must not hand the job to the timeout sweep in the same
    poll (or a later one) for time it spent queued before the claim.
    """
    poller = Poller(tmp_path)
    job_id = poller.review(timeout=3600)  # enqueued at t=0
    poller.now[0] = CLAIMED_AT + 3300
    poller.claim()

    poller.at(3601)  # claimed 301 s: released, then the timeout sweep runs
    row = poller.row(job_id)
    assert row["state"] == "queued"
    assert poller.jobs() == [(job_id, "queued", 1)]
    assert _failed_and_retries(poller) == (0, 0)
    assert poller.events() == [(job_id, "released-claimed")]

    poller.at(3601 + 3599)  # one second short of a full timeout since release
    assert poller.row(job_id)["state"] == "queued"
    assert poller.jobs() == [(job_id, "queued", 1)]
    assert _failed_and_retries(poller) == (0, 0)

    # The clock restarted; it was not switched off.
    poller.at(3601 + 3600)
    assert poller.row(job_id)["state"] == "failed"


def test_a_released_job_counts_its_queue_wait_from_the_release_on_the_dashboard(
    tmp_path: Path,
) -> None:
    # The dashboard's stuck / overdue must agree with the poller's clock.
    poller = Poller(tmp_path)
    job_id = poller.review(timeout=3600)
    poller.now[0] = CLAIMED_AT + 3300
    poller.claim()
    poller.at(3601)
    assert poller.row(job_id)["state"] == "queued"
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    snapshot = collect_snapshot(poller.path, log, now=CLAIMED_AT + 3700, alive=None)
    queued = {row["job"]: row for row in snapshot["queued"]}
    assert queued[job_id[:8]]["age_seconds"] == 99
    assert snapshot["stuck"]["overdue_jobs"] == []


def test_a_failed_grok_delivery_keeps_its_original_queue_clock(tmp_path: Path) -> None:
    # The other requeue path, lib/queue.py release (used only when a Grok
    # delivery fails), is not an unstarted claim: each poll is a real
    # delivery attempt, and the timeout is what bounds a delivery that keeps
    # failing (test_persistent_grok_failure_uses_two_full_timeout_windows_
    # before_stuck). So it keeps the clock it was queued with, and a sweep
    # that read the job before the claim can still fail it.
    now = [0.0]
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: now[0])
    queued = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="clarify",
            recipient="grok",
            source_comment_id="question-1",
            timeout_seconds=3600,
        ),
        dedupe_key="clarify:g1",
    )
    now[0] = 3600.0
    assert queue.claim("grok") is not None
    released = queue.release(queued.id, owner="grok")
    assert released.state == "queued"
    assert (released.created_at, released.last_activity_at) == (0.0, 0.0)
    assert queue.mark_timed_out(released) is True


def test_release_claimed_minutes_comes_from_orch_yml(tmp_path: Path) -> None:
    poller = Poller(
        tmp_path,
        job_states=JobStateConfig(stale_seconds=120, release_claimed_seconds=600),
    )
    job_id = poller.review()
    poller.claim()
    poller.at(9 * 60)
    assert poller.row(job_id)["state"] == "running"
    poller.at(10 * 60 + 1)
    assert poller.row(job_id)["state"] == "queued"


def test_a_dead_box_job_takes_the_failure_path_into_t6(tmp_path: Path) -> None:
    poller = Poller(tmp_path)
    job_id = poller.review()
    poller.claim()
    poller.live.update({11, 22})
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start="r", alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", pid=22, start="w")
    poller.now[0] = CLAIMED_AT + 30
    poller.queue.heartbeat(job_id, owner="codex")

    poller.at(60)
    assert poller.row(job_id)["state"] == "running"

    poller.live.discard(11)  # the runner died; its command still runs
    poller.at(90)
    assert poller.row(job_id)["state"] == "running"

    poller.live.discard(22)  # and now the command is gone too
    poller.at(120)
    assert poller.row(job_id)["state"] == "failed"
    assert poller.events() == [(job_id, "dead")]
    assert any("dead" in line and "pid 11" in line for line in poller.lines)
    # T6 handled it like any failed job: one retry, attempt 2.
    jobs = poller.jobs()
    assert [(state, attempt) for _id, state, attempt in jobs] == [("failed", 1), ("queued", 2)]
    assert int(poller.row(job_id)["failure_handled"]) == 1
    # With the job no longer running, the device monitor frees its slot.
    poller.at(180)
    assert poller.pool.placement(job_id) is None


def test_a_dead_remote_job_needs_the_device_to_confirm_the_worker_gone(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box=False, check=255)
    job_id = poller.review()
    placed = poller.claim()
    assert placed.device == "pc"
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start="r", alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", remote=True, tag="b" * 32)
    poller.now[0] = CLAIMED_AT + 30
    poller.queue.heartbeat(job_id, owner="codex")

    poller.at(60)  # runner gone, device unreachable: not dead
    assert poller.row(job_id)["state"] == "running"
    poller.transport.check = 1  # the worker still runs there
    poller.at(90)
    assert poller.row(job_id)["state"] == "running"

    poller.transport.check = 0  # gone
    poller.at(120)
    assert poller.row(job_id)["state"] == "failed"
    assert poller.events() == [(job_id, "dead")]


def test_a_stale_job_is_left_to_the_timeout(tmp_path: Path) -> None:
    poller = Poller(tmp_path)
    job_id = poller.review(timeout=3600)
    poller.claim()
    poller.live.add(11)
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.now[0] = CLAIMED_AT + 10
    poller.queue.heartbeat(job_id, owner="codex")
    poller.at(30 * 60)  # stale for 30 minutes, runner alive
    assert poller.row(job_id)["state"] == "running"
    assert poller.events() == []
    poller.at(10 + 3600)
    assert poller.row(job_id)["state"] == "failed"  # the existing T6 timeout


def test_a_job_state_failure_is_recorded_and_never_stops_the_poll(tmp_path: Path) -> None:
    poller = Poller(tmp_path)
    job_id = poller.review()
    poller.claim()

    def broken(_placement):
        raise RuntimeError("boom")

    poller.dispatcher.devices.liveness = broken  # type: ignore[union-attr]
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start=None, alive=lambda *_: False)
    poller.at(60)
    with poller.queue.connect() as connection:
        errors = [tuple(row) for row in connection.execute("SELECT repo, error FROM poll_errors")]
    assert ("maxharris1/factory", "job-states:RuntimeError: boom") in errors
    assert poller.row(job_id)["state"] == "running"


def test_read_placements_tolerates_an_f2_01_slot_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT, "
            "harness TEXT, os_user TEXT, placed_at REAL)"
        )
        connection.execute("INSERT INTO device_slots VALUES ('j', 'box', 'codex', 'codex-bot', 5)")
        found = read_placements(connection)
        assert found["j"].runner_pid is None and found["j"].placed_at == 5.0
    with sqlite3.connect(tmp_path / "empty.db") as connection:
        assert read_placements(connection) == {}


# --- orch.yml ------------------------------------------------------------------


def orch_yml(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\nprojects:\n  - repo: owner/repo\n" + extra, encoding="utf-8"
    )
    return path


def test_job_states_default_to_2_5_and_10_minutes(tmp_path: Path) -> None:
    config, findings = load_orch_yml(orch_yml(tmp_path, ""))
    assert config is not None, findings
    assert config.job_states == JobStateConfig(
        stale_seconds=120, release_claimed_seconds=300, unreachable_lost_seconds=600
    )


def test_job_states_are_configurable_in_orch_yml(tmp_path: Path) -> None:
    config, findings = load_orch_yml(
        orch_yml(
            tmp_path,
            "job_states:\n  stale_minutes: 3\n  release_claimed_minutes: 10\n"
            "  unreachable_lost_minutes: 15\n",
        )
    )
    assert config is not None, findings
    assert config.job_states == JobStateConfig(
        stale_seconds=180, release_claimed_seconds=600, unreachable_lost_seconds=900
    )


LOST_MINUTES = "job_states.unreachable_lost_minutes must be a positive integer"


@pytest.mark.parametrize(
    "extra, message",
    [
        ("job_states: 5\n", "job_states must be a mapping"),
        ("job_states:\n  stale_minutes: 0\n", "job_states.stale_minutes must be a positive"),
        ("job_states:\n  stale_minutes: 1.5\n", "job_states.stale_minutes must be a positive"),
        ("job_states:\n  release_claimed_minutes: x\n", "job_states.release_claimed_minutes"),
        ("job_states:\n  stale_seconds: 30\n", "unknown field 'job_states.stale_seconds'"),
        *(
            (f"job_states:\n  unreachable_lost_minutes: {value}\n", LOST_MINUTES)
            for value in ("0", "-1", '"10"', "true")
        ),
    ],
)
def test_bad_job_states_are_refused(tmp_path: Path, extra: str, message: str) -> None:
    config, findings = load_orch_yml(orch_yml(tmp_path, extra))
    assert config is None
    assert any(message in item.message for item in findings), findings


# --- dashboard and orch status ---------------------------------------------------


def dashboard_db(tmp_path: Path) -> tuple[Path, Path]:
    """bbfa3217 twelve minutes after its claim (no poller ran), plus a
    stale job whose runner is gone, a stale job no runner recorded itself on
    (quiet), a dead job and a running one."""
    poller = Poller(tmp_path, box_slots=5)
    claimed = poller.review()
    poller.claim()

    def job(key: str, ticket: str) -> str:
        return poller.queue.enqueue(
            JobRequest(
                repo="maxharris1/factory",
                ticket_id=ticket,
                action="build",
                recipient="codex",
                source_comment_id=f"go-{key}",
                timeout_seconds=3600,
            ),
            dedupe_key=key,
        ).id

    ids = {"claimed": claimed}
    for name, ticket, runner in (("stale", "80", 31), ("dead", "81", 41), ("running", "82", 51)):
        ids[name] = job(name, ticket)
        poller.pool.claim("codex")
        poller.pool.attach_runner(ids[name], owner="codex", pid=runner, start=None, alive=lambda *_: False)
        poller.pool.attach_worker(ids[name], owner="codex", pid=runner + 1, start=None)
    ids["quiet"] = job("quiet", "83")
    poller.pool.claim("codex")
    poller.now[0] = CLAIMED_AT + 60
    poller.queue.heartbeat(ids["quiet"], owner="codex")
    poller.queue.heartbeat(ids["stale"], owner="codex")
    poller.queue.heartbeat(ids["dead"], owner="codex")
    poller.now[0] = CLAIMED_AT + 11 * 60
    poller.queue.heartbeat(ids["running"], owner="codex")
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    (tmp_path / "ids.json").write_text(json.dumps(ids), encoding="utf-8")
    return poller.path, log


def test_the_dashboard_shows_each_state_and_the_real_round(tmp_path: Path) -> None:
    db, log = dashboard_db(tmp_path)
    ids = json.loads((tmp_path / "ids.json").read_text())
    # The dead job's runner (41) and worker (42) are gone. The stale job's
    # runner (31) is gone too while its worker (32) may still run: a live
    # runner would keep it running (#91).
    live = {32, 51, 52}
    snapshot = collect_snapshot(
        db,
        log,
        now=CLAIMED_AT + 12 * 60,
        alive=lambda pid, _start: pid in live,
    )
    running = {row["job"]: row for row in snapshot["running"]}
    states = {name: running[job_id[:8]]["job_state"] for name, job_id in ids.items()}
    assert states == {
        "claimed": "claimed", "stale": "stale", "quiet": "stale", "dead": "dead", "running": "running",
    }
    causes = {name: running[job_id[:8]]["stale_cause"] for name, job_id in ids.items()}
    assert causes == {"claimed": None, "stale": "runner-gone", "quiet": None, "dead": None, "running": None}
    review = running[ids["claimed"][:8]]
    assert review["round"] == "1/1"  # from detail.round, not "round 0"

    # Stale and dead jobs count in stuck / overdue and need attention.
    overdue = {row["job"]: row for row in snapshot["stuck"]["overdue_jobs"]}
    assert set(overdue) == {ids["stale"][:8], ids["quiet"][:8], ids["dead"][:8]}
    assert {job: row["stale_cause"] for job, row in overdue.items()} == {
        ids["stale"][:8]: "runner-gone", ids["quiet"][:8]: None, ids["dead"][:8]: None,
    }
    details = {item["ticket_id"]: (item["kind"], item["detail"]) for item in snapshot["attention"]["items"]}
    assert details == {
        "80": ("stale", "codex build runner gone, worker alive, no heartbeat for 11m 00s"),
        "83": ("stale", "codex build running, no heartbeat for 11m 00s"),
        "81": ("dead", "codex build runner gone, no heartbeat for 11m 00s"),
    }
    assert snapshot["attention"]["count"] == 3

    pages = render_pages(snapshot)
    index = re.sub(r"<script>.*?</script>", "", pages["index.html"], flags=re.S)
    card = re.search(
        rf'<a class="tile [^"]*" href="{REVIEW_72}"[^>]*>.*?</a>',
        index,
        re.S,
    )
    assert card, index
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", card.group(0)))
    assert "claimed" in text and 'aria-label="waiting to start"' in card.group(0)
    assert 'title="round 1/1"' in card.group(0) and "round 0" not in text
    assert "hb —" in text and '<i style="width:0%">' in card.group(0)
    assert "no heartbeat" in index and "runner dead" in index
    assert "runner gone, worker alive" in _tile_of(index, ids["stale"])
    assert "runner gone, worker alive" not in _tile_of(index, ids["quiet"])


def _tile_of(html: str, job_id: str) -> str:
    match = re.search(rf'<a class="tile [^"]*" href="[^"]*" title="[^"]*\(job {job_id[:8]}\)">.*?</a>', html, re.S)
    assert match, job_id
    return match.group(0)


@needs_node
def test_the_page_script_draws_the_same_states(tmp_path: Path) -> None:
    db, log = dashboard_db(tmp_path)
    ids = json.loads((tmp_path / "ids.json").read_text())
    live = {32, 51, 52}
    snapshot = collect_snapshot(
        db, log, now=CLAIMED_AT + 12 * 60, alive=lambda pid, _start: pid in live
    )
    out = _run_page(tmp_path, snapshot, "main", BAR + ["lanes"])
    lanes = out["text"]["lanes"]
    for needle in ("claimed", "waiting to start", "1/1", "no heartbeat", "runner dead", "running",
                   "runner gone, worker alive"):
        assert needle in lanes, (needle, lanes)
    assert "round 0" not in lanes
    assert attention_count(out) == "3"
    assert "runner gone, worker alive" in _tile_of(out["html"]["lanes"], ids["stale"])
    assert "runner gone, worker alive" not in _tile_of(out["html"]["lanes"], ids["quiet"])
    dump = out["dump"]["lanes"]
    card = dump[dump.index(REVIEW_72) :]
    card = card[: card.index("a.tile", 1)] if "a.tile" in card[1:] else card
    assert 'span.hbt "hb —"' in card and "span.chip.warn \"claimed\"" in card


def test_the_dashboard_never_copies_more_of_detail_than_the_round(tmp_path: Path) -> None:
    poller = Poller(tmp_path)
    secret = "ghp_" + "A" * 36
    poller.queue.enqueue(
        JobRequest(
            repo="maxharris1/factory",
            ticket_id="90",
            action="review",
            recipient="codex",
            source_comment_id="c",
            timeout_seconds=3600,
            detail=json.dumps({"round": "2/3", "brief": secret, "linear_url": "https://x"}),
        ),
        dedupe_key="d1",
    )
    poller.queue.enqueue(
        JobRequest(
            repo="maxharris1/factory",
            ticket_id="91",
            action="review",
            recipient="codex",
            source_comment_id="c2",
            timeout_seconds=3600,
            detail=json.dumps({"round": "<b>2</b>"}),
        ),
        dedupe_key="d2",
    )
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    snapshot = collect_snapshot(poller.path, log, now=CLAIMED_AT, alive=None)
    rounds = {row["ticket_id"]: row["round"] for row in snapshot["queued"]}
    assert rounds == {"90": "2/3", "91": None}
    assert secret not in json.dumps(snapshot)
    assert "https://x" not in json.dumps(snapshot)


def test_orch_status_shows_the_job_state(tmp_path: Path) -> None:
    poller = Poller(tmp_path / "db", now=1_000_000.0)
    poller.review()
    poller.claim()

    def status(orch: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(ROOT / "bin" / "orch"), "status",
                "--db", str(poller.path), "--machines", str(poller.machines),
                "--orch", str(orch),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    (tmp_path / "good").mkdir()
    result = status(orch_yml(tmp_path / "good", "job_states:\n  stale_minutes: 2\n"))
    assert result.returncode == 0, result.stderr
    assert "maxharris1/factory#72 review -> codex claimed " in result.stdout

    (tmp_path / "bad").mkdir()
    refused = status(orch_yml(tmp_path / "bad", "job_states:\n  stale_minutes: 0\n"))
    assert refused.returncode == 1
    assert "stale_minutes" in refused.stderr


def test_orch_status_falls_back_to_defaults_when_the_checkout_orch_yml_is_bad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    import lib.status as status

    poller = Poller(tmp_path / "db", now=1_000_000.0)
    poller.review()
    poller.claim()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "orch.yml").write_text("poll_seconds: 5\n", encoding="utf-8")
    monkeypatch.setattr(status, "ROOT", checkout)
    out = io.StringIO()
    code = status.main(
        ["--db", str(poller.path), "--machines", str(poller.machines)],
        stdout=out,
        clock=lambda: 1_000_000.0,
    )
    assert code == 0
    assert "review -> codex claimed " in out.getvalue()
    assert "using the default job_states" in capsys.readouterr().err
