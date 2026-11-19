from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib import job_detail
from lib.update_check import HeadDiff
from tests.qa_support import HEAD_A, HEAD_B, Harness, qa_config, qa_report, ready, scenarios, verdict
from tests.test_qa_cloud import Lab, doctor_failed, drifted, with_verify
from tests.test_qa_scope_table import SRC, FakeDiff

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

FAIL_BODY = (
    "- [blocking] f-1 the saved draft is gone after reload\n"
    "Evidence: https://example.test/run/1\n"
    "| qa-1 | ac-1 | Failed | https://example.test/run/1 |\n"
)
DRIFT_LINE = (
    "- [drift] qa-2 (ac-1): the verify skill or its map no longer matches the app; "
    "maintain-verification-skill owns it, not a product change"
)
DRIFT_BODY = f"{DRIFT_LINE}\nEvidence: https://example.test/run/1\n| qa-2 | ac-1 | Drift | https://example.test/run/1 |\n"


def blocking() -> object:
    return replace(qa_config(), gate="block")


def _approved(run: Harness, *, qa_result: str | None) -> None:
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    if qa_result is not None:
        failed = qa_result == "FAIL"
        run.post(
            "q1",
            qa_report(HEAD_A, qa_result, body=FAIL_BODY if failed else "", cause="product" if failed else None),
        )
    run.poll()


def _fix_jobs(run: Harness) -> list[tuple]:
    return run.rows("SELECT recipient, head, source_comment_id, detail FROM jobs WHERE action = 'fix'")


def _outcome(run: Harness, comment_id: str) -> tuple | None:
    found = run.rows(
        "SELECT outcome FROM handled_comments WHERE comment_id = ?", comment_id
    )
    return found[0] if found else None


def _reviewing(run: Harness) -> None:
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")


def _lane_reports(run: Harness, comment_id: str, head: str, result: str) -> None:
    run.post(comment_id, qa_report(head, result, body=FAIL_BODY if result == "FAIL" else ""))
    run.finish("qa", comment_id)


def _ticket(run: Harness) -> list[tuple]:
    return run.rows("SELECT workflow_state, review_round FROM ticket_state")


def _fix_detail(run: Harness) -> dict:
    with run.queue.connect() as connection:
        fixes = connection.execute(
            "SELECT id, recipient, head, source_comment_id, detail FROM jobs WHERE action = 'fix'"
        ).fetchall()
        assert [tuple(row)[1:4] for row in fixes] == [("claude", HEAD_A, "v1")]
        return json.loads(job_detail.load_row(connection, fixes[0]["id"], fixes[0]["detail"]))


def _pass_at_a(tmp_path: Path) -> tuple[Harness, str, tuple]:
    run = Harness(tmp_path, blocking())
    _approved(run, qa_result="PASS")
    return run, HEAD_A, ("PASS", None)


def _carried_to_b(tmp_path: Path, diff_to_b: HeadDiff) -> tuple[Harness, str, tuple]:
    diff = FakeDiff({(None, HEAD_A): HeadDiff("p1", SRC), (HEAD_A, HEAD_B): diff_to_b})
    run = Harness(tmp_path, replace(qa_config(paths=("src/",)), gate="block"), qa_diff=diff)
    _reviewing(run)
    _lane_reports(run, "q1", HEAD_A, "PASS")
    run.poll()
    run.post("v1", verdict(HEAD_A, "REVISE"))
    run.poll()
    run.finish("claude", "r2")
    run.post("r2", ready(HEAD_B, "2/3", body=scenarios("qa-1")), head=HEAD_B)
    run.poll()
    run.finish("codex", "v2")
    run.post("v2", verdict(HEAD_B, "APPROVE", "2/3"))
    run_a = run.rows("SELECT run_id FROM qa_runs WHERE head = ? AND arm = 'primary'", HEAD_A)[0][0]
    return run, HEAD_B, ("PASS", f"carried:{run_a}")


def _same_patch_id(tmp_path: Path) -> tuple[Harness, str, tuple]:
    return _carried_to_b(tmp_path, HeadDiff("p1", SRC))


def _outside_qa_paths(tmp_path: Path) -> tuple[Harness, str, tuple]:
    return _carried_to_b(tmp_path, HeadDiff("p2", (("docs/x.md", 4),)))


