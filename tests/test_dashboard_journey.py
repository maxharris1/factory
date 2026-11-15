"""Issue #145: one visit list for the ticket page's run log and journey.

`job_flow` returns `visits` beside `path`, one per path entry in the same
order, with who worked each visit, when it started and how long it took
where the database knows, `hops_to_shipped` and `all_durations_known`.
"""

import json
import sqlite3
from pathlib import Path

import pytest

import lib.dashboard_flow as flow
from lib.dashboard import collect_snapshot
from lib.telemetry import DDL as TELEMETRY_DDL
from tests.test_dashboard import NOW, add_job, add_ticket
from tests.test_dashboard_layout import CHART, _snapshot, seeded

FIXTURE = [
    "intake", "questions", "planning", "plan-check", "building",
    "planning", "review", "fixing", "stuck", "fixing",
]
OFF_CHART = "no direct transition on the chart"


def _chart() -> dict:
    chart = flow.load_chart(CHART)
    assert chart is not None
    return chart


def _step(action: str, recipient: str, created_at: float, started_at=None, finished_at=None):
    return flow.JobStep(action, recipient, created_at, started_at, finished_at)


def _fixture_flow() -> dict:
    history = [
        _step("plan", "claude", NOW - 7000),
        _step("build", "claude", NOW - 6000),
        _step("plan", "claude", NOW - 5000),
        _step("review", "codex", NOW - 4000),
        _step("fix", "claude", NOW - 3000),
        _step("stuck", "grok", NOW - 2000),
        _step("fix", "claude", NOW - 1000),
    ]
    return flow.job_flow(_chart(), history, "fixing", NOW)


def test_visits_match_path_in_length_and_order() -> None:
    result = _fixture_flow()
    assert result["path"] == FIXTURE
    assert [visit["state"] for visit in result["visits"]] == FIXTURE


def test_fixture_visit_fields() -> None:
    visits = _fixture_flow()["visits"]
    assert [visit["who"] for visit in visits] == [
        "script", "script", "claude", "script", "claude",
        "claude", "codex", "claude", "script", "claude",
    ]
    assert [visit["off_chart"] for visit in visits] == [
        False, False, False, False, False, True, True, False, False, True,
    ]
    assert [visit["reason"] for visit in visits] == [
        None,
        "brief and acceptance criteria posted",
        "kind=go, queue plan",
        "trusted builder posts kind=plan",
        "every acceptance criterion mapped",
        OFF_CHART,
        OFF_CHART,
        "REVISE, under the round cap",
        "device offline 60 minutes or job dies twice (T6) or CI red three times",
        OFF_CHART,
    ]
    assert [visit["current"] for visit in visits] == [False] * 9 + [True]


def test_fixture_times_are_known_only_where_a_job_backs_the_visit() -> None:
    result = _fixture_flow()
    times = [(v["started_at"], v["duration_seconds"], v["time_known"]) for v in result["visits"]]
    assert times == [
        (None, None, False),
        (None, None, False),
        (NOW - 7000, None, False),
        (None, None, False),
        (NOW - 6000, 1000, True),
        (NOW - 5000, 1000, True),
        (NOW - 4000, 1000, True),
        (NOW - 3000, 1000, True),
        (NOW - 2000, 1000, True),
        (NOW - 1000, 1000, True),
    ]
    assert result["all_durations_known"] is False
    assert result["hops_to_shipped"] == 3


def test_telemetry_start_wins_over_created_at_and_finish_ends_a_visit() -> None:
    chart = _chart()
    result = flow.job_flow(
        chart,
        [_step("build", "claude", NOW - 900, NOW - 800, NOW - 500)],
        "handed-off",
        NOW,
    )
    building = next(v for v in result["visits"] if v["state"] == "building")
    assert building["started_at"] == NOW - 800
    assert building["duration_seconds"] == 300
    assert building["time_known"] is True
    assert result["visits"][-1]["state"] == "handed-off"
    assert result["visits"][-1]["started_at"] is None
    assert result["visits"][-1]["current"] is True


def test_running_job_without_finish_counts_up_to_now() -> None:
    result = flow.job_flow(
        _chart(), [_step("build", "claude", NOW - 900, NOW - 850, None)], "building", NOW
    )
    current = result["visits"][-1]
    assert (current["state"], current["started_at"], current["duration_seconds"]) == (
        "building", NOW - 850, 850
    )
    assert current["current"] is True


