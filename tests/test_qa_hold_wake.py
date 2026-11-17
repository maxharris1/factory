from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.factory_md import HoldConfig
from lib.queue import cancel_queued
from lib.update_check import HeadDiff
from tests.qa_support import HEAD_A, Harness, qa_config, qa_report, ready, scenarios, verdict

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

TABLE = "| qa-1 | Verified | ok |\n| qa-2 | Verified | ok |\n| qa-3 | Not verified | slow |\n"


def held(tmp_path: Path, qa, **kwargs) -> Harness:
    run = Harness(tmp_path, qa, ship="merge", **kwargs)
    base = run.thread.base
    run.thread.base = replace(
        base,
        factory=replace(
            base.factory,
            hold=HoldConfig(label="factory:hold", approver="maxharris1", paths=("src/**",)),
        ),
        changed_files=("src/app.tsx",),
        labels=(),
        label_events=(),
        ticket_url="https://github.com/maxharris1/factory/pull/812",
    )
    return run


def approve(run: Harness, body: str = scenarios("qa-1", "qa-2", "qa-3")) -> None:
    run.post("r1", ready(HEAD_A, body=body), head=HEAD_A)
    run.poll()
    run.finish("codex", "v1")
    run.post("v1", verdict(HEAD_A, "APPROVE"))


def wakes(run: Harness) -> list[dict]:
    return [
        json.loads(detail)
        for (detail,) in run.rows(
            "SELECT detail FROM jobs WHERE action = 'awaiting-ok' ORDER BY rowid"
        )
    ]


def primary_run_id(run: Harness) -> str:
    return run.rows("SELECT run_id FROM qa_runs WHERE arm = 'primary'")[0][0]


def polls_until_wake(run: Harness, limit: int = 6) -> int:
    for count in range(1, limit + 1):
        run.poll()
        if wakes(run):
            return count
    raise AssertionError("no awaiting-ok wake")


def test_a_reported_pass_names_the_result_counts_and_evidence_link(tmp_path: Path) -> None:
    run = held(tmp_path, qa_config())
    approve(run)
    run.post("q1", qa_report(HEAD_A, "PASS", body=TABLE))
    run.poll(4)

    [wake] = wakes(run)
    link = f"/qa/runs/{primary_run_id(run)}"
    assert wake["qa"] == {
        "result": "PASS",
        "passed": 2,
        "total": 3,
        "evidence_link": link,
        "summary": f"QA PASS: 2 of 3 scenarios passed. Evidence: {link}",
    }


TODAY = {
    "approver": "maxharris1",
    "clears_with": f"<!-- orch:v1 kind=ok head={HEAD_A} --> from maxharris1",
    "head": HEAD_A,
    "held_paths": ["src/app.tsx"],
    "label": "factory:hold",
    "link": "https://github.com/maxharris1/factory/pull/812",
    "outcome": "awaiting-ok",
    "post_to": "https://github.com/maxharris1/factory/pull/812",
    "pr": "https://github.com/maxharris1/factory/pull/812",
    "pr_number": "812",
    "project_bot": "grok-bot",
    "reasons": ["paths"],
}


def test_with_qa_off_the_wake_detail_and_its_poll_match_today(tmp_path: Path) -> None:
    run = held(tmp_path, None)
    approve(run)

    assert polls_until_wake(run) == 1
    assert wakes(run) == [TODAY]
    run.poll(4)
    assert len(wakes(run)) == 1


def test_a_running_run_holds_the_wake_until_it_reports(tmp_path: Path) -> None:
    run = held(tmp_path, qa_config())
    approve(run)
    assert run.queue.claim("qa") is not None
    run.poll(5)
    assert run.rows("SELECT state FROM qa_runs") == [("running",)]
    assert wakes(run) == []
    assert run.rows("SELECT workflow_state FROM ticket_state") == [("awaiting-ok",)]

    run.post("q1", qa_report(HEAD_A, "FAIL", body=TABLE))
    run.poll(5)

    [wake] = wakes(run)
    assert wake["qa"]["result"] == "FAIL"
    assert {key: value for key, value in wake.items() if key != "qa"} == TODAY


