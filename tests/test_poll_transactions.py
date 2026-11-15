"""#440 ac-3: no orch.db write transaction is held across a GitHub or Linear
call during a poll cycle.

Every fake network call here first opens its own connection to orch.db with
no busy wait and takes the write lock. That fails at once if the dispatcher
still holds a write transaction while it calls out.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from lib.dispatcher import CIResult, Dispatcher, TrackedTicket
from lib.wait import ExistingJobQueue
from tests.test_dispatcher import (
    GO,
    PLAN,
    QUESTIONS,
    READY,
    VERDICT,
    FakeReviewActions,
    comment,
    factory,
    handled_outcome,
    seed_review_job,
    setup,
    ticket,
    track,
)
from tests.test_github_only_project import (
    DOCUMENTED_READY,
    PR_HEAD,
    FactoryPrWorld,
    gh_comment,
)

BLOCK_SECONDS = 40
HEADS = [f"{n:x}" * 40 for n in range(1, 6)]


class Flow:
    """One ticket's comment thread, processed after every post."""

    def __init__(self, tmp_path: Path):
        self.now = [100.0]
        self.dispatcher, self.queue = setup(tmp_path, self.now)
        track(self.queue)
        self.comments: list = []
        self.head = HEADS[0]

    def snapshot(self) -> TrackedTicket:
        return ticket(
            *self.comments,
            head=self.head,
            ci={self.head: CIResult("success")},
            factory_config=factory(rounds_high=3),
        )

    def post(self, comment_id: str, body: str, author: str) -> str:
        self.now[0] += 10
        self.comments.append(
            comment(comment_id, body, author=author, created_at=self.now[0])
        )
        self.dispatcher.process(self.snapshot())
        return handled_outcome(self.queue, comment_id)

    def go(self, comment_id: str) -> str:
        return self.post(comment_id, GO.format(state="building"), "grok-login")

    def verdict(self, round_number: int, verdict: str) -> str:
        return self.post(
            f"{verdict.lower()}-{round_number}",
            VERDICT.format(
                round=f"{round_number}/3", head=self.head, verdict=verdict, risk="high"
            ),
            "codex-login",
        )

    def finish(self, recipient: str) -> None:
        job = self.queue.claim(recipient)
        assert job is not None
        self.queue.finish(job.id, owner=recipient, handoff_comment_id=f"handoff-{job.id}")

    def review(self, number: int) -> None:
        self.head = HEADS[number]
        ready = self.post(f"ready-{number}", READY.format(head=self.head), "claude-login")
        assert ready == "queued-review"
        self.finish("codex")

    def to_review(self) -> None:
        assert self.go("go-1") == "queued-build"
        self.finish("claude")
        self.review(1)

    def to_cap(self) -> None:
        self.to_review()
        for number in (1, 2, 3):
            if number > 1:
                self.finish("claude")
                self.review(number)
            self.verdict(number, "REVISE")
        assert handled_outcome(self.queue, "revise-3") == "capped-review"


def writable(path: Path) -> bool:
    connection = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc):
            raise
        return False
    finally:
        connection.close()


class ProbedActions(FakeReviewActions):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.probes: list[tuple[str, bool]] = []

    def set_state(self, ticket: TrackedTicket, state: str) -> None:
        self.probes.append((state, writable(self.path)))
        super().set_state(ticket, state)


def replay_probed(
    dispatcher: Dispatcher, comment_id: str, snapshot: TrackedTicket
) -> ProbedActions:
    """Process an already handled comment again, so it takes its replay
    branch, with every label write probing the database."""
    path = dispatcher.state_path
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM handled_comments WHERE comment_id = ?", (comment_id,)
        )
    probe = ProbedActions(path)
    dispatcher.review_actions = probe
    dispatcher.process(snapshot)
    return probe


def assert_label_written_unlocked(probe: ProbedActions, *states: str) -> None:
    assert [state for state, ok in probe.probes if not ok] == []
    assert set(states) <= {state for state, _ in probe.probes}


