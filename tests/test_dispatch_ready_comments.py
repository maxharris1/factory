from __future__ import annotations

from pathlib import Path

import pytest

from lib.dispatch.constants import STOPPED_POLL_SECONDS
from lib.dispatcher import Dispatcher
from lib.hook_t7 import T7Hook
from tests.merge_queue_support import APPROVE, TRUSTED_LOGINS, VERDICT, sha
from tests.test_review_before_ci import (
    CI_LOG,
    board_with,
    built_with_ci_pending,
    instruction,
    reviewed_at_pending_ci,
)
from tests.test_retry_resume import REASON


@pytest.mark.parametrize("mode", ["interrupt", "upgrade", "existing-table"])
def test_a_red_ready_back_at_an_older_head_recovers_when_the_verdict_head_was_never_recorded(
    tmp_path: Path, mode: str
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    if mode == "upgrade":
        board.poll()
        with board.queue.connect() as connection:
            connection.execute("DROP TABLE verdict_heads")
    else:
        original = board.dispatcher._mark_handled

        def stop_after_handled(ticket, comment_id, outcome):
            original(ticket, comment_id, outcome)
            if comment_id == "approve-older":
                raise KeyboardInterrupt()

        board.dispatcher._mark_handled = stop_after_handled
        with pytest.raises(KeyboardInterrupt):
            board.poll()
    if mode == "existing-table":
        with board.queue.connect() as connection:
            connection.execute("DELETE FROM verdict_heads WHERE comment_id = 'approve-older'")
            connection.execute("DROP TABLE verdict_heads_floor")
    board.dispatcher = Dispatcher(
        tmp_path / "d.db",
        board.queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=board.actions,
        grok_delivery=board.grok,
        log=board.log.append,
    )
    assert board.outcome("approve-older") == "approve-ci-red-fix"
    board.finish("claude")

    hidden = next(item for item in pr.comments if item.id == "approve-older")
    pr.comments.remove(hidden)
    pr.push(older, ci="failure")
    board.ready(pr, older, "return-ready")
    board.poll()
    pr.comments.append(hidden)
    board.poll()

    fix = board.queue.claim("claude")
    assert fix is not None, "the ready back at the older head left no runnable CI fix"
    assert fix.request.head == older
    assert fix.request.source_comment_id == "return-ready"
    assert CI_LOG in instruction({"detail": fix.request.detail})


def test_a_fresh_database_notes_no_verdict_head_floor(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    with board.queue.connect() as connection:
        assert connection.execute("SELECT * FROM verdict_heads_floor").fetchall() == []


def test_a_stale_verdict_handled_late_does_not_answer_the_newer_review(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    board.poll()
    board.finish("claude")
    pr.push(older, ci="failure")
    board.ready(pr, older, "first-return")
    board.poll()
    board.finish("claude")

    revise = VERDICT.format(round="1/3", head=older, verdict="REVISE", risk="high")
    board.post(pr, "old-stale-revise", revise, "codex-login")
    hidden = pr.comments.pop()
    pr.push(older, ci="pending")
    board.ready(pr, older, "new-review-ready")
    board.poll()
    board.finish("codex")
    pr.push(sha(2), ci="pending")
    pr.comments.append(hidden)
    board.poll()
    assert board.outcome("old-stale-revise").startswith("stale-")

    pr.push(older, ci="failure")
    board.ready(pr, older, "red-ready-before-new-verdict")
    board.poll()
    board.post(pr, "new-revise", revise, "codex-login")
    board.poll()

    fixes = [job for job in board.jobs("fix") if job["state"] in ("queued", "running")]
    assert [job["source_comment_id"] for job in fixes] == ["new-revise"]
    assert CI_LOG in instruction(fixes[0])
    first = board.claim("claude")
    board.queue.finish(first.id, owner="claude", handoff_comment_id="first-fix")
    assert board.queue.claim("claude") is None


def test_a_reviews_own_verdict_handled_stale_still_answers_it(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    pr.push(older, ci="pending")
    board.ready(pr, older, "ready-at-older")
    board.poll()
    board.finish("codex")
    pr.push(sha(2), ci="pending")
    revise = VERDICT.format(round="1/3", head=older, verdict="REVISE", risk="high")
    board.post(pr, "revise-older", revise, "codex-login")
    board.poll()
    assert board.outcome("revise-older").startswith("stale-")

    pr.push(older, ci="failure")
    board.ready(pr, older, "red-back-at-older")
    board.poll()

    fix = board.queue.claim("claude")
    assert fix is not None, "the red ready after an answered review left no runnable CI fix"
    assert fix.request.source_comment_id == "red-back-at-older"
    assert CI_LOG in instruction({"detail": fix.request.detail})


def _restart(board, tmp_path: Path) -> None:
    board.dispatcher = Dispatcher(
        tmp_path / "d.db",
        board.queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=board.actions,
        grok_delivery=board.grok,
        log=board.log.append,
    )


@pytest.mark.parametrize("upgrade", [False, True])
def test_an_upgrade_backfills_the_post_time_of_a_verdict_recorded_before_it(
    tmp_path: Path, upgrade: bool
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    revise = VERDICT.format(round="1/3", head=older, verdict="REVISE", risk="high")
    board.post(pr, "old-stale-revise", revise, "codex-login")
    board.poll()
    assert board.outcome("old-stale-revise") is None
    hidden = pr.comments.pop()
    if upgrade:
        with board.queue.connect() as connection:
            connection.execute("ALTER TABLE verdict_heads DROP COLUMN posted_at")
        _restart(board, tmp_path)
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    board.poll()
    assert board.outcome("approve-older") == "approve-ci-red-fix"
    board.finish("claude")
    pr.push(older, ci="failure")
    board.ready(pr, older, "first-return")
    board.poll()
    board.finish("claude")
    pr.push(older, ci="pending")
    board.ready(pr, older, "new-review-ready")
    board.poll()
    board.finish("codex")
    pr.push(sha(2), ci="pending")
    pr.comments.append(hidden)
    board.poll()
    assert board.outcome("old-stale-revise").startswith("stale-")
    with board.queue.connect() as connection:
        posted_at = connection.execute(
            "SELECT posted_at FROM verdict_heads WHERE comment_id = 'old-stale-revise'"
        ).fetchone()[0]
    assert posted_at == hidden.created_at

    pr.push(older, ci="failure")
    board.ready(pr, older, "red-ready-before-new-verdict")
    board.poll()
    board.post(pr, "new-revise", revise, "codex-login")
    board.poll()

    fixes = [job for job in board.jobs("fix") if job["state"] in ("queued", "running")]
    assert [job["source_comment_id"] for job in fixes] == ["new-revise"]
    assert CI_LOG in instruction(fixes[0])


def _handled_ready_at_a_newer_head(board, pr) -> str:
    board.ready(pr, sha(2), "wrong-head-ready")
    board.poll()
    assert board.outcome("wrong-head-ready") == "stale-ready-head"
    return "wrong-head-ready"


def _handled_duplicate_go(board, pr) -> str:
    pr.push(sha(2), ci="pending")
    board.go(pr, "duplicate-go")
    board.poll()
    assert board.outcome("duplicate-go") == "deduped-build"
    return "duplicate-go"


@pytest.mark.parametrize("omitted", [False, True])
@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("handled", [_handled_ready_at_a_newer_head, _handled_duplicate_go])
def test_an_upgrade_never_counts_a_handled_non_verdict_as_the_reviews_verdict(
    tmp_path: Path, upgrade: bool, omitted: bool, handled
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    comment_id = handled(board, pr)
    if omitted:
        pr.comments.remove(next(item for item in pr.comments if item.id == comment_id))
    if upgrade:
        with board.queue.connect() as connection:
            connection.execute("DROP TABLE verdict_heads")
        _restart(board, tmp_path)
    pr.push(older, ci="failure")
    board.ready(pr, older, "red-ready-before-verdict")
    board.poll()
    board.revise(pr, "1/3")
    board.poll()

    fixes = [job for job in board.jobs("fix") if job["state"] in ("queued", "running")]
    assert [job["source_comment_id"] for job in fixes] == ["revise-42-1/3"]
    assert CI_LOG in instruction(fixes[0])


def _upgrade_from(board, missing: str) -> None:
    with board.queue.connect() as connection:
        connection.execute(f"DROP TABLE {missing}")


@pytest.mark.parametrize("missing", ["verdict_heads_floor", "verdict_heads"])
@pytest.mark.parametrize("omitted", ["stale-revise", "stuck-go"])
def test_a_comment_handled_before_a_retry_never_answers_the_retried_review(
    tmp_path: Path, missing: str, omitted: str
) -> None:
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    board.ready(pr, pr.head, "review-ready")
    board.poll()
    if omitted == "stale-revise":
        stale = VERDICT.format(round="1/3", head=sha(9), verdict="REVISE", risk="high")
        board.post(pr, omitted, stale, "codex-login")
        board.poll()
        assert board.outcome(omitted) == "stale-revise"
    board.fail("codex")
    board.poll()
    board.now[0] += STOPPED_POLL_SECONDS
    board.poll()
    board.fail("codex")
    board.poll()
    assert board.state("42")["stuck_reason"] == "job-failed-2"
    if omitted == "stuck-go":
        board.go(pr, omitted)
        board.now[0] += STOPPED_POLL_SECONDS
        board.poll()
        assert board.outcome(omitted) == "ticket-stuck"
    pr.comments.remove(next(item for item in pr.comments if item.id == omitted))
    _upgrade_from(board, missing)
    _restart(board, tmp_path)
    board.post(pr, "retry-review", REASON, "grok-login")
    board.now[0] += STOPPED_POLL_SECONDS
    board.poll()
    assert board.outcome("retry-review") == "queued-retry-review"
    board.finish("codex")
    pr.push(pr.head, ci="failure")
    board.ready(pr, pr.head, "red-ready")
    board.poll()
    board.revise(pr, "1/3")
    board.poll()

    fixes = [job for job in board.jobs("fix") if job["state"] in ("queued", "running")]
    assert board.outcome("red-ready") == "ci-red-in-flight"
    assert [job["source_comment_id"] for job in fixes] == ["revise-42-1/3"]


@pytest.mark.parametrize("missing", ["verdict_heads_floor", "verdict_heads"])
def test_a_blocked_verdict_handled_by_a_hook_still_answers_its_review(
    tmp_path: Path, missing: str
) -> None:
    board = board_with(tmp_path, on=True)
    board.dispatcher.hooks = (T7Hook(),)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    blocked = VERDICT.format(round="1/3", head=pr.head, verdict="BLOCKED", risk="high")
    board.post(pr, "blocked-review", blocked, "codex-login")
    board.poll()
    assert board.outcome("blocked-review") == "woke-grok-blocked"
    pr.comments.remove(next(item for item in pr.comments if item.id == "blocked-review"))
    _upgrade_from(board, missing)
    _restart(board, tmp_path)
    board.dispatcher.hooks = (T7Hook(),)
    pr.push(pr.head, ci="failure")
    board.ready(pr, pr.head, "red-ready")
    board.poll()

    fix = board.queue.claim("claude")
    assert board.outcome("red-ready") == "queued-ci-fix"
    assert fix is not None and fix.request.source_comment_id == "red-ready"
