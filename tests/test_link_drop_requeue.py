"""#218: a remote run lost to an SSH link drop is requeued as the same row, for free.

`orch job run` records `link-drop` after `run-died` when its follow got no
certain answer from the device for the whole grace window. T6 then requeues
the same job row (no attempt, no round) within its own budget and backoff,
after the device confirmed the worker gone or after the worker's
device-enforced deadline. Real failures on a reachable device stay failures.

End-to-end legs run the real `orch job run` over tests/fake_ssh.py; the rest
drive tests/test_job_state.py's Poller on a fake clock, with FakeTransport as
the device's answers. Nothing reaches a real host or the live orch.db.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
from itertools import count
from pathlib import Path

from lib import placed_run
from lib.dispatcher import CIResult
from lib.job_state import link_drop_end, link_drop_settled_at, record_job_event
from lib.queue import JobRequest
from lib.remote_follow import LOST, FollowEnd
from tests.test_detached_remote_cli import (
    CODEX,
    device_pids,
    events,
    harness,
    run,
    setup,
    started_harness,
)
from tests.test_dispatcher import READY, VERDICT, comment, seed_builder_job, ticket
from tests.test_job_state import Poller
from tests.test_remote_start import gone, kill_quietly, wait_gone


HEAD = "f218" + "0" * 36
NEW_HEAD = "f218" + "1" * 36
BACKOFF = (60, 120, 300, 600, 600, 600)
TIMEOUT = 3600
SETTLE = 10 * 60 + TIMEOUT + 5 * 60
UNREACHABLE, RUNNING, GONE = 255, 1, 0
RUNNERS = count(900_001)


def dropped_run(poller: Poller, job_id: str, *, bot: str = "codex-bot", owner: str = "codex") -> float:
    """One `orch job run` on the pc whose follow lost the device; when it ended."""
    placed = placed_run.prepare(
        poller.pool, poller.config, job_id=job_id, bot=bot, owner=owner, worktree=False,
        log=lambda _: None, transport=poller.transport, alive=lambda *_: False,
        pid=next(RUNNERS),
    )
    assert placed.target is not None, "placed on a remote device"
    placed.before_start()
    placed.follow_end = FollowEnd(LOST, 255, "no certain answer")
    placed.ended(255)
    return poller.now[0]


def poll(poller: Poller, at: float) -> None:
    poller.now[0] = at
    poller.dispatcher.reconcile_jobs()


def fake_clock(poller: Poller, at: float) -> None:
    poller.now[0] = at
    poller.queue.clock = lambda: poller.now[0]


def names(poller: Poller, job_id: str) -> list[str]:
    return [event for event, _detail in events(poller.path, job_id)]


def details(poller: Poller, job_id: str, event: str) -> list[str]:
    return [detail for name, detail in events(poller.path, job_id) if name == event]


def stuck(poller: Poller, repo: str, ticket_id: str) -> tuple[int, str | None] | None:
    with sqlite3.connect(poller.path) as connection:
        row = connection.execute(
            "SELECT stuck, stuck_reason FROM ticket_state WHERE repo = ? AND ticket_id = ?",
            (repo.lower(), ticket_id),
        ).fetchone()
    return None if row is None else (int(row[0]), row[1])


def not_stuck(poller: Poller, repo: str = "maxharris1/factory", ticket_id: str = "72") -> bool:
    state = stuck(poller, repo, ticket_id)
    return state is None or state[0] == 0


def wakes(poller: Poller) -> list[str]:
    with sqlite3.connect(poller.path) as connection:
        return [
            str(row[0])
            for row in connection.execute("SELECT detail FROM jobs WHERE action = 'stuck'")
        ]


def retries(poller: Poller) -> list[tuple[str, int, int]]:
    with sqlite3.connect(poller.path) as connection:
        return connection.execute(
            "SELECT id, attempt, generation FROM jobs "
            "WHERE dedupe_key LIKE 't6-retry:%' ORDER BY created_at"
        ).fetchall()


def remote_review(tmp_path: Path, **kwargs) -> tuple[Poller, str]:
    poller = Poller(tmp_path, box=False, check=UNREACHABLE, **kwargs)
    job_id = poller.review(timeout=TIMEOUT)
    assert poller.claim().device == "pc"
    return poller, job_id


def confirm_gone_and_requeue(poller: Poller, job_id: str, backoff: int) -> None:
    """The device answers, the worker is gone: dead, then the requeue after its backoff."""
    poller.transport.check = GONE
    failed_at = poller.now[0] + 5
    poll(poller, failed_at)
    assert poller.row(job_id)["state"] == "failed", "dead once the device confirms the worker gone"
    poll(poller, failed_at + backoff - 1)
    assert poller.row(job_id)["state"] == "failed", "no requeue before the backoff"
    poll(poller, failed_at + backoff)
    assert poller.row(job_id)["state"] == "queued", "requeued once the backoff passed"
    poller.transport.check = UNREACHABLE


def test_link_drop_end_is_the_latest_runs_drop_and_never_an_older_one(tmp_path: Path) -> None:
    poller = Poller(tmp_path)
    job_id = poller.review()

    def end() -> tuple[str, float] | None:
        with poller.queue.connect() as connection:
            return link_drop_end(connection, job_id)

    def write(event: str, detail: str, at: float) -> None:
        with poller.queue.connect() as connection:
            record_job_event(connection, job_id, event, detail, at)
            connection.commit()

    assert end() is None, "no run ended yet"
    write("run-died", "lost contact with pc", 10.0)
    assert end() is None, "a run-died alone is not a link drop"
    write("link-drop", "pc: no certain answer for 30 minutes", 10.0)
    assert end() == ("pc: no certain answer for 30 minutes", 10.0)
    write("requeued-link-drop", "requeued (link-drop requeue 1 of 6)", 20.0)
    assert end() is None, "an older run's drop never counts after its requeue"
    write("run-died", "orch job run's command on pc: exit 3", 30.0)
    assert end() is None, "a later run that died for real is not a link drop"
    write("link-drop", "pc: no certain answer for 2 minutes", 40.0)
    assert end() == ("pc: no certain answer for 2 minutes", 40.0)
    write("run-exited", "orch job run's command on pc: exit 0", 50.0)
    assert end() is None, "a later run that exited is not a link drop"


def test_the_settle_time_covers_the_launch_ttl_the_timeout_and_a_margin() -> None:
    assert link_drop_settled_at(1000.0, TIMEOUT) == 1000.0 + SETTLE


def test_a_dropped_link_requeues_the_same_row_for_free(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    before = poller.row(job_id)
    count_file = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--follow-grace", "2", "--", *harness(count_file, 600, 0.1))
    worker = None
    try:
        worker = started_harness(count_file, proc)
        ssh.offline(CODEX).touch()
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 255, err
        assert names(poller, job_id) == ["run-died", "link-drop"], "the run end names the drop"
        [drop] = details(poller, job_id, "link-drop")
        assert drop.startswith("pc: "), drop
        assert poller.pool.placement(job_id) is not None, "slot held: the worker may still run"
        assert poller.row(job_id)["state"] == "running"

        fake_clock(poller, time.time())
        poller.transport.check = UNREACHABLE
        poll(poller, poller.now[0] + 10)
        poller.transport.check = RUNNING
        poll(poller, poller.now[0] + 10)
        assert poller.row(job_id)["state"] == "running", "never requeued beside a live worker"
        assert "requeued-link-drop" not in names(poller, job_id)

        ssh.offline(CODEX).unlink()
        kill_quietly(worker)
        assert wait_gone(worker)
        poller.transport.check = GONE
        failed_at = poller.now[0] + 10
        poll(poller, failed_at)
        assert poller.row(job_id)["state"] == "failed", "dead once the device confirms it gone"
        assert names(poller, job_id)[-2:] == ["dead", "requeue-deferred"]

        poll(poller, failed_at + BACKOFF[0])
    finally:
        kill_quietly(worker)
        if proc.poll() is None:
            proc.kill()
    row = poller.row(job_id)
    assert poller.jobs() == [(job_id, "queued", 1)], "the same row, queued, no new job"
    for field in ("attempt", "generation", "head", "detail", "action"):
        assert row[field] == before[field], field
    assert poller.pool.placement(job_id) is None, "slot freed by the requeue"
    [requeued] = details(poller, job_id, "requeued-link-drop")
    assert requeued.endswith("(link-drop requeue 1 of 6)"), requeued
    assert retries(poller) == [], "no T6 retry row"
    assert not_stuck(poller) and wakes(poller) == []
    assert poller.claim().job_id == job_id, "claimable again as the same job"


def test_a_dropped_build_whose_ready_landed_is_done_not_requeued(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=False, check=UNREACHABLE)
    build = poller.queue.enqueue(
        JobRequest(
            repo="owner/repo", ticket_id="42", action="build", recipient="claude",
            source_comment_id="go-1", timeout_seconds=TIMEOUT,
        ),
        dedupe_key="go-1",
    ).id
    claimed = poller.pool.claim("claude")
    assert claimed is not None and claimed[1].device == "pc"
    dropped = dropped_run(poller, build, bot="claude-bot", owner="claude")
    ready_id = f"ready-{NEW_HEAD[:6]}"
    ready = comment(ready_id, READY.format(head=NEW_HEAD), author="claude-login", created_at=dropped + 5)
    poller.now[0] = dropped + 10
    poller.dispatcher.process(ticket(ready, head=NEW_HEAD, ci={NEW_HEAD: CIResult("pending")}))

    poller.transport.check = GONE
    poll(poller, dropped + 20)
    poll(poller, dropped + 20 + BACKOFF[0])
    row = poller.row(build)
    assert (row["state"], row["handoff_comment_id"]) == ("done", ready_id)
    assert names(poller, build)[:2] == ["run-died", "link-drop"], "a link-dropped run"
    assert "done-from-ready" in names(poller, build)
    assert "requeued-link-drop" not in names(poller, build)
    assert "requeue-deferred" not in names(poller, build)
    assert retries(poller) == []


def test_a_dropped_review_whose_verdict_landed_is_done_not_requeued(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=False, check=UNREACHABLE)
    seed_builder_job(poller.queue)
    start = poller.now[0]
    ready = comment(f"ready-{HEAD}", READY.format(head=HEAD), author="claude-login", created_at=start)
    poller.dispatcher.process(ticket(ready, head=HEAD, ci={HEAD: CIResult("success")}))
    poller.now[0] = start + 10
    claimed = poller.pool.claim("codex")
    assert claimed is not None
    review, placement = claimed
    assert review.request.action == "review" and placement.device == "pc"
    dropped = dropped_run(poller, review.id)
    verdict = comment(
        "verdict-r1",
        VERDICT.format(round="1/3", head=HEAD[:7], verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=dropped + 5,
    )
    poller.now[0] = dropped + 10
    poller.dispatcher.process(ticket(ready, verdict, head=HEAD, ci={HEAD: CIResult("success")}))

    poller.transport.check = GONE
    poll(poller, dropped + 20)
    poll(poller, dropped + 20 + BACKOFF[0])
    row = poller.row(review.id)
    assert (row["state"], row["handoff_comment_id"]) == ("done", "verdict-r1")
    assert names(poller, review.id)[:2] == ["run-died", "link-drop"], "a link-dropped run"
    assert "done-from-verdict" in names(poller, review.id)
    assert "requeued-link-drop" not in names(poller, review.id)
    assert "requeue-deferred" not in names(poller, review.id)
    assert retries(poller) == []


def test_two_link_drops_in_a_row_requeue_twice_and_never_stick(tmp_path: Path) -> None:
    poller, job_id = remote_review(tmp_path)
    dropped_run(poller, job_id)
    poll(poller, poller.now[0] + 5)
    assert poller.row(job_id)["state"] == "running", "unreachable: left running"
    confirm_gone_and_requeue(poller, job_id, BACKOFF[0])

    assert poller.claim().job_id == job_id
    dropped_run(poller, job_id)
    confirm_gone_and_requeue(poller, job_id, BACKOFF[1])

    requeues = details(poller, job_id, "requeued-link-drop")
    assert [detail.rsplit("(", 1)[1] for detail in requeues] == [
        "link-drop requeue 1 of 6)", "link-drop requeue 2 of 6)"
    ]
    row = poller.row(job_id)
    assert poller.jobs() == [(job_id, "queued", 1)], "one row, attempt 1, no retry"
    assert (row["attempt"], row["generation"]) == (1, 1)
    assert retries(poller) == []
    assert not_stuck(poller) and wakes(poller) == []


def _settle_cycle(poller: Poller, job_id: str) -> float:
    """Claim, drop, and wait out the settle time with the device unreachable throughout.

    Returns when the job was failed (dead).
    """
    assert poller.claim().job_id == job_id, "the same row is claimed again"
    dropped = dropped_run(poller, job_id)
    poll(poller, dropped + 1)
    poll(poller, dropped + 61 * 60)
    assert poller.row(job_id)["state"] == "running", "offline an hour, inside the settle time"
    assert not_stuck(poller), "not stuck device-offline inside the settle time"
    poll(poller, dropped + SETTLE - 1)
    assert poller.row(job_id)["state"] == "running", "one second short of the settle time"
    poll(poller, dropped + SETTLE)
    assert poller.row(job_id)["state"] == "failed", "dead once its worker is past its deadline"
    assert "link drop" in details(poller, job_id, "dead")[-1]
    return dropped + SETTLE


def _spend_budget(poller: Poller, job_id: str) -> None:
    for used, backoff in enumerate(BACKOFF):
        failed_at = _settle_cycle(poller, job_id)
        poll(poller, failed_at + backoff - 1)
        assert poller.row(job_id)["state"] == "failed", f"requeue {used + 1} waits its backoff"
        assert int(poller.row(job_id)["failure_handled"]) == 0, "deferred, not handled"
        poll(poller, failed_at + backoff)
        assert poller.row(job_id)["state"] == "queued", f"requeue {used + 1} after {backoff} s"
        assert details(poller, job_id, "requeued-link-drop")[-1].endswith(
            f"(link-drop requeue {used + 1} of 6)"
        )
        assert not_stuck(poller)
    assert names(poller, job_id).count("requeue-deferred") == 1, "recorded once per row"


def test_a_device_that_stays_dead_spends_the_budget_with_backoff_then_sticks_with_link_drop(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box=False, check=UNREACHABLE)
    first = poller.review(timeout=TIMEOUT)
    _spend_budget(poller, first)

    _settle_cycle(poller, first)
    assert "link-drop-budget-spent" in names(poller, first)
    assert "requeued-link-drop" not in names(poller, first)[-3:], "no 7th free requeue"
    [(second, attempt, generation)] = retries(poller)
    assert (attempt, generation) == (2, 2), "the budget spent: today's T6 retry"
    assert poller.row(second)["state"] == "queued"
    assert not_stuck(poller)

    _spend_budget(poller, second)
    _settle_cycle(poller, second)
    assert "link-drop-budget-spent" in names(poller, second)
    assert stuck(poller, "maxharris1/factory", "72") == (1, "job-failed-2")
    [wake] = wakes(poller)
    assert "link-drop" in json.loads(wake)["detail"], wake
    assert len(retries(poller)) == 1, "never a third attempt"


def test_device_offline_waits_for_the_link_drop_settle_time(tmp_path: Path) -> None:
    poller, job_id = remote_review(tmp_path)
    dropped = dropped_run(poller, job_id)
    poll(poller, dropped + 1)
    poll(poller, dropped + 61 * 60)
    assert poller.row(job_id)["state"] == "running"
    assert not_stuck(poller), "no device-offline stuck before the settle time"
    poll(poller, dropped + SETTLE)
    assert not_stuck(poller), "past the settle time it is dead, not stuck device-offline"
    assert "link drop" in details(poller, job_id, "dead")[-1]
    poll(poller, dropped + SETTLE + BACKOFF[0])
    assert poller.row(job_id)["state"] == "queued"
    assert not_stuck(poller) and wakes(poller) == []

    # Without a link-drop end, an hour offline is still today's device-offline stuck.
    other = Poller(tmp_path / "plain", box=False, check=UNREACHABLE)
    plain = other.review(timeout=TIMEOUT)
    assert other.claim().device == "pc"
    other.pool.attach_runner(plain, owner="codex", pid=next(RUNNERS), start=None, alive=lambda *_: False)
    other.pool.attach_worker(plain, owner="codex", remote=True, tag="c" * 32)
    started = other.now[0]
    poll(other, started + 1)
    poll(other, started + 61 * 60)
    assert stuck(other, "maxharris1/factory", "72") == (1, "device-offline")


def test_a_link_drop_requeue_prefers_another_device_with_a_free_slot(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=True, box_slots=1, pc=True, check=UNREACHABLE)
    job_id = poller.review(timeout=TIMEOUT)
    assert poller.claim().device == "pc", "an empty pool places off-box"
    dropped_run(poller, job_id)
    confirm_gone_and_requeue(poller, job_id, BACKOFF[0])
    again = poller.claim()
    assert (again.job_id, again.device) == (job_id, "box"), "moved off the device it dropped on"


def test_it_falls_back_to_the_same_device_when_it_is_the_only_one_with_room(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box=True, box_slots=1, pc=True, check=UNREACHABLE)
    job_id = poller.review(timeout=TIMEOUT)
    assert poller.claim().device == "pc"
    other = poller.queue.enqueue(
        JobRequest(
            repo="maxharris1/factory", ticket_id="80", action="build", recipient="codex",
            source_comment_id="go-80", timeout_seconds=TIMEOUT,
        ),
        dedupe_key="go-80",
    ).id
    filler = poller.claim()
    assert (filler.job_id, filler.device) == (other, "box"), "the box is now full"
    poller.live.add(77)
    poller.pool.attach_runner(other, owner="codex", pid=77, start=None, alive=lambda *_: False)
    dropped_run(poller, job_id)
    confirm_gone_and_requeue(poller, job_id, BACKOFF[0])
    again = poller.claim()
    assert (again.job_id, again.device) == (job_id, "pc"), "the dropped device is the only one with room"


def _reachable_failure(tmp_path: Path, code: int) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    proc = run(poller, job_id, ssh, "--", sys.executable, "-c", f"import sys; sys.exit({code})")
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == code, err
    assert events(db, job_id) == [("run-died", f"orch job run's command on pc: exit {code}")]
    assert poller.pool.placement(job_id) is None, "the device answered: slot freed"

    last = float(poller.row(job_id)["last_activity_at"])
    fake_clock(poller, last + 121)
    poller.dispatcher.reconcile_jobs()
    assert poller.row(job_id)["state"] == "failed"
    [(retry, attempt, generation)] = retries(poller)
    assert (attempt, generation) == (2, 2), "today's T6 retry, attempt 2"
    assert poller.row(retry)["state"] == "queued"
    assert "requeued-link-drop" not in names(poller, job_id)
    assert "link-drop" not in names(poller, job_id)


def test_a_reachable_harness_exiting_3_is_a_plain_failure(tmp_path: Path) -> None:
    _reachable_failure(tmp_path, 3)


def test_a_reachable_harness_exiting_255_is_a_plain_failure(tmp_path: Path) -> None:
    _reachable_failure(tmp_path, 255)


def test_a_worker_killed_on_a_reachable_device_is_not_a_link_drop(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    count_file = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--", *harness(count_file, 600, 0.1))
    try:
        worker = started_harness(count_file, proc)
        wrapper, start = device_pids(tmp_path, "codex-bot", job_id)
        os.kill(wrapper, signal.SIGKILL)
        os.killpg(start, signal.SIGKILL)
        _, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 255, err
    assert gone(worker)
    assert names(poller, job_id) == ["run-died"], "worker gone on a reachable device: no link-drop"
