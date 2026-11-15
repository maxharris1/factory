import json
import sqlite3
from pathlib import Path

import pytest

import lib.dashboard_flow as flow
from lib.dashboard import collect_snapshot
from lib.telemetry import DDL as TELEMETRY_DDL
from tests.test_dashboard import NOW, add_job, add_ticket
from tests.test_dashboard_layout import CHART, _snapshot, seeded
from tests.merge_queue_support import Board, sha

FIXTURE = [
    "intake", "planning", "plan-check", "building",
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
        "script", "claude", "script", "claude",
        "claude", "codex", "claude", "script", "claude",
    ]
    assert [visit["off_chart"] for visit in visits] == [
        False, True, False, False, True, True, False, False, True,
    ]
    assert [visit["reason"] for visit in visits] == [
        None,
        OFF_CHART,
        "trusted builder posts kind=plan",
        "every acceptance criterion mapped",
        OFF_CHART,
        OFF_CHART,
        "REVISE, under the round cap",
        "device offline 60 minutes or job dies twice (T6) or CI red three times",
        OFF_CHART,
    ]
    assert [visit["current"] for visit in visits] == [False] * 8 + [True]


def test_fixture_times_are_known_only_where_a_job_backs_the_visit() -> None:
    result = _fixture_flow()
    times = [(v["started_at"], v["duration_seconds"], v["time_known"]) for v in result["visits"]]
    assert times == [
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
    "blocked": 3,
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
            "elapsed_seconds, events, turns, tool_calls, files_touched, commands, test_runs, "
            "tokens_in, tokens_cached, tokens_out, steps) "
            "VALUES ('t-build', 'claude-bot', 'claude', 'maxharris1/cleffy', ?, ?, 0, 0, 0, '{}', "
            "0, 0, 0, 0, 0, 0, '[]')",
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


def _telemetry_snapshot(tmp_path: Path, state: str, started_at: object, finished_at: object):
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(TELEMETRY_DDL)
        add_ticket(connection, "maxharris1/cleffy", "62", state=state)
        add_job(connection, "t-clock", action="build", state="running", ticket_id="62",
                created_at=NOW - 900)
        connection.execute(
            "INSERT INTO job_telemetry (job_id, bot, harness, repo, started_at, finished_at, "
            "updated_at, elapsed_seconds, events, turns, tool_calls, files_touched, commands, "
            "test_runs, tokens_in, tokens_cached, tokens_out, steps) "
            "VALUES ('t-clock', 'claude-bot', 'claude', 'maxharris1/cleffy', ?, ?, ?, 0, 0, 0, "
            "'{}', 0, 0, 0, 0, 0, 0, '[]')",
            (started_at, finished_at, NOW),
        )
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "62")
    return {visit["state"]: visit for visit in job["flow"]["visits"]}


BAD_TIMES = ["bad-clock", float("inf"), float("-inf")]


@pytest.mark.parametrize("bad", BAD_TIMES, ids=repr)
def test_unusable_telemetry_start_falls_back_to_created_at(tmp_path: Path, bad: object) -> None:
    visit = _telemetry_snapshot(tmp_path, "building", bad, None)["building"]
    assert (visit["started_at"], visit["duration_seconds"], visit["time_known"]) == (
        NOW - 900, 900, True
    )


@pytest.mark.parametrize("bad", BAD_TIMES, ids=repr)
def test_unusable_telemetry_finish_leaves_the_duration_unknown(
    tmp_path: Path, bad: object
) -> None:
    visit = _telemetry_snapshot(tmp_path, "review", NOW - 700, bad)["building"]
    assert (visit["started_at"], visit["duration_seconds"], visit["time_known"]) == (
        NOW - 700, None, False
    )


def _row(from_state, to_state, at, source="set-state"):
    return flow.Transition(from_state, to_state, at, source)


