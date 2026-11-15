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
