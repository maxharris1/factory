"""#763 ac-5: one qa-failed wake for a blocking primary FAIL at an approved head."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.qa_support import HEAD_A, Harness, qa_config, qa_report, ready, scenarios, verdict

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

BLOCKING = "- [blocking] f-1 the saved draft is gone after reload\nEvidence: https://example.test/run/1\n"


def _approved(run: Harness) -> None:
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.poll()


def _wakes(run: Harness) -> list[tuple]:
    return run.rows(
        "SELECT recipient, head, source_comment_id, detail FROM jobs WHERE action = 'qa-failed'"
    )


def test_a_blocking_primary_fail_at_an_approved_head_wakes_the_driver_once(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=True))
    _approved(run)
    run.post("q1", qa_report(HEAD_A, "FAIL", body=BLOCKING))
    run.poll(12)

    wakes = _wakes(run)
    assert [row[:3] for row in wakes] == [("grok", HEAD_A, "q1")]
    detail = json.loads(wakes[0][3])
    assert detail["finding"] == "[blocking] f-1 the saved draft is gone after reload"
    assert detail["evidence_link"] == "https://example.test/run/1"


def test_a_replay_of_the_same_comment_queues_no_second_wake(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    _approved(run)
    run.post("q1", qa_report(HEAD_A, "FAIL", body=BLOCKING))
    run.poll()
    with run.queue.connect() as connection:
        connection.execute("DELETE FROM handled_comments WHERE comment_id = 'q1'")
    run.poll(12)

    assert len(_wakes(run)) == 1


def test_the_same_fail_on_the_shootout_arm_wakes_nobody(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=True))
    _approved(run)
    run.post("q1", qa_report(HEAD_A, "FAIL", arm="shootout", runner="cursor-cloud", body=BLOCKING))
    run.poll(12)

    assert run.rows("SELECT arm, state, result FROM qa_runs WHERE arm = 'shootout'") == [
        ("shootout", "reported", "FAIL")
    ]
    assert _wakes(run) == []


def test_an_approve_after_the_fail_still_wakes_once(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.post("q1", qa_report(HEAD_A, "FAIL", body=BLOCKING))
    run.poll()
    assert _wakes(run) == []
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.poll(12)

    assert [row[:3] for row in _wakes(run)] == [("grok", HEAD_A, "q1")]


def test_a_fail_without_a_blocking_line_wakes_nobody(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    _approved(run)
    run.post("q1", qa_report(HEAD_A, "FAIL", body="[follow-up] f-1 a slow load\n"))
    run.poll(12)

    assert _wakes(run) == []
