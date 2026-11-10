from pathlib import Path

import pytest

from lib.queue import JobRequest, QueueError, SQLiteJobQueue


def request(
    *,
    action: str = "build",
    recipient: str = "claude",
    attempt: int = 1,
) -> JobRequest:
    return JobRequest(
        repo="owner/repo",
        ticket_id="42",
        action=action,
        recipient=recipient,
        source_comment_id="comment-1",
        timeout_seconds=5400,
        attempt=attempt,
    )


def test_queue_contract_is_durable_and_deduplicates_enqueue(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    first_queue = SQLiteJobQueue(path)
    first = first_queue.enqueue(request(), dedupe_key="t1:42", now=10)
    duplicate = first_queue.enqueue(request(), dedupe_key="t1:42", now=20)
    restarted_queue = SQLiteJobQueue(path)
    claimed = restarted_queue.claim("claude", now=30)

    assert duplicate.id == first.id
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.state == "running"
    assert claimed.last_activity_at == 30
    assert restarted_queue.claim("claude", now=31) is None


def test_claim_only_returns_jobs_for_the_requested_recipient(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(
        request(action="review", recipient="codex"),
        dedupe_key="review",
        now=10,
    )

    assert queue.claim("claude", now=20) is None
    claimed = queue.claim("codex", now=20)
    assert claimed is not None
    assert claimed.request.action == "review"


def test_heartbeat_and_successful_finish_record_the_handoff(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "queue.sqlite3")
    queued = queue.enqueue(request(), dedupe_key="build", now=10)
    running = queue.claim("claude", now=20)
    assert running is not None

    heartbeat = queue.heartbeat(running.id, now=30)
    finished = queue.finish(
        running.id,
        handoff_comment_id="handoff-9",
        now=40,
    )
    repeated = queue.finish(
        running.id,
        handoff_comment_id="handoff-9",
        now=50,
    )

    assert queued.id == heartbeat.id
    assert heartbeat.last_activity_at == 30
    assert finished.state == "done"
    assert finished.handoff_comment_id == "handoff-9"
    assert repeated == finished
    assert queue.unhandled_failures() == ()


def test_finish_without_handoff_exposes_one_t6_failure(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "queue.sqlite3")
    queue.enqueue(request(), dedupe_key="build", now=10)
    running = queue.claim("claude", now=20)
    assert running is not None

    failed = queue.finish(running.id, handoff_comment_id=None, now=30)
    assert failed.state == "failed"
    assert queue.unhandled_failures() == (failed,)

    queue.mark_failure_handled(failed.id)
    assert queue.unhandled_failures() == ()
    with pytest.raises(QueueError, match="already handled"):
        queue.mark_failure_handled(failed.id)


def test_timed_out_update_fails_closed_after_a_concurrent_heartbeat(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    queue = SQLiteJobQueue(path)
    queue.enqueue(request(), dedupe_key="build", now=10)
    stale = queue.claim("claude", now=20)
    assert stale is not None
    SQLiteJobQueue(path).heartbeat(stale.id, now=30)

    assert queue.mark_timed_out(stale, now=40) is False
    current = queue.running()
    assert len(current) == 1
    assert current[0].last_activity_at == 30


@pytest.mark.parametrize(
    "invalid",
    [
        request(action="merge"),
        request(recipient="unknown"),
        request(attempt=3),
    ],
)
def test_queue_refuses_invalid_jobs(tmp_path: Path, invalid: JobRequest) -> None:
    queue = SQLiteJobQueue(tmp_path / "queue.sqlite3")
    with pytest.raises(QueueError):
        queue.enqueue(invalid, dedupe_key="invalid", now=10)
