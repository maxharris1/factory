from __future__ import annotations

from pathlib import Path

import pytest

from lib.hook_t7 import T7Hook
from lib.queue import JobRequest
from tests.merge_queue_support import VERDICT, sha
from tests.test_dispatch_ready_comments import _restart
from tests.test_review_before_ci import (
    CI_LOG,
    board_with,
    built_with_ci_pending,
    instruction,
    reviewed_at_pending_ci,
)

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
    assert CI_LOG in instruction(fixes[0])


@pytest.mark.parametrize("upgrade", ["missing-table", "existing-table"])
def test_a_hidden_blocked_verdict_still_answers_its_review(tmp_path: Path, upgrade: str) -> None:
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
    assert board.outcome("blocked-verdict") == "woke-grok-blocked"
    _hide(pr, "blocked-verdict")
    _upgrade(board, tmp_path, upgrade, "blocked-verdict")
    pr.push(sha(2), ci="pending")
    board.poll()
    pr.push(older, ci="failure")
    board.ready(pr, older, "red-ready")
    board.poll()

    fixes = _active_fixes(board)
    assert [job["source_comment_id"] for job in fixes] == ["red-ready"]
    assert CI_LOG in instruction(fixes[0])


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
    assert CI_LOG in instruction(fixes[0])


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
