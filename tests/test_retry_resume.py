from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatch.constants import STOPPED_POLL_SECONDS
from lib.dispatch.types import Comment
from lib.hook_t7 import T7Hook
from lib.queue import Job
from tests.merge_queue_support import PR, REPO, Board, sha
from tests.test_dispatcher import PLAN, RETRY, VERDICT
from tests.test_remote_start import FakeTransport, config_of, machines_file

REASON = RETRY.format(reason="Max: retry.")
STUCK_FIXTURE = Path(__file__).parent / "fixtures" / "comments" / "stuck.txt"


def _stuck_fix_with_unsettled_failure(b: Board, p: PR) -> None:
    b.go(p, "go")
    b.poll()
    b.finish("claude")
    b.ready(p, p.head)
    b.poll()
    b.finish("codex")
    b.revise(p, "1/3")
    b.poll()
    b.fail("claude")
    b.poll()
    b.now[0] += STOPPED_POLL_SECONDS
    b.poll()
    b.fail("claude")
    settle = b.queue.mark_failure_handled

    def crash(job_id: str) -> None:
        raise SystemExit("crash after the stuck commit")

    b.queue.mark_failure_handled = crash
    with pytest.raises(SystemExit):
        b.poll()
    b.queue.mark_failure_handled = settle
    assert b.state("42")["stuck_reason"] == "job-failed-2"


def test_retry_retires_the_failure_t6_left_unsettled(tmp_path: Path) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    _stuck_fix_with_unsettled_failure(b, p)

    b.post(p, "retry-fix", REASON, "grok-login")
    b.now[0] += 600
    b.poll()

    assert b.outcome("retry-fix") == "queued-retry-fix"
    assert b.state("42")["stuck"] == 0
    job = b.claim("claude")
    p.push(sha(2))
    b.ready(p, p.head, "fixed-ready")
    b.queue.finish(job.id, owner="claude", handoff_comment_id="fixed-ready")
    b.now[0] += 600
    for _ in range(5):
        b.poll()
    assert b.outcome("fixed-ready") == "queued-review"
    assert b.queue.claim("codex") is not None


def test_retry_after_update_failed_2_queues_another_update(tmp_path: Path) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.build_and_approve(p, mergeable=False)
    for n in (1, 2):
        job = b.claim("claude")
        ready = f"bad-ready-{n}"
        b.queue.finish(job.id, owner="claude", handoff_comment_id=ready)
        b.ready(p, p.head, comment_id=ready)
        b.poll()
        if n == 1:
            b.poll()
    assert b.state("42")["stuck_reason"] == "update-failed-2"

    b.post(p, "retry-update", REASON, "grok-login")
    b.now[0] += 600
    for _ in range(10):
        b.poll()

    assert b.outcome("retry-update") == "queued-retry-update-from-main"
    assert [r["state"] for r in b.jobs("update-from-main")] == ["done", "done", "queued"]
    row = b.state("42")
    assert (row["workflow_state"], row["stuck"], row["update_failures"]) == ("updating", 0, 1)
    job = b.claim("claude")
    assert job.request.action == "update-from-main"
    b.queue.finish(job.id, owner="claude", handoff_comment_id="bad-ready-3")
    b.ready(p, p.head, comment_id="bad-ready-3")
    b.poll()
    b.poll()
    assert b.state("42")["stuck_reason"] == "update-failed-2"


def test_retry_of_a_failed_plan_accepts_the_replacement_plan(tmp_path: Path) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go", state="planning")
    b.poll()
    original = b.claim("claude")
    b.queue.finish(original.id, owner="claude", handoff_comment_id=None)
    b.poll()
    b.fail("claude")
    b.poll()
    assert b.state("42")["stuck_reason"] == "job-failed-2"
    b.actions.state_failures.add(("owner/repo", "plan-check"))
    b.post(p, "plan-1", PLAN.format(round=1, ac="met"), "claude-login")
    b.poll()
    b.actions.state_failures.clear()

    b.post(p, "retry-plan", REASON, "grok-login")
    b.now[0] += 600
    for _ in range(10):
        b.poll()
    assert b.outcome("retry-plan") == "queued-retry-plan"
    resumed = b.claim("claude")
    b.post(p, "plan-2", PLAN.format(round=1, ac="met"), "claude-login")
    b.queue.finish(resumed.id, owner="claude", handoff_comment_id="plan-2")
    b.poll()

    assert b.outcome("plan-2") == "queued-plan-build"
    assert b.state("42")["workflow_state"] == "building"
    assert b.claim("claude").request.action == "build"