def _recorded(board: Board) -> tuple[list[flow.JobStep], list[flow.Transition]]:
    with board.queue.connect() as connection:
        jobs = [
            flow.JobStep(
                row["action"], row["recipient"], row["created_at"], None, None, row["attempt"]
            )
            for row in connection.execute("SELECT * FROM jobs ORDER BY created_at, rowid")
        ]
        rows = [
            flow.Transition(row["from_state"], row["to_state"], row["at"], row["source"])
            for row in connection.execute("SELECT * FROM ticket_transitions ORDER BY id")
        ]
    return jobs, rows


RECORDED_JOBS = [
    _step("build", "claude", NOW - 5000),
    _step("review", "codex", NOW - 4000),
    _step("fix", "claude", NOW - 3000),
    _step("review", "codex", NOW - 2000),
]
RECORDED = [
    _row(None, "questions", NOW - 6000),
    _row("questions", "building", NOW - 5000),
    _row("building", "review", NOW - 4000),
    _row("review", "fixing", NOW - 3000),
    _row("fixing", "review", NOW - 2000),
]


def test_fully_recorded_ticket_takes_every_time_from_the_rows() -> None:
    result = flow.job_flow(_chart(), RECORDED_JOBS, "review", NOW, RECORDED, NOW - 7000)
    assert [(v["state"], v["who"], v["started_at"], v["duration_seconds"]) for v in result["visits"]] == [
        ("intake", "script", NOW - 7000, 1000),
        ("questions", "script", NOW - 6000, 1000),
        ("building", "claude", NOW - 5000, 1000),
        ("review", "codex", NOW - 4000, 1000),
        ("fixing", "claude", NOW - 3000, 1000),
        ("review", "codex", NOW - 2000, 2000),
    ]
    assert [v["off_chart"] for v in result["visits"]] == [False] * 6
    assert result["visits"][-1]["current"] is True
    assert result["state_age_seconds"] == 2000
    assert result["all_durations_known"] is True


def test_a_job_enqueued_just_before_its_transition_belongs_to_the_state_it_works_in() -> None:
    jobs = [step._replace(created_at=step.created_at - 1) for step in RECORDED_JOBS]
    result = flow.job_flow(_chart(), jobs, "review", NOW, RECORDED, NOW - 7000)
    assert [(v["state"], v["who"], v["started_at"]) for v in result["visits"]] == [
        ("intake", "script", NOW - 7000),
        ("questions", "script", NOW - 6000),
        ("building", "claude", NOW - 5000),
        ("review", "codex", NOW - 4000),
        ("fixing", "claude", NOW - 3000),
        ("review", "codex", NOW - 2000),
    ]
    assert result["all_durations_known"] is True


def test_a_job_two_visits_before_its_state_keeps_its_time_window() -> None:
    rows = [_row(None, "building", NOW - 3000), _row("building", "review", NOW - 2000)]
    early = [_step("review", "codex", NOW - 3500), _step("build", "claude", NOW - 3001)]
    result = flow.job_flow(_chart(), early, "review", NOW, rows, NOW - 4000)
    assert [(v["state"], v["who"]) for v in result["visits"]] == [
        ("intake", "codex"),
        ("building", "claude"),
        ("review", "script"),
    ]


def test_first_row_back_through_a_script_only_state_leaves_the_old_visit_its_job() -> None:
    jobs = [_step("build", "claude", NOW - 5000), _step("review", "codex", NOW - 4000)]
    rows = [_row("ship", "review", NOW - 2000)]
    result = flow.job_flow(_chart(), jobs, "review", NOW, rows, None)
    assert [(v["state"], v["who"], v["started_at"]) for v in result["visits"]] == [
        ("intake", "script", None),
        ("building", "claude", NOW - 5000),
        ("review", "codex", NOW - 4000),
        ("ship", "script", None),
        ("review", "script", NOW - 2000),
    ]


