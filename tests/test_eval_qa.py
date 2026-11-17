"""#794: the weekly report's QA section, read from qa_runs and split by runner."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from lib import cli
from lib.eval_report import build_metrics, render_metrics, render_qa
from tests.test_dispatcher import setup

REPO = "owner/repo"
DAY = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
SINCE = "2026-11-09T00:00:00Z"
UNTIL = "2026-11-15T23:59:59Z"


def _db(directory: Path) -> Path:
    _dispatcher, queue = setup(directory)
    return queue.path


def _run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    ticket: str = "12",
    head: str = "a" * 40,
    runner: str = "codex",
    arm: str = "primary",
    scope: str = "full",
    state: str = "reported",
    result: str | None = "PASS",
    minutes: float = 10,
    started_at: float = DAY,
    comment_id: str | None = None,
) -> None:
    finished = None if state in ("queued", "running") else started_at + minutes * 60
    connection.execute(
        """
        INSERT INTO qa_runs (
            run_id, repo, ticket_id, head, round, scope, runner, arm, job_id,
            state, result, comment_id, started_at, finished_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, REPO, ticket, head, scope, runner, arm,
            None if scope == "none" else f"job-{run_id}",
            state, result if state == "reported" else None,
            comment_id or f"c-{run_id}", started_at, finished,
        ),
    )


def _approve(connection: sqlite3.Connection, ticket: str, head: str) -> None:
    comment_id = f"approve-{ticket}-{head[:7]}"
    connection.execute(
        "INSERT INTO verdict_heads (repo, ticket_id, comment_id, head, review_round, posted_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (REPO, ticket, comment_id, head, DAY),
    )
    connection.execute(
        "INSERT INTO ship_intents (repo, ticket_id, comment_id, head, policy, status) "
        "VALUES (?, ?, ?, ?, 'handoff', 'handed-off')",
        (REPO, ticket, comment_id, head),
    )


def _revise(connection: sqlite3.Connection, ticket: str, head: str) -> None:
    connection.execute(
        "INSERT INTO verdict_heads (repo, ticket_id, comment_id, head, review_round, posted_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (REPO, ticket, f"revise-{ticket}", head, DAY),
    )


def _section(db: Path) -> list[str]:
    return render_qa(build_metrics(db, since=SINCE, until=UNTIL)).splitlines()


def test_qa_section_reports_each_figure_split_by_runner(tmp_path: Path) -> None:
    db = _db(tmp_path)
    approved, revised = "b" * 40, "c" * 40
    with sqlite3.connect(db) as connection:
        _run(connection, "c1", scope="full", result="PASS", minutes=10)
        _run(connection, "c2", ticket="13", scope="targeted", result="FAIL", minutes=30, head=approved)
        _run(connection, "c3", ticket="14", scope="smoke", result="BLOCKED", minutes=20, head=revised)
        _run(connection, "c4", ticket="15", scope="full", state="running")
        _run(connection, "c5", ticket="16", scope="none", result="PASS")
        _run(connection, "c6", ticket="17", started_at=DAY - 7 * 86400, result="FAIL")
        _run(connection, "x1", runner="cursor", arm="shootout", scope="full", result="PASS", minutes=4)
        _run(
            connection, "x2", ticket="13", runner="cursor", arm="shootout", scope="targeted",
            result="FAIL", minutes=6, head=approved,
        )
        _run(
            connection, "x3", ticket="14", runner="cursor", arm="shootout", scope="smoke",
            result="FAIL", minutes=8, head=revised,
        )
        _approve(connection, "13", approved[:12])
        _revise(connection, "14", revised)

    lines = _section(db)

    assert lines[:2] == ["qa by runner", "window 2026-11-09T00:00:00Z to 2026-11-15T23:59:59Z"]
    codex = lines[lines.index("runner codex") + 1 : lines.index("runner cursor")]
    assert codex == [
        "runs full 2 targeted 1 smoke 1",
        "results PASS 1 FAIL 1 BLOCKED 1",
        "median-minutes 20.00",
        "fail-on-approved-head 1",
        "wrong-fail not ruled",
        "wrong-pass not ruled",
    ]
    cursor = lines[lines.index("runner cursor") + 1 : lines.index("runner cursor") + 7]
    assert cursor == [
        "runs full 1 targeted 1 smoke 1",
        "results PASS 1 FAIL 2 BLOCKED 0",
        "median-minutes 6.00",
        "fail-on-approved-head 1",
        "wrong-fail not ruled",
        "wrong-pass not ruled",
    ]