def _stuck_update(
    b: Board, p: PR, *, pushed: str | None = None, late_from: int | None = None
) -> Comment | None:
    b.build_and_approve(p, mergeable=False)
    delayed = None
    for attempt in (1, 2):
        job = b.claim("claude")
        if attempt == 2 and pushed is not None:
            p.push(pushed)
        if attempt == late_from:
            b.ready(p, p.head, "delayed-ready")
            delayed = p.comments.pop()
        b.queue.finish(job.id, owner="claude", handoff_comment_id=None)
        for _ in range(10):
            b.poll()
    assert b.state("42")["stuck_reason"] == "update-failed-2"
    return delayed


def test_retry_of_an_update_that_pushed_a_clean_head_resumes_the_update(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    _stuck_update(b, p, pushed=sha(2))

    b.post(p, "retry-update", REASON, "grok-login")
    b.now[0] += 600
    for _ in range(10):
        b.poll()
    assert b.outcome("retry-update") == "queued-retry-update-from-main"
    job = b.claim("claude")
    assert (job.request.action, job.request.head) == ("update-from-main", sha(2))

    b.ready(p, p.head, "updated-ready")
    b.queue.finish(job.id, owner="claude", handoff_comment_id="updated-ready")
    for _ in range(5):
        b.poll()
    assert b.outcome("updated-ready") == f"approval-carried:{sha(1)[:12]}->{sha(2)[:12]}"
    assert p.merged


@pytest.mark.parametrize("late_from", [1, 2])
def test_a_late_ready_from_a_counted_update_leaves_the_retry_queued(
    tmp_path: Path, late_from: int
) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    delayed = _stuck_update(b, p, late_from=late_from)

    b.post(p, "retry-update", REASON, "grok-login")
    b.now[0] += 600
    for _ in range(10):
        b.poll()
    assert delayed is not None
    p.comments.append(delayed)
    for _ in range(10):
        b.poll()

    assert b.outcome("delayed-ready") == "update-failure-counted"
    assert (b.state("42")["stuck"], b.state("42")["update_failures"]) == (0, 1)
    assert [r["state"] for r in b.jobs("update-from-main")] == ["failed", "failed", "queued"]


def _retried_review(b: Board, p: PR, *, revises: int) -> None:
    b.go(p, "go")
    b.poll()
    b.finish("claude")
    for n in range(1, revises + 1):
        b.ready(p, p.head)
        b.poll()
        b.finish("codex")
        b.revise(p, f"{n}/3")
        b.poll()
        b.finish("claude")
        p.push(sha(n + 1))
    b.actions.state_failures.add((REPO, "review"))
    ready = b.ready(p, p.head)
    b.poll()
    b.actions.state_failures.clear()
    p.comments[-1] = replace(p.comments[-1], edited_at=b.now[0])
    b.poll()
    assert b.outcome(ready) == "rejected-edited"
    assert b.state("42")["workflow_state"] == ("fixing" if revises else "building")
    b.fail("codex")
    b.poll()
    b.now[0] += STOPPED_POLL_SECONDS
    b.poll()
    b.fail("codex")
    b.poll()
    assert b.state("42")["stuck_reason"] == "job-failed-2"

    b.post(p, "retry-review", REASON, "grok-login")
    b.now[0] += 600
    b.poll()
    assert b.outcome("retry-review") == "queued-retry-review"
    b.finish("codex")


def _queued_fix_heads(b: Board) -> list[str]:
    return [row["head"] for row in b.jobs("fix") if row["state"] == "queued"]


@pytest.mark.parametrize("revises", [0, 1], ids=["building", "fixing"])
def test_a_retried_review_s_revise_queues_the_fix(tmp_path: Path, revises: int) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    _retried_review(b, p, revises=revises)
    assert b.state("42")["workflow_state"] == "review"

    b.revise(p, f"{revises + 1}/3")
    b.poll()

    assert b.outcome(f"revise-42-{revises + 1}/3") == "queued-review-fix"
    assert b.state("42")["workflow_state"] == "fixing"
    assert _queued_fix_heads(b) == [p.head]
    assert b.claim("claude").request.action == "fix"


def test_a_retried_review_s_revise_at_the_cap_caps(tmp_path: Path) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    _retried_review(b, p, revises=2)

    b.revise(p, "3/3")
    b.poll()

    assert b.outcome("revise-42-3/3") == "capped-review"
    assert b.state("42")["workflow_state"] == "capped"
    assert _queued_fix_heads(b) == []


def test_a_retried_review_s_approve_ships(tmp_path: Path) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    _retried_review(b, p, revises=1)

    approve = b.approve(p, "2/3")
    b.poll()

    assert b.outcome(approve) == "merge-queue"
    assert p.merged


def test_a_stale_verdict_after_a_retried_review_is_still_refused(tmp_path: Path) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    _retried_review(b, p, revises=1)

    for comment_id, round_text, head in (
        ("old-head", "2/3", sha(1)),
        ("wrong-round", "1/3", p.head),
    ):
        body = VERDICT.format(round=round_text, head=head, verdict="REVISE", risk="high")
        b.post(p, comment_id, body, "codex-login")
        b.poll()
        assert b.outcome(comment_id) == "stale-revise"
    assert b.state("42")["workflow_state"] == "review"
    assert _queued_fix_heads(b) == []


def _fix_running_on_pc(tmp_path: Path, *, answers: bool) -> tuple[Board, PR, DevicePool, Job]:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    b.finish("claude")
    b.ready(p, p.head)
    b.poll()
    b.finish("codex")
    b.revise(p, "1/3")
    b.poll()
    pool = DevicePool(b.queue, config_of(machines_file(tmp_path, box_enabled=False)))
    claimed = pool.claim("claude")
    assert claimed is not None
    job = claimed[0]
    pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", remote=True, tag="e" * 32)
    (tmp_path / "now").mkdir()
    now_config = config_of(machines_file(tmp_path / "now", box_enabled=False))
    b.dispatcher.devices = DeviceMonitor(
        pool, now_config, FakeTransport(check=1 if answers else 255), alive=lambda *_: True
    )
    b.poll()
    assert (job.request.action, b.jobs("fix")[0]["state"]) == ("fix", "running")
    return b, p, pool, job


def _disable_pc_since_last_poll(b: Board, tmp_path: Path) -> None:
    # The poll itself loses a disabled device's jobs (issue #679), so the
    # retry meets a disable made after the last poll.
    (tmp_path / "off").mkdir()
    b.dispatcher.devices.config = config_of(
        machines_file(tmp_path / "off", box_enabled=False, pc=False)
    )


def _events(b: Board, job_id: str) -> list[str]:
    with b.queue.connect() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT event FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
            )
        ]