@pytest.mark.parametrize("enqueued", [NOW - 2000, NOW - 2001, NOW - 2030], ids=["same-tick", "one-tick", "slow-label"])
@pytest.mark.parametrize("from_state", ["handed-off", "merge-queue", "updating"])
def test_first_row_back_from_beyond_the_bridge_leaves_the_old_visit_its_job(from_state, enqueued) -> None:
    jobs = [
        _step("build", "claude", NOW - 5000),
        _step("review", "codex", NOW - 4000),
        _step("review", "codex", enqueued),
    ]
    rows = [_row(from_state, "review", NOW - 2000)]
    result = flow.job_flow(_chart(), jobs, "review", NOW, rows, None)
    reviews = [(v["who"], v["started_at"]) for v in result["visits"] if v["state"] == "review"]
    assert reviews == [("codex", NOW - 4000), ("codex", NOW - 2000)]
    assert result["path"][-2] == from_state


def test_first_row_after_a_ready_keeps_both_reviews_on_their_own_visits(tmp_path: Path) -> None:
    board = Board(tmp_path, finish_line=None)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM ticket_transitions")
    board.now[0] += 1000
    frozen = board.queue.clock

    def ticking() -> float:
        board.now[0] += 1
        return frozen()

    board.queue.clock = ticking
    pr.push(sha(2))
    board.ready(pr, pr.head, "new-ready")
    board.poll()
    assert board.outcome("new-ready") == "queued-review"
    jobs, rows = _recorded(board)
    old, new = [job.created_at for job in jobs if job.action == "review"]
    assert new < rows[0].at
    result = flow.job_flow(_chart(), jobs, "review", board.now[0], rows)
    assert [(v["who"], v["started_at"]) for v in result["visits"] if v["state"] == "review"] == [
        ("codex", old),
        ("codex", rows[0].at),
    ]


@pytest.mark.parametrize("delay", [1, 100])
def test_script_only_first_row_leaves_the_old_visit_its_retried_review(
    tmp_path: Path, delay: int
) -> None:
    board = Board(tmp_path, finish_line=None)
    pr = board.open("42", sha(1))
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    failed = board.fail("codex")
    board.now[0] += 100
    board.poll()
    retry = board.finish("codex")
    assert failed.request.action == retry.request.action == "review"
    ticket = board.snapshot(pr)
    board.dispatcher._prepare_ship_intent(ticket, "approval", pr.head, "handoff")
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM ticket_transitions")
    board.now[0] += delay
    board.dispatcher._discard_pending_ship_intent(ticket, "approval")
    jobs, rows = _recorded(board)
    assert [(job.action, job.created_at) for job in jobs][-2:] == [
        ("review", failed.created_at),
        ("review", retry.created_at),
    ]
    assert [(row.from_state, row.to_state) for row in rows] == [("ship", "review")]
    result = flow.job_flow(_chart(), jobs, "review", board.now[0], rows)
    assert [(v["who"], v["started_at"]) for v in result["visits"] if v["state"] == "review"] == [
        ("codex", failed.created_at),
        ("script", rows[0].at),
    ]


def test_script_only_first_row_leaves_an_older_completed_review_to_its_visit(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, finish_line=None)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    board.now[0] += 1000
    pr.push(sha(2))
    board.ready(pr, pr.head, "second-ready")
    board.poll()
    assert board.outcome("second-ready") == "queued-review"
    board.finish("codex")
    ticket = board.snapshot(pr)
    board.dispatcher._prepare_ship_intent(ticket, "second-approval", pr.head, "handoff")
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM ticket_transitions")
    board.now[0] += 100
    board.dispatcher._discard_pending_ship_intent(ticket, "second-approval")
    jobs, rows = _recorded(board)
    first, second = [job.created_at for job in jobs if job.action == "review"]
    assert [job.attempt for job in jobs if job.action == "review"] == [1, 1]
    assert [(row.from_state, row.to_state) for row in rows] == [("ship", "review")]
    result = flow.job_flow(_chart(), jobs, "review", board.now[0], rows)
    assert [(v["who"], v["started_at"]) for v in result["visits"] if v["state"] == "review"] == [
        ("codex", first),
        ("script", rows[0].at),
    ]
    assert second < rows[0].at


