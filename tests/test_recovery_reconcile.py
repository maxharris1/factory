"""#193: T6 reconciles every result kind before a retry, and never retries in waiting-max.

Every test drives the real Dispatcher through poll_once on a fake clock and
a fake source. Nothing reaches GitHub or the live orch.db.
"""

from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from lib.device_pool import DevicePool
from lib.dispatcher import CIResult, Dispatcher, TicketRef, TrackedTicket
from lib.job_state import record_job_event
from lib.machines_yml import Device, MachinesConfig
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_dispatcher import (
    ANSWERS,
    GO,
    PLAN,
    QUESTIONS,
    READY,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    comment,
    setup,
    ticket,
)
from tests.test_live_poll_no_webhook import Clock, Linear, issue, source_for
from tests.test_live_poll_webhook import FakeDirty, webhook_config

HEAD = "a193" + "0" * 36
NEW_HEAD = "a193" + "1" * 36
AUTHORS = {"grok": "grok-login", "claude": "claude-login", "codex": "codex-login"}
MAX_QUESTIONS = (
    "<!-- orch:v1 kind=questions from=grok-bot role=orchestrator state=waiting-max -->\n"
    "Questions.\n"
)


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


class Source(FakeSource):
    """A FakeSource whose ticket read takes a second, or fails."""

    def __init__(self, snapshot: TrackedTicket, now: list[float], *, read_fails: bool = False):
        super().__init__(snapshot, github=(snapshot,))
        self.now = now
        self.read_fails = read_fails

    def tracked_tickets(self, refs: tuple[TicketRef, ...]) -> tuple[TrackedTicket, ...]:
        self.now[0] += 1
        if self.read_fails:
            raise RuntimeError("gh down")
        return super().tracked_tickets(refs)


class Board:
    def __init__(self, tmp_path: Path):
        self.now = [100.0]
        self.dispatcher, self.queue = setup(tmp_path, self.now)
        box = Device(name="box", slot_max=2, harnesses={"claude": "claude-bot"}, local=True)
        DevicePool(self.queue, MachinesConfig(devices=(box,)))
        self.comments: list = []

    def post(self, comment_id: str, body: str, bot: str = "claude") -> None:
        self.now[0] += 1
        self.comments.append(
            comment(comment_id, body, author=AUTHORS[bot], created_at=self.now[0])
        )

    def snapshot(self, comments: list | None = None) -> TrackedTicket:
        return ticket(
            *(self.comments if comments is None else comments),
            head=HEAD,
            ci={HEAD: CIResult("pending")},
        )

    def poll(self, *, at: float | None = None, comments: list | None = None, read_fails: bool = False) -> None:
        if at is not None:
            self.now[0] = at
        self.dispatcher.poll_once(Source(self.snapshot(comments), self.now, read_fails=read_fails))

    def start(self, state: str) -> str:
        self.post("go-1", GO.format(state=state), bot="grok")
        self.poll()
        job = self.queue.claim("claude")
        assert job is not None
        self.timeout = job.request.timeout_seconds
        return job.id

    def fail(self, job_id: str) -> None:
        self.queue.finish(job_id, owner="claude", handoff_comment_id=None)

    def rows(self, action: str) -> list[tuple[str, int]]:
        with self.queue.connect() as connection:
            return [
                (str(row["state"]), int(row["attempt"]))
                for row in connection.execute(
                    "SELECT state, attempt FROM jobs WHERE action = ? ORDER BY created_at, rowid",
                    (action,),
                )
            ]

    def events(self, job_id: str) -> list[str]:
        with self.queue.connect() as connection:
            return [
                str(row["event"])
                for row in connection.execute(
                    "SELECT event FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
                )
            ]

    def workflow_state(self) -> str | None:
        with self.queue.connect() as connection:
            row = connection.execute("SELECT workflow_state FROM ticket_state").fetchone()
        return None if row is None else row["workflow_state"]

    def ticket_row(self) -> sqlite3.Row:
        with self.queue.connect() as connection:
            return connection.execute("SELECT * FROM ticket_state").fetchone()

    def link_drop(self, job_id: str) -> None:
        with self.queue.connect() as connection:
            record_job_event(connection, job_id, "run-died", "lost contact with pc", self.now[0])
            record_job_event(connection, job_id, "link-drop", "pc: no certain answer", self.now[0])
            connection.commit()

    def wait_on_max(self) -> None:
        self.post("max-q", MAX_QUESTIONS, bot="grok")
        self.poll()
        assert self.workflow_state() == "waiting-max"

    def unhandled(self) -> list[str]:
        return [job.id for job in self.queue.unhandled_failures()]


