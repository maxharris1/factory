from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.attention import DRIVER_WAKE_UNANSWERED
from lib.dispatcher import Dispatcher, PollCycleFailed
from lib.github_actions import GitHubAdapterError
from lib.job_state import JobStateConfig
from lib.orch_yml import DriverInbox, Project
from lib.queue import Job
from tests.test_approver_credential import MAX, load, orch_text
from tests.test_dispatcher import ANSWERS, GO, QUESTIONS, FailOnceGrokDelivery, comment, handled_outcome
from tests.test_driver_inbox import InboxIssue
from tests.test_poll_once_errors import BrokenSource
from tests.test_plan_size import SizeFlow, estimated
from tests.test_resume_plan import ResumeFlow
from tests.test_rounds_and_caps import HEADS

MINUTE = 60.0
OK = f"<!-- orch:v1 kind=ok from=grok-bot role=orchestrator head={HEADS[0]} -->\nOK.\n"


def poll(flow: ResumeFlow, at: float) -> None:
    flow.now[0] = at
    flow.dispatcher.process(flow.snapshot())
    flow.dispatcher.reconcile_jobs()


def post(flow: ResumeFlow, at: float, comment_id: str, body: str, author: str) -> None:
    flow.now[0] = at
    flow.comments.append(comment(comment_id, body, author=author, created_at=at))
    flow.dispatcher.process(flow.snapshot())


def clarify_deliveries(flow: ResumeFlow) -> list[Job]:
    return [job for job in flow.dispatcher.grok_delivery.jobs if job.request.action == "clarify"]


def clarify_keys(flow: ResumeFlow) -> list[str]:
    return [
        key
        for job, key in zip(flow.dispatcher.grok_delivery.jobs, flow.dispatcher.grok_delivery.logical_keys)
        if job.request.action == "clarify"
    ]


def finished_at(flow: ResumeFlow, job: Job) -> float:
    with flow.queue.connect() as connection:
        return float(connection.execute("SELECT finished_at FROM jobs WHERE id = ?", (job.id,)).fetchone()[0])


def flags(flow: ResumeFlow) -> list[sqlite3.Row]:
    with flow.queue.connect() as connection:
        return connection.execute(
            "SELECT repo, ticket_id, detail, raised_at FROM needs_attention WHERE reason = ?",
            (DRIVER_WAKE_UNANSWERED,),
        ).fetchall()


def grok_job_count(flow: ResumeFlow) -> int:
    with flow.queue.connect() as connection:
        return int(connection.execute("SELECT count(*) FROM jobs WHERE recipient = 'grok'").fetchone()[0])


def asked(tmp_path: Path) -> tuple[ResumeFlow, float]:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.start_build(planned=False)
    flow.finish_with("10")
    assert flow.say("10", QUESTIONS, "claude-login") == "waiting-max"
    flow.dispatcher.reconcile_jobs()
    [wake] = clarify_deliveries(flow)
    return flow, finished_at(flow, wake)


def rewoken(tmp_path: Path) -> tuple[ResumeFlow, float, float]:
    flow, t0 = asked(tmp_path)
    poll(flow, t0 + 20 * MINUTE)
    _, rewake = clarify_deliveries(flow)
    return flow, t0, finished_at(flow, rewake)


def test_unanswered_wake_redelivers_once(tmp_path: Path) -> None:
    flow, t0 = asked(tmp_path)

    poll(flow, t0 + 20 * MINUTE - 1)
    assert len(clarify_deliveries(flow)) == 1

    poll(flow, t0 + 20 * MINUTE)
    first, rewake = clarify_deliveries(flow)
    assert (first.request.attempt, rewake.request.attempt) == (1, 2)
    assert rewake.request.source_comment_id == first.request.source_comment_id == "10"
    first_key, rewake_key = clarify_keys(flow)
    assert rewake_key == first_key

    poll(flow, t0 + 25 * MINUTE)
    poll(flow, t0 + 30 * MINUTE)
    assert len(clarify_deliveries(flow)) == 2
    assert flags(flow) == []


