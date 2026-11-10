import ast
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import (
    CIResult,
    Comment,
    Dispatcher,
    PollLoop,
    TicketRef,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig
from lib.queue import Job, JobRequest, SQLiteJobQueue

GO = (
    "<!-- orch:v1 kind=go from=grok-bot role=orchestrator "
    "state={state} -->\nGo.\n"
)
BRIEF = "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\nBrief.\n"
READY = (
    "<!-- orch:v1 kind=ready from=claude-bot role=builder head={head} -->\n"
    "Ready.\n"
)
TRUSTED_LOGINS = {
    "grok-login": "grok-bot",
    "claude-login": "claude-bot",
    "codex-login": "codex-bot",
}


class FakeSource:
    def __init__(
        self,
        *tickets: TrackedTicket,
        github: tuple[TrackedTicket, ...] = (),
        linear: tuple[TrackedTicket, ...] = (),
    ):
        self.tickets = {
            (ticket.repo.lower(), ticket.ticket_id): ticket for ticket in tickets
        }
        self.github = github
        self.linear = linear
        self.calls = 0
        self.github_label: str | None = None
        self.linear_query: str | None = None
        self.last_refs: tuple[TicketRef, ...] = ()

    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]:
        self.github_label = label_prefix
        return self.github

    def linear_candidates(self, query: str) -> tuple[TrackedTicket, ...]:
        self.linear_query = query
        return self.linear

    def tracked_tickets(
        self,
        refs: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]:
        self.calls += 1
        self.last_refs = refs
        return tuple(
            self.tickets[(ref.repo.lower(), ref.ticket_id)]
            for ref in refs
            if (ref.repo.lower(), ref.ticket_id) in self.tickets
        )


def factory(*, tickets: str = "github") -> FactoryConfig:
    return FactoryConfig(
        base_branch="main",
        tickets=tickets,
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
    timeout: int = 90,
    tickets: str = "github",
) -> TrackedTicket:
    return TrackedTicket(
        repo="owner/repo",
        ticket_id="42",
        factory=factory(tickets=tickets),
        comments=comments,
        current_head=head,
        ci=ci or {},
        job_timeout_minutes=timeout,
    )


def setup(
    tmp_path: Path,
    now: list[float] | None = None,
) -> tuple[Dispatcher, SQLiteJobQueue]:
    current = now if now is not None else [100.0]
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: current[0])
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
    )
    return dispatcher, queue


def seed_builder_job(queue: SQLiteJobQueue) -> None:
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="go-seed",
            timeout_seconds=5400,
        ),
        dedupe_key="seed-builder:g1",
    )
    running = queue.claim("claude")
    assert running is not None
    queue.finish(
        running.id,
        owner="claude",
        handoff_comment_id="ready-seed",
    )


def test_dispatcher_refuses_separate_state_and_queue_databases(
    tmp_path: Path,
) -> None:
    queue = SQLiteJobQueue(tmp_path / "queue.db", clock=lambda: 0)

    with pytest.raises(ValueError, match="one database"):
        Dispatcher(
            tmp_path / "state.db",
            queue,
            trusted_logins=TRUSTED_LOGINS,
        )


@pytest.mark.parametrize(
    ("state", "action"),
    [("building", "build"), ("planning", "plan")],
)
def test_t1_reads_kind_go_and_queues_the_builder(
    tmp_path: Path,
    state: str,
    action: str,
) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(
        ticket(
            comment("go-1", GO.format(state=state), author="grok-login"),
        )
    )

    job = queue.claim("claude")
    assert job is not None
    assert job.request.action == action
    assert job.request.recipient == "claude"
    assert job.request.source_comment_id == "go-1"
    assert job.request.timeout_seconds == 90 * 60


