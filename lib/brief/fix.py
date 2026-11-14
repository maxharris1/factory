"""The fix job: a REVISE verdict or a red CI run, fixed as a class."""

from __future__ import annotations

from lib.brief.build import builder_notes
from lib.queue import Job


def task_lines(job: Job, facts: dict) -> list[str]:
    head = facts["head"] or "the PR head"
    return [
        "Fix what the job's `instruction` names (a REVISE verdict link or a CI log, in Job data). "
        "Run as poteto-mode.",
        f"1. Check out the PR branch at {head}.",
        "2. Test first: a test that fails on the current head and covers the whole class of the "
        "finding. Then fix the class.",
        "3. Run the tests. Self-check: principle-prove-it-works, no-comments.",
        "4. Push, then post the ready handoff below with the head you pushed.",
        *builder_notes(job),
    ]
