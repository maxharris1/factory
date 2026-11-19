from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.linear_states import linear_move
from lib.dispatch.types import LinearState, LinearStateRead, TicketRef
from lib.queue import JobRequest
from tests.test_dispatcher import VERDICT, CIResult, FakeSource, comment, setup, ticket

REPO = "owner/repo"
RFR = LinearState("Ready for Review", "started", True)
IN_REVIEW = LinearState("In Review", "started", True)
IN_PROGRESS = LinearState("In Progress", "started", False)
TODO = LinearState("Todo", "unstarted", False)
DONE = LinearState("Done", "completed", False)
CANCELED = LinearState("Canceled", "canceled", False)


class LinearSource(FakeSource):
    def __init__(self, *tickets, merged: bool = False):
        super().__init__(*tickets)
        self.states: dict[str, LinearState] = {}
        self.errors: dict[str, str] = {}
        self.state_reads: list[tuple[str, ...]] = []
        self.merged = merged
        self.fresh_reads: list[str] = []

    def linear_states(self, refs: tuple[TicketRef, ...]) -> LinearStateRead:
        self.state_reads.append(tuple(ref.ticket_id for ref in refs))
        return LinearStateRead(
            {key: value for key, value in self.states.items() if key not in self.errors},
            dict(self.errors),
        )

    def fresh_ticket(self, ref: TicketRef):
        self.fresh_reads.append(ref.ticket_id)
        return replace(ticket(tickets="linear", ticket_id=ref.ticket_id), merged=self.merged)


def seed(queue, ticket_id: str, state: str, *, stuck: int = 0, **columns) -> None:
    values = {
        "gate_count": 0,
        "stuck": stuck,
        "workflow_state": state,
        "timeout_seconds": 3600,
        **columns,
    }
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO tracked_tickets VALUES (?, ?, 'linear', 0)", (REPO, ticket_id)
        )
        connection.execute(
            f"INSERT INTO ticket_state (repo, ticket_id, {', '.join(values)}) "
            f"VALUES (?, ?, {', '.join('?' for _ in values)})",
            (REPO, ticket_id, *values.values()),
        )


def enqueue(queue, ticket_id: str, action: str, recipient: str, key: str):
    return queue.enqueue(
        JobRequest(
            repo=REPO,
            ticket_id=ticket_id,
            action=action,
            recipient=recipient,
            source_comment_id=f"c-{key}",
            timeout_seconds=3600,
        ),
        dedupe_key=key,
    )


def state_of(queue, ticket_id: str) -> sqlite3.Row:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT * FROM ticket_state WHERE repo = ? AND ticket_id = ?", (REPO, ticket_id)
        ).fetchone()


def linear_row(queue, ticket_id: str) -> sqlite3.Row | None:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT * FROM linear_states WHERE repo = ? AND ticket_id = ?", (REPO, ticket_id)
        ).fetchone()


def transitions(queue, ticket_id: str) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        return [
            (row["from_state"], row["to_state"], row["source"])
            for row in connection.execute(
                "SELECT * FROM ticket_transitions WHERE repo = ? AND ticket_id = ? ORDER BY id",
                (REPO, ticket_id),
            )
        ]


def job_states(queue, ticket_id: str) -> dict[str, str]:
    with queue.connect() as connection:
        return {
            row["dedupe_key"]: row["state"]
            for row in connection.execute(
                "SELECT dedupe_key, state FROM jobs WHERE ticket_id = ?", (ticket_id,)
            )
        }


def cancel_reasons(queue, ticket_id: str) -> dict[str, str]:
    with queue.connect() as connection:
        return {
            row["dedupe_key"]: row["detail"]
            for row in connection.execute(
                """
                SELECT jobs.dedupe_key, event.detail FROM jobs
                JOIN job_events AS event ON event.job_id = jobs.id
                WHERE jobs.ticket_id = ? AND event.event = 'cancelled'
                """,
                (ticket_id,),
            )
        }


def attention(queue, ticket_id: str) -> list[str]:
    with queue.connect() as connection:
        return [
            row["reason"]
            for row in connection.execute(
                "SELECT reason FROM needs_attention WHERE ticket_id = ?", (ticket_id,)
            )
        ]


