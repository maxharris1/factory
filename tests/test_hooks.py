import ast
import os
import sqlite3
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from lib import hook_t8
from lib.dispatcher import (
    CIResult,
    Comment,
    Dispatcher,
    Hook,
    ReviewShipper,
    RetryableStateError,
    ShipError,
    TicketRef,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig
from lib.hook_t7 import T7Hook
from lib.hook_t8 import ShipRequest, Shipper, T8Hook
from lib.queue import ACTIONS, Job, JobRequest, SQLiteJobQueue

TRUSTED_LOGINS = {
    "github": {
        "grok-login": {"grok-bot"},
        "claude-login": {"claude-bot"},
        "codex-login": {"codex-bot"},
        "tracy-login": {"tracy-site"},
    },
    "linear": {},
}
VERDICT = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
    "round=1 head={head} verdict={verdict} -->\nVerdict.\n"
)
STOP = "<!-- orch:v1 kind=stuck from={sender} role={role}{state} -->\nStop.\n"
OK = "<!-- orch:v1 kind=ok from=tracy-site role=approver head={head} -->\nOK.\n"
DECLINE = "<!-- orch:v1 kind=decline from=tracy-site role=approver -->\nNo.\n"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
BASE = "0" * 39 + "1"
MOVED = "c" * 40


class FakeShipper:
    def __init__(self) -> None:
        self.requests: list[ShipRequest] = []

    def ship(self, request: ShipRequest) -> None:
        self.requests.append(request)


class FakeReviewActions:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.merges: list[tuple[bool, str]] = []
        self.merged_heads: set[str] = set()
        self.merge_error: Exception | None = None
        self.fast_forwards: list[tuple[str, str]] = []
        self.fast_forward_error: BaseException | None = None
        self.parents: dict[str, str | None] = {
            BASE: None,
            HEAD_A: BASE,
            HEAD_B: BASE,
            MOVED: BASE,
        }
        self.branches: dict[str, str] = {"main": BASE}
        self.fast_forward_lands_on: str | None = None
        self.branch_tip_error_after_fast_forward: Exception | None = None
        self.approvals: list[tuple[str, str]] = []
        self.state_failures: set[str] = set()
        self.events: list[str] = []
        self.notices: list[str] = []

    def comment_once(self, ticket: TrackedTicket, body: str, marker: str) -> None:
        del ticket
        if not any(marker in notice.splitlines() for notice in self.notices):
            self.notices.append(body)

    def set_state(self, ticket: TrackedTicket, state: str) -> None:
        del ticket
        self.states.append(state)
        self.events.append(f"state:{state}")
        if state in self.state_failures:
            raise RuntimeError("state write failed")

    def stable_patch_id(self, ticket: TrackedTicket, head: str) -> str:
        del ticket, head
        return "0" * 40

    def is_merged(self, ticket: TrackedTicket, head: str) -> bool:
        del ticket
        self.events.append(f"is-merged:{head}")
        return head in self.merged_heads

    def merge(
        self,
        ticket: TrackedTicket,
        *,
        squash: bool,
        match_head_commit: str,
    ) -> None:
        del ticket
        if self.merge_error is not None:
            raise self.merge_error
        self.merges.append((squash, match_head_commit))
        self.events.append(f"merge:{match_head_commit}")
        self.merged_heads.add(match_head_commit)

    def fast_forward(
        self,
        ticket: TrackedTicket,
        *,
        branch: str,
        head: str,
    ) -> None:
        del ticket
        if self.fast_forward_error is not None:
            raise self.fast_forward_error
        tip = self.branches[branch]
        ancestor: str | None = head
        while ancestor is not None and ancestor != tip:
            ancestor = self.parents[ancestor]
        if ancestor != tip:
            raise RuntimeError(f"{head} is not a fast-forward of {branch} at {tip}")
        self.branches[branch] = self.fast_forward_lands_on or head
        self.fast_forwards.append((branch, head))
        self.events.append(f"fast-forward:{branch}:{head}")
        self.merged_heads.add(head)

    def branch_tip(self, ticket: TrackedTicket, branch: str) -> str:
        del ticket
        self.events.append(f"branch-tip:{branch}")
        if self.fast_forwards and self.branch_tip_error_after_fast_forward:
            raise self.branch_tip_error_after_fast_forward
        return self.branches[branch]

    def merged_by_someone_else(self, head: str, tip: str, parent: str) -> None:
        """The PR was merged at head outside the factory; main is now tip."""
        self.parents[tip] = parent
        self.branches["main"] = tip
        self.merged_heads.add(head)

    def handoff(self, ticket: TrackedTicket, head: str) -> None:
        del ticket, head

    def await_approver(
        self,
        ticket: TrackedTicket,
        head: str,
        approver: str,
    ) -> None:
        del ticket
        self.approvals.append((head, approver))


class FakeGrokDelivery:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def deliver(self, job: Job, *, logical_key: str) -> str:
        del logical_key
        self.jobs.append(job)
        return f"delivery-{job.id}"


class FakeBot:
    def __init__(self, queue: SQLiteJobQueue, recipient: str):
        self.queue = queue
        self.recipient = recipient
        self.handled: list[Job] = []

    def take(self) -> Job | None:
        job = self.queue.claim(self.recipient)
        if job is None:
            return None
        self.queue.finish(
            job.id,
            owner=self.recipient,
            handoff_comment_id=f"handoff-{job.id}",
        )
        self.handled.append(job)
        return job

    def drain(self) -> list[Job]:
        while self.take() is not None:
            pass
        return self.handled


def factory(ship: str = "after:tracy-site", base_branch: str = "main") -> FactoryConfig:
    """finish_line: ready-for-review, so a ship: merge APPROVE merges at T4
    (#130: finish_line: merge is tests/merge_queue_support.py's Board)."""
    return FactoryConfig(
        base_branch=base_branch,
        tickets="github",
        builder_harness="claude",
        reviewer_harness="codex",
        rounds_low=1,
        rounds_high=3,
        verify="none",
        ship=ship,
        skills=(),
        finish_line="ready-for-review",
    )


def ticket(
    *comments: Comment,
    head: str | None = HEAD_A,
    ship: str = "after:tracy-site",
    ticket_id: str = "42",
    base_branch: str = "main",
) -> TrackedTicket:
    return TrackedTicket(
        repo="owner/repo",
        ticket_id=ticket_id,
        factory=factory(ship, base_branch),
        comments=comments,
        current_head=head,
        ci={head: CIResult("success")} if head else {},
        risk="high",
    )


def say(comment_id: str, body: str, author: str, at: float) -> Comment:
    return Comment(id=comment_id, author=author, body=body, created_at=at)


def approve(comment_id: str, head: str, at: float) -> Comment:
    return say(
        comment_id,
        VERDICT.format(head=head, verdict="APPROVE"),
        "codex-login",
        at,
    )


def stop(
    comment_id: str,
    at: float,
    *,
    state: str | None = None,
    sender: str = "claude-bot",
    author: str = "claude-login",
) -> Comment:
    role = "orchestrator" if sender == "grok-bot" else "builder"
    suffix = f" state={state}" if state else ""
    body = STOP.format(sender=sender, role=role, state=suffix)
    return say(comment_id, body, author, at)


def setup(
    tmp_path: Path,
    shipper: Shipper | None = None,
    *,
    hooks: tuple[Hook, ...] | None = None,
    now: list[float] | None = None,
    grok_delivery: FakeGrokDelivery | None = None,
    actions: FakeReviewActions | None = None,
) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    clock = now if now is not None else [100.0]
    queue = SQLiteJobQueue(path, clock=lambda: clock[0])
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=actions or FakeReviewActions(),
        grok_delivery=grok_delivery or FakeGrokDelivery(),
        hooks=hooks if hooks is not None else (T7Hook(), T8Hook(shipper)),
    )
    return dispatcher, queue


def in_review(queue: SQLiteJobQueue, head: str, ticket_id: str = "42") -> None:
    """Put the ticket in F1-05's review state at head, as T2 would."""
    with queue.connect() as connection:
        seeded = connection.execute(
            "SELECT count(*) FROM jobs WHERE dedupe_key LIKE 'seed-review:%'"
        ).fetchone()[0]
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id=ticket_id,
            action="review",
            recipient="codex",
            source_comment_id=f"ready-{head}",
            timeout_seconds=5400,
            head=head,
        ),
        dedupe_key=f"seed-review:{ticket_id}:{head}:{seeded}",
    )
    review = queue.claim("codex")
    assert review is not None
    queue.finish(review.id, owner="codex", handoff_comment_id=f"review-{head}")
    with queue.connect() as connection:
        connection.execute(
            """
            INSERT INTO ticket_state (repo, ticket_id, workflow_state)
            VALUES ('owner/repo', ?, 'review')
            ON CONFLICT(repo, ticket_id) DO UPDATE SET workflow_state = 'review'
            """,
            (ticket_id,),
        )


def restart(
    tmp_path: Path,
    shipper: Shipper | None = None,
    *,
    grok_delivery: FakeGrokDelivery | None = None,
) -> tuple[Dispatcher, SQLiteJobQueue]:
    return setup(tmp_path, shipper, grok_delivery=grok_delivery)