def test_rulings_count_wrong_fails_and_passes_per_runner(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("ALTER TABLE qa_runs ADD COLUMN ruling TEXT")
        _run(connection, "c1", result="FAIL")
        _run(connection, "c2", ticket="13", result="FAIL")
        _run(connection, "c3", ticket="14", result="PASS")
        _run(connection, "x1", runner="cursor", arm="shootout", result="PASS")
        for run_id, ruling in (("c1", "wrong"), ("c2", "right"), ("c3", "wrong")):
            connection.execute("UPDATE qa_runs SET ruling = ? WHERE run_id = ?", (ruling, run_id))

    lines = _section(db)

    codex = lines.index("runner codex")
    assert lines[codex + 5 : codex + 7] == ["wrong-fail 1", "wrong-pass 1"]
    cursor = lines.index("runner cursor")
    assert lines[cursor + 5 : cursor + 7] == ["wrong-fail not ruled", "wrong-pass not ruled"]


def test_pairs_report_agreement_and_list_each_disagreeing_ticket_with_both_links(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    agree, disagree = "d" * 40, "e" * 40
    with sqlite3.connect(db) as connection:
        _run(connection, "p1", ticket="20", head=agree, result="PASS", comment_id="101")
        _run(
            connection, "s1", ticket="20", head=agree, runner="cursor", arm="shootout",
            result="PASS", comment_id="102",
        )
        _run(connection, "p2", ticket="21", head=disagree, result="PASS", comment_id="201")
        _run(
            connection, "s2", ticket="21", head=disagree, runner="cursor", arm="shootout",
            result="FAIL", comment_id="202",
        )
        _run(connection, "p3", ticket="22", result="FAIL", comment_id="301")
        _run(
            connection, "s3", ticket="22", runner="cursor", arm="shootout", state="running",
        )

    lines = _section(db)

    pairs = lines.index("qa pairs agreed 1 disagreed 1")
    assert lines[pairs + 1 :] == [
        f"disagreed {REPO} 21 {disagree[:12]}"
        f" codex PASS https://github.com/{REPO}/pull/21#issuecomment-201"
        f" cursor FAIL https://github.com/{REPO}/pull/21#issuecomment-202",
    ]


def test_a_week_with_no_qa_run_prints_one_line_and_leaves_the_metrics_unchanged(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path / "now")
    with sqlite3.connect(db) as connection:
        _run(connection, "old", started_at=DAY - 7 * 86400)
        _run(connection, "none", ticket="13", scope="none")
    baseline = tmp_path / "o4.db"
    sqlite3.connect(baseline).close()

    current = build_metrics(db, since=SINCE, until=UNTIL)
    old = build_metrics(baseline)

    assert render_qa(current) == "qa no runs in the window\n"
    assert render_qa(old) == "qa unrecorded(no-qa_runs-table)\n"
    assert "qa" not in render_metrics(current, old).split()


def test_cli_prints_the_qa_section_after_the_metrics(tmp_path: Path, capsys) -> None:
    db = _db(tmp_path / "now")
    baseline = tmp_path / "o4.db"
    sqlite3.connect(baseline).close()
    args = ["--db", str(db), "--baseline", str(baseline), "--since", SINCE, "--until", UNTIL]

    assert cli.main(["eval-report", *args]) == 0

    metrics = build_metrics(db, since=SINCE, until=UNTIL)
    assert capsys.readouterr().out == render_metrics(metrics, build_metrics(baseline)) + (
        "qa no runs in the window\n"
    )