def capped_and_stuck_review(tmp_path: Path):
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    seed(queue, "AUT-2282", "capped", review_round=6, cap_extra=1)
    seed(
        queue,
        "AUT-2283",
        "review",
        stuck=1,
        review_round=2,
        stuck_reason="review refused twice",
        stuck_since=900.0,
    )
    enqueue(queue, "AUT-2282", "capped", "grok", "wake-2282")
    enqueue(queue, "AUT-2283", "stuck", "grok", "wake-2283")
    dispatcher.flag_attention(REPO, "AUT-2282", "capped", "round 6")
    dispatcher.flag_attention(REPO, "AUT-2283", "stuck", "review refused twice")
    source = LinearSource(
        ticket(tickets="linear", ticket_id="AUT-2282"),
        ticket(tickets="linear", ticket_id="AUT-2283"),
    )
    source.states = {"AUT-2282": IN_PROGRESS, "AUT-2283": IN_PROGRESS}
    return dispatcher, queue, source, now


@pytest.mark.parametrize(
    ("state", "stuck", "paused", "linear", "processed", "move"),
    [
        (None, False, None, RFR, True, None),
        ("intake", False, None, RFR, True, None),
        ("questions", False, None, CANCELED, True, None),
        ("capped", False, None, RFR, True, "hand-off"),
        ("review", True, None, IN_REVIEW, True, "hand-off"),
        ("waiting-max", False, "build", RFR, True, "hand-off"),
        ("waiting-max", False, None, RFR, True, None),
        ("waiting-max", False, "clarify", RFR, True, None),
        ("waiting-max", False, "clarify", CANCELED, True, None),
        ("capped", False, None, LinearState("QA Review", "started", True), True, "hand-off"),
        ("capped", False, None, LinearState("Ready for Review", "started", False), True, None),
        ("building", False, None, RFR, False, None),
        ("building", False, None, RFR, True, "hand-off"),
        ("building", True, None, RFR, False, "hand-off"),
        ("handed-off", False, None, RFR, True, None),
        ("ship", False, None, RFR, True, None),
        ("merge-queue", False, None, RFR, True, None),
        ("waiting-external", False, None, IN_REVIEW, True, None),
        ("capped", False, None, IN_PROGRESS, True, None),
        ("capped", False, None, DONE, True, "done"),
        ("handed-off", False, None, DONE, True, "done"),
        ("review", True, None, CANCELED, True, "canceled"),
        ("handed-off-externally", False, None, IN_PROGRESS, True, "resume"),
        ("handed-off-externally", False, None, RFR, True, None),
        ("handed-off-externally", False, None, TODO, True, None),
        ("handed-off-externally", False, None, DONE, True, "done"),
        ("handed-off-externally", False, None, CANCELED, True, "canceled"),
        ("merged", False, None, IN_PROGRESS, True, None),
        ("abandoned", False, None, IN_PROGRESS, True, None),
    ],
)
def test_linear_move(state, stuck, paused, linear, processed, move) -> None:
    assert linear_move(state, stuck, paused, linear, processed) == move


def test_each_poll_stores_every_open_linear_tickets_state(tmp_path: Path) -> None:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    seed(queue, "AUT-1", "capped")
    seed(queue, "AUT-2", "review", stuck=1)
    seed(queue, "AUT-3", "merged")
    seed(queue, "AUT-4", "abandoned")
    source = LinearSource()
    source.states = {"AUT-1": IN_PROGRESS, "AUT-2": TODO}

    dispatcher.poll_once(source)
    now[0] += 60
    source.states["AUT-1"] = LinearState("Blocked", "started", False)
    dispatcher.poll_once(source)

    assert source.state_reads == [("AUT-1", "AUT-2"), ("AUT-1", "AUT-2")]
    first, second = linear_row(queue, "AUT-1"), linear_row(queue, "AUT-2")
    assert (first["state_name"], first["state_type"], first["read_at"]) == (
        "Blocked",
        "started",
        1060.0,
    )
    assert (second["state_name"], second["state_type"], second["read_at"]) == (
        "Todo",
        "unstarted",
        1060.0,
    )
    assert linear_row(queue, "AUT-3") is None
    assert linear_row(queue, "AUT-4") is None


