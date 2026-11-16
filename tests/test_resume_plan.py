from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.queue import Job
from tests.test_approver_credential import MAX, AppFlow
from tests.test_brief import FACTORY_ROOT
from tests.test_dispatcher import ANSWERS, GO, PLAN, QUESTIONS, RETRY, comment, handled_outcome

PR = "https://github.com/owner/repo/pull/42#issuecomment-"
PLAN_URL = f"{PR}500"
PLANNED = "The plan handoff is quoted in the Plan section below ({url}): it is the plan."
UNPLANNED = "No plan handoff comment is linked: build from the ticket."


class ResumeFlow(AppFlow):
    def say(self, comment_id: str, body: str, author: str) -> str:
        self.now[0] += 10
        self.comments.append(comment(comment_id, body, author=author, created_at=self.now[0], url=f"{PR}{comment_id}"))
        self.dispatcher.process(self.snapshot())
        return handled_outcome(self.queue, comment_id)

    def finish_with(self, handoff_id: str) -> Job:
        job = self.queue.claim("claude")
        assert job is not None
        self.queue.finish(job.id, owner="claude", handoff_comment_id=handoff_id)
        return job

    def start_build(self, planned: bool) -> None:
        if not planned:
            assert self.say("1", GO.format(state="building"), "grok-login") == "queued-build"
            return
        assert self.say("1", GO.format(state="planning"), "grok-login") == "queued-plan"
        self.finish_with("500")
        assert self.say("500", PLAN.format(round=1, ac="met"), "claude-login") == "queued-plan-build"

    def resume(self, number: int, by: str) -> None:
        question, answer = f"{number}0", f"{number}1"
        self.finish_with(question)
        assert self.say(question, QUESTIONS, "claude-login") == "waiting-max"
        if by == "go":
            assert self.say(answer, "/go\nKeep going.", MAX) == "resumed-build"
        else:
            assert self.say(answer, ANSWERS, "grok-login") == "resumed-build"


@pytest.mark.parametrize("planned", [True, False], ids=["planned", "unplanned"])
@pytest.mark.parametrize(
    "resumes",
    [("go", "answers"), ("answers", "go"), ("go", "go"), ("answers", "answers"), ("go",), ("answers", "go", "answers")],
    ids="-".join,
)
def test_a_build_keeps_its_plan_through_mixed_resumes(tmp_path: Path, planned: bool, resumes: tuple[str, ...]) -> None:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.start_build(planned)
    for number, by in enumerate(resumes, start=2):
        flow.resume(number, by)

    job = flow.queue.claim("claude")
    assert job is not None and job.request.action == "build"
    detail = json.loads(job.request.detail or "{}")
    assert detail["resume"] is True
    task = render_brief(job, bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    if planned:
        assert PLANNED.format(url=PLAN_URL) in task
        assert "Plan." in task.split("## Plan\n", 1)[1]
    else:
        assert UNPLANNED in task
        assert "## Plan\n" not in task
    assert "Read the plan handoff comment at" not in task
    last = f"{PR}{len(resumes) + 1}1"
    assert detail["instruction"] == last


@pytest.mark.parametrize("planned", [True, False], ids=["planned", "unplanned"])
def test_a_retried_build_keeps_its_plan_and_passes_it_on(tmp_path: Path, planned: bool) -> None:
    flow = ResumeFlow(tmp_path, enabled=True)
    flow.start_build(planned)
    flow.finish_with("400")
    with flow.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET stuck = 1, stuck_reason = 'manual' WHERE repo = 'owner/repo' AND ticket_id = '42'"
        )
    assert flow.say("retry-1", RETRY.format(reason="Max: retry."), MAX) == "queued-retry-build"
    retried = flow.queue.claim("claude")
    assert retried is not None
    flow.queue.finish(retried.id, owner="claude", handoff_comment_id="20")
    assert flow.say("20", QUESTIONS, "claude-login") == "waiting-max"
    assert flow.say("21", ANSWERS, "grok-login") == "resumed-build"
    resumed = flow.queue.claim("claude")
    assert resumed is not None

    for job in (retried, resumed):
        task = render_brief(job, bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
        if planned:
            assert PLANNED.format(url=PLAN_URL) in task
            assert "Plan." in task.split("## Plan\n", 1)[1]
        else:
            assert UNPLANNED in task
            assert "## Plan\n" not in task