def park_followup(queue: SQLiteJobQueue, ticket_id: str = "42") -> None:
    with queue.connect() as connection:
        connection.execute(
            """
            INSERT INTO followup_filings (
                repo, ticket_id, comment_id, round, title, body, labels, status,
                issue_number, created_at
            ) VALUES ('owner/repo', ?, 'verdict-0', 4, 'Follow-ups', 'body', '[]',
                      'commented', 77, 0)
            """,
            (ticket_id,),
        )


def skipped_keys(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT reason, once_key FROM skipped_wakes ORDER BY skipped_at, dedupe_key"
        ).fetchall()
    return [(str(row["reason"]), str(row["once_key"])) for row in rows]


def skip_facts(queue: SQLiteJobQueue, reason: str) -> list[dict[str, str]]:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT body FROM skipped_wakes WHERE reason = ? ORDER BY skipped_at, dedupe_key",
            (reason,),
        ).fetchall()
    return [
        dict(line[2:].split(": ", 1) for line in str(row["body"]).splitlines() if line.startswith("- "))
        for row in rows
    ]


def jobs(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        rows = connection.execute(
            """
            SELECT action, recipient FROM jobs
            WHERE dedupe_key NOT LIKE 'seed-review:%'
            ORDER BY created_at, id
            """
        ).fetchall()
    return [(str(row["action"]), str(row["recipient"])) for row in rows]


def test_t7_single_wake_on_repeated_terminal_comments(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    first = ticket(
        stop("blocked-1", 1, state="blocked", sender="grok-bot", author="grok-login"),
        stop("blocked-2", 2, state="blocked", sender="grok-bot", author="grok-login"),
        stop("capped-1", 3, state="capped", sender="grok-bot", author="grok-login"),
        stop("capped-2", 4, state="capped", sender="grok-bot", author="grok-login"),
    )
    dispatcher.process(first)
    grok = FakeBot(queue, "grok")
    assert sorted(job.request.action for job in grok.drain()) == ["blocked", "capped"]

    delivery = FakeGrokDelivery()
    restarted, restarted_queue = restart(tmp_path, grok_delivery=delivery)
    again = replace(
        first,
        comments=first.comments
        + (
            stop("blocked-3", 5, state="blocked", sender="grok-bot", author="grok-login"),
            stop("capped-3", 6, state="capped", sender="grok-bot", author="grok-login"),
        ),
    )
    restarted.process(again)
    restarted.reconcile_jobs()
    assert FakeBot(restarted_queue, "grok").drain() == []

    restarted.process(replace(again, comments=again.comments + (stop("stuck-1", 7),)))
    restarted.process(
        replace(
            again,
            comments=again.comments + (stop("stuck-1", 7), stop("stuck-2", 8)),
        )
    )
    restarted.reconcile_jobs()
    assert [job.request.action for job in delivery.jobs] == ["stuck"]
    assert jobs(restarted_queue).count(("stuck", "grok")) == 1
    assert len(jobs(restarted_queue)) == 3


def test_t7_unparsed_header_from_trusted_login_wakes_grok_once(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    snapshot = ticket(
        say("bad-1", "<!-- orch:v1 kind=nonsense -->\nHuh.\n", "claude-login", 1),
        say("bad-2", "<!-- orch:v2 kind=go -->\n", "someone-else", 2),
        say("chat-1", "Looks good to me.", "claude-login", 3),
    )

    dispatcher.process(snapshot)
    restart(tmp_path)[0].process(snapshot)

    assert FakeBot(queue, "grok").drain() == []
    assert skipped_keys(queue) == [("unparsed", "bad-1")]
    [notice] = dispatcher.review_actions.notices
    assert "unknown kind" in notice
    assert jobs(queue) == []


def test_t7_plan_missing_ac_and_round_wakes_grok_once(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    malformed = say(
        "plan-1",
        "<!-- orch:v1 kind=plan from=claude-bot role=builder -->\nPlan.\n",
        "claude-login",
        1,
    )

    dispatcher.process(ticket(malformed))
    restart(tmp_path)[0].process(ticket(malformed))

    assert FakeBot(queue, "grok").drain() == []
    assert skipped_keys(queue) == [("unparsed", "plan-1")]
    [notice] = dispatcher.review_actions.notices
    assert "missing required key 'ac'" in notice


def test_t7_config_capped_wakes_grok_once_across_restart(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    for target in (dispatcher, restart(tmp_path)[0], restart(tmp_path)[0]):
        target.wake_grok_once(
            ticket(),
            reason="capped",
            source_comment_id="factory.md@abc1234",
            detail="review round cap reached",
            head=HEAD_A,
        )

    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["capped"]


def test_t7_refuses_forged_blocked_and_stuck(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(
        ticket(
            say(
                "verdict-1",
                VERDICT.format(head=HEAD_A, verdict="BLOCKED"),
                "claude-login",
                1,
            ),
            stop("stuck-1", 2, sender="codex-bot", author="claude-login"),
        )
    )

    assert jobs(queue) == []
    assert skipped_keys(queue) == [("rejected", "stuck-1"), ("rejected", "verdict-1")]


def test_t8_ok_on_previewed_sha_ships_that_sha(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)

    dispatcher.process(
        ticket(
            approve("verdict-1", HEAD_A, 1),
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
        )
    )

    assert shipper.requests == [
        ShipRequest(
            repo="owner/repo",
            ticket_id="42",
            head=HEAD_A,
            approver="tracy-site",
            source_comment_id="ok-1",
            approve_comment_id="verdict-1",
        )
    ]
    assert jobs(queue) == []


def test_t8_pending_ok_refused_after_later_revise(tmp_path: Path) -> None:
    revise = say(
        "verdict-2",
        VERDICT.format(head=HEAD_A, verdict="REVISE"),
        "codex-login",
        3,
    )
    approved_then_ok = (
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )

    unwired, unwired_queue = setup(tmp_path / "unwired")
    in_review(unwired_queue, HEAD_A)
    unwired.process(ticket(*approved_then_ok))
    shipper = FakeShipper()
    rewired, _queue = setup(tmp_path / "unwired", shipper)
    rewired.process(ticket(*approved_then_ok, revise))
    assert shipper.requests == []

    one_poll_shipper = FakeShipper()
    one_poll, queue = setup(tmp_path / "one-poll", one_poll_shipper)
    in_review(queue, HEAD_A)
    declined = say("decline-1", DECLINE, "tracy-login", 2.5)
    one_poll.process(ticket(*approved_then_ok, declined, revise))
    assert one_poll_shipper.requests == []
    assert jobs(queue) == []
    assert sorted(reason for reason, _key in skipped_keys(queue)) == [
        "rejected",
        "rejected",
        "stale",
    ]


def test_t8_ok_on_any_other_sha_is_refused(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    comments = (
        approve("verdict-1", HEAD_A, 1),
        say("ok-wrong", OK.format(head=HEAD_B), "tracy-login", 2),
    )

    dispatcher.process(ticket(*comments))
    assert shipper.requests == []

    dispatcher.process(
        ticket(*comments, say("ok-right", OK.format(head=HEAD_A), "tracy-login", 3))
    )
    assert [request.source_comment_id for request in shipper.requests] == ["ok-right"]


def test_t8_ok_is_refused_without_a_waiting_preview_or_after_head_moves(
    tmp_path: Path,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    revise = say(
        "verdict-2",
        VERDICT.format(head=HEAD_A, verdict="REVISE"),
        "codex-login",
        2,
    )

    dispatcher.process(ticket(say("ok-0", OK.format(head=HEAD_A), "tracy-login", 0)))
    in_review(queue, HEAD_A)
    dispatcher.process(
        ticket(
            approve("verdict-1", HEAD_A, 1),
            revise,
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 3),
        )
    )
    in_review(queue, HEAD_A)
    approved = approve("verdict-3", HEAD_A, 4)
    dispatcher.process(ticket(approved))
    dispatcher.process(
        ticket(
            approved,
            say("ok-2", OK.format(head=HEAD_A), "tracy-login", 5),
            head=HEAD_B,
        )
    )

    assert shipper.requests == []
    with queue.connect() as connection:
        outcomes = dict(
            connection.execute(
                "SELECT comment_id, outcome FROM handled_comments"
            ).fetchall()
        )
    assert outcomes["ok-0"] == "rejected-ok-not-waiting"
    assert outcomes["ok-1"] == "rejected-ok-not-waiting"
    assert outcomes["ok-2"] == "refused-ok-head-moved"


def test_t8_ok_from_a_non_approver_is_refused_on_a_live_preview(
    tmp_path: Path,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    approved = approve("verdict-1", HEAD_A, 1)
    forged = say("ok-forged", OK.format(head=HEAD_A), "claude-login", 2)

    dispatcher.process(ticket(approved, forged))

    assert outcomes(queue)["verdict-1"] == "waiting-external"
    assert outcomes(queue)["ok-forged"] == "rejected-ok-author"
    assert shipper.requests == []
    dispatcher.process(
        ticket(approved, forged, say("ok-1", OK.format(head=HEAD_A), "tracy-login", 3))
    )
    assert [request.source_comment_id for request in shipper.requests] == ["ok-1"]


def test_t8_ok_without_after_ship_is_refused(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)

    dispatcher.process(
        ticket(
            approve("verdict-1", HEAD_A, 1),
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
            ship="handoff",
        )
    )

    assert outcomes(queue)["ok-1"] == "rejected-ok-not-after-ship"
    assert shipper.requests == []


def test_t8_ok_without_a_wired_shipper_stays_unhandled(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    in_review(queue, HEAD_A)
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    dispatcher.process(snapshot)

    shipper = FakeShipper()
    restart(tmp_path, shipper)[0].process(snapshot)
    assert [request.head for request in shipper.requests] == [HEAD_A]


def test_t8_ships_once_across_restart_and_repeated_ok(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
        say("ok-2", OK.format(head=HEAD_A), "tracy-login", 3),
    )

    dispatcher.process(snapshot)
    restart(tmp_path, shipper)[0].process(snapshot)

    assert len(shipper.requests) == 1


def test_t8_decline_queues_fix_and_third_decline_is_stuck(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    builder = FakeBot(queue, "claude")
    comments: tuple[Comment, ...] = ()
    heads = ("1" * 40, "2" * 40, "3" * 40)
    for number, head in enumerate(heads, start=1):
        comments += (
            approve(f"verdict-{number}", head, number * 10),
            say(f"decline-{number}", DECLINE, "tracy-login", number * 10 + 1),
        )
        in_review(queue, head)
        dispatcher.process(ticket(*comments, head=head))
        fixes = [job for job in builder.drain() if job.request.head == head]
        if number < 3:
            assert [job.request.action for job in fixes] == ["fix"]
            assert fixes[0].request.source_comment_id == f"decline-{number}"
            assert not dispatcher.is_stuck(ticket(head=head))
        else:
            assert fixes == []
            assert dispatcher.is_stuck(ticket(head=head))

    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["stuck"]
    assert shipper.requests == []

    late_ok = say("ok-late", OK.format(head=heads[2]), "tracy-login", 99)
    dispatcher.process(ticket(*comments, late_ok, head=heads[2]))
    assert shipper.requests == []


def test_t8_repeated_decline_of_one_preview_queues_one_fix(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, FakeShipper())
    in_review(queue, HEAD_A)
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("decline-1", DECLINE, "tracy-login", 2),
        say("decline-2", DECLINE, "tracy-login", 3),
    )

    dispatcher.process(snapshot)
    restart(tmp_path)[0].process(snapshot)

    assert jobs(queue) == [("fix", "claude")]
    assert skipped_keys(queue) == [("rejected", "decline-2")]


def test_t8_decline_naming_another_sha_is_refused(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, FakeShipper())
    in_review(queue, HEAD_A)
    body = DECLINE.replace("role=approver", f"role=approver head={HEAD_B}")

    dispatcher.process(
        ticket(approve("verdict-1", HEAD_A, 1), say("decline-1", body, "tracy-login", 2))
    )

    assert jobs(queue) == []


def test_no_post_merge_job_is_queued_in_phase_1(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)

    dispatcher.process(
        ticket(
            approve("verdict-1", HEAD_A, 1),
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
        )
    )
    dispatcher.reconcile_jobs()

    assert len(shipper.requests) == 1
    assert jobs(queue) == []
    assert not any("post" in action for action in ACTIONS)


def test_hooks_cannot_execute_a_harness_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("process execution is forbidden")

    monkeypatch.setattr(os, "system", fail)
    monkeypatch.setattr(os, "popen", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    dispatcher.process(
        ticket(
            say("bad-1", "<!-- orch:v1 kind=nope -->\n", "claude-login", 1),
            approve("verdict-1", HEAD_A, 2),
            say("decline-1", DECLINE, "tracy-login", 3),
        )
    )
    assert jobs(queue) == [("fix", "claude")]
    assert skipped_keys(queue) == [("unparsed", "bad-1")]

    for name in ("hook_t7.py", "hook_t8.py"):
        tree = ast.parse((Path(__file__).parents[1] / "lib" / name).read_text())
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "subprocess" not in imported
        assert "os" not in imported


BRIEF = "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\nBrief.\n"
GO = (
    "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\n"
    "Go.\n"
)


class RaisingShipper:
    def __init__(self) -> None:
        self.calls = 0

    def ship(self, request: ShipRequest) -> None:
        self.calls += 1
        raise RuntimeError("merge refused")


class RetryOnceShipper:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[ShipRequest] = []

    def ship(self, request: ShipRequest) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RetryableStateError("Linear poll budget is exhausted")
        self.requests.append(request)


class RacingShipper:
    """While it ships, a second dispatcher on the same database polls."""

    def __init__(self) -> None:
        self.requests: list[ShipRequest] = []
        self.other: Dispatcher | None = None
        self.snapshot: TrackedTicket | None = None

    def ship(self, request: ShipRequest) -> None:
        self.requests.append(request)
        assert self.other is not None and self.snapshot is not None
        self.other.process(self.snapshot)


class GreedyHook:
    def __init__(self, answer: tuple[bool, str]):
        self.answer = answer
        self.seen: list[str] = []

    def initialize(self, dispatcher: Dispatcher) -> None:
        return None

    def handle(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        fields: object,
    ) -> tuple[bool, str]:
        self.seen.append(comment.id)
        return self.answer


class FakeSource:
    def __init__(self, *tickets: TrackedTicket):
        self.tickets = tickets

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()) -> tuple[TrackedTicket, ...]:
        return self.tickets

    def linear_candidates(
        self,
        query: str,
        *,
        exclude: frozenset[str],
        reserve: int,
    ) -> tuple[TrackedTicket, ...]:
        del query, exclude, reserve
        return ()

    def tracked_tickets(
        self,
        refs: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]:
        wanted = {ref.ticket_id for ref in refs}
        return tuple(item for item in self.tickets if item.ticket_id in wanted)


def test_t8_short_approve_previews_the_full_head_f1_05_showed(
    tmp_path: Path,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    approved = approve("verdict-1", HEAD_A[:7], 1)
    short_ok = say("ok-1", OK.format(head=HEAD_A[:7]), "tracy-login", 2)

    dispatcher.process(ticket(approved, short_ok))
    assert shipper.requests == []

    dispatcher.process(
        ticket(approved, short_ok, say("ok-2", OK.format(head=HEAD_A), "tracy-login", 3))
    )
    assert [request.head for request in shipper.requests] == [HEAD_A]
    with queue.connect() as connection:
        stored = connection.execute("SELECT head FROM external_previews").fetchall()
    assert [str(row["head"]) for row in stored] == [HEAD_A]


def test_t8_preview_claim_is_won_by_one_insert(tmp_path: Path) -> None:
    _dispatcher, queue = setup(tmp_path)
    snapshot = ticket()
    with queue.connect() as first:
        assert hook_t8._claim(first, snapshot, "verdict-1", HEAD_A, "shipping", "ok-1")
    with queue.connect() as second:
        assert not hook_t8._claim(
            second, snapshot, "verdict-1", HEAD_A, "declined", "decline-1"
        )


def test_t8_two_dispatchers_cannot_both_ship(tmp_path: Path) -> None:
    racing = RacingShipper()
    other_shipper = FakeShipper()
    first, queue = setup(tmp_path, racing)
    second, _queue = setup(tmp_path, other_shipper)
    in_review(queue, HEAD_A)
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    racing.other = second
    racing.snapshot = snapshot

    first.process(snapshot)

    assert len(racing.requests) == 1
    assert other_shipper.requests == []
    with queue.connect() as connection:
        outcome = connection.execute(
            "SELECT outcome FROM external_previews"
        ).fetchone()["outcome"]
    assert outcome == "shipped"
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]


def test_t8_crash_after_ship_returns_never_ships_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    original = hook_t8._set_outcome

    def crash(
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        approve_id: str,
        outcome: str,
    ) -> None:
        if outcome == "shipped":
            raise RuntimeError("simulated crash")
        original(dispatcher, ticket, approve_id, outcome)

    monkeypatch.setattr(hook_t8, "_set_outcome", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        dispatcher.process(snapshot)
    monkeypatch.setattr(hook_t8, "_set_outcome", original)

    for _poll in range(2):
        restart(tmp_path, shipper)[0].process(snapshot)

    assert len(shipper.requests) == 1
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]


def test_t8_ship_error_fails_closed_and_polling_continues(tmp_path: Path) -> None:
    now = [0.0]
    shipper = RaisingShipper()
    delivery = FakeGrokDelivery()
    dispatcher, queue = setup(tmp_path, shipper, now=now, grok_delivery=delivery)
    in_review(queue, HEAD_A)
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="44",
            action="build",
            recipient="claude",
            source_comment_id="go-44",
            timeout_seconds=60,
        ),
        dedupe_key="build-44:g1",
    )
    shipping = ticket(
        say("brief-42", BRIEF, "grok-login", 0),
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    building = ticket(say("go-43", GO, "grok-login", 0), ticket_id="43")
    source = FakeSource(shipping, building)

    now[0] = 100
    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    assert shipper.calls == 1
    claude = FakeBot(queue, "claude").drain()
    assert sorted(
        (job.request.ticket_id, job.request.attempt) for job in claude
    ) == [("43", 1), ("44", 2)]
    assert [job.request.action for job in delivery.jobs] == ["ship-failed"]
    assert "merge refused" in str(delivery.jobs[0].request.detail)


def test_t8_retryable_ship_releases_claim_and_retries(
    tmp_path: Path,
) -> None:
    shipper = RetryOnceShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )

    dispatcher.process(snapshot)
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM external_previews"
        ).fetchone()[0] == 0
        assert connection.execute(
            """
            SELECT count(*) FROM handled_comments
            WHERE comment_id = 'ok-1'
            """
        ).fetchone()[0] == 0

    dispatcher.process(snapshot)

    assert shipper.calls == 2
    assert [request.head for request in shipper.requests] == [HEAD_A]
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT outcome FROM external_previews"
        ).fetchone()[0] == "shipped"
    assert FakeBot(queue, "grok").drain() == []


def test_t7_trusted_comment_with_orch_v_that_fails_to_parse_wakes_unparsed(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(
        ticket(
            say(
                "bad-1",
                "Ready now.\n<!-- orch:v1 kind=ready head=abc1234 -->\n",
                "claude-login",
                1,
            ),
            say("bad-2", "<!-- orch:v1 kind=ready", "codex-login", 2),
            say("bad-3", "<!-- orch:v1 kind=ready", "someone-else", 3),
        )
    )

    assert FakeBot(queue, "grok").drain() == []
    assert skipped_keys(queue) == [("unparsed", "bad-1"), ("unparsed", "bad-2")]


def test_hooks_cannot_claim_go_or_ready_and_first_answer_wins(
    tmp_path: Path,
) -> None:
    first = GreedyHook((False, "held"))
    second = GreedyHook((True, "never"))
    dispatcher, queue = setup(tmp_path, hooks=(first, second))
    snapshot = ticket(
        say("go-1", GO, "grok-login", 1),
        say("brief-1", BRIEF, "grok-login", 2),
    )

    dispatcher.process(snapshot)
    dispatcher.process(snapshot)

    assert first.seen == ["brief-1", "brief-1"]
    assert second.seen == []
    build = queue.claim("claude")
    assert build is not None and build.request.action == "build"


def test_t7_capped_wakes_once_per_head_and_refused_once_per_ticket(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    refused = (
        "<!-- orch:v1 kind=stuck from=grok-bot role=orchestrator "
        "head={head} state=refused -->\nRefused.\n"
    )
    capped_1 = stop("capped-1", 1, state="capped", sender="grok-bot", author="grok-login")
    capped_2 = stop("capped-2", 2, state="capped", sender="grok-bot", author="grok-login")

    dispatcher.process(ticket(capped_1, head=HEAD_A))
    dispatcher.process(ticket(capped_1, capped_2, head=HEAD_B))
    dispatcher.process(
        ticket(
            say("refused-1", refused.format(head=HEAD_A), "grok-login", 3),
            say("refused-2", refused.format(head=HEAD_B), "grok-login", 4),
        )
    )

    grok = FakeBot(queue, "grok").drain()
    assert sorted((job.request.action, job.request.head) for job in grok) == [
        ("capped", HEAD_A),
        ("capped", HEAD_B),
    ]
    assert skipped_keys(queue) == [("refused", "-")]


def test_t7_prose_that_mentions_orch_v_is_not_unparsed(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)

    dispatcher.process(
        ticket(
            say(
                "prose-1",
                "I will post the orch:v1 ready header once CI is green.",
                "claude-login",
                1,
            ),
            say("prose-2", "See `<!-- orch:v1 kind=ready -->` above.", "codex-login", 2),
        )
    )

    assert jobs(queue) == []


def test_t8_crash_before_third_decline_marks_stuck_never_ships_a_later_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    heads = ("1" * 40, "2" * 40, "3" * 40)
    comments: tuple[Comment, ...] = ()
    for number, head in enumerate(heads[:2], start=1):
        comments += (
            approve(f"verdict-{number}", head, number * 10),
            say(f"decline-{number}", DECLINE, "tracy-login", number * 10 + 1),
        )
        in_review(queue, head)
        dispatcher.process(ticket(*comments, head=head))
    comments += (
        approve("verdict-3", heads[2], 30),
        say("decline-3", DECLINE, "tracy-login", 31),
    )
    in_review(queue, heads[2])

    def crash(**_kwargs: object) -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(dispatcher, "_mark_stuck_and_wake", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        dispatcher.process(ticket(*comments, head=heads[2]))
    monkeypatch.undo()

    head = "4" * 40
    downtime = comments + (
        approve("verdict-4", head, 40),
        say("ok-4", OK.format(head=head), "tracy-login", 41),
    )
    restarted, _queue = restart(tmp_path, shipper)
    restarted.process(ticket(*downtime, head=head))

    assert shipper.requests == []
    assert restarted.is_stuck(ticket(head=head))
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["stuck"]


@pytest.mark.parametrize("change", ["head-moved", "newer-verdict"])
def test_t8_orphaned_ship_claim_goes_to_ship_failed_and_wakes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    comments = (
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    original = hook_t8._set_outcome

    def crash(
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        approve_id: str,
        outcome: str,
    ) -> bool:
        if outcome == "shipped":
            raise RuntimeError("simulated crash")
        return original(dispatcher, ticket, approve_id, outcome)

    monkeypatch.setattr(hook_t8, "_set_outcome", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        dispatcher.process(ticket(*comments))
    monkeypatch.setattr(hook_t8, "_set_outcome", original)

    if change == "head-moved":
        after = ticket(*comments, head=HEAD_B)
    else:
        revise = say(
            "verdict-2",
            VERDICT.format(head=HEAD_A, verdict="REVISE"),
            "codex-login",
            3,
        )
        after = ticket(*comments, revise)
    for _poll in range(2):
        restart(tmp_path, shipper)[0].process(after)

    assert len(shipper.requests) == 1
    with queue.connect() as connection:
        outcome = connection.execute(
            "SELECT outcome FROM external_previews"
        ).fetchone()["outcome"]
    assert outcome == "ship-failed"
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]
    expected = ["stale"] if change == "newer-verdict" else []
    assert [reason for reason, _key in skipped_keys(queue)] == expected


def test_t8_losing_dispatcher_cannot_overwrite_shipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipper = FakeShipper()
    winner, queue = setup(tmp_path, shipper)
    loser, _queue = setup(tmp_path, FakeShipper())
    in_review(queue, HEAD_A)
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    stale_reads: list[str] = []
    writes: list[tuple[str, bool]] = []
    original = hook_t8._set_outcome

    def stale_read(
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> str | None:
        stale_reads.append(comment_id)
        return "verdict-1"

    def spy(
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        approve_id: str,
        outcome: str,
    ) -> bool:
        changed = original(dispatcher, ticket, approve_id, outcome)
        writes.append((outcome, changed))
        return changed

    winner.process(snapshot)
    monkeypatch.setattr(loser, "_is_handled", lambda *_args: False)
    monkeypatch.setattr(hook_t8, "_shipping_claim", stale_read)
    monkeypatch.setattr(hook_t8, "_set_outcome", spy)
    loser.process(snapshot)

    assert stale_reads == ["ok-1"]
    assert writes == [("ship-failed", False)]
    with queue.connect() as connection:
        outcome = connection.execute(
            "SELECT outcome FROM external_previews"
        ).fetchone()["outcome"]
    assert outcome == "shipped"
    assert len(shipper.requests) == 1
    assert jobs(queue) == []


def test_ready_never_reaches_the_hooks(tmp_path: Path) -> None:
    hook = GreedyHook((True, "claimed"))
    dispatcher, _queue = setup(tmp_path, hooks=(hook,))
    ready = (
        "<!-- orch:v1 kind=ready from=claude-bot role=builder "
        f"head={HEAD_A} -->\nReady.\n"
    )

    dispatcher.process(ticket(say("ready-1", ready, "claude-login", 1)))

    assert hook.seen == []


def test_t8_two_declines_competing_for_one_preview_queue_one_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, queue = setup(tmp_path, FakeShipper())
    second, _queue = setup(tmp_path, FakeShipper())
    in_review(queue, HEAD_A)
    approved = approve("verdict-1", HEAD_A, 1)

    first.process(ticket(approved, say("decline-1", DECLINE, "tracy-login", 2)))
    monkeypatch.setattr(hook_t8, "_claimed_by", lambda *_args: None)
    second.process(ticket(approved, say("decline-2", DECLINE, "tracy-login", 3)))

    assert jobs(queue) == [("fix", "claude")]
    assert skipped_keys(queue) == [("rejected", "decline-2")]
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT comment_id FROM external_previews"
        ).fetchall()
    assert [str(row["comment_id"]) for row in rows] == ["decline-1"]


def test_t8_stored_declines_do_not_rewrite_another_stuck_reason(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path, FakeShipper())
    dispatcher.process(ticket(stop("stuck-1", 1)))
    with queue.connect() as connection:
        for number in range(1, 4):
            connection.execute(
                """
                INSERT INTO external_previews (
                    repo, ticket_id, approve_comment_id, head, outcome, comment_id
                ) VALUES ('owner/repo', '42', ?, ?, 'declined', ?)
                """,
                (f"verdict-{number}", HEAD_A, f"decline-{number}"),
            )

    dispatcher.process(
        ticket(
            stop("stuck-1", 1),
            approve("verdict-4", HEAD_A, 2),
            say("ok-4", OK.format(head=HEAD_A), "tracy-login", 3),
            say("decline-5", DECLINE, "tracy-login", 4),
        )
    )

    with queue.connect() as connection:
        row = connection.execute(
            """
            SELECT stuck_reason, stuck_source_comment_id FROM ticket_state
            WHERE repo = 'owner/repo' AND ticket_id = '42'
            """
        ).fetchone()
    assert (row["stuck_reason"], row["stuck_source_comment_id"]) == (
        "t7-stuck",
        "stuck-1",
    )


class RecordingShipper:
    def __init__(self, inner: ReviewShipper):
        self.inner = inner
        self.requests: list[ShipRequest] = []

    def ship(self, request: ShipRequest) -> None:
        self.requests.append(request)
        self.inner.ship(request)


def wired(
    tmp_path: Path,
    actions: FakeReviewActions,
    source: FakeSource,
    *,
    grok_delivery: FakeGrokDelivery | None = None,
) -> tuple[Dispatcher, SQLiteJobQueue, RecordingShipper]:
    t8 = T8Hook()
    dispatcher, queue = setup(
        tmp_path,
        hooks=(T7Hook(), t8),
        actions=actions,
        grok_delivery=grok_delivery,
    )
    shipper = RecordingShipper(ReviewShipper(dispatcher, source))
    t8.shipper = shipper
    return dispatcher, queue, shipper


def ship_request(head: str = HEAD_A) -> ShipRequest:
    return ShipRequest(
        repo="owner/repo",
        ticket_id="42",
        head=head,
        approver="tracy-site",
        source_comment_id="ok-1",
        approve_comment_id="verdict-1",
    )


def outcomes(queue: SQLiteJobQueue) -> dict[str, str]:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT comment_id, outcome FROM handled_comments"
        ).fetchall()
    return {str(row["comment_id"]): str(row["outcome"]) for row in rows}


def workflow_state(queue: SQLiteJobQueue) -> str:
    with queue.connect() as connection:
        return str(
            connection.execute(
                """
                SELECT workflow_state FROM ticket_state
                WHERE repo = 'owner/repo' AND ticket_id = '42'
                """
            ).fetchone()[0]
        )


def test_p13_ok_covers_the_head_f1_05_previewed_after_a_patch_id_rebase(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    source = FakeSource(
        ticket(
            say("brief-1", BRIEF, "grok-login", 0),
            approve("verdict-1", HEAD_A, 1),
            say("ok-a", OK.format(head=HEAD_A), "tracy-login", 2),
            say("ok-b", OK.format(head=HEAD_B), "tracy-login", 3),
            head=HEAD_B,
        )
    )
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)

    assert actions.approvals == [(HEAD_B, "tracy-site")]
    assert outcomes(queue)["ok-a"] == "refused-ok-wrong-sha"
    assert outcomes(queue)["ok-b"] == "shipped"
    assert [request.head for request in shipper.requests] == [HEAD_B]
    assert actions.fast_forwards == [("main", HEAD_B)]
    assert actions.merges == []
    assert actions.states[-1] == "merged"
    assert workflow_state(queue) == "merged"


def test_p14_ok_on_a_stale_red_approve_never_reaches_ship(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    snapshot = ticket(
        say("brief-1", BRIEF, "grok-login", 0),
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    source = FakeSource(replace(snapshot, ci={HEAD_A: CIResult("failure")}))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)

    dispatcher.poll_once(source)

    assert outcomes(queue)["verdict-1"] == "stale-approve"
    assert outcomes(queue)["ok-1"] == "rejected-ok-not-waiting"
    assert shipper.requests == []
    assert actions.merges == []


SHORT_HEAD = HEAD_A[:7]


def set_intent(queue: SQLiteJobQueue, column: str, value: str) -> None:
    with queue.connect() as connection:
        connection.execute(
            f"UPDATE ship_intents SET {column} = ? WHERE comment_id = 'verdict-1'",
            (value,),
        )


@pytest.mark.parametrize(
    "gate",
    ["live", "intent-not-completed", "intent-policy", "intent-not-full-sha"],
)
def test_t8_preview_needs_a_completed_after_intent_on_a_full_sha(
    tmp_path: Path,
    gate: str,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    approved = approve("verdict-1", HEAD_A, 1)
    dispatcher.process(ticket(approved))
    head = HEAD_A
    if gate == "intent-not-completed":
        set_intent(queue, "status", "unconfirmed")
    elif gate == "intent-policy":
        set_intent(queue, "policy", "after:someone-else")
    elif gate == "intent-not-full-sha":
        head = SHORT_HEAD
        set_intent(queue, "head", head)

    dispatcher.process(
        ticket(approved, say("ok-1", OK.format(head=head), "tracy-login", 2), head=head)
    )

    if gate == "live":
        assert [request.head for request in shipper.requests] == [HEAD_A]
    else:
        assert outcomes(queue)["ok-1"] == "rejected-ok-not-waiting"
        assert shipper.requests == []


NO_INTENT = "no completed after:tracy-site ship intent"
NOT_LATEST = "latest review verdict is not APPROVE verdict-1"
NOT_BEFORE_OK = "APPROVE verdict-1 does not precede ok ok-1"
SHIPPER_GATES = {
    "stale-approve": NO_INTENT,
    "intent-not-completed": NO_INTENT,
    "intent-policy": NO_INTENT,
    "intent-head": NO_INTENT,
    "intent-approve-id": NO_INTENT,
    "fresh-revise": NOT_LATEST,
    "fresh-newer-approve": NOT_LATEST,
    "fresh-approve-edited": NOT_LATEST,
    "fresh-approve-deleted": NOT_LATEST,
    "fresh-ok-missing": NOT_BEFORE_OK,
    "fresh-ok-before-approve": NOT_BEFORE_OK,
    "ticket-policy": "ticket ship policy is merge",
    "head-moved": "PR head is",
    "red-ci": "is not green",
    "pending-ci": "is not green",
    "missing-ci": "is not green",
    "short-sha": "not a 40-character SHA",
    "long-sha": "not a 40-character SHA",
    "uppercase-sha": "not a 40-character SHA",
}
BRIEFED = say("brief-1", BRIEF, "grok-login", 0)
APPROVED = approve("verdict-1", HEAD_A, 1)
OK_1 = say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2)


def accepted(
    tmp_path: Path,
    actions: FakeReviewActions,
    *,
    review: bool = True,
) -> tuple[Dispatcher, SQLiteJobQueue, RecordingShipper, FakeSource, TrackedTicket]:
    """Poll a snapshot without the ok, then serve a fresh read that has it."""
    source = FakeSource(ticket(BRIEFED, APPROVED))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    if review:
        in_review(queue, HEAD_A)
    dispatcher.poll_once(source)
    fresh = ticket(BRIEFED, APPROVED, OK_1)
    source.tickets = (fresh,)
    return dispatcher, queue, shipper, source, fresh


@pytest.mark.parametrize("gate", sorted(SHIPPER_GATES))
def test_p14_review_shipper_raises_unless_every_gate_holds(
    tmp_path: Path,
    gate: str,
) -> None:
    actions = FakeReviewActions()
    _dispatcher, queue, shipper, source, fresh = accepted(
        tmp_path, actions, review=gate != "stale-approve"
    )
    request = ship_request()
    revise = say(
        "verdict-2", VERDICT.format(head=HEAD_A, verdict="REVISE"), "codex-login", 3
    )
    head = {
        "short-sha": SHORT_HEAD,
        "long-sha": HEAD_A + "a",
        "uppercase-sha": HEAD_A.upper(),
    }.get(gate)
    if head is not None:
        set_intent(queue, "head", head)
        request = replace(request, head=head)
        source.tickets = (ticket(*fresh.comments, head=head),)
    elif gate == "intent-not-completed":
        set_intent(queue, "status", "unconfirmed")
    elif gate == "intent-policy":
        set_intent(queue, "policy", "after:someone-else")
    elif gate == "intent-head":
        set_intent(queue, "head", HEAD_B)
    elif gate == "intent-approve-id":
        request = replace(request, approve_comment_id="verdict-0")
    elif gate == "fresh-revise":
        source.tickets = (replace(fresh, comments=fresh.comments + (revise,)),)
    elif gate == "fresh-newer-approve":
        newer = approve("verdict-2", HEAD_A, 3)
        source.tickets = (replace(fresh, comments=fresh.comments + (newer,)),)
    elif gate == "fresh-approve-edited":
        edited = replace(revise, id="verdict-1", created_at=1)
        source.tickets = (replace(fresh, comments=(BRIEFED, edited, OK_1)),)
    elif gate == "fresh-approve-deleted":
        source.tickets = (replace(fresh, comments=(BRIEFED, OK_1)),)
    elif gate == "fresh-ok-missing":
        source.tickets = (replace(fresh, comments=(BRIEFED, APPROVED)),)
    elif gate == "fresh-ok-before-approve":
        early = replace(OK_1, created_at=0.5)
        source.tickets = (replace(fresh, comments=(BRIEFED, early, APPROVED)),)
    elif gate == "ticket-policy":
        source.tickets = (replace(fresh, factory=factory("merge")),)
    elif gate == "head-moved":
        green = {HEAD_A: CIResult("success"), HEAD_B: CIResult("success")}
        source.tickets = (replace(fresh, current_head=HEAD_B, ci=green),)
    elif gate in ("red-ci", "pending-ci"):
        status = "failure" if gate == "red-ci" else "pending"
        source.tickets = (replace(fresh, ci={HEAD_A: CIResult(status)}),)
    elif gate == "missing-ci":
        source.tickets = (replace(fresh, ci={}),)

    with pytest.raises(ShipError, match=SHIPPER_GATES[gate]):
        shipper.inner.ship(request)

    assert actions.fast_forwards == []
    assert actions.branches == {"main": BASE}
    assert actions.merges == []
    assert "state:merged" not in actions.events
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM external_ship_intents"
        ).fetchone()[0] == 0


class SnapshotThenFreshSource(FakeSource):
    """The poll reads the snapshot; every later read is the fresh ticket."""

    def __init__(self, snapshot: TrackedTicket, fresh: TrackedTicket):
        super().__init__(snapshot)
        self.fresh = fresh
        self.reads = 0

    def tracked_tickets(
        self,
        refs: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]:
        self.reads += 1
        if self.reads > 1:
            self.tickets = (self.fresh,)
        return super().tracked_tickets(refs)


def test_b1_revise_posted_after_the_poll_snapshot_blocks_the_merge(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    snapshot = ticket(BRIEFED, APPROVED, OK_1)
    revise = say(
        "verdict-2", VERDICT.format(head=HEAD_A, verdict="REVISE"), "codex-login", 3
    )
    source = SnapshotThenFreshSource(
        snapshot, replace(snapshot, comments=snapshot.comments + (revise,))
    )
    dispatcher, queue, shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)

    assert [request.head for request in shipper.requests] == [HEAD_A]
    assert outcomes(queue)["ok-1"] == "ship-failed"
    assert actions.merges == []
    assert "state:merged" not in actions.events
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM external_ship_intents"
        ).fetchone()[0] == 0
    grok = delivery.jobs
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert "ShipError: latest review verdict is not APPROVE" in str(
        grok[0].request.detail
    )


def test_review_shipper_ships_when_every_gate_holds(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    _dispatcher, queue, shipper, _source, _fresh = accepted(tmp_path, actions)

    shipper.inner.ship(ship_request())

    assert actions.fast_forwards == [("main", HEAD_A)]
    assert actions.merges == []
    assert actions.events[-5:] == [
        "branch-tip:main",
        f"is-merged:{HEAD_A}",
        f"fast-forward:main:{HEAD_A}",
        "branch-tip:main",
        "state:merged",
    ]
    assert workflow_state(queue) == "merged"


@pytest.mark.parametrize("lookup", ["untracked", "none-found", "two-found"])
def test_review_shipper_refuses_a_ticket_it_cannot_look_up(
    tmp_path: Path,
    lookup: str,
) -> None:
    actions = FakeReviewActions()
    _dispatcher, queue, shipper, source, fresh = accepted(tmp_path, actions)
    if lookup == "untracked":
        with queue.connect() as connection:
            connection.execute("DELETE FROM tracked_tickets")
    else:
        source.tickets = () if lookup == "none-found" else (fresh, fresh)

    message = "is not tracked" if lookup == "untracked" else "was not found"
    with pytest.raises(ShipError, match=message):
        shipper.inner.ship(ship_request())
    assert actions.merges == []


def test_review_shipper_refuses_a_replayed_ok_with_another_head(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    _dispatcher, queue, shipper, _source, _fresh = accepted(tmp_path, actions)
    with queue.connect() as connection:
        connection.execute(
            """
            INSERT INTO external_ship_intents (
                repo, ticket_id, source_comment_id, head, status
            ) VALUES ('owner/repo', '42', 'ok-1', ?, 'completed')
            """,
            (HEAD_B,),
        )

    with pytest.raises(ShipError, match="does not match"):
        shipper.inner.ship(ship_request())
    assert actions.merges == []
    assert "state:merged" not in actions.events


@pytest.mark.parametrize("failure", ["label", "settle"])
def test_review_shipper_keeps_a_confirmed_merge_when_settling_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    source = FakeSource(
        ticket(
            say("brief-1", BRIEF, "grok-login", 0),
            approve("verdict-1", HEAD_A, 1),
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
        )
    )
    dispatcher, queue, shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)
    if failure == "label":
        actions.state_failures.add("merged")
    else:

        def fail(*_args: object) -> None:
            raise RuntimeError("state write failed")

        monkeypatch.setattr(dispatcher, "_settle_external_ship", fail)

    dispatcher.poll_once(source)

    assert outcomes(queue)["ok-1"] == "shipped"
    assert actions.fast_forwards == [("main", HEAD_A)]
    assert FakeBot(queue, "grok").drain() == []
    assert delivery.jobs == []
    actions.state_failures.clear()
    monkeypatch.undo()
    before = len(actions.states)
    dispatcher.poll_once(source)
    dispatcher.poll_once(source)
    assert actions.states[before:] == ["merged"]
    assert workflow_state(queue) == "merged"
    assert actions.fast_forwards == [("main", HEAD_A)]
    assert actions.merges == []


def test_r1b1_c_main_already_at_the_head_is_shipped_without_a_second_fast_forward(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    actions.branches["main"] = HEAD_A
    actions.merged_heads.add(HEAD_A)
    source = FakeSource(
        ticket(
            say("brief-1", BRIEF, "grok-login", 0),
            approve("verdict-1", HEAD_A, 1),
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
        )
    )
    dispatcher, queue, shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)
    shipper.inner.ship(ship_request())

    assert outcomes(queue)["ok-1"] == "shipped"
    assert actions.fast_forwards == []
    assert actions.merges == []
    assert actions.branches == {"main": HEAD_A}
    assert workflow_state(queue) == "merged"
    assert actions.states[-2:] == ["merged", "merged"]
    assert FakeBot(queue, "grok").drain() == []
    assert delivery.jobs == []


def test_review_shipper_raises_on_merge_error_and_never_retries(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    actions.fast_forward_error = RuntimeError("merge refused")
    source = FakeSource(
        ticket(
            say("brief-1", BRIEF, "grok-login", 0),
            approve("verdict-1", HEAD_A, 1),
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
        )
    )
    dispatcher, queue, shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)
    actions.fast_forward_error = None
    with pytest.raises(ShipError, match="is failed"):
        shipper.inner.ship(ship_request())

    assert outcomes(queue)["ok-1"] == "ship-failed"
    grok = delivery.jobs
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert "merge refused" in str(grok[0].request.detail)
    assert actions.fast_forwards == []
    assert actions.merges == []
    assert "state:merged" not in actions.events
    assert workflow_state(queue) == "waiting-external"


def test_review_shipper_never_merges_again_after_a_crash_inside_merge(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    actions.fast_forward_error = SystemExit("simulated crash")
    _dispatcher, queue, shipper, _source, _fresh = accepted(tmp_path, actions)
    with pytest.raises(SystemExit):
        shipper.inner.ship(ship_request())
    actions.fast_forward_error = None

    with pytest.raises(ShipError, match="no recorded result"):
        shipper.inner.ship(ship_request())
    assert actions.fast_forwards == []
    assert actions.merges == []

    actions.merged_heads.add(HEAD_A)
    with pytest.raises(ShipError, match="no recorded result"):
        shipper.inner.ship(ship_request())
    assert workflow_state(queue) == "waiting-external"

    actions.branches["main"] = HEAD_A
    shipper.inner.ship(ship_request())
    assert actions.fast_forwards == []
    assert actions.merges == []
    assert workflow_state(queue) == "merged"


def test_m1_a_settle_that_keeps_failing_wakes_grok_once_and_comments_continue(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    actions.state_failures.add("merged")
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1))
    dispatcher, queue, _shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)
    assert outcomes(queue)["ok-1"] == "shipped"
    assert jobs(queue) == []
    forged = say("ok-forged", OK.format(head=HEAD_A), "claude-login", 3)
    source.tickets = (ticket(BRIEFED, APPROVED, OK_1, forged),)
    dispatcher.poll_once(source)
    assert outcomes(queue)["ok-forged"] == "after-merge"
    assert ("ship-failed", "grok") in jobs(queue)
    for _poll in range(3):
        dispatcher.poll_once(source)

    assert jobs(queue) == [("ship-failed", "grok")]
    grok = delivery.jobs
    ship_failed = next(
        job for job in grok if job.request.action == "ship-failed"
    )
    assert ship_failed.request.source_comment_id == "ok-1"
    assert "failed 3 times" in str(ship_failed.request.detail)
    assert actions.fast_forwards == [("main", HEAD_A)]
    assert actions.merges == []
    assert workflow_state(queue) == "merged"
    assert actions.states.count("merged") == 6


def test_m2_external_ship_intents_gains_settle_columns_idempotently(
    tmp_path: Path,
) -> None:
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: 100.0)
    with queue.connect() as connection:
        connection.execute(
            """
            CREATE TABLE external_ship_intents (
                repo TEXT NOT NULL,
                ticket_id TEXT NOT NULL,
                source_comment_id TEXT NOT NULL,
                head TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                PRIMARY KEY (repo, ticket_id, source_comment_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO external_ship_intents
                (repo, ticket_id, source_comment_id, head, status)
            VALUES ('owner/repo', '42', 'ok-1', ?, 'completed')
            """,
            (HEAD_A,),
        )

    setup(tmp_path)
    setup(tmp_path)

    with queue.connect() as connection:
        row = connection.execute(
            "SELECT settled, settle_failures FROM external_ship_intents"
        ).fetchone()
    assert tuple(row) == (0, 0)


def test_ff_ship_lands_main_exactly_on_the_approved_sha_with_no_new_commit(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    commits = dict(actions.parents)
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)

    assert outcomes(queue)["ok-1"] == "shipped"
    assert [request.head for request in shipper.requests] == [HEAD_A]
    assert actions.branches == {"main": HEAD_A}
    assert actions.fast_forwards == [("main", HEAD_A)]
    assert actions.parents == commits
    assert actions.merges == []
    assert workflow_state(queue) == "merged"


@pytest.mark.parametrize("history", ["main-moved", "diverged"])
def test_ff_ship_raises_on_a_non_fast_forward_and_leaves_main_untouched(
    tmp_path: Path,
    history: str,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    if history == "main-moved":
        actions.branches["main"] = MOVED
    else:
        actions.parents[HEAD_A] = None
    main_before = dict(actions.branches)
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1))
    dispatcher, queue, shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)

    assert [request.head for request in shipper.requests] == [HEAD_A]
    assert outcomes(queue)["ok-1"] == "ship-failed"
    assert actions.branches == main_before
    assert actions.fast_forwards == []
    assert actions.merges == []
    assert "state:merged" not in actions.events
    grok = delivery.jobs
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert "is not a fast-forward of main" in str(grok[0].request.detail)
    with pytest.raises(ShipError, match="is failed"):
        shipper.inner.ship(ship_request())
    assert actions.branches == main_before
    assert actions.merges == []


def test_after_repo_is_never_shipped_with_squash(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    _dispatcher, _queue, shipper, _source, _fresh = accepted(tmp_path, actions)

    shipper.inner.ship(ship_request())

    assert actions.merges == []
    assert actions.fast_forwards == [("main", HEAD_A)]


def test_ff_mode_t8_calls_ship_at_most_once_across_restart_and_repeated_ok(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)
    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    again = say("ok-2", OK.format(head=HEAD_A), "tracy-login", 3)
    source.tickets = (ticket(BRIEFED, APPROVED, OK_1, again),)
    restarted, queue, reshipper = wired(tmp_path, actions, source)
    restarted.poll_once(source)

    assert len(shipper.requests) + len(reshipper.requests) == 1
    assert outcomes(queue)["ok-1"] == "shipped"
    assert "ok-2" not in outcomes(queue)
    assert actions.fast_forwards == [("main", HEAD_A)]
    assert actions.merges == []


@pytest.mark.parametrize("head", [SHORT_HEAD, HEAD_A.upper(), HEAD_A + "a"])
def test_ff_ship_head_refuses_a_sha_that_is_not_full_lowercase(
    tmp_path: Path,
    head: str,
) -> None:
    actions = FakeReviewActions()
    actions.parents[head] = BASE
    dispatcher, _queue = setup(tmp_path, actions=actions)

    with pytest.raises(ShipError, match="not a 40-character SHA"):
        dispatcher._ship_head(ticket(head=head), head)

    assert actions.fast_forwards == []
    assert actions.branches == {"main": BASE}
    assert actions.merges == []


def test_fu1_tied_comment_ids_order_as_integers(tmp_path: Path) -> None:
    dispatcher, _queue = setup(tmp_path)
    revise_9 = say(
        "9", VERDICT.format(head=HEAD_A, verdict="REVISE"), "codex-login", 5
    )
    approve_10 = approve("10", HEAD_A, 5)

    latest = dispatcher._latest_review_verdict(ticket(approve_10, revise_9))

    assert latest is not None
    assert latest[0].id == "10"
    assert latest[1]["verdict"] == "APPROVE"


def test_fu1_ok_with_a_tied_higher_id_follows_the_approve(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    approved = approve("9", HEAD_A, 5)
    dispatcher.process(ticket(approved))

    dispatcher.process(
        ticket(approved, say("10", OK.format(head=HEAD_A), "tracy-login", 5))
    )

    assert outcomes(queue)["10"] == "shipped"
    assert [request.approve_comment_id for request in shipper.requests] == ["9"]


class RaisingHook:
    def initialize(self, dispatcher: Dispatcher) -> None:
        del dispatcher

    def handle(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        fields: object,
    ) -> tuple[bool, str] | None:
        del dispatcher, ticket, comment, fields
        raise RuntimeError("comment loop failed")


def test_n1_settle_runs_when_the_comment_loop_raises(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, hooks=(RaisingHook(),), actions=actions)
    with queue.connect() as connection:
        connection.execute(
            """
            INSERT INTO external_ship_intents
                (repo, ticket_id, source_comment_id, head, status)
            VALUES ('owner/repo', '42', 'ok-1', ?, 'completed')
            """,
            (HEAD_A,),
        )

    with pytest.raises(RuntimeError, match="comment loop failed"):
        dispatcher.process(ticket(say("any-1", BRIEF, "grok-login", 0)))

    assert actions.states == ["merged"]
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT settled FROM external_ship_intents"
        ).fetchone()[0] == 1


SQUASHED = "d" * 40
MERGE_COMMIT = "e" * 40


@pytest.mark.parametrize(
    ("tip", "parent"),
    [(SQUASHED, BASE), (MERGE_COMMIT, HEAD_A)],
    ids=["a-squash-merged-elsewhere", "b-main-ahead-via-merge-commit"],
)
def test_r1b1_merged_elsewhere_is_refused_with_one_wake(
    tmp_path: Path,
    tip: str,
    parent: str,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    actions.merged_by_someone_else(HEAD_A, tip, parent)
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1))
    dispatcher, queue, shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    assert [request.head for request in shipper.requests] == [HEAD_A]
    assert outcomes(queue)["ok-1"] == "ship-failed"
    assert actions.branches == {"main": tip}
    assert actions.fast_forwards == []
    assert actions.merges == []
    assert "state:merged" not in actions.events
    assert workflow_state(queue) != "merged"
    grok = delivery.jobs
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert f"main is {tip}, not {HEAD_A}" in str(grok[0].request.detail)
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT status FROM external_ship_intents"
        ).fetchone()[0] == "failed"


def test_m1_fast_forward_moves_the_configured_base_branch(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    actions.branches = {"release": BASE}
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1, base_branch="release"))
    dispatcher, queue, _shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)

    assert outcomes(queue)["ok-1"] == "shipped"
    assert actions.branches == {"release": HEAD_A}
    assert actions.fast_forwards == [("release", HEAD_A)]
    assert actions.merges == []


def test_m2_review_shipper_orders_tied_approve_9_before_ok_10(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    approved = approve("9", HEAD_A, 1)
    source = FakeSource(ticket(BRIEFED, approved))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)
    dispatcher.poll_once(source)
    source.tickets = (
        ticket(BRIEFED, approved, say("10", OK.format(head=HEAD_A), "tracy-login", 1)),
    )

    shipper.inner.ship(
        replace(ship_request(), source_comment_id="10", approve_comment_id="9")
    )

    assert actions.fast_forwards == [("main", HEAD_A)]
    assert workflow_state(queue) == "merged"


def test_m4_comment_loop_error_survives_a_failing_settle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, _queue = setup(tmp_path, hooks=(RaisingHook(),))
    settles: list[str] = []

    def fail(ticket: TrackedTicket) -> None:
        settles.append(ticket.ticket_id)
        raise ValueError("settle failed")

    monkeypatch.setattr(dispatcher, "_settle_external_ships", fail)

    with pytest.raises(RuntimeError, match="comment loop failed") as raised:
        dispatcher.process(ticket(say("any-1", BRIEF, "grok-login", 0)))

    assert settles == ["42"]
    assert any(
        "ValueError: settle failed" in note
        for note in getattr(raised.value, "__notes__", [])
    )


CONCURRENT = "f" * 40


@pytest.mark.parametrize(
    "after_fast_forward",
    ["1-base-left-elsewhere", "2-tip-re-read-raises"],
)
def test_r2b1_tip_re_read_after_fast_forward_fails_closed(
    tmp_path: Path,
    after_fast_forward: str,
) -> None:
    actions = FakeReviewActions()
    delivery = FakeGrokDelivery()
    if after_fast_forward == "1-base-left-elsewhere":
        actions.parents[CONCURRENT] = HEAD_A
        actions.fast_forward_lands_on = CONCURRENT
        expected = f"main is {CONCURRENT}, not {HEAD_A}, after fast-forward"
    else:
        actions.branch_tip_error_after_fast_forward = RuntimeError("tip read failed")
        # #48 G2 F2.3: only the class (or an inner safe_detail), never
        # str(exc), of the failed tip read reaches the error and the wake.
        expected = f"main is unreadable (RuntimeError), not {HEAD_A}, after fast-forward"
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1))
    dispatcher, queue, shipper = wired(
        tmp_path,
        actions,
        source,
        grok_delivery=delivery,
    )
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    assert [request.head for request in shipper.requests] == [HEAD_A]
    assert outcomes(queue)["ok-1"] == "ship-failed"
    assert actions.fast_forwards == [("main", HEAD_A)]
    assert actions.merges == []
    assert "state:merged" not in actions.events
    assert workflow_state(queue) != "merged"
    grok = delivery.jobs
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert "raised ShipError: " in str(grok[0].request.detail)
    assert expected in str(grok[0].request.detail)
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT status FROM external_ship_intents"
        ).fetchone()[0] == "failed"
        assert connection.execute(
            "SELECT outcome FROM external_previews"
        ).fetchone()[0] == "ship-failed"
    with pytest.raises(ShipError, match="is failed"):
        shipper.inner.ship(ship_request())
    assert actions.fast_forwards == [("main", HEAD_A)]


