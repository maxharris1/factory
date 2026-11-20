"""The plan job: the builder maps every criterion to work, in a handoff comment."""

from __future__ import annotations

from lib.brief.common import (
    QUESTIONS_LINES,
    Lead,
    answers_lines,
    detail_of,
    journey_lines,
    live_lines,
    read_ticket_line,
    resume_lines,
    ticket_ref,
    title_line,
)
from lib.queue import Job

# The most a plan handoff carries: GitHub caps a comment at 65,536 characters.
PLAN_HANDOFF_LIMIT = 60_000


def lead(job: Job, facts: dict) -> Lead:
    return Lead(
        f"Plan {ticket_ref(facts)}: map every acceptance criterion to work and a proof.",
        (
            "Follow the router in Skills below.",
            f"1. {read_ticket_line(facts)} Start with an architect scaffold.",
            *_first_plan_lines(facts),
            "2. Map every acceptance criterion to the work that meets it, and name each criterion's "
            "proof: the test, command or observation that will show it is met. The plan lives in the "
            "plan handoff comment, never in a file in the repo.",
            *(
                journey_lines("plan", facts["qa_plan"], facts["skills"])
                if facts["qa_plan"]
                else live_lines("plan", job.created_at) if facts["qa"] else []
            ),
            "3. Estimate the changed lines the build will add and remove, tests and docs included. "
            "Put the number on its own line in the plan body as `Estimate: <n>`, before any "
            "`## Proposed split`. `orch job done` refuses a plan without it.",
            "4. The size check multiplies the estimate by 1.35 and rounds up, because estimates run "
            f"low. When that padded number is over {facts['plan_max_lines']} lines, or the ticket has "
            f"more than {facts['plan_max_criteria']} acceptance criteria, add a `## Proposed split` "
            "section with one `### <title>` block per smaller ticket. Under each title put its "
            "criteria as `- ac-<n>: ...` lines, its expected lines as `Estimate: <n>`, and "
            "`Depends on: part <k>` naming the earlier parts it builds on, or `Depends on: none`. "
            "A part with no `Depends on:` line depends on the part before it. "
            "Such a plan does not start the build. On a GitHub-ticket repo the dispatcher files each "
            "part of a split it can parse as a ticket and closes this one. A split it cannot parse "
            "waits for Max, who answers or splits the ticket.",
            f"5. Post the plan handoff below with `round={facts['plan_round']}/2`: "
            "`ac=met` when every criterion is mapped, `ac=unmet` when one is not. With `ac=unmet`, "
            "name the unmapped criterion as `unsat=<ac-n>`; leave `unsat=` out otherwise.",
            title_line(facts),
            *answers_lines(detail_of(job)),
            *resume_lines(detail_of(job)),
            *QUESTIONS_LINES,
        ),
        ("The plan maps every `ac-<n>` to work and a named proof, and carries the `Estimate:` line.",),
    )


def _first_plan_lines(facts: dict) -> list[str]:
    if facts["plan_round"] != 2:
        return []
    lines = []
    if facts["unmapped"]:
        lines.append(
            f"The first plan left `{facts['unmapped']}` unmapped. Map it, and keep what the "
            "first plan already mapped."
        )
    if facts["plan_text"]:
        lines.append("The first plan is quoted in the First plan section below.")
    return lines


def handoff(bot: str, facts: dict) -> tuple[str, str]:
    header = (
        f"<!-- orch:v1 kind=plan from={bot} role=builder ac=<met|unmet> "
        f"round={facts['plan_round']}/2 unsat=<id> -->"
    )
    example = (
        "The plan itself: the `Estimate: <n>` line, the criterion-to-work map with each "
        "criterion's proof, and any proposed split, in at most "
        f"{PLAN_HANDOFF_LIMIT:,} characters (GitHub caps a comment at 65,536)."
    )
    return header, example
