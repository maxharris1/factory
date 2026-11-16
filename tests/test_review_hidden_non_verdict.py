from __future__ import annotations

from pathlib import Path

import pytest

from lib.dispatch.constants import STOPPED_POLL_SECONDS
from lib.handoff_check import expected_for, problems
from lib.hook_t7 import T7Hook
from lib.queue import JobRequest
from tests.merge_queue_support import TRUSTED_LOGINS, VERDICT, sha
from tests.test_dispatch_ready_comments import _restart
from tests.test_review_before_ci import (
    board_with,
    built_with_ci_pending,
    ci_failure,
    reviewed_at_pending_ci,
)
from tests.test_retry_resume import REASON

BUILDER_BLOCKED = "<!-- orch:v1 kind=stuck from=claude-bot role=builder state=blocked -->\nBlocked.\n"


def _upgrade(board, tmp_path: Path, upgrade: str, hidden_id: str) -> None:
    with board.queue.connect() as connection:
        if upgrade == "missing-table":
            connection.execute("DROP TABLE verdict_heads")
        else:
            connection.execute("DELETE FROM verdict_heads WHERE comment_id = ?", (hidden_id,))
            connection.execute("ALTER TABLE verdict_heads DROP COLUMN posted_at")
            connection.execute("DROP TABLE verdict_heads_floor")
    _restart(board, tmp_path)


def _hide(pr, comment_id: str) -> None:
    pr.comments.remove(next(item for item in pr.comments if item.id == comment_id))


def _active_fixes(board) -> list[dict]:
    return [job for job in board.jobs("fix") if job["state"] in ("queued", "running")]


@pytest.mark.parametrize("upgrade", ["missing-table", "existing-table"])
def test_a_hidden_builder_blocked_report_never_answers_the_review(
    tmp_path: Path, upgrade: str
) -> None:
    board = board_with(tmp_path, on=True)
    board.dispatcher.hooks = (T7Hook(),)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    pr.push(sha(2), ci="pending")
    board.post(pr, "builder-blocked", BUILDER_BLOCKED, "claude-login")
    board.poll()
    assert board.outcome("builder-blocked") == "woke-grok-blocked"
    _hide(pr, "builder-blocked")
    _upgrade(board, tmp_path, upgrade, "builder-blocked")
    pr.push(older, ci="failure")
    board.ready(pr, older, "red-ready-before-verdict")
    board.poll()
    board.revise(pr, "1/3")
    board.poll()

    fixes = _active_fixes(board)
    assert [job["source_comment_id"] for job in fixes] == ["revise-42-1/3"]
    assert ci_failure(fixes[0]) == {"failed_checks": ["pytest"]}


@pytest.mark.parametrize("upgrade", ["missing-table", "existing-table"])
def test_a_hidden_blocked_verdict_keeps_the_ticket_blocked(tmp_path: Path, upgrade: str) -> None:
    board = board_with(tmp_path, on=True)
    board.dispatcher.hooks = (T7Hook(),)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    pr.push(older, ci="pending")
    board.ready(pr, older)
    board.poll()
    review = board.claim("codex")
    board.queue.finish(review.id, owner="codex", handoff_comment_id="blocked-verdict")
    blocked = VERDICT.format(round="1/3", head=older, verdict="BLOCKED", risk="high")
    board.post(pr, "blocked-verdict", blocked, "codex-login")
    board.poll()
    assert board.outcome("blocked-verdict") == "blocked-review"
    _hide(pr, "blocked-verdict")
    _upgrade(board, tmp_path, upgrade, "blocked-verdict")
    pr.push(sha(2), ci="pending")
    board.poll()
    pr.push(older, ci="failure")
    board.ready(pr, older, "red-ready")
    board.poll()

    assert board.state("42")["workflow_state"] == "blocked"
    assert _active_fixes(board) == []