def test_r2m2_review_shipper_refuses_tied_ok_9_before_approve_10(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    approved = approve("10", HEAD_A, 1)
    source = FakeSource(ticket(BRIEFED, approved))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)
    dispatcher.poll_once(source)
    source.tickets = (
        ticket(BRIEFED, approved, say("9", OK.format(head=HEAD_A), "tracy-login", 1)),
    )

    with pytest.raises(ShipError, match="APPROVE 10 does not precede ok 9"):
        shipper.inner.ship(
            replace(ship_request(), source_comment_id="9", approve_comment_id="10")
        )

    assert actions.fast_forwards == []
    assert actions.merges == []
    assert workflow_state(queue) != "merged"
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM external_ship_intents"
        ).fetchone()[0] == 0


def test_r2m2_t8_refuses_tied_ok_9_before_approve_10(tmp_path: Path) -> None:
    """The preview is live, so only T8's own ordering guard can refuse ok 9."""
    actions = FakeReviewActions()
    approved = approve("10", HEAD_A, 1)
    source = FakeSource(ticket(BRIEFED, approved))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)
    dispatcher.poll_once(source)
    assert outcomes(queue)["10"] == "waiting-external"
    assert dispatcher._ship_intent(ticket(), "10") == (
        HEAD_A,
        "after:tracy-site",
        "completed",
    )

    source.tickets = (
        ticket(BRIEFED, approved, say("9", OK.format(head=HEAD_A), "tracy-login", 1)),
    )
    dispatcher.poll_once(source)

    assert outcomes(queue)["9"] == "rejected-ok-not-waiting"
    assert shipper.requests == []
    assert actions.fast_forwards == []
    assert actions.merges == []
    assert workflow_state(queue) == "waiting-external"


