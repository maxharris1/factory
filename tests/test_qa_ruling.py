"""#795: an approver's /qa right|wrong records a ruling on a reported QA run."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib.approver_commands import SlashQa, parse_slash_qa
from lib.orch_yml import Approvals
from lib.queue import SQLiteJobQueue
from lib.update_check import HeadDiff
from tests.qa_support import HEAD_A, HEAD_B, Harness, qa_config, qa_report, ready, scenarios, verdict

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

REASON = "the draft was saved, the agent reloaded too early"
RULING = "SELECT head, runner, arm, ruling, ruled_by, ruled_at, ruling_reason FROM qa_runs ORDER BY rowid"


def _outcome(run: Harness, comment_id: str) -> str:
    [(outcome,)] = run.rows("SELECT outcome FROM handled_comments WHERE comment_id = ?", comment_id)
    return outcome


def _reported_fail(run: Harness) -> None:
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.post("q1", qa_report(HEAD_A, "FAIL", body="[blocking] f-1 the saved draft is gone\n"))
    run.poll()


def test_an_approvers_ruling_lands_on_the_reported_run(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    _reported_fail(run)

    run.post("m1", f"/qa wrong\n{REASON}")
    ruled_at = run.now[0]
    run.poll()

    assert _outcome(run, "m1") == "qa-ruled"
    assert run.rows(RULING) == [(HEAD_A, "claude-cloud", "primary", "wrong", "maxharris1", ruled_at, REASON)]


def test_the_ruling_targets_the_current_heads_run_only(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    _reported_fail(run)
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "REVISE"))
    run.poll()
    run.finish("claude", "r2")
    run.post("r2", ready(HEAD_B, "2/3", body=scenarios("qa-1")), head=HEAD_B)
    run.poll()
    run.post("q2", qa_report(HEAD_B, "FAIL", body="[blocking] f-1 the saved draft is gone\n"))
    run.poll()

    run.post("m1", "/qa wrong")
    ruled_at = run.now[0]
    run.poll()

    assert _outcome(run, "m1") == "qa-ruled"
    assert run.rows(RULING) == [
        (HEAD_A, "claude-cloud", "primary", None, None, None, None),
        (HEAD_B, "claude-cloud", "primary", "wrong", "maxharris1", ruled_at, None),
    ]


def test_a_later_ruling_replaces_the_earlier_one(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    _reported_fail(run)
    run.post("m1", f"/qa wrong\n{REASON}")
    run.poll()

    run.post("m2", "/qa right\nlooked again, the draft really is lost")
    ruled_at = run.now[0]
    run.poll()

    assert _outcome(run, "m2") == "qa-ruled"
    assert run.rows(RULING) == [
        (HEAD_A, "claude-cloud", "primary", "right", "maxharris1", ruled_at, "looked again, the draft really is lost")
    ]


def _shootout_reported(run: Harness) -> None:
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.post("q1", qa_report(HEAD_A, "FAIL", body="[blocking] f-1 the saved draft is gone\n"))
    run.post("q2", qa_report(HEAD_A, "PASS", arm="shootout", runner="cursor-cloud"))
    run.poll()


def test_a_named_runner_rules_its_shootout_run_and_leaves_the_primary(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=True))
    _shootout_reported(run)

    run.post("m1", "/qa wrong cursor-cloud")
    ruled_at = run.now[0]
    run.poll()

    assert _outcome(run, "m1") == "qa-ruled"
    assert run.rows(RULING) == [
        (HEAD_A, "claude-cloud", "primary", None, None, None, None),
        (HEAD_A, "cursor-cloud", "shootout", "wrong", "maxharris1", ruled_at, None),
    ]


def test_an_unknown_runner_rules_nothing(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=True))
    _shootout_reported(run)

    run.post("m1", "/qa wrong codex-cloud")
    run.poll()

    assert _outcome(run, "m1") == "qa-ruling-no-run"
    assert [row[3:] for row in run.rows(RULING)] == [(None, None, None, None)] * 2


def test_a_ruling_with_no_reported_run_rules_nothing(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()

    run.post("m1", "/qa wrong")
    run.poll()

    assert _outcome(run, "m1") == "qa-ruling-no-run"
    assert run.rows("SELECT state, ruling FROM qa_runs") == [("queued", None)]


def test_a_head_that_needed_no_qa_has_no_run_to_rule(tmp_path: Path) -> None:
    outside = HeadDiff("p1", (("README.md", 3),))
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=lambda repo, base, since, head: outside)
    run.post("r1", ready(HEAD_A), head=HEAD_A)
    run.poll()
    assert run.rows("SELECT state, reason, comment_id FROM qa_runs") == [("reported", "qa-not-required", None)]

    run.post("m1", "/qa wrong")
    run.poll()

    assert _outcome(run, "m1") == "qa-ruling-no-run"
    assert run.rows("SELECT ruling FROM qa_runs") == [(None,)]


@pytest.mark.parametrize(
    ("author", "approvers"),
    [
        ("someone-else", frozenset({"maxharris1"})),
        ("maxharris1", frozenset({"another-approver"})),
    ],
    ids=["stranger", "trusted-login-not-approver"],
)
def test_a_ruling_from_outside_the_approvers_sets_nothing_and_queues_nothing(
    tmp_path: Path, author: str, approvers: frozenset[str]
) -> None:
    run = Harness(tmp_path, qa_config(), approvals=Approvals(logins=approvers))
    _reported_fail(run)
    jobs_before = run.rows("SELECT id FROM jobs ORDER BY rowid")

    run.post("m1", f"/qa wrong\n{REASON}", author=author)
    run.poll()

    assert _outcome(run, "m1") == "qa-ruling-not-from-approver"
    assert [row[3:] for row in run.rows(RULING)] == [(None, None, None, None)]
    assert run.rows("SELECT id FROM jobs ORDER BY rowid") == jobs_before


def test_an_edited_ruling_is_refused_and_queues_nothing(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    _reported_fail(run)
    jobs_before = run.rows("SELECT id FROM jobs ORDER BY rowid")

    run.thread.post("m1", "/qa wrong", at=run.now[0], edited_at=run.now[0] + 1)
    run.poll()

    assert _outcome(run, "m1") == "rejected-edited"
    assert [row[3:] for row in run.rows(RULING)] == [(None, None, None, None)]
    assert run.rows("SELECT id FROM jobs ORDER BY rowid") == jobs_before


@pytest.mark.parametrize(
    "body",
    [
        "/qa",
        "/qa maybe",
        "/qa wrongly",
        "/qa wrong a b",
        "/qa right\nreason\nthird line",
        "/qa-wrong",
        "please /qa wrong",
        "/go",
        "",
    ],
)
def test_not_a_ruling(body: str) -> None:
    assert parse_slash_qa(body) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/qa right", SlashQa(ruling="right", runner=None, reason=None)),
        ("/QA Wrong cursor-cloud", SlashQa(ruling="wrong", runner="cursor-cloud", reason=None)),
        ("  /qa wrong Cursor-Cloud  ", SlashQa(ruling="wrong", runner="cursor-cloud", reason=None)),
        ("/qa wrong\n\nreason here", SlashQa(ruling="wrong", runner=None, reason="reason here")),
    ],
)
def test_a_ruling(body: str, expected: SlashQa) -> None:
    assert parse_slash_qa(body) == expected


OLD_QA_RUNS = """
CREATE TABLE qa_runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    head TEXT NOT NULL,
    patch_id TEXT,
    round INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL CHECK (scope IN ('full', 'targeted', 'smoke', 'none')),
    runner TEXT NOT NULL,
    arm TEXT NOT NULL CHECK (arm IN ('primary', 'shootout')),
    job_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'reported', 'cancelled', 'stale')),
    result TEXT CHECK (result IS NULL OR result IN ('PASS', 'FAIL', 'BLOCKED')),
    reason TEXT,
    comment_id TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    CHECK ((scope = 'none') = (job_id IS NULL))
)
"""


def test_an_existing_qa_runs_table_gains_the_ruling_columns_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "orch.db"
    with sqlite3.connect(path) as connection:
        connection.execute(OLD_QA_RUNS)
        connection.execute(
            "INSERT INTO qa_runs (run_id, repo, ticket_id, head, round, scope, runner, arm, job_id,"
            " state, result, comment_id, started_at, finished_at)"
            " VALUES ('run-1', 'maxharris1/factory', '763', ?, 1, 'full', 'claude-cloud', 'primary',"
            " 'job-1', 'reported', 'FAIL', 'q1', 1000.0, 1030.0)",
            (HEAD_A,),
        )

    Harness(tmp_path, qa_config())

    with SQLiteJobQueue(path, clock=lambda: 0.0).connect() as connection:
        rows = connection.execute(
            "SELECT run_id, result, ruling, ruled_by, ruled_at, ruling_reason FROM qa_runs"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE qa_runs SET ruling = 'maybe'")
    assert [tuple(row) for row in rows] == [("run-1", "FAIL", None, None, None, None)]
