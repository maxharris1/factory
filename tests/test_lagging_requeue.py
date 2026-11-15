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
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib import device_sync, lane_limit, skill_preflight
from lib.device_pool import Placement, choose_device
from lib.device_monitor import DeviceMonitor
from lib.dispatcher import Dispatcher
from lib.job_state import LAGGING_BACKOFF_SECONDS, LAGGING_WAIT_SECONDS, free_requeues
from lib.queue import JobRequest
from lib.remote import SshTransport
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
from lib.usage_readings import Reading, store
from tests.test_usage_limit import _feed
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


def test_a_job_queued_behind_a_lagging_box_sticks_device_lagging_once_past_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    job_id, started = factory.device.run_one()
    assert started is None
    elapsed = 0
    while elapsed < LAGGING_WAIT_SECONDS:
        elapsed += 30
        factory.poll(START + elapsed)
        assert factory.device.waiter.step() is None, "the lagging box takes no job"
        if elapsed < LAGGING_WAIT_SECONDS:
            assert factory.stuck() == (0, None), "not stuck inside the bound"
            assert factory.jobs() in ([(job_id, "queued", 1)], [(job_id, "failed", 1)])

    assert factory.stuck() == (1, "device-lagging")
    assert factory.jobs() == [(job_id, "failed", 1)], "no attempt 2"
    assert factory.events(job_id)[-1] == "lag-blocked"
    [wake] = factory.wakes()
    assert f"origin/main at {factory.later[:12]}" in wake["detail"]
    assert not any("job-failed-2" in line for line in factory.lines)


def test_time_in_a_usage_limited_lane_never_counts_toward_the_lagging_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    job_id, started = factory.device.run_one()
    assert started is None
    ended = int(START) + LAGGING_WAIT_SECONDS + 600
    with sqlite3.connect(factory.device.db) as connection:
        lane_limit.record(connection, job_id=job_id, harness="claude", bot="claude-bot",
                          limit=lane_limit.UsageLimit(ended), now=START + 1)
    at = START
    while at < ended + LAGGING_WAIT_SECONDS - 60:
        at += 30
        factory.poll(at)
        assert factory.device.waiter.step() is None, "the lagging box takes no job"
        assert factory.stuck() == (0, None), "waiting in the limited lane spends no lag bound"
    while at < ended + LAGGING_WAIT_SECONDS + 300:
        at += 30
        factory.poll(at)
        factory.device.waiter.step()
    assert factory.stuck() == (1, "device-lagging")
    assert [job[1:] for job in factory.jobs()] == [("failed", 1)], "no attempt 2"


def test_healthy_readings_after_a_pause_ends_never_extend_the_lagging_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    job_id, started = factory.device.run_one()
    assert started is None
    ended = int(START) + 60
    with sqlite3.connect(factory.device.db) as connection:
        lane_limit.record(connection, job_id=job_id, harness="claude", bot="claude-bot",
                          limit=lane_limit.UsageLimit(ended), now=START + 1)
    at = START
    while at < ended + LAGGING_WAIT_SECONDS + 300:
        at += 30
        with sqlite3.connect(factory.device.db) as connection:
            store(connection, harness="claude", bot="claude-bot", device="remote", job_id="healthy",
                  readings=[Reading("five_hour", 0.5, int(at) + 3600, at)])
        factory.poll(at)
    assert factory.stuck() == (1, "device-lagging")
    assert [job[1:] for job in factory.jobs()] == [("failed", 1)], "no attempt 2"