def test_t1_fails_closed_for_a_forged_go_comment(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(
        ticket(
            comment(
                "go-1",
                GO.format(state="building"),
                author="someone-else",
            )
        )
    )

    assert queue.claim("claude") is None


@pytest.mark.parametrize("state", [None, "review"])
def test_t1_wakes_grok_when_go_has_no_build_or_plan_state(
    tmp_path: Path,
    state: str | None,
) -> None:
    dispatcher, queue = setup(tmp_path)
    state_field = f" state={state}" if state is not None else ""
    body = (
        "<!-- orch:v1 kind=go from=grok-bot role=orchestrator"
        f"{state_field} -->\nGo.\n"
    )
    dispatcher.process(ticket(comment("go-1", body, author="grok-login")))

    assert queue.claim("claude") is None
    wake = queue.claim("grok")
    assert wake is not None
    assert wake.request.action == "clarify"


def test_poll_discovers_and_persists_github_ticket_from_grok_go(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    candidate = ticket(
        comment(
            "go-1",
            GO.format(state="building"),
            author="grok-login",
        )
    )
    source = FakeSource(candidate, github=(candidate,))

    dispatcher.poll_once(source)
    assert source.github_label == "orch:state/"
    assert source.linear_query is not None
    assert queue.claim("claude") is not None

    restarted_queue = SQLiteJobQueue(queue.path, clock=queue.clock)
    restarted = Dispatcher(
        queue.path,
        restarted_queue,
        trusted_logins=TRUSTED_LOGINS,
    )
    no_candidates = FakeSource(candidate)
    restarted.poll_once(no_candidates)
    assert no_candidates.calls == 1


def test_poll_discovers_linear_ticket_from_grok_brief_only(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    candidate = ticket(
        comment("brief-1", BRIEF, author="grok-login"),
        tickets="linear",
    )
    source = FakeSource(candidate, linear=(candidate,))

    dispatcher.poll_once(source)

    assert "kind=brief" in str(source.linear_query)
    assert source.calls == 1
    assert queue.outstanding() == ()


def test_poll_does_not_track_spoofed_candidate(tmp_path: Path) -> None:
    dispatcher, _queue = setup(tmp_path)
    candidate = ticket(
        comment("brief-1", BRIEF, author="untrusted-login"),
    )
    source = FakeSource(candidate, github=(candidate,))

    dispatcher.poll_once(source)

    assert source.calls == 1
    assert source.last_refs == ()


def test_restart_and_semantic_duplicate_do_not_queue_t1_twice(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    body = GO.format(state="building")
    first = comment("go-1", body, author="grok-login", created_at=1)
    duplicate = comment("go-2", body, author="grok-login", created_at=2)
    snapshot = ticket(first, duplicate)
    dispatcher.process(snapshot)

    restarted_queue = SQLiteJobQueue(
        tmp_path / "d.db",
        clock=lambda: 200,
    )
    restarted = Dispatcher(
        tmp_path / "d.db",
        restarted_queue,
        trusted_logins=TRUSTED_LOGINS,
    )
    restarted.process(snapshot)

    assert queue.claim("claude") is not None
    assert queue.claim("claude") is None


def test_t1_dedupes_plan_and_build_separately_in_one_generation(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(
        ticket(
            comment(
                "go-plan",
                GO.format(state="planning"),
                author="grok-login",
                created_at=1,
            ),
            comment(
                "go-build",
                GO.format(state="building"),
                author="grok-login",
                created_at=2,
            ),
        )
    )

    first = queue.claim("claude")
    assert first is not None
    second = queue.claim("claude")
    assert second is not None
    assert {first.request.action, second.request.action} == {"plan", "build"}


def test_t2_waits_for_ci_then_queues_cross_family_review(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    head = "a" * 7
    ready = comment("ready-1", READY.format(head=head), author="claude-login")
    pending = ticket(ready, head=head, ci={head: CIResult("pending")})
    dispatcher.process(pending)
    assert queue.claim("codex") is None

    dispatcher.process(replace(pending, ci={head: CIResult("success")}))
    review = queue.claim("codex")
    assert review is not None
    assert review.request.action == "review"
    assert review.request.head == head
    assert dispatcher.gate_count(pending) == 0


def test_t2_refuses_ready_without_a_builder_job(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 40
    dispatcher.process(
        ticket(
            comment(
                "ready-1",
                READY.format(head=head),
                author="claude-login",
            ),
            head=head,
            ci={head: CIResult("success")},
        )
    )

    assert queue.claim("codex") is None


@pytest.mark.parametrize(
    ("author", "sender"),
    [
        ("codex-login", "claude-bot"),
        ("claude-login", "codex-bot"),
    ],
)
def test_t2_refuses_wrong_sender_and_spoofed_from_field(
    tmp_path: Path,
    author: str,
    sender: str,
) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    head = "a" * 40
    body = (
        f"<!-- orch:v1 kind=ready from={sender} role=builder "
        f"head={head} -->\nReady.\n"
    )
    dispatcher.process(
        ticket(
            comment("ready-1", body, author=author),
            head=head,
            ci={head: CIResult("success")},
        )
    )

    assert queue.claim("codex") is None


@pytest.mark.parametrize("reported_length", [7, 40])
def test_t2_matches_short_or_full_ready_head_to_full_pr_head(
    tmp_path: Path,
    reported_length: int,
) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    full_head = "abcdef1" + "2" * 33
    reported = full_head[:reported_length]
    dispatcher.process(
        ticket(
            comment(
                "ready-1",
                READY.format(head=reported),
                author="claude-login",
            ),
            head=full_head,
            ci={full_head: CIResult("success")},
        )
    )

    review = queue.claim("codex")
    assert review is not None
    assert review.request.head == full_head


def test_t2_marks_a_stale_ready_handled_without_queueing(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    old_head = "a" * 7
    current_head = "b" * 7
    stale = ticket(
        comment("ready-1", READY.format(head=old_head), author="claude-login"),
        head=current_head,
        ci={old_head: CIResult("success")},
    )

    dispatcher.process(stale)
    dispatcher.process(stale)

    assert queue.claim("codex") is None


def test_t2_red_increments_gate_count_and_third_in_a_row_is_stuck(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    for number in range(1, 4):
        head = str(number) * 7
        snapshot = ticket(
            comment(
                f"ready-{number}",
                READY.format(head=head),
                author="claude-login",
            ),
            head=head,
            ci={head: CIResult("failure", f"log-{number}")},
        )
        dispatcher.process(snapshot)
        assert dispatcher.gate_count(snapshot) == number
        if number < 3:
            fix = queue.claim("claude")
            assert fix is not None
            assert fix.request.action == "fix"
            assert fix.request.detail == f"log-{number}"
        else:
            assert dispatcher.is_stuck(snapshot)

    assert queue.claim("claude") is None
    stopped = queue.claim("grok")
    assert stopped is not None
    assert stopped.request.action == "stuck"


def test_stuck_flag_and_grok_wake_roll_back_together_on_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    snapshot = ticket()
    for number in range(1, 3):
        head = str(number) * 7
        snapshot = ticket(
            comment(
                f"ready-{number}",
                READY.format(head=head),
                author="claude-login",
            ),
            head=head,
            ci={head: CIResult("failure")},
        )
        dispatcher.process(snapshot)

    head = "3" * 7
    third = ticket(
        comment("ready-3", READY.format(head=head), author="claude-login"),
        head=head,
        ci={head: CIResult("failure")},
    )
    original = queue.enqueue_in_transaction

    def crash(
        connection: sqlite3.Connection,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Job:
        if request.action == "stuck":
            raise RuntimeError("simulated crash")
        return original(connection, request, dedupe_key=dedupe_key)

    monkeypatch.setattr(queue, "enqueue_in_transaction", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        dispatcher.process(third)

    assert not dispatcher.is_stuck(third)
    assert queue.claim("grok") is None

    monkeypatch.setattr(queue, "enqueue_in_transaction", original)
    dispatcher.process(third)
    assert dispatcher.is_stuck(third)
    assert queue.claim("grok") is not None


def test_stuck_short_circuit_does_not_requeue_completed_wake(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    snapshot = ticket()
    for number in range(1, 4):
        head = str(number) * 7
        snapshot = ticket(
            comment(
                f"ready-{number}",
                READY.format(head=head),
                author="claude-login",
            ),
            head=head,
            ci={head: CIResult("failure")},
        )
        dispatcher.process(snapshot)

    first = queue.claim("grok")
    assert first is not None
    now[0] = 110
    queue.finish(first.id, owner="grok", handoff_comment_id="stuck-1")
    dispatcher.process(
        ticket(
            comment(
                "go-after-stuck",
                GO.format(state="building"),
                author="grok-login",
            )
        )
    )

    assert queue.claim("grok") is None
    with queue.connect() as connection:
        count = connection.execute(
            "SELECT count(*) FROM jobs WHERE action = 'stuck'"
        ).fetchone()[0]
    assert count == 1


def test_reconciles_do_not_requeue_completed_stuck_wake(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    for number in range(1, 4):
        head = str(number) * 7
        dispatcher.process(
            ticket(
                comment(
                    f"ready-{number}",
                    READY.format(head=head),
                    author="claude-login",
                ),
                head=head,
                ci={head: CIResult("failure")},
            )
        )

    wake = queue.claim("grok")
    assert wake is not None
    queue.finish(wake.id, owner="grok", handoff_comment_id="stuck-1")
    for _iteration in range(5):
        dispatcher.reconcile_jobs()

    assert queue.claim("grok") is None
    with queue.connect() as connection:
        count = connection.execute(
            "SELECT count(*) FROM jobs WHERE action = 'stuck'"
        ).fetchone()[0]
    assert count == 1


def test_failed_stuck_wake_is_requeued_once(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    for number in range(1, 4):
        head = str(number) * 7
        dispatcher.process(
            ticket(
                comment(
                    f"ready-{number}",
                    READY.format(head=head),
                    author="claude-login",
                ),
                head=head,
                ci={head: CIResult("failure")},
            )
        )

    first = queue.claim("grok")
    assert first is not None
    queue.finish(first.id, owner="grok", handoff_comment_id=None)
    dispatcher.reconcile_jobs()

    second = queue.claim("grok")
    assert second is not None
    assert second.id != first.id
    assert second.request.generation > first.request.generation


def test_duplicate_ready_for_one_head_does_not_increment_gate_twice(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    head = "a" * 7
    snapshot = ticket(
        comment("ready-1", READY.format(head=head), author="claude-login", created_at=1),
        comment("ready-2", READY.format(head=head), author="claude-login", created_at=2),
        head=head,
        ci={head: CIResult("failure")},
    )
    dispatcher.process(snapshot)

    assert dispatcher.gate_count(snapshot) == 1
    assert queue.claim("claude") is not None
    assert queue.claim("claude") is None


def test_green_ci_resets_the_consecutive_red_count(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    statuses = ("failure", "failure", "success", "failure")
    expected = (1, 2, 0, 1)
    snapshot = ticket()
    for number, (status, count) in enumerate(zip(statuses, expected), start=1):
        head = f"{number:x}" * 7
        snapshot = ticket(
            comment(
                f"ready-{number}",
                READY.format(head=head),
                author="claude-login",
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
    snapshot = ticket(
        comment("go-1", GO.format(state="building"), author="grok-login")
    )
    dispatcher.process(snapshot)
    now[0] = 110
    first = queue.claim("claude")
    assert first is not None
    now[0] = 120
    queue.finish(first.id, owner="claude", handoff_comment_id=None)

    now[0] = 121
    dispatcher.reconcile_jobs()
    now[0] = 130
    retry = queue.claim("claude")
    assert retry is not None
    assert retry.request.attempt == 2
    now[0] = 140
    queue.finish(retry.id, owner="claude", handoff_comment_id=None)

    now[0] = 141
    dispatcher.reconcile_jobs()
    assert dispatcher.is_stuck(snapshot)
    now[0] = 142
    stopped = queue.claim("grok")
    assert stopped is not None
    assert stopped.request.action == "stuck"


def test_t6_does_not_retry_job_for_already_stuck_ticket(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    snapshot = ticket(
        comment(
            "go-1",
            GO.format(state="building"),
            author="grok-login",
        )
    )
    dispatcher.process(snapshot)
    running = queue.claim("claude")
    assert running is not None
    now[0] = 110
    queue.finish(running.id, owner="claude", handoff_comment_id=None)
    with queue.connect() as connection:
        connection.execute(
            """
            UPDATE ticket_state
            SET stuck = 1, stuck_reason = 'already-stuck',
                stuck_source_comment_id = 'go-1', timeout_seconds = 5400
            WHERE repo = 'owner/repo' AND ticket_id = '42'
            """
        )

    dispatcher.reconcile_jobs()

    assert not any(
        job.request.action == "build" and job.request.attempt == 2
        for job in queue.outstanding()
    )
    assert queue.claim("grok") is not None


def test_t6_times_out_unclaimed_queued_job_from_created_at(
    tmp_path: Path,
) -> None:
    now = [0.0]
    dispatcher, queue = setup(tmp_path, now)
    dispatcher.process(
        ticket(
            comment(
                "go-1",
                GO.format(state="building"),
                author="grok-login",
            )
        )
    )

    now[0] = 5399
    dispatcher.reconcile_jobs()
    initial = queue.outstanding()
    assert len(initial) == 1
    failed_job_id = initial[0].id
    now[0] = 5400
    dispatcher.reconcile_jobs()

    outstanding = queue.outstanding()
    assert len(outstanding) == 1
    assert outstanding[0].request.attempt == 2
    assert outstanding[0].request.generation == 2
    assert failed_job_id in outstanding[0].dedupe_key
    assert ":g2" in outstanding[0].dedupe_key


def test_t6_retries_separate_failed_fix_jobs_without_dedupe_collision(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)

    head_one = "1" * 7
    dispatcher.process(
        ticket(
            comment(
                "ready-1",
                READY.format(head=head_one),
                author="claude-login",
            ),
            head=head_one,
            ci={head_one: CIResult("failure")},
        )
    )
    first_fix = queue.claim("claude")
    assert first_fix is not None
    queue.finish(first_fix.id, owner="claude", handoff_comment_id=None)
    dispatcher.reconcile_jobs()
    first_retry = queue.claim("claude")
    assert first_retry is not None
    queue.finish(
        first_retry.id,
        owner="claude",
        handoff_comment_id="ready-retry-1",
    )

    head_two = "2" * 7
    dispatcher.process(
        ticket(
            comment(
                "ready-2",
                READY.format(head=head_two),
                author="claude-login",
            ),
            head=head_two,
            ci={head_two: CIResult("failure")},
        )
    )
    second_fix = queue.claim("claude")
    assert second_fix is not None
    assert second_fix.request.generation == 1
    queue.finish(second_fix.id, owner="claude", handoff_comment_id=None)
    dispatcher.reconcile_jobs()

    second_retry = queue.claim("claude")
    assert second_retry is not None
    assert second_retry.id != first_retry.id
    assert second_fix.id in second_retry.dedupe_key
    assert first_fix.id in first_retry.dedupe_key


def test_t6_leaves_failure_unhandled_when_retry_was_not_inserted(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    queued = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="go-1",
            timeout_seconds=5400,
        ),
        dedupe_key="build:g1",
    )
    running = queue.claim("claude")
    assert running is not None
    queue.finish(running.id, owner="claude", handoff_comment_id=None)
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="go-1",
            timeout_seconds=5400,
            generation=2,
            attempt=2,
        ),
        dedupe_key=f"t6-retry:{queued.id}:g2",
    )

    dispatcher.reconcile_jobs()

    assert [job.id for job in queue.unhandled_failures()] == [queued.id]


def test_t6_timeout_uses_project_override_and_heartbeat_progress(
    tmp_path: Path,
) -> None:
    now = [0.0]
    dispatcher, queue = setup(tmp_path, now)
    dispatcher.process(
        ticket(
            comment(
                "go-1",
                GO.format(state="building"),
                author="grok-login",
            ),
            timeout=2,
        )
    )
    running = queue.claim("claude")
    assert running is not None
    assert running.request.timeout_seconds == 120

    now[0] = 119
    dispatcher.reconcile_jobs()
    queue.heartbeat(running.id, owner="claude")
    now[0] = 238
    dispatcher.reconcile_jobs()
    assert queue.claim("claude") is None

    now[0] = 239
    dispatcher.reconcile_jobs()
    now[0] = 240
    retry = queue.claim("claude")
    assert retry is not None
    assert retry.request.attempt == 2


def test_t6_successful_handoff_is_not_retried(tmp_path: Path) -> None:
    now = [0.0]
    dispatcher, queue = setup(tmp_path, now)
    dispatcher.process(
        ticket(
            comment(
                "go-1",
                GO.format(state="building"),
                author="grok-login",
            )
        )
    )
    running = queue.claim("claude")
    assert running is not None
    now[0] = 10
    queue.finish(running.id, owner="claude", handoff_comment_id="ready-1")

    now[0] = 10000
    dispatcher.reconcile_jobs()
    assert queue.claim("claude") is None


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
    dispatcher.process(
        ticket(
            comment(
                "go-1",
                GO.format(state="building"),
                author="grok-login",
            )
        )
    )
    assert queue.claim("claude") is not None

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