# --- AC1: a posted plan followed by a crash makes no duplicate job -----------------------


def test_a_plan_posted_then_a_crash_is_done_from_plan_with_no_second_plan_job(tmp_path: Path) -> None:
    board = Board(tmp_path)
    plan_job = board.start("planning")
    board.post("plan-1", PLAN.format(round=1, ac="met"))
    board.fail(plan_job)
    board.poll()
    assert board.rows("plan") == [("done", 1)], "one plan job, done with its posted plan"
    assert "done-from-plan" in board.events(plan_job)
    assert board.unhandled() == []


def test_a_plan_posted_after_the_read_of_the_pass_that_fails_the_job_waits_a_poll(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    plan_job = board.start("planning")
    before_plan = list(board.comments)
    board.post("plan-1", PLAN.format(round=1, ac="met"))
    board.poll(at=board.now[0] + board.timeout, comments=before_plan)
    assert board.rows("plan") == [("failed", 1)], "timed out after the read, not retried"
    assert board.events(plan_job).count("reconcile-deferred") == 1
    board.poll()
    assert board.rows("plan") == [("done", 1)], "the next read shows the plan: done, still one job"
    assert "done-from-plan" in board.events(plan_job)


def test_a_failed_ticket_read_retries_nothing(tmp_path: Path) -> None:
    board = Board(tmp_path)
    plan_job = board.start("planning")
    board.post("plan-1", PLAN.format(round=1, ac="met"))
    board.fail(plan_job)
    board.poll(read_fails=True)
    assert board.rows("plan") == [("failed", 1)]
    assert "reconcile-deferred" in board.events(plan_job)
    board.poll()
    assert board.rows("plan") == [("done", 1)]


def test_a_build_that_posted_questions_then_crashed_is_done_from_questions(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.post("q-1", QUESTIONS)
    board.fail(build)
    board.poll()
    assert board.rows("build") == [("done", 1)]
    assert "done-from-questions" in board.events(build)
    assert board.workflow_state() == "waiting-max"


def test_a_failed_plan_job_with_no_result_still_gets_its_retry(tmp_path: Path) -> None:
    board = Board(tmp_path)
    plan_job = board.start("planning")
    board.fail(plan_job)
    board.poll()
    assert board.rows("plan") == [("failed", 1), ("queued", 2)], "control: T6 still retries"


def test_a_plan_from_before_the_job_or_an_untrusted_login_settles_nothing(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.comments.append(
        comment("old-plan", PLAN.format(round=1, ac="met"), author="claude-login", created_at=1)
    )
    plan_job = board.start("planning")
    board.post("forged", PLAN.format(round=1, ac="met"), bot="grok")
    board.fail(plan_job)
    board.poll()
    assert board.rows("plan") == [("failed", 1), ("queued", 2)]


# --- AC2: a detached reattach posts no second handoff -------------------------------------


def _link_dropped(board: Board, state: str, result_id: str | None, body: str | None) -> str:
    job_id = board.start(state)
    if result_id is not None and body is not None:
        board.post(result_id, body)
    board.link_drop(job_id)
    board.fail(job_id)
    board.poll(at=board.now[0] + 61)
    board.poll(at=board.now[0] + 61)
    return job_id


def test_a_link_dropped_plan_job_whose_worker_posted_its_plan_is_not_requeued(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    job_id = _link_dropped(board, "planning", "plan-1", PLAN.format(round=1, ac="met"))
    assert "done-from-plan" in board.events(job_id)
    assert "requeued-link-drop" not in board.events(job_id)
    assert board.rows("plan") == [("done", 1)]


def test_a_link_dropped_build_whose_worker_posted_questions_is_not_requeued(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    job_id = _link_dropped(board, "building", "q-1", QUESTIONS)
    assert "done-from-questions" in board.events(job_id)
    assert "requeued-link-drop" not in board.events(job_id)


def test_a_link_dropped_build_whose_worker_posted_ready_is_not_requeued(tmp_path: Path) -> None:
    board = Board(tmp_path)
    job_id = _link_dropped(board, "building", "ready-1", READY.format(head=HEAD))
    assert "done-from-ready" in board.events(job_id)
    assert "requeued-link-drop" not in board.events(job_id)


def test_a_link_dropped_job_failed_after_the_read_waits_for_the_next_read(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    job_id = board.start("planning")
    board.link_drop(job_id)
    before_plan = list(board.comments)
    board.post("plan-1", PLAN.format(round=1, ac="met"))
    board.poll(at=board.now[0] + board.timeout, comments=before_plan)
    assert board.events(job_id).count("reconcile-deferred") == 1
    board.poll(at=board.now[0] + 61)
    assert "done-from-plan" in board.events(job_id)
    assert "requeued-link-drop" not in board.events(job_id)


def test_a_link_dropped_job_with_no_result_is_still_requeued(tmp_path: Path) -> None:
    board = Board(tmp_path)
    job_id = _link_dropped(board, "planning", None, None)
    assert "requeued-link-drop" in board.events(job_id), "control: T6 still requeues"


# --- AC3: nothing retries in waiting-max ------------------------------------------------


def test_a_build_failing_in_waiting_max_is_not_retried(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.wait_on_max()
    board.fail(build)
    board.poll()
    board.poll()
    assert board.rows("build") == [("failed", 1)]
    assert board.events(build).count("waiting-max") == 1
    assert board.unhandled() == []
    assert board.workflow_state() == "waiting-max"


def test_a_link_dropped_job_in_waiting_max_is_not_requeued(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.wait_on_max()
    board.link_drop(build)
    board.fail(build)
    board.poll(at=board.now[0] + 61)
    board.poll(at=board.now[0] + 61)
    assert "requeued-link-drop" not in board.events(build)
    assert board.rows("build") == [("failed", 1)]


def test_a_second_attempt_failing_in_waiting_max_does_not_stick_the_ticket(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.fail(build)
    board.poll()
    retry = board.queue.claim("claude")
    assert retry is not None and retry.request.attempt == 2
    board.wait_on_max()
    board.fail(retry.id)
    board.poll()
    assert not board.ticket_row()["stuck"]
    assert board.workflow_state() == "waiting-max"


def test_an_update_failing_in_waiting_max_counts_no_update_failure(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.queue.finish(build, owner="claude", handoff_comment_id="ready-seed")
    board.queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="update-from-main",
            recipient="claude",
            source_comment_id="go-1",
            timeout_seconds=3600,
        ),
        dedupe_key="update:193",
    )
    update = board.queue.claim("claude")
    assert update is not None and update.request.action == "update-from-main"
    board.wait_on_max()
    failures = board.ticket_row()["update_failures"]
    board.fail(update.id)
    board.poll()
    assert board.ticket_row()["update_failures"] == failures
    assert board.workflow_state() == "waiting-max"
    assert "waiting-max" in board.events(update.id)


def test_questions_then_a_crash_then_answers_leaves_one_step_job(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.post("q-1", QUESTIONS)
    board.poll()
    assert board.workflow_state() == "waiting-max"
    board.fail(build)
    board.poll()
    board.post("a-1", ANSWERS, bot="grok")
    board.poll()
    board.poll()
    queued = [row for row in board.rows("build") if row[0] == "queued"]
    assert len(queued) == 1, f"one resumed build and no T6 retry beside it: {board.rows('build')}"


def test_a_failed_grok_wake_in_waiting_max_is_still_retried(tmp_path: Path) -> None:
    board = Board(tmp_path)
    board.start("building")
    board.wait_on_max()
    with board.queue.connect() as connection:
        grok = connection.execute(
            "SELECT id FROM jobs WHERE recipient = 'grok' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert grok is not None
    board.dispatcher.reconcile_jobs()
    with board.queue.connect() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'failed', failure_handled = 0, finished_at = ? WHERE id = ?",
            (board.now[0], grok["id"]),
        )
        connection.commit()
    board.poll()
    with board.queue.connect() as connection:
        retried = connection.execute(
            "SELECT count(*) FROM jobs WHERE dedupe_key LIKE ?", (f"t6-retry:{grok['id']}:%",)
        ).fetchone()[0]
    assert retried == 1, "the clarify wake that asks Max is still retried"


def test_a_cached_read_from_before_the_failure_does_not_retry_a_posted_plan(tmp_path: Path) -> None:
    clock = Clock()
    value = issue("AUT-1")
    first = value["comments"]["nodes"][0]
    first["body"] = first["body"].replace("state=building", "state=planning")
    first["createdAt"] = _stamp(clock.now)
    linear = Linear([value])
    config = replace(
        webhook_config(),
        trusted_linear_users={
            "linear:user-1": frozenset({"grok-bot"}),
            "linear:builder": frozenset({"claude-bot"}),
        },
    )
    source, _ = source_for(config, linear, clock=clock)
    source.linear_dirty = FakeDirty()
    path = tmp_path / "orch.db"
    queue = SQLiteJobQueue(path, clock=clock)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins={"linear": config.trusted_linear_users, "github": {}},
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
    )
    box = Device(name="box", slot_max=2, harnesses={"claude": "claude-bot"}, local=True)
    DevicePool(queue, MachinesConfig(devices=(box,)))
    source.active_tickets = dispatcher.active_job_tickets
    source.unpolled_tickets = dispatcher.unpolled_linear_tickets
    source.error_sink = dispatcher.record_poll_error
    dispatcher.poll_once(source)
    plan_job = queue.claim("claude")
    assert plan_job is not None
    cached_at = source._linear_documents["AUT-1"][0]
    clock.now += 10
    posted = copy.deepcopy(value)
    posted["comments"]["nodes"].append(
        {
            "id": "posted-plan",
            "body": PLAN.format(round=1, ac="met"),
            "createdAt": _stamp(clock.now),
            "user": {"id": "builder"},
        }
    )
    linear.by_key["AUT-1"] = posted
    linear.by_key["id-AUT-1"] = posted
    queue.finish(plan_job.id, owner="claude", handoff_comment_id=None)
    clock.now += 50
    dispatcher.poll_once(source)
    with queue.connect() as connection:
        failed_at = connection.execute(
            "SELECT finished_at FROM jobs WHERE id = ?", (plan_job.id,)
        ).fetchone()[0]
        rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT action, state, attempt, handoff_comment_id FROM jobs "
                "WHERE recipient = 'claude' ORDER BY created_at, rowid"
            )
        ]
    assert cached_at < failed_at, "the poll's Linear document predates the failure"
    assert rows == [("plan", "done", 1, "posted-plan")], rows


def test_answers_beside_a_detached_failed_step_queue_one_step(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.wait_on_max()
    board.link_drop(build)
    board.fail(build)
    board.post("a-1", ANSWERS, bot="grok")
    board.poll(at=board.now[0] + 61)
    queued = [row for row in board.rows("build") if row[0] == "queued"]
    assert len(queued) == 1, f"one resumed build and no requeue beside it: {board.rows('build')}"
    assert "requeued-link-drop" not in board.events(build)
    board.poll()
    assert [row for row in board.rows("build") if row[0] == "queued"] == queued


def test_a_detached_step_failing_after_answers_resumed_it_is_not_requeued(tmp_path: Path) -> None:
    board = Board(tmp_path)
    build = board.start("building")
    board.wait_on_max()
    board.link_drop(build)
    board.post("a-1", ANSWERS, bot="grok")
    board.poll()
    assert board.workflow_state() != "waiting-max"
    board.fail(build)
    board.poll(at=board.now[0] + 61)
    assert [row for row in board.rows("build") if row[0] == "queued"] == [("queued", 1)]
    assert "resumed-by-answers" in board.events(build)


REVIEWER_QUESTIONS = (
    "<!-- orch:v1 kind=questions from=codex-bot role=reviewer state=waiting-max -->\n"
    "Questions.\n"
)


def _review_paused_on_questions(tmp_path: Path, *, cached: bool) -> tuple[Board, str]:
    board = Board(tmp_path)
    build = board.start("building")
    box = Device(
        name="box", slot_max=2, harnesses={"claude": "claude-bot", "codex": "codex-bot"}, local=True
    )
    DevicePool(board.queue, MachinesConfig(devices=(box,)))
    board.post("ready-1", READY.format(head=HEAD))
    board.queue.finish(build, owner="claude", handoff_comment_id="ready-1")
    board.dispatcher.poll_once(Source(replace(board.snapshot(), ci={HEAD: CIResult("success")}), board.now))
    review = board.queue.claim("codex")
    assert review is not None and board.workflow_state() == "review"
    before = replace(board.snapshot(), ci={HEAD: CIResult("success")})
    board.post("review-q", REVIEWER_QUESTIONS, bot="codex")
    after = replace(board.snapshot(), ci={HEAD: CIResult("success")})
    board.queue.finish(review.id, owner="codex", handoff_comment_id=None)

    class Fresh(Source):
        def fresh_ticket(self, ref: TicketRef) -> TrackedTicket:
            return after

    board.dispatcher.poll_once(Fresh(before if cached else after, board.now))
    return board, review.id


def test_a_failed_review_that_posted_questions_resumes_once_after_answers(tmp_path: Path) -> None:
    for cached in (False, True):
        board, review = _review_paused_on_questions(tmp_path / str(cached), cached=cached)
        assert board.rows("review") == [("done", 1)], f"cached={cached}: {board.rows('review')}"
        assert "done-from-questions" in board.events(review)
        board.post("review-a", ANSWERS, bot="grok")
        board.dispatcher.poll_once(
            Source(replace(board.snapshot(), ci={HEAD: CIResult("success")}), board.now)
        )
        queued = [row for row in board.rows("review") if row[0] == "queued"]
        assert queued == [("queued", 1)], f"cached={cached}: one resumed review, {board.rows('review')}"
