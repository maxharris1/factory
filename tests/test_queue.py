import dataclasses
import inspect
import json
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib import cli, job_detail
from lib.dashboard import _detail_round
from lib.dispatcher import Dispatcher
from lib import queue as queue_module
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


class _SingleRow:
    def __init__(self, row: sqlite3.Row | None):
        self.row = row

    def fetchone(self) -> sqlite3.Row | None:
        return self.row


class _InterleavingConnection(sqlite3.Connection):
    """Runs `interleave` at one point inside a queue operation.

    The hook points match SQL text: before `BEGIN IMMEDIATE`, and after a
    `SELECT * FROM jobs WHERE id = ?` read outside a transaction. Rewording
    either statement in lib/queue.py silently skips the interleave, so
    test_finish_races_t6_timeout_threaded backs these tests with a real race.
    """

    interleave: Callable[[], None] | None = None

    def execute(self, sql: str, parameters=()):
        if self.interleave is not None and sql == "BEGIN IMMEDIATE":
            interleave = self.interleave
            self.interleave = None
            interleave()
        cursor = super().execute(sql, parameters)
        if (
            self.interleave is not None
            and not self.in_transaction
            and sql.strip().startswith("SELECT * FROM jobs WHERE id = ?")
        ):
            row = cursor.fetchone()
            interleave = self.interleave
            self.interleave = None
            interleave()
            return _SingleRow(row)
        return cursor


class _InterleavingQueue(SQLiteJobQueue):
    interleave: Callable[[], None] | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            factory=_InterleavingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.interleave = self.interleave
        return connection


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
    heartbeat = queue.heartbeat(running.id, owner="claude")
    now[0] = 40
    finished = queue.finish(
        running.id,
        owner="claude",
        handoff_comment_id="handoff-9",
    )
    now[0] = 50
    repeated = queue.finish(
        running.id,
        owner="claude",
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
    failed = queue.finish(running.id, owner="claude", handoff_comment_id=None)
    assert failed.state == "failed"
    assert queue.unhandled_failures() == (failed,)

    queue.mark_failure_handled(failed.id)
    assert queue.unhandled_failures() == ()
    with pytest.raises(QueueError, match="already handled"):
        queue.mark_failure_handled(failed.id)


def test_finish_refuses_the_wrong_owner(tmp_path: Path) -> None:
    queue = make_queue(tmp_path / "queue.sqlite3", [10.0])
    queue.enqueue(request(), dedupe_key="build")
    running = queue.claim("claude")
    assert running is not None

    with pytest.raises(QueueError, match="belongs to claude, not codex"):
        queue.finish(
            running.id,
            owner="codex",
            handoff_comment_id="handoff-9",
        )

    assert queue.outstanding() == (running,)


def test_release_returns_only_the_owners_running_job_to_queued(
    tmp_path: Path,
) -> None:
    now = [10.0]
    queue = make_queue(tmp_path / "queue.sqlite3", now)
    queued = queue.enqueue(request(), dedupe_key="build")
    running = queue.claim("claude")
    assert running is not None

    with pytest.raises(QueueError, match="belongs to claude, not codex"):
        queue.release(running.id, owner="codex")
    now[0] = 20
    released = queue.release(running.id, owner="claude")

    assert released.id == queued.id
    assert released.state == "queued"
    assert released.created_at == 10
    with pytest.raises(QueueError, match="running"):
        queue.release(running.id, owner="claude")


def test_finish_does_not_overwrite_a_t6_timeout_or_duplicate_its_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    now = [10.0]
    queue = make_queue(path, now)
    queue.enqueue(request(), dedupe_key="build")
    running = queue.claim("claude")
    assert running is not None
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins={
            "github": {"grok-bot": {"grok"}},
            "linear": {},
        },
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    finishing_queue = _InterleavingQueue(path, clock=lambda: now[0])

    def time_out_and_retry() -> None:
        now[0] = 20
        assert queue.mark_timed_out(running) is True
        dispatcher.reconcile_jobs()

    finishing_queue.interleave = time_out_and_retry
    with pytest.raises(QueueError, match="running"):
        finishing_queue.finish(
            running.id,
            owner="claude",
            handoff_comment_id="handoff-9",
        )

    with queue.connect() as connection:
        original = connection.execute(
            "SELECT state FROM jobs WHERE id = ?",
            (running.id,),
        ).fetchone()
        retries = connection.execute(
            "SELECT id FROM jobs WHERE attempt = 2",
        ).fetchall()
    assert original is not None
    assert original["state"] == "failed"
    assert len(retries) == 1


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
    make_queue(path, now).heartbeat(stale.id, owner="claude")

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


# --- #108 Unit 1: cancel (#66) ------------------------------------------------


def _dispatcher_queue(tmp_path: Path, now: list[float]) -> SQLiteJobQueue:
    from tests.test_dispatcher import setup

    return setup(tmp_path, now)[1]


def test_cancel_moves_a_queued_job_to_cancelled_with_an_event(tmp_path: Path) -> None:
    now = [100.0]
    queue = _dispatcher_queue(tmp_path, now)
    job = queue.enqueue(request(), dedupe_key="k")
    now[0] = 150.0

    cancelled = queue.cancel(job.id, "operator")

    assert (cancelled.state, cancelled.finished_at) == ("cancelled", 150.0)
    with queue.connect() as connection:
        events = connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = ?", (job.id,)
        ).fetchall()
    assert [tuple(row) for row in events] == [("cancelled", "operator")]