def test_failed_read_keeps_last_state_and_logs_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    seed(queue, "AUT-1", "capped")
    source = LinearSource()
    source.states = {"AUT-1": IN_PROGRESS}
    dispatcher.poll_once(source)

    def poll_errors() -> list[str]:
        with queue.connect() as connection:
            return [
                row["error"]
                for row in connection.execute(
                    "SELECT error FROM poll_errors WHERE error LIKE 'linear-state:%'"
                )
            ]

    source.errors = {"AUT-1": "Linear returned HTTP 500"}
    for _ in range(2):
        now[0] += 60
        dispatcher.poll_once(source)
    row = linear_row(queue, "AUT-1")
    assert (row["state_name"], row["state_type"], row["read_at"]) == (
        "In Progress",
        "started",
        1000.0,
    )
    assert row["read_error"] == "Linear returned HTTP 500"
    assert poll_errors() == ["linear-state:Linear returned HTTP 500"]
    assert capsys.readouterr().err.count("Linear state read failed") == 1

    source.errors = {}
    now[0] += 60
    dispatcher.poll_once(source)
    assert linear_row(queue, "AUT-1")["read_error"] is None
    assert linear_row(queue, "AUT-1")["read_at"] == 1180.0

    source.errors = {"AUT-1": "Linear returned HTTP 500"}
    now[0] += 60
    dispatcher.poll_once(source)
    assert len(poll_errors()) == 2


def test_aut2282_capped_round6_moved_to_rfr_is_handed_off_externally(
    tmp_path: Path,
) -> None:
    dispatcher, queue, source, now = capped_and_stuck_review(tmp_path)
    source.states["AUT-2282"] = RFR
    dispatcher.poll_once(source)

    row = state_of(queue, "AUT-2282")
    assert row["workflow_state"] == "handed-off-externally"
    assert row["external_from_state"] == "capped"
    assert row["external_from_stuck"] == 0
    assert row["external_at"] == 1000.0
    assert (row["review_round"], row["cap_extra"]) == (6, 1)
    assert transitions(queue, "AUT-2282") == [
        ("capped", "handed-off-externally", "linear-handoff")
    ]
    assert attention(queue, "AUT-2282") == []
    assert job_states(queue, "AUT-2282") == {"wake-2282": "cancelled"}
    assert cancel_reasons(queue, "AUT-2282") == {"wake-2282": "handed-off-externally"}

    reads_before = source.calls
    refs_after = []
    for _ in range(2):
        now[0] += 600
        dispatcher.poll_once(source)
        refs_after.append(tuple(ref.ticket_id for ref in source.last_refs))
    assert all("AUT-2282" not in refs for refs in refs_after)
    assert source.calls == reads_before + 2
    assert job_states(queue, "AUT-2282") == {"wake-2282": "cancelled"}
    assert state_of(queue, "AUT-2282")["workflow_state"] == "handed-off-externally"


def test_aut2283_stuck_review_moved_to_rfr_is_handed_off_externally(
    tmp_path: Path,
) -> None:
    dispatcher, queue, source, now = capped_and_stuck_review(tmp_path)
    source.states["AUT-2283"] = RFR
    dispatcher.poll_once(source)

    row = state_of(queue, "AUT-2283")
    assert row["workflow_state"] == "handed-off-externally"
    assert (row["stuck"], row["external_from_stuck"]) == (0, 1)
    assert row["external_from_state"] == "review"
    assert row["stuck_reason"] == "review refused twice"
    assert row["review_round"] == 2
    assert attention(queue, "AUT-2283") == []
    assert cancel_reasons(queue, "AUT-2283") == {"wake-2283": "handed-off-externally"}


def test_factory_handoff_is_not_external(tmp_path: Path) -> None:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    seed(queue, "AUT-1", "handed-off")
    seed(queue, "AUT-2", "ship")
    source = LinearSource()
    source.states = {"AUT-1": RFR, "AUT-2": RFR}
    dispatcher.poll_once(source)
    assert state_of(queue, "AUT-1")["workflow_state"] == "handed-off"
    assert state_of(queue, "AUT-2")["workflow_state"] == "ship"
    assert transitions(queue, "AUT-1") == []


def test_handed_off_ticket_jobs_are_not_claimed(tmp_path: Path) -> None:
    dispatcher, queue, source, now = capped_and_stuck_review(tmp_path)
    source.states["AUT-2282"] = RFR
    dispatcher.poll_once(source)
    with queue.connect() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'queued' WHERE ticket_id = 'AUT-2282'"
        )
    enqueue(queue, "AUT-2282", "build", "claude", "build-2282")
    enqueue(queue, "AUT-2283", "build", "claude", "build-2283")
    assert queue.claim("claude").request.ticket_id == "AUT-2283"
    assert queue.claim("claude") is None
    assert queue.claim("grok") is None