def _orphans(b: Board) -> list[tuple[str, str]]:
    with b.queue.connect() as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'device_orphans'"
        ).fetchone() is None:
            return []
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute("SELECT job_id, device FROM device_orphans")
        ]


@pytest.mark.parametrize(
    ("answers", "pc_enabled"), [(False, True), (True, False)], ids=["unreachable", "disabled"]
)
def test_retry_requeues_a_job_stranded_on_its_device(
    tmp_path: Path, answers: bool, pc_enabled: bool
) -> None:
    b, p, pool, job = _fix_running_on_pc(tmp_path, answers=answers)
    if not pc_enabled:
        _disable_pc_since_last_poll(b, tmp_path)

    b.post(p, "retry-1", REASON, "grok-login")
    b.poll()

    assert b.outcome("retry-1") == "queued-retry-stranded"
    [row] = b.jobs("fix")
    assert (row["id"], row["state"], row["attempt"], row["generation"]) == (
        job.id, "queued", job.request.attempt, job.request.generation,
    )
    assert _orphans(b) == [(job.id, "pc")]
    assert _events(b, job.id)[-2:] == ["device-lost", "requeued-link-drop"]
    assert pool.claim("claude") is None
    state = b.state("42")
    assert (state["workflow_state"], state["stuck"]) == ("fixing", 0)

    with b.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'retry-1'")
    b.poll()

    assert b.outcome("retry-1") == "queued-retry-stranded"
    assert [r["state"] for r in b.jobs("fix")] == ["queued"]
    assert _events(b, job.id).count("requeued-link-drop") == 1


def test_retry_still_refuses_a_job_on_a_reachable_enabled_device(tmp_path: Path) -> None:
    b, p, pool, job = _fix_running_on_pc(tmp_path, answers=True)

    b.post(p, "retry-1", REASON, "grok-login")
    b.poll()

    assert b.outcome("retry-1") == "rejected-retry-not-stuck"
    assert [r["state"] for r in b.jobs("fix")] == ["running"]
    placement = pool.placement(job.id)
    assert placement is not None and placement.device == "pc"


