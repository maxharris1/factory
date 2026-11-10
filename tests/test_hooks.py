import ast
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib import hook_t8
from lib.dispatcher import (
    CIResult,
    Comment,
    Dispatcher,
    Hook,
    TicketRef,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig
from lib.hook_t7 import T7Hook
from lib.hook_t8 import ShipRequest, Shipper, T8Hook
from lib.queue import ACTIONS, Job, JobRequest, SQLiteJobQueue

TRUSTED_LOGINS = {
    "grok-login": "grok-bot",
    "claude-login": "claude-bot",
    "codex-login": "codex-bot",
    "tracy-login": "tracy-site",
}
VERDICT = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer "
    "head={head} verdict={verdict} -->\nVerdict.\n"
)
STOP = "<!-- orch:v1 kind=stuck from={sender} role={role}{state} -->\nStop.\n"
OK = "<!-- orch:v1 kind=ok from=tracy-site role=approver head={head} -->\nOK.\n"
DECLINE = "<!-- orch:v1 kind=decline from=tracy-site role=approver -->\nNo.\n"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


class FakeShipper:
    def __init__(self) -> None:
        self.requests: list[ShipRequest] = []

    def ship(self, request: ShipRequest) -> None:
        self.requests.append(request)


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


def factory(ship: str = "after:tracy-site") -> FactoryConfig:
    return FactoryConfig(
        base_branch="main",
        tickets="github",
        builder_harness="claude",
        reviewer_harness="codex",
        rounds_low=1,
        rounds_high=3,
        verify="none",
        ship=ship,
        skills=(),
    )


def ticket(
    *comments: Comment,
    head: str | None = HEAD_A,
    ship: str = "after:tracy-site",
    ticket_id: str = "42",
) -> TrackedTicket:
    return TrackedTicket(
        repo="owner/repo",
        ticket_id=ticket_id,
        factory=factory(ship),
        comments=comments,
        current_head=head,
        ci={head: CIResult("success")} if head else {},
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
) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    clock = now if now is not None else [100.0]
    queue = SQLiteJobQueue(path, clock=lambda: clock[0])
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        hooks=hooks if hooks is not None else (T7Hook(), T8Hook(shipper)),
    )
    return dispatcher, queue


def restart(
    tmp_path: Path,
    shipper: Shipper | None = None,
) -> tuple[Dispatcher, SQLiteJobQueue]:
    return setup(tmp_path, shipper)


def jobs(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT action, recipient FROM jobs ORDER BY created_at, id"
        ).fetchall()
    return [(str(row["action"]), str(row["recipient"])) for row in rows]