def test_a_refusal_awaiting_its_first_reconcile_is_not_stuck_while_the_lane_is_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    job_id, started = factory.device.run_one()
    assert started is None
    another = factory.device.queue.enqueue(
        JobRequest("owner/repo", "43", "build", "claude", "c43", 3600),
        dedupe_key="other-device-build",
    )
    factory.now[0] = START + 1
    _feed(factory.device.db, tmp_path, another.id, "claude", {
        "type": "result", "subtype": "success", "is_error": True,
        "result": f"Claude AI usage limit reached|{int(START) + 3600}",
    }, factory.now)
    factory.poll(START + LAGGING_WAIT_SECONDS + 1)
    with sqlite3.connect(factory.device.db) as connection:
        assert lane_limit.active(connection, "claude", factory.now[0]) is not None
    assert factory.stuck() == (0, None), "an active pause is waiting, not lag"
    assert (job_id, "failed", 1) not in factory.jobs()


def test_a_checkout_that_recovers_during_the_last_backoff_starts_the_job_unstuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root, remote=True)
    job_id, started = factory.device.run_one()
    assert started is None
    for retry_at in (30, 91, 212, 453, 694):
        factory.poll(START + retry_at)
        factory.now[0] = START + retry_at + 1
        assert factory.device.waiter.step() is None
        assert factory.jobs() == [(job_id, "failed", 1)]
    factory.now[0] = START + 800
    factory.deployed()
    subprocess.run(["git", "clone", "-q", str(factory.root), str(factory.device.checkout)],
                   check=True)
    report = skill_preflight.check(
        bot="claude-bot", placement=Placement(job_id, "pc", "claude", "claude-bot", START),
        device=factory.device.waiter.pool.config.device("pc"), transport=SshTransport(),
        home=factory.device.home, checkout=str(factory.device.checkout), root=factory.root,
    )
    assert report.error is None, "both checkouts are current"

    factory.poll(START + 800)
    factory.poll(START + LAGGING_WAIT_SECONDS)
    assert factory.stuck() == (0, None), "the lag is gone; its age alone sticks nothing"
    factory.poll(START + 935)
    assert factory.device.waiter.step() == job_id
    assert factory.jobs() == [(job_id, "running", 1)]
    assert factory.stuck() == (0, None)


def test_a_remote_start_still_refused_past_the_bound_sticks_device_lagging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root, remote=True)
    job_id, started = factory.device.run_one()
    assert started is None
    for retry_at in (30, 91, 212, 453, 694):
        factory.poll(START + retry_at)
        assert factory.stuck() == (0, None), "not stuck inside the bound"
        factory.now[0] = START + retry_at + 1
        assert factory.device.waiter.step() is None
    factory.poll(START + 935)

    assert factory.stuck() == (1, "device-lagging")
    assert factory.jobs() == [(job_id, "failed", 1)], "no attempt 2"


def test_partial_recovery_healthy_box_can_take_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root, remote=True)
    pool = factory.device.waiter.pool
    config = replace(pool.config, devices=tuple(
        replace(device, enabled=True) if device.local else device
        for device in pool.config.devices
    ))
    pool.config = config
    factory.dispatcher.devices.config = config
    monkeypatch.setenv("HOME", str(factory.device.home))
    job_id, started = factory.device.run_one()
    assert started is None
    for retry_at in (30, 91, 212, 453, 694):
        factory.poll(START + retry_at)
        factory.now[0] = START + retry_at + 1
        assert factory.device.waiter.step() is None
    factory.now[0] = START + 800
    factory.deployed()
    factory.poll(START + 935)
    factory.now[0] = START + 936
    assert factory.device.waiter.step() is None, "the pc, with no checkout, refuses"
    report = skill_preflight.check(
        bot="claude-bot", placement=Placement(job_id, "box", "claude", "claude-bot", START),
        device=config.device("box"), transport=SshTransport(), home=factory.device.home,
        checkout=str(factory.root), root=factory.root,
    )
    assert report.error is None, "the box is current"

    factory.poll(START + 937)
    assert factory.stuck() == (0, None), "the current box can run the retry"
    factory.poll(START + 936 + LAGGING_BACKOFF_SECONDS[-1])
    assert factory.device.waiter.step() == job_id
    assert factory.jobs() == [(job_id, "running", 1)], "the same row starts on the box"
    assert factory.stuck() == (0, None)


