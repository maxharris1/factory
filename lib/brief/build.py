"""The build job, plus the notes and ready handoff every builder job shares."""

from __future__ import annotations

import re

from lib.brief.common import (
    GH,
    QUESTIONS_LINES,
    answers_lines,
    comment_url,
    detail_of,
    read_ticket_line,
    resume_lines,
)
from lib.queue import Job


def task_lines(job: Job, facts: dict) -> list[str]:
    base = facts["base"] or "<base branch>"
    if facts["plan_text"]:
        plan_line = f"The plan handoff is quoted in the Plan section below ({facts['plan'] or 'see the PR'}): it is the plan."
    elif facts["plan"]:
        plan_line = f"Read the plan handoff comment at {facts['plan']}: it is the plan."
    else:
        plan_line = "No plan handoff comment is linked: build from the ticket."
    return [
        "Build the ticket. Follow the router in Skills above.",
        f"1. {read_ticket_line(facts)} {plan_line}",
        f"2. Work on the ticket's branch (`gitBranchName` in Job data, else a new branch off `{base}`). "
        "Test first.",
        "3. Run only the tests for the files you touched plus a short smoke set. "
        "Before `ready`, run the router's \"Before ready\" steps.",
        base_step(4, facts),
        "5. Push, then post the ready handoff below with the head you pushed.",
        _title_line(facts),
        *answers_lines(detail_of(job)),
        *builder_notes(job, facts),
    ]


def base_step(number: int, facts: dict) -> str:
    base = facts["base"] or "<base branch>"
    return (
        f"{number}. Bring the base in before `ready`: `git fetch origin {base}`, then either "
        f"`git rebase origin/{base}` and push with `git push --force-with-lease`, or "
        f"`git merge origin/{base}` and push. If the base changed files you touched, rerun their tests."
    )


def _title_line(facts: dict) -> str:
    source = facts["source"]
    if source is None:
        title = (
            "`#<issue>: <issue title>`, with the source issue's number (not the PR's); "
            "for a Linear ticket, `<KEY>: <title>` (for example `AUT-2284: <title>`)"
        )
    elif source.startswith("#"):
        title = f"`{source}: <issue title>`"
    else:
        title = f"`{source}: <title>`"
    return (
        f"The PR's title starts with its source issue: {title}. If you open the PR, title it so; "
        f"if its title lacks the prefix, fix it with `{GH} pr edit --title`."
    )


def builder_notes(job: Job, facts: dict) -> list[str]:
    notes = [
        "Tests: run only the tests for the files you touched plus a short smoke set, in the foreground. "
        "Then push and post `ready`. CI runs the full suite and is the gate: a red CI comes back to you "
        "as a fix job. Print mode stops background tasks after your last turn, and unpushed work is lost.",
        "Never run the whole test suite locally, in the background, or in chunks, and never poll, "
        "watch or wait on that suite, on GitHub Actions or on any other CI from inside this job. "
        "CI after `ready` is the full-suite gate.",
        *QUESTIONS_LINES,
    ]
    if facts["pre_ready"] is not None:
        spec = facts["pre_ready"]
        fence = "`" * (max((len(run) for run in re.findall("`+", spec.command)), default=0) + 1)
        command = f"{fence} {spec.command} {fence}" if len(fence) > 1 else f"`{spec.command}`"
        notes.append(
            f"Before `ready` counts, the factory runs {command} (timeout {spec.timeout_seconds}s) "
            "in your worktree at the head you name. `ready` is not accepted until it passes at the "
            "pushed head. Run it yourself before you post."
        )
    detail = detail_of(job)
    notes.extend(resume_lines(detail))
    if job.request.attempt > 1:
        notes.append(
            f"This is a retry. {_retry_why(detail, facts)} The working directory, or the branch "
            "you push to, may hold the last attempt's work: check `git status` and `git log @{u}..` "
            "before you start, and build on it rather than redo it."
        )
    return notes


def _retry_why(detail: dict[str, object], facts: dict) -> str:
    if detail.get("retry_comment") is not None:
        link = comment_url(facts, detail.get("retry_comment"))
        where = f" ({link})" if link else " on the PR"
        return (
            f"Attempt 1 ended with the ticket stuck, and Max restarted it with a retry comment{where}: "
            "read his reason there."
        )
    failed = detail.get("retry_of")
    job = f" (job {failed})" if isinstance(failed, str) and failed.isalnum() else ""
    return (
        f"Attempt 1{job} ended without a valid handoff comment: it failed, was stopped, or ran past "
        "its wall-clock budget. A second failure marks the ticket stuck."
    )


def ready_handoff(bot: str) -> tuple[str, str]:
    header = f"<!-- orch:v1 kind=ready from={bot} role=builder head=<pushed-head-sha> -->"
    example = "What changed, the tests and their results, and anything left open."
    return header, example
