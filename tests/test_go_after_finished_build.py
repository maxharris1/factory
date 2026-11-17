"""A driver go after the generation's job finished queues a fresh one (#808)."""

from pathlib import Path

import pytest

from lib.dispatch.helpers import _scope
from lib.dispatch.transitions import write_workflow_state
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    GO,
    _attention,
    comment,
    handled_outcome,
    queued_harness_jobs,
    setup,
    ticket,
)

SCOPE = _scope("owner/repo", "42")


def go(comment_id: str, state: str, created_at: float) -> object:
    return comment(comment_id, GO.format(state=state), author="grok-login", created_at=created_at)


def set_state(queue: SQLiteJobQueue, state: str) -> None:
    with queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = ?"
            " WHERE repo = 'owner/repo' AND ticket_id = '42'",
            (state,),
        )


def finish_next(queue: SQLiteJobQueue, action: str) -> None:
    job = queue.claim("claude")
    assert job is not None and job.request.action == action
    queue.finish(job.id, owner="claude", handoff_comment_id=f"handoff-{job.id}")


def job_keys(queue: SQLiteJobQueue, action: str) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        return [
            (row["dedupe_key"], row["source_comment_id"])
            for row in connection.execute(
                "SELECT dedupe_key, source_comment_id FROM jobs WHERE action = ? ORDER BY rowid",
                (action,),
            )
        ]


def replay(queue: SQLiteJobQueue, comment_id: str) -> None:
    with queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = ?", (comment_id,))


def enqueue(queue: SQLiteJobQueue, action: str, recipient: str) -> None:
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action=action,
            recipient=recipient,
            source_comment_id=f"{action}-source",
            timeout_seconds=5400,
        ),
        dedupe_key=f"test-{action}:{recipient}",
    )


@pytest.mark.parametrize("state", ["fixing", "building"])
def test_a_go_after_a_finished_build_queues_a_build_keyed_on_the_go(
    tmp_path: Path, state: str
) -> None:
    dispatcher, queue = setup(tmp_path)
    first = go("go-1", "building", 1)
    dispatcher.process(ticket(first))
    finish_next(queue, "build")
    set_state(queue, state)
    second = go("go-2", "building", 2)

    dispatcher.process(ticket(first, second))

    assert handled_outcome(queue, "go-2") == "queued-build"
    (build,) = queued_harness_jobs(queue)
    assert build.request.source_comment_id == "go-2"
    assert job_keys(queue, "build")[-1] == (f"t1:{SCOPE}:build:go-2", "go-2")
    assert "dedupe-dropped" not in _attention(queue)

    finish_next(queue, "build")
    set_state(queue, state)
    replay(queue, "go-2")
    dispatcher.process(ticket(first, second))

    assert handled_outcome(queue, "go-2") == "deduped-build"
    assert queued_harness_jobs(queue) == []
    assert len(job_keys(queue, "build")) == 2


def test_a_planning_go_on_a_fixing_ticket_after_a_finished_plan_queues_a_plan(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    first = go("go-1", "planning", 1)
    dispatcher.process(ticket(first))
    finish_next(queue, "plan")
    set_state(queue, "fixing")
    second = go("go-2", "planning", 2)

    dispatcher.process(ticket(first, second))

    assert handled_outcome(queue, "go-2") == "queued-plan"
    (plan,) = queued_harness_jobs(queue)
    assert plan.request.source_comment_id == "go-2"
    assert job_keys(queue, "plan")[-1] == (f"t1:{SCOPE}:plan:go-2", "go-2")


@pytest.mark.parametrize("claimed", [False, True], ids=["queued", "running"])
def test_a_go_while_the_generation_build_is_outstanding_is_deduped_quietly(
    tmp_path: Path, claimed: bool
) -> None:
    dispatcher, queue = setup(tmp_path)
    first = go("go-1", "building", 1)
    dispatcher.process(ticket(first))
    if claimed:
        assert queue.claim("claude") is not None

    dispatcher.process(ticket(first, go("go-2", "building", 2)))

    assert handled_outcome(queue, "go-2") == "deduped-build"
    assert job_keys(queue, "build") == [(f"t1:{SCOPE}:build:g1", "go-1")]
    assert "dedupe-dropped" not in _attention(queue)


def test_a_go_on_a_fixing_ticket_with_a_queued_fix_queues_no_parallel_build(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    first = go("go-1", "building", 1)
    dispatcher.process(ticket(first))
    finish_next(queue, "build")
    set_state(queue, "fixing")
    enqueue(queue, "fix", "claude")

    dispatcher.process(ticket(first, go("go-2", "building", 2)))

    assert handled_outcome(queue, "go-2") == "deduped-build"
    assert len(job_keys(queue, "build")) == 1


def dropped_go(tmp_path: Path) -> SQLiteJobQueue:
    dispatcher, queue = setup(tmp_path)
    first = go("go-1", "building", 1)
    dispatcher.process(ticket(first))
    finish_next(queue, "build")
    set_state(queue, "review")
    dispatcher.process(ticket(first, go("go-2", "building", 2)))
    assert handled_outcome(queue, "go-2") == "deduped-build"
    assert _attention(queue) == ["dedupe-dropped"]
    return queue


def test_a_later_harness_job_clears_the_dedupe_dropped_flag(tmp_path: Path) -> None:
    queue = dropped_go(tmp_path)

    enqueue(queue, "clarify", "grok")
    assert _attention(queue) == ["dedupe-dropped"]

    enqueue(queue, "fix", "claude")
    assert _attention(queue) == []


def test_a_workflow_state_change_clears_the_dedupe_dropped_flag(tmp_path: Path) -> None:
    queue = dropped_go(tmp_path)

    for state, flags in (("review", ["dedupe-dropped"]), ("fixing", [])):
        with queue.connect() as connection:
            write_workflow_state(connection, "owner/repo", "42", state, source="test", at=1)
            connection.commit()
        assert _attention(queue) == flags