def _qa_not_required(tmp_path: Path) -> tuple[Harness, str, tuple]:
    diff = FakeDiff({(None, HEAD_A): HeadDiff("p1", (("README.md", 3),))})
    run = Harness(tmp_path, replace(qa_config(paths=("src/",)), gate="block"), qa_diff=diff)
    run.post("r1", ready(HEAD_A), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    return run, HEAD_A, (None, "qa-not-required")


@pytest.mark.parametrize(
    "approved_head",
    [_pass_at_a, _same_patch_id, _outside_qa_paths, _qa_not_required],
    ids=["pass", "carried-same-patch-id", "carried-outside-qa-paths", "qa-not-required"],
)
def test_approve_with_covering_qa_ships(tmp_path: Path, approved_head) -> None:
    run, head, qa_row = approved_head(tmp_path)
    run.poll(3)

    assert run.rows("SELECT result, reason FROM qa_runs WHERE head = ? AND arm = 'primary'", head) == [qa_row]
    assert run.actions.handoffs == [head]


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
    assert run.rows("SELECT result, cause FROM qa_runs WHERE arm = 'primary'") == [("FAIL", "product")]
    assert run.rows("SELECT COUNT(*) FROM jobs WHERE action IN ('qa-failed', 'qa-skill-drift')") == [(0,)]


def _drift_wakes(run: Harness) -> list[dict]:
    return [json.loads(row[0]) for row in run.rows("SELECT detail FROM jobs WHERE action = 'qa-skill-drift'")]


def _approved_with(tmp_path: Path, cause: str, body: str) -> Harness:
    run = Harness(tmp_path, replace(blocking(), verify="verify-live"))
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1", "qa-2")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.post("q1", qa_report(HEAD_A, "FAIL", body=body, cause=cause))
    run.poll(3)
    return run


def test_approve_with_a_drift_fail_parks_for_skill_maintenance_without_a_round(tmp_path: Path) -> None:
    run = _approved_with(tmp_path, "drift", DRIFT_BODY)

    assert _fix_jobs(run) == []
    assert run.rows("SELECT workflow_state, review_round, paused_action FROM ticket_state") == [
        ("waiting-max", 0, "qa")
    ]
    assert run.rows("SELECT recipient, source_comment_id FROM jobs WHERE action = 'qa-skill-drift'") == [
        ("grok", "q1")
    ]
    wake = _drift_wakes(run)[0]
    assert (wake["verify"], wake["drift"], wake["evidence_link"]) == (
        "verify-live", [DRIFT_LINE.removeprefix("- ")], "https://example.test/run/1"
    )
    assert "maintain-verification-skill" in wake["ask"]
    assert run.rows("SELECT COUNT(*) FROM jobs WHERE action IN ('qa-failed', 'qa-env-failed')") == [(0,)]
    assert run.actions.handoffs == []


def test_approve_with_a_mixed_fail_fixes_only_the_product_scenario_and_wakes_drift_once(tmp_path: Path) -> None:
    run = _approved_with(tmp_path, "product", FAIL_BODY + DRIFT_BODY)
    run.poll(2)

    assert _fix_detail(run)["qa_findings"]["failed"] == ["qa-1"]
    assert _ticket(run) == [("fixing", 1)]
    assert [wake["drift"] for wake in _drift_wakes(run)] == [[DRIFT_LINE.removeprefix("- ")]]


def test_revise_with_a_drift_fail_fixes_the_review_only_and_wakes_drift(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _reviewing(run)
    run.post("q1", qa_report(HEAD_A, "FAIL", body=DRIFT_BODY, cause="drift"))
    run.poll()
    run.post("v1", verdict(HEAD_A, "REVISE"))
    run.poll(3)

    detail = _fix_detail(run)
    assert detail["instruction"] == "comment:v1"
    assert "qa_findings" not in detail
    assert _ticket(run) == [("fixing", 1)]
    assert len(_drift_wakes(run)) == 1


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


def _lane_blocked(run: Harness) -> None:
    _lane_reports(run, "q1", HEAD_A, "BLOCKED")


def _lane_times_out(run: Harness) -> None:
    run.now[0] += blocking().timeout_minutes * 60 + 1


@pytest.mark.parametrize("environment_fails", [_lane_blocked, _lane_times_out], ids=["blocked", "job-failed"])
def test_approve_with_an_environment_failure_retries_qa_once_at_the_same_head(
    tmp_path: Path, environment_fails
) -> None:
    run = Harness(tmp_path, blocking())
    _reviewing(run)
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    run.poll()
    environment_fails(run)
    run.poll(4)

    assert run.rows("SELECT attempt, head FROM jobs WHERE action = 'qa' ORDER BY rowid") == [
        (1, HEAD_A),
        (2, HEAD_A),
    ]
    assert _ticket(run) == [("review", 0)]
    assert _fix_jobs(run) == []
    assert run.actions.handoffs == []
    assert _outcome(run, "v1") is None


def test_a_second_environment_failure_parks_the_ticket_and_wakes_the_driver_once(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _reviewing(run)
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    _lane_reports(run, "q1", HEAD_A, "BLOCKED")
    run.poll(2)
    _lane_reports(run, "q2", HEAD_A, "BLOCKED")
    run.poll(6)

    assert run.rows("SELECT workflow_state, review_round, paused_action FROM ticket_state") == [
        ("waiting-max", 0, "qa")
    ]
    assert run.rows("SELECT recipient FROM jobs WHERE action = 'qa-env-failed'") == [("grok",)]
    assert len(run.qa_jobs()) == 2
    assert _fix_jobs(run) == []
    assert run.actions.handoffs == []


@pytest.mark.parametrize(
    ("qa_result", "failed"),
    [("PASS", None), ("FAIL", ["qa-1"]), ("BLOCKED", None)],
    ids=["pass", "fail", "environment-failure"],
)
@pytest.mark.parametrize("qa_first", [True, False], ids=["qa-first", "verdict-first"])
def test_revise_queues_one_fix_once_qa_is_in(
    tmp_path: Path, qa_result: str, failed: list[str] | None, qa_first: bool
) -> None:
    run = Harness(tmp_path, blocking())
    _reviewing(run)
    if qa_first:
        _lane_reports(run, "q1", HEAD_A, qa_result)
        run.poll()
        run.post("v1", verdict(HEAD_A, "REVISE"))
    else:
        run.post("v1", verdict(HEAD_A, "REVISE"))
        run.poll(2)
        assert _fix_jobs(run) == []
        _lane_reports(run, "q1", HEAD_A, qa_result)
    run.poll(3)

    detail = _fix_detail(run)
    assert detail["instruction"] == "comment:v1"
    assert ("qa_findings" in detail) == (failed is not None)
    assert detail.get("qa_findings", {}).get("failed") == failed
    assert _ticket(run) == [("fixing", 1)]
    assert len(run.qa_jobs()) == 1


def test_revise_waits_for_running_qa_until_the_timeout(tmp_path: Path) -> None:
    qa = replace(blocking(), timeout_minutes=10)
    run = Harness(tmp_path, qa)
    _reviewing(run)
    lane = run.queue.claim("qa")
    assert lane is not None
    verdict_at = run.post("v1", verdict(HEAD_A, "REVISE")).created_at
    run.poll(2)

    assert _ticket(run) == [("review", 0)]
    assert _fix_jobs(run) == []
    assert _outcome(run, "v1") is None

    run.now[0] = verdict_at + qa.timeout_minutes * 60 - 60
    run.queue.heartbeat(lane.id, owner="qa")
    run.poll()
    assert _fix_jobs(run) == []

    run.now[0] = verdict_at + qa.timeout_minutes * 60 + 1
    run.queue.heartbeat(lane.id, owner="qa")
    run.poll(2)

    detail = _fix_detail(run)
    assert detail["instruction"] == "comment:v1"
    assert "qa_findings" not in detail
    assert _ticket(run) == [("fixing", 1)]


@pytest.mark.parametrize("qa_result", ["PASS", "FAIL", "BLOCKED"])
def test_blocked_blocks_whatever_qa_reported(tmp_path: Path, qa_result: str) -> None:
    run = Harness(tmp_path, blocking())
    _reviewing(run)
    _lane_reports(run, "q1", HEAD_A, qa_result)
    run.poll()
    run.post("v1", verdict(HEAD_A, "BLOCKED"))
    run.poll(3)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [("blocked",)]
    assert _fix_jobs(run) == []
    assert len(run.qa_jobs()) == 1
    assert run.actions.handoffs == []


def test_blocked_cancels_the_running_qa(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _reviewing(run)
    run.post("v1", verdict(HEAD_A, "BLOCKED"))
    run.poll(3)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [("blocked",)]
    assert run.rows(
        "SELECT qa_runs.state, qa_runs.reason, jobs.state FROM qa_runs JOIN jobs ON qa_runs.job_id = jobs.id"
    ) == [("cancelled", "blocked", "cancelled")]
    assert _fix_jobs(run) == []


@pytest.mark.parametrize("second_verdict", ["APPROVE", "REVISE"])
def test_the_same_qa_scenario_failing_twice_takes_the_repeat_block_path(
    tmp_path: Path, second_verdict: str
) -> None:
    run = Harness(tmp_path, blocking())
    _approved(run, qa_result="FAIL")
    run.finish("claude", "r2")
    run.post("r2", ready(HEAD_B, "2/3", body=scenarios("qa-1")), head=HEAD_B)
    run.poll()
    run.finish("codex", "v2")
    run.post("v2", verdict(HEAD_B, second_verdict, "2/3"))
    run.post("q2", qa_report(HEAD_B, "FAIL", body=FAIL_BODY))
    run.poll(3)

    assert _ticket(run) == [("waiting-max", 2)]
    assert run.rows("SELECT outcome FROM review_events WHERE comment_id = 'v2'") == [("waiting-max-repeat",)]
    assert len(_fix_jobs(run)) == 1
    assert run.actions.handoffs == []


def test_a_qa_retry_without_a_flake_ticket_queues_nothing_and_wakes_the_driver_once(tmp_path: Path) -> None:
    run = Harness(tmp_path, blocking())
    _reviewing(run)
    run.post("q1", qa_report(HEAD_A, "PASS"))
    run.poll()
    before = len(run.qa_jobs())
    run.post(
        "retry1",
        f"<!-- orch:v1 kind=retry from=grok-bot role=orchestrator head={HEAD_A} -->\n"
        "Rerun QA on this head. No flake ticket has been filed.",
    )
    run.poll(3)

    assert len(run.qa_jobs()) == before
    assert len(run.rows("SELECT action FROM jobs WHERE recipient = 'grok' AND source_comment_id = 'retry1'")) == 1


@pytest.mark.parametrize(
    ("review", "workflow_state", "fixes"),
    [("REVISE", "fixing", 1), ("BLOCKED", "blocked", 0)],
)
def test_report_mode_neither_waits_for_nor_cancels_running_qa(
    tmp_path: Path, review: str, workflow_state: str, fixes: int
) -> None:
    run = Harness(tmp_path, qa_config())
    _reviewing(run)
    run.post("v1", verdict(HEAD_A, review))
    run.poll(3)

    assert run.rows("SELECT workflow_state FROM ticket_state") == [(workflow_state,)]
    assert len(_fix_jobs(run)) == fixes
    assert run.rows("SELECT qa_runs.state, jobs.state FROM qa_runs JOIN jobs ON qa_runs.job_id = jobs.id") == [
        ("queued", "queued")
    ]


def _lane_doctor_fails(tmp_path: Path, review: str) -> Lab:
    lab = Lab(tmp_path, lanes=1, gate="block")
    lab.broken[(0, 1)] = doctor_failed
    lab.run.post("r1", ready(HEAD_A, body=scenarios("qa-1", pack="demo")), head=HEAD_A)
    lab.run.poll()
    lab.run.finish("codex", "v1")
    lab.run.post("v1", verdict(HEAD_A, review))
    return lab


def _post_newest_qa_comment(lab: Lab, comment_id: str) -> None:
    lab.tick(4)
    body = list(lab.posted.values())[-1][1]
    assert "result=BLOCKED" in body and "reported BLOCKED" in body and "cause=" not in body
    lab.run.post(comment_id, body)
    lab.run.poll(3)


def test_a_lane_doctor_failure_retries_then_parks_without_spending_a_round(tmp_path: Path) -> None:
    lab = _lane_doctor_fails(tmp_path, "APPROVE")
    run = lab.run
    _post_newest_qa_comment(lab, "q1")

    assert run.rows("SELECT attempt, head, state FROM jobs WHERE action = 'qa' ORDER BY rowid") == [
        (1, HEAD_A, "done"),
        (2, HEAD_A, "queued"),
    ]
    assert _ticket(run) == [("review", 0)]
    assert _fix_jobs(run) == []
    assert run.actions.handoffs == []

    _post_newest_qa_comment(lab, "q2")

    assert len(lab.posted) == 2
    assert run.rows("SELECT workflow_state, review_round, paused_action FROM ticket_state") == [
        ("waiting-max", 0, "qa")
    ]
    assert run.rows("SELECT recipient FROM jobs WHERE action = 'qa-env-failed'") == [("grok",)]
    assert _fix_jobs(run) == []
    assert run.actions.handoffs == []


def test_revise_with_a_lane_doctor_failure_queues_a_fix_without_qa_findings(tmp_path: Path) -> None:
    lab = _lane_doctor_fails(tmp_path, "REVISE")
    lab.run.poll()
    _post_newest_qa_comment(lab, "q1")

    detail = _fix_detail(lab.run)
    assert detail["instruction"] == "comment:v1"
    assert "qa_findings" not in detail
    assert _ticket(lab.run) == [("fixing", 1)]
    assert len(lab.run.qa_jobs()) == 1


def test_a_verify_skill_lane_that_reports_drift_queues_no_product_fix(tmp_path: Path) -> None:
    lab = Lab(tmp_path, lanes=1, gate="block")
    with_verify(lab)
    lab.broken[(0, 1)] = drifted()
    run = lab.run
    run.post("r1", ready(HEAD_A, body=scenarios("qa-1", pack="demo")), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))
    lab.tick(4)
    body = lab.only_comment()
    assert "result=FAIL cause=drift" in body and "[blocking]" not in body
    run.post("q1", body)
    run.poll(3)

    assert _fix_jobs(run) == []
    assert run.rows("SELECT workflow_state, review_round, paused_action FROM ticket_state") == [
        ("waiting-max", 0, "qa")
    ]
    assert [wake["verify"] for wake in _drift_wakes(run)] == ["verify-live"]
    assert run.actions.handoffs == []