def test_first_row_written_polls_after_its_review_was_queued_owns_that_review(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path, finish_line=None)
    pr = board.open("42", sha(1))
    board.build_and_approve(pr)
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM ticket_transitions")
    board.now[0] += 1000
    pr.push(sha(2))
    board.ready(pr, pr.head, "new-ready")
    board.actions.state_failures.add((pr.repo, "review"))
    board.poll()
    board.now[0] += 1000
    board.actions.state_failures.clear()
    board.poll()
    assert board.outcome("new-ready") == "queued-review"
    with board.queue.connect() as connection:
        old, new = [
            row["created_at"]
            for row in connection.execute(
                "SELECT created_at FROM jobs WHERE action = 'review' ORDER BY created_at"
            )
        ]
        (at,) = [row["at"] for row in connection.execute("SELECT at FROM ticket_transitions")]
    assert new < at - 60
    snapshot = collect_snapshot(board.queue.path, tmp_path / "missing.log", now=board.now[0])
    job = next(job for job in snapshot["queued"] if job["ticket_id"] == "42")
    assert [(v["who"], v["started_at"]) for v in job["flow"]["visits"] if v["state"] == "review"] == [
        ("codex", old),
        ("codex", at),
    ]


def test_legacy_first_row_back_into_updating_leaves_the_older_update_to_its_visit(
    tmp_path: Path,
) -> None:
    jobs = [
        _step("build", "claude", 111.0),
        _step("review", "codex", 122.0),
        _step("update-from-main", "claude", 133.0),
        _step("update-from-main", "claude", 148.0),
    ]
    rows = [_row("waiting-external", "updating", 149.0, "update-queued")]
    expected = [("claude", 133.0), ("claude", 149.0)]
    result = flow.job_flow(_chart(), jobs, "updating", 200.0, rows)
    assert [(v["who"], v["started_at"]) for v in result["visits"] if v["state"] == "updating"] == expected
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/cleffy", "64", state="updating")
        for job_id, action, recipient, state, at in [
            ("u-build", "build", "claude", "done", 111.0),
            ("u-review", "review", "codex", "done", 122.0),
            ("u-update-1", "update-from-main", "claude", "done", 133.0),
            ("u-update-2", "update-from-main", "claude", "running", 148.0),
        ]:
            add_job(connection, job_id, action=action, recipient=recipient, state=state,
                    ticket_id="64", created_at=at)
        connection.execute(
            "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
            "VALUES ('maxharris1/cleffy', '64', 'waiting-external', 'updating', 149.0, 'update-queued')"
        )
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "64")
    assert [(v["who"], v["started_at"]) for v in job["flow"]["visits"] if v["state"] == "updating"] == expected


PLAN = "<!-- orch:v1 kind=plan from=claude-bot role=builder round={round}/2 ac={ac} -->\nPlan.\n"


def test_legacy_plan_gap_first_row_leaves_the_old_plan_to_its_visit() -> None:
    jobs = [_step("plan", "claude", 111.0), _step("plan", "claude", 126.0)]
    rows = [_row("plan-check", "planning", 127.0, "plan-gap")]
    result = flow.job_flow(_chart(), jobs, "planning", 200.0, rows)
    assert [(v["who"], v["started_at"]) for v in result["visits"] if v["state"] == "planning"] == [
        ("claude", 111.0),
        ("claude", 127.0),
    ]