@pytest.mark.parametrize(
    ("answers", "pc_enabled"), [(False, True), (True, False)], ids=["unreachable", "disabled"]
)
def test_retry_of_a_stranded_job_on_a_stuck_ticket_lets_the_replacement_resume_it(
    tmp_path: Path, answers: bool, pc_enabled: bool
) -> None:
    b, p, pool, job = _fix_running_on_pc(tmp_path, answers=answers)
    b.dispatcher.hooks = (T7Hook(),)
    b.post(p, "stuck-report", STUCK_FIXTURE.read_text(), "grok-login")
    b.poll()
    assert b.state("42")["stuck"] == 1
    if not pc_enabled:
        _disable_pc_since_last_poll(b, tmp_path)

    b.post(p, "retry-1", REASON, "grok-login")
    b.now[0] += 600
    b.poll()

    assert b.outcome("retry-1") == "queued-retry-stranded"
    state = b.state("42")
    assert (state["workflow_state"], state["stuck"], state["stuck_reason"]) == (
        "fixing", 0, None,
    )
    [row] = b.jobs("fix")
    assert (row["state"], row["attempt"], row["generation"]) == (
        "queued", job.request.attempt, job.request.generation,
    )
    box = DevicePool(b.queue, config_of(machines_file(tmp_path / "now", box_enabled=True)))
    claimed = box.claim("claude")
    assert claimed is not None
    replacement, placed = claimed
    assert (replacement.id, placed.device) == (job.id, "box")
    p.push(sha(2))
    b.ready(p, p.head, "replacement-ready")
    b.queue.finish(replacement.id, owner="claude", handoff_comment_id="replacement-ready")
    b.now[0] += 600
    b.poll()

    assert b.outcome("replacement-ready") == "queued-review"
    assert b.state("42")["stuck"] == 0


