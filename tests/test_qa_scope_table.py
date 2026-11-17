"""#763 ac-2: each scope-table row, driven through poll_once."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.update_check import HeadDiff
from tests.qa_support import HEAD_A, HEAD_B, Harness, qa_config, qa_report, ready, scenarios, verdict

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


class FakeDiff:
    """qa_diff keyed on (since, head); a missing key raises, as a git failure would."""

    def __init__(self, diffs: dict[tuple[str | None, str], HeadDiff]):
        self.diffs = diffs
        self.calls: list[tuple] = []

    def __call__(self, repo: str, base: str, since: str | None, head: str) -> HeadDiff:
        self.calls.append((repo, base, since, head))
        return self.diffs[(since, head)]


def _fixed(
    run: Harness,
    first_result: str,
    *,
    live: str = scenarios("qa-1", "qa-2"),
    fixed_live: str | None = None,
    report: str = "",
) -> None:
    run.post("r1", ready(HEAD_A, body=live), head=HEAD_A)
    run.poll()
    run.post("q1", qa_report(HEAD_A, first_result, body=report))
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "REVISE"))
    run.poll()
    run.finish("claude", "r2")
    run.post("r2", ready(HEAD_B, "2/3", body=live if fixed_live is None else fixed_live), head=HEAD_B)
    run.poll()


def _at_b(run: Harness) -> tuple[list[tuple], list[tuple]]:
    jobs = [(job["scope"], job["scenarios"]) for job in run.qa_jobs() if job["head"] == HEAD_B]
    rows = run.rows("SELECT scope, state, result, reason FROM qa_runs WHERE head = ?", HEAD_B)
    return jobs, rows


def _run_id(run: Harness, head: str) -> str:
    return run.rows("SELECT run_id FROM qa_runs WHERE head = ? AND arm = 'primary'", head)[0][0]


SRC = (("src/app.py", 10),)


def test_a_head_with_the_patch_id_of_a_passed_head_carries_the_pass(tmp_path: Path) -> None:
    diff = FakeDiff({(None, HEAD_A): HeadDiff("p1", SRC), (HEAD_A, HEAD_B): HeadDiff("p1", SRC)})
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=diff)
    _fixed(run, "PASS")

    assert _at_b(run) == ([], [("none", "reported", "PASS", f"carried:{_run_id(run, HEAD_A)}")])
    assert diff.calls[-1] == ("maxharris1/factory", "main", HEAD_A, HEAD_B)


def test_a_diff_since_the_last_qa_head_outside_qa_paths_carries_its_result(tmp_path: Path) -> None:
    diff = FakeDiff(
        {(None, HEAD_A): HeadDiff("p1", SRC), (HEAD_A, HEAD_B): HeadDiff("p2", (("docs/x.md", 4),))}
    )
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=diff)
    _fixed(run, "FAIL")

    assert _at_b(run) == ([], [("none", "reported", "FAIL", f"carried:{_run_id(run, HEAD_A)}")])


def test_no_live_scenario_and_a_diff_under_qa_paths_runs_smoke(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=FakeDiff({(None, HEAD_A): HeadDiff("p1", SRC)}))
    run.post("r1", ready(HEAD_A), head=HEAD_A)
    run.poll()

    assert [(job["scope"], job["scenarios"]) for job in run.qa_jobs()] == [("smoke", [])]


def test_no_live_scenario_and_no_diff_under_qa_paths_records_qa_not_required(tmp_path: Path) -> None:
    diff = FakeDiff({(None, HEAD_A): HeadDiff("p1", (("README.md", 3),))})
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=diff)
    run.post("r1", ready(HEAD_A), head=HEAD_A)
    run.poll()

    assert run.qa_jobs() == []
    assert [row[1:] for row in run.qa_runs()] == [
        ("none", "claude-cloud", "primary", "reported", None, "qa-not-required")
    ]


def test_a_fix_over_full_rerun_lines_under_qa_paths_runs_full(tmp_path: Path) -> None:
    diff = FakeDiff(
        {
            (None, HEAD_A): HeadDiff("p1", SRC),
            (HEAD_A, HEAD_B): HeadDiff("p2", (("src/a.py", 300), ("src/b.py", 101), ("docs/x.md", 900))),
        }
    )
    run = Harness(tmp_path, qa_config(paths=("src/",), full_rerun_lines=400), qa_diff=diff)
    _fixed(run, "FAIL")

    assert _at_b(run)[0] == [("full", ["qa-1", "qa-2"])]


def test_when_git_cannot_tell_a_fix_runs_full_and_carries_nothing(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=FakeDiff({}))
    _fixed(run, "PASS")

    assert _at_b(run)[0] == [("full", ["qa-1", "qa-2"])]


def _small_fix() -> FakeDiff:
    return FakeDiff({(None, HEAD_A): HeadDiff("p1", SRC), (HEAD_A, HEAD_B): HeadDiff("p2", (("src/app.py", 20),))})

TABLE = "| Flow | Proves | Result | Evidence |\n|---|---|---|---|\n"


def test_a_ready_after_a_fix_runs_targeted_on_the_scenarios_that_did_not_pass(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=_small_fix())
    report = TABLE + "| qa-1 | ac-1 | Verified | video |\n| qa-2 | ac-2 | Failed | video |\n"
    _fixed(run, "FAIL", report=report)

    assert _at_b(run)[0] == [("targeted", ["qa-2"])]


def test_a_scenario_never_reported_counts_as_not_passed(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=_small_fix())
    report = TABLE + "| qa-1 | ac-1 | Verified | video |\n| qa-2 | ac-2 | Failed | video |\n"
    _fixed(run, "FAIL", fixed_live=scenarios("qa-1", "qa-2", "qa-3"), report=report)

    assert _at_b(run)[0] == [("targeted", ["qa-2", "qa-3"])]


def test_a_targeted_run_after_every_scenario_passed_carries_every_live_id(tmp_path: Path) -> None:
    run = Harness(tmp_path, qa_config(paths=("src/",)), qa_diff=_small_fix())
    report = TABLE + "| qa-1 | ac-1 | Verified | video |\n| qa-2 | ac-2 | Verified | video |\n"
    _fixed(run, "PASS", report=report)

    assert _at_b(run)[0] == [("targeted", ["qa-1", "qa-2"])]
