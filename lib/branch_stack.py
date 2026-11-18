from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from lib.feeder_gates import Wait, github_ref


@dataclass(frozen=True)
class OpenPull:
    """An open PR's commits on its head that its base lacks. GitHub lists
    at most 250 of them, and the snapshot reads 100, so a longer PR is
    incomplete and its head is compared instead."""

    commits: frozenset[str]
    complete: bool
    base: str
    head: str


class BranchReads(Protocol):
    def ahead_commits(self, repo: str, base: str, tip: str) -> Sequence[str]: ...

    def open_pull_commits(self, repo: str) -> Mapping[int, OpenPull]: ...

    def merge_base(self, repo: str, a: str, b: str) -> str: ...

    def branch_has(self, repo: str, branch: str, sha: str) -> bool: ...


def stacked_pulls(
    reads: BranchReads, repo: str, base: str, tip: str, own: int | None
) -> tuple[int, ...]:
    ahead = set(reads.ahead_commits(repo, base, tip))
    if not ahead:
        return ()
    return tuple(sorted(
        number for number, pull in reads.open_pull_commits(repo).items()
        if number != own and _carries(reads, repo, tip, ahead, pull)
    ))


def _carries(reads: BranchReads, repo: str, tip: str, ahead: set[str], pull: OpenPull) -> bool:
    if not ahead.isdisjoint(pull.commits):
        return True
    if pull.complete:
        return False
    # Every commit tip shares with the PR head is an ancestor of their merge
    # base, so the PR carries one of ours iff that base is ours and not its base's.
    shared = reads.merge_base(repo, tip, pull.head)
    return shared in ahead and not reads.branch_has(repo, pull.base, shared)


def stack_wait(repo: str, where: str, pulls: Sequence[int]) -> Wait:
    labels = ", ".join(github_ref(repo, number).label(repo) for number in pulls)
    whose = "its" if len(pulls) == 1 else "their"
    return Wait(f"waiting on {labels} ({where} carries {whose} commits)")
