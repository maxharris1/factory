"""Repro A from the #818 round-3 review (#852): enabling a device while the
poller still acts on its old, disabled view must not fail or signal a job
claimed onto that device."""

from __future__ import annotations

import os
from pathlib import Path

from lib import settings_apply
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatcher import Dispatcher
from lib.queue import SQLiteJobQueue
from lib.settings_apply import Applier, SettingsRequest
from tests import test_remote_start as remote
from tests.merge_queue_support import Board, sha
from tests.test_dispatcher import RETRY, TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions
from tests.test_settings_apply import ENABLE_PC, SignalTransport, loaded

TAG = "0123456789abcdef0123456789abcdef"


def enable(machines: Path, queue: SQLiteJobQueue, state: Path, restart, pending, run):
    state.mkdir()
    return Applier(machines, state, state / "deploy", "maxharris1", {}, restart, pending,
                   settings_apply.claim_lock(queue.path), run=run).apply(
        SettingsRequest("enable-request-abcdefghij", "maxharris1", "2026-11-17T10:00:00Z",
                        "pc", "enabled", False, True))


def run_on(pool: DevicePool, job_id: str) -> None:
    pool.attach_runner(job_id, owner="claude", pid=os.getpid(), start=None,
                       alive=lambda pid, start: pid is not None)
    pool.attach_worker(job_id, owner="claude", remote=True, tag=TAG)


def test_a_job_claimed_after_an_enable_is_not_lost_while_the_poller_finishes_its_cycle(
    tmp_path: Path,
) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text(ENABLE_PC, encoding="utf-8")
    config = loaded(machines)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 100.0)
    waiter = DevicePool(queue, config, machines_path=machines)
    transport = SignalTransport()
    monitor = DeviceMonitor(DevicePool(queue, config, machines_path=machines), config,
                            transport, machines_path=machines,
                            alive=lambda pid, start: pid is not None)
    dispatcher = Dispatcher(queue.path, queue, trusted_logins=TRUSTED_LOGINS,
                            devices=monitor, review_actions=FakeReviewActions(),
                            grok_delivery=FakeGrokDelivery())
    job = remote.enqueue(queue, "claude", "1", timeout=5400)
    monitor.reload()
    assert not monitor.config.device("pc").enabled
    during: list[str | None] = []
    lost_during: list[tuple[str, ...]] = []

    def poller_still_finishing_its_cycle(argv, environ, log):
        if Path(argv[0]).name != "poller-service.sh":
            return 0
        claimed = waiter.claim("claude")
        during.append(None if claimed is None else claimed[1].device)
        if claimed is not None:
            run_on(waiter, job)
        lost_during.append(monitor.reconcile(100.0, 900.0, dispatcher._device_lost).lost)
        return 0

    entry = enable(machines, queue, tmp_path / "state", ("poller", "waiter:claude-bot"), (),
                   poller_still_finishing_its_cycle)

    assert entry.result == "applied", entry
    assert loaded(machines).device("pc").enabled, "the enable is on disk"
    assert during == [None], "no claim lands on pc while the old poller cycle runs"
    assert lost_during == [()], "the old cycle loses nothing"
    assert remote.state(queue, job) == "queued", "the job waits"
    assert "device-lost" not in [e for e, _ in remote.job_events(queue, job)]
    assert transport.signals == [], "no worker was signalled"

    monitor.reload()
    claimed = waiter.claim("claude")
    assert claimed is not None and claimed[1].device == "pc", (
        "the same unrestarted waiter places on pc once the poller adopts the enable"
    )
    run_on(waiter, job)
    result = monitor.reconcile(100.0, 900.0, dispatcher._device_lost)

    assert result.lost == (), "the poller holds pc enabled"
    assert remote.state(queue, job) == "running", "the job claimed onto pc keeps running"
    assert "device-lost" not in [e for e, _ in remote.job_events(queue, job)]
    assert transport.signals == [], "no worker was signalled"


def test_a_retry_does_not_lose_a_job_claimed_onto_a_device_enabled_live(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    b.finish("claude")
    b.ready(p, p.head)
    b.poll()
    b.finish("codex")
    b.revise(p, "1/3")
    b.poll()
    [fix] = b.jobs("fix")
    assert fix["state"] == "queued"
    machines = remote.machines_file(tmp_path, box_enabled=False, pc=False)
    config = loaded(machines)
    waiter = DevicePool(b.queue, config, machines_path=machines)
    transport = SignalTransport()
    b.dispatcher.devices = DeviceMonitor(
        DevicePool(b.queue, config, machines_path=machines), config, transport,
        machines_path=machines, alive=lambda pid, start: pid is not None,
    )
    b.poll()

    entry = enable(machines, b.queue, tmp_path / "state", (), ("poller", "waiter-claude-bot"),
                   lambda *_: 0)

    assert entry.result == "pending", entry
    assert waiter.claim("claude") is None, "no claim lands on pc before the poller adopts"
    assert [r["state"] for r in b.jobs("fix")] == ["queued"]

    b.poll()
    claimed = waiter.claim("claude")
    assert claimed is not None and claimed[1].device == "pc", (
        "the same unrestarted waiter places on pc after the poller's next cycle"
    )
    run_on(waiter, claimed[0].id)
    b.post(p, "retry-1", RETRY.format(reason="Max: retry."), "grok-login")
    b.poll()

    assert b.outcome("retry-1") == "rejected-retry-not-stuck", "pc is enabled, nothing stranded"
    assert [r["state"] for r in b.jobs("fix")] == ["running"]
    assert "device-lost" not in [e for e, _ in remote.job_events(b.queue, claimed[0].id)]
    assert transport.signals == [], "no worker was signalled"
