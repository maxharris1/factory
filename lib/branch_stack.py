"""Stacked-branch holds (issue #205, N-01, F-22).

A ticket's branch can exist before the feeder feeds it: a Linear issue's
gitBranchName someone already pushed to, or a branch made by hand. When that
branch carries commits of another open PR (as PR #131 carried #113's), the
ticket waits until that PR merges, so its review never covers another PR's
unreviewed work. A PR counts as open while GitHub lists it for one of the
branch's commits that the base branch lacks; the ticket's own PR never holds
it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from lib.feeder_gates import Wait, github_ref


class BranchReads(Protocol):
    def ahead_commits(self, repo: str, base: str, branch: str) -> Sequence[str]: ...

    def commit_pulls(self, repo: str, sha: str) -> Collection[int]: ...


def stacked_pulls(
    reads: BranchReads, repo: str, base: str, branch: str, own: int | None
) -> tuple[int, ...]:
    """The other open PRs whose commits branch carries, oldest first."""
    found: set[int] = set()
    for sha in reads.ahead_commits(repo, base, branch):
        found.update(reads.commit_pulls(repo, sha))
    return tuple(sorted(number for number in found if number != own))


def stack_wait(repo: str, branch: str, pulls: Sequence[int]) -> Wait:
    labels = ", ".join(github_ref(repo, number).label(repo) for number in pulls)
    whose = "its" if len(pulls) == 1 else "their"
    return Wait(f"waiting on {labels} (branch {branch} carries {whose} commits)")
