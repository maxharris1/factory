from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult
from lib.handoff_check import expected_for, problems
from tests.test_dispatcher import GO, READY, VERDICT, comment, factory, handled_outcome, setup, ticket
from tests.test_job_bound_accept import HEAD, _claim, _enqueue, _jobs, _ready, _wakes

pytestmark = pytest.mark.job_bound


@pytest.mark.parametrize("failed", [False, True])
def test_config_change_cannot_register_another_bots_verdict(tmp_path: Path, failed: bool) -> None:
    dispatcher, queue = setup(tmp_path)
    old = replace(factory(), builder_harness="codex", reviewer_harness="claude")
    go = comment("123", GO.format(state="building"), author="grok-login", created_at=150)

    def snapshot(config, *comments):
        return ticket(
            go, *comments, factory_config=config, head=HEAD, ci={HEAD: CIResult("success")}
        )

    dispatcher.process(snapshot(old))
    build = _claim(queue, "codex")
    queue.finish(build.id, owner="codex", handoff_comment_id="234")
    ready = comment(
        "234",
        READY.format(head=HEAD).replace("claude-bot", "codex-bot"),
        author="codex-login",
        created_at=200,
    )
    dispatcher.process(snapshot(old, ready))
    review = _claim(queue, "claude")
    assert review.request.action == "review"
    verdict = comment(
        "456",
        VERDICT.format(round="1/3", head=HEAD, verdict="APPROVE", risk="high"),
        author="codex-login",
        created_at=300,
    )

    def read(path):
        if "/issues/comments/" in path:
            return {
                "body": verdict.body,
                "user": {"login": verdict.author},
                "issue_url": "https://api.github.com/repos/owner/repo/issues/42",
            }
        return {"head": {"sha": HEAD, "ref": "branch"}}

    errors = problems(expected_for(review, "claude-bot"), "456", read, dispatcher.trusted_logins)
    assert "from=codex-bot is not claude-bot" in errors
    if failed:
        queue.finish(review.id, owner="claude", handoff_comment_id=None)
    new_snapshot = snapshot(factory(), ready, verdict)
    dispatcher.process(new_snapshot)
    if failed:
        dispatcher.reconcile_jobs()
        dispatcher.process(new_snapshot)

    with queue.connect() as connection:
        registered = connection.execute(
            "SELECT handoff_comment_id FROM jobs WHERE id = ?", (review.id,)
        ).fetchone()[0]
    assert registered is None
    assert handled_outcome(queue, "456") == "rejected-unregistered-verdict"
    assert len([detail for detail in _wakes(queue) if "456" in detail]) == 1


def test_running_build_for_another_bot_leaves_a_ready_unregistered(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    _enqueue(queue, "build", "codex")
    _claim(queue, "codex")
    snapshot = _ready("ready-stray")
    snapshot = replace(snapshot, factory_md=snapshot.factory)

    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    assert handled_outcome(queue, "ready-stray") == "rejected-unregistered-ready"
    assert _jobs(queue, "review") == 0
    assert len([detail for detail in _wakes(queue) if "ready-stray" in detail]) == 1
