from pathlib import Path

import pytest

from lib import cli
from lib.device_pool import DevicePool
from lib.orch_yml import OrchConfig, ProjectPins
from lib.queue import SQLiteJobQueue
from tests.qa_support import REPO
from tests.test_pinned_placement import CONFIG, FREE, PINNED, enqueue, state, write_orch
from tests.test_qa_cloud import Lab


def lab_pinned_to_pc(tmp_path: Path, pins: dict[str, frozenset[str]]) -> tuple[Lab, DevicePool]:
    lab = Lab(tmp_path, lanes=1)
    pool = DevicePool(lab.run.queue, CONFIG, pins=lambda: pins)
    lab.worker.placeable = pool.room_for
    return lab, pool


def test_a_pinned_projects_qa_job_waits_while_its_listed_device_is_full(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {REPO.lower(): frozenset({"pc"})})
    holder = enqueue(lab.run.queue, REPO, "holder")
    assert pool.claim("claude")[1].device == "pc"
    job_id = lab.enqueue(ids=1)

    for _ in range(3):
        lab.worker._claim()
        lab.run.now[0] += 200
        lab.run.queue.heartbeat(holder.id, owner="claude")
        lab.run.dispatcher.reconcile_jobs()
        assert state(lab.run.queue, job_id) == "queued"
    assert lab.lanes() == []

    lab.run.queue.finish(holder.id, owner="claude", handoff_comment_id="ready-1")
    assert pool.release(holder.id, owner="claude") is True
    lab.worker._claim()

    assert state(lab.run.queue, job_id) == "running"
    assert [row[5] for row in lab.lanes()] == ["claude-cloud"]


def test_an_unpinned_projects_qa_job_is_claimed_while_other_devices_are_full(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {"owner/other": frozenset({"pc"})})
    enqueue(lab.run.queue, "owner/other", "holder")
    assert pool.claim("claude")[1].device == "pc"
    job_id = lab.enqueue(ids=1)

    lab.worker._claim()

    assert state(lab.run.queue, job_id) == "running"


def test_the_qa_worker_runtime_reads_its_pins_from_orch_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_NO_LOCAL_OVERLAY", "1")
    monkeypatch.setattr(cli, "_machines_config", lambda path, *, required: CONFIG)
    monkeypatch.setattr(cli, "_machines_path", lambda path: None)
    orch = tmp_path / "orch.yml"
    write_orch(orch, "[pc]")
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 100.0)
    pool = DevicePool(queue, CONFIG, pins=ProjectPins(orch))
    holder = enqueue(queue, PINNED, "holder")
    assert pool.claim("claude")[1].device == "pc"

    worker = cli._qa_runtime(database, OrchConfig(poll_seconds=60, projects=()), orch)

    assert worker.placeable("Owner/Pinned") is False
    assert worker.placeable(FREE) is True
    queue.finish(holder.id, owner="claude", handoff_comment_id="ready-1")
    assert pool.release(holder.id, owner="claude") is True
    assert worker.placeable("Owner/Pinned") is True
