"""Exactly one verdict or handoff per job (#80, Max via Factory on PR #82).

Once an autostart waiter has claimed a job, its harness run is the only
thing that runs or finishes that job:

* a second claim is refused: a plain `orch wait` for the bot is refused
  while the bot's autostart waiter runs, and a running job is never handed
  out again;
* a second `orch job run` of a running job is refused, and a bot turn's
  `orch job run` of an autostart job is refused, nothing started;
* `orch job done` is idempotent (the same outcome again is a no-op success),
  a conflicting second call is refused without changing state, and a bot
  turn outside the autostart run cannot finish the job at all;
* a duplicate verdict comment at the same head triggers no second action:
  no second job, no second ship, no wake.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import waiter as waiter_module
from lib.dispatcher import CIResult
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    VERDICT,
    FakeReviewActions,
    comment,
    seed_review_job,
    setup,
    ticket,
)
from tests.test_waiter import (  # noqa: F401  (autostart is a fixture)
    MACHINES,
    ORCH,
    _running,
    _wait_for,
    autostart,
    enqueue,
    local_pool,
    make_queue,
    state,
)


def orch(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("ORCH_DB", "ORCH_AUTOSTART")}
    return subprocess.run(
        [sys.executable, str(ORCH), *args],
        env=env, cwd=cwd, text=True, capture_output=True, timeout=60, check=False,
    )


def job_row(db: Path, job_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def claimed(tmp_path: Path) -> tuple[Path, SQLiteJobQueue, str]:
    """A job a plain (non-autostart) claim took through the device pool."""
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    taken = local_pool(queue).claim("claude")
    assert taken is not None and taken[0].id == job_id
    return db, queue, job_id


# --- a second claim is refused -----------------------------------------------


def test_plain_wait_is_refused_while_the_bots_autostart_waiter_runs(autostart: dict) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    lock = waiter_module.lock_path(db, "claude-bot")
    _wait_for(lambda: lock.exists() and lock.read_text().strip() == str(proc.pid), 20, proc)
    queued = enqueue(queue, "plain", recipient="claude", head=None)
    # Let the autostart waiter take it first, then a bot turn tries to claim.
    _wait_for(lambda: _running(db, queued), 20, proc)
    result = orch("wait", "claude-bot", "--db", str(db), "--machines", str(MACHINES))
    assert result.returncode == 2
    assert f"claude-bot's autostart waiter (pid {proc.pid})" in result.stderr
    assert "second claim is refused" in result.stderr
    assert result.stdout == ""


def test_a_running_job_is_never_claimed_again(tmp_path: Path) -> None:
    db, queue, job_id = claimed(tmp_path)
    assert local_pool(queue).claim("claude") is None
    assert queue.claim("claude") is None
    assert state(db, job_id) == "running"


def test_an_autostart_waiter_will_not_start_beside_a_plain_wait(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    plain = waiter_module.share_waiter_lock(db, "claude-bot")
    try:
        with pytest.raises(waiter_module.WaiterError, match="a plain orch wait for claude-bot"):
            waiter_module.hold_waiter_lock(db, "claude-bot")
    finally:
        os.close(plain)


# --- a second run is refused ---------------------------------------------------


def test_a_bot_turn_cannot_run_a_job_the_autostart_waiter_claimed(
    autostart: dict, tmp_path: Path
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    marker = tmp_path / "bot-turn-ran"
    result = orch("job", "run", "claude-bot", job_id, "--db", str(db),
                  "--machines", str(MACHINES), "--", "touch", str(marker))
    assert result.returncode == 2
    assert "was claimed by claude-bot's autostart waiter" in result.stderr
    assert "nothing was started" in result.stderr
    time.sleep(0.3)
    assert not marker.exists()
    assert state(db, job_id) == "running"
    # The harness run still finishes it, once.
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20, proc)


def test_a_second_run_of_a_running_job_is_refused(tmp_path: Path) -> None:
    db, _queue, job_id = claimed(tmp_path)
    first = subprocess.Popen(["sleep", "30"])
    try:
        from lib.worktrees import process_start

        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE device_slots SET runner_pid = ?, runner_start = ? WHERE job_id = ?",
                (first.pid, process_start(first.pid), job_id),
            )
        marker = tmp_path / "second-ran"
        result = orch("job", "run", "claude-bot", job_id, "--db", str(db),
                      "--machines", str(MACHINES), "--", "touch", str(marker))
        assert result.returncode == 2
        assert f"already run by pid {first.pid}" in result.stderr
        assert "second run of a running job is refused" in result.stderr
        assert not marker.exists()
        assert state(db, job_id) == "running"
    finally:
        first.kill()
        first.wait()


# --- orch job done is idempotent, and only the job's run may call it ----------


def test_job_done_with_the_same_handoff_again_is_a_no_op_success(tmp_path: Path) -> None:
    db, _queue, job_id = claimed(tmp_path)
    first = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-1")
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == f"done {job_id}"
    again = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-1")
    assert again.returncode == 0, again.stderr
    assert again.stdout.startswith(f"done {job_id}")
    assert "nothing changed" in again.stdout
    assert job_row(db, job_id) == ("done", "c-1")


@pytest.mark.parametrize("second", [["--handoff", "c-2"], ["--failed"]])
def test_a_conflicting_second_job_done_is_refused_and_changes_nothing(
    tmp_path: Path, second: list[str]
) -> None:
    db, _queue, job_id = claimed(tmp_path)
    assert orch("job", "done", "claude-bot", job_id, "--db", str(db),
                "--handoff", "c-1").returncode == 0
    with sqlite3.connect(db) as connection:
        before = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *second)
    assert result.returncode == 1
    assert f"job {job_id} is already done with handoff c-1" in result.stderr
    assert "nothing changed" in result.stderr
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone() == before


def test_a_repeated_failed_is_a_no_op_and_a_handoff_after_it_is_refused(tmp_path: Path) -> None:
    db, _queue, job_id = claimed(tmp_path)
    assert orch("job", "done", "claude-bot", job_id, "--db", str(db), "--failed").returncode == 0
    again = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--failed")
    assert again.returncode == 0 and "nothing changed" in again.stdout
    late = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-9")
    assert late.returncode == 1
    assert "already failed" in late.stderr
    assert job_row(db, job_id) == ("failed", None)


@pytest.mark.parametrize("outcome", [["--handoff", "c-bot-turn"], ["--failed"]])
def test_a_bot_turn_cannot_finish_a_job_the_autostart_waiter_runs(
    autostart: dict, outcome: list[str]
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    result = orch("job", "done", "claude-bot", job_id, "--db", str(db), *outcome)
    assert result.returncode == 2
    assert "claimed by its autostart waiter" in result.stderr
    assert "Nothing changed" in result.stderr
    assert job_row(db, job_id) == ("running", None)
    # The harness inside the run finishes it with its own handoff.
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20, proc)
    assert job_row(db, job_id) == ("done", "c-" + job_id[:6])
    # Afterwards the same handoff again is a no-op; a bot turn's is refused.
    same = orch("job", "done", "claude-bot", job_id, "--db", str(db),
                "--handoff", "c-" + job_id[:6])
    assert same.returncode == 0 and "nothing changed" in same.stdout
    other = orch("job", "done", "claude-bot", job_id, "--db", str(db), "--handoff", "c-other")
    assert other.returncode == 1
    assert job_row(db, job_id) == ("done", "c-" + job_id[:6])


def test_descends_from_walks_the_parent_chain() -> None:
    from lib.worktrees import descends_from

    parents = {50: 40, 40: 30, 30: 1}
    assert descends_from(50, 30, parent=parents.get)
    assert descends_from(50, 50, parent=parents.get)
    assert not descends_from(50, 99, parent=parents.get)
    assert not descends_from(50, 30, parent=lambda _pid: None)
    assert descends_from(os.getpid(), os.getppid())


# --- a duplicate verdict at the same head triggers no second action -----------


def _jobs(queue: SQLiteJobQueue) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT action, recipient, dedupe_key FROM jobs ORDER BY created_at, rowid"
            )
        ]


def _outcomes(queue: SQLiteJobQueue) -> dict[str, str]:
    with queue.connect() as connection:
        return dict(
            connection.execute("SELECT comment_id, outcome FROM handled_comments").fetchall()
        )


def test_a_duplicate_revise_at_the_same_head_starts_no_second_job(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    revise = VERDICT.format(round="1/3", head=head, verdict="REVISE", risk="high")
    first = comment("v-1", revise, author="codex-login", created_at=1)
    dispatcher.process(ticket(first, head=head))
    after_first = _jobs(queue)
    assert [job[0] for job in after_first].count("fix") == 1

    duplicate = comment("v-2", revise, author="codex-login", created_at=2)
    dispatcher.process(ticket(first, duplicate, head=head))
    dispatcher.process(ticket(first, duplicate, head=head))  # and on a later poll

    assert _jobs(queue) == after_first  # no second fix, no Grok wake either
    assert _outcomes(queue)["v-2"] == "duplicate-verdict"


def test_a_duplicate_approve_at_the_same_head_does_not_ship_twice(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "b" * 40
    seed_review_job(dispatcher, queue, head)
    approve = VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high")
    green = {head: CIResult("success")}
    first = comment("a-1", approve, author="codex-login", created_at=1)
    duplicate = comment("a-2", approve, author="codex-login", created_at=2)

    dispatcher.process(ticket(first, duplicate, head=head, ci=green))
    jobs = _jobs(queue)
    dispatcher.process(ticket(first, duplicate, head=head, ci=green))

    assert actions.merges == [(True, head)]
    assert _jobs(queue) == jobs
    assert [job for job in jobs if job[1] != "grok"] == [
        job for job in _jobs(queue) if job[1] != "grok"
    ]
    outcomes = _outcomes(queue)
    assert outcomes["a-1"] == "merged"
    assert outcomes["a-2"] == "duplicate-verdict"
    # Only the first verdict's done wake exists.
    assert not [job for job in jobs if job[2].endswith(":a-2")]


def test_a_duplicate_approve_on_a_later_poll_does_not_ship_twice(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "c" * 40
    seed_review_job(dispatcher, queue, head)
    approve = VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high")
    green = {head: CIResult("success")}
    first = comment("a-1", approve, author="codex-login", created_at=1)
    dispatcher.process(ticket(first, head=head, ci=green))
    jobs = _jobs(queue)
    duplicate = comment("a-2", approve, author="codex-login", created_at=2)
    dispatcher.process(ticket(first, duplicate, head=head, ci=green))
    assert actions.merges == [(True, head)]
    assert _jobs(queue) == jobs
    assert _outcomes(queue)["a-2"] == "duplicate-verdict"


def test_a_different_verdict_at_the_same_head_is_not_a_duplicate(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "d" * 40
    seed_review_job(dispatcher, queue, head)
    revise = comment(
        "v-1", VERDICT.format(round="1/3", head=head, verdict="REVISE", risk="high"),
        author="codex-login", created_at=1,
    )
    approve = comment(
        "v-2", VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high"),
        author="codex-login", created_at=2,
    )
    dispatcher.process(ticket(revise, approve, head=head, ci={head: CIResult("success")}))
    assert actions.merges == []
    outcomes = _outcomes(queue)
    assert outcomes["v-1"] == "queued-review-fix"
    assert outcomes["v-2"] == "stale-approve"  # the existing rule, unchanged


# --- Codex PR #82 r2 F2.3: only an acted-on verdict makes a duplicate ----------


def test_a_rejected_wrong_role_revise_does_not_suppress_its_correction(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "e" * 40
    seed_review_job(dispatcher, queue, head)
    revise = VERDICT.format(round="1/3", head=head, verdict="REVISE", risk="high")
    wrong = comment(
        "v-wrong", revise.replace("role=reviewer", "role=builder"),
        author="codex-login", created_at=1,
    )
    dispatcher.process(ticket(wrong, head=head))
    assert _outcomes(queue)["v-wrong"] == "rejected-revise-author"
    assert [job[0] for job in _jobs(queue)].count("fix") == 0

    corrected = comment("v-right", revise, author="codex-login", created_at=2)
    dispatcher.process(ticket(wrong, corrected, head=head))
    dispatcher.process(ticket(wrong, corrected, head=head))  # and on a later poll
    assert _outcomes(queue)["v-right"] == "queued-review-fix"
    assert [job[0] for job in _jobs(queue)].count("fix") == 1

    # A repeat of the accepted correction is still a duplicate (one job).
    again = comment("v-again", revise, author="codex-login", created_at=3)
    dispatcher.process(ticket(wrong, corrected, again, head=head))
    assert _outcomes(queue)["v-again"] == "duplicate-verdict"
    assert [job[0] for job in _jobs(queue)].count("fix") == 1


def test_a_stale_verdict_does_not_make_a_later_one_a_duplicate(tmp_path: Path) -> None:
    """A verdict that took no action (stale, CI red) is no dedupe anchor."""
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "f" * 40
    seed_review_job(dispatcher, queue, head)
    approve = VERDICT.format(round="1/3", head=head, verdict="APPROVE", risk="high")
    first = comment("a-red", approve, author="codex-login", created_at=1)
    dispatcher.process(ticket(first, head=head, ci={head: CIResult("failure")}))
    assert _outcomes(queue)["a-red"] == "approve-ci-red"
    assert actions.merges == []

    second = comment("a-green", approve, author="codex-login", created_at=2)
    green = {head: CIResult("success")}
    dispatcher.process(ticket(first, second, head=head, ci=green))
    dispatcher.process(ticket(first, second, head=head, ci=green))
    assert _outcomes(queue)["a-green"] == "merged"
    assert actions.merges == [(True, head)]
