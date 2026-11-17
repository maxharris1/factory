"""Issue #530: a detail record for every tracked ticket, published as tickets.json.

The records are the data behind a ticket page that opens for every tracked
ticket, shipped and stuck ones included, not only for a running or queued
job. They are built from the database, written in the same all-or-nothing
set as status.json and the pages, and leave status.json byte for byte as it
was.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

from lib import telemetry
from lib.dashboard import collect_snapshot, redact, write_snapshot_pages
from lib.dashboard_flow import load_chart
from lib.dashboard_pages.ticket_detail import _detail_model
from lib.dashboard_push import SshTransport
from tests.test_dashboard import NOW, TRACKED_AT, _fail_replacing, add_job, add_ticket, make_db, seeded
from tests.test_dashboard_telemetry import add_telemetry

PAGES = ("index.html", "job.html", "activity.html", "efficiency.html", "health.html", "ticket.html")
CLEFFY = "maxharris1/cleffy"

RECORD_KEYS = {
    "repo", "ticket_id", "state", "state_age_seconds", "round", "hops", "off_chart_count",
    "terminal", "hops_to_shipped", "all_durations_known", "visits", "shipped", "stopped", "jobs",
}
VISIT_KEYS = {
    "state", "who", "started_at", "duration_seconds", "time_known", "off_chart", "reason", "current",
}
SHIPPED_KEYS = {
    "at", "how", "intake_to_ship_seconds", "revise_rounds", "jobs", "tokens_in", "tokens_out",
    "cost_usd",
}
STOPPED_KEYS = {"state", "at", "stopped_in", "reason", "ways_out"}
WAY_OUT_KEYS = {"to", "label"}

MARKUP = "<img src=x onerror=y>"
BREAKOUT = "</script><script>"
GH_TOKEN = "gh" + "p_" + "Ab3Cd5Ef7G" * 3 + "h9J2k4"
CRED_URL = "https://" + "user:pass" + "@host/x"


def transition(
    connection: sqlite3.Connection,
    ticket_id: str,
    from_state: str | None,
    to_state: str,
    at: float,
    *,
    repo: str = CLEFFY,
) -> None:
    connection.execute(
        "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
        "VALUES (?, ?, ?, ?, ?, 'poller')",
        (repo, ticket_id, from_state, to_state, at),
    )


def walk(connection: sqlite3.Connection, ticket_id: str, states: list[str], start: float) -> None:
    """Record the ticket entering each state in turn, 100 seconds apart."""
    previous = None
    for index, state in enumerate(states):
        transition(connection, ticket_id, previous, state, start + 100 * index)
        previous = state


def publish(db: Path, log: Path, out: Path) -> tuple[dict, dict, str]:
    """(status.json, tickets.json, raw tickets.json) as the writer publishes them."""
    write_snapshot_pages(out, collect_snapshot(db, log, now=NOW))
    status = json.loads((out / "status.json").read_text(encoding="utf-8"))
    raw = (out / "tickets.json").read_text(encoding="utf-8")
    return status, json.loads(raw), raw


def records(db: Path, log: Path, tmp_path: Path) -> dict:
    return publish(db, log, tmp_path / "out")[1]["tickets"]


def strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def ways_out(state: str) -> list[dict]:
    chart = load_chart()
    return [{"to": e["to"], "label": e["label"]} for e in chart["edges"] if e["from"] == state]




def _mixed(tmp_path: Path) -> tuple[Path, Path]:
    """seeded() (ticket 12 queued, 13 running, AUT-7 stuck, 5 waiting on Max,
    14 running but untracked) plus ticket 70, merged with no job."""
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "70", state="merged", review_round=1)
        walk(connection, "70", ["intake", "questions", "building", "review", "ship", "merged"],
             NOW - 3000)
    return db, log


def test_every_tracked_ticket_has_a_record_keyed_by_repo_and_ticket(tmp_path: Path) -> None:
    db, log = _mixed(tmp_path)
    status, published, _ = publish(db, log, tmp_path / "out")
    assert published["schema"] == 1
    assert published["generated_at_epoch"] == NOW
    tickets = published["tickets"]
    assert set(tickets) == {f"{row['repo']}#{row['ticket_id']}" for row in status["tickets"]}
    assert set(tickets) == {
        "maxharris1/cleffy#12",
        "maxharris1/cleffy#13",
        "maxharris1/cleffy#70",
        "aut0-code/aut0#AUT-7",
        "maxharris1/tracy-harris-coaching#5",
    }
    for key, record in tickets.items():
        assert f"{record['repo']}#{record['ticket_id']}" == key


def test_a_shipped_ticket_with_no_job_has_its_state_and_visits(tmp_path: Path) -> None:
    db, log = _mixed(tmp_path)
    record = records(db, log, tmp_path)["maxharris1/cleffy#70"]
    assert [visit["state"] for visit in record["visits"]] == [
        "intake", "questions", "building", "review", "ship", "merged",
    ]
    assert [visit["started_at"] for visit in record["visits"]] == [
        TRACKED_AT, NOW - 2900, NOW - 2800, NOW - 2700, NOW - 2600, NOW - 2500,
    ]
    assert [visit["duration_seconds"] for visit in record["visits"]] == [
        700, 100, 100, 100, 100, 2500,
    ]
    assert (record["state"], record["state_age_seconds"], record["round"]) == ("merged", 2500, 1)
    assert (record["hops"], record["off_chart_count"]) == (5, 0)
    assert record["jobs"] == []


def test_a_stuck_ticket_with_no_job_has_a_record(tmp_path: Path) -> None:
    db, log = _mixed(tmp_path)
    record = records(db, log, tmp_path)["aut0-code/aut0#AUT-7"]
    assert record["state"] == "stuck"
    assert record["visits"][-1]["state"] == "stuck"
    assert record["visits"][-1]["current"] is True
    assert record["jobs"] == []


def test_a_live_tickets_record_matches_its_job_row(tmp_path: Path) -> None:
    db, log = _mixed(tmp_path)
    status, published, _ = publish(db, log, tmp_path / "out")
    chart = load_chart()
    row = next(row for row in status["running"] if row["ticket_id"] == "13")
    record = published["tickets"]["maxharris1/cleffy#13"]
    assert record["visits"] == row["flow"]["visits"]
    assert _detail_model({"flow": record}, chart) == _detail_model(row, chart)
    assert record["round"] == 2


def test_hops_and_off_chart_count_follow_the_visits(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "71", state="review")
        walk(connection, "71", ["intake", "review"], NOW - 500)
    record = records(db, log, tmp_path)["maxharris1/cleffy#71"]
    assert record["hops"] == 1
    assert record["off_chart_count"] == 1
    assert record["visits"][1]["off_chart"] is True




def _shipped(tmp_path: Path) -> tuple[Path, Path]:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "80", state="done", review_round=2)
        walk(
            connection, "80",
            ["intake", "building", "review", "fixing", "review", "ship", "merged", "done"],
            NOW - 3000,
        )
        for job_id in ("m-build", "m-review", "m-fix"):
            add_job(connection, job_id, state="done", ticket_id="80", created_at=NOW - 2900)
        add_ticket(connection, CLEFFY, "81", state="handed-off", review_round=0)
        walk(connection, "81", ["intake", "building", "review", "ship", "handed-off"], NOW - 2000)
        for job_id in ("h-build", "h-review"):
            add_job(connection, job_id, state="done", ticket_id="81", created_at=NOW - 1900)
        add_ticket(connection, CLEFFY, "82", state="merged")
        walk(connection, "82", ["intake", "ship", "handed-off", "merged"], NOW - 1000)
        add_job(connection, "u-build", state="done", ticket_id="82", created_at=NOW - 900)
    add_telemetry(db, "m-build", tokens_in=100, tokens_out=10, cost_usd=0.5)
    add_telemetry(db, "m-review", tokens_in=200, tokens_out=20, cost_usd=None)
    add_telemetry(db, "m-fix", tokens_in=300, tokens_out=30, cost_usd=0.25)
    add_telemetry(db, "u-build", tokens_in=400, tokens_out=40, cost_usd=None)
    return db, log


def test_tickets_whose_shown_identifiers_cut_to_the_same_text_keep_separate_records(
    tmp_path: Path,
) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    repos = ["owner/" + "a" * 94 + suffix for suffix in ("x", "y")]
    tickets = ["T-" + "ab-" * 25 + suffix for suffix in ("x", "y")]
    with sqlite3.connect(db) as connection:
        add_ticket(connection, repos[0], "1", state="building")
        add_ticket(connection, repos[1], "1", state="merged")
        add_ticket(connection, CLEFFY, tickets[0], state="review")
        add_ticket(connection, CLEFFY, tickets[1], state="stuck", stuck=1)
    published = records(db, tmp_path / "poller.log", tmp_path)
    assert {key: record["state"] for key, record in published.items()} == {
        f"{repos[0]}#1": "building",
        f"{repos[1]}#1": "merged",
        f"{CLEFFY}#{tickets[0]}": "review",
        f"{CLEFFY}#{tickets[1]}": "stuck",
    }


def test_tickets_that_redact_to_the_same_key_keep_separate_records(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    other_token = GH_TOKEN[:-1] + "Z"
    with sqlite3.connect(db) as connection:
        add_ticket(connection, f"evil/{GH_TOKEN}", "1", state="building")
        add_ticket(connection, f"evil/{other_token}", "1", state="merged")
    _, published, raw = publish(db, tmp_path / "poller.log", tmp_path / "out")
    assert GH_TOKEN not in raw and other_token not in raw
    assert sorted(record["state"] for record in published["tickets"].values()) == [
        "building", "merged",
    ]
    for key in published["tickets"]:
        assert key == redact(key)


def test_a_merged_tickets_record_says_when_and_how_it_shipped(tmp_path: Path) -> None:
    db, log = _shipped(tmp_path)
    record = records(db, log, tmp_path)["maxharris1/cleffy#80"]
    assert record["shipped"] == {
        "at": NOW - 2400,
        "how": "merged",
        "intake_to_ship_seconds": int(NOW - 2400 - TRACKED_AT),
        "revise_rounds": 2,
        "jobs": 3,
        "tokens_in": 600,
        "tokens_out": 60,
        "cost_usd": 0.75,
    }
    assert record["stopped"] is None


def test_a_handed_off_ticket_with_no_telemetry_has_null_totals(tmp_path: Path) -> None:
    db, log = _shipped(tmp_path)
    shipped = records(db, log, tmp_path)["maxharris1/cleffy#81"]["shipped"]
    assert shipped == {
        "at": NOW - 1600,
        "how": "handed-off",
        "intake_to_ship_seconds": 2000,
        "revise_rounds": 0,
        "jobs": 2,
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": None,
    }


def test_a_ticket_handed_off_then_merged_shipped_by_handoff(tmp_path: Path) -> None:
    db, log = _shipped(tmp_path)
    shipped = records(db, log, tmp_path)["maxharris1/cleffy#82"]["shipped"]
    assert (shipped["at"], shipped["how"]) == (NOW - 800, "handed-off")
    assert (shipped["tokens_in"], shipped["tokens_out"]) == (400, 40)
    assert shipped["cost_usd"] is None


def test_unmetered_jobs_leave_tokens_null_and_count_the_job(tmp_path: Path) -> None:
    db, log = _shipped(tmp_path)
    add_telemetry(db, "h-build", tokens_in=0, tokens_out=0, tokens_cached=0, tokens_unknown=1)
    shipped = records(db, log, tmp_path)["maxharris1/cleffy#81"]["shipped"]
    assert shipped["jobs"] == 2
    assert (shipped["tokens_in"], shipped["tokens_out"], shipped["cost_usd"]) == (None, None, None)


def test_live_and_stopped_tickets_have_no_shipped_facts(tmp_path: Path) -> None:
    db, log = _mixed(tmp_path)
    tickets = records(db, log, tmp_path)
    assert tickets["maxharris1/cleffy#13"]["shipped"] is None
    assert tickets["aut0-code/aut0#AUT-7"]["shipped"] is None




def test_a_stuck_ticket_says_where_it_stopped_and_why(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "90", state="stuck", stuck=1, stuck_reason="gaps-twice")
        walk(connection, "90", ["intake", "planning", "plan-check", "planning", "stuck"], NOW - 800)
    record = records(db, log, tmp_path)["maxharris1/cleffy#90"]
    assert record["stopped"] == {
        "state": "stuck",
        "at": NOW - 400,
        "stopped_in": "planning",
        "reason": "gaps-twice",
        "ways_out": ways_out("stuck"),
    }
    assert record["shipped"] is None


def test_an_abandoned_ticket_stopped_in_waiting_max(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "91", state="abandoned")
        walk(connection, "91", ["intake", "questions", "waiting-max", "abandoned"], NOW - 900)
    stopped = records(db, log, tmp_path)["maxharris1/cleffy#91"]["stopped"]
    assert stopped == {
        "state": "abandoned",
        "at": NOW - 600,
        "stopped_in": "waiting-max",
        "reason": None,
        "ways_out": ways_out("abandoned"),
    }


def test_a_capped_ticket_lists_its_ways_out_from_the_chart(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "92", state="capped", review_round=3)
        walk(connection, "92", ["intake", "building", "review", "capped"], NOW - 900)
    stopped = records(db, log, tmp_path)["maxharris1/cleffy#92"]["stopped"]
    assert stopped["ways_out"] == ways_out("capped")
    assert {"to": "fixing", "label": "Max okays one more round"} in stopped["ways_out"]
    assert stopped["stopped_in"] == "review"


def test_a_stuck_flag_on_a_live_state_stops_in_that_state(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "93", state="building", stuck=1, stuck_reason="job-timeout")
    stopped = records(db, log, tmp_path)["maxharris1/cleffy#93"]["stopped"]
    assert stopped == {
        "state": "stuck",
        "at": None,
        "stopped_in": "building",
        "reason": "job-timeout",
        "ways_out": ways_out("stuck"),
    }


def test_a_stuck_flag_stops_when_its_wake_was_queued(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "93", state="building", stuck=1, stuck_reason="job-timeout")
        add_job(connection, "w-93", action="stuck", recipient="grok", ticket_id="93", created_at=NOW - 70)
        connection.execute(
            "UPDATE ticket_state SET stuck_wake_job_id = 'w-93' WHERE repo = ? AND ticket_id = '93'", (CLEFFY,))
    stopped = records(db, log, tmp_path)["maxharris1/cleffy#93"]["stopped"]
    assert (stopped["state"], stopped["at"], stopped["stopped_in"]) == ("stuck", NOW - 70, "building")


def test_a_stop_with_no_transition_time_has_no_stop_time(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "94", state="blocked")
    stopped = records(db, log, tmp_path)["maxharris1/cleffy#94"]["stopped"]
    assert (stopped["state"], stopped["at"]) == ("blocked", None)
    assert stopped["ways_out"] == ways_out("blocked")


def test_the_stuck_reason_is_capped_like_the_snapshots(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "95", state="stuck", stuck=1, stuck_reason="why " * 100)
    status, published, _ = publish(db, log, tmp_path / "out")
    row = next(row for row in status["stuck"]["tickets"] if row["ticket_id"] == "95")
    assert published["tickets"]["maxharris1/cleffy#95"]["stopped"]["reason"] == row["reason"]
    assert len(row["reason"]) == 200




def test_a_tickets_live_jobs_are_attached_running_newest_first(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "96", state="review", review_round=1)
        add_job(connection, "ra-older", action="review", recipient="codex", state="running",
                ticket_id="96", created_at=NOW - 3000)
        add_job(connection, "rb-newer", action="review", recipient="claude", state="running",
                ticket_id="96", created_at=NOW - 1000)
        add_job(connection, "qc-next1", action="fix", state="queued", ticket_id="96",
                created_at=NOW - 200)
    add_telemetry(db, "rb-newer", harness="claude", current_task="Read the diff",
                  plan=json.dumps([{"text": "Read the diff", "status": "in_progress"}]))
    status, published, _ = publish(db, log, tmp_path / "out")
    jobs = published["tickets"]["maxharris1/cleffy#96"]["jobs"]
    assert [job["job"] for job in jobs] == ["rb-newer", "ra-older", "qc-next1"]
    rows = {row["job"]: row for row in [*status["running"], *status["queued"]]}
    for job in jobs:
        assert job == {key: value for key, value in rows[job["job"]].items() if key != "flow"}
    assert jobs[0]["telemetry"]["current_task"] == "Read the diff"
    assert jobs[0]["telemetry"]["plan"] == [{"text": "Read the diff", "status": "in_progress"}]


def test_an_untracked_tickets_job_gets_no_record(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    tickets = records(db, log, tmp_path)
    assert not any(record["ticket_id"] == "14" for record in tickets.values())




def test_the_writer_publishes_tickets_json_after_status_and_the_pages(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    out = tmp_path / "out"
    paths = write_snapshot_pages(out, collect_snapshot(db, log, now=NOW))
    names = ("status.json", *PAGES, "tickets.json")
    assert paths == tuple(out / name for name in names)
    assert sorted(path.name for path in out.iterdir()) == sorted(names)


def test_a_failed_tickets_json_rename_rolls_back_the_whole_set(
    tmp_path: Path, monkeypatch
) -> None:
    db, log = seeded(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW)
    out = tmp_path / "out"
    out.mkdir()
    old = {"status.json": "old status", "index.html": "old index", "tickets.json": "old tickets"}
    for name, text in old.items():
        (out / name).write_text(text, encoding="utf-8")
    _fail_replacing(monkeypatch, "tickets.json")
    with pytest.raises(OSError, match="disk full"):
        write_snapshot_pages(out, snapshot)
    assert {path.name: path.read_text(encoding="utf-8") for path in out.iterdir()} == old


def test_the_push_renames_tickets_json_with_the_rest_in_one_step(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    paths = write_snapshot_pages(tmp_path / "out", collect_snapshot(db, log, now=NOW))
    commands = SshTransport("pc-wsl-bots", "factory-dashboard").commands(paths)
    names = ("status.json", *PAGES, "tickets.json")
    assert commands[-1][-1] == "cd -- factory-dashboard && " + " && ".join(
        f"mv -f -- .{name}.push-tmp {name}" for name in names
    )
    assert len(commands) == len(names) + 1




def test_records_carry_only_the_named_fields(tmp_path: Path) -> None:
    db, log = _shipped(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "90", state="stuck", stuck=1, stuck_reason="gaps-twice")
        walk(connection, "90", ["intake", "planning", "stuck"], NOW - 800)
        add_ticket(connection, CLEFFY, "92", state="capped")
        walk(connection, "92", ["intake", "building", "review", "capped"], NOW - 900)
    status, published, _ = publish(db, log, tmp_path / "out")
    assert set(published) == {"schema", "generated_at_epoch", "tickets"}
    rows = {row["job"]: row for row in [*status["running"], *status["queued"]]}
    seen = {"shipped": 0, "stopped": 0, "ways_out": 0, "jobs": 0}
    for record in published["tickets"].values():
        assert set(record) == RECORD_KEYS
        for visit in record["visits"]:
            assert set(visit) == VISIT_KEYS
        if record["shipped"] is not None:
            seen["shipped"] += 1
            assert set(record["shipped"]) == SHIPPED_KEYS
        if record["stopped"] is not None:
            seen["stopped"] += 1
            assert set(record["stopped"]) == STOPPED_KEYS
            for way in record["stopped"]["ways_out"]:
                seen["ways_out"] += 1
                assert set(way) == WAY_OUT_KEYS
        for job in record["jobs"]:
            seen["jobs"] += 1
            assert job == {key: value for key, value in rows[job["job"]].items() if key != "flow"}
    assert all(seen.values()), seen


def test_status_json_is_unchanged_by_the_records(tmp_path: Path) -> None:
    db, log = _mixed(tmp_path)
    snapshot = collect_snapshot(db, log, now=NOW)
    out = tmp_path / "out"
    write_snapshot_pages(out, snapshot)
    written = (out / "status.json").read_text(encoding="utf-8")
    without = {key: value for key, value in snapshot.items() if key != "ticket_records"}
    assert written == json.dumps(without, indent=2, sort_keys=True, allow_nan=False) + "\n"
    assert "ticket_records" not in json.loads(written)
    assert "ticket_records" in snapshot




def _hostile(tmp_path: Path) -> tuple[Path, Path]:
    db, log = seeded(tmp_path)
    repo = f"evil/{MARKUP}{GH_TOKEN}"
    ticket = f"{BREAKOUT}7"
    with sqlite3.connect(db) as connection:
        add_ticket(connection, repo, ticket, state=MARKUP, stuck=1,
                   stuck_reason=f"{MARKUP} {BREAKOUT} {GH_TOKEN} {CRED_URL}")
        transition(connection, ticket, None, "intake", NOW - 900, repo=repo)
        transition(connection, ticket, "intake", BREAKOUT, NOW - 800, repo=repo)
        add_job(connection, "x-hostile", recipient=MARKUP, state="running", repo=repo,
                ticket_id=ticket, created_at=NOW - 700)
        add_ticket(connection, CLEFFY, "97", state="merged")
        walk(connection, "97", ["intake", "ship", "merged"], NOW - 600)
        add_job(connection, "x-shipped", state="done", ticket_id="97", created_at=NOW - 550)
    add_telemetry(db, "x-hostile", harness=BREAKOUT, repo=GH_TOKEN,
                  current_task=f"{MARKUP} {GH_TOKEN}")
    add_telemetry(db, "x-shipped", harness=MARKUP, cost_usd=0.1)
    return db, log


def test_no_markup_or_secret_reaches_tickets_json(tmp_path: Path) -> None:
    db, log = _hostile(tmp_path)
    _, published, raw = publish(db, log, tmp_path / "out")
    assert "<" not in raw and ">" not in raw and "&" not in raw
    assert GH_TOKEN not in raw and "user:pass" not in raw
    assert not re.search(r"gh[pousr]_[A-Za-z0-9_]{16,}", raw)
    hostile = next(r for r in published["tickets"].values() if r["ticket_id"].endswith("7")
                   and r["repo"].startswith("evil/"))
    assert hostile["state"] is None
    assert hostile["stopped"]["state"] == "other"
    assert {visit["state"] for visit in hostile["visits"]} <= {"intake", "other"}
    assert hostile["jobs"][0]["recipient"] == "other"
    assert hostile["jobs"][0]["telemetry"]["harness"] == "other"
    for value in strings(published):
        assert value == redact(value)


def test_tickets_json_escapes_markup_characters(tmp_path: Path) -> None:
    db, log = _hostile(tmp_path)
    _, published, raw = publish(db, log, tmp_path / "out")
    assert "\\u003c" in raw
    assert any(MARKUP in value for value in strings(published))




def test_a_database_without_transitions_or_telemetry_still_has_records(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, CLEFFY, "98", state="merged", review_round=1)
        add_job(connection, "n-build", state="done", ticket_id="98", created_at=NOW - 900)
        connection.execute("DROP TABLE ticket_transitions")
    tickets = records(db, log, tmp_path)
    shipped = tickets["maxharris1/cleffy#98"]["shipped"]
    assert shipped["jobs"] == 1
    assert (shipped["tokens_in"], shipped["tokens_out"], shipped["cost_usd"]) == (None, None, None)
    assert shipped["at"] is None and shipped["intake_to_ship_seconds"] is None
    assert tickets["maxharris1/cleffy#98"]["all_durations_known"] is False
    assert tickets["aut0-code/aut0#AUT-7"]["stopped"]["at"] is None


def test_an_empty_database_has_no_records(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    assert records(db, tmp_path / "poller.log", tmp_path) == {}


def test_a_database_without_ticket_tables_has_no_records(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE tracked_tickets")
        connection.execute("DROP TABLE ticket_state")
    assert records(db, log, tmp_path) == {}


def test_a_database_with_only_ticket_tables_still_has_records(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE tracked_tickets (repo TEXT, ticket_id TEXT, tracker TEXT, tracked_at REAL)"
        )
        connection.execute(
            "CREATE TABLE ticket_state (repo TEXT, ticket_id TEXT, workflow_state TEXT, "
            "review_round INTEGER, gate_count INTEGER, stuck INTEGER, stuck_reason TEXT, "
            "paused_action TEXT)"
        )
        connection.execute(f"INSERT INTO tracked_tickets VALUES ('{CLEFFY}', '1', 'github', NULL)")
        connection.execute(
            f"INSERT INTO ticket_state VALUES ('{CLEFFY}', '1', 'done', 0, 0, 0, NULL, NULL)"
        )
    record = records(db, tmp_path / "poller.log", tmp_path)["maxharris1/cleffy#1"]
    assert record["shipped"] == {
        "at": None, "how": "merged", "intake_to_ship_seconds": None, "revise_rounds": 0,
        "jobs": 0, "tokens_in": None, "tokens_out": None, "cost_usd": None,
    }



PATH = ["intake", "planning", "plan-check", "building", "review", "fixing", "review", "fixing",
        "review", "ship", "merged", "done"]


def synthetic(path: Path, count: int) -> Path:
    """`count` shipped tickets, each with 12 recorded visits and 6 metered jobs."""
    make_db(path)
    tickets, transitions, jobs = [], [], []
    for n in range(count):
        ticket = str(1000 + n)
        tickets.append((CLEFFY, ticket))
        previous = None
        for i, state in enumerate(PATH):
            transitions.append((CLEFFY, ticket, previous, state, NOW - 90_000 + 600 * i))
            previous = state
        for j, action in enumerate(("build", "review", "fix", "review", "fix", "review")):
            jobs.append((f"{ticket}-{j}", CLEFFY, ticket, action, NOW - 89_000 + 900 * j))
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO tracked_tickets VALUES (?, ?, 'github', ?)",
            [(repo, ticket, NOW - 95_000) for repo, ticket in tickets],
        )
        connection.executemany(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state, review_round) "
            "VALUES (?, ?, 'done', 2)",
            tickets,
        )
        connection.executemany(
            "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
            "VALUES (?, ?, ?, ?, ?, 'poller')",
            transitions,
        )
        connection.executemany(
            "INSERT INTO jobs (id, dedupe_key, repo, ticket_id, action, recipient, "
            "source_comment_id, timeout_seconds, generation, attempt, state, created_at, "
            "last_activity_at) VALUES (:id, :id, :repo, :ticket, :action, 'claude', :id, 5400, "
            "1, 1, 'done', :at, :at)",
            [dict(zip(("id", "repo", "ticket", "action", "at"), job)) for job in jobs],
        )
        telemetry.ensure_table(connection)
        connection.executemany(
            "INSERT INTO job_telemetry (job_id, bot, harness, repo, started_at, updated_at, "
            "elapsed_seconds, events, turns, tool_calls, files_touched, commands, test_runs, "
            "tokens_in, tokens_cached, tokens_out, cost_usd, steps) VALUES "
            "(?, 'claude-bot', 'claude', ?, ?, ?, 600, 40, 3, '{}', 2, 5, 1, 120000, 90000, "
            "4000, 1.25, '[]')",
            [(job_id, repo, at, at + 600) for job_id, repo, _, _, at in jobs],
        )
    return path


@pytest.mark.parametrize("count", [222, 1000])
def test_tickets_json_size_is_bounded(tmp_path: Path, count: int) -> None:
    db = synthetic(tmp_path / "orch.db", count)
    out = tmp_path / "out"
    started = time.monotonic()
    write_snapshot_pages(out, collect_snapshot(db, tmp_path / "poller.log", now=NOW))
    seconds = time.monotonic() - started
    size = (out / "tickets.json").stat().st_size
    status = (out / "status.json").stat().st_size
    print(f"\n{count} tickets: tickets.json {size} bytes, status.json {status} bytes, {seconds:.2f} s")
    assert len(json.loads((out / "tickets.json").read_text())["tickets"]) == count
    assert size < 8 * 1024 * 1024