@pytest.mark.job_bound
def test_a_hidden_unregistered_ready_never_answers_the_review(tmp_path: Path) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = board.open("42", older)
    pr.push(older, ci="pending")
    board.go(pr, "go")
    board.poll()
    build = board.claim("claude")
    board.queue.finish(build.id, owner="claude", handoff_comment_id="ready-older")
    board.ready(pr, older, "ready-older")
    board.poll()
    review = board.claim("codex")
    board.queue.finish(review.id, owner="codex", handoff_comment_id="verdict-real")
    pr.push(sha(2), ci="pending")
    board.ready(pr, sha(2), "ready-stray")
    board.poll()
    assert board.outcome("ready-stray") == "rejected-unregistered-ready"
    _hide(pr, "ready-stray")
    with board.queue.connect() as connection:
        connection.execute("DROP TABLE verdict_heads_floor")
    _restart(board, tmp_path)
    pr.push(older, ci="failure")
    board.queue.enqueue(
        JobRequest(
            repo=pr.repo,
            ticket_id=pr.number,
            action="build",
            recipient="claude",
            source_comment_id="build2",
            timeout_seconds=5400,
            head=older,
        ),
        dedupe_key="build2",
    )
    rebuild = board.claim("claude")
    board.queue.finish(rebuild.id, owner="claude", handoff_comment_id="red-ready")
    board.ready(pr, older, "red-ready")
    board.poll()
    revise = VERDICT.format(round="1/3", head=older, verdict="REVISE", risk="high")
    board.post(pr, "verdict-real", revise, "codex-login")
    board.poll()

    fixes = _active_fixes(board)
    assert [job["source_comment_id"] for job in fixes] == ["verdict-real"]
    assert ci_failure(fixes[0]) == {"failed_checks": ["pytest"]}