@pytest.mark.parametrize(
    ("author", "ships"),
    [("mallory-login", True), ("codex-login", False)],
    ids=["untrusted-revise-is-ignored", "trusted-revise-blocks"],
)
def test_a_later_revise_blocks_the_ship_only_from_a_trusted_reviewer(
    tmp_path: Path,
    author: str,
    ships: bool,
) -> None:
    actions = FakeReviewActions()
    revise = say(
        "verdict-2", VERDICT.format(head=HEAD_A, verdict="REVISE"), author, 1.5
    )
    source = FakeSource(ticket(BRIEFED, APPROVED, revise, OK_1))
    dispatcher, queue, shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)

    dispatcher.poll_once(source)

    assert outcomes(queue)["verdict-1"] == "waiting-external"
    if ships:
        assert outcomes(queue)["verdict-2"] == "rejected-revise-author"
        assert outcomes(queue)["ok-1"] == "shipped"
        assert [request.head for request in shipper.requests] == [HEAD_A]
        assert actions.fast_forwards == [("main", HEAD_A)]
        assert workflow_state(queue) == "merged"
    else:
        assert outcomes(queue)["ok-1"] == "rejected-ok-not-waiting"
        assert shipper.requests == []
        assert actions.fast_forwards == []
        assert workflow_state(queue) != "merged"
    assert actions.merges == []


