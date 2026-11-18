"""A one-ticket QA thread driven through poll_once (#763)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from lib.dispatcher import CIResult, Comment, Dispatcher, TrackedTicket
from lib.factory_md import QaConfig, QaLane, QaRunner
from lib.orch_yml import Approvals
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, factory

REPO = "maxharris1/factory"
TICKET = "763"
TRUSTED = {"github": {"maxharris1": {"grok-bot", "claude-bot", "codex-bot"}}}
APPROVALS = Approvals(logins=frozenset({"maxharris1"}))
HEAD_A, HEAD_B, HEAD_C = "a" * 40, "b" * 40, "c" * 40


def qa_config(*, shootout: bool = False, paths: tuple[str, ...] = (), full_rerun_lines: int = 400) -> QaConfig:
    return QaConfig(
        runners=(QaRunner("claude-cloud", "full-stack"), QaRunner("cursor-cloud", "full-stack")),
        lane=QaLane(boot="make boot", base_url="http://localhost:3000"),
        profile="full-stack",
        shootout=shootout,
        paths=paths,
        full_rerun_lines=full_rerun_lines,
    )


def scenarios(*ids: str) -> str:
    lines = [f"- {qa} (ac-1): open the page. Pass: tests/test_x.py" for qa in ids]
    return "\n## Verify, live\n" + "\n".join(lines) + "\n"


def ready(head: str, round_: str = "1/3", *, body: str = "") -> str:
    return (
        f"<!-- orch:v1 kind=ready from=claude-bot role=builder round={round_} head={head}"
        f" risk=high state=review builder=claude -->\n{body}"
    )


def verdict(head: str, verdict_: str, round_: str = "1/3") -> str:
    ac = "met" if verdict_ == "APPROVE" else "unmet"
    return (
        f"<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round={round_} head={head}"
        f" ac={ac} verdict={verdict_} risk=high -->\n"
    )


def qa_report(head: str, result: str, *, arm: str = "primary", runner: str = "claude-cloud", body: str = "") -> str:
    return (
        f"<!-- orch:v1 kind=qa from=grok-bot role=qa head={head} result={result}"
        f" scope=full runner={runner} arm={arm} -->\n{body}"
    )


class Thread:
    """A PollSource over one ticket whose comments the test appends."""

    def __init__(self, base: TrackedTicket):
        self.base = base
        self.comments: list[Comment] = []
        self.head: str | None = None
        self.merged = False
        self.config = type("Config", (), {"poll_seconds": 30})()

    def post(
        self,
        comment_id: str,
        body: str,
        *,
        at: float,
        head: str | None = None,
        author: str = "maxharris1",
        edited_at: float | None = None,
    ) -> Comment:
        item = Comment(id=comment_id, author=author, body=body, created_at=at, edited_at=edited_at)
        self.comments.append(item)
        if head is not None:
            self.head = head
        return item

    def snapshot(self) -> TrackedTicket:
        ci = {self.head: CIResult("success")} if self.head else {}
        return replace(self.base, comments=tuple(self.comments), current_head=self.head, ci=ci)

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()):
        return (self.snapshot(),)

    def linear_candidates(self, query, *, exclude, reserve):
        return ()

    def tracked_tickets(self, refs):
        found = []
        for ref in refs:
            if self.merged:
                self.dispatcher.record_terminal(ref.repo, ref.ticket_id, "merged")
                if not ref.allow_merged:
                    continue
            found.append(self.snapshot())
        return tuple(found)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        qa: QaConfig | None,
        *,
        qa_diff: Callable | None = None,
        ship: str = "handoff",
        approvals: Approvals = APPROVALS,
    ):
        self.now = [1000.0]
        path = tmp_path / "orch.db"
        self.queue = SQLiteJobQueue(path, clock=lambda: self.now[0])
        self.actions = FakeReviewActions()
        kwargs = {} if qa_diff is None else {"qa_diff": qa_diff}
        self.dispatcher = Dispatcher(
            path,
            self.queue,
            trusted_logins=TRUSTED,
            review_actions=self.actions,
            grok_delivery=FakeGrokDelivery(),
            approvals=approvals,
            **kwargs,
        )
        base = TrackedTicket(
            repo=REPO,
            ticket_id=TICKET,
            factory=replace(factory(ship=ship), qa=qa),
            comments=(),
            current_head=None,
            ci={},
            risk="high",
            ready_starts_review=True,
            pr_number="812",
        )
        self.thread = Thread(base)
        self.thread.dispatcher = self.dispatcher

    def poll(self, cycles: int = 1) -> None:
        for _ in range(cycles):
            self.dispatcher.poll_once(self.thread)
            self.now[0] += 30

    def post(self, comment_id: str, body: str, *, head: str | None = None, author: str = "maxharris1") -> Comment:
        return self.thread.post(comment_id, body, at=self.now[0], head=head, author=author)

    def rows(self, sql: str, *args: object) -> list[tuple]:
        with self.queue.connect() as connection:
            return [tuple(row) for row in connection.execute(sql, args)]

    def qa_jobs(self) -> list[dict]:
        return [
            {"state": state, **json.loads(detail)}
            for state, detail in self.rows(
                "SELECT state, detail FROM jobs WHERE action = 'qa' ORDER BY rowid"
            )
        ]

    def qa_runs(self) -> list[tuple]:
        return self.rows(
            "SELECT head, scope, runner, arm, state, result, reason FROM qa_runs ORDER BY rowid"
        )

    def finish(self, recipient: str, handoff: str | None) -> None:
        job = self.queue.claim(recipient)
        assert job is not None, recipient
        self.queue.finish(job.id, owner=recipient, handoff_comment_id=handoff)