def test_second_timeout_flags_once(tmp_path: Path) -> None:
    flow, t0, t1 = rewoken(tmp_path)
    _, rewake = clarify_deliveries(flow)

    poll(flow, t1 + 20 * MINUTE - 1)
    assert flags(flow) == []

    poll(flow, t1 + 20 * MINUTE)
    [flag] = flags(flow)
    assert (flag["repo"], flag["ticket_id"]) == ("owner/repo", "42")
    assert rewake.id in flag["detail"]
    assert flow.head in flag["detail"]
    assert f"{int((t1 + 20 * MINUTE - t0) // MINUTE)} min" in flag["detail"]

    poll(flow, t1 + 30 * MINUTE)
    poll(flow, t1 + 60 * MINUTE)
    assert [(row["detail"], row["raised_at"]) for row in flags(flow)] == [(flag["detail"], flag["raised_at"])]
    assert len(clarify_deliveries(flow)) == 2


@pytest.mark.parametrize(
    ("body", "author"),
    [
        (ANSWERS, "grok-login"),
        (GO.format(state="building"), "grok-login"),
        ("/go\nKeep going.", MAX),
    ],
    ids=["answers", "go", "slash-go"],
)
def test_answer_before_timeout_cancels(tmp_path: Path, body: str, author: str) -> None:
    flow, t0 = asked(tmp_path)
    post(flow, t0 + 10 * MINUTE, "answer", body, author)

    for minutes in (20, 30, 40, 45):
        poll(flow, t0 + minutes * MINUTE)

    assert len(clarify_deliveries(flow)) == 1
    assert flags(flow) == []


def test_ok_is_not_an_answer(tmp_path: Path) -> None:
    flow, t0 = asked(tmp_path)
    post(flow, t0 + 10 * MINUTE, "ok", OK, "grok-login")

    poll(flow, t0 + 20 * MINUTE)
    assert len(clarify_deliveries(flow)) == 2


def test_edited_answers_does_not_cancel(tmp_path: Path) -> None:
    flow, t0 = asked(tmp_path)
    at = t0 + 10 * MINUTE
    flow.now[0] = at
    flow.comments.append(replace(comment("answer", ANSWERS, author="grok-login", created_at=at), edited_at=at + 1))
    flow.dispatcher.process(flow.snapshot())
    assert handled_outcome(flow.queue, "answer") == "rejected-edited"

    for minutes in (20, 40, 60):
        poll(flow, t0 + minutes * MINUTE)
    assert len(clarify_deliveries(flow)) == 2
    assert len(flags(flow)) == 1


def test_answer_after_rewake_clears_flag(tmp_path: Path) -> None:
    flow, _, t1 = rewoken(tmp_path)
    poll(flow, t1 + 20 * MINUTE)
    assert len(flags(flow)) == 1

    assert flow.say("answer", ANSWERS, "grok-login") == "resumed-build"
    poll(flow, t1 + 25 * MINUTE)
    assert flags(flow) == []

    poll(flow, t1 + 60 * MINUTE)
    assert flags(flow) == []
    assert len(clarify_deliveries(flow)) == 2


def test_untrusted_answers_does_not_cancel(tmp_path: Path) -> None:
    flow, t0 = asked(tmp_path)
    post(flow, t0 + 10 * MINUTE, "answer", ANSWERS, "mallory")

    poll(flow, t0 + 20 * MINUTE)
    assert len(clarify_deliveries(flow)) == 2


def test_retried_first_wake_gets_its_own_rewake(tmp_path: Path) -> None:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.start_build(planned=False)
    flow.finish_with("10")
    assert flow.say("10", QUESTIONS, "claude-login") == "waiting-max"
    delivery = flow.dispatcher.grok_delivery

    class FailFirstAttempt:
        jobs = delivery.jobs

        def deliver(self, job: Job, *, logical_key: str) -> str:
            if job.request.attempt == 1:
                raise RuntimeError("transport down")
            return delivery.deliver(job, logical_key=logical_key)

    flow.dispatcher.grok_delivery = FailFirstAttempt()
    flow.dispatcher.reconcile_jobs()
    poll(flow, flow.now[0] + flow.snapshot().job_timeout_minutes * MINUTE)
    [retry] = clarify_deliveries(flow)
    assert retry.request.attempt == 2

    poll(flow, finished_at(flow, retry) + 20 * MINUTE)
    assert len(clarify_deliveries(flow)) == 2
    assert flags(flow) == []