@pytest.mark.parametrize(
    "late",
    [
        say("late", DECLINE, "tracy-login", 2),
        say("late", OK.format(head=HEAD_A), "tracy-login", 2),
        say("late", VERDICT.format(head=HEAD_A, verdict="BLOCKED"), "codex-login", 2),
        stop("late", 2, state="blocked"),
        say("late", "<!-- orch:v1 kind=nonsense -->\nHuh.\n", "claude-login", 2),
    ],
    ids=["t8-decline", "t8-ok", "t7-blocked", "t7-stuck", "t7-unparsed"],
)
def test_no_hook_acts_on_a_merged_ticket(tmp_path: Path, late: Comment) -> None:
    """#108: merged is absorbing for hooks too. The approver was shown a
    preview, the PR was then merged; a late decline, ok or stop is handled
    as after-merge with no job, wake or ship."""
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
    in_review(queue, HEAD_A)
    approved = approve("1", HEAD_A, 1)
    dispatcher.process(ticket(approved))
    before = jobs(queue)
    dispatcher.record_terminal("owner/repo", "42", "merged")

    dispatcher.process(ticket(approved, late))

    assert outcomes(queue)["late"] == "after-merge"
    assert jobs(queue) == before
    assert shipper.requests == []
    assert workflow_state(queue) == "merged"