@pytest.mark.job_bound
def test_retry_of_a_stranded_job_that_posted_its_ready_settles_it_instead_of_rerunning(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    config = config_of(machines_file(tmp_path))
    pool = DevicePool(b.queue, config)
    b.dispatcher.devices = DeviceMonitor(
        pool, config, FakeTransport(check=255), alive=lambda *_: True
    )
    claimed = pool.claim("claude")
    assert claimed is not None
    job, placed = claimed
    assert placed.device == "pc"
    pool.attach_runner(job.id, owner="claude", pid=11, start="r", alive=lambda *_: False)
    pool.attach_worker(job.id, owner="claude", remote=True, tag="e" * 32)
    b.poll()
    b.ready(p, p.head)
    b.post(p, "retry-1", REASON, "grok-login")
    b.poll()

    assert b.outcome("retry-1") == "retried-stranded-posted"
    assert pool.claim("claude") is None
    b.poll()

    [build] = b.jobs("build")
    assert build["state"] == "done"
    assert [r["state"] for r in b.jobs("review")] == ["queued"]
    assert pool.claim("claude") is None


LEFTOVER_TAG = "e" * 32


def _running_with_leftovers(
    b: Board, tmp_path: Path, recipient: str
) -> tuple[DevicePool, FakeTransport, Job]:
    """A job on the pc whose `orch job run` is gone while a leaked process
    still carries its worker tag, so the device reports the worker running."""
    config = config_of(machines_file(tmp_path))
    pool = DevicePool(b.queue, config)
    transport = FakeTransport(check=1)
    b.dispatcher.devices = DeviceMonitor(pool, config, transport, alive=lambda *_: False)
    claimed = pool.claim(recipient)
    assert claimed is not None
    job, placed = claimed
    assert placed.device == "pc"
    pool.attach_runner(job.id, owner=recipient, pid=11, start="r", alive=lambda *_: False)
    pool.attach_worker(job.id, owner=recipient, remote=True, tag=LEFTOVER_TAG)
    b.poll()
    assert _job_state(b, job.id) == "running"
    return pool, transport, job


def _answer(b: Board, recipient: str, comment_id: str) -> None:
    job = b.claim(recipient)
    b.queue.finish(job.id, owner=recipient, handoff_comment_id=comment_id)


def _job_state(b: Board, job_id: str) -> str:
    [row] = [row for row in b.jobs() if row["id"] == job_id]
    return str(row["state"])


def _handoff(b: Board, job_id: str) -> str | None:
    [row] = [row for row in b.jobs() if row["id"] == job_id]
    return row["handoff_comment_id"]


def _stops(transport: FakeTransport) -> int:
    return sum(1 for kind, _user, _alias in transport.calls if kind == "stop")


@pytest.mark.job_bound
def test_4f441ac6_a_dead_runner_with_tagged_leftovers_settles_from_its_posted_ready(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    pool, transport, build = _running_with_leftovers(b, tmp_path, "claude")
    p.push(sha(2))
    ready = b.ready(p, p.head)
    b.poll()

    assert _job_state(b, build.id) == "done"
    assert _handoff(b, build.id) == ready
    events = _events(b, build.id)
    assert events.index("dead") < events.index("done-from-ready")
    assert pool.placement(build.id) is None
    assert _orphans(b) == [(build.id, "pc")]

    b.poll()

    assert _stops(transport) >= 1
    assert b.outcome(ready) == "queued-review"
    assert [r["state"] for r in b.jobs("review")] == ["queued"]
    assert b.jobs("review")[0]["head"] == sha(2)


@pytest.mark.job_bound
def test_settling_from_a_posted_ready_is_idempotent_across_polls(tmp_path: Path) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    _pool, _transport, build = _running_with_leftovers(b, tmp_path, "claude")
    p.push(sha(2))
    ready = b.ready(p, p.head)
    for _ in range(4):
        b.poll()

    assert b.outcome(ready) == "queued-review"
    assert [r["state"] for r in b.jobs("review")] == ["queued"]
    events = _events(b, build.id)
    assert (events.count("dead"), events.count("done-from-ready")) == (1, 1)
    assert _job_state(b, build.id) == "done"


def _review_running_with_leftovers(
    b: Board, tmp_path: Path
) -> tuple[PR, DevicePool, FakeTransport, Job]:
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    _answer(b, "claude", b.ready(p, p.head))
    b.poll()
    pool, transport, review = _running_with_leftovers(b, tmp_path, "codex")
    assert review.request.action == "review"
    return p, pool, transport, review


@pytest.mark.job_bound
def test_a_dead_reviewer_with_tagged_leftovers_settles_from_its_posted_verdict(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p, pool, transport, review = _review_running_with_leftovers(b, tmp_path)
    verdict = b.revise(p, "1/3")
    b.poll()

    assert _job_state(b, review.id) == "done"
    assert _handoff(b, review.id) == verdict
    assert "done-from-verdict" in _events(b, review.id)
    assert pool.placement(review.id) is None
    assert _orphans(b) == [(review.id, "pc")]

    b.poll()

    assert b.outcome(verdict) == "queued-review-fix"
    assert b.state("42")["workflow_state"] == "fixing"
    assert _queued_fix_heads(b) == [p.head]
    assert _stops(transport) >= 1
    review_round = b.state("42")["review_round"]

    b.poll()
    b.poll()

    assert _queued_fix_heads(b) == [p.head]
    assert len(b.jobs("fix")) == 1
    assert b.state("42")["review_round"] == review_round
    assert _events(b, review.id).count("done-from-verdict") == 1


@pytest.mark.job_bound
def test_a_dead_runner_with_tagged_leftovers_and_no_handoff_stays_running(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    pool, _transport, build = _running_with_leftovers(b, tmp_path, "claude")
    for _ in range(3):
        b.now[0] += 600
        b.poll()

    assert _job_state(b, build.id) == "running"
    assert not {"dead", "done-from-ready"} & set(_events(b, build.id))
    assert pool.placement(build.id) is not None
    assert _orphans(b) == []


@pytest.mark.job_bound
def test_a_handoff_for_another_job_does_not_settle_a_dead_runner_with_leftovers(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p, pool, _transport, review = _review_running_with_leftovers(b, tmp_path)
    for comment_id, round_text, head in (
        ("old-head", "1/3", sha(9)),
        ("other-round", "2/3", p.head),
    ):
        body = VERDICT.format(round=round_text, head=head, verdict="REVISE", risk="high")
        b.post(p, comment_id, body, "codex-login")
    b.ready(p, p.head, "builder-ready-again")
    b.poll()
    b.poll()

    assert _job_state(b, review.id) == "running"
    assert not {"dead", "done-from-verdict"} & set(_events(b, review.id))
    assert pool.placement(review.id) is not None
    assert _orphans(b) == []


@pytest.mark.job_bound
def test_a_ready_from_an_earlier_generation_does_not_settle_a_dead_runner_with_leftovers(
    tmp_path: Path,
) -> None:
    b = Board(tmp_path)
    p = b.open("42", sha(1))
    b.go(p, "go")
    b.poll()
    _answer(b, "claude", b.ready(p, p.head))
    b.poll()
    _answer(b, "codex", b.revise(p, "1/3"))
    b.poll()
    pool, _transport, fix = _running_with_leftovers(b, tmp_path, "claude")
    assert fix.request.action == "fix"
    b.poll()
    b.poll()

    assert _job_state(b, fix.id) == "running"
    assert not {"dead", "done-from-ready"} & set(_events(b, fix.id))
    assert pool.placement(fix.id) is not None
    assert _orphans(b) == []