@pytest.mark.parametrize("dispatch_first", [False, True], ids=["waiter-first", "dispatcher-first"])
def test_a_lagging_separate_box_checkout_leaves_a_current_remote_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path, dispatch_first: bool
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    local_root = tmp_path / "local-factory"
    shutil.copytree(factory.root, local_root)
    subprocess.run([*git_in(factory.root), "reset", "-q", "--hard", factory.later], check=True)
    pool = factory.device.waiter.pool
    config = replace(pool.config, devices=tuple(
        replace(device, factory_root=str(local_root)) if device.local else device
        for device in pool.config.devices
    ))
    pool.config = config
    factory.dispatcher.devices.config = config
    job_id, started = factory.device.run_one()
    assert started is None
    with factory.device.queue.connect() as connection:
        rows = device_sync.read(connection)
    assert [(row.device, row.checkout, row.state) for row in rows] == [
        ("box", str(local_root), device_sync.LAGGING)
    ]
    factory.poll(START + 30)
    assert factory.jobs() == [(job_id, "queued", 1)]
    config = replace(config, devices=tuple(replace(device, enabled=True) for device in config.devices))
    pool.config = config
    factory.dispatcher.devices.config = config
    subprocess.run(["git", "clone", "-q", str(factory.root), str(factory.device.checkout)], check=True)
    report = skill_preflight.check(
        bot="claude-bot", placement=Placement(job_id, "pc", "claude", "claude-bot", START),
        device=config.device("pc"), transport=SshTransport(), home=factory.device.home,
        checkout=str(factory.device.checkout), root=factory.root,
    )
    assert report.error is None, "the pc is current"
    with factory.device.queue.connect() as connection:
        blocked = device_sync.blocked(connection)
    assert choose_device(config, "claude", {}, blocked).name == "pc"

    factory.now[0] = START + 935
    if dispatch_first:
        factory.poll(START + 935)
        assert factory.jobs() == [(job_id, "queued", 1)], "the lagging box blocks only the box"
        assert factory.device.waiter.step() == job_id
    else:
        assert factory.device.waiter.step() == job_id
        factory.poll(START + 935)
    assert factory.jobs() == [(job_id, "running", 1)], "the same row starts on the pc"
    assert factory.stuck() == (0, None)


def test_a_box_factory_root_that_links_to_the_source_checkout_still_refuses_every_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_root: Path
) -> None:
    factory = Factory(tmp_path, monkeypatch, lock_root)
    alias = tmp_path / "factory-alias"
    alias.symlink_to(factory.root, target_is_directory=True)
    pool = factory.device.waiter.pool
    config = replace(pool.config, devices=tuple(
        replace(device, factory_root=str(alias)) if device.local else device
        for device in pool.config.devices
    ))
    pool.config = config
    factory.dispatcher.devices.config = config
    job_id, started = factory.device.run_one()
    assert started is None
    with factory.device.queue.connect() as connection:
        rows = device_sync.read(connection)
    assert [(row.device, row.checkout, row.state) for row in rows] == [
        ("box", str(alias), device_sync.LAGGING)
    ]
    factory.poll(START + 30)
    config = replace(config, devices=tuple(replace(device, enabled=True) for device in config.devices))
    pool.config = config
    factory.dispatcher.devices.config = config
    with pytest.raises(skill_preflight.CheckoutLagging) as refusal:
        skill_preflight.check(
            bot="claude-bot", placement=Placement(job_id, "pc", "claude", "claude-bot", START),
            device=config.device("pc"), transport=SshTransport(), home=factory.device.home,
            checkout=str(factory.device.checkout), root=factory.root,
        )
    assert refusal.value.box_behind_origin, "the pc is refused for the same source lag"

    factory.poll(START + 935)
    assert factory.jobs() == [(job_id, "failed", 1)]
    assert factory.stuck() == (1, "device-lagging")
