from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from lib.dashboard_flow import QUEUE_JOB_BEFORE_ROW, JobStep, Transition, job_flow, load_chart
from lib.dispatch.transitions import write_workflow_state
from lib.queue import JobRequest
from tests.merge_queue_support import Board, sha
from tests.test_merge_queue_closed import closed_only_on_fresh_reads

LIB = Path(__file__).resolve().parents[1] / "lib"

WRITERS = {
    "set-state": "lib/dispatch/core.py",
    "terminal": "lib/dispatch/tracking.py",
    "enter-merged": "lib/dispatch/tracking.py",
    "t5-abandoned": "lib/dispatch/jobs.py",
    "answers-handoff": "lib/dispatch/jobs.py",
    "t5-questions": "lib/dispatch/questions.py",
    "t5-answers": "lib/dispatch/questions.py",
    "ship-intent": "lib/dispatch/ship.py",
    "ship-hold": "lib/dispatch/ship.py",
    "ship-state": "lib/dispatch/ship.py",
    "ship-discard": "lib/dispatch/ship.py",
    "handoff-reconcile": "lib/dispatch/ship.py",
    "hold-gate": "lib/dispatch/holds.py",
    "held-base-update": "lib/dispatch/holds.py",
    "update-settled": "lib/dispatch/merge_queue.py",
    "closed-unmerged": "lib/dispatch/merge_queue.py",
    "update-queued": "lib/dispatch/merge_queue.py",
    "update-failed": "lib/dispatch/merge_queue.py",
    "update-conflicts-anew": "lib/dispatch/merge_queue.py",
    "plan-check": "lib/dispatch/plan.py",
    "plan-gap": "lib/dispatch/plan.py",
    "plan-auto-split": "lib/dispatch/plan.py",
    "t3": "lib/dispatch/verdict/t3.py",
    "t3-unsat": "lib/dispatch/verdict/t3.py",
    "t3-repeat": "lib/dispatch/verdict/repeat.py",
    "cap-resume": "lib/dispatch/caps/resume.py",
    "handoff-reopen": "lib/dispatch/caps/resume.py",
    "blocked-resume": "lib/dispatch/caps/resume.py",
    "overrule-review": "lib/dispatch/caps/resume.py",
    "unsat-fix": "lib/dispatch/caps/resume.py",
    "t-blocked": "lib/dispatch/verdict/blocked.py",
    "waiting-max-qa": "lib/dispatch/qa.py",
    "retry": "lib/dispatch/retry.py",
    "rebase-queued": "lib/dispatch/rebase.py",
    "linear-handoff": "lib/dispatch/linear_states.py",
    "linear-resume": "lib/dispatch/linear_states.py",
    "linear-done": "lib/dispatch/linear_states.py",
    "linear-canceled": "lib/dispatch/linear_states.py",
}

RAW_WRITE = re.compile(
    r"(UPDATE\s+ticket_state\b(?:(?!\bWHERE\b).)*\bworkflow_state\s*="
    r"|INSERT\s+INTO\s+ticket_state\b[^)]*\bworkflow_state\b"
    r"|DO\s+UPDATE\s+SET\b(?:(?!\bWHERE\b).)*\bworkflow_state\s*=)",
    re.IGNORECASE | re.DOTALL,
)


def _lib_files():
    for path in sorted(LIB.rglob("*.py")):
        yield path, ast.parse(path.read_text(), filename=str(path))


def _sources_in_lib() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path, tree in _lib_files():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "write_workflow_state"
            ):
                continue
            [source] = [k.value for k in node.keywords if k.arg == "source"]
            assert isinstance(source, ast.Constant), f"{path}: source must be a literal"
            found.setdefault(source.value, []).append(
                str(path.relative_to(LIB.parent))
            )
    return found


def test_every_workflow_state_writer_is_listed_once_with_its_file() -> None:
    assert _sources_in_lib() == {source: [file] for source, file in WRITERS.items()}


def _called(call: ast.Call) -> str:
    return getattr(call.func, "attr", None) or getattr(call.func, "id", "")


def test_every_writer_that_queues_before_its_row_is_named_to_the_dashboard() -> None:
    queued_first = {"set-state"}
    for _, tree in _lib_files():
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            queues = [
                call.lineno for call in calls
                if "enqueue" in _called(call) or _called(call).startswith("_ensure_stuck_wake")
            ]
            for call in calls:
                if _called(call) == "write_workflow_state" and any(at < call.lineno for at in queues):
                    [source] = [k.value.value for k in call.keywords if k.arg == "source"]
                    queued_first.add(source)
    assert queued_first == QUEUE_JOB_BEFORE_ROW


