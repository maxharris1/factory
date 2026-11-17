from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from dataclasses import replace

import pytest

from lib import feeder
from lib.dispatcher import CIResult
from lib.eval_report import Unrecorded, build_metrics
from lib.queue import JobRequest
from tests.merge_queue_support import sha
from tests.test_dispatcher import GO, VERDICT, comment, setup, ticket
from tests.test_github_actions import CHECKS, HEAD, STATUS, adapter, served
from tests.test_hold_merge_queue import held_board
from tests.test_review_before_ci import (
    board_with,
    built_with_ci_pending,
    reviewed_at_pending_ci,
)


def values(path):
    return dict(build_metrics(path).values)


def test_a_ci_fix_from_check_names_has_no_log_tail(tmp_path):
    actions, gh = adapter()
    gh.route(CHECKS, served("check-runs-failure.json"))
    gh.route(STATUS, served("status-empty.json"))
    result = actions.ci_status("owner/repo", HEAD)
    dispatcher, queue = setup(tmp_path)
    ready = comment("ready-1", "ready", author="claude-login")
    snapshot = ticket(ready, head=HEAD, ci={HEAD: result})
    dispatcher._queue_ci_fix(snapshot, ready, HEAD, result, 0, 1, dedupe_key="ci-repro")
    assert values(queue.path)["ci-fix-without-log"] == 1


def _fix_job(connection, job_id, detail, head="h" * 40):
    connection.execute(
        """
        INSERT INTO jobs (
            id, dedupe_key, repo, ticket_id, action, recipient, source_comment_id,
            timeout_seconds, generation, attempt, head, detail, state, created_at,
            last_activity_at
        ) VALUES (?, ?, 'owner/repo', '1', 'fix', 'claude', ?, 60, 1, 1, ?, ?, 'done', 0, 0)
        """,
        (job_id, job_id, job_id, head, json.dumps(detail)),
    )


def test_a_ci_fix_counts_only_without_a_log_tail(tmp_path):
    _dispatcher, queue = setup(tmp_path)
    head = "h" * 40
    with sqlite3.connect(queue.path) as connection:
        _fix_job(connection, "fallback", {"instruction": f"CI failed on {head}"})
        _fix_job(
            connection,
            "empty-tail",
            {"instruction": "failed: test", "evidence": {"ci": {"log_tail": ""}}},
        )
        _fix_job(
            connection,
            "with-tail",
            {"instruction": "failed: test", "evidence": {"ci": {"log_tail": "E  boom"}}},
        )
        _fix_job(connection, "review-fix", {"instruction": "https://x/pull/1#issuecomment-1"})
    assert values(queue.path)["ci-fix-without-log"] == 2


def _sized_board(tmp_path):
    board = held_board(tmp_path)
    original = board.snapshot
    size = [100]
    board.snapshot = lambda pr, **kw: replace(original(pr, **kw), changed_lines=size[0])
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    return board, pr, size


def _verdict_sizes(board):
    with board.queue.connect() as db:
        return [tuple(row) for row in db.execute("SELECT head, changed_lines FROM verdict_heads")]


def test_a_later_head_size_does_not_fill_an_unknown_historical_size(tmp_path):
    board, pr, size = _sized_board(tmp_path)
    with board.queue.connect() as db:
        db.execute("UPDATE verdict_heads SET changed_lines = NULL")
    size[0] = 900
    pr.push(sha(2))
    board.poll()
    assert _verdict_sizes(board) == [(sha(1), None)]


def test_an_unseen_historical_verdict_is_stored_without_a_size(tmp_path):
    board, pr, size = _sized_board(tmp_path)
    with board.queue.connect() as db:
        db.execute("DELETE FROM verdict_heads")
    size[0] = 900
    pr.push(sha(2))
    board.poll()
    assert _verdict_sizes(board) == [(sha(1), None)]


def test_a_verdict_on_the_current_head_stores_its_size(tmp_path):
    board, _pr, _size = _sized_board(tmp_path)
    assert _verdict_sizes(board) == [(sha(1), 100)]


def test_an_acceptance_after_a_later_post_does_not_count(tmp_path):
    board = board_with(tmp_path, on=True)
    pr = built_with_ci_pending(board, sha(1))
    reviewed_at_pending_ci(board, pr, pr.head)
    board.approve(pr, comment_id="first")
    board.poll()
    board.approve(pr, comment_id="second")
    board.poll()
    with board.queue.connect() as db:
        assert list(db.execute("SELECT comment_id FROM ship_intents")) == []
    pr.ci[pr.head] = "success"
    board.poll()
    assert values(board.queue.path)["verdicts-on-accepted-head"] == 0


