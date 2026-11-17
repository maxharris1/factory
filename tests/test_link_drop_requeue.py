"""#218: a remote run lost to an SSH link drop is requeued as the same row, for free.

`orch job run` records `link-drop` after `run-died` when its follow got no
certain answer from the device for the whole grace window. T6 then requeues
the same job row (no attempt, no round) within its own budget and backoff,
after the device confirmed the worker gone, after the worker's
device-enforced deadline, or once the device is lost (#662). Real
failures on a reachable device stay failures.

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
from dataclasses import replace
from itertools import count
from pathlib import Path

import pytest

from lib import job_token, placed_run
from lib.dispatcher import CIResult
from lib.job_state import JobStateConfig, link_drop_end, link_drop_settled_at, record_job_event
from lib.queue import ClaimProofError, JobRequest, Proof
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
LOST_AFTER_A_DAY = JobStateConfig(unreachable_lost_seconds=24 * 3600.0)


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
    assert not_stuck(poller), "not stuck inside the settle time"
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
    poller = Poller(tmp_path, box=False, check=UNREACHABLE, job_states=LOST_AFTER_A_DAY)
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


def test_a_lost_device_frees_a_link_dropped_slot_at_the_limit(tmp_path: Path) -> None:
    poller, job_id = remote_review(tmp_path)
    dropped = dropped_run(poller, job_id)
    poll(poller, dropped + 1)
    poll(poller, dropped + 600)
    assert poller.row(job_id)["state"] == "running", "one second short of the limit"
    poll(poller, dropped + 601)
    assert poller.row(job_id)["state"] == "failed", "lost at the limit, not at the settle time"
    assert "device-lost" in names(poller, job_id)
    assert poller.pool.placement(job_id) is None, "the slot is freed"
    poll(poller, dropped + 601 + BACKOFF[0])
    assert poller.row(job_id)["state"] == "queued"
    assert not_stuck(poller) and wakes(poller) == []


def lost_review(poller: Poller) -> tuple[str, float]:
    """#72's review started on the pc, lost once the pc fails probes for 10 minutes."""
    job_id = poller.review(timeout=TIMEOUT)
    assert poller.claim().device == "pc", "an empty pool places off-box"
    poller.live.add(77)
    poller.pool.attach_runner(job_id, owner="codex", pid=77, start=None, alive=lambda *_: False)
    poller.pool.attach_worker(job_id, owner="codex", remote=True, tag="e" * 32)
    started = poller.now[0]
    poll(poller, started + 1)
    poll(poller, started + 601)
    assert poller.row(job_id)["state"] == "failed"
    return job_id, started + 601


def test_a_lost_job_is_requeued_as_the_same_row_after_its_backoff_on_another_device(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box=True, box_slots=1, pc=True, check=UNREACHABLE)
    job_id, lost_at = lost_review(poller)
    poll(poller, lost_at + BACKOFF[0] - 1)
    assert poller.row(job_id)["state"] == "failed", "no requeue before the backoff"
    poll(poller, lost_at + BACKOFF[0])
    assert poller.row(job_id)["state"] == "queued"
    assert details(poller, job_id, "requeued-link-drop")[-1].endswith("(link-drop requeue 1 of 6)")
    row = poller.row(job_id)
    assert (row["attempt"], row["generation"]) == (1, 1)
    assert retries(poller) == [] and not_stuck(poller)
    again = poller.claim()
    assert (again.job_id, again.device) == (job_id, "box"), "the lost device is backed off"


def test_a_late_end_from_the_lost_run_never_touches_the_next_claim(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=True, box_slots=1, pc=True, check=UNREACHABLE)
    job_id = poller.review(timeout=TIMEOUT)
    old, old_hash = job_token.mint()
    assert poller.pool.claim("codex", token_hash=old_hash)[1].device == "pc"
    lost = placed_run.prepare(
        poller.pool, poller.config, job_id=job_id, bot="codex-bot", owner="codex", worktree=False,
        log=lambda _: None, transport=poller.transport, alive=lambda *_: False,
        pid=next(RUNNERS), proof=Proof(old),
    )
    lost.before_start()
    started = poller.now[0]
    poll(poller, started + 1)
    poll(poller, started + 601)
    poll(poller, started + 601 + BACKOFF[0])
    _new, new_hash = job_token.mint()
    claimed = poller.pool.claim("codex", token_hash=new_hash)
    assert claimed is not None and claimed[0].id == job_id
    before = events(poller.path, job_id)

    lost.follow_end = FollowEnd(LOST, 255, "no certain answer")
    lost.ended(255)
    poller.transport.check = GONE
    with pytest.raises(ClaimProofError):
        lost.finish(255)

    assert events(poller.path, job_id) == before, "the old run records nothing"
    assert poller.row(job_id)["state"] == "running"
    assert poller.pool.placement(job_id) == claimed[1], "the new claim's slot is kept"


