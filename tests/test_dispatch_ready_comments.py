from __future__ import annotations

from pathlib import Path

import pytest

from lib.dispatcher import Dispatcher
from tests.merge_queue_support import APPROVE, GO, READY, TRUSTED_LOGINS, VERDICT, sha
from tests.test_dispatcher import ANSWERS, PLAN, RETRY
from tests.test_review_before_ci import (
    CI_LOG,
    board_with,
    built_with_ci_pending,
    instruction,
    reviewed_at_pending_ci,
)


@pytest.mark.parametrize("mode", ["interrupt", "upgrade", "existing-table"])
def test_a_red_ready_back_at_an_older_head_recovers_when_the_verdict_head_was_never_recorded(
    tmp_path: Path, mode: str
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older, "approve-older")
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    if mode in ("interrupt", "existing-table"):
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
                connection.execute("ALTER TABLE verdict_heads DROP COLUMN posted_at")
                connection.execute("DROP TABLE verdict_heads_floor")
    else:
        board.poll()
        with board.queue.connect() as connection:
            connection.execute("DROP TABLE verdict_heads")
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


def test_a_fresh_database_puts_no_handled_comment_under_the_verdict_head_floor(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    _restart(board, tmp_path)
    with board.queue.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM verdict_heads_floor")] == [
            (0,)
        ]


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


@pytest.mark.parametrize("omitted", [False, True])
@pytest.mark.parametrize("upgrade", [False, True])
def test_an_upgrade_never_counts_a_handled_builder_ready_as_the_reviews_verdict(
    tmp_path: Path, upgrade: bool, omitted: bool
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    board.ready(pr, sha(2), "wrong-head-ready")
    board.poll()
    assert board.outcome("wrong-head-ready") == "stale-ready-head"
    if omitted:
        pr.comments.remove(next(item for item in pr.comments if item.id == "wrong-head-ready"))
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


ORCHESTRATOR_QUESTIONS = (
    "<!-- orch:v1 kind=questions from=grok-bot role=orchestrator -->\nQuestions.\n"
)


@pytest.mark.parametrize(
    ("body", "author", "outcome"),
    [
        (GO.format(state="building"), "grok-login", "deduped-build"),
        (PLAN.format(round=1, ac="met"), "claude-login", "rejected-plan-without-plan-job"),
        (ORCHESTRATOR_QUESTIONS, "grok-login", "no-questions"),
        (ANSWERS, "grok-login", "rejected-answers-without-question"),
        (RETRY.format(reason="Try again."), "grok-login", "rejected-retry-not-stuck"),
        (READY.format(head=sha(3)), "claude-login", "stale-ready-head"),
    ],
    ids=["duplicate-go", "plan", "questions", "answers", "retry", "wrong-head-ready"],
)
def test_a_hidden_handled_non_verdict_comment_never_answers_the_review(
    tmp_path: Path, body: str, author: str, outcome: str
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    pr.push(sha(2), ci="pending")
    board.post(pr, "non-verdict", body, author)
    board.poll()
    assert board.outcome("non-verdict") == outcome
    pr.comments.remove(next(item for item in pr.comments if item.id == "non-verdict"))
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