@pytest.mark.parametrize(
    ("ticket_id", "shape"),
    [
        ("AUT-2282", {"workflow_state": "capped", "review_round": 6, "stuck": 0}),
        (
            "AUT-2283",
            {
                "workflow_state": "review",
                "review_round": 2,
                "stuck": 1,
                "stuck_reason": "review refused twice",
            },
        ),
    ],
)
def test_resume_restores_the_recorded_state_and_round(
    tmp_path: Path, ticket_id: str, shape: dict
) -> None:
    dispatcher, queue, source, now = capped_and_stuck_review(tmp_path)
    source.states[ticket_id] = RFR
    dispatcher.poll_once(source)
    with queue.connect() as connection:
        jobs_before = connection.execute(
            "SELECT count(*) FROM jobs WHERE recipient != 'grok'"
        ).fetchone()[0]
        tracked_before = connection.execute(
            "SELECT * FROM tracked_tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()

    for linear in (TODO, IN_PROGRESS):
        now[0] += 60
        source.states[ticket_id] = linear
        dispatcher.poll_once(source)

    row = state_of(queue, ticket_id)
    assert {name: row[name] for name in shape} == shape
    assert (row["external_from_state"], row["external_at"]) == (None, None)
    assert transitions(queue, ticket_id)[-2:] == [
        (shape["workflow_state"], "handed-off-externally", "linear-handoff"),
        ("handed-off-externally", shape["workflow_state"], "linear-resume"),
    ]
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM jobs WHERE recipient != 'grok'"
        ).fetchone()[0] == jobs_before
        assert [tuple(r) for r in connection.execute(
            "SELECT * FROM tracked_tickets WHERE ticket_id = ?", (ticket_id,)
        )] == [tuple(r) for r in tracked_before]
    assert ticket_id in {ref.ticket_id for ref in dispatcher._tracked_refs()}


def test_resume_requeues_the_build_the_handoff_cancelled(tmp_path: Path) -> None:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    seed(queue, "AUT-9", "fixing", review_round=2)
    job = enqueue(queue, "AUT-9", "fix", "claude", "fix-9")
    source = LinearSource(ticket(tickets="linear", ticket_id="AUT-9"))
    source.states = {"AUT-9": IN_REVIEW}
    dispatcher.poll_once(source)
    assert job_states(queue, "AUT-9") == {"fix-9": "cancelled"}
    assert queue.claim("claude") is None

    now[0] += 60
    source.states["AUT-9"] = IN_PROGRESS
    dispatcher.poll_once(source)
    now[0] += 60
    dispatcher.poll_once(source)

    assert state_of(queue, "AUT-9")["workflow_state"] == "fixing"
    assert job_states(queue, "AUT-9") == {"fix-9": "queued"}
    claimed = queue.claim("claude")
    assert claimed is not None and claimed.id == job.id


def test_t6_waits_while_handed_off_externally(tmp_path: Path) -> None:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    seed(queue, "AUT-9", "fixing", review_round=2)
    job = enqueue(queue, "AUT-9", "fix", "claude", "fix-9")
    claimed = queue.claim("claude")
    assert claimed is not None and claimed.id == job.id
    source = LinearSource(ticket(tickets="linear", ticket_id="AUT-9"))
    source.states = {"AUT-9": IN_REVIEW}
    dispatcher.poll_once(source)
    assert state_of(queue, "AUT-9")["workflow_state"] == "handed-off-externally"

    def failure() -> tuple[int, int]:
        with queue.connect() as connection:
            return (
                connection.execute(
                    "SELECT failure_handled FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()[0],
                connection.execute(
                    "SELECT count(*) FROM jobs WHERE ticket_id = 'AUT-9'"
                ).fetchone()[0],
            )

    with queue.connect() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'failed', finished_at = ? WHERE id = ?", (now[0], job.id)
        )
    for _ in range(2):
        now[0] += 60
        dispatcher.poll_once(source)
    assert failure() == (0, 1)
    assert state_of(queue, "AUT-9")["stuck"] == 0

    now[0] += 60
    source.states["AUT-9"] = IN_PROGRESS
    dispatcher.poll_once(source)
    assert state_of(queue, "AUT-9")["workflow_state"] == "fixing"
    assert failure()[0] == 1


