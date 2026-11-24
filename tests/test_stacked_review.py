from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from lib.dispatch.types import StackParent
from lib.dispatcher import CIResult, Comment
from tests.test_dispatcher import READY, VERDICT, FakeSource, comment, factory, setup, ticket

PARENT = "factory/issue-1449"
HEAD = "c41d" * 10


def child(*comments: Comment, parent_sha: str, mergeable_state: str):
    return replace(
        ticket(*comments, head=HEAD, ci={HEAD: CIResult("success")}),
        factory=replace(factory(), base_branch=PARENT),
        pr_number="42",
        pr_base=PARENT,
        mergeable_state=mergeable_state,
        ready_starts_review=True,
        stack=StackParent(1449, PARENT, parent_sha),
    )


def rows(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid").fetchall()


def attention(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT reason FROM needs_attention")]


def test_a_parent_push_queues_no_review_on_its_stacked_child(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    ready = comment("ready-1", READY.format(head=HEAD), author="claude-login", created_at=100)

    def poll(*comments: Comment, parent_sha: str = "1" * 40, mergeable_state: str = "clean") -> None:
        snapshot = child(*comments, parent_sha=parent_sha, mergeable_state=mergeable_state)
        dispatcher.poll_once(FakeSource(snapshot, github=(snapshot,)))

    poll(ready)
    review = queue.claim("codex")
    assert review is not None and review.request.action == "review"
    now[0] = 120
    revise = comment(
        "revise-1",
        VERDICT.format(round="1/3", head=HEAD, verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=120,
    )
    queue.finish(review.id, owner="codex", handoff_comment_id=revise.id)
    poll(ready, revise)
    before = rows(tmp_path / "d.db")

    now[0] = 200
    poll(ready, revise, parent_sha="2" * 40, mergeable_state="behind")

    assert rows(tmp_path / "d.db") == before
    assert "base-mismatch" not in attention(tmp_path / "d.db")
