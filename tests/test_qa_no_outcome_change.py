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

from lib.dispatcher import Comment, Dispatcher, TrackedTicket
from lib.queue import SQLiteJobQueue
from tests.qa_support import (
    HEAD_A,
    HEAD_B,
    REPO,
    TICKET,
    TRUSTED,
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


def _thread(with_qa_reports: bool) -> list[Comment]:
    bodies = [
        ("r1", ready(HEAD_A, body=scenarios("qa-1", "qa-2"))),
        ("v1", verdict(HEAD_A, "REVISE")),
        ("r2", ready(HEAD_B, "2/3", body=scenarios("qa-1", "qa-2"))),
        ("q1", qa_report(HEAD_B, "FAIL", body=BLOCKING)),
        ("v2", verdict(HEAD_B, "APPROVE", "2/3")),
        ("q2", qa_report(HEAD_B, "FAIL", body=BLOCKING)),
    ]
    if not with_qa_reports:
        bodies = [item for item in bodies if not item[0].startswith("q")]
    return [
        Comment(id=comment_id, author="maxharris1", body=body, created_at=1000.0 + index)
        for index, (comment_id, body) in enumerate(bodies)
    ]


def _replay(tmp_path: Path, qa: object, with_qa_reports: bool) -> tuple[list[tuple], tuple, list[tuple]]:
    now = [1000.0]
    path = tmp_path / "orch.db"
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    actions = FakeReviewActions()
    dispatcher = Dispatcher(
        path, queue, trusted_logins=TRUSTED, review_actions=actions, grok_delivery=FakeGrokDelivery()
    )
    comments = _thread(with_qa_reports)
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
