"""#765: the QA gate table under qa.gate: block, driven through poll_once.

Slice #765a covers the APPROVE row and the APPROVE with no QA row at all.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatch.qa import (
    Covered,
    EnsureQa,
    EnvFailed,
    Failed,
    FixFromQa,
    Missing,
    Proceed,
    Running,
    Wait,
    decide_gate,
)
from tests.qa_support import HEAD_A, Harness, qa_config, qa_report, ready, scenarios, verdict

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

FAIL_BODY = (
    "- [blocking] f-1 the saved draft is gone after reload\n"
    "Evidence: https://example.test/run/1\n"
    "| qa-1 | ac-1 | Failed | https://example.test/run/1 |\n"
)


def blocking() -> object:
    return replace(qa_config(), gate="block")


def _approved(run: Harness, *, qa_result: str | None) -> None:
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    if qa_result is not None:
        run.post("q1", qa_report(HEAD_A, qa_result, body=FAIL_BODY if qa_result == "FAIL" else ""))
    run.poll()


def _fix_jobs(run: Harness) -> list[tuple]:
    return run.rows("SELECT recipient, head, source_comment_id, detail FROM jobs WHERE action = 'fix'")


def _outcome(run: Harness, comment_id: str) -> tuple | None:
    found = run.rows(
        "SELECT outcome FROM handled_comments WHERE comment_id = ?", comment_id
    )
    return found[0] if found else None


@pytest.mark.parametrize(
    ("status", "action"),
    [
        (Covered(), Proceed()),
        (Failed("q1"), FixFromQa("q1")),
        (Running(), Wait("approve-qa-running")),
        (Missing(), EnsureQa()),
        (EnvFailed(), Wait("approve-qa-env-failed")),
    ],
)
def test_decide_gate_approve_row(status: object, action: object) -> None:
    assert decide_gate("APPROVE", status) == action


def test_approve_with_a_covering_pass_ships(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _approved(run, qa_result="PASS")
    run.poll()

    assert run.actions.handoffs == [HEAD_A]
    assert _fix_jobs(run) == []


def test_approve_with_a_fail_spends_the_round_on_a_qa_fix(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _approved(run, qa_result="FAIL")
    run.poll(3)

    assert run.actions.handoffs == []
    fixes = _fix_jobs(run)
    assert [row[:3] for row in fixes] == [("claude", HEAD_A, "v1")]
    detail = json.loads(fixes[0][3])
    assert detail["round"] == "2/3"
    assert detail["qa_findings"] == {
        "link": "comment:q1",
        "evidence_link": "https://example.test/run/1",
        "failed": ["qa-1"],
    }
    assert run.rows("SELECT workflow_state, review_round FROM ticket_state") == [("fixing", 1)]
    assert run.rows("SELECT COUNT(*) FROM jobs WHERE action = 'qa-failed'") == [(0,)]


def test_approve_while_qa_runs_waits_then_ships_on_pass(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _approved(run, qa_result=None)
    run.poll(3)

    assert run.actions.handoffs == []
    assert _outcome(run, "v1") is None
    assert run.rows("SELECT workflow_state, review_round FROM ticket_state") == [("review", 0)]

    run.post("q1", qa_report(HEAD_A, "PASS"))
    run.poll(2)
    assert run.actions.handoffs == [HEAD_A]


def test_approve_with_no_qa_row_queues_qa_and_ships_once_a_pass_covers(tmp_path: Path) -> None:
    run = Harness(tmp_path, None)
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.thread.base = replace(run.thread.base, factory=replace(run.thread.base.factory, qa=blocking()))
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.poll(3)

    assert run.actions.handoffs == []
    assert [(job["state"], job["head"], job["scope"]) for job in run.qa_jobs()] == [
        ("queued", HEAD_A, "full")
    ]

    run.post("q1", qa_report(HEAD_A, "PASS"))
    run.poll(2)
    assert run.actions.handoffs == [HEAD_A]


def test_qa_fail_at_the_round_cap_caps_like_a_revise(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    run.thread.base = replace(
        run.thread.base, factory=replace(run.thread.base.factory, rounds_high=1)
    )
    run.post("r1", ready(HEAD_A, "1/1", body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE", "1/1"))
    run.post("q1", qa_report(HEAD_A, "FAIL", body=FAIL_BODY))
    run.poll(3)

    assert run.actions.handoffs == []
    assert _fix_jobs(run) == []
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("capped",)]
    assert run.rows("SELECT COUNT(*) FROM jobs WHERE action = 'capped'") == [(1,)]


def test_an_approve_on_a_ticket_parked_for_qa_waits(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    with run.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = 'waiting-max', paused_action = 'qa'"
        )
        connection.commit()
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.poll(2)

    assert run.actions.handoffs == []
    assert _outcome(run, "v1") is None
    assert run.rows("SELECT COUNT(*) FROM jobs WHERE action = 'stale'") == [(0,)]


def test_report_mode_never_reads_the_qa_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = Harness(tmp_path, qa_config())

    def refuse(*_args: object) -> None:
        raise AssertionError("_qa_status read under gate: report")

    monkeypatch.setattr(run.dispatcher, "_qa_status", refuse)
    _approved(run, qa_result="FAIL")
    run.poll(12)

    assert run.actions.handoffs == [HEAD_A]
    assert run.rows("SELECT COUNT(*) FROM jobs WHERE action = 'qa-failed'") == [(1,)]
