from __future__ import annotations

from pathlib import Path

import pytest

from lib.dispatch.helpers import _spread
from tests.merge_queue_support import PR, Board, sha
from tests.test_dispatcher import RETRY

REASON = RETRY.format(reason="Max: retry.")


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
    while (b.dispatcher._cycle + 2 + _spread("owner/repo", "42")) % 5 != 0:
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

    assert b.outcome("retry-update") == "retried-merge-queue"
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
