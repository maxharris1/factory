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
        "3. Resolve any conflict without changing behavior. Change nothing besides the merge resolution.",
        "4. Run only the tests affected by the files you touched while resolving the merge, "
        "plus a short smoke run. Do not run the full suite unless those affected tests are the full suite. "
        "With no conflicts to resolve, the smoke run is enough.",
        "5. Push, post the ready handoff below with the head you pushed, and finish the job.",
        "Do not poll, watch or wait on GitHub Actions or any other CI from inside this job: "
        "the factory's own review and CI gate check CI after `ready`.",
        *builder_notes(job),
    ]
