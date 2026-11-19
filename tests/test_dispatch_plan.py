from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.dispatcher import TrackedTicket
from lib.queue import Job
from tests.test_brief import FACTORY_ROOT
from tests.test_dispatcher import ANSWERS, GO, VERDICT
from tests.test_plan_size import SizeFlow, estimated
from tests.test_resume_plan import ResumeFlow
from tests.test_rounds_and_caps import HEADS, round_of

FLAG = (
    "Size flag: the PR changes {changed_lines} lines, over both the plan's estimate of {estimate} x1.35 "
    "({padded}) and the 400-line plan line. It is not a finding and does not block. Use the estimate in "
    "your closing `Size:` line."
)


class Lines(ResumeFlow):
    changed_lines: int | None = None

    def snapshot(self) -> TrackedTicket:
        return replace(super().snapshot(), changed_lines=self.changed_lines)


class Sized(Lines, SizeFlow):
    pass


def review(flow: Lines, changed_lines: int | None) -> Job:
    flow.finish_with("build-done")
    flow.changed_lines = changed_lines
    assert flow.ready(HEADS[1]) == "queued-review"
    job = flow.queue.claim("codex")
    assert job is not None and job.request.action == "review"
    return job


def planned_review(tmp_path: Path, estimate: int, changed_lines: int | None) -> tuple[Sized, Job]:
    flow = Sized(tmp_path)
    assert flow.plan("500", "ac=met", estimated(estimate)) == "queued-plan-build"
    return flow, review(flow, changed_lines)


def size_flag(job: Job) -> dict | None:
    return json.loads(job.request.detail or "{}").get("size_flag")


def brief(job: Job) -> str:
    return render_brief(job, bot="codex-bot", factory_root=FACTORY_ROOT, context=None)


def test_a_ready_far_over_its_estimate_carries_the_flag_into_the_review_brief(tmp_path: Path) -> None:
    _flow, job = planned_review(tmp_path, 290, 401)

    assert size_flag(job) == {"changed_lines": 401, "estimate": 290}
    assert brief(job).count(FLAG.format(changed_lines=401, estimate=290, padded=392)) == 1


@pytest.mark.parametrize(("estimate", "changed_lines"), [(290, 400), (100, 390), (290, None)])
def test_a_ready_within_the_line_or_of_unknown_size_has_no_flag(
    tmp_path: Path, estimate: int, changed_lines: int | None
) -> None:
    _flow, job = planned_review(tmp_path, estimate, changed_lines)

    assert size_flag(job) is None
    assert "Size flag:" not in brief(job)


@pytest.mark.parametrize(("changed_lines", "flag"), [(540, None), (541, {"changed_lines": 541, "estimate": 400})])
def test_a_plan_parked_for_size_and_resumed_is_judged_on_its_padded_estimate(
    tmp_path: Path, changed_lines: int, flag: dict | None
) -> None:
    flow = Sized(tmp_path)
    assert flow.plan("500", "ac=met", estimated(400)) == "waiting-max-size"
    assert flow.say("501", ANSWERS, "grok-login") == "resumed-build"

    assert size_flag(review(flow, changed_lines)) == flag


def test_a_ticket_built_without_a_plan_has_no_flag(tmp_path: Path) -> None:
    flow = Lines(tmp_path, enabled=True)
    assert flow.say("1", GO.format(state="building"), "grok-login") == "queued-build"

    assert size_flag(review(flow, 900)) is None


@pytest.mark.parametrize("changed_lines", [400, 401])
def test_the_flag_does_not_change_how_an_approve_is_handled(tmp_path: Path, changed_lines: int) -> None:
    flow, job = planned_review(tmp_path, 290, changed_lines)
    flow.queue.finish(job.id, owner="codex", handoff_comment_id="verdict-1")
    approve = VERDICT.format(round=round_of(job), head=HEADS[1], verdict="APPROVE", risk="high")

    assert flow.post("verdict-1", approve, "codex-login") == "merged"
    assert flow.state()["workflow_state"] == "merged"
