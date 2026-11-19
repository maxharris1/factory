"""#1018: a claim never places a job on a remote checkout behind the box HEAD."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from lib import device_sync
from lib.device_pool import DevicePool
from lib.machines_yml import Device, MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue

OLD = "a" * 40
NEW = "b" * 40


def remote(name: str) -> Device:
    return Device(
        name=name,
        slot_max=2,
        harnesses=MappingProxyType({"claude": "claude-bot"}),
        tailscale_name=f"{name}.example.ts.net",
        workdir_root="/home/work",
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


def job_states(queue: SQLiteJobQueue) -> list[str]:
    with queue.connect() as connection:
        return [str(row[0]) for row in connection.execute("SELECT state FROM jobs")]


def test_a_lagging_device_takes_no_job_until_the_deploy_pass_syncs_it(tmp_path: Path) -> None:
    pool, queue = pool_at(tmp_path, NEW, remote("mini"))
    deploy_pass_wrote(queue, "mini", device_sync.SYNCED, sha=OLD, target=OLD)

    assert pool.claim("claude") is None
    assert job_states(queue) == ["queued"]

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
    assert job_states(queue) == ["queued"]


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