def test_t7_blocked_verdict_wakes_grok_once(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    blocked = say(
        "verdict-1",
        VERDICT.format(head=HEAD_A, verdict="BLOCKED"),
        "codex-login",
        1,
    )

    dispatcher.process(ticket(blocked))

    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["blocked"]
    assert grok[0].request.head == HEAD_A
    assert grok[0].request.source_comment_id == "verdict-1"
    assert FakeBot(queue, "claude").drain() == []
    assert FakeBot(queue, "codex").drain() == []


def test_t7_single_wake_on_repeated_terminal_comments(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    blocked = VERDICT.format(head=HEAD_A, verdict="BLOCKED")
    first = ticket(
        say("blocked-1", blocked, "codex-login", 1),
        say("blocked-2", blocked, "codex-login", 2),
        stop("capped-1", 3, state="capped", sender="grok-bot", author="grok-login"),
        stop("capped-2", 4, state="capped", sender="grok-bot", author="grok-login"),
    )
    dispatcher.process(first)
    grok = FakeBot(queue, "grok")
    assert sorted(job.request.action for job in grok.drain()) == ["blocked", "capped"]

    restarted, restarted_queue = restart(tmp_path)
    again = replace(
        first,
        comments=first.comments
        + (
            say("blocked-3", blocked, "codex-login", 5),
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
    stuck = FakeBot(restarted_queue, "grok").drain()
    assert [job.request.action for job in stuck] == ["stuck"]
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

    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["unparsed"]
    assert grok[0].request.source_comment_id == "bad-1"
    assert "unknown kind" in str(grok[0].request.detail)
    assert len(jobs(queue)) == 1


def test_t7_config_refused_wakes_grok_once_across_restart(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    for target in (dispatcher, restart(tmp_path)[0], restart(tmp_path)[0]):
        target.wake_grok_once(
            repo="owner/repo",
            ticket_id="42",
            reason="refused",
            source_comment_id="factory.md@abc1234",
            timeout_seconds=5400,
            detail="missing required field 'ship'",
        )

    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["refused"]


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


def test_t8_ok_on_previewed_sha_ships_that_sha(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)

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

    unwired, _queue = setup(tmp_path / "unwired")
    unwired.process(ticket(*approved_then_ok))
    shipper = FakeShipper()
    rewired, _queue = setup(tmp_path / "unwired", shipper)
    rewired.process(ticket(*approved_then_ok, revise))
    assert shipper.requests == []

    one_poll_shipper = FakeShipper()
    one_poll, queue = setup(tmp_path / "one-poll", one_poll_shipper)
    declined = say("decline-1", DECLINE, "tracy-login", 2.5)
    one_poll.process(ticket(*approved_then_ok, declined, revise))
    assert one_poll_shipper.requests == []
    assert jobs(queue) == []


def test_t8_ok_on_any_other_sha_is_refused(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, _queue = setup(tmp_path, shipper)
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
    dispatcher, _queue = setup(tmp_path, shipper)
    revise = say(
        "verdict-2",
        VERDICT.format(head=HEAD_A, verdict="REVISE"),
        "codex-login",
        2,
    )

    dispatcher.process(ticket(say("ok-0", OK.format(head=HEAD_A), "tracy-login", 0)))
    dispatcher.process(
        ticket(
            approve("verdict-1", HEAD_A, 1),
            revise,
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 3),
        )
    )
    dispatcher.process(
        ticket(
            approve("verdict-3", HEAD_A, 4),
            say("ok-2", OK.format(head=HEAD_A), "tracy-login", 5),
            head=HEAD_B,
        )
    )

    assert shipper.requests == []


@pytest.mark.parametrize(
    ("author", "ship"),
    [("claude-login", "after:tracy-site"), ("tracy-login", "merge")],
)
def test_t8_ok_from_non_approver_or_without_after_ship_is_refused(
    tmp_path: Path,
    author: str,
    ship: str,
) -> None:
    shipper = FakeShipper()
    dispatcher, _queue = setup(tmp_path, shipper)

    dispatcher.process(
        ticket(
            approve("verdict-1", HEAD_A, 1),
            say("ok-1", OK.format(head=HEAD_A), author, 2),
            ship=ship,
        )
    )

    assert shipper.requests == []


def test_t8_ok_without_a_wired_shipper_stays_unhandled(tmp_path: Path) -> None:
    dispatcher, _queue = setup(tmp_path)
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
    dispatcher, _queue = setup(tmp_path, shipper)
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
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("decline-1", DECLINE, "tracy-login", 2),
        say("decline-2", DECLINE, "tracy-login", 3),
    )

    dispatcher.process(snapshot)
    restart(tmp_path)[0].process(snapshot)

    assert jobs(queue) == [("fix", "claude")]


def test_t8_decline_naming_another_sha_is_refused(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, FakeShipper())
    body = DECLINE.replace("role=approver", f"role=approver head={HEAD_B}")

    dispatcher.process(
        ticket(approve("verdict-1", HEAD_A, 1), say("decline-1", body, "tracy-login", 2))
    )

    assert jobs(queue) == []


def test_no_post_merge_job_is_queued_in_phase_1(tmp_path: Path) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)

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
    dispatcher.process(
        ticket(
            say("bad-1", "<!-- orch:v1 kind=nope -->\n", "claude-login", 1),
            approve("verdict-1", HEAD_A, 2),
            say("decline-1", DECLINE, "tracy-login", 3),
        )
    )
    assert {action for action, _recipient in jobs(queue)} == {"unparsed", "fix"}

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

    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]:
        return self.tickets

    def linear_candidates(self, query: str) -> tuple[TrackedTicket, ...]:
        return ()

    def tracked_tickets(
        self,
        refs: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]:
        wanted = {ref.ticket_id for ref in refs}
        return tuple(item for item in self.tickets if item.ticket_id in wanted)


def test_t8_preview_is_a_full_sha_and_short_heads_are_not_expanded(
    tmp_path: Path,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)

    dispatcher.process(
        ticket(
            approve("verdict-1", HEAD_A[:7], 1),
            say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
        )
    )
    full = (
        approve("verdict-2", HEAD_A, 3),
        say("ok-2", OK.format(head=HEAD_A[:7]), "tracy-login", 4),
    )
    dispatcher.process(ticket(*full))
    assert shipper.requests == []

    dispatcher.process(
        ticket(*full, say("ok-3", OK.format(head=HEAD_A), "tracy-login", 5))
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
    snapshot = ticket(
        approve("verdict-1", HEAD_A, 1),
        say("ok-1", OK.format(head=HEAD_A), "tracy-login", 2),
    )
    racing.other = second
    racing.snapshot = snapshot

    first.process(snapshot)

    assert len(racing.requests) == 1
    assert other_shipper.requests == []
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]


def test_t8_crash_after_ship_returns_never_ships_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipper = FakeShipper()
    dispatcher, queue = setup(tmp_path, shipper)
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
    dispatcher, queue = setup(tmp_path, shipper, now=now)
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
    grok = FakeBot(queue, "grok").drain()
    assert [job.request.action for job in grok] == ["ship-failed"]
    assert "merge refused" in str(grok[0].request.detail)


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
            say("bad-2", "orch:v1 kind=ready", "codex-login", 2),
            say("bad-3", "orch:v1 kind=ready", "someone-else", 3),
        )
    )

    grok = FakeBot(queue, "grok").drain()
    assert sorted(job.request.source_comment_id for job in grok) == ["bad-1", "bad-2"]
    assert {job.request.action for job in grok} == {"unparsed"}


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
        ("refused", HEAD_A),
    ]
