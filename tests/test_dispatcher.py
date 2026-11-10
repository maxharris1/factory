import ast
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import (
    CIResult,
    Comment,
    Dispatcher,
    PollLoop,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig
from lib.queue import SQLiteJobQueue

GO = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator -->\nGo.\n"
READY = (
    "<!-- orch:v1 kind=ready from=claude-bot role=builder head={head} -->\n"
    "Ready.\n"
)


class FakeSource:
    def __init__(self, *tickets: TrackedTicket):
        self.tickets = tickets
        self.calls = 0

    def tracked_tickets(self) -> tuple[TrackedTicket, ...]:
        self.calls += 1
        return self.tickets


def factory() -> FactoryConfig:
    return FactoryConfig(
        base_branch="main",
        tickets="github",
        builder_harness="claude",
        reviewer_harness="codex",
        rounds_low=1,
        rounds_high=3,
        verify="none",
        ship="merge",
        skills=(),
    )


def comment(
    comment_id: str,
    body: str,
    *,
    author: str,
    created_at: float = 1,
) -> Comment:
    return Comment(
        id=comment_id,
        author=author,
        body=body,
        created_at=created_at,
    )


def ticket(
    *comments: Comment,
    head: str | None = None,
    ci: dict[str, CIResult] | None = None,
    requires_plan: bool = False,
    timeout: int = 90,
) -> TrackedTicket:
    return TrackedTicket(
        repo="owner/repo",
        ticket_id="42",
        factory=factory(),
        comments=comments,
        current_head=head,
        ci=ci or {},
        requires_plan=requires_plan,
        job_timeout_minutes=timeout,
    )


def setup(
    tmp_path: Path,
    now: list[float] | None = None,
) -> tuple[Dispatcher, SQLiteJobQueue]:
    current = now if now is not None else [100.0]
    queue = SQLiteJobQueue(tmp_path / "queue.sqlite3")
    dispatcher = Dispatcher(
        tmp_path / "dispatcher.sqlite3",
        queue,
        clock=lambda: current[0],
    )
    return dispatcher, queue


@pytest.mark.parametrize(
    ("requires_plan", "action"),
    [(False, "build"), (True, "plan")],
)
def test_t1_reads_kind_go_and_queues_the_builder(
    tmp_path: Path,
    requires_plan: bool,
    action: str,
) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(
        ticket(
            comment("go-1", GO, author="grok-bot"),
            requires_plan=requires_plan,
        )
    )

    job = queue.claim("claude", now=101)
    assert job is not None
    assert job.request.action == action
    assert job.request.recipient == "claude"
    assert job.request.source_comment_id == "go-1"
    assert job.request.timeout_seconds == 90 * 60


def test_t1_fails_closed_for_a_forged_go_comment(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(ticket(comment("go-1", GO, author="someone-else")))

    assert queue.claim("claude", now=101) is None


def test_restart_and_semantic_duplicate_do_not_queue_t1_twice(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    first = comment("go-1", GO, author="grok-bot", created_at=1)
    duplicate = comment("go-2", GO, author="grok-bot", created_at=2)
    snapshot = ticket(first, duplicate)
    dispatcher.process(snapshot)

    restarted = Dispatcher(
        tmp_path / "dispatcher.sqlite3",
        SQLiteJobQueue(tmp_path / "queue.sqlite3"),
        clock=lambda: 200,
    )
    restarted.process(snapshot)

    assert queue.claim("claude", now=201) is not None
    assert queue.claim("claude", now=202) is None


def test_t2_waits_for_ci_then_queues_cross_family_review(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 7
    ready = comment("ready-1", READY.format(head=head), author="claude-bot")
    pending = ticket(ready, head=head, ci={head: CIResult("pending")})
    dispatcher.process(pending)
    assert queue.claim("codex", now=101) is None

    dispatcher.process(replace(pending, ci={head: CIResult("success")}))
    review = queue.claim("codex", now=102)
    assert review is not None
    assert review.request.action == "review"
    assert review.request.head == head
    assert dispatcher.gate_count(pending) == 0


def test_t2_marks_a_stale_ready_handled_without_queueing(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    old_head = "a" * 7
    current_head = "b" * 7
    stale = ticket(
        comment("ready-1", READY.format(head=old_head), author="claude-bot"),
        head=current_head,
        ci={old_head: CIResult("success")},
    )

    dispatcher.process(stale)
    dispatcher.process(stale)

    assert queue.claim("codex", now=101) is None


def test_t2_red_increments_gate_count_and_third_in_a_row_is_stuck(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    for number in range(1, 4):
        head = str(number) * 7
        snapshot = ticket(
            comment(
                f"ready-{number}",
                READY.format(head=head),
                author="claude-bot",
            ),
            head=head,
            ci={head: CIResult("failure", f"log-{number}")},
        )
        dispatcher.process(snapshot)
        assert dispatcher.gate_count(snapshot) == number
        if number < 3:
            fix = queue.claim("claude", now=100 + number)
            assert fix is not None
            assert fix.request.action == "fix"
            assert fix.request.detail == f"log-{number}"
        else:
            assert dispatcher.is_stuck(snapshot)

    assert queue.claim("claude", now=104) is None
    stopped = queue.claim("grok", now=104)
    assert stopped is not None
    assert stopped.request.action == "stuck"


def test_duplicate_ready_for_one_head_does_not_increment_gate_twice(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 7
    snapshot = ticket(
        comment("ready-1", READY.format(head=head), author="claude-bot", created_at=1),
        comment("ready-2", READY.format(head=head), author="claude-bot", created_at=2),
        head=head,
        ci={head: CIResult("failure")},
    )
    dispatcher.process(snapshot)

    assert dispatcher.gate_count(snapshot) == 1
    assert queue.claim("claude", now=101) is not None
    assert queue.claim("claude", now=102) is None


def test_green_ci_resets_the_consecutive_red_count(tmp_path: Path) -> None:
    dispatcher, _queue = setup(tmp_path)
    statuses = ("failure", "failure", "success", "failure")
    expected = (1, 2, 0, 1)
    snapshot = ticket()
    for number, (status, count) in enumerate(zip(statuses, expected), start=1):
        head = f"{number:x}" * 7
        snapshot = ticket(
            comment(
                f"ready-{number}",
                READY.format(head=head),
                author="claude-bot",
            ),
            head=head,
            ci={head: CIResult(status)},
        )
        dispatcher.process(snapshot)
        assert dispatcher.gate_count(snapshot) == count
    assert not dispatcher.is_stuck(snapshot)


def test_t6_retries_no_handoff_once_then_marks_stuck(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    snapshot = ticket(comment("go-1", GO, author="grok-bot"))
    dispatcher.process(snapshot)
    first = queue.claim("claude", now=110)
    assert first is not None
    queue.finish(first.id, handoff_comment_id=None, now=120)

    now[0] = 121
    dispatcher.reconcile_jobs()
    retry = queue.claim("claude", now=130)
    assert retry is not None
    assert retry.request.attempt == 2
    queue.finish(retry.id, handoff_comment_id=None, now=140)

    now[0] = 141
    dispatcher.reconcile_jobs()
    assert dispatcher.is_stuck(snapshot)
    stopped = queue.claim("grok", now=142)
    assert stopped is not None
    assert stopped.request.action == "stuck"


def test_t6_timeout_uses_project_override_and_heartbeat_progress(
    tmp_path: Path,
) -> None:
    now = [0.0]
    dispatcher, queue = setup(tmp_path, now)
    dispatcher.process(
        ticket(
            comment("go-1", GO, author="grok-bot"),
            timeout=2,
        )
    )
    running = queue.claim("claude", now=0)
    assert running is not None
    assert running.request.timeout_seconds == 120

    now[0] = 119
    dispatcher.reconcile_jobs()
    queue.heartbeat(running.id, now=119)
    now[0] = 238
    dispatcher.reconcile_jobs()
    assert queue.claim("claude", now=238) is None

    now[0] = 239
    dispatcher.reconcile_jobs()
    retry = queue.claim("claude", now=240)
    assert retry is not None
    assert retry.request.attempt == 2


def test_t6_successful_handoff_is_not_retried(tmp_path: Path) -> None:
    now = [0.0]
    dispatcher, queue = setup(tmp_path, now)
    dispatcher.process(ticket(comment("go-1", GO, author="grok-bot")))
    running = queue.claim("claude", now=0)
    assert running is not None
    queue.finish(running.id, handoff_comment_id="ready-1", now=10)

    now[0] = 10000
    dispatcher.reconcile_jobs()
    assert queue.claim("claude", now=10001) is None


def test_poll_loop_polls_every_60_seconds(tmp_path: Path) -> None:
    dispatcher, _queue = setup(tmp_path)
    source = FakeSource()
    sleeps: list[float] = []

    PollLoop(dispatcher, source, sleep=sleeps.append).run(max_polls=3)

    assert source.calls == 3
    assert sleeps == [60, 60]


def test_dispatcher_cannot_execute_a_harness_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("process execution is forbidden")

    monkeypatch.setattr(os, "system", fail)
    monkeypatch.setattr(os, "popen", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(ticket(comment("go-1", GO, author="grok-bot")))
    assert queue.claim("claude", now=101) is not None

    for name in ("dispatcher.py", "queue.py"):
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
