"""#87 r3 F2.1: a timed-out remote job never gets attempt 2 beside its worker.

The timeout sweep (Dispatcher.reconcile_jobs -> queue.mark_timed_out) used to
fail any running job whose heartbeat had stopped for its timeout, and T6 then
queued attempt 2 while attempt 1's remote worker still ran. Now a running job
whose slot records a remote start is never timed out: it leaves running only
through the dead path, once its runner is gone and the device confirms its
worker gone (then T6 retries as before), or as lost once the device stays
unreachable for 10 minutes (#662; T6 then requeues the same row). All on a
fake clock and a scripted SSH transport.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_job_state import CLAIMED_AT, Poller

TIMEOUT = 3600
TAG = "b" * 32
GONE, RUNNING, UNREACHABLE = 0, 1, 255


def remote_job(poller: Poller, *, runner_alive: bool = False) -> str:
    """Attempt 1 of #72's review, started on the PC; heartbeats from t=30s."""
    job_id = poller.review(timeout=TIMEOUT)
    placed = poller.claim()
    assert placed.device == "pc"
    if runner_alive:
        poller.live.add(11)
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start="r", alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", remote=True, tag=TAG)
    poller.now[0] = CLAIMED_AT + 30
    poller.queue.heartbeat(job_id, owner="codex")
    return job_id


def held_events(poller: Poller, job_id: str) -> list[str]:
    with poller.queue.connect() as connection:
        return [
            str(row["detail"])
            for row in connection.execute(
                "SELECT detail FROM job_events WHERE job_id = ? AND event = 'timeout-held'",
                (job_id,),
            )
        ]


def review_jobs(poller: Poller) -> list[tuple[str, str, int]]:
    """The review jobs, without the Grok wakes a stuck flag queues."""
    with poller.queue.connect() as connection:
        return [
            (str(row["id"]), str(row["state"]), int(row["attempt"]))
            for row in connection.execute(
                "SELECT * FROM jobs WHERE action = 'review' ORDER BY created_at, id"
            )
        ]


def assert_no_attempt_2(poller: Poller, job_id: str) -> None:
    assert review_jobs(poller) == [(job_id, "running", 1)]
    with poller.queue.connect() as connection:
        handled = connection.execute(
            "SELECT failure_handled FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert handled == 0
    # A free PC slot (3 slots, 1 used) and a free box slot: nothing to claim.
    assert poller.pool.used() == {"pc": 1}
    assert poller.pool.claim("codex") is None
    assert poller.queue.claim("codex") is None


def test_a_live_remote_worker_past_its_timeout_gets_no_attempt_2(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box_slots=2, check=RUNNING)
    job_id = remote_job(poller)  # its runner on the box is gone

    poller.at(30 + TIMEOUT - 1)
    assert_no_attempt_2(poller, job_id)
    assert held_events(poller, job_id) == []

    # Heartbeats stopped an hour ago; the device says the worker still runs.
    poller.at(30 + TIMEOUT)
    assert_no_attempt_2(poller, job_id)
    (detail,) = held_events(poller, job_id)
    assert "60-minute timeout" in detail and "pc" in detail and "no retry" in detail
    assert any("timeout-held" in line for line in poller.lines)

    # Hours later, still alive: still one attempt, and the hold is noted once.
    for hours in (2, 3, 6):
        poller.at(hours * 3600)
        assert_no_attempt_2(poller, job_id)
    assert len(held_events(poller, job_id)) == 1
    assert poller.events().count((job_id, "timeout-held")) == 1


def test_a_live_runner_with_a_remote_worker_is_not_timed_out_either(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box_slots=2, check=RUNNING)
    job_id = remote_job(poller, runner_alive=True)
    poller.at(30 + TIMEOUT + 600)
    assert_no_attempt_2(poller, job_id)
    assert len(held_events(poller, job_id)) == 1


def test_once_the_worker_is_confirmed_gone_the_t6_retry_still_comes(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box_slots=2, check=RUNNING)
    job_id = remote_job(poller)
    poller.at(30 + TIMEOUT + 60)
    assert_no_attempt_2(poller, job_id)

    poller.transport.check = GONE  # the device confirms the worker gone
    poller.at(30 + TIMEOUT + 120)
    assert poller.row(job_id)["state"] == "failed"
    assert poller.events() == [(job_id, "timeout-held"), (job_id, "dead")]
    # T6 handled it like any failed job: exactly one retry, attempt 2.
    assert [(state, attempt) for _id, state, attempt in review_jobs(poller)] == [
        ("failed", 1),
        ("queued", 2),
    ]
    assert int(poller.row(job_id)["failure_handled"]) == 1
    claimed = poller.pool.claim("codex")
    assert claimed is not None
    retry, _placement = claimed
    assert retry.id != job_id
    assert int(poller.row(retry.id)["attempt"]) == 2


def test_an_unreachable_device_loses_the_job_and_requeues_it_without_a_retry(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box_slots=2, check=UNREACHABLE)
    job_id = remote_job(poller)
    poller.at(60)  # first failed probe starts the device's offline clock
    poller.at(60 + 599)
    assert_no_attempt_2(poller, job_id)

    poller.at(60 + 600)
    assert poller.row(job_id)["state"] == "failed"
    assert int(poller.row(job_id)["failure_handled"]) == 0
    assert poller.pool.placement(job_id) is None
    poller.at(60 + 600 + 60)
    assert review_jobs(poller) == [(job_id, "queued", 1)]


def test_a_box_job_keeps_the_plain_timeout(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box_slots=3, pc=False)  # pc off: the box
    job_id = poller.review(timeout=TIMEOUT)
    assert poller.claim().device == "box"
    # Started (#91: a placed job with no worker never started), its runner
    # and worker still live: nothing but the timeout acts on it.
    poller.live.update((11, 22))
    poller.pool.attach_runner(job_id, owner="codex", pid=11, start="r", alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", pid=22, start="w")
    poller.now[0] = CLAIMED_AT + 30
    poller.queue.heartbeat(job_id, owner="codex")
    poller.at(30 + TIMEOUT)
    assert [(state, attempt) for _id, state, attempt in review_jobs(poller)] == [
        ("failed", 1),
        ("queued", 2),
    ]
    assert held_events(poller, job_id) == []
    with poller.queue.connect() as connection:
        events = [str(row[0]) for row in connection.execute("SELECT event FROM job_events")]
    assert "dead" not in events


def test_mark_timed_out_refuses_a_job_with_a_remote_start(tmp_path: Path) -> None:
    poller = Poller(tmp_path, check=RUNNING)
    job_id = remote_job(poller)
    (job,) = [job for job in poller.queue.outstanding() if job.id == job_id]
    poller.now[0] = CLAIMED_AT + 30 + TIMEOUT
    assert poller.queue.mark_timed_out(job) is False
    assert poller.row(job_id)["state"] == "running"


def test_mark_timed_out_works_on_a_database_with_no_slot_table(tmp_path: Path) -> None:
    now = [100.0]
    queue = SQLiteJobQueue(tmp_path / "q.db", clock=lambda: now[0])
    queue.enqueue(
        JobRequest(
            repo="maxharris1/factory",
            ticket_id="72",
            action="review",
            recipient="codex",
            source_comment_id="ready-1",
            timeout_seconds=60,
            head="a" * 40,
        ),
        dedupe_key="review:72:r1",
    )
    running = queue.claim("codex")
    assert running is not None
    with sqlite3.connect(tmp_path / "q.db") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'device_slots'"
            ).fetchone()
            is None
        )
    now[0] = 200.0
    assert queue.mark_timed_out(running) is True