def test_new_head_after_rewake_flags_instead_of_waking_again(tmp_path: Path) -> None:
    flow, t0, t1 = rewoken(tmp_path)
    assert t1 == t0 + 20 * MINUTE
    flow.head = HEADS[1]

    poll(flow, t0 + 40 * MINUTE)
    assert len(clarify_deliveries(flow)) == 2
    [flag] = flags(flow)
    assert HEADS[1] in flag["detail"]


def test_repeated_head_changes_keep_one_rewake_and_flag_on_time(tmp_path: Path) -> None:
    flow, t0 = asked(tmp_path)
    raised = []
    for step, minutes in enumerate(range(5, 65, 5), start=1):
        flow.head = HEADS[step % len(HEADS)]
        poll(flow, t0 + minutes * MINUTE)
        if flags(flow) and not raised:
            raised.append(minutes)
    assert [finished_at(flow, job) for job in clarify_deliveries(flow)] == [t0, t0 + 20 * MINUTE]
    assert raised == [40]
    assert len(flags(flow)) == 1


def settle_by_answer(flow: ResumeFlow) -> None:
    post(flow, flow.now[0] + MINUTE, "answer", ANSWERS, "grok-login")
    assert handled_outcome(flow.queue, "answer") == "resumed-build"


def settle_by_stuck(flow: ResumeFlow) -> None:
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET stuck = 1, stuck_reason = 'manual'")


def settle_to(state: str):
    def settle(flow: ResumeFlow) -> None:
        with flow.queue.connect() as connection:
            connection.execute("UPDATE ticket_state SET workflow_state = ?", (state,))

    return settle


@pytest.mark.parametrize(
    "settle",
    [settle_by_answer, settle_by_stuck, settle_to("capped"), settle_to("awaiting-ok")],
    ids=["answer", "stuck", "capped", "awaiting-ok"],
)
def test_queued_rewake_is_cancelled_once_settled(tmp_path: Path, settle) -> None:
    flow, t0 = asked(tmp_path)
    delivery = flow.dispatcher.grok_delivery
    flow.dispatcher.grok_delivery = FailOnceGrokDelivery()
    poll(flow, t0 + 20 * MINUTE)
    [failed] = flow.dispatcher.grok_delivery.jobs
    with flow.queue.connect() as connection:
        assert connection.execute("SELECT state FROM jobs WHERE id = ?", (failed.id,)).fetchone()[0] == "queued"

    settle(flow)
    flow.dispatcher.grok_delivery = delivery
    poll(flow, t0 + 22 * MINUTE)
    assert len(clarify_deliveries(flow)) == 1


def inbox_flow(tmp_path: Path, issue: InboxIssue) -> ResumeFlow:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.dispatcher.review_actions = issue
    flow.dispatcher.wake_routes = {
        "owner/repo": Project("owner/repo", driver_inbox=DriverInbox("owner/repo", 7), grok_webhook=False)
    }
    flow.start_build(planned=False)
    flow.finish_with("10")
    assert flow.say("10", QUESTIONS, "claude-login") == "waiting-max"
    flow.dispatcher.reconcile_jobs()
    return flow


def clarify_posts(issue: InboxIssue) -> list[str]:
    return [body for _, _, body in issue.posts if "**Wake: clarify**" in body]


def test_inbox_timeout_starts_at_the_post(tmp_path: Path) -> None:
    issue = InboxIssue()
    issue.fail = GitHubAdapterError("inbox down")
    flow = inbox_flow(tmp_path, issue)
    t0 = flow.now[0]

    for minutes in (20, 40):
        poll(flow, t0 + minutes * MINUTE)
    assert grok_job_count(flow) == 1
    assert flags(flow) == []

    issue.fail = None
    poll(flow, t0 + 50 * MINUTE)
    assert len(clarify_posts(issue)) == 1

    poll(flow, t0 + 70 * MINUTE - 1)
    assert len(clarify_posts(issue)) == 1
    poll(flow, t0 + 70 * MINUTE)
    assert len(clarify_posts(issue)) == 2


