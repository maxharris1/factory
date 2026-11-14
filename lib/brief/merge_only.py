"""The merge-only job: bring the base branch in by a merge commit, nothing else."""

from __future__ import annotations

from lib.brief.build import builder_notes
from lib.queue import Job


def task_lines(job: Job, facts: dict) -> list[str]:
    base = facts["base"] or "<base branch>"
    head = facts["head"] or "the PR head"
    return [
        f"Merge-only job: bring `origin/{base}` into the PR branch, nothing else.",
        f"1. Check out the PR branch at {head} and run `git fetch origin {base}`.",
        f"2. `git merge origin/{base}`: a merge commit only, no rebase, no force-push.",
        "3. Resolve any conflict without changing behavior, then run the tests.",
        "4. Push, then post the ready handoff below with the head you pushed.",
        *builder_notes(job),
    ]
