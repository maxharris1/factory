import json
from dataclasses import replace
from pathlib import Path

from lib.dispatcher import Comment
from lib.queue import Job
from tests.test_hooks import (
    HEAD_A,
    VERDICT,
    FakeBot,
    FakeGrokDelivery,
    approve,
    in_review,
    say,
    setup,
    stop,
    ticket,
)

PR_URL = "https://github.com/owner/repo/pull/42"


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
    go = linked(
        say(
            "904",
            "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\nGo.\n",
            "grok-login",
            1,
        )
    )
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
