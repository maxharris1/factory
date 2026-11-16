"""The build job, plus the notes and ready handoff every builder job shares."""

from __future__ import annotations

import re

from lib.brief.common import (
    QUESTIONS_LINES,
    TEST_SCOPE,
    answers_lines,
    comment_url,
    detail_of,
    local_checks,
    read_ticket_line,
    resume_lines,
    title_line,
)
from lib.queue import Job

_PRINT_MODE = {"claude-bot": " Print mode stops background tasks after your last turn, and unpushed work is lost."}


def task_lines(job: Job, facts: dict) -> list[str]:
    base = facts["base"] or "<base branch>"
    if facts["plan_text"]:
        plan_line = f"The plan handoff is quoted in the Plan section below ({facts['plan'] or 'see the PR'}): it is the plan."
    elif facts["plan"]:
        plan_line = f"Read the plan handoff comment at {facts['plan']}: it is the plan."
    else:
        plan_line = "No plan handoff comment is linked: build from the ticket."
    branch = (
        f"PR #{facts['pr']}'s branch (`gitBranchName` in Job data)"
        if facts["pr"]
        else f"the ticket's branch (`gitBranchName` in Job data, else a new branch off `{base}`)"
    )
    return [
        "Build the ticket. Follow the router in Skills above.",
        f"1. {read_ticket_line(facts)} {plan_line}",
        f"2. Work on {branch}. Test first.",
        f"3. Run only the tests for the files you touched plus a short smoke set, {TEST_SCOPE} "
        "Before `ready`, run the router's \"Before ready\" steps.",
        base_step(4, facts),
        "5. Push, then post the ready handoff below with the head you pushed.",
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


def builder_notes(job: Job, facts: dict, *, ci_reported_red: bool = False) -> list[str]:
    gate = (
        f"CI does not run on `{facts['no_ci_base']}`. Run {local_checks(facts)} locally at the head you push, "
        "paste each command and its result in the ready, and say that CI did not run."
        if facts["no_ci_base"] and not ci_reported_red
        else "CI runs the full suite and is the gate: a red CI comes back to you as a fix job."
    )
    notes = [
        title_line(facts),
        gate + _PRINT_MODE.get(f"{job.request.recipient}-bot", ""),
        "Never poll, watch or wait on GitHub Actions or any other CI from inside this job.",
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
            f"Attempt 1 ended with the ticket stuck, and a `retry` comment{where} restarted it: "
            "read its reason there."
        )
    failed = detail.get("retry_of")
    job = f" (job {failed})" if isinstance(failed, str) and failed.isalnum() else ""
    return (
        f"Attempt 1{job} ended without a valid handoff comment: it failed, was stopped, or ran past "
        "its wall-clock budget. The factory retries a failed job once on its own, and this is that "
        "retry; if it fails too, the ticket is marked stuck and waits for a person."
    )


def ready_handoff(bot: str) -> tuple[str, str]:
    header = f"<!-- orch:v1 kind=ready from={bot} role=builder head=<pushed-head-sha> -->"
    example = "What changed, the tests and their results, and anything left open."
    return header, example