@pytest.mark.job_bound
@pytest.mark.parametrize("upgrade", ["missing-table", "existing-table"])
@pytest.mark.parametrize(
    ("body", "outcome"),
    [("None.", "no-questions"), ("1. Should the retry cap be 3?", "waiting-max")],
)
def test_a_hidden_registered_questions_handoff_never_answers_the_review(
    tmp_path: Path, upgrade: str, body: str, outcome: str
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = board.open("42", older)
    pr.push(older, ci="pending")
    board.go(pr, "go")
    board.poll()
    build = board.claim("claude")
    board.queue.finish(build.id, owner="claude", handoff_comment_id="ready-older")
    board.ready(pr, older, "ready-older")
    board.poll()
    review = board.claim("codex")
    board.queue.finish(review.id, owner="codex", handoff_comment_id="review-questions")
    pr.push(sha(2), ci="pending")
    board.post(
        pr,
        "review-questions",
        f"<!-- orch:v1 kind=questions from=codex-bot role=reviewer -->\n## Questions\n{body}\n",
        "codex-login",
    )
    board.poll()
    assert board.outcome("review-questions") == outcome
    _hide(pr, "review-questions")
    _upgrade(board, tmp_path, upgrade, "review-questions")
    pr.push(older, ci="failure")
    board.queue.enqueue(
        JobRequest(
            repo=pr.repo,
            ticket_id=pr.number,
            action="build",
            recipient="claude",
            source_comment_id="build2",
            timeout_seconds=5400,
            head=older,
        ),
        dedupe_key="build2",
    )
    rebuild = board.claim("claude")
    board.queue.finish(rebuild.id, owner="claude", handoff_comment_id="red-ready")
    board.ready(pr, older, "red-ready")
    board.poll()

    assert _active_fixes(board) == []
    assert board.outcome("red-ready") == "ci-red-in-flight"


ORCHESTRATOR_STUCK = "<!-- orch:v1 kind=stuck from=grok-bot role=orchestrator state=stuck -->\nStuck.\n"


def _red_ready_after_a_hidden_stuck_questions_handoff(
    tmp_path: Path, upgrade: str, retried_handoff: str
):
    board = board_with(tmp_path, on=True)
    board.dispatcher.hooks = (T7Hook(),)
    older = sha(1)
    pr = board.open("42", older)
    pr.push(older, ci="pending")
    board.go(pr, "go")
    board.poll()
    build = board.claim("claude")
    board.queue.finish(build.id, owner="claude", handoff_comment_id="111111")
    board.ready(pr, older, "111111")
    board.poll()
    assert board.outcome("111111") == "queued-review"
    review = board.claim("codex")
    body = "<!-- orch:v1 kind=questions from=codex-bot role=reviewer -->\nWhich head should I review?"
    raw = {
        "issue_url": "https://api.github.com/repos/owner/repo/issues/42",
        "user": {"login": "codex-login"},
        "body": body,
    }
    assert problems(
        expected_for(review, "codex-bot"), "123456", lambda _: raw, TRUSTED_LOGINS["github"]
    ) == []
    board.post(pr, "stuck-report", ORCHESTRATOR_STUCK, "grok-login")
    board.poll()
    assert board.state("42")["stuck"]
    board.post(pr, "123456", body, "codex-login")
    board.queue.finish(review.id, owner="codex", handoff_comment_id="123456")
    board.now[0] += STOPPED_POLL_SECONDS
    board.poll()
    assert board.outcome("123456") == "ticket-stuck"
    board.post(pr, "retry-review", REASON, "grok-login")
    board.now[0] += STOPPED_POLL_SECONDS
    board.poll()
    assert board.outcome("retry-review") == "queued-retry-review"
    retried = board.claim("codex")
    assert problems(
        expected_for(retried, "codex-bot"), retried_handoff, lambda _: raw, TRUSTED_LOGINS["github"]
    ) == []
    board.queue.finish(retried.id, owner="codex", handoff_comment_id=retried_handoff)
    _hide(pr, "123456")
    _upgrade(board, tmp_path, upgrade, "123456")
    board.dispatcher.hooks = (T7Hook(),)
    pr.push(older, ci="failure")
    board.queue.enqueue(
        JobRequest(
            repo=pr.repo,
            ticket_id=pr.number,
            action="build",
            recipient="claude",
            source_comment_id="build2",
            timeout_seconds=5400,
            head=older,
        ),
        dedupe_key="build2",
    )
    rebuild = board.claim("claude")
    board.queue.finish(rebuild.id, owner="claude", handoff_comment_id="red-ready")
    board.ready(pr, older, "red-ready")
    board.poll()
    return board, pr, older


@pytest.mark.job_bound
@pytest.mark.parametrize("upgrade", ["missing-table", "existing-table"])
def test_a_hidden_questions_handoff_handled_while_stuck_never_answers_the_retried_review(
    tmp_path: Path, upgrade: str
) -> None:
    board, pr, older = _red_ready_after_a_hidden_stuck_questions_handoff(
        tmp_path, upgrade, "654321"
    )

    assert board.outcome("red-ready") == "ci-red-in-flight"
    assert _active_fixes(board) == []
    revise = VERDICT.format(round="1/3", head=older, verdict="REVISE", risk="high")
    board.post(pr, "654321", revise, "codex-login")
    board.poll()

    fixes = _active_fixes(board)
    assert [job["source_comment_id"] for job in fixes] == ["654321"]
    assert ci_failure(fixes[0]) == {"failed_checks": ["pytest"]}


@pytest.mark.job_bound
@pytest.mark.parametrize("upgrade", ["missing-table", "existing-table"])
def test_a_retried_review_reusing_the_hidden_stuck_questions_id_is_not_answered(
    tmp_path: Path, upgrade: str
) -> None:
    board, _pr, _older = _red_ready_after_a_hidden_stuck_questions_handoff(
        tmp_path, upgrade, "123456"
    )

    assert board.outcome("red-ready") == "ci-red-in-flight"
    assert _active_fixes(board) == []
