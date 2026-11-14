"""The build job, plus the notes and ready handoff every builder job shares."""

from __future__ import annotations

from lib.brief.common import QUESTIONS_LINES
from lib.queue import Job


def task_lines(job: Job, facts: dict) -> list[str]:
    base = facts["base"] or "<base branch>"
    plan_line = (
        f"Read the plan handoff comment at {facts['plan']}: it is the plan."
        if facts["plan"]
        else "No plan handoff comment is linked: build from the ticket."
    )
    return [
        "Build the ticket. Follow the router in Skills above.",
        f"1. Read the ticket and its brief (Job data). {plan_line}",
        f"2. Work on the ticket's branch (`gitBranchName` in Job data, else a new branch off `{base}`). "
        "Test first.",
        "3. Before `ready`, run the router's \"Before ready\" steps.",
        "4. Push, then post the ready handoff below with the head you pushed.",
        *builder_notes(job),
    ]


def builder_notes(job: Job) -> list[str]:
    notes = [
        "Run test suites in the foreground, however long they take: never in the background. "
        "Print mode stops background tasks after your last turn, and unpushed work is lost.",
        *QUESTIONS_LINES,
    ]
    if job.request.attempt > 1:
        notes.append(
            "This is a retry. The working directory may hold the last attempt's work: check "
            "`git status` and `git log @{u}..` before you start, and build on it rather than redo it."
        )
    return notes


def ready_handoff(bot: str, facts: dict) -> tuple[str, str]:
    # #105: round and risk come from the job, never from the builder.
    round_key = f" round={facts['round']}" if facts["round"] else ""
    risk_key = f" risk={facts['risk']}" if facts["risk"] else ""
    header = (
        f"<!-- orch:v1 kind=ready from={bot} role=builder{round_key} "
        f"head=<pushed-head-sha>{risk_key} -->"
    )
    example = "What changed, the tests and their results, and anything left open."
    return header, example
