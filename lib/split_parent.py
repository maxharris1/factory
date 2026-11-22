from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from lib.followups import split_record

if TYPE_CHECKING:
    from lib.dispatcher import Comment
    from lib.feeder import IssueRecord

ABANDONED_LABEL = "orch:state/abandoned"


class SplitParentGitHub(Protocol):
    def issue_state(self, repo: str, number: int) -> str: ...

    def split_children(self, repo: str, parent: int) -> Sequence[IssueRecord]: ...

    def post_issue_comment_once(self, repo: str, number: int, body: str, marker: str) -> str: ...

    def remove_issue_label(self, repo: str, number: int, label: str) -> None: ...

    def close_issue(
        self, repo: str, number: int, comment: str | None, *, completed: bool = False
    ) -> None: ...


def _done(child: IssueRecord, merged: Collection[int]) -> bool:
    return (child.state == "closed" and child.reason == "completed") or child.number in merged


def split_evidence(
    body: str,
    comments: Iterable[Comment],
    trusted: Collection[str],
    children: Sequence[IssueRecord],
) -> str | None:
    if children:
        return "split into " + ", ".join(f"#{child.number}" for child in children)
    if record := split_record(body):
        return record
    trusted = {login.lower() for login in trusted}
    return next(
        (
            record
            for comment in comments
            if comment.author.lower() in trusted and (record := split_record(comment.body))
        ),
        None,
    )


def close_split_parent(
    github: SplitParentGitHub,
    repo: str,
    parent: int,
    triggers: Sequence[IssueRecord],
    merged: Collection[int],
    label: str,
) -> Literal["already-closed", "parts-open", "closed"]:
    if github.issue_state(repo, parent) == "closed":
        return "already-closed"
    # A trigger wins over a search hit: the search index can lag a close.
    siblings = {child.number: child for child in github.split_children(repo, parent)}
    siblings.update((child.number, child) for child in triggers)
    if not all(_done(child, merged) for child in siblings.values()):
        return "parts-open"
    marker = f"<!-- orch-split-closed parent={parent} -->"
    parts = ", ".join(f"#{number}" for number in sorted(siblings))
    body = (
        f"{marker}\nAll parts of this split have merged (or closed completed): {parts}."
    )
    github.post_issue_comment_once(repo, parent, body, marker)
    github.remove_issue_label(repo, parent, label)
    github.remove_issue_label(repo, parent, ABANDONED_LABEL)
    github.close_issue(repo, parent, None, completed=True)
    return "closed"
