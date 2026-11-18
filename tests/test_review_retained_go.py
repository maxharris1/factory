from __future__ import annotations

from pathlib import Path

from tests.merge_queue_support import sha
from tests.test_dispatch_ready_comments import _restart
from tests.test_review_before_ci import (
    board_with,
    built_with_ci_pending,
    ci_failure,
    reviewed_at_pending_ci,
)


def test_a_hidden_untrusted_go_never_answers_the_review_after_an_upgrade(
    tmp_path: Path,
) -> None:
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older)
    pr.push(sha(2), ci="pending")
    board.go(pr, "untrusted-go", author="stranger")
    board.poll()
    assert board.outcome("untrusted-go") == "rejected-go-author"
    pr.comments.remove(next(item for item in pr.comments if item.id == "untrusted-go"))
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
    assert ci_failure(fixes[0]) == {"failed_checks": ["pytest"]}