@pytest.mark.parametrize("finish", ["running", "done", "failed"])
def test_cancel_refuses_a_job_that_is_not_queued(tmp_path: Path, finish: str) -> None:
    queue = _dispatcher_queue(tmp_path, [100.0])
    job = queue.enqueue(request(), dedupe_key="k")
    assert queue.claim("claude") is not None
    if finish != "running":
        queue.finish(
            job.id, owner="claude", handoff_comment_id="h" if finish == "done" else None
        )

    with pytest.raises(QueueError, match="only a queued job"):
        queue.cancel(job.id, "operator")
    with queue.connect() as connection:
        row = connection.execute("SELECT state FROM jobs WHERE id = ?", (job.id,)).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE event = 'cancelled'"
        ).fetchone()[0]
    assert (row["state"], events) == (finish, 0)


def test_cancel_refuses_an_unknown_job(tmp_path: Path) -> None:
    queue = _dispatcher_queue(tmp_path, [100.0])
    with pytest.raises(QueueError, match="unknown job"):
        queue.cancel("missing", "operator")


def test_cancelled_job_is_never_retried(tmp_path: Path) -> None:
    queue = _dispatcher_queue(tmp_path, [100.0])
    job = queue.enqueue(request(), dedupe_key="k")
    queue.cancel(job.id, "operator")

    assert queue.outstanding() == ()
    assert queue.unhandled_failures() == ()
    assert queue.claim("claude") is None
    again = queue.enqueue(request(), dedupe_key="k")
    assert (again.id, again.state) == (job.id, "cancelled")


