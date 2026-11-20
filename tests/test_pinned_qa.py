from pathlib import Path

from lib.device_pool import DevicePool
from tests.qa_support import REPO
from tests.test_pinned_placement import CONFIG, enqueue, state
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
