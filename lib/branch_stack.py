from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from lib.feeder_gates import Wait, github_ref


@dataclass(frozen=True)
class OpenPull:
    commits: frozenset[str]
    complete: bool
    base: str
    head: str


class BranchReads(Protocol):
    def ahead_commits(self, repo: str, base: str, tip: str) -> Sequence[str]: ...

    def open_pull_commits(self, repo: str) -> Mapping[int, OpenPull]: ...

    def pull_commits(self, repo: str, base: str, head: str) -> Collection[str]: ...


def stacked_pulls(
    reads: BranchReads, repo: str, base: str, tip: str, own: int | None
) -> tuple[int, ...]:
    ahead = set(reads.ahead_commits(repo, base, tip))
    if not ahead:
        return ()
    return tuple(sorted(
        number for number, pull in reads.open_pull_commits(repo).items()
        if number != own and _carries(reads, repo, ahead, pull)
    ))


def _carries(reads: BranchReads, repo: str, ahead: set[str], pull: OpenPull) -> bool:
    if not ahead.isdisjoint(pull.commits):
        return True
    if pull.complete:
        return False
    return not ahead.isdisjoint(reads.pull_commits(repo, pull.base, pull.head))


def stack_wait(repo: str, where: str, pulls: Sequence[int]) -> Wait:
    labels = ", ".join(github_ref(repo, number).label(repo) for number in pulls)
    whose = "its" if len(pulls) == 1 else "their"
    return Wait(f"waiting on {labels} ({where} carries {whose} commits)")