def test_a_takeover_after_a_drop_is_never_failed_on_the_old_runs_deadline(tmp_path: Path) -> None:
    poller, job_id = remote_review(tmp_path, job_states=LOST_AFTER_A_DAY)
    dropped = dropped_run(poller, job_id)
    poller.transport.check = GONE
    takeover = placed_run.prepare(
        poller.pool, poller.config, job_id=job_id, bot="codex-bot", owner="codex", worktree=False,
        log=lambda _: None, transport=poller.transport, alive=lambda *_: False,
        pid=next(RUNNERS),
    )
    takeover.before_start()
    assert takeover.target is not None
    poller.transport.check = UNREACHABLE

    poll(poller, dropped + 1)
    poll(poller, dropped + SETTLE + 1)
    poll(poller, dropped + SETTLE + BACKOFF[0] + 1)
    slot = poller.pool.placement(job_id)
    assert slot is not None and slot.worker_tag == takeover.target.tag, "the takeover's slot held"
    assert poller.row(job_id)["state"] != "queued", "never requeued beside the takeover's worker"
    assert "requeued-link-drop" not in names(poller, job_id)
    assert not any("link drop" in detail for detail in details(poller, job_id, "dead"))
    assert poller.pool.claim("codex") is None, "the row is not started a second time"


def test_a_spent_budget_frees_the_settled_slot_so_attempt_2_runs_on_a_one_slot_device(
    tmp_path: Path,
) -> None:
    poller = Poller(tmp_path, box=False, check=UNREACHABLE, job_states=LOST_AFTER_A_DAY)
    poller.config = replace(poller.config, devices=tuple(
        replace(device, slot_max=1) if device.name == "pc" else device
        for device in poller.config.devices
    ))
    poller.pool.config = poller.dispatcher.devices.config = poller.config
    first = poller.review(timeout=TIMEOUT)
    _spend_budget(poller, first)
    _settle_cycle(poller, first)
    [(second, attempt, _generation)] = retries(poller)
    assert attempt == 2
    assert poller.pool.placement(first) is None, "the settled run's slot is freed"
    assert poller.claim().job_id == second, "attempt 2 takes the only slot"


def test_a_dropped_review_waits_for_an_open_fix_for_its_head(tmp_path: Path) -> None:
    poller = Poller(tmp_path, box=True, check=UNREACHABLE)
    review = poller.review()
    poller.claim()
    dropped_run(poller, review)
    fix = poller.queue.enqueue(
        JobRequest(
            repo="maxharris1/factory", ticket_id="72", action="fix", recipient="claude",
            source_comment_id="verdict-r1", timeout_seconds=TIMEOUT, head="a" * 40,
        ),
        dedupe_key="fix-r1",
    ).id
    poller.transport.check = GONE
    failed_at = poller.now[0] + 5
    poll(poller, failed_at)
    poll(poller, failed_at + BACKOFF[0])
    assert poller.row(review)["state"] == "failed", "held while the fix is open"
    assert poller.row(fix)["state"] == "queued"
    assert "retry-deferred" in names(poller, review)
    assert "requeued-link-drop" not in names(poller, review)
    assert poller.pool.claim("codex") is None, "the review is not claimable beside the fix"


@pytest.mark.parametrize("spent", [False, True], ids=["free-requeues-left", "budget-spent"])
def test_a_review_held_behind_an_open_fix_frees_its_settled_slot_for_the_fix(
    tmp_path: Path, spent: bool
) -> None:
    poller = Poller(tmp_path, box=False, check=UNREACHABLE, job_states=LOST_AFTER_A_DAY)
    poller.config = replace(poller.config, devices=tuple(
        replace(device, slot_max=1) if device.name == "pc" else device
        for device in poller.config.devices
    ))
    poller.pool.config = poller.dispatcher.devices.config = poller.config
    review = poller.review(timeout=TIMEOUT)
    if spent:
        _spend_budget(poller, review)
    poller.claim()
    dropped = dropped_run(poller, review)
    fix = poller.queue.enqueue(
        JobRequest(
            repo="maxharris1/factory", ticket_id="72", action="fix", recipient="claude",
            source_comment_id="verdict-r1", timeout_seconds=TIMEOUT, head="a" * 40,
        ),
        dedupe_key="fix-r1",
    ).id
    poll(poller, dropped + SETTLE)
    poll(poller, dropped + SETTLE + 60)
    assert poller.row(review)["state"] == "failed", "held while the fix is open"
    assert "retry-deferred" in names(poller, review)
    assert poller.pool.placement(review) is None, "the settled run's slot is freed"
    claimed = poller.pool.claim("claude")
    assert claimed is not None and claimed[0].id == fix, "the fix takes the only slot"


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
