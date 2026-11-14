"""The plan job: the builder maps every criterion to work, in a handoff comment."""

from __future__ import annotations

# The most a plan handoff carries: GitHub caps a comment at 65,536 characters.
PLAN_HANDOFF_LIMIT = 60_000


def task_lines(facts: dict) -> list[str]:
    return [
        "This is a plan job. Run as poteto-mode.",
        "1. Read the ticket and its brief (Job data). Start with an architect scaffold.",
        "2. Map every acceptance criterion to the work that meets it. The plan lives in the "
        "plan handoff comment, never in a file in the repo.",
        f"3. Post the plan handoff below with `round={facts['plan_round']}/2`: "
        "`ac=met` when every criterion is mapped, `ac=unmet` when one is not.",
    ]


def handoff(bot: str, facts: dict) -> tuple[str, str]:
    header = (
        f"<!-- orch:v1 kind=plan from={bot} role=builder ac=<met|unmet> "
        f"round={facts['plan_round']}/2 -->"
    )
    example = (
        "The plan itself: the criterion-to-work map, in at most "
        f"{PLAN_HANDOFF_LIMIT:,} characters (GitHub caps a comment at 65,536)."
    )
    return header, example