def _dispatcher(path: Path, queue: SQLiteJobQueue) -> Dispatcher:
    return Dispatcher(
        path,
        queue,
        trusted_logins={"github": {"grok-bot": {"grok"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )


def test_finish_with_a_different_handoff_is_refused(tmp_path: Path) -> None:
    queue = make_queue(tmp_path / "queue.sqlite3", [10.0])
    queue.enqueue(request(), dedupe_key="build")
    running = queue.claim("claude")
    assert running is not None
    first = queue.finish(running.id, owner="claude", handoff_comment_id="handoff-9")

    with pytest.raises(QueueError, match="running"):
        queue.finish(running.id, owner="claude", handoff_comment_id="handoff-10")
    with pytest.raises(QueueError, match="running"):
        queue.finish(running.id, owner="claude", handoff_comment_id=None)

    with queue.connect() as connection:
        row = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (first.id,)
        ).fetchone()
    assert (row["state"], row["handoff_comment_id"]) == ("done", "handoff-9")


def test_heartbeat_refuses_the_wrong_owner(tmp_path: Path) -> None:
    now = [10.0]
    queue = make_queue(tmp_path / "queue.sqlite3", now)
    queue.enqueue(request(), dedupe_key="build")
    now[0] = 20
    running = queue.claim("claude")
    assert running is not None

    now[0] = 30
    with pytest.raises(QueueError, match="belongs to claude, not codex"):
        queue.heartbeat(running.id, owner="codex")

    assert queue.outstanding()[0].last_activity_at == 20


@pytest.mark.parametrize("operation", ["release", "heartbeat", "finish"])
def test_queue_ops_refuse_an_unknown_owner(tmp_path: Path, operation: str) -> None:
    queue = make_queue(tmp_path / "queue.sqlite3", [10.0])
    queue.enqueue(request(recipient="grok"), dedupe_key="wake")
    running = queue.claim("grok")
    assert running is not None
    with queue.connect() as connection:
        connection.execute("UPDATE jobs SET recipient = 'grok-bot'")
    calls = {
        "release": lambda: queue.release(running.id, owner="grok-bot"),
        "heartbeat": lambda: queue.heartbeat(running.id, owner="grok-bot"),
        "finish": lambda: queue.finish(
            running.id, owner="grok-bot", handoff_comment_id="handoff-9"
        ),
    }

    with pytest.raises(QueueError, match="unknown owner 'grok-bot'"):
        calls[operation]()

    assert [(job.state, job.last_activity_at) for job in queue.outstanding()] == [
        ("running", 10.0)
    ]


def test_finish_races_t6_timeout_threaded(tmp_path: Path) -> None:
    for round_index in range(50):
        path = tmp_path / f"round-{round_index}.sqlite3"
        queue = SQLiteJobQueue(path, clock=lambda: 10.0)
        queue.enqueue(request(), dedupe_key="build")
        running = queue.claim("claude")
        assert running is not None
        dispatcher = _dispatcher(path, queue)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def finish() -> None:
            finisher = SQLiteJobQueue(path, clock=lambda: 10.0)
            barrier.wait()
            try:
                finisher.finish(running.id, owner="claude", handoff_comment_id="handoff-9")
            except QueueError:
                pass
            except BaseException as exc:
                errors.append(exc)

        def time_out() -> None:
            barrier.wait()
            try:
                queue.mark_timed_out(running)
                dispatcher.reconcile_jobs()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=finish), threading.Thread(target=time_out)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        with queue.connect() as connection:
            state = connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (running.id,)
            ).fetchone()["state"]
            retries = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE attempt = 2"
            ).fetchone()[0]
        assert (state, retries) in {("done", 0), ("failed", 1)}


def test_claim_refuses_a_row_that_is_no_longer_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = make_queue(tmp_path / "queue.sqlite3", [10.0])
    queue.enqueue(request(), dedupe_key="build")
    running = queue.claim("claude")
    assert running is not None

    def stale_order(
        connection: sqlite3.Connection, recipient: str, *, now: float
    ) -> list[queue_module.Candidate]:
        return [
            queue_module.Candidate(
                running.id, running.request.repo, running.request.ticket_id,
                running.request.action, running.created_at, resume=False,
            )
        ]

    monkeypatch.setattr(queue_module, "claim_order", stale_order)
    with pytest.raises(QueueError, match="no longer queued"):
        queue.claim("claude")


