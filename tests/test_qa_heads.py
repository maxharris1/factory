"""#763 ac-4: results at an old head are stale, and a new head cancels open QA."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.qa_support import HEAD_A, HEAD_B, Harness, qa_config, qa_report, ready, scenarios, verdict

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")


def _fixed_to_head_b(run: Harness, *, shootout: bool = False) -> None:
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "REVISE"))
    run.poll()
    run.finish("claude", "r2")
    run.post("r2", ready(HEAD_B, "2/3", body=scenarios("qa-1")), head=HEAD_B)
    run.poll()


def test_a_new_head_cancels_the_open_runs_and_their_jobs_at_the_old_head(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=True))
    _fixed_to_head_b(run)

    old = run.rows(
        "SELECT qa_runs.state, qa_runs.reason, jobs.state FROM qa_runs"
        " JOIN jobs ON jobs.id = qa_runs.job_id WHERE qa_runs.head = ? ORDER BY qa_runs.rowid",
        HEAD_A,
    )
    assert old == [("cancelled", "new-head", "cancelled"), ("cancelled", "new-head", "cancelled")]
    assert [job["head"] for job in run.qa_jobs() if job["state"] == "queued"] == [HEAD_B, HEAD_B]


def test_a_running_job_at_the_old_head_keeps_running_and_its_run_is_cancelled(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    assert run.queue.claim("qa") is not None
    run.poll()
    assert [row[4] for row in run.qa_runs()] == ["running"]
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "REVISE"))
    run.poll()
    run.finish("claude", "r2")
    run.post("r2", ready(HEAD_B, "2/3"), head=HEAD_B)
    run.poll()

    assert run.rows("SELECT state FROM jobs WHERE action = 'qa' AND head = ?", HEAD_A) == [("running",)]
    assert run.rows("SELECT state, reason FROM qa_runs WHERE head = ?", HEAD_A) == [("cancelled", "new-head")]


def test_a_result_for_an_old_head_is_recorded_stale_and_queues_nothing(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    _fixed_to_head_b(run)
    jobs_before = run.rows("SELECT id, state FROM jobs ORDER BY rowid")

    run.post("q1", qa_report(HEAD_A, "FAIL", body="[blocking] f-1 the page is blank\n"))
    run.poll()

    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'q1'") == [("qa-stale",)]
    assert run.rows("SELECT state, result, comment_id FROM qa_runs WHERE head = ?", HEAD_A) == [
        ("stale", "FAIL", "q1")
    ]
    assert run.rows("SELECT state FROM qa_runs WHERE head = ?", HEAD_B) == [("queued",)]
    assert run.rows("SELECT id, state FROM jobs ORDER BY rowid") == jobs_before


def test_a_result_at_the_current_head_reports_its_run(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(shootout=True))
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.post("q1", qa_report(HEAD_A, "PASS", arm="shootout", runner="cursor-cloud"))
    run.post("q2", qa_report(HEAD_A, "FAIL", arm="primary", runner="cursor-cloud"))
    run.poll()

    assert [row[2:6] for row in run.qa_runs()] == [
        ("claude-cloud", "primary", "queued", None),
        ("cursor-cloud", "shootout", "reported", "PASS"),
    ]
    outcomes = dict(run.rows("SELECT comment_id, outcome FROM handled_comments"))
    assert (outcomes["q1"], outcomes["q2"]) == ("qa-reported", "qa-unmatched")


def test_a_qa_comment_from_a_builder_is_not_recorded(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.post("q1", qa_report(HEAD_A, "PASS").replace("from=grok-bot", "from=claude-bot"))
    run.poll()

    assert [row[4] for row in run.qa_runs()] == ["queued"]
    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'q1'") == [("qa-untrusted",)]


def test_with_qa_off_a_qa_comment_stays_unhandled(tmp_path: Path) -> None:
    run = Harness(tmp_path, None)
    run.post("r1", ready(HEAD_A), head=HEAD_A)
    run.poll()
    run.post("q1", qa_report(HEAD_A, "PASS"))
    run.poll()

    assert run.rows("SELECT outcome FROM handled_comments WHERE comment_id = 'q1'") == []


def test_qa_detail_carries_live_bodies_and_regression(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config())
    body = (
        "\n## Verify, live\n"
        "- qa-1 (ac-1): Open the page with pack demo. Pass: it shows the title. Saves: after screenshot.\n"
        "- qa-2 (ac-2): Click save. Pass: it saves.\n"
        "- Regression lane: qa-2 on base.\n"
    )
    run.post("r1", ready(HEAD_A, body=body), head=HEAD_A)
    run.poll()

    [job] = run.qa_jobs()
    assert job["live"] == [
        {
            "id": "qa-1", "criterion": "ac-1", "steps": "Open the page with pack demo.",
            "passes": "it shows the title.", "pack": "demo", "saves": ["after screenshot"],
        },
        {"id": "qa-2", "criterion": "ac-2", "steps": "Click save.", "passes": "it saves.", "pack": None, "saves": []},
    ]
    assert job["regression"] == ["qa-2"]
