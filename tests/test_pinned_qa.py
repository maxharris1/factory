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
    lab.worker.place = pool.place_qa
    return lab, pool


def test_real_qa_is_placed_on_pc(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {REPO.lower(): frozenset({"pc"})})
    qa_id = lab.enqueue(ids=1)

    lab.worker._claim()

    assert state(lab.run.queue, qa_id) == "running"
    assert pool.used() == {"pc": 1}
    enqueue(lab.run.queue, REPO, "build")
    assert pool.claim("claude") is None


def test_qa_does_not_claim_after_pc_slot_is_taken(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {REPO.lower(): frozenset({"pc"})})
    qa_id = lab.enqueue(ids=1)
    plan = lab.worker._plan
    competing = []

    def plan_after_competing_claim(job):
        planned = plan(job)
        if not competing:
            competing.append(enqueue(lab.run.queue, REPO, "competing"))
            assert pool.claim("claude")[1].device == "pc"
        return planned

    lab.worker._plan = plan_after_competing_claim
    lab.worker._claim()

    assert state(lab.run.queue, qa_id) == "queued"
    assert pool.used() == {"pc": 1}
    assert lab.lanes() == []

    holder = competing[0]
    lab.run.queue.finish(holder.id, owner="claude", handoff_comment_id="ready-1")
    assert pool.release(holder.id, owner="claude") is True
    lab.worker._claim()

    assert state(lab.run.queue, qa_id) == "running"
    assert pool.used() == {"pc": 1}


def test_a_pinned_projects_qa_job_waits_while_its_listed_device_is_full(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {REPO.lower(): frozenset({"pc"})})
    holder = enqueue(lab.run.queue, REPO, "holder")
    assert pool.claim("claude")[1].device == "pc"
    qa_id = lab.enqueue(ids=1)

    for _ in range(3):
        lab.worker._claim()
        lab.run.now[0] += 200
        lab.run.queue.heartbeat(holder.id, owner="claude")
        lab.run.dispatcher.reconcile_jobs()
        assert state(lab.run.queue, qa_id) == "queued"
    assert lab.lanes() == []
    assert pool.used() == {"pc": 1}

    lab.run.queue.finish(holder.id, owner="claude", handoff_comment_id="ready-1")
    assert pool.release(holder.id, owner="claude") is True
    lab.worker._claim()

    assert state(lab.run.queue, qa_id) == "running"
    assert pool.used() == {"pc": 1}


def test_two_pinned_qa_jobs_do_not_share_one_free_slot(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {REPO.lower(): frozenset({"pc"})})
    first = lab.enqueue(ids=1, ticket="784")
    second = lab.enqueue(ids=1, ticket="785")

    lab.worker._claim()

    assert sorted([state(lab.run.queue, first), state(lab.run.queue, second)]) == ["queued", "running"]
    assert pool.used() == {"pc": 1}


def test_a_finished_qa_job_frees_its_slot(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {REPO.lower(): frozenset({"pc"})})
    qa_id = lab.enqueue(ids=1)
    lab.worker._claim()
    assert pool.used() == {"pc": 1}

    lab.run.queue.finish(qa_id, owner="qa", handoff_comment_id=None)

    assert pool.used() == {}
    enqueue(lab.run.queue, REPO, "build")
    assert pool.claim("claude")[1].device == "pc"


def test_an_unpinned_projects_qa_job_is_claimed_while_other_devices_are_full(tmp_path: Path) -> None:
    lab, pool = lab_pinned_to_pc(tmp_path, {"owner/other": frozenset({"pc"})})
    enqueue(lab.run.queue, "owner/other", "holder")
    assert pool.claim("claude")[1].device == "pc"
    qa_id = lab.enqueue(ids=1)

    lab.worker._claim()

    assert state(lab.run.queue, qa_id) == "running"
    assert pool.used() == {"pc": 1}


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
    pinned_qa = enqueue(queue, PINNED, "pinned-qa", action="qa", recipient="qa")
    free_qa = enqueue(queue, FREE, "free-qa", action="qa", recipient="qa")

    worker = cli._qa_runtime(database, OrchConfig(poll_seconds=60, projects=()), orch)

    def places(job_id: str, repo: str) -> bool:
        with queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placed = worker.place(connection, job_id, repo)
            connection.rollback()
        return placed

    assert places(pinned_qa.id, "Owner/Pinned") is False
    assert places(free_qa.id, FREE) is True
    queue.finish(holder.id, owner="claude", handoff_comment_id="ready-1")
    assert pool.release(holder.id, owner="claude") is True
    assert places(pinned_qa.id, "Owner/Pinned") is True