def test_no_sql_in_lib_writes_workflow_state_outside_the_helper() -> None:
    offenders = []
    for path, tree in _lib_files():
        if path.name == "transitions.py":
            continue
        for node in ast.walk(tree):
            texts = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                texts.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                texts.append(
                    "".join(
                        part.value if isinstance(part, ast.Constant) else "{}"
                        for part in node.values
                    )
                )
            for text in texts:
                if RAW_WRITE.search(text):
                    offenders.append(f"{path.relative_to(LIB.parent)}:{node.lineno}")
    assert offenders == []


def test_the_raw_write_pattern_catches_each_sql_shape_it_guards() -> None:
    assert RAW_WRITE.search("UPDATE ticket_state SET workflow_state = 'x'")
    assert RAW_WRITE.search(
        "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, ?, ?)"
    )
    assert RAW_WRITE.search("ON CONFLICT(repo) DO UPDATE SET workflow_state = 'x'")
    assert not RAW_WRITE.search("UPDATE ticket_state SET stuck = 1 WHERE workflow_state = 'x'")
    assert not RAW_WRITE.search("INSERT INTO ticket_state (repo, ticket_id, stuck) VALUES (?, ?, 1)")


def _rows(board: Board, number: str = "42") -> list[sqlite3.Row]:
    with board.queue.connect() as connection:
        return connection.execute(
            """
            SELECT from_state, to_state, at, source FROM ticket_transitions
            WHERE repo = ? AND ticket_id = ? ORDER BY id
            """,
            ("owner/repo", number),
        ).fetchall()


def _assert_ledger_matches(board: Board, number: str = "42") -> list[sqlite3.Row]:
    rows = _rows(board, number)
    assert rows, "no transitions recorded"
    assert rows[0]["from_state"] is None
    for before, after in zip(rows, rows[1:]):
        assert after["from_state"] == before["to_state"]
        assert after["at"] >= before["at"]
        assert after["from_state"] != after["to_state"]
    assert rows[-1]["to_state"] == board.state(number)["workflow_state"]
    return rows


def test_a_ticket_merged_through_the_queue_records_each_state_it_entered(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=None)
    pr.mergeable, pr.mergeable_state = True, "clean"
    board.poll()

    rows = _assert_ledger_matches(board)
    assert [r["to_state"] for r in rows][-1] == "merged"
    assert {"set-state", "ship-state", "enter-merged"} <= {r["source"] for r in rows}
    assert all(r["at"] <= board.now[0] for r in rows)


def test_a_revise_round_and_a_conflict_update_record_their_transitions(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    assert board.state("42")["workflow_state"] == "fixing"
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    board.finish("codex")
    pr.mergeable, pr.mergeable_state = None, None
    board.approve(pr, "2/3")
    board.poll()
    pr.conflict()
    board.poll()
    assert board.state("42")["workflow_state"] == "updating"

    rows = _assert_ledger_matches(board)
    assert {"t3", "update-queued"} <= {r["source"] for r in rows}
    assert "fixing" in [r["to_state"] for r in rows]


def test_journey_owners_survive_jobs_enqueued_before_their_transition(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    ticking = board.queue.clock

    def tick() -> float:
        board.now[0] += 1
        return ticking()

    board.queue.clock = tick
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()

    with board.queue.connect() as connection:
        jobs = [
            JobStep(r["action"], r["recipient"], r["created_at"], None, None)
            for r in connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid")
        ]
        tracked_at = connection.execute("SELECT tracked_at FROM tracked_tickets").fetchone()[0]
    rows = [Transition(r["from_state"], r["to_state"], r["at"], r["source"]) for r in _rows(board)]
    assert any(job.created_at < row.at for job in jobs for row in rows if job.action == "build"
               and row.to_state == "building"), "the build is enqueued before its transition"

    result = job_flow(load_chart(), jobs, "review", board.now[0], rows, tracked_at)
    assert [(v["state"], v["who"]) for v in result["visits"]] == [
        ("intake", "script"),
        ("building", "claude"),
        ("review", "codex"),
        ("fixing", "claude"),
        ("review", "codex"),
    ]


def test_a_legacy_tickets_first_row_keeps_the_older_review_on_its_own_visit(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, finish_line=None)
    pr = board.open("42", sha(1))
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    ticket = board.snapshot(pr)
    board.dispatcher._prepare_ship_intent(ticket, "approval", pr.head, "handoff")
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM ticket_transitions")
    board.now[0] += 100
    board.dispatcher._discard_pending_ship_intent(ticket, "approval")

    with board.queue.connect() as connection:
        jobs = [
            JobStep(r["action"], r["recipient"], r["created_at"], None, None)
            for r in connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid")
        ]
    rows = [Transition(r["from_state"], r["to_state"], r["at"], r["source"]) for r in _rows(board)]
    assert [(row.from_state, row.to_state) for row in rows] == [("ship", "review")]

    result = job_flow(load_chart(), jobs, "review", board.now[0], rows, None)
    assert [(v["state"], v["who"], v["started_at"]) for v in result["visits"]][-3:] == [
        ("review", "codex", jobs[-1].created_at),
        ("ship", "script", None),
        ("review", "script", rows[0].at),
    ]


def test_a_closed_watched_pr_records_its_abandon(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr, mergeable=False)
    closed_only_on_fresh_reads(board)
    board.poll()

    rows = _assert_ledger_matches(board)
    assert rows[-1]["to_state"] == "abandoned"
    assert rows[-1]["source"] == "closed-unmerged"


@pytest.fixture
def board(tmp_path: Path) -> Board:
    return Board(tmp_path)


def _seed(board: Board, state: str | None) -> None:
    with board.queue.connect() as connection:
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) VALUES (?, ?, ?)",
            ("owner/repo", "7", state),
        )


