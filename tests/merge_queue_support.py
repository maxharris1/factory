"""#130 test harness: approved PRs on a finish_line: merge project.

A Board is a fake GitHub with any number of PRs and a real Dispatcher over a
temp database. Every poll goes through Dispatcher.poll_once, so the merge
queue step runs exactly as it does in the poller: after the comment pass,
reading each queue head fresh.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from lib import job_detail
from lib.dispatcher import CIResult, Comment, Dispatcher, LabelEvent, TicketRef, TrackedTicket
from lib.factory_md import FactoryConfig, HoldConfig
from lib.orch_yml import Approvals
from lib.queue import Job, SQLiteJobQueue
from lib.update_check import UpdateClass
from tests.test_dispatcher import (
    GO,
    READY,
    TRUSTED_LOGINS,
    VERDICT,
    FakeGrokDelivery,
    FakeReviewActions,
)

REPO = "owner/repo"
APPROVE = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
    "round={round} head={head} ac=met verdict=APPROVE risk=high -->\nApproved.\n"
)


def sha(n: int) -> str:
    return f"{n:040x}"


@dataclass
class PR:
    repo: str
    number: str
    head: str
    comments: list[Comment] = field(default_factory=list)
    ci: dict[str, str] = field(default_factory=dict)
    mergeable: bool | None = True
    mergeable_state: str | None = "clean"
    merged: bool = False
    closed: bool = False
    labels: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    label_events: tuple[LabelEvent, ...] = ()

    def push(self, head: str, *, ci: str = "success", state: str = "clean") -> None:
        self.head = head
        self.ci[head] = ci
        self.mergeable = state != "dirty"
        self.mergeable_state = state

    def conflict(self) -> None:
        self.mergeable = False
        self.mergeable_state = "dirty"


class BoardActions(FakeReviewActions):
    def __init__(self, board: Board):
        super().__init__()
        self.board = board

    def merge(self, ticket: TrackedTicket, *, squash: bool, match_head_commit: str) -> None:
        super().merge(ticket, squash=squash, match_head_commit=match_head_commit)
        self.board.pr(ticket.ticket_id, ticket.repo).merged = True


class Board:
    def __init__(
        self,
        tmp_path: Path,
        *,
        ship: str = "handoff",
        finish_line: str | None = "merge",
        hold: HoldConfig | None = None,
        classifier=None,
        trusted_logins=TRUSTED_LOGINS,
        approvals: Approvals | None = None,
    ):
        self.now = [100.0]
        self.actions = BoardActions(self)
        self.grok = FakeGrokDelivery()
        path = tmp_path / "d.db"
        self.queue = SQLiteJobQueue(path, clock=lambda: self.now[0])
        self.classified: list[tuple[str, str, str, str]] = []
        self.classified_shared: list[tuple[str, ...]] = []
        self.verdict = UpdateClass(True, "each hunk took one side")
        self.log: list[str] = []

        def default_classifier(
            repo: str, base: str, approved: str, head: str, shared_paths: tuple[str, ...]
        ) -> UpdateClass:
            self.classified.append((repo, base, approved, head))
            self.classified_shared.append(shared_paths)
            return self.verdict

        self.dispatcher = Dispatcher(
            path,
            self.queue,
            trusted_logins=trusted_logins,
            approvals=approvals,
            review_actions=self.actions,
            grok_delivery=self.grok,
            update_classifier=default_classifier if classifier is None else classifier,
            log=self.log.append,
        )
        if classifier is False:
            self.dispatcher.update_classifier = None
        self.factory = FactoryConfig(
            base_branch="main",
            tickets="github",
            builder_harness="claude",
            reviewer_harness="codex",
            rounds_low=1,
            rounds_high=3,
            verify="none",
            ship=ship,
            skills=(),
            hold=hold,
            finish_line=finish_line,
        )
        self.prs: dict[tuple[str, str], PR] = {}
        self.last_refs: tuple[TicketRef, ...] = ()
        self.fresh_reads: list[str] = []

    # The PollSource the dispatcher reads.
    def github_candidates(self, label_prefix: str, *, exclude=frozenset()):
        return ()

    def linear_candidates(self, query: str, *, exclude, reserve: int):
        return ()

    def tracked_tickets(self, refs: tuple[TicketRef, ...]) -> tuple[TrackedTicket, ...]:
        self.last_refs = refs
        found = []
        for ref in refs:
            pr = self.prs.get((ref.repo.lower(), ref.ticket_id))
            if pr is None:
                continue
            if pr.merged:
                self.dispatcher.record_terminal(pr.repo, pr.number, "merged", self.snapshot(pr))
                if not ref.allow_merged:
                    continue
            found.append(self.snapshot(pr))
        return tuple(found)

    def closed_pulls(self, refs: tuple[TicketRef, ...]) -> frozenset[tuple[str, str]]:
        return frozenset(
            key
            for ref in refs
            if (pr := self.prs.get(key := (ref.repo.lower(), ref.ticket_id))) is not None
            and (pr.closed or pr.merged)
        )

    def fresh_ticket(self, ref: TicketRef) -> TrackedTicket | None:
        self.fresh_reads.append(ref.ticket_id)
        pr = self.prs.get((ref.repo.lower(), ref.ticket_id))
        return None if pr is None else self.snapshot(pr, fresh=True)

    def snapshot(self, pr: PR, *, fresh: bool = False) -> TrackedTicket:
        hold = {}
        if self.factory.hold is not None:
            hold = {
                "labels": pr.labels,
                "changed_files": pr.changed_files,
                "label_events": pr.label_events,
            }
        return TrackedTicket(
            repo=pr.repo,
            ticket_id=pr.number,
            factory=self.factory,
            comments=tuple(pr.comments),
            current_head=pr.head,
            ci={head: CIResult(status) for head, status in pr.ci.items()},
            risk="high",
            pr_number=pr.number,
            ticket_url=f"https://github.com/{pr.repo}/pull/{pr.number}",
            merged=pr.merged and fresh,
            mergeable=pr.mergeable,
            mergeable_state=pr.mergeable_state,
            closed=pr.closed,
            **hold,
        )

    # Driving the board.
    def open(self, number: str, head: str, *, repo: str = REPO) -> PR:
        pr = PR(repo=repo, number=number, head=head, ci={head: "success"})
        self.prs[(repo.lower(), number)] = pr
        with self.queue.connect() as connection:
            connection.execute(
                "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) "
                "VALUES (?, ?, 'github', 0)",
                (repo.lower(), number),
            )
        return pr

    def pr(self, number: str, repo: str = REPO) -> PR:
        return self.prs[(repo.lower(), number)]

    def poll(self) -> None:
        self.now[0] += 1
        self.dispatcher.poll_once(self)

    def post(self, pr: PR, comment_id: str, body: str, author: str) -> None:
        self.now[0] += 10
        pr.comments.append(
            Comment(id=comment_id, author=author, body=body, created_at=self.now[0])
        )

    def go(self, pr: PR, comment_id: str, *, state: str = "building", author: str = "grok-login") -> None:
        self.post(pr, comment_id, GO.format(state=state), author)

    def ready(self, pr: PR, head: str, comment_id: str | None = None) -> str:
        comment_id = comment_id or f"ready-{pr.number}-{head[-6:]}"
        self.post(pr, comment_id, READY.format(head=head), "claude-login")
        return comment_id

    def approve(self, pr: PR, round_text: str = "1/3", comment_id: str | None = None) -> str:
        comment_id = comment_id or f"approve-{pr.number}-{pr.head[-6:]}"
        self.post(pr, comment_id, APPROVE.format(round=round_text, head=pr.head), "codex-login")
        return comment_id

    def revise(self, pr: PR, round_text: str) -> str:
        comment_id = f"revise-{pr.number}-{round_text}"
        self.post(
            pr,
            comment_id,
            VERDICT.format(round=round_text, head=pr.head, verdict="REVISE", risk="high"),
            "codex-login",
        )
        return comment_id

    def claim(self, recipient: str) -> Job:
        job = self.queue.claim(recipient)
        assert job is not None, f"no {recipient} job queued"
        return job

    def finish(self, recipient: str) -> Job:
        job = self.claim(recipient)
        self.queue.finish(job.id, owner=recipient, handoff_comment_id=f"handoff-{job.id}")
        return job

    def fail(self, recipient: str) -> Job:
        job = self.claim(recipient)
        self.queue.finish(job.id, owner=recipient, handoff_comment_id=None)
        return job

    def build_and_approve(self, pr: PR, *, mergeable: bool | None = True) -> str:
        """go, build, ready, review, APPROVE at round 1: the APPROVE's id."""
        pr.mergeable = mergeable
        pr.mergeable_state = None if mergeable is None else ("clean" if mergeable else "dirty")
        self.go(pr, f"go-{pr.number}")
        self.poll()
        self.finish("claude")
        self.ready(pr, pr.head)
        self.poll()
        self.finish("codex")
        approve = self.approve(pr)
        self.poll()
        return approve

    # Reading the database.
    def state(self, number: str, repo: str = REPO) -> sqlite3.Row:
        with self.queue.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (repo.lower(), number),
            ).fetchone()
        assert row is not None
        return row

    def outcome(self, comment_id: str) -> str | None:
        with self.queue.connect() as connection:
            row = connection.execute(
                "SELECT outcome FROM handled_comments WHERE comment_id = ?", (comment_id,)
            ).fetchone()
        return None if row is None else str(row["outcome"])

    def jobs(self, action: str | None = None, number: str | None = None) -> list[dict]:
        query = "SELECT * FROM jobs WHERE 1 = 1"
        args: list[str] = []
        if action is not None:
            query += " AND action = ?"
            args.append(action)
        if number is not None:
            query += " AND ticket_id = ?"
            args.append(number)
        with self.queue.connect() as connection:
            return [
                {**dict(row), "detail": job_detail.load_row(connection, row["id"], row["detail"])}
                for row in connection.execute(query + " ORDER BY created_at, rowid", args)
            ]

    def wakes(self, reason: str) -> list[dict]:
        return [
            json.loads(row["detail"] or "{}")
            for row in self.jobs(reason)
            if row["recipient"] == "grok"
        ]
