"""The plan job: the builder maps every criterion to work, in a handoff comment."""

from __future__ import annotations

from lib.brief.common import QUESTIONS_LINES, answers_lines, detail_of, read_ticket_line, resume_lines
from lib.queue import Job

# The most a plan handoff carries: GitHub caps a comment at 65,536 characters.
PLAN_HANDOFF_LIMIT = 60_000


def task_lines(job: Job, facts: dict) -> list[str]:
    return [
        "This is a plan job. Follow the router in Skills above.",
        f"1. {read_ticket_line(facts)} Start with an architect scaffold.",
        *_first_plan_lines(facts),
        "2. Map every acceptance criterion to the work that meets it, and name each criterion's "
        "proof: the test, command or observation that will show it is met. The plan lives in the "
        "plan handoff comment, never in a file in the repo.",
        "3. Estimate the changed lines the build will add and remove, tests and docs included, and "
        "put the number in the header as `lines=<n>`. A plan without `lines=` counts as a plan "
        "with a criterion left unmapped.",
        f"4. When the estimate is over {facts['plan_max_lines']} lines, or the ticket has more than "
        f"{facts['plan_max_criteria']} acceptance criteria, add a `## Proposed split` section that "
        "names the smaller tickets, each with its criteria and its expected lines. Such a plan "
        "does not start the build: it waits for Max, who answers or splits the ticket.",
        f"5. Post the plan handoff below with `round={facts['plan_round']}/2`: "
        "`ac=met` when every criterion is mapped, `ac=unmet` when one is not. With `ac=unmet`, "
        "name the unmapped criterion as `unsat=<ac-n>`; leave `unsat=` out otherwise.",
        *answers_lines(detail_of(job)),
        *resume_lines(detail_of(job)),
        *QUESTIONS_LINES,
    ]


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
        f"round={facts['plan_round']}/2 lines=<n> unsat=<id> -->"
    )
    example = (
        "The plan itself: the criterion-to-work map with each criterion's proof, the expected "
        "size, and any proposed split, in at most "
        f"{PLAN_HANDOFF_LIMIT:,} characters (GitHub caps a comment at 65,536)."
    )
    return header, example