def _write(board: Board, state: str, **kwargs) -> bool:
    with board.queue.connect() as connection:
        return write_workflow_state(
            connection, "Owner/Repo", "7", state, source="test", at=5.0, **kwargs
        )


def _ledger(board: Board) -> list[tuple]:
    with board.queue.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT repo, ticket_id, from_state, to_state, at, source "
                "FROM ticket_transitions ORDER BY id"
            )
        ]


def _state(board: Board) -> str | None:
    with board.queue.connect() as connection:
        row = connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = '7'"
        ).fetchone()
    return None if row is None else row[0]


def test_a_new_row_records_a_transition_from_null(board: Board) -> None:
    assert _write(board, "building", insert={"gate_count": 0, "stuck": 0})
    assert _state(board) == "building"
    assert _ledger(board) == [("owner/repo", "7", None, "building", 5.0, "test")]


def test_a_missing_row_without_insert_writes_nothing(board: Board) -> None:
    assert not _write(board, "building")
    assert _state(board) is None
    assert _ledger(board) == []


def test_an_update_records_the_prior_state(board: Board) -> None:
    _seed(board, "review")
    assert _write(board, "fixing", also={"review_round": 2})
    assert _ledger(board) == [("owner/repo", "7", "review", "fixing", 5.0, "test")]


def test_a_rejected_guard_writes_nothing(board: Board) -> None:
    _seed(board, "merged")
    assert not _write(board, "review", guard="workflow_state != 'merged'")
    assert _state(board) == "merged"
    assert _ledger(board) == []


def test_a_same_state_write_records_no_transition(board: Board) -> None:
    _seed(board, "ship")
    assert _write(board, "ship")
    assert _ledger(board) == []


def test_a_failed_transition_insert_rolls_back_the_state_and_the_enqueue(
    board: Board,
) -> None:
    _seed(board, "review")
    with board.queue.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER refuse BEFORE INSERT ON ticket_transitions
            BEGIN SELECT RAISE(ABORT, 'refused'); END
            """
        )
    request = JobRequest(
        repo="owner/repo",
        ticket_id="7",
        action="fix",
        recipient="claude",
        source_comment_id="c1",
        timeout_seconds=60,
    )
    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        with board.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            board.queue.enqueue_in_transaction(connection, request, dedupe_key="k")
            write_workflow_state(
                connection, "owner/repo", "7", "fixing", source="test", at=5.0
            )
    assert _state(board) == "review"
    assert board.jobs() == []


def test_a_failed_insert_rolls_back_a_dispatcher_state_write(board: Board) -> None:
    _seed(board, "merge-queue")
    with board.queue.connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER refuse BEFORE INSERT ON ticket_transitions
            BEGIN SELECT RAISE(ABORT, 'refused'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        board.dispatcher.record_terminal("owner/repo", "7", "merged")
    assert _state(board) == "merge-queue"
