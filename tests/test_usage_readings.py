"""Subscription usage readings (issue #151): parse, store, route, snapshot.

The Claude fixture is a real `rate_limit_event` from Claude Code 2.1.322
(`claude -p --output-format stream-json --verbose`, claude-bot on mac,
2026-11-14), with its uuid and session_id zeroed. Its event carried only the
7-day window, so the 5-hour window reads as no reading.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import math
import os
import sqlite3
from pathlib import Path

import pytest

from lib import telemetry, usage_readings
from lib.dashboard import collect_snapshot
from lib.dashboard_usage import read_usage
from lib.machines_yml import Device, MachinesConfig
from lib.telemetry import Scrubber, Session, Summary
from lib.usage import reserve_check
from lib.usage_readings import Reading, TelemetryUsage, parse_claude
from tests.test_dashboard import make_db

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "usage" / "claude-2.1.322-rate_limit_event.jsonl"
CAPTURED = 1794673532.0  # when the fixture's event was emitted
SEVEN_DAY_RESET = 1795204800
FIVE_HOUR_RESET = int(CAPTURED) + 3 * 3600
BOX = MachinesConfig(
    (Device("box", 3, {"claude": "claude-bot", "codex": "codex-bot", "cursor": "cursor-bot"}, local=True),)
)


def fixture_event() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def event(info: dict) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": info}


# --- parsing --------------------------------------------------------------------------


def test_the_real_claude_event_gives_its_seven_day_window_only() -> None:
    assert parse_claude(fixture_event(), CAPTURED) == [
        Reading("seven_day", 0.23, SEVEN_DAY_RESET, "allowed", CAPTURED)
    ]


def test_without_unified_windows_the_rate_limit_type_names_the_window() -> None:
    readings = parse_claude(
        event({"status": "allowed_warning", "resetsAt": FIVE_HOUR_RESET, "rateLimitType": "five_hour",
               "utilization": 0.8}),
        CAPTURED,
    )
    assert readings == [Reading("five_hour", 0.8, FIVE_HOUR_RESET, "allowed_warning", CAPTURED)]


def test_a_missing_utilization_is_unknown_never_zero() -> None:
    readings = parse_claude(
        event({"status": "allowed", "resetsAt": FIVE_HOUR_RESET, "rateLimitType": "five_hour"}),
        CAPTURED,
    )
    assert readings == [Reading("five_hour", None, FIVE_HOUR_RESET, "allowed", CAPTURED)]


@pytest.mark.parametrize(
    "window",
    [
        {"utilization": True, "resetsAt": FIVE_HOUR_RESET, "status": "allowed"},
        {"utilization": math.nan, "resetsAt": FIVE_HOUR_RESET, "status": "allowed"},
        {"utilization": 1.5, "resetsAt": FIVE_HOUR_RESET, "status": "allowed"},
        {"utilization": "0.4", "resetsAt": FIVE_HOUR_RESET, "status": "allowed"},
    ],
)
def test_an_invalid_utilization_is_dropped_to_unknown(window: dict) -> None:
    [reading] = parse_claude(event({"unifiedWindows": {"five_hour": window}}), CAPTURED)
    assert reading.used is None


@pytest.mark.parametrize(
    "unified",
    [
        {"five_hour": {"utilization": 0.1, "resetsAt": int(CAPTURED) + 90 * 86400}},
        {"five_hour": {"utilization": 0.1, "resetsAt": FIVE_HOUR_RESET + 0.5}},
        {"five_hour": {"utilization": 0.1, "resetsAt": True}},
        {"five_hour": {"utilization": 0.1, "resetsAt": str(FIVE_HOUR_RESET)}},
        {"seven_day_opus": {"utilization": 0.1, "resetsAt": FIVE_HOUR_RESET}},
        {"five_hour": "0.1"},
    ],
)
def test_a_bad_reset_or_an_unknown_window_drops_the_reading(unified: dict) -> None:
    assert parse_claude(event({"unifiedWindows": unified}), CAPTURED) == []


def test_an_unknown_status_word_is_not_kept() -> None:
    [reading] = parse_claude(
        event({"unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": FIVE_HOUR_RESET,
                                                "status": "token sk-abc"}}}),
        CAPTURED,
    )
    assert reading.status is None


def test_the_summary_keeps_the_latest_reading_per_window_for_claude_only() -> None:
    summary = Summary("claude", Scrubber({}))
    summary.feed(event({"unifiedWindows": {"seven_day": {"utilization": 0.1, "resetsAt": SEVEN_DAY_RESET}}}),
                 CAPTURED - 60)
    summary.feed(fixture_event(), CAPTURED)
    assert summary.usage == {"seven_day": Reading("seven_day", 0.23, SEVEN_DAY_RESET, "allowed", CAPTURED)}
    codex = Summary("codex", Scrubber({}))
    codex.feed(fixture_event(), CAPTURED)
    assert codex.usage == {}


# --- storing on the telemetry flush ------------------------------------------------------


def _session(db: Path, *, job_id: str = "job-1", device: str = "box", at: float = CAPTURED) -> Session:
    return Session(
        connect=lambda: sqlite3.connect(db),
        job_id=job_id,
        bot="claude-bot",
        repo="acme/widgets",
        harness="claude",
        argv=["claude"],
        raw_log=None,
        scrub=Scrubber({}),
        log=lambda _message: None,
        stdout=io.BytesIO(),
        clock=lambda: at,
        device=device,
    )


def _stored(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT harness, bot, device, window, used, resets_at, status, observed_at, job_id"
            " FROM usage_readings ORDER BY device, window"
        ).fetchall()


def test_a_session_stores_its_readings_with_its_telemetry_row_on_flush(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db, device="pc")
    session.consume(io.BytesIO(FIXTURE.read_bytes()))
    session.flush()
    assert _stored(db) == [
        ("claude", "claude-bot", "pc", "seven_day", 0.23, SEVEN_DAY_RESET, "allowed", CAPTURED, "job-1")
    ]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM job_telemetry").fetchone() == (1,)


def test_an_older_reading_never_replaces_a_newer_one(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    newer = _session(db, job_id="job-new", at=CAPTURED)
    newer.consume(io.BytesIO(FIXTURE.read_bytes()))
    newer.finish(0)
    older = _session(db, job_id="job-old", at=CAPTURED - 600)
    older.consume(io.BytesIO(json.dumps(event({"unifiedWindows": {"seven_day": {
        "utilization": 0.05, "resetsAt": SEVEN_DAY_RESET}}})).encode() + b"\n"))
    older.finish(0)
    [row] = _stored(db)
    assert row[4] == 0.23 and row[-1] == "job-new"


def test_a_run_without_rate_limit_events_stores_no_reading(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    session = _session(db)
    session.consume(io.BytesIO(b'{"type":"system","subtype":"init"}\n'))
    session.finish(0)
    with sqlite3.connect(db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "usage_readings" not in tables


def test_telemetry_off_means_no_session_and_so_no_readings(tmp_path: Path) -> None:
    assert telemetry.start_session(
        connect=lambda: sqlite3.connect(tmp_path / "orch.db"),
        database=tmp_path / "orch.db",
        job_id="job-1",
        bot="claude-bot",
        repo="acme/widgets",
        command=["claude", "-p", "x"],
        log=lambda _message: None,
        environ={"ORCH_TELEMETRY": "off"},
        device="box",
    ) is None


# --- the usage source for routing ---------------------------------------------------------


def _with_rows(db: Path, *rows: tuple[str, float | None, int, float]) -> None:
    with sqlite3.connect(db) as connection:
        usage_readings.store(
            connection, harness="claude", bot="claude-bot", device="box", job_id="job-1",
            readings=[Reading(window, used, resets, "allowed", observed) for window, used, resets, observed in rows],
        )


def _source(db: Path, now: float) -> TelemetryUsage:
    bots = {"claude": "claude-bot", "codex": "codex-bot"}
    return TelemetryUsage(lambda: sqlite3.connect(db), bot_for=bots.get, device="box", clock=lambda: now)


def test_one_fresh_window_is_an_unverified_report_and_never_blocks(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("seven_day", 0.9, SEVEN_DAY_RESET, CAPTURED))
    report = _source(db, CAPTURED + 60).read("claude")
    assert report is not None and report.verified is False
    assert report.remaining == pytest.approx(0.1)
    assert reserve_check("claude", _source(db, CAPTURED + 60)).reason == "no-verified-source"


def test_both_fresh_windows_are_verified_and_the_reserve_applies(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("seven_day", 0.3, SEVEN_DAY_RESET, CAPTURED), ("five_hour", 0.85, FIVE_HOUR_RESET, CAPTURED))
    report = _source(db, CAPTURED + 60).read("claude")
    assert report is not None and report.verified is True
    assert report.remaining == pytest.approx(0.15)
    check = reserve_check("claude", _source(db, CAPTURED + 60))
    assert (check.start, check.reason) == (False, "reroute")


def test_stale_missing_or_unsourced_readings_give_no_report(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    assert _source(db, CAPTURED).read("claude") is None  # no table
    _with_rows(db, ("five_hour", 0.5, FIVE_HOUR_RESET, CAPTURED))
    assert _source(db, FIVE_HOUR_RESET + 1).read("claude") is None  # the window reset
    assert _source(db, CAPTURED + usage_readings.STALE_SECONDS + 1).read("claude") is None
    assert _source(db, CAPTURED).read("codex") is None
    assert _source(db, CAPTURED).read("cursor") is None


# --- the snapshot ------------------------------------------------------------------------


def _bots(usage: dict) -> dict[tuple[str, str], dict]:
    return {(entry["device"], entry["harness"]): entry for entry in usage["bots"]}


def test_snapshot_usage_has_one_entry_per_bot_account(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        usage_readings.store(connection, harness="claude", bot="claude-bot", device="box", job_id="job-1",
                             readings=parse_claude(fixture_event(), CAPTURED))
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n  - name: box\n    local: true\n    slot_max: 3\n    harnesses:\n"
        "      claude: claude-bot\n      codex: codex-bot\n      cursor: cursor-bot\n",
        encoding="utf-8",
    )
    snapshot = collect_snapshot(db, tmp_path / "poll.log", now=CAPTURED + 120, machines_path=machines)
    usage = snapshot["usage"]
    assert usage["state"] == "ok"
    bots = _bots(usage)
    assert set(bots) == {("box", "claude"), ("box", "codex"), ("box", "cursor")}
    claude = bots[("box", "claude")]
    assert claude == {
        "bot": "claude-bot",
        "harness": "claude",
        "device": "box",
        "state": "ok",
        "five_hour": {"used": None, "resets_at": None, "observed_at": None, "status": None,
                      "state": "no reading"},
        "seven_day": {"used": 0.23, "resets_at": SEVEN_DAY_RESET, "observed_at": CAPTURED,
                      "status": "allowed", "state": "ok"},
        "headroom": 0.77,
        "observed_at": CAPTURED,
        "age_seconds": 120,
    }
    assert bots[("box", "codex")]["state"] == "no reading"
    assert bots[("box", "codex")]["headroom"] is None
    assert bots[("box", "cursor")]["state"] == "not tracked"
    json.dumps(snapshot, allow_nan=False)


def test_a_database_without_the_table_reads_as_no_telemetry(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "orch.db")
    usage = read_usage(connection, CAPTURED, BOX)
    assert usage["state"] == "no telemetry"
    assert {e["harness"]: e["state"] for e in usage["bots"]} == {
        "claude": "no telemetry", "codex": "no reading", "cursor": "not tracked"}


def test_a_reset_or_old_reading_is_stale_with_no_used_value(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("five_hour", 0.6, FIVE_HOUR_RESET, CAPTURED), ("seven_day", 0.2, SEVEN_DAY_RESET, CAPTURED))
    with sqlite3.connect(db) as connection:
        reset = _bots(read_usage(connection, FIVE_HOUR_RESET + 1, BOX))[("box", "claude")]
        old = _bots(read_usage(connection, CAPTURED + usage_readings.STALE_SECONDS + 1, BOX))[("box", "claude")]
    assert reset["five_hour"]["state"] == "stale" and reset["five_hour"]["used"] is None
    assert reset["seven_day"]["state"] == "ok" and reset["headroom"] == 0.8
    assert reset["state"] == "ok"
    assert old["state"] == "stale" and old["headroom"] is None
    assert old["five_hour"]["used"] is None and old["seven_day"]["used"] is None


def test_headroom_is_the_tighter_fresh_window(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    _with_rows(db, ("five_hour", 0.6, FIVE_HOUR_RESET, CAPTURED), ("seven_day", 0.2, SEVEN_DAY_RESET, CAPTURED))
    with sqlite3.connect(db) as connection:
        entry = _bots(read_usage(connection, CAPTURED + 1, BOX))[("box", "claude")]
    assert entry["headroom"] == 0.4


def test_bad_rows_never_reach_the_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        usage_readings.ensure_table(connection)
        connection.executemany(
            "INSERT INTO usage_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("claude", "claude-bot", "box", "five_hour", 7.0, FIVE_HOUR_RESET, "rejected", CAPTURED, "j"),
                ("claude", "claude-bot", "box", "seven_day", 0.2, "soon", "allowed", CAPTURED, "j"),
                ("claude", "bot with spaces", "box", "five_hour", 0.2, FIVE_HOUR_RESET, "allowed", CAPTURED, "j"),
                ("other", "x", "box", "five_hour", 0.2, FIVE_HOUR_RESET, "allowed", CAPTURED, "j"),
            ],
        )
        usage = read_usage(connection, CAPTURED + 1, BOX)
    assert [(e["device"], e["harness"], e["bot"]) for e in usage["bots"]] == [
        ("box", "claude", "claude-bot"), ("box", "codex", "codex-bot"), ("box", "cursor", "cursor-bot")]
    claude = usage["bots"][0]
    assert claude["five_hour"]["used"] is None and claude["five_hour"]["state"] == "no reading"
    assert claude["seven_day"]["state"] == "no reading" and claude["state"] == "no reading"


# --- no credential file, no process, no network --------------------------------------------


def test_reading_usage_opens_no_file_but_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    real_open, real_os_open, real_path_open = builtins.open, os.open, Path.open

    def record_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def record_os_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    def record_path_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_path_open(self, *args, **kwargs)

    payload = FIXTURE.read_bytes()
    monkeypatch.setattr(builtins, "open", record_open)
    monkeypatch.setattr(io, "open", record_open)
    monkeypatch.setattr(os, "open", record_os_open)
    monkeypatch.setattr(Path, "open", record_path_open)
    db = tmp_path / "orch.db"
    session = _session(db)
    session.consume(io.BytesIO(payload))
    session.finish(0)
    assert _source(db, CAPTURED + 1).read("claude") is not None
    with sqlite3.connect(db) as connection:
        read_usage(connection, CAPTURED + 1, BOX)
    assert all(path.startswith(str(tmp_path)) for path in opened), opened


@pytest.mark.parametrize("module", ["lib/usage_readings.py", "lib/dashboard_usage.py"])
def test_usage_modules_import_no_process_or_network_library(module: str) -> None:
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
             for alias in node.names}
    names |= {node.module.split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom) and node.module}
    assert not names & {"subprocess", "urllib", "http", "requests", "socket", "os", "pathlib"}