@pytest.mark.parametrize("start", ["capped", "handed-off-externally"])
@pytest.mark.parametrize(
    ("merged", "settled"), [(True, "merged"), (False, "done")]
)
def test_done_in_linear_settles_the_ticket(
    tmp_path: Path, start: str, merged: bool, settled: str
) -> None:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    extra = {"external_from_state": "capped", "external_at": 900.0} if start != "capped" else {}
    seed(queue, "AUT-5", start, review_round=6, **extra)
    enqueue(queue, "AUT-5", "build", "claude", "build-5")
    dispatcher.flag_attention(REPO, "AUT-5", "capped", "round 6")
    source = LinearSource(merged=merged)
    source.states = {"AUT-5": DONE}
    dispatcher.poll_once(source)

    assert source.fresh_reads == ["AUT-5"]
    row = state_of(queue, "AUT-5")
    assert row["workflow_state"] == settled
    assert transitions(queue, "AUT-5")[-1][:2] == (start, settled)
    assert attention(queue, "AUT-5") == []
    assert job_states(queue, "AUT-5") == {"build-5": "cancelled"}


@pytest.mark.parametrize("start", ["capped", "handed-off-externally"])
def test_canceled_stuck_ticket_is_abandoned_and_unstuck(tmp_path: Path, start: str) -> None:
    now = [1000.0]
    dispatcher, queue = setup(tmp_path, now)
    seed(queue, "AUT-6", start, stuck=int(start == "capped"), stuck_reason="refused")
    enqueue(queue, "AUT-6", "stuck", "grok", "wake-6")
    source = LinearSource()
    source.states = {"AUT-6": CANCELED}
    dispatcher.poll_once(source)

    row = state_of(queue, "AUT-6")
    assert (row["workflow_state"], row["stuck"], row["stuck_reason"]) == ("abandoned", 0, None)
    assert transitions(queue, "AUT-6")[-1] == (start, "abandoned", "linear-canceled")
    assert cancel_reasons(queue, "AUT-6") == {"wake-6": "linear-canceled"}
    now[0] += 60
    dispatcher.poll_once(source)
    assert len(source.state_reads) == 1


def reviewed(tmp_path: Path, verdict: str, linear: LinearState):
    dispatcher, queue = setup(tmp_path, [1000.0])
    head = "a" * 40
    seed(queue, "AUT-9", "review", review_round=0)
    build = enqueue(queue, "AUT-9", "build", "claude", "build-9")
    review = enqueue(queue, "AUT-9", "review", "codex", "review-9")
    with queue.connect() as connection:
        connection.execute(
            "UPDATE jobs SET state = 'done', finished_at = 999 WHERE id = ?", (build.id,)
        )
        connection.execute(
            """
            UPDATE jobs SET state = 'done', finished_at = 999, head = ?,
                handoff_comment_id = 'verdict-9'
            WHERE id = ?
            """,
            (head, review.id),
        )
    body = VERDICT.format(round=1, head=head, verdict=verdict, risk="high")
    source = LinearSource(
        ticket(
            comment("verdict-9", body, author="codex-login"),
            tickets="linear",
            ticket_id="AUT-9",
            head=head,
            ci={head: CIResult("success")},
        )
    )
    source.states = {"AUT-9": linear}
    dispatcher.poll_once(source)
    return dispatcher, queue


def test_canceled_in_linear_beats_a_pending_approve(tmp_path: Path) -> None:
    dispatcher, queue = reviewed(tmp_path, "APPROVE", CANCELED)
    assert state_of(queue, "AUT-9")["workflow_state"] == "abandoned"
    assert dispatcher.review_actions.merges == []


def test_rfr_in_linear_parks_before_a_pending_revise_starts_a_round(tmp_path: Path) -> None:
    dispatcher, queue = reviewed(tmp_path, "REVISE", RFR)
    row = state_of(queue, "AUT-9")
    assert (row["workflow_state"], row["review_round"], row["external_from_state"]) == (
        "handed-off-externally",
        0,
        "review",
    )
    assert job_states(queue, "AUT-9") == {"build-9": "done", "review-9": "done"}


def test_building_moved_after_process_skips_handoff_verification(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, [1000.0])
    seed(queue, "AUT-9", "building")
    build = enqueue(queue, "AUT-9", "build", "claude", "build-9")
    with queue.connect() as connection:
        connection.execute(
            """
            UPDATE jobs SET state = 'done', finished_at = 900,
                handoff_comment_id = 'missing'
            WHERE id = ?
            """,
            (build.id,),
        )
    source = LinearSource(ticket(tickets="linear", ticket_id="AUT-9"))
    source.states = {"AUT-9": RFR}
    dispatcher.poll_once(source)

    assert transitions(queue, "AUT-9") == [
        ("building", "handed-off-externally", "linear-handoff")
    ]
    assert attention(queue, "AUT-9") == []