def test_a_held_approval_is_accepted_from_its_hold_time(tmp_path):
    _dispatcher, queue = setup(tmp_path)
    head = "c" * 40
    with sqlite3.connect(queue.path) as connection:
        for comment_id, posted in (("held", 10.0), ("before", 15.0), ("after", 30.0)):
            connection.execute(
                "INSERT INTO verdict_heads VALUES ('owner/repo', '1', ?, ?, 1, ?, NULL)",
                (comment_id, head, posted),
            )
        connection.execute(
            "INSERT INTO ship_holds VALUES ('owner/repo', '1', ?, 'max', 'paths', 'held', 20)",
            (head,),
        )
    assert values(queue.path)["verdicts-on-accepted-head"] == 1


def test_a_first_round_approval_survives_a_base_update_and_a_later_round(tmp_path):
    board = held_board(tmp_path)
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    assert values(board.queue.path)["first-round-approve-rate-high-risk"] == 1.0
    pr.conflict()
    board.poll()
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    board.finish("codex")
    board.revise(pr, "1/3")
    board.poll()
    board.finish("claude")
    pr.push(sha(3))
    board.ready(pr, sha(3))
    board.poll()
    board.finish("codex")
    board.approve(pr, round_text="2/3")
    board.poll()
    after = values(board.queue.path)
    assert after["first-round-approve-rate-high-risk"] == 1.0
    assert after["first-pass-approve-rate"] == 1.0
    assert after["first-round-revise-rate"] == 0.0


def test_a_snapshot_before_posted_at_reports_the_verdict_measures_unrecorded(tmp_path):
    _, queue = setup(tmp_path)
    with sqlite3.connect(queue.path) as db:
        db.execute("ALTER TABLE verdict_heads DROP COLUMN changed_lines")
        db.execute("ALTER TABLE verdict_heads DROP COLUMN posted_at")
    before = queue.path.read_bytes()
    measured = values(queue.path)
    missing = Unrecorded("no-verdict_heads-posted_at-column")
    assert measured["approved-by-round-2-share"] == missing
    assert measured["first-round-approve-rate-lines-unknown"] == missing
    assert measured["verdicts-on-accepted-head"] == missing
    assert measured["ci-fix-without-log"] == 0
    assert measured["prs-reviewed"] == 0
    assert queue.path.read_bytes() == before


def test_a_sha_prefix_matches_the_full_accepted_head(tmp_path):
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now=now)
    full = "a" * 40
    with queue.connect() as db:
        db.execute(
            "INSERT INTO ticket_state (repo,ticket_id,workflow_state,review_round) "
            "VALUES ('owner/repo','42','review',0)"
        )
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="review",
            recipient="codex",
            source_comment_id="ready-1",
            timeout_seconds=60,
            head=full,
            detail='{"round":"1/3"}',
        ),
        dedupe_key="review-1",
    )
    job = queue.claim("codex")
    queue.finish(job.id, owner="codex", handoff_comment_id="v-first")
    first = comment(
        "v-first",
        VERDICT.format(round="1/3", head=full[:7], verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=90,
    )
    dispatcher.process(ticket(first, head=full, ci={full: CIResult("success")}))
    now[0] = 120
    later = comment(
        "v-later",
        VERDICT.format(round="1/3", head=full, verdict="REVISE", risk="high"),
        author="codex-login",
        created_at=110,
    )
    dispatcher.process(ticket(first, later, head=full, ci={full: CIResult("success")}))
    assert values(queue.path)["verdicts-on-accepted-head"] == 1


def test_a_hand_written_go_that_lifts_a_cap_is_counted(tmp_path):
    dispatcher, queue = setup(tmp_path)
    with queue.connect() as db:
        feeder.ensure_tables(db)
        db.execute(
            "INSERT INTO ticket_state (repo,ticket_id,workflow_state,review_round) "
            "VALUES ('owner/repo','42','capped',3)"
        )
    go = comment("manual-cap-go", GO.format(state="building"), author="grok-login", created_at=90)
    dispatcher.process(ticket(go, head="a" * 40))
    assert values(queue.path)["hand-written-gos"] == 1


def test_a_go_resume_counts_and_an_answers_resume_does_not(tmp_path):
    _dispatcher, queue = setup(tmp_path)
    with sqlite3.connect(queue.path) as connection:
        feeder.ensure_tables(connection)
        for comment_id, outcome, cause in (
            ("go-resume", "resumed-fix", "extra-round"),
            ("answers-resume", "resumed-fix", "answers"),
            ("blocked-go", "queued-blocked-review", None),
        ):
            connection.execute(
                "INSERT INTO handled_comments VALUES ('owner/repo', '1', ?, ?)",
                (comment_id, outcome),
            )
            connection.execute(
                "INSERT INTO handled_times VALUES ('owner/repo', '1', ?, 0)", (comment_id,)
            )
            if cause is not None:
                _fix_job(
                    connection,
                    comment_id,
                    {"instruction": "x", "evidence": {"instructions": {"cause": cause}}},
                )
    assert values(queue.path)["hand-written-gos"] == 2


def test_a_replaced_hold_keeps_its_approval_accepted_from_the_first_hold(tmp_path):
    board = held_board(tmp_path)
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    board.approve(pr, comment_id="later-on-accepted-head")
    board.poll()

    def count():
        return dict(
            build_metrics(
                board.queue.path,
                since="1970-01-01T00:02:20Z",
                until="1970-01-01T00:02:24Z",
            ).values
        )["verdicts-on-accepted-head"]

    assert count() == 1
    pr.conflict()
    board.poll()
    board.finish("claude")
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    board.finish("codex")
    board.approve(pr, comment_id="new-head-approve")
    board.poll()
    with board.queue.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT approve_comment_id FROM ship_holds")] == [
            ("new-head-approve",)
        ]
    assert count() == 1



