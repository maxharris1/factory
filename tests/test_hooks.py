import ast
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, Comment, Dispatcher, TrackedTicket
from lib.factory_md import FactoryConfig
from lib.hook_t7 import T7Hook
from lib.hook_t8 import ShipRequest, T8Hook
from lib.queue import ACTIONS, Job, SQLiteJobQueue

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
        self.queue.finish(job.id, handoff_comment_id=f"handoff-{job.id}")
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
) -> TrackedTicket:
    return TrackedTicket(
        repo="owner/repo",
        ticket_id="42",
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
    shipper: FakeShipper | None = None,
) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        hooks=(T7Hook(), T8Hook(shipper)),
    )
    return dispatcher, queue


def restart(
    tmp_path: Path,
    shipper: FakeShipper | None = None,
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
        VERDICT.format(head=HEAD_A[:7], verdict="BLOCKED"),
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
            say("ok-1", OK.format(head=HEAD_A[:7]), "tracy-login", 2),
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