def test_resumed_plan_gap_first_row_owns_the_plan_it_queued(tmp_path: Path) -> None:
    board = Board(tmp_path, finish_line=None)
    pr = board.open("42", sha(1))
    board.go(pr, "go", state="planning")
    board.poll()
    board.finish("claude")
    board.post(pr, "plan-1", PLAN.format(round=1, ac="unmet"), "claude-login")
    board.actions.state_failures.add((pr.repo, "plan-check"))
    board.poll()
    assert board.state("42")["workflow_state"] == "plan-check"
    with board.queue.connect() as connection:
        connection.execute("DELETE FROM ticket_transitions")
    board.now[0] += 100
    board.actions.state_failures.clear()

    def ticking() -> float:
        board.now[0] += 1
        return board.now[0]

    board.queue.clock = ticking
    board.poll()
    assert board.outcome("plan-1") == "queued-plan-gap"
    jobs, rows = _recorded(board)
    old, new = [job.created_at for job in jobs if job.action == "plan"]
    assert [(row.from_state, row.to_state, row.source) for row in rows] == [
        ("plan-check", "planning", "plan-gap")
    ]
    assert old < new < rows[0].at
    expected = [("claude", old), ("claude", rows[0].at)]
    result = flow.job_flow(_chart(), jobs, "planning", board.now[0], rows)
    assert [(v["who"], v["started_at"]) for v in result["visits"] if v["state"] == "planning"] == expected
    snapshot = collect_snapshot(board.queue.path, tmp_path / "missing.log", now=board.now[0])
    job = next(job for job in snapshot["queued"] if job["ticket_id"] == "42")
    assert [(v["who"], v["started_at"]) for v in job["flow"]["visits"] if v["state"] == "planning"] == expected


@pytest.mark.parametrize(
    ("jobs", "from_state", "to_state", "visits"),
    [
        (
            [_step("build", "claude", NOW - 5000), _step("review", "codex", NOW - 4000),
             _step("fix", "claude", NOW - 3001)],
            "review",
            "fixing",
            [("building", "claude", NOW - 5000), ("review", "codex", NOW - 4000),
             ("fixing", "claude", NOW - 3000)],
        ),
        (
            [_step("build", "claude", NOW - 3001)],
            "questions",
            "building",
            [("intake", "script", None), ("questions", "script", None),
             ("building", "claude", NOW - 3000)],
        ),
    ],
    ids=["job-backed-from-state", "script-only-from-state"],
)
def test_first_row_takes_the_job_enqueued_just_before_it(jobs, from_state, to_state, visits) -> None:
    rows = [_row(from_state, to_state, NOW - 3000)]
    result = flow.job_flow(_chart(), jobs, to_state, NOW, rows, None)
    assert [(v["state"], v["who"], v["started_at"]) for v in result["visits"]][-len(visits):] == visits
    assert [v["state"] for v in result["visits"]].count(to_state) == 1


@pytest.mark.parametrize("tracked_at", [None, NOW - 5500], ids=["untracked", "after-first-row"])
def test_start_state_has_no_time_without_a_tracked_at_before_the_first_row(tracked_at) -> None:
    result = flow.job_flow(_chart(), RECORDED_JOBS, "review", NOW, RECORDED, tracked_at)
    intake, questions = result["visits"][:2]
    assert (intake["state"], intake["started_at"], intake["duration_seconds"]) == (
        "intake", None, None
    )
    assert (questions["state"], questions["started_at"]) == ("questions", NOW - 6000)
    assert result["all_durations_known"] is False


def test_no_rows_gives_the_job_derived_flow() -> None:
    history = [
        _step("plan", "claude", NOW - 7000),
        _step("build", "claude", NOW - 6000),
        _step("plan", "claude", NOW - 5000),
        _step("review", "codex", NOW - 4000),
        _step("fix", "claude", NOW - 3000),
        _step("stuck", "grok", NOW - 2000),
        _step("fix", "claude", NOW - 1000),
    ]
    assert flow.job_flow(_chart(), history, "fixing", NOW, (), NOW - 9000) == _fixture_flow()