def test_rewake_waiting_in_the_inbox_is_dropped_once_answered(tmp_path: Path) -> None:
    issue = InboxIssue()
    flow = inbox_flow(tmp_path, issue)
    t0 = flow.now[0]
    assert len(clarify_posts(issue)) == 1

    issue.fail = GitHubAdapterError("inbox down")
    poll(flow, t0 + 20 * MINUTE)
    assert grok_job_count(flow) == 2

    settle_by_answer(flow)
    issue.fail = None
    poll(flow, t0 + 25 * MINUTE)
    assert len(clarify_posts(issue)) == 1


def test_failed_read_never_rewakes(tmp_path: Path) -> None:
    flow, t0 = asked(tmp_path)
    flow.now[0] = t0 + 20 * MINUTE
    with pytest.raises(PollCycleFailed):
        flow.dispatcher.poll_once(BrokenSource(broken="tracked"))
    assert len(clarify_deliveries(flow)) == 1
    assert flags(flow) == []


def test_restarts_keep_one_rewake_and_one_flag(tmp_path: Path) -> None:
    flow, t0 = asked(tmp_path)
    for minutes in (20, 40, 60):
        prior = flow.dispatcher
        flow.dispatcher = Dispatcher(
            flow.queue.path,
            flow.queue,
            trusted_logins=prior.trusted_logins,
            review_actions=prior.review_actions,
            grok_delivery=prior.grok_delivery,
            approvals=prior.approvals,
        )
        poll(flow, t0 + minutes * MINUTE)
    assert len(clarify_deliveries(flow)) == 2
    assert len(flags(flow)) == 1


def capped(tmp_path: Path) -> ResumeFlow:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.to_cap()
    return flow


def parked_over_size(tmp_path: Path) -> ResumeFlow:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met", estimated(401)) == "waiting-max-size"
    return flow


def stuck_on_question(tmp_path: Path) -> ResumeFlow:
    flow, _ = asked(tmp_path)
    with flow.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET stuck = 1, stuck_reason = 'manual' WHERE repo = 'owner/repo' AND ticket_id = '42'"
        )
    return flow


@pytest.mark.parametrize("held", [capped, parked_over_size, stuck_on_question], ids=["capped", "plan-size", "stuck"])
def test_held_or_capped_never_rewakes(tmp_path: Path, held) -> None:
    flow = held(tmp_path)
    flow.dispatcher.reconcile_jobs()
    start = flow.now[0]
    jobs = grok_job_count(flow)

    for minutes in (20, 40, 45):
        poll(flow, start + minutes * MINUTE)

    assert grok_job_count(flow) == jobs
    assert flags(flow) == []


def test_parked_over_size_wakes_the_driver_once(tmp_path: Path) -> None:
    flow = parked_over_size(tmp_path)
    flow.dispatcher.reconcile_jobs()
    assert [job.request.source_comment_id for job in clarify_deliveries(flow)] == ["500"]


@pytest.mark.parametrize(
    ("extra", "seconds"),
    [("", 1200.0), ("job_states:\n  driver_wake_minutes: 5\n", 300.0)],
    ids=["omitted", "five"],
)
def test_driver_wake_minutes_config(tmp_path: Path, extra: str, seconds: float) -> None:
    config, findings = load(tmp_path, orch_text(extra=extra))
    assert config is not None, findings
    assert config.job_states.driver_wake_seconds == seconds


@pytest.mark.parametrize("value", ["0", "x", "-1", "1.5", '"5"'])
def test_bad_driver_wake_minutes_are_refused(tmp_path: Path, value: str) -> None:
    config, findings = load(tmp_path, orch_text(extra=f"job_states:\n  driver_wake_minutes: {value}\n"))
    assert config is None
    assert any(
        item.message == "job_states.driver_wake_minutes must be a positive integer (minutes)" for item in findings
    ), findings


def test_configured_timeout_drives_rewake(tmp_path: Path) -> None:
    config, findings = load(tmp_path, orch_text(extra="job_states:\n  driver_wake_minutes: 5\n"))
    assert config is not None, findings
    flow, t0 = asked(tmp_path)
    flow.dispatcher.job_states = config.job_states

    poll(flow, t0 + 5 * MINUTE - 1)
    assert len(clarify_deliveries(flow)) == 1

    poll(flow, t0 + 5 * MINUTE)
    assert [job.request.attempt for job in clarify_deliveries(flow)] == [1, 2]