OLD_TICKET_STATE = """
CREATE TABLE ticket_state (
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    gate_count INTEGER NOT NULL DEFAULT 0,
    stuck INTEGER NOT NULL DEFAULT 0,
    stuck_reason TEXT,
    stuck_source_comment_id TEXT,
    timeout_seconds INTEGER,
    generation INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (repo, ticket_id)
)
"""


class PauseAfterColumnRead:
    """Hold a connection right after it reads ticket_state's columns."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        read: threading.Event,
        resume: threading.Event,
    ):
        self.connection = connection
        self.read = read
        self.resume = resume

    def __enter__(self) -> "PauseAfterColumnRead":
        self.connection.__enter__()
        return self

    def __exit__(self, *exc: object) -> object:
        return self.connection.__exit__(*exc)

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)

    def execute(self, sql: str, *args: object) -> object:
        cursor = self.connection.execute(sql, *args)
        if sql.strip() != "PRAGMA table_info(ticket_state)" or self.read.is_set():
            return cursor
        rows = cursor.fetchall()
        self.read.set()
        self.resume.wait(1)
        return FetchedRows(rows)


class FetchedRows:
    def __init__(self, rows: list[sqlite3.Row]):
        self.rows = rows

    def fetchall(self) -> list[sqlite3.Row]:
        return self.rows


def test_initialize_migrations_are_race_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "d.db"
    with sqlite3.connect(path) as connection:
        connection.execute(OLD_TICKET_STATE)
    read = threading.Event()
    resume = threading.Event()
    plain = Dispatcher._connect

    def connect(self: Dispatcher) -> object:
        connection = plain(self)
        if threading.current_thread().name == "first":
            return PauseAfterColumnRead(connection, read, resume)
        return connection

    monkeypatch.setattr(Dispatcher, "_connect", connect)
    errors: list[BaseException] = []

    def first() -> None:
        try:
            setup(tmp_path)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=first, name="first")
    thread.start()
    assert read.wait(5)

    setup(tmp_path)
    resume.set()
    thread.join(10)

    assert errors == []
    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ticket_state)")
        }
    assert {"stuck_wake_job_id", "workflow_state", "cap_extra"} <= columns


def test_t4_replay_after_settle_keeps_merged(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    source = FakeSource(ticket(BRIEFED, APPROVED, OK_1))
    dispatcher, queue, _shipper = wired(tmp_path, actions, source)
    in_review(queue, HEAD_A)
    dispatcher.poll_once(source)
    assert outcomes(queue)["ok-1"] == "shipped"
    assert workflow_state(queue) == "merged"
    with queue.connect() as connection:
        connection.execute(
            "DELETE FROM handled_comments WHERE comment_id = 'verdict-1'"
        )

    dispatcher.poll_once(source)

    assert outcomes(queue)["verdict-1"] == "ship-already-completed-after:tracy-site"
    assert actions.states[-1] == "merged"
    assert "waiting-external" not in actions.states[actions.states.index("merged"):]
    assert workflow_state(queue) == "merged"


def test_edited_older_verdict_does_not_override_a_newer_approve(
    tmp_path: Path,
) -> None:
    actions = FakeReviewActions()
    _dispatcher, queue, shipper, source, fresh = accepted(tmp_path, actions)
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO handled_comments VALUES ('owner/repo', '42', 'verdict-0', 'waiting')"
        )
    edited = say(
        "verdict-0", VERDICT.format(head=HEAD_A, verdict="REVISE"), "codex-login", 0.5
    )
    source.tickets = (replace(fresh, comments=fresh.comments + (edited,)),)

    _dispatcher.poll_once(source)

    assert outcomes(queue)["ok-1"] == "shipped"
    assert [request.approve_comment_id for request in shipper.requests] == ["verdict-1"]
    assert actions.fast_forwards == [("main", HEAD_A)]
    assert workflow_state(queue) == "merged"


def test_approve_on_red_ci_wakes_grok_once(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    in_review(queue, HEAD_A)
    red = replace(
        ticket(BRIEFED, APPROVED, ship="handoff"),
        ci={HEAD_A: CIResult("failure")},
    )

    dispatcher.process(red)
    dispatcher.process(red)

    assert outcomes(queue)["verdict-1"] == "approve-ci-red"
    assert jobs(queue) == [("blocked", "grok")]
    with queue.connect() as connection:
        head, detail = connection.execute(
            "SELECT head, detail FROM jobs WHERE action = 'blocked'"
        ).fetchone()
    assert head == HEAD_A
    assert f"landed on red CI at {HEAD_A}" in detail


def test_approve_on_red_ci_while_held_waits_without_a_wake(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    in_review(queue, HEAD_A)
    with queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'awaiting-ok'"
        )

    dispatcher.process(
        replace(
            ticket(BRIEFED, APPROVED, ship="handoff"),
            ci={HEAD_A: CIResult("failure")},
        )
    )

    assert "verdict-1" not in outcomes(queue)
    assert jobs(queue) == []