def _accepted_head_count(path, posted):
    def iso(at):
        return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = build_metrics(path, since=iso(posted - 1), until=iso(posted + 1))
    return dict(report.values)["verdicts-on-accepted-head"]


def test_a_held_approval_replaced_without_a_base_update_stays_accepted(tmp_path):
    board = held_board(tmp_path)
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    board.approve(pr, comment_id="later-on-accepted-head")
    board.poll()
    with board.queue.connect() as db:
        posted = db.execute(
            "SELECT posted_at FROM verdict_heads WHERE comment_id = 'later-on-accepted-head'"
        ).fetchone()[0]
    assert _accepted_head_count(board.queue.path, posted) == 1
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    board.finish("codex")
    board.approve(pr, comment_id="new-head-approve")
    board.poll()
    with board.queue.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT approve_comment_id FROM ship_holds")] == [
            ("new-head-approve",)
        ]
    assert _accepted_head_count(board.queue.path, posted) == 1


def test_a_hold_from_before_the_acceptance_table_keeps_its_acceptance(tmp_path):
    board = held_board(tmp_path)
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    with board.queue.connect() as db:
        held = db.execute("SELECT approve_comment_id, held_at FROM ship_holds").fetchone()
        db.execute("DROP TABLE verdict_acceptances")
    board.dispatcher._initialize()
    with board.queue.connect() as db:
        assert [
            tuple(row) for row in db.execute("SELECT comment_id, accepted_at FROM verdict_acceptances")
        ] == [tuple(held)]
    board.poll()
    board.approve(pr, comment_id="later-on-accepted-head")
    board.poll()
    with board.queue.connect() as db:
        posted = db.execute(
            "SELECT posted_at FROM verdict_heads WHERE comment_id = 'later-on-accepted-head'"
        ).fetchone()[0]
    assert _accepted_head_count(board.queue.path, posted) == 1
    pr.push(sha(2))
    board.ready(pr, sha(2))
    board.poll()
    board.finish("codex")
    board.approve(pr, comment_id="new-head-approve")
    board.poll()
    assert _accepted_head_count(board.queue.path, posted) == 1


class _CutAfterAcceptanceTable(sqlite3.Connection):
    def execute(self, sql, *args):
        cursor = super().execute(sql, *args)
        if "CREATE TABLE IF NOT EXISTS verdict_acceptances" in sql:
            raise RuntimeError("interrupted after the acceptance table was created")
        return cursor


def test_an_upgrade_cut_after_the_acceptance_table_still_backfills_the_hold(tmp_path, monkeypatch):
    board = held_board(tmp_path)
    pr = board.open("42", sha(1))
    pr.changed_files = ("src/view/Button.tsx",)
    board.build_and_approve(pr)
    with board.queue.connect() as db:
        held = db.execute("SELECT approve_comment_id, held_at FROM ship_holds").fetchone()
        db.execute("DROP TABLE verdict_acceptances")

    def cut_connect():
        connection = sqlite3.connect(board.queue.path, factory=_CutAfterAcceptanceTable)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(board.dispatcher, "_connect", cut_connect)
    with pytest.raises(RuntimeError, match="interrupted"):
        board.dispatcher._initialize()
    monkeypatch.undo()
    board.dispatcher._initialize()

    with board.queue.connect() as db:
        assert [
            tuple(row) for row in db.execute("SELECT comment_id, accepted_at FROM verdict_acceptances")
        ] == [tuple(held)]
