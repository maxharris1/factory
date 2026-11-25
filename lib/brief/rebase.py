"""The rebase job: rebase the PR onto the base branch and push with a lease, never a merge."""

from __future__ import annotations

from lib.brief.build import builder_notes, proof_lines
from lib.brief.common import NO_CI_WAIT, TEST_SCOPE, Lead, detail_of, pr_ref, stack_lines
from lib.queue import Job


def lead(job: Job, facts: dict) -> Lead:
    base = facts["base"] or "<base branch>"
    head = facts["head"] or "<pr-head>"
    branch = facts["branch"] or "<pr-branch>"
    return Lead(
        f"Rebase {pr_ref(facts)} onto `origin/{base}`, nothing else.",
        (
            *stack_lines(facts),
            f"1. Check out `{branch}` at {head} and run `git fetch origin {base}`.",
            f"2. `git rebase --onto origin/{base} {facts['onto_from']}`, without `--rebase-merges`. "
            "Never run `git merge`."
            if facts["onto_from"]
            else f"2. `git rebase origin/{base}`, without `--rebase-merges`. Never run `git merge`.",
            "3. Resolve each conflict without changing behavior, then `git rebase --continue`.",
            "4. If a generated Supabase types file conflicts, take the base side, regenerate it with the "
            "project's documented types command, and commit the result. If that command cannot run, "
            "the conflict counts as one you cannot resolve.",
            f"5. Check that `git rev-list --merges origin/{base}..HEAD` prints nothing.",
            f"6. Run only the tests for the files you touched while resolving conflicts, plus a short "
            f"smoke set, {TEST_SCOPE}",
            f"7. `git push --force-with-lease={branch}:{head} origin HEAD:{branch}`. "
            "Never push with a bare `--force`.",
            "8. Post the ready handoff below with the head you pushed, and finish the job.",
            "If a conflict cannot be resolved without changing behavior, or the wall-clock budget runs "
            "short: list the files with `git diff --name-only --diff-filter=U`, run "
            f"`git rebase --abort 2>/dev/null; git reset --hard {head}` (the abort ends a rebase still in "
            "progress, the reset undoes one that already finished), "
            f"check that `git rev-parse HEAD` prints `{head}`, push nothing, and post a `questions` "
            "handoff that names each file. That questions handoff is the job's handoff.",
            *builder_notes(job, facts),
            NO_CI_WAIT,
        ),
        (
            f"The ready names the pushed head: `origin/{base}` plus the PR's commits, with no merge commit.",
            *proof_lines(detail_of(job)),
        ),
    )
