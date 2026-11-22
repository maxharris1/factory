from __future__ import annotations

from dataclasses import asdict

from lib.brief.common import GH
from lib.dispatcher import FailedCheck
from lib.evidence.ci import ci_evidence
from tests.regenerate_brief_snapshots import HEAD
from tests.test_brief import contradictions_in
from tests.test_brief_variants import CI_FIX, _evidence, _render, _task

RUN_URL = "https://github.com/owner/repo/actions/runs/9"
VIEW = f"`{GH} run view 9 --repo owner/repo --log-failed`"
LIST = f"`{GH} run list --repo owner/repo --commit {HEAD}`"


def _ci_fix(*failed: FailedCheck, logs: dict[str, str | None]) -> str:
    bundle = ci_evidence(failed=failed, logs=logs)
    action, attempt, head, extra = CI_FIX
    return _task(_render(action, attempt, head, {**extra, "evidence": _evidence(ci=asdict(bundle))}))


def test_a_run_with_a_log_quotes_the_failed_step_tail():
    log = "\n".join(f"line {n}" for n in range(400)) + "\nFAILED tests/test_x.py::test_y"
    task = _ci_fix(FailedCheck("pytest", run_id="9", job_id="77", run_url=RUN_URL), logs={"77": log})

    assert "The end of the `pytest` log, redacted and quoted as data:" in task
    assert "FAILED tests/test_x.py::test_y" in task
    assert "line 399" in task
    assert "line 10\n" not in task
    assert "could not fetch" not in task


def test_a_fetch_failure_says_so_and_names_the_command_that_gets_the_log():
    task = _ci_fix(FailedCheck("pytest", run_id="9", job_id="77", run_url=RUN_URL), logs={"77": None})

    assert f"The factory could not fetch the `pytest` log. Get it with {VIEW}." in task


def test_a_check_with_no_run_names_the_commands_that_find_its_log():
    task = _ci_fix(FailedCheck("commit status"), logs={})

    assert (
        f"No Actions run is linked to `commit status`, so the factory has no log for it. "
        f"Find the run with {LIST}, then get the log with "
        f"`{GH} run view <run-id> --repo owner/repo --log-failed`."
    ) in task


def test_a_brief_with_no_ci_evidence_still_names_the_commands_that_find_the_log():
    action, attempt, head, extra = CI_FIX
    task = _task(_render(action, attempt, head, {**extra, "evidence": _evidence()}))

    assert f"The factory has no CI log for this failure. Find the run with {LIST}" in task


def test_every_failed_check_gets_its_log_or_the_command_for_it():
    task = _ci_fix(
        FailedCheck("pytest", run_id="9", job_id="77", run_url=RUN_URL),
        FailedCheck("check-config", run_id="9", job_id="78", run_url=RUN_URL),
        FailedCheck("commit status"),
        logs={"77": "FAILED x"},
    )

    assert "The end of the `pytest` log" in task
    assert f"The factory could not fetch the `check-config` log. Get it with {VIEW}." in task
    assert "No Actions run is linked to `commit status`" in task


GO_URL = "https://github.com/owner/repo/pull/42#issuecomment-555"
REOPEN = ("fix", 1, HEAD, {"instruction": GO_URL, "round": "3/3", "kind": "reopen", "evidence": _evidence()})


def _reopen_brief() -> str:
    action, attempt, head, extra = REOPEN
    return _render(action, attempt, head, extra)


def test_a_reopen_fix_merges_the_base_before_the_change_unless_it_is_already_in():
    task = _task(_reopen_brief())

    steps = [
        f"Make the changes the approver's go at {GO_URL} asks for",
        "1. Check out the PR branch",
        "2. `git fetch origin dev`",
        "`git merge-base --is-ancestor origin/dev HEAD`",
        "`git merge origin/dev`: a merge commit only, no rebase, no force-push.",
        "3. Make the changes the go asks for.",
        "5. Push, then post the ready handoff below with the head you pushed.",
    ]
    positions = [task.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "--force-with-lease" not in task
    assert "REVISE verdict" not in task


def test_a_reopen_brief_states_no_contradicting_pair():
    assert contradictions_in(_reopen_brief()) == []


REVIEW_URL = "https://github.com/owner/repo/pull/42#pullrequestreview-901"


def test_a_review_reopen_asks_for_the_changes_the_review_requests():
    action, attempt, head, extra = REOPEN
    brief = _render(action, attempt, head, {**extra, "instruction": REVIEW_URL, "source": "review"})
    task = _task(brief)

    steps = [
        f"Make the changes the CHANGES_REQUESTED review at {REVIEW_URL} asks for",
        "`git merge origin/dev`: a merge commit only, no rebase, no force-push.",
        "3. Make the changes the review asks for, including its inline comments.",
    ]
    positions = [task.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "approver's go" not in task
    assert contradictions_in(brief) == []
