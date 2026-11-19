from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from lib import device_deploy, device_sync
from lib.device_pool import DevicePool, placement_blocks
from lib.dispatcher import Dispatcher
from lib.machines_yml import Device, MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, Called
from lib.skill_preflight import Pinned
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions

OLD = "a" * 40
NEW = "b" * 40


def remote(name: str) -> Device:
    return Device(
        name=name,
        slot_max=2,
        harnesses=MappingProxyType({"claude": "claude-bot"}),
        tailscale_name=f"{name}.example.ts.net",
        workdir_root="/home/work",
        ssh_alias=f"{name}-{{bot}}",
        factory_root="/home/{bot}/factory",
    )


def pool_at(tmp_path: Path, head: str | None, *devices: Device) -> tuple[DevicePool, SQLiteJobQueue]:
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    pool = DevicePool(queue, MachinesConfig(devices=devices), head=lambda: head)
    queue.enqueue(
        JobRequest(
            repo="owner/repo", ticket_id="1", action="build", recipient="claude",
            source_comment_id="go-1", timeout_seconds=5400,
        ),
        dedupe_key="1",
    )
    return pool, queue


def deploy_pass_wrote(queue: SQLiteJobQueue, device: str, state: str, sha: str, target: str) -> None:
    with queue.connect() as connection:
        device_sync.upsert(
            connection, device=device, bot="claude-bot", harness="claude",
            checkout="/home/claude-bot/factory", target=target, sha=sha, state=state,
            detail="" if state == device_sync.SYNCED else "fetch failed", now=50.0,
            replaces_deploying=True,
        )
        connection.commit()


def jobs(queue: SQLiteJobQueue) -> list[tuple[str, int]]:
    with queue.connect() as connection:
        rows = connection.execute("SELECT state, attempt FROM jobs")
        return [(str(state), int(attempt)) for state, attempt in rows]


class Monitor:
    def __init__(self, pool: DevicePool):
        self.pool = pool
        self.config = pool.config

    def reconcile(self, now, lost_after, on_lost):
        return None


def dispatcher_for(tmp_path: Path, pool: DevicePool) -> Dispatcher:
    return Dispatcher(
        tmp_path / "d.db", pool.queue, trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(), grok_delivery=FakeGrokDelivery(),
        devices=Monitor(pool),
    )


class Offline:
    def call(self, argv, *, timeout, input=None) -> Called:
        return Called(SSH_UNREACHABLE, "", "")


def offline_pass(pool: DevicePool) -> None:
    device_deploy.run_pass(
        pool.queue, pool.config, sha=NEW, url="unused", pinned=Pinned("x", {}, {}),
        transport=Offline(),
    )


def test_a_lagging_device_takes_no_job_until_the_deploy_pass_syncs_it(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)

    assert pool.claim("claude") is None
    assert jobs(queue) == [("queued", 1)]

    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=NEW, target=NEW)
    claimed = pool.claim("claude")

    assert claimed is not None
    assert claimed[1].device == "mini"


def test_a_device_that_fails_to_sync_leaves_the_job_queued(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)
    deploy_pass_wrote(queue, "mini", device_sync.REFUSED, sha=OLD, target=NEW)

    assert pool.claim("claude") is None
    assert pool.claim("claude") is None
    assert jobs(queue) == [("queued", 1)]


def test_a_pool_with_one_current_device_places_the_job_there(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"), remote("studio"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)
    deploy_pass_wrote(queue, "studio", device_sync.SYNCED, sha=NEW, target=NEW)

    claimed = pool.claim("claude")

    assert claimed is not None
    assert claimed[1].device == "studio"


def test_an_unreadable_box_head_blocks_nothing_extra(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, None, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)

    claimed = pool.claim("claude")

    assert claimed is not None
    assert claimed[1].device == "mini"


def test_a_job_waiting_on_a_remote_behind_the_box_head_never_times_out(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)
    dispatcher = dispatcher_for(tmp_path, pool)
    assert pool.claim("claude") is None

    queue.clock = lambda: 100.0 + 5400
    dispatcher.reconcile_jobs()

    assert jobs(queue) == [("queued", 1)]


def test_a_harness_whose_every_remote_is_behind_the_box_head_is_lag_blocked(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)

    assert dispatcher_for(tmp_path, pool)._lag_blocked_recipients() == {"claude"}


def test_a_remote_behind_the_box_head_stays_blocked_through_an_unreachable_pass(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)

    offline_pass(pool)

    assert placement_blocks(queue, pool.config, NEW).blocks("mini", "claude")
    assert pool.claim("claude") is None


def test_a_refused_checkout_stays_refused_through_an_unreachable_pass(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.REFUSED, sha=NEW, target=NEW)

    offline_pass(pool)

    with queue.connect() as connection:
        assert [row.state for row in device_sync.read(connection)] == [device_sync.REFUSED]
    assert pool.claim("claude") is None