def test_a_run_past_its_timeout_wakes_once_saying_qa_did_not_finish(tmp_path: Path) -> None:
    run = held(tmp_path, replace(qa_config(), timeout_minutes=5))
    approve(run)
    run.poll(5)
    assert wakes(run) == []

    run.now[0] += 5 * 60
    run.poll()
    run.post("q1", qa_report(HEAD_A, "PASS", body=TABLE))
    run.poll(5)

    [wake] = wakes(run)
    link = f"/qa/runs/{primary_run_id(run)}"
    assert wake["qa"] == {
        "result": None,
        "reason": "timeout",
        "evidence_link": link,
        "summary": f"QA did not finish (timeout). Evidence: {link}",
    }


def test_a_cancelled_run_wakes_at_once_saying_qa_did_not_finish(tmp_path: Path) -> None:
    run = held(tmp_path, qa_config())
    approve(run)
    run.poll()
    [(job_id,)] = run.rows("SELECT id FROM jobs WHERE action = 'qa'")
    with run.queue.connect() as connection:
        cancel_queued(connection, job_id, "test", run.now[0])
        connection.commit()
    run.poll(3)

    [wake] = wakes(run)
    assert wake["qa"]["reason"] == "job-cancelled"
    assert wake["qa"]["summary"].startswith("QA did not finish (job-cancelled).")


def test_no_run_needed_wakes_at_once_and_says_why(tmp_path: Path) -> None:
    def diff(repo, base, since, head):
        return HeadDiff("p1", (("docs/readme.md", 3),))

    run = held(tmp_path, qa_config(paths=("web/**",)), qa_diff=diff)
    approve(run, body="")

    assert polls_until_wake(run) == 1
    assert wakes(run)[0]["qa"] == {
        "result": None,
        "reason": "qa-not-required",
        "summary": "No QA run: qa-not-required.",
    }


def _shootout_running(run: Harness) -> None:
    run.queue.claim("qa")
    run.queue.claim("qa")


def _shootout_reported(run: Harness) -> None:
    run.post("s1", qa_report(HEAD_A, "FAIL", arm="shootout", runner="cursor-cloud", body=TABLE))


def _shootout_cancelled(run: Harness) -> None:
    [(job_id,)] = run.rows("SELECT job_id FROM qa_runs WHERE arm = 'shootout'")
    with run.queue.connect() as connection:
        cancel_queued(connection, job_id, "test", run.now[0])
        connection.commit()


@pytest.mark.parametrize(
    "shootout",
    [None, _shootout_running, _shootout_reported, _shootout_cancelled],
    ids=["queued", "running", "reported", "cancelled"],
)
@pytest.mark.parametrize("primary_reports", [True, False], ids=["primary-pass", "primary-open"])
def test_a_shootout_run_changes_neither_the_wake_timing_nor_its_detail(
    tmp_path: Path, shootout, primary_reports: bool
) -> None:
    def drive(path: Path, with_shootout: bool) -> tuple[int | None, list[dict]]:
        path.mkdir()
        run = held(path, qa_config(shootout=with_shootout))
        run.post("r1", ready(HEAD_A, body=scenarios("qa-1", "qa-2", "qa-3")), head=HEAD_A)
        run.poll()
        if with_shootout and shootout is not None:
            shootout(run)
        if primary_reports:
            run.post("q1", qa_report(HEAD_A, "PASS", body=TABLE))
        run.poll()
        run.finish("codex", "v1")
        run.post("v1", verdict(HEAD_A, "APPROVE"))
        try:
            count = polls_until_wake(run)
        except AssertionError:
            count = None
        found = wakes(run)
        for wake in found:
            assert wake["qa"].pop("evidence_link") == f"/qa/runs/{primary_run_id(run)}"
            wake["qa"].pop("summary")
        return count, found

    without = drive(tmp_path / "without", False)
    assert without == drive(tmp_path / "with", True)
    assert without[0] == (1 if primary_reports else None)