def test_t3_replay_writes_its_label_and_wake_outside_the_transaction(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()

    probe = replay_probed(flow.dispatcher, "revise-3", flow.snapshot())

    assert handled_outcome(flow.queue, "revise-3") == "capped-review"
    assert_label_written_unlocked(probe, "capped")


def test_cap_resume_replay_writes_its_label_outside_the_transaction(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()
    assert flow.go("go-2") == "queued-fix"

    probe = replay_probed(flow.dispatcher, "go-2", flow.snapshot())

    assert handled_outcome(flow.queue, "go-2") == "queued-fix"
    assert_label_written_unlocked(probe, "fixing")


def test_blocked_resume_replay_writes_its_label_outside_the_transaction(
    tmp_path: Path,
) -> None:
    flow = Flow(tmp_path)
    flow.to_review()
    assert flow.verdict(1, "BLOCKED") == "blocked-review"
    assert flow.go("go-2") == "queued-blocked-review"

    probe = replay_probed(flow.dispatcher, "go-2", flow.snapshot())

    assert handled_outcome(flow.queue, "go-2") == "queued-blocked-review"
    assert_label_written_unlocked(probe, "review")


def test_questions_replay_writes_its_label_outside_the_transaction(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    snapshot = ticket(
        comment("questions-1", QUESTIONS, author="claude-login"), head=head
    )
    dispatcher.process(snapshot)
    assert handled_outcome(queue, "questions-1") == "waiting-max"

    probe = replay_probed(dispatcher, "questions-1", snapshot)

    assert handled_outcome(queue, "questions-1") == "waiting-max"
    assert_label_written_unlocked(probe, "waiting-max")


def test_plan_replay_writes_its_labels_outside_the_transaction(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", GO.format(state="planning"), author="grok-login")
    plan = comment(
        "plan-1", PLAN.format(round=1, ac="met"), author="claude-login", created_at=2
    )
    dispatcher.process(ticket(go))
    dispatcher.process(ticket(go, plan))
    assert handled_outcome(queue, "plan-1") == "queued-plan-build"

    probe = replay_probed(dispatcher, "plan-1", ticket(go, plan))

    assert handled_outcome(queue, "plan-1") == "queued-plan-build"
    assert_label_written_unlocked(probe, "plan-check", "building")


class BlockingGh:
    """The world's fake gh. Every call probes orch.db, and the first read of
    the tracked PR blocks until released."""

    def __init__(self, world: FactoryPrWorld, pull: str):
        self.world = world
        self.gh = world.gh
        self.pull = pull
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.probes: list[bool] = []

    def __call__(self, argv, timeout):
        self.probes.append(writable(self.world.path))
        if any(part.endswith(self.pull) for part in argv) and not self.blocked.is_set():
            self.blocked.set()
            assert self.release.wait(BLOCK_SECONDS), "gh call never released"
        return self.gh(argv, timeout)


def test_waiter_writes_succeed_while_a_poll_gh_call_blocks(tmp_path: Path) -> None:
    world = FactoryPrWorld(tmp_path)
    world.gh.comments = [gh_comment(9001, DOCUMENTED_READY.format(head=PR_HEAD))]
    world.poll()
    assert [job[:2] for job in world.jobs()] == [("review", "codex")]

    gh = BlockingGh(world, "/pulls/51")
    world.source.gh_runner = gh
    writes: list[tuple[str, bool, bool]] = []
    waiter_errors: list[BaseException] = []

    def waiter() -> None:
        try:
            assert gh.blocked.wait(5), "the poll cycle never read the tracked PR"
            queue = ExistingJobQueue(world.path, clock=lambda: 200.0)
            claimed = queue.claim("codex")
            assert claimed is not None
            beat = queue.heartbeat(claimed.id, owner="codex")
            writes.append((beat.state, writable(world.path), gh.release.is_set()))
        except BaseException as exc:
            waiter_errors.append(exc)
        finally:
            gh.release.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    world.poll()
    thread.join(10)

    assert waiter_errors == []
    assert writes == [("running", True, False)]
    assert gh.probes and all(gh.probes)
