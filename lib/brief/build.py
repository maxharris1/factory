"""The build job, plus the notes and ready handoff every builder job shares."""

from __future__ import annotations

from lib.queue import Job


def task_lines(job: Job, facts: dict) -> list[str]:
    base = facts["base"] or "<base branch>"
    plan_line = (
        f"Read the plan handoff comment at {facts['plan']}: it is the plan."
        if facts["plan"]
        else "No plan handoff comment is linked: build from the ticket."
    )
    return [
        "Build the ticket. Run as poteto-mode, with the playbook that fits it "
        "(feature; bug-fix, repro first; refactor).",
        f"1. Read the ticket and its brief (Job data). {plan_line}",
        f"2. Work on the ticket's branch (`gitBranchName` in Job data, else a new branch off `{base}`). "
        "Test first.",
        "3. Before `ready`, self-check: prove-it-works, deslop, no-comments.",
        "4. Push, then post the ready handoff below with the head you pushed.",
        _title_line(facts),
        *builder_notes(job),
    ]


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
        "if its title lacks the prefix, fix it with `env -u GH_TOKEN gh pr edit --title`."
    )


def builder_notes(job: Job) -> list[str]:
    notes = [
        "Run test suites in the foreground, however long they take: never in the background. "
        "Print mode stops background tasks after your last turn, and unpushed work is lost.",
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