def test_queued_timeout_fails_closed_after_a_same_tick_claim(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    now = [10.0]
    queue = make_queue(path, now)
    queue.enqueue(request(), dedupe_key="build")
    (stale,) = queue.outstanding()
    assert stale.state == "queued"
    running = make_queue(path, now).claim("claude")
    assert running is not None
    assert running.last_activity_at == stale.last_activity_at

    assert queue.mark_timed_out(stale) is False
    assert [job.state for job in queue.outstanding()] == ["running"]


def _bulky_detail(size: int = 100_000) -> str:
    return json.dumps(
        {
            "evidence": {"criteria": {"text": "c" * size}},
            "instruction": "failed: pytest",
            "plan_text": "Plan.",
            "resume": True,
            "round": "2/3",
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _raw_detail(path: Path, job_id: str) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT detail FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_bulky_detail_keys_live_in_a_file_and_claim_returns_the_full_detail(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    queue = make_queue(path, [10.0])
    detail = _bulky_detail()
    job = queue.enqueue(replace(request(), detail=detail), dedupe_key="build")

    raw = _raw_detail(path, job.id)
    assert len(raw) < 4096
    assert json.loads(raw) == {
        "resume": True,
        "round": "2/3",
        "spilled": ["evidence", "instruction", "plan_text"],
    }
    assert (tmp_path / "job-detail" / f"{job.id}.json").is_file()
    assert job.request.detail == detail
    claimed = queue.claim("claude")
    assert claimed is not None and claimed.request.detail == detail


def test_a_retry_copy_of_a_merged_detail_spills_again_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    queue = make_queue(path, [10.0])
    first = queue.enqueue(replace(request(), detail=_bulky_detail()), dedupe_key="build")
    copy = queue.enqueue(replace(request(), detail=first.request.detail), dedupe_key="retry")

    assert json.loads(_raw_detail(path, copy.id)) == json.loads(_raw_detail(path, first.id))
    assert copy.request.detail == first.request.detail


def test_a_deduped_enqueue_writes_no_second_file(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    queue = make_queue(path, [10.0])
    first = queue.enqueue(replace(request(), detail=_bulky_detail()), dedupe_key="build")
    again = queue.enqueue(replace(request(), detail=_bulky_detail(10)), dedupe_key="build")

    assert again.id == first.id
    assert again.request.detail == _bulky_detail()
    assert [item.name for item in (tmp_path / "job-detail").iterdir()] == [f"{first.id}.json"]


def test_small_and_plain_text_detail_stays_in_the_row(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    queue = make_queue(path, [10.0])
    small = queue.enqueue(replace(request(), detail='{"round": "1/3"}'), dedupe_key="small")
    plain = queue.enqueue(replace(request(), detail="stuck: no handoff"), dedupe_key="plain")

    assert _raw_detail(path, small.id) == '{"round": "1/3"}'
    assert _raw_detail(path, plain.id) == "stuck: no handoff"
    assert (small.request.detail, plain.request.detail) == ('{"round": "1/3"}', "stuck: no handoff")
    assert not (tmp_path / "job-detail").exists()


def test_a_missing_detail_file_degrades_to_the_slim_row(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    queue = make_queue(path, [10.0])
    job = queue.enqueue(replace(request(), detail=_bulky_detail()), dedupe_key="build")
    (tmp_path / "job-detail" / f"{job.id}.json").unlink()

    claimed = queue.claim("claude")
    assert claimed is not None
    assert json.loads(claimed.request.detail or "") == {"resume": True, "round": "2/3"}


def test_a_spilled_detail_without_a_database_directory_fails_closed() -> None:
    slim, spill = job_detail.split(_bulky_detail())
    assert spill is not None
    with pytest.raises(job_detail.JobDetailError, match="no database directory"):
        job_detail.load(None, "job-1", slim)
    assert job_detail.load(None, "job-1", '{"round": "1/3"}') == '{"round": "1/3"}'


def _inline_jobs(path: Path, count: int, size: int) -> dict[str, str]:
    queue = make_queue(path, [10.0])
    details: dict[str, str] = {}
    for index in range(count):
        job = queue.enqueue(request(), dedupe_key=f"job-{index}")
        details[job.id] = _bulky_detail(size)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "UPDATE jobs SET detail = ? WHERE id = ?",
            [(detail, job_id) for job_id, detail in details.items()],
        )
    return details


def _every_reader(path: Path) -> dict[str, dict[str, object]]:
    queue = make_queue(path, [10.0])
    with queue.connect() as connection:
        rows = connection.execute("SELECT id, detail FROM jobs").fetchall()
        return {
            "job": {job.id: job.request.detail for job in queue.outstanding()},
            "verdict": {
                str(row["id"]): job_detail.load_row(connection, row["id"], row["detail"]) for row in rows
            },
            "dashboard round": {str(row["id"]): _detail_round(row["detail"]) for row in rows},
        }


def test_db_spill_detail_moves_inline_detail_out_and_vacuums(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "orch.db"
    details = _inline_jobs(path, 300, 80_000)
    before_size = path.stat().st_size
    assert before_size > 20_000_000
    before = _every_reader(path)
    assert before["job"] == details

    assert cli.main(["db-spill-detail", "--db", str(path), "--vacuum"]) == 0
    out = capsys.readouterr().out
    assert "moved the detail of 300 jobs" in out
    assert "vacuumed" in out
    assert path.stat().st_size < before_size * 0.10
    assert _every_reader(path) == before

    assert cli.main(["db-spill-detail", "--db", str(path)]) == 0
    assert "moved the detail of 0 jobs" in capsys.readouterr().out


def test_db_spill_detail_refuses_while_a_poller_holds_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "orch.db"
    details = _inline_jobs(path, 1, 10)
    with cli._poll_lock(path):
        assert cli.main(["db-spill-detail", "--db", str(path), "--vacuum"]) == 1
    assert "another orch poll owns" in capsys.readouterr().err
    assert _raw_detail(path, next(iter(details))) == next(iter(details.values()))


_MAIN_JOBS_TABLE = """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        repo TEXT NOT NULL,
        ticket_id TEXT NOT NULL,
        action TEXT NOT NULL,
        recipient TEXT NOT NULL,
        source_comment_id TEXT NOT NULL,
        timeout_seconds INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        attempt INTEGER NOT NULL,
        head TEXT,
        detail TEXT,
        state TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_activity_at REAL NOT NULL,
        finished_at REAL,
        handoff_comment_id TEXT,
        failure_handled INTEGER NOT NULL DEFAULT 0,
        claim_token_hash TEXT
    )
"""


def _claim_index(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name = 'idx_jobs_state_recipient'"
        ).fetchall()


def _old_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(_MAIN_JOBS_TABLE)
        connection.execute(
            """
            INSERT INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
                              timeout_seconds, generation, attempt, state, created_at, last_activity_at)
            VALUES ('old-job', 'old', 'owner/repo', '42', 'build', 'claude', 'comment-1',
                    5400, 1, 1, 'queued', 10.0, 10.0)
            """
        )


def test_a_fresh_database_has_the_claim_index(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    make_queue(path, [10.0])
    assert _claim_index(path) == [("index", "idx_jobs_state_recipient", "jobs")]


def test_opening_a_database_from_before_the_claim_index_adds_it_and_keeps_its_jobs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    _old_database(path)
    assert _claim_index(path) == []

    queue = make_queue(path, [20.0])

    assert _claim_index(path) == [("index", "idx_jobs_state_recipient", "jobs")]
    claimed = queue.claim("claude")
    assert claimed is not None and claimed.id == "old-job"


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "old"])
@pytest.mark.parametrize("ticket_state", [False, True], ids=["no-ticket-state", "ticket-state"])
def test_the_claim_order_plan_searches_the_claim_index(
    tmp_path: Path, fresh: bool, ticket_state: bool
) -> None:
    path = tmp_path / "queue.sqlite3"
    if not fresh:
        _old_database(path)
    queue = make_queue(path, [20.0])
    statements: list[str] = []
    with queue.connect() as connection:
        if ticket_state:
            connection.execute(
                "CREATE TABLE ticket_state (repo TEXT, ticket_id TEXT, workflow_state TEXT)"
            )
        connection.set_trace_callback(statements.append)
        queue_module.claim_order(connection, "claude", now=20.0)
        connection.set_trace_callback(None)
        (statement,) = [sql for sql in statements if "FROM jobs" in sql]
        assert ("ticket_state" in statement) is ticket_state
        plan = [str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")]
    assert "SEARCH jobs USING INDEX idx_jobs_state_recipient (state=? AND recipient=?)" in plan


def test_claim_order_reads_six_columns_into_frozen_candidates(tmp_path: Path) -> None:
    queue = make_queue(tmp_path / "queue.sqlite3", [10.0])
    queue.enqueue(request(), dedupe_key="build")
    statements: list[str] = []
    with queue.connect() as connection:
        connection.set_trace_callback(statements.append)
        candidates = queue_module.claim_order(connection, "claude", now=10.0)
        connection.set_trace_callback(None)
    (statement,) = [sql for sql in statements if "FROM jobs" in sql]
    selected = statement.split("SELECT", 1)[1].split("FROM jobs", 1)[0]
    assert "*" not in statement
    assert [column.strip() for column in selected.split(",")] == [
        "id", "repo", "ticket_id", "action", "created_at", "detail",
    ]
    assert len(candidates) == 1
    assert all(isinstance(candidate, queue_module.Candidate) for candidate in candidates)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(candidates[0], "id", "other")


def test_claim_never_reads_another_rows_bulky_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "queue.sqlite3"
    queue = make_queue(path, [10.0])
    queue.enqueue(request(), dedupe_key="queued-claude")
    bulky = [
        queue.enqueue(request(), dedupe_key=f"finished-claude-{n}").id for n in range(2)
    ] + [
        queue.enqueue(request(recipient="codex"), dedupe_key=f"queued-codex-{n}").id for n in range(2)
    ]
    with sqlite3.connect(path) as connection:
        for job_id in bulky:
            connection.execute(
                "UPDATE jobs SET detail = ? WHERE id = ?",
                (json.dumps({"blob": "x" * 200_000}), job_id),
            )
        connection.execute(
            "UPDATE jobs SET state = 'done', finished_at = 11.0 WHERE id IN (?, ?)", bulky[:2]
        )
        lengths = [
            row[0]
            for row in connection.execute(
                f"SELECT length(detail) FROM jobs WHERE id IN ({', '.join('?' * len(bulky))})", bulky
            )
        ]
    assert len(lengths) == 4 and min(lengths) > 65536

    read: list[int] = []
    connect = queue._connect

    def recording_connect() -> sqlite3.Connection:
        connection = connect()

        def text(value: bytes) -> str:
            read.append(len(value))
            return value.decode()

        connection.text_factory = text
        return connection

    monkeypatch.setattr(queue, "_connect", recording_connect)
    claimed = queue.claim("claude")
    assert claimed is not None and claimed.id not in bulky
    assert read and max(read) <= 65536


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ('{"resume": true}', True),
        ('{"resume": 1}', False),
        ('{"resume": "true"}', False),
        ("true", False),
        ("1", False),
        ('"true"', False),
        ('[{"resume": true}]', False),
        ("resume please", False),
        ("", False),
        (None, False),
    ],
)
def test_a_candidate_resumes_only_when_its_detail_says_resume_true(
    tmp_path: Path, detail: str | None, expected: bool
) -> None:
    path = tmp_path / "queue.sqlite3"
    queue = make_queue(path, [10.0])
    job = queue.enqueue(request(), dedupe_key="build")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE jobs SET detail = ? WHERE id = ?", (detail, job.id))
    with queue.connect() as connection:
        (candidate,) = queue_module.claim_order(connection, "claude", now=10.0)
    assert candidate.resume == queue_module.is_resume(detail) == expected