def test_partial_record_keeps_the_leading_visits_job_derived() -> None:
    rows = [_row("review", "fixing", NOW - 3100), _row("fixing", "review", NOW - 2100)]
    result = flow.job_flow(_chart(), RECORDED_JOBS, "review", NOW, rows, NOW - 9000)
    assert [(v["state"], v["started_at"], v["duration_seconds"]) for v in result["visits"]] == [
        ("intake", None, None),
        ("building", NOW - 5000, 1000),
        ("review", NOW - 4000, 900),
        ("fixing", NOW - 3100, 1000),
        ("review", NOW - 2100, 2100),
    ]
    assert result["all_durations_known"] is False


def test_off_chart_recorded_hop_is_not_bridged() -> None:
    rows = [_row(None, "building", NOW - 3000), _row("building", "review", NOW - 2000)]
    result = flow.job_flow(
        _chart(),
        [_step("build", "claude", NOW - 3000), _step("review", "codex", NOW - 2000)],
        "review",
        NOW,
        rows,
        NOW - 4000,
    )
    assert result["path"] == ["intake", "building", "review"]
    assert [(v["off_chart"], v["reason"]) for v in result["visits"]] == [
        (False, None),
        (True, OFF_CHART),
        (False, "ready, CI green"),
    ]
    assert result["all_durations_known"] is True


def test_current_state_past_the_last_row_has_no_start() -> None:
    rows = [*RECORDED[:-1], _row("fixing", "review", NOW - 2100)]
    result = flow.job_flow(_chart(), RECORDED_JOBS, "ship", NOW, rows, NOW - 7000)
    assert [v["state"] for v in result["visits"]][-2:] == ["review", "ship"]
    assert (result["visits"][-1]["started_at"], result["visits"][-1]["current"]) == (None, True)
    assert (result["visits"][-2]["started_at"], result["visits"][-2]["duration_seconds"]) == (
        NOW - 2100, None
    )
    assert result["all_durations_known"] is False


def _recorded_ticket(connection: sqlite3.Connection) -> None:
    add_ticket(connection, "maxharris1/cleffy", "63", state="review")
    add_job(connection, "r-build", action="build", state="done", ticket_id="63",
            created_at=NOW - 2000)
    add_job(connection, "r-review", action="review", recipient="codex", state="running",
            ticket_id="63", created_at=NOW - 1000)
    connection.executemany(
        "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
        "VALUES (?, '63', ?, ?, ?, 'test')",
        [
            ("maxharris1/cleffy", "building", "review", NOW - 1000),
            ("maxharris1/cleffy", None, "questions", NOW - 3000),
            ("maxharris1/cleffy", "questions", "building", NOW - 2000),
            ("maxharris1/other", None, "planning", NOW - 2500),
        ],
    )


def test_snapshot_reads_recorded_transitions(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _recorded_ticket(connection)
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "63")
    assert [(v["state"], v["who"], v["started_at"], v["duration_seconds"])
            for v in job["flow"]["visits"]] == [
        ("intake", "script", NOW - 3600, 600),
        ("questions", "script", NOW - 3000, 1000),
        ("building", "claude", NOW - 2000, 1000),
        ("review", "codex", NOW - 1000, 1000),
    ]
    assert job["flow"]["state_age_seconds"] == 1000
    assert job["flow"]["all_durations_known"] is True


def test_snapshot_without_the_transitions_table(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        _recorded_ticket(connection)
        connection.execute("DROP TABLE ticket_transitions")
    snapshot = collect_snapshot(db, log, now=NOW)
    job = next(row for row in snapshot["running"] if row["ticket_id"] == "63")
    assert [(v["state"], v["started_at"], v["duration_seconds"])
            for v in job["flow"]["visits"]] == [
        ("intake", None, None),
        ("building", NOW - 2000, 1000),
        ("review", NOW - 1000, 1000),
    ]
    assert job["flow"]["all_durations_known"] is False
