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
        "plus a short smoke run, in the foreground; never in the background or in chunks. "
        "Do not run the full suite unless those affected tests are the full suite. "
        "With no conflicts to resolve, the smoke run is enough.",
        "5. Push, post the ready handoff below with the head you pushed, and finish the job.",
        *_approved_note(job),
        *builder_notes(job, facts),
    ]


def _approved_note(job: Job) -> list[str]:
    if job.request.action != "update-from-main":
        return []
    return [
        "The PR is approved. Resolve each conflict hunk by taking exactly one side when you "
        "can, and change nothing else: the poller then carries the approval to your merge "
        "commit. In a file the project's factory.md lists under `shared_paths`, keeping both "
        "sides of a hunk, one after the other and unchanged, also carries the approval. Any "
        "other edit sends the PR back to review."
    ]
