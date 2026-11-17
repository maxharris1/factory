from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from lib.feeder_gates import Wait, github_ref


class BranchReads(Protocol):
    def ahead_commits(self, repo: str, base: str, tip: str) -> Sequence[str]: ...

    def commit_pulls(self, repo: str, sha: str) -> Mapping[int, str]: ...

    def branch_has(self, repo: str, branch: str, sha: str) -> bool: ...


def stacked_pulls(
    reads: BranchReads, repo: str, base: str, tip: str, own: int | None
) -> tuple[int, ...]:
    found: set[int] = set()
    for sha in reads.ahead_commits(repo, base, tip):
        pulls = {
            number: pull_base
            for number, pull_base in reads.commit_pulls(repo, sha).items()
            if number != own
        }
        has = {pull_base: reads.branch_has(repo, pull_base, sha) for pull_base in set(pulls.values())}
        found.update(number for number, pull_base in pulls.items() if not has[pull_base])
    return tuple(sorted(found))


def stack_wait(repo: str, where: str, pulls: Sequence[int]) -> Wait:
    labels = ", ".join(github_ref(repo, number).label(repo) for number in pulls)
    whose = "its" if len(pulls) == 1 else "their"
    return Wait(f"waiting on {labels} ({where} carries {whose} commits)")
