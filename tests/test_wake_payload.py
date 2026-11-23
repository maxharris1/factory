import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.helpers import _grok_logical_key
from lib.dispatcher import Comment, Dispatcher
from lib.grok_delivery import (
    ClaudeDriverDelivery,
    WebhookGrokDelivery,
    WebhookResult,
    wake_body,
)
from lib.poll_source import _linear_comment
from lib.queue import Job, JobRequest, SQLiteJobQueue, job_from_row
from tests.merge_queue_support import Board, sha
from tests.test_hooks import (
    HEAD_A,
    TRUSTED_LOGINS,
    VERDICT,
    FakeBot,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    approve,
    factory,
    in_review,
    park_followup,
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
    in_review(queue, HEAD_A)
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

    assert FakeBot(queue, "grok").drain() == []
    [notice] = dispatcher.review_actions.notices
    assert f"- link: {bad.url}" in notice.splitlines()


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
        ticket(),
        reason="capped",
        source_comment_id="factory.md@abc1234",
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
    park_followup(queue)
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

    delivery = FakeGrokDelivery()
    restarted, _ = setup(tmp_path, now=now, grok_delivery=delivery)
    restarted.poll_once(FakeSource(on_pr()))

    [wake] = delivery.jobs
    assert detail(wake)["job_id"] == build.id
    assert detail(wake)["link"] == go.url


def test_stuck_wake_after_a_rejected_route_links_the_read_cause(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    routed = replace(factory(), reviewer_harness="cursor", reviewer_model="gpt-5")
    go = linked(
        say("go-1", GO.format(state=" state=building builder=claude"), "grok-login", 1)
    )
    dispatcher.process(replace(on_pr(go), factory=routed))
    build = queue.claim("claude")
    assert build is not None
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")
    with queue.connect() as connection:
        connection.execute("DELETE FROM comment_links")

    delivery = FakeGrokDelivery()
    restarted, queue = setup(tmp_path, grok_delivery=delivery)
    drifted = replace(
        factory(),
        builder_harness="codex",
        reviewer_harness="cursor",
        reviewer_model="claude-sonnet-4",
    )
    ready = linked(
        say(
            "ready-1",
            "<!-- orch:v1 kind=ready from=claude-bot role=builder "
            f"head={HEAD_A} -->\nReady.\n",
            "claude-login",
            2,
        )
    )
    restarted.process(replace(on_pr(go, ready), factory=drifted))
    restarted.reconcile_jobs()

    [wake] = delivery.jobs
    assert wake.request.action == "stuck"
    assert detail(wake)["link"] == go.url
    with queue.connect() as connection:
        remembered = connection.execute(
            "SELECT link FROM comment_links WHERE comment_id = 'go-1'"
        ).fetchone()
    assert remembered["link"] == go.url


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
    park_followup(queue, "AUT-42")
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


def test_a_linear_keyed_clarify_wake_carries_every_id() -> None:
    request = JobRequest(
        repo="aut0-code/aut0",
        ticket_id="AUT-2286",
        action="clarify",
        recipient="grok",
        source_comment_id="7105835030",
        timeout_seconds=60,
        detail=json.dumps({"project_bot": "offer-brief-project"}),
    )
    job = Job("job-2286", "dedupe", request, "running", 1, 1, None, None)
    expected = {
        "job_id": "job-2286",
        "ticket_id": "AUT-2286",
        "repo": "aut0-code/aut0",
        "source_comment_id": "7105835030",
        "action": "clarify",
    }
    key = _grok_logical_key(job)

    body = json.loads(wake_body(job, key))
    assert {name: body[name] for name in expected} == expected

    fired: list[bytes] = []

    def fire(url: str, authorization: str, sent: bytes, timeout: float) -> WebhookResult:
        del url, authorization, timeout
        fired.append(sent)
        return WebhookResult(200, {})

    ClaudeDriverDelivery(
        url_env="FAKE_FIRE_URL",
        token_env="FAKE_FIRE_TOKEN",
        environ={"FAKE_FIRE_URL": "https://fire.test/fire", "FAKE_FIRE_TOKEN": "tok"},
        transport=fire,
    ).deliver(job, logical_key=key)

    [sent] = fired
    text = json.loads(json.loads(sent)["text"])
    assert {name: text[name] for name in expected} == expected


def test_each_wake_delivery_logs_one_matchable_line(tmp_path: Path) -> None:
    environ = {
        "FAKE_GROK_URL": "https://grok-secret.test/hook",
        "FAKE_GROK_KEY": "grok-key-secret",
    }
    statuses = [503, 200]
    received: list[bytes] = []

    def transport(url: str, authorization: str, body: bytes, timeout: float) -> WebhookResult:
        del url, authorization, timeout
        received.append(body)
        return WebhookResult(statuses.pop(0), {})

    lines: list[str] = []
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=WebhookGrokDelivery(
            url_env="FAKE_GROK_URL",
            key_env="FAKE_GROK_KEY",
            environ=environ,
            transport=transport,
        ),
        log=lines.append,
    )
    snapshot = on_pr(linked(say("906", GO.format(state=""), "grok-login", 1)))

    dispatcher.poll_once(FakeSource(snapshot))
    assert len(received) == 1
    assert [line for line in lines if "wake delivered" in line] == []

    dispatcher.poll_once(FakeSource(snapshot))
    assert len(received) == 2
    with queue.connect() as connection:
        [row] = connection.execute("SELECT * FROM jobs WHERE recipient = 'grok'").fetchall()
        job = job_from_row(row, connection)
    request = job.request
    digest = hashlib.sha256(wake_body(job, _grok_logical_key(job))).hexdigest()
    [line] = [line for line in lines if "wake delivered" in line]
    assert line == (
        f"wake delivered: job={job.id} repo={request.repo} ticket={request.ticket_id} "
        f"action={request.action} attempt={request.attempt} body_sha256={digest}"
    )
    assert digest == hashlib.sha256(received[-1]).hexdigest()
    assert "https://" not in line
    assert "Bearer" not in line
    assert not any(value in line for value in environ.values())