def test_consecutive_jobs_in_one_state_start_at_the_first() -> None:
    result = flow.job_flow(
        _chart(),
        [
            _step("review", "codex", NOW - 900),
            _step("review", "codex", NOW - 400),
        ],
        "review",
        NOW,
    )
    assert [v["state"] for v in result["visits"]].count("review") == 1
    review = result["visits"][-1]
    assert (review["started_at"], review["duration_seconds"]) == (NOW - 900, 900)


def test_all_durations_known_when_every_visit_is_backed() -> None:
    chart = _chart()
    assert chart["start"] == "intake"
    result = flow.job_flow(
        {**chart, "start": "building"},
        [
            _step("build", "claude", NOW - 900, NOW - 900, NOW - 600),
            _step("review", "codex", NOW - 600, NOW - 600, None),
        ],
        "review",
        NOW,
    )
    assert [v["state"] for v in result["visits"]] == ["building", "review"]
    assert [v["duration_seconds"] for v in result["visits"]] == [300, 600]
    assert result["all_durations_known"] is True


def test_no_chart_and_empty_history() -> None:
    result = flow.job_flow(None, [], None, NOW)
    assert result["visits"] == [] and result["path"] == []
    assert result["all_durations_known"] is False
    assert result["hops_to_shipped"] is None


HOPS = {
    "intake": 5,
    "questions": 4,
    "planning": 5,
    "plan-check": 4,
    "building": 3,
    "fixing": 3,
    "review": 2,
    "capped": 4,
    "waiting-max": 4,
    "ship": 1,
    "updating": 2,
    "merge-queue": 1,
    "awaiting-ok": 1,
    "waiting-external": 1,
    "post-merge": 1,
    "handed-off": 0,
    "merged": 0,
    "done": 0,
    "stuck": None,
    "blocked": None,
    "refused": None,
    "abandoned": None,
}


def test_hops_table_lists_every_chart_state() -> None:
    assert {state["name"] for state in _chart()["states"]} == set(HOPS)


@pytest.mark.parametrize(("state", "hops"), HOPS.items())
def test_hops_to_shipped_from_every_chart_state(state: str, hops: int | None) -> None:
    assert flow.hops_to_shipped(_chart(), state) == hops


def test_hops_to_shipped_without_a_state_or_chart() -> None:
    assert flow.hops_to_shipped(_chart(), None) is None
    assert flow.hops_to_shipped(_chart(), "no-such-state") is None
    assert flow.hops_to_shipped(None, "review") is None


def test_snapshot_flow_carries_visits(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "40")
    visits = job["flow"]["visits"]
    assert [v["state"] for v in visits] == job["flow"]["path"]
    assert [(v["who"], v["started_at"], v["duration_seconds"]) for v in visits] == [
        ("script", None, None),
        ("script", None, None),
        ("claude", NOW - 5000, 1000),
        ("codex", NOW - 4000, 1000),
        ("claude", NOW - 3000, 1000),
        ("codex", NOW - 2000, 2000),
    ]
    assert job["flow"]["hops_to_shipped"] == 2
    assert job["flow"]["all_durations_known"] is False


def test_snapshot_reads_job_telemetry_times(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(TELEMETRY_DDL)
        add_ticket(connection, "maxharris1/cleffy", "60", state="building")
        add_job(connection, "t-build", action="build", state="running", ticket_id="60",
                created_at=NOW - 900)
        connection.execute(
            "INSERT INTO job_telemetry (job_id, bot, harness, repo, started_at, updated_at, "
            "elapsed_seconds, events, turns, tool_calls, files_touched, commands) "
            "VALUES ('t-build', 'claude-bot', 'claude', 'maxharris1/cleffy', ?, ?, 0, 0, 0, '{}', 0, 0)",
            (NOW - 700, NOW - 10),
        )
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "60")
    current = job["flow"]["visits"][-1]
    assert (current["state"], current["started_at"], current["duration_seconds"]) == (
        "building", NOW - 700, 700
    )


def test_unknown_recipient_is_not_echoed_in_visits(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/cleffy", "61", state="building")
        add_job(connection, "m-build", action="build", recipient="mallory", state="queued",
                ticket_id="61")
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["queued"] if row["ticket_id"] == "61")
    assert job["flow"]["visits"][-1]["who"] == "other"
    assert "mallory" not in json.dumps(snapshot)
