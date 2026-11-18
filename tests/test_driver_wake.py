"""A clarify wake nobody answers is re-delivered once, then flagged (#916)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.attention import DRIVER_WAKE_UNANSWERED
from lib.job_state import JobStateConfig
from lib.queue import Job
from tests.test_approver_credential import MAX, load, orch_text
from tests.test_dispatcher import ANSWERS, GO, QUESTIONS, comment
from tests.test_plan_size import SizeFlow
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
        (OK, "grok-login"),
    ],
    ids=["answers", "go", "slash-go", "ok"],
)
def test_answer_before_timeout_cancels(tmp_path: Path, body: str, author: str) -> None:
    flow, t0 = asked(tmp_path)
    post(flow, t0 + 10 * MINUTE, "answer", body, author)

    for minutes in (20, 30, 40, 45):
        poll(flow, t0 + minutes * MINUTE)

    assert len(clarify_deliveries(flow)) == 1
    assert flags(flow) == []


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


def capped(tmp_path: Path) -> ResumeFlow:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.to_cap()
    return flow


def parked_over_size(tmp_path: Path) -> ResumeFlow:
    flow = SizeFlow(tmp_path)
    assert flow.plan("500", "ac=met lines=401") == "waiting-max-size"
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
