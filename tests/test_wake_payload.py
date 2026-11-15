import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import Comment
from lib.poll_source import _linear_comment
from lib.queue import Job
from tests.merge_queue_support import Board, sha
from tests.test_hooks import (
    HEAD_A,
    VERDICT,
    FakeBot,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    approve,
    in_review,
    say,
    setup,
    stop,
    ticket,
)

PR_URL = "https://github.com/owner/repo/pull/42"
LINEAR_URL = "https://linear.app/team/issue/AUT-42/title"
GO = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator{state} -->\nGo.\n"


def linked(comment: Comment) -> Comment:
    return replace(comment, url=f"{PR_URL}#issuecomment-{comment.id}")


def on_pr(*comments: Comment, ship: str = "after:tracy-site"):
    return replace(ticket(*comments, ship=ship), ticket_url=PR_URL, pr_number="42")


def detail(job: Job) -> dict[str, object]:
    parsed = json.loads(str(job.request.detail))
    assert isinstance(parsed, dict)
    return parsed


def test_blocked_verdict_wake_links_the_verdict(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    blocked = linked(
        say("901", VERDICT.format(head=HEAD_A, verdict="BLOCKED"), "codex-login", 1)
    )

    dispatcher.process(on_pr(blocked))

    [wake] = FakeBot(queue, "grok").drain()
    assert wake.request.action == "blocked"
    assert detail(wake)["link"] == blocked.url
    assert detail(wake)["project_bot"] == "grok-bot"


def test_unparsed_wake_links_the_unparsed_comment(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    bad = linked(say("902", "<!-- orch:v1 kind=nonsense -->\nHuh.\n", "claude-login", 1))

    dispatcher.process(on_pr(bad))

    [wake] = FakeBot(queue, "grok").drain()
    assert wake.request.action == "unparsed"
    assert detail(wake)["link"] == bad.url


def test_stuck_wake_links_the_stuck_comment(tmp_path: Path) -> None:
    delivery = FakeGrokDelivery()
    dispatcher, _ = setup(tmp_path, grok_delivery=delivery)
    stuck = linked(stop("903", 1))

    dispatcher.process(on_pr(stuck))
    dispatcher.reconcile_jobs()

    [wake] = delivery.jobs
    assert wake.request.action == "stuck"
    assert detail(wake)["link"] == stuck.url


def test_wake_with_no_known_comment_still_carries_a_link(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)

    dispatcher.wake_grok_once(
        repo="owner/repo",
        ticket_id="42",
        reason="refused",
        source_comment_id="factory.md@abc1234",
        timeout_seconds=5400,
        detail="missing required field 'ship'",
    )

    [wake] = FakeBot(queue, "grok").drain()
    assert detail(wake) == {
        "detail": "missing required field 'ship'",
        "link": "comment:factory.md@abc1234",
    }


def test_job_failed_twice_wake_links_the_queueing_comment_and_names_the_job(
    tmp_path: Path,
) -> None:
    now = [100.0]
    delivery = FakeGrokDelivery()
    dispatcher, queue = setup(tmp_path, now=now, grok_delivery=delivery)
    go = linked(say("904", GO.format(state=" state=building"), "grok-login", 1))
    dispatcher.process(on_pr(go))
    failed_id = ""
    for at in (110.0, 130.0):
        now[0] = at
        build = queue.claim("claude")
        assert build is not None
        failed_id = build.id
        queue.finish(build.id, owner="claude", handoff_comment_id=None)
        now[0] = at + 5
        dispatcher.reconcile_jobs()

    [wake] = delivery.jobs
    assert wake.request.action == "stuck"
    assert detail(wake)["link"] == go.url
    assert detail(wake)["job_id"] == failed_id


def test_done_wake_links_the_approve_and_names_the_follow_up_owner(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    in_review(queue, HEAD_A)
    verdict = linked(approve("905", HEAD_A, 1))

    dispatcher.process(on_pr(verdict, ship="merge"))

    [wake] = [job for job in FakeBot(queue, "grok").drain() if job.request.action == "done"]
    assert detail(wake)["link"] == verdict.url
    assert detail(wake)["follow_up_owner"] == "grok-bot"


def test_clarify_wake_for_a_go_without_state_links_the_go(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = linked(say("906", GO.format(state=""), "grok-login", 1))

    dispatcher.process(on_pr(go))

    [wake] = FakeBot(queue, "grok").drain()
    assert wake.request.action == "clarify"
    assert detail(wake)["link"] == go.url


@pytest.mark.parametrize("answered", [False, True])
def test_clarify_wakes_for_questions_and_answers_link_their_comment(
    tmp_path: Path, answered: bool
) -> None:
    dispatcher, queue = setup(tmp_path)
    question = linked(
        say(
            "907",
            "<!-- orch:v1 kind=questions from=claude-bot role=builder -->\nWhich side?\n",
            "claude-login",
            1,
        )
    )
    dispatcher.process(on_pr(question))
    cause = question
    if answered:
        FakeBot(queue, "grok").drain()
        cause = linked(
            say(
                "908",
                "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator -->\nLeft.\n",
                "grok-login",
                2,
            )
        )
        dispatcher.process(on_pr(question, cause))

    [wake] = FakeBot(queue, "grok").drain()
    assert wake.request.action == "clarify"
    assert detail(wake)["link"] == cause.url


@pytest.mark.parametrize(
    ("failing_state_label", "queued_before_rollout"),
    [(None, False), ("building", False), (None, True)],
)
def test_job_failed_twice_wake_links_the_queueing_comment_after_a_restart(
    tmp_path: Path,
    failing_state_label: str | None,
    queued_before_rollout: bool,
) -> None:
    now = [100.0]
    actions = FakeReviewActions()
    if failing_state_label is not None:
        actions.state_failures.add(failing_state_label)
    dispatcher, queue = setup(tmp_path, now=now, actions=actions)
    go = linked(say("909", GO.format(state=" state=building"), "grok-login", 1))
    dispatcher.poll_once(FakeSource(on_pr(go)))
    if queued_before_rollout:
        with queue.connect() as connection:
            connection.execute("DELETE FROM comment_links")
        dispatcher, queue = setup(tmp_path, now=now)
        dispatcher.poll_once(FakeSource(on_pr(go)))
    for at in (110.0, 130.0):
        now[0] = at
        build = queue.claim("claude")
        assert build is not None
        queue.finish(build.id, owner="claude", handoff_comment_id=None)
        if at == 110.0:
            dispatcher.reconcile_jobs()

    class Unavailable(FakeSource):
        def tracked_tickets(self, refs):
            raise RuntimeError("ticket API temporarily unavailable")

    delivery = FakeGrokDelivery()
    restarted, _ = setup(tmp_path, now=now, grok_delivery=delivery)
    restarted.poll_once(Unavailable())

    [wake] = delivery.jobs
    assert detail(wake)["job_id"] == build.id
    assert detail(wake)["link"] == go.url


def test_done_wake_on_linear_falls_back_to_the_ticket_url(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.trusted_logins["linear"]["linear:reviewer-id"] = frozenset({"codex-bot"})
    approval = _linear_comment(
        {
            "id": "d5abb520-f2c4-4d85-b534-f5a3ed449000",
            "body": VERDICT.format(head=HEAD_A, verdict="APPROVE"),
            "createdAt": "2026-11-14T01:00:00+00:00",
            "user": {"id": "reviewer-id"},
        }
    )
    in_review(queue, HEAD_A, ticket_id="AUT-42")
    linear = ticket(approval, ship="merge", ticket_id="AUT-42")
    linear = replace(
        linear,
        factory=replace(linear.factory, tickets="linear"),
        ticket_url=LINEAR_URL,
        pr_number="42",
        grok_bot="project-bot",
    )

    dispatcher.process(linear)

    [wake] = [job for job in FakeBot(queue, "grok").drain() if job.request.action == "done"]
    assert detail(wake)["follow_up_owner"] == "project-bot"
    assert detail(wake)["link"] == LINEAR_URL


def test_failed_update_wake_names_the_failed_job(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    board.fail("claude")
    board.poll()
    board.poll()
    failed = board.fail("claude")
    board.poll()

    [wake] = board.wakes("stuck")
    assert wake["job_id"] == failed.id
