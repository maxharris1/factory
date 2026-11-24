"""The build job, plus the notes and ready handoff every builder job shares."""

from __future__ import annotations

from lib.brief.common import (
    QUESTIONS_LINES,
    Lead,
    TEST_SCOPE,
    answers_lines,
    command_code,
    comment_url,
    detail_of,
    fenced,
    journey_lines,
    live_lines,
    local_checks,
    read_ticket_line,
    resume_lines,
    ticket_ref,
    title_line,
)
from lib.proof import from_detail
from lib.queue import Job

PROOF_EXAMPLE = """## Proof
- ac-1 (review): `<command>`
  ```
  <its output>
  ```
- ac-2 (qa): `<verify-skill command for journey upload-file>`
  ```
  <its output>
  ```
- ac-3 (qa): unverified. The verify skill's doctor failed: `<doctor command>`
  ```
  <doctor output>
  ```"""
_PRINT_MODE = {"claude-bot": " Print mode stops background tasks after your last turn, and unpushed work is lost."}


def lead(job: Job, facts: dict) -> Lead:
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
    return Lead(
        f"Build {ticket_ref(facts)} so every acceptance criterion is met.",
        (
            "Follow the router in Skills.",
            f"1. {read_ticket_line(facts)} {plan_line}",
            f"2. Work on {branch}. Write tests first, as the `tdd` skill says.",
            f"3. Run only the tests for the files you touched plus a short smoke set, {TEST_SCOPE} "
            "Before `ready`, run the router's \"Before ready\" steps.",
            base_step(4, facts),
            "5. Push, then post the ready handoff below with the head you pushed.",
            *(
                journey_lines("ready", facts["qa_plan"])
                if facts["qa_plan"]
                else live_lines("ready", job.created_at) if facts["qa"] and not (facts["plan_text"] or facts["plan"]) else []
            ),
            *answers_lines(detail_of(job)),
            *builder_notes(job, facts),
        ),
        (
            "The ready names the pushed head and each criterion's test or command with its output.",
            *proof_lines(detail_of(job)),
        ),
    )


def base_step(number: int, facts: dict) -> str:
    base = facts["base"] or "<base branch>"
    return (
        f"{number}. Before `ready`: `git fetch origin {base}`, then `git rebase origin/{base}` "
        f"(`--force-with-lease`) or `git merge origin/{base}` (`git push`). "
        "Rerun touched tests if base changed them."
    )


def builder_notes(job: Job, facts: dict) -> list[str]:
    if facts["no_ci_base"] and facts["local_gate"]:
        spec = facts["local_gate"]
        gate = (
            f"CI does not run on `{facts['no_ci_base']}`. Before `ready` counts, the factory runs "
            f"{command_code(spec.command)} (timeout {spec.timeout_seconds}s), factory.md `ci.local_gate`, in a fresh "
            "checkout of the head you name and records `local-gate-passed` at it. Run it yourself before you post. "
            "You need not paste local results."
        )
    elif facts["no_ci_base"]:
        gate = (
            f"CI does not run on `{facts['no_ci_base']}`. Run {local_checks(facts)} locally at the head you push, "
            "paste each command and its result in the ready, and say that CI did not run."
        )
    else:
        gate = "CI runs the full suite and is the gate: a red CI comes back to you as a fix job."
    notes = [
        title_line(facts),
        gate + _PRINT_MODE.get(f"{job.request.recipient}-bot", ""),
        "Never poll, watch or wait on GitHub Actions or any other CI from inside this job.",
        *QUESTIONS_LINES,
    ]
    if facts["pre_ready"] is not None:
        spec = facts["pre_ready"]
        notes.append(
            f"Before `ready` counts, the factory runs {command_code(spec.command)} (timeout {spec.timeout_seconds}s) "
            "in a fresh checkout of the head you name, without anything uncommitted or untracked "
            "and without your installed dependencies or generated files. `ready` is not accepted "
            "until it passes at the pushed head. Run it yourself before you post."
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


def proof_lines(detail: dict[str, object]) -> list[str]:
    items = from_detail(detail.get("proof"))
    if not items:
        return []
    return [
        "The ready's `## Proof` section has one entry for each criterion id below. A `review` entry "
        "gives the command you ran, in backticks, and its output in a fenced block. A `qa` entry gives "
        "the verify skill's command for the criterion's journeys and its output, and names each journey.",
        *(
            f"- {item.id} ({item.kind})"
            + (
                ": journeys unknown, because neither the brief's ticket quote nor the source issue "
                "shows a Features line, so write it as `unverified` with the doctor command and its output" if item.journeys is None
                else f": journeys {', '.join(item.journeys)}" if item.journeys else ""
            )
            for item in items
        ),
        "Its shape, with placeholders:",
        fenced(PROOF_EXAMPLE),
        "Run the verify skill's doctor before any qa entry. If it fails, write that entry as "
        "`unverified` with the doctor command and its output. Never leave an entry out.",
    ]


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
    example = "What changed, the Proof above, and anything left open."
    return header, example
