"""#763 ac-3: a recorded QA FAIL changes no review, fix, round or ship outcome.

The same thread is replayed through tests/test_lifecycle_replay.py's Thread
with QA off, with QA on, and with QA on plus blocking FAIL reports, one at
the fixed head before its review and one after its APPROVE. The qa jobs time
out unclaimed during the run, as they do while no QA worker exists.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.cli import _retry_refusal
from lib.dispatcher import Comment, Dispatcher, TrackedTicket
from lib.queue import SQLiteJobQueue
from tests.qa_support import (
    APPROVALS,
    HEAD_A,
    HEAD_B,
    REPO,
    TICKET,
    TRUSTED,
    Harness,
    qa_config,
    qa_report,
    ready,
    scenarios,
    verdict,
)
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, factory
from tests.test_lifecycle_replay import Thread

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

BLOCKING = "[blocking] f-1 the saved draft is gone after reload\nEvidence: https://example.test/run/1\n"
RULING_REASON = "the draft was saved, the agent reloaded too early"


def _thread(with_qa_reports: bool, with_ruling: bool = False) -> list[Comment]:
    bodies = [
        ("r1", ready(HEAD_A, body=scenarios("qa-1", "qa-2"))),
        ("v1", verdict(HEAD_A, "REVISE")),
        ("r2", ready(HEAD_B, "2/3", body=scenarios("qa-1", "qa-2"))),
        ("q1", qa_report(HEAD_B, "FAIL", body=BLOCKING)),
        ("qw1", f"/qa wrong\n{RULING_REASON}"),
        ("v2", verdict(HEAD_B, "APPROVE", "2/3")),
        ("q2", qa_report(HEAD_B, "FAIL", body=BLOCKING)),
        ("qw2", f"/qa wrong\n{RULING_REASON}"),
    ]
    if not with_ruling:
        bodies = [item for item in bodies if not item[0].startswith("qw")]
    if not with_qa_reports:
        bodies = [item for item in bodies if not item[0].startswith("q")]
    return [
        Comment(id=comment_id, author="maxharris1", body=body, created_at=1000.0 + index)
        for index, (comment_id, body) in enumerate(bodies)
    ]


def _replay(
    tmp_path: Path, qa: object, with_qa_reports: bool, with_ruling: bool = False
) -> tuple[list[tuple], tuple, list[tuple]]:
    now = [1000.0]
    path = tmp_path / "orch.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    actions = FakeReviewActions()
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED,
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
        approvals=APPROVALS,
    )
    comments = _thread(with_qa_reports, with_ruling)
    base = TrackedTicket(
        repo=REPO,
        ticket_id=TICKET,
        factory=replace(factory(ship="handoff"), qa=qa),
        comments=(),
        current_head=None,
        ci={},
        risk="high",
        ready_starts_review=True,
        pr_number="812",
    )
    source = Thread(dispatcher, base, comments)
    polls = 0
    answers = [item.id for item in comments if not item.id.startswith("q")]
    shipped_at: tuple = ()
    for shown in range(1, len(comments) + 1):
        source.shown = shown
        last = [item.id for item in comments[:shown] if not item.id.startswith("q")][-1]
        polls = 0 if comments[shown - 1].id == last else polls
        for _ in range(6):
            dispatcher.poll_once(source)
            now[0] += 30
            polls += 1
            if actions.handoffs and not shipped_at:
                shipped_at = (last, polls)
        for recipient in ("claude", "codex"):
            job = queue.claim(recipient)
            if job is not None:
                answer = answers[answers.index(comments[shown - 1].id) + 1]
                queue.finish(job.id, owner=recipient, handoff_comment_id=answer)
    for _ in range(11):
        dispatcher.poll_once(source)
        now[0] += 30
    source.merged = True
    dispatcher.poll_once(source)
    with queue.connect() as connection:
        work = [
            (row["action"], row["recipient"], row["head"], json.loads(row["detail"] or "{}").get("round"))
            for row in connection.execute(
                "SELECT * FROM jobs WHERE action NOT IN ('qa', 'qa-failed') ORDER BY rowid"
            )
        ]
        states = [tuple(row) for row in connection.execute("SELECT workflow_state, review_round, stuck FROM ticket_state")]
    return work, shipped_at, states


def test_qa_reports_change_no_review_fix_round_or_ship_outcome(tmp_path: Path) -> None:
    qa = qa_config(shootout=True)
    qa = replace(qa, timeout_minutes=5)
    off = _replay(tmp_path / "off", None, False)
    on = _replay(tmp_path / "on", qa, False)
    reported = _replay(tmp_path / "reported", qa, True)

    assert off[0] == [
        ("review", "codex", HEAD_A, "1/3"),
        ("fix", "claude", HEAD_A, "2/3"),
        ("review", "codex", HEAD_B, "2/3"),
        ("done", "grok", HEAD_B, None),
    ]
    assert off[1] == ("v2", 1)
    assert on == off
    assert reported == off


def _rows(tmp_path: Path, sql: str) -> list[tuple]:
    with SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 0.0).connect() as connection:
        return [tuple(row) for row in connection.execute(sql)]


def _every_job(tmp_path: Path) -> list[tuple]:
    return [
        (action, recipient, head, json.loads(detail or "{}").get("round"))
        for action, recipient, head, detail in _rows(
            tmp_path, "SELECT action, recipient, head, detail FROM jobs ORDER BY rowid"
        )
    ]


@pytest.mark.parametrize(("gate", "shootout"), [("report", True), ("block", False)])
def test_a_qa_ruling_changes_no_job_round_or_ship_outcome(tmp_path: Path, gate: str, shootout: bool) -> None:
    qa = replace(qa_config(shootout=shootout), gate=gate, timeout_minutes=5)
    reported = _replay(tmp_path / "reported", qa, True)
    ruled = _replay(tmp_path / "ruled", qa, True, with_ruling=True)

    assert "qa" in [job[0] for job in _every_job(tmp_path / "reported")]
    assert _every_job(tmp_path / "ruled") == _every_job(tmp_path / "reported")
    assert ruled == reported
    assert _rows(tmp_path / "ruled", "SELECT outcome FROM handled_comments WHERE comment_id LIKE 'qw%'") == [
        ("qa-ruled",),
        ("qa-ruled",),
    ]
    assert _rows(
        tmp_path / "ruled",
        "SELECT head, arm, result, ruling, ruled_by, ruling_reason FROM qa_runs WHERE ruling IS NOT NULL",
    ) == [(HEAD_B, "primary", "FAIL", "wrong", "maxharris1", RULING_REASON)]


def test_the_replay_did_record_the_qa_fail_and_time_out_a_qa_job(tmp_path: Path) -> None:
    qa = replace(qa_config(), timeout_minutes=5)
    _replay(tmp_path, qa, True)
    with SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 0.0).connect() as connection:
        runs = [tuple(row) for row in connection.execute("SELECT head, state, result, reason FROM qa_runs ORDER BY rowid")]
        failed = connection.execute("SELECT COUNT(*) FROM jobs WHERE action = 'qa' AND state = 'failed'").fetchone()[0]
        wakes = connection.execute("SELECT source_comment_id FROM jobs WHERE action = 'qa-failed'").fetchall()
    assert runs[0] == (HEAD_A, "cancelled", None, "job-failed")
    assert runs[1][:3] == (HEAD_B, "reported", "FAIL")
    assert failed >= 1
    assert [tuple(row) for row in wakes] == [("q1",)]


@pytest.mark.parametrize("qa_on", [False, True])
def test_a_queued_qa_job_does_not_block_an_operator_retry(tmp_path: Path, qa_on: bool) -> None:
    run = Harness(tmp_path, qa_config() if qa_on else None)
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    for _ in range(2):
        run.finish("codex", None)
        run.poll()
    assert run.rows("SELECT stuck, stuck_reason FROM ticket_state") == [(1, "job-failed-2")]
    assert run.rows("SELECT state FROM jobs WHERE action = 'qa'") == ([("queued",)] if qa_on else [])

    machines = tmp_path / "machines.yml"
    assert _retry_refusal(run.queue.path, REPO, int(TICKET), "orch retry", machines) is None
