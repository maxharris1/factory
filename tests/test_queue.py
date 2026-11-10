import inspect
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib.queue import JobRequest, QueueError, SQLiteJobQueue


def make_queue(path: Path, now: list[float]) -> SQLiteJobQueue:
    return SQLiteJobQueue(path, clock=lambda: now[0])


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
    now = [10.0]
    first_queue = make_queue(path, now)
    first = first_queue.enqueue(request(), dedupe_key="t1:42")
    now[0] = 20
    duplicate = first_queue.enqueue(request(), dedupe_key="t1:42")
    restarted_queue = make_queue(path, now)
    now[0] = 30
    claimed = restarted_queue.claim("claude")

    assert duplicate.id == first.id
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.state == "running"
    assert claimed.last_activity_at == 30
    assert restarted_queue.claim("claude") is None


def test_claim_only_returns_jobs_for_the_requested_recipient(tmp_path: Path) -> None:
    now = [10.0]
    queue = make_queue(tmp_path / "queue.sqlite3", now)
    queue.enqueue(
        request(action="review", recipient="codex"),
        dedupe_key="review",
    )

    now[0] = 20
    assert queue.claim("claude") is None
    claimed = queue.claim("codex")
    assert claimed is not None
    assert claimed.request.action == "review"


def test_heartbeat_and_successful_finish_record_the_handoff(tmp_path: Path) -> None:
    now = [10.0]
    queue = make_queue(tmp_path / "queue.sqlite3", now)
    queued = queue.enqueue(request(), dedupe_key="build")
    now[0] = 20
    running = queue.claim("claude")
    assert running is not None

    now[0] = 30
    heartbeat = queue.heartbeat(running.id)
    now[0] = 40
    finished = queue.finish(
        running.id,
        handoff_comment_id="handoff-9",
    )
    now[0] = 50
    repeated = queue.finish(
        running.id,
        handoff_comment_id="handoff-9",
    )

    assert queued.id == heartbeat.id
    assert heartbeat.last_activity_at == 30
    assert finished.state == "done"
    assert finished.handoff_comment_id == "handoff-9"
    assert repeated == finished
    assert queue.unhandled_failures() == ()


def test_finish_without_handoff_exposes_one_t6_failure(tmp_path: Path) -> None:
    now = [10.0]
    queue = make_queue(tmp_path / "queue.sqlite3", now)
    queue.enqueue(request(), dedupe_key="build")
    now[0] = 20
    running = queue.claim("claude")
    assert running is not None

    now[0] = 30
    failed = queue.finish(running.id, handoff_comment_id=None)
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
    now = [10.0]
    queue = make_queue(path, now)
    queue.enqueue(request(), dedupe_key="build")
    now[0] = 20
    stale = queue.claim("claude")
    assert stale is not None
    now[0] = 30
    make_queue(path, now).heartbeat(stale.id)

    now[0] = 40
    assert queue.mark_timed_out(stale) is False
    current = queue.outstanding()
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
    queue = make_queue(tmp_path / "queue.sqlite3", [10.0])
    with pytest.raises(QueueError):
        queue.enqueue(invalid, dedupe_key="invalid")


def test_concurrent_claim_dispatches_one_job_once(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    now = [10.0]
    queue = make_queue(path, now)
    queue.enqueue(request(), dedupe_key="build")
    source = inspect.getsource(SQLiteJobQueue.claim)
    assert 'execute("BEGIN IMMEDIATE")' in source

    def claim() -> str | None:
        job = make_queue(path, now).claim("claude")
        return job.id if job is not None else None

    with ThreadPoolExecutor(max_workers=16) as executor:
        claimed = list(executor.map(lambda _index: claim(), range(32)))

    assert len([job_id for job_id in claimed if job_id is not None]) == 1


@pytest.mark.parametrize("contents", [b"", b"not a sqlite database"])
def test_queue_refuses_empty_or_garbage_database(
    tmp_path: Path,
    contents: bytes,
) -> None:
    path = tmp_path / "d.db"
    path.write_bytes(contents)

    with pytest.raises(QueueError, match="database file"):
        make_queue(path, [10.0])

    assert path.read_bytes() == contents
