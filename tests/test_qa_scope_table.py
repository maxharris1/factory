"""#763 ac-2: each scope-table row, driven through poll_once."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.qa_support import HEAD_A, Harness, qa_config, ready, scenarios

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")


def test_the_first_ready_queues_a_full_run_with_every_live_scenario(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1", "qa-2")), head=HEAD_A)
    run.poll()

    assert [(job["scope"], job["scenarios"], job["head"]) for job in run.qa_jobs()] == [
        ("full", ["qa-1", "qa-2"], HEAD_A)
    ]
    assert run.rows("SELECT action, recipient FROM jobs ORDER BY rowid") == [
        ("review", "codex"),
        ("qa", "qa"),
    ]


def test_without_shootout_one_job_goes_to_the_first_runner(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=False))
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()

    assert [(job["runner"], job["arm"]) for job in run.qa_jobs()] == [("claude-cloud", "primary")]


def test_with_shootout_each_runner_gets_a_job_at_the_same_head(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=True))
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()

    assert [(job["runner"], job["arm"], job["head"]) for job in run.qa_jobs()] == [
        ("claude-cloud", "primary", HEAD_A),
        ("cursor-cloud", "shootout", HEAD_A),
    ]
    assert [row[2:5] for row in run.qa_runs()] == [
        ("claude-cloud", "primary", "queued"),
        ("cursor-cloud", "shootout", "queued"),
    ]


def test_with_qa_off_no_qa_row_or_job_is_written(tmp_path: Path) -> None:
    run = Harness(tmp_path, None)
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll(3)

    assert run.qa_runs() == []
    assert run.rows("SELECT action, recipient FROM jobs") == [("review", "codex")]


def test_a_second_poll_at_the_same_head_queues_no_second_run(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll(3)

    assert len(run.qa_jobs()) == 1


def test_a_failed_qa_job_cancels_its_run_and_is_never_retried(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.now[0] += 3601
    run.poll(2)

    assert run.rows("SELECT state, failure_handled FROM jobs WHERE action = 'qa'") == [("failed", 1)]
    assert [row[4:] for row in run.qa_runs()] == [("cancelled", None, "job-failed")]
    assert run.rows("SELECT stuck FROM ticket_state") == [(0,)]
    assert run.rows("SELECT COUNT(*) FROM jobs WHERE recipient = 'grok'") == [(0,)]
