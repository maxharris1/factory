"""#357: a start refused because a factory checkout lags waits, it does not fail.

The real waiter refuses the job (preflight-lagging, a box checkout behind its
origin/main), and the real dispatcher's T6 requeues the same row after a
backoff, on a fake clock over one temp orch.db. A refusal charges no free
requeue, no round and no attempt, so it never reaches job-failed-2; a device
still lagging LAGGING_WAIT_SECONDS after the first refusal sticks the ticket
device-lagging.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lib import device_sync
from lib.device_monitor import DeviceMonitor
from lib.dispatcher import Dispatcher
from lib.job_state import LAGGING_BACKOFF_SECONDS, LAGGING_WAIT_SECONDS, free_requeues
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, TRUSTED_LOGINS
from tests.test_remote_start import FakeTransport
from tests.test_skill_preflight import (
    Device,
    box_behind_origin_main,
    deploy,
    git_in,
    install,
    lock_root,
    sync_rows,
)
from tests.test_waiter import state

__all__ = ["lock_root"]

START = 1_800_000_000.0


@pytest.fixture
def _skill_preflight():
    """The real preflight, over the suite-wide no-op in tests/conftest.py."""


class Factory:
    """A box waiter whose checkout lags origin/main, and a poller on its database."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: Path, *,
                 remote: bool = False):
        self.device = Device(tmp_path, monkeypatch, remote=remote)
        install(self.device.home)
        deploy(root)
        self.head, self.later = box_behind_origin_main(root)
        self.root = root
        self.now = [START]
        self.device.queue.clock = lambda: self.now[0]
        pool = self.device.waiter.pool
        self.lines: list[str] = []
        self.dispatcher = Dispatcher(
            self.device.db,
            self.device.queue,
            trusted_logins=TRUSTED_LOGINS,
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
            devices=DeviceMonitor(pool, pool.config, FakeTransport(), alive=lambda *_: False),
            log=self.lines.append,
        )

    def refused(self, at: float, job_id: str) -> None:
        """The waiter claims job_id at `at` and preflight refuses it."""
        self.now[0] = at
        with sqlite3.connect(self.device.db) as connection:
            device_sync.clear(connection, "box")
        assert self.device.waiter.step() is None, "a lagging checkout starts nothing"
        assert state(self.device.db, job_id) == "failed"

    def poll(self, at: float) -> None:
        self.now[0] = at
        self.dispatcher.reconcile_jobs()

    def deployed(self) -> None:
        subprocess.run([*git_in(self.root), "reset", "-q", "--hard", self.later], check=True)
        with sqlite3.connect(self.device.db) as connection:
            device_sync.clear(connection, "box")

    def events(self, job_id: str) -> list[str]:
        with sqlite3.connect(self.device.db) as connection:
            return [str(row[0]) for row in connection.execute(
                "SELECT event FROM job_events WHERE job_id = ? ORDER BY id", (job_id,))]

    def jobs(self) -> list[tuple[str, str, int]]:
        with sqlite3.connect(self.device.db) as connection:
            return connection.execute(
                "SELECT id, state, attempt FROM jobs WHERE action != 'stuck' ORDER BY created_at"
            ).fetchall()

    def stuck(self) -> tuple[int, str | None]:
        with sqlite3.connect(self.device.db) as connection:
            row = connection.execute(
                "SELECT stuck, stuck_reason FROM ticket_state WHERE repo = 'owner/repo' "
                "AND ticket_id = '42'"
            ).fetchone()
        return (0, None) if row is None else (int(row[0]), row[1])

    def wakes(self) -> list[dict]:
        with sqlite3.connect(self.device.db) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT detail FROM jobs WHERE action = 'stuck'")]

    def free_requeues(self, job_id: str) -> int:
        with sqlite3.connect(self.device.db) as connection:
            return free_requeues(connection, job_id)


def test_two_lagging_refusals_in_a_row_requeue_after_a_backoff_and_leave_the_ticket_unstuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    first, second = LAGGING_BACKOFF_SECONDS[:2]
    job_id, started = factory.device.run_one()
    assert started is None
    assert state(factory.device.db, job_id) == "failed"
    assert factory.free_requeues(job_id) == 0, "a lagging refusal charges no free requeue"

    factory.poll(START + first - 1)
    assert state(factory.device.db, job_id) == "failed", "no requeue before the backoff"
    assert "requeue-deferred" in factory.events(job_id)
    factory.poll(START + first)
    assert state(factory.device.db, job_id) == "queued", "requeued once the backoff passed"

    again = START + first + 1
    factory.refused(again, job_id)
    factory.poll(again + second - 1)
    assert state(factory.device.db, job_id) == "failed", "the second backoff is longer"
    factory.poll(again + second)

    assert factory.jobs() == [(job_id, "queued", 1)], "the same row, no attempt 2"
    assert factory.stuck() == (0, None)
    assert factory.free_requeues(job_id) == 0
    assert factory.events(job_id).count("requeued-lagging") == 2
    assert "failed-unstarted" not in factory.events(job_id)
    assert factory.wakes() == []


def test_the_same_job_starts_once_the_checkout_reaches_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    job_id, started = factory.device.run_one()
    assert started is None

    factory.deployed()
    factory.poll(START + LAGGING_BACKOFF_SECONDS[0])

    assert factory.device.waiter.step() == job_id
    assert len(factory.device.spawn.calls) == 1
    assert factory.jobs() == [(job_id, "running", 1)]
    assert sync_rows(factory.device.db) == []


def test_a_device_still_lagging_past_the_bound_sticks_device_lagging_not_job_failed_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    job_id, started = factory.device.run_one()
    assert started is None
    refused_at = START
    used = 0
    while True:
        backoff = LAGGING_BACKOFF_SECONDS[min(used, len(LAGGING_BACKOFF_SECONDS) - 1)]
        factory.poll(refused_at + backoff)
        if refused_at + backoff - START >= LAGGING_WAIT_SECONDS:
            break
        assert state(factory.device.db, job_id) == "queued"
        assert factory.stuck() == (0, None), "not stuck inside the bound"
        used += 1
        refused_at += backoff + 1
        factory.refused(refused_at, job_id)

    assert used >= 2
    assert factory.stuck() == (1, "device-lagging")
    [wake] = factory.wakes()
    assert wake["job_id"] == job_id
    assert "device still lagging after 15 min" in wake["detail"]
    assert f"origin/main at {factory.later[:12]}" in wake["detail"]
    assert factory.jobs() == [(job_id, "failed", 1)], "no attempt 2"
    assert not any("job-failed-2" in line for line in factory.lines)


def test_a_remote_placement_refused_for_a_box_lag_marks_the_box_lagging_not_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root, remote=True)

    job_id, started = factory.device.run_one()

    assert started is None
    assert state(factory.device.db, job_id) == "failed"
    error = (f"preflight-lagging: box claude-bot {lock_root.resolve()} at {factory.head[:12]}, "
             f"origin/main at {factory.later[:12]}")
    assert sync_rows(factory.device.db) == [("box", "claude-bot", "lagging", error)]
