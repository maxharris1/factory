import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import (
    account_doctor, dashboard_cli, device_pool, device_sync, feeder, lane_limit, linear_webhook,
    qa_evidence_push, telemetry,
)
from lib.dashboard import HANDED_OFF_EXTERNALLY, _age, _read_db, collect_snapshot
from tests.test_dashboard import NOW, add_job, add_ticket, seeded
from tests.test_dashboard_accounts import world
from tests.test_dashboard_poll_seconds import _orch

STAMP = "2026-11-26 22:15:58"
STAMP_EPOCH = datetime(2026, 11, 26, 22, 15, 58, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(
    "then",
    [
        STAMP_EPOCH,
        int(STAMP_EPOCH),
        str(STAMP_EPOCH),
        STAMP,
        "2026-11-26T22:15:58",
        "2026-11-26T22:15:58Z",
        "2026-11-26T17:15:58-05:00",
    ],
)
def test_age_reads_an_epoch_or_a_datetime_string(then: object) -> None:
    assert _age(STAMP_EPOCH + 90, then) == 90


def test_age_of_nothing_is_none() -> None:
    assert _age(NOW, None) is None


def test_age_still_refuses_a_value_that_is_no_time() -> None:
    with pytest.raises(ValueError):
        _age(NOW, "soon")


def _cleffy_213(tmp_path: Path, **stamps: str) -> tuple[Path, Path]:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(
            connection, "2b81e0489a1349e78012ee59ef76692c", action="awaiting-ok",
            state="done", repo="maxharris1/Cleffy", ticket_id="213",
        )
        connection.execute(
            "UPDATE jobs SET finished_at = ? WHERE id = '2b81e0489a1349e78012ee59ef76692c'", (STAMP,)
        )
        for column, value in stamps.items():
            connection.execute(f"UPDATE jobs SET {column} = ?", (value,))
    return db, log


def test_a_snapshot_reads_a_finished_at_datetime_string(tmp_path: Path) -> None:
    db, log = _cleffy_213(tmp_path)
    snapshot = collect_snapshot(db, log, now=STAMP_EPOCH + 60)
    assert snapshot["queue"]["totals"]["queued"] == 2


def test_a_snapshot_reads_every_jobs_clock_as_a_datetime_string(tmp_path: Path) -> None:
    db, log = _cleffy_213(tmp_path, created_at="2026-11-26 22:05:58", last_activity_at=STAMP)
    snapshot = collect_snapshot(db, log, now=STAMP_EPOCH + 60)
    running = snapshot["running"]
    assert running and all(job["heartbeat_age_seconds"] == 60 for job in running)


class _Capture:
    def __init__(self) -> None:
        self.sent: list[tuple[Path, ...]] = []

    def send(self, paths) -> None:
        self.sent.append(tuple(paths))


def _push(tmp_path: Path, db: Path, log: Path, monkeypatch) -> tuple[int, _Capture]:
    capture = _Capture()
    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(dashboard_cli, "transport_for", lambda config: capture)
    code = dashboard_cli.main(
        ["push", "--once", "--db", str(db), "--log", str(log),
         "--out", str(tmp_path / "site"), "--orch", str(_orch(tmp_path, 60)),
         "--machines", str(tmp_path / "no-machines.yml")]
    )
    return code, capture


def test_a_push_publishes_with_cleffy_213s_finished_at_left_as_is(
    tmp_path: Path, monkeypatch
) -> None:
    db, log = _cleffy_213(tmp_path)
    code, capture = _push(tmp_path, db, log, monkeypatch)
    assert code == 0
    assert len(capture.sent) == 1
    assert all(path.is_file() for path in capture.sent[0])
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT finished_at FROM jobs WHERE id = '2b81e0489a1349e78012ee59ef76692c'"
        ).fetchone() == (STAMP,)


STAMPED_COLUMNS = (
    ("jobs", "created_at"),
    ("jobs", "finished_at"),
    ("jobs", "last_activity_at"),
    ("poll_errors", "recorded_at"),
    ("device_slots", "placed_at"),
    ("ticket_state", "stuck_since"),
)


def _as_datetime_strings(db: Path, *, only: str | None = None, where: str = "1") -> None:
    with sqlite3.connect(db) as connection:
        for table, column in STAMPED_COLUMNS:
            present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column in present and only in (None, table):
                connection.execute(
                    f"UPDATE {table} SET {column} = datetime({column}, 'unixepoch') "
                    f"WHERE {column} IS NOT NULL AND {where}"
                )


def _finish(connection: sqlite3.Connection, job_id: str, at: float) -> None:
    connection.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (at, job_id))


def _relay_world(tmp_path: Path) -> tuple[Path, Path]:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(
            connection, "2b81e0489a1349e78012ee59ef76692c", action="awaiting-ok",
            state="done", repo="maxharris1/Cleffy", ticket_id="213",
        )
        _finish(connection, "2b81e0489a1349e78012ee59ef76692c", STAMP_EPOCH)
        for job_id, ticket_id, finished_at in (
            ("hand-40-a", "40", NOW - 5), ("hand-40-b", "40", NOW - 60), ("hand-41", "41", NOW - 120),
        ):
            add_job(connection, job_id, state="done", ticket_id=ticket_id, handoff_comment_id=f"c-{job_id}")
            _finish(connection, job_id, finished_at)
        add_ticket(connection, "maxharris1/cleffy", "20", state="building", stuck=1, stuck_reason="job-timeout")
        add_job(
            connection, "wake-20", action="stuck", recipient="grok", state="queued",
            ticket_id="20", created_at=NOW - 70,
        )
        connection.execute(
            "UPDATE ticket_state SET stuck_wake_job_id = 'wake-20' WHERE ticket_id = '20'"
        )
        add_ticket(connection, "maxharris1/cleffy", "30", state="building")
        add_job(connection, "plan-30", action="plan", state="done", ticket_id="30", created_at=NOW - 120)
        add_job(connection, "build-30", state="running", ticket_id="30", created_at=NOW - 60)
    return db, log


def _handoff_ages(snapshot: dict) -> list[int]:
    return [item["age_seconds"] for item in snapshot["handoff_savings"]["recent"]]


def _jobs_in_flow(snapshot: dict, ticket: str) -> list[str]:
    visits = snapshot["ticket_records"][f"maxharris1/cleffy#{ticket}"]["visits"]
    return [job["job"] for visit in visits for job in visit["jobs"]]


def test_a_snapshot_is_the_same_when_every_stamp_is_a_datetime_string(tmp_path: Path) -> None:
    db, log = _relay_world(tmp_path)
    as_epochs = collect_snapshot(db, log, now=NOW)
    assert _handoff_ages(as_epochs)[:3] == [5, 60, 120], "handoffs newest first"
    assert _jobs_in_flow(as_epochs, "30") == ["plan-30", "build-30"], "flow in created order"
    _as_datetime_strings(db)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT finished_at FROM jobs WHERE id = '2b81e0489a1349e78012ee59ef76692c'"
        ).fetchone() == (STAMP,), "the Cleffy #213 row as orch.db holds it"
    assert collect_snapshot(db, log, now=NOW) == as_epochs


def test_a_snapshot_orders_handoffs_and_flows_by_instant_across_mixed_stamps(tmp_path: Path) -> None:
    db, log = _relay_world(tmp_path)
    _as_datetime_strings(db, only="jobs", where="id IN ('hand-41', 'plan-30')")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert _handoff_ages(snapshot)[:3] == [5, 60, 120]
    thread = next(item for item in snapshot["relay"]["threads"] if item["ticket_id"] == "40")
    assert [item["age_seconds"] for item in thread["handoffs"]] == [5, 60]
    assert _jobs_in_flow(snapshot, "30") == ["plan-30", "build-30"]


def _stuck_thread(snapshot: dict) -> dict:
    return next(item for item in snapshot["relay"]["threads"] if item["ticket_id"] == "20")


def test_a_stuck_ticket_whose_wake_job_was_stamped_as_a_datetime_string_stays_in_the_relay(
    tmp_path: Path,
) -> None:
    db, log = _relay_world(tmp_path)
    as_epoch = _stuck_thread(collect_snapshot(db, log, now=NOW))
    _as_datetime_strings(db, only="jobs", where="id = 'wake-20'")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert _stuck_thread(snapshot) == as_epoch
    assert as_epoch["now"]["age_seconds"] == 70
    assert snapshot["ticket_records"]["maxharris1/cleffy#20"]["stopped"]["at"] == NOW - 70


def test_poller_activity_reads_a_poll_error_stamped_as_a_datetime_string(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    assert collect_snapshot(db, log, now=NOW)["poller"]["last_activity_age_seconds"] == 10
    _as_datetime_strings(db, only="poll_errors", where="repo = 'poll'")
    poller = collect_snapshot(db, log, now=NOW)["poller"]
    assert poller["last_activity_age_seconds"] == 10
    assert (poller["errors"]["last_hour"], poller["errors"]["last_day"]) == (1, 2)
    assert poller["errors"]["recent"][0]["error"] == "poll:PollCycleError"


def test_a_device_placement_stamped_as_a_datetime_string_keeps_its_running_job(tmp_path: Path) -> None:
    (tmp_path / "epoch").mkdir()
    (tmp_path / "datetime").mkdir()
    control_db, machines = world(tmp_path / "epoch")
    control = collect_snapshot(control_db, tmp_path / "poll.log", now=NOW, machines_path=machines)
    db, machines = world(tmp_path / "datetime")
    _as_datetime_strings(db, only="device_slots", where="job_id = 'job-b-pc'")
    snapshot = collect_snapshot(db, tmp_path / "poll.log", now=NOW, machines_path=machines)
    assert len(snapshot["running"]) == 3
    assert snapshot["running"] == control["running"]


def _subsecond_world(tmp_path: Path) -> tuple[Path, Path]:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET finished_at = NULL")
        for job_id, ticket_id, finished_at in (
            ("older-40", "40", NOW - 4.75),
            ("newer-40", "40", NOW - 4.25),
            *((f"later-{n:02}", "41", NOW - 4 + n / 4) for n in range(11)),
        ):
            add_job(connection, job_id, state="done", ticket_id=ticket_id, handoff_comment_id=f"c-{job_id}")
            _finish(connection, job_id, finished_at)
    return db, log


def _recent_jobs(snapshot: dict) -> list[str]:
    return [item["job"] for item in snapshot["handoff_savings"]["recent"]]


def _relay_jobs(snapshot: dict, ticket: str) -> list[str]:
    thread = next(item for item in snapshot["relay"]["threads"] if item["ticket_id"] == ticket)
    return [item["job"] for item in thread["handoffs"]]


def test_handoffs_order_by_the_subsecond_instant_of_a_datetime_string(tmp_path: Path) -> None:
    db, log = _subsecond_world(tmp_path)
    as_epochs = collect_snapshot(db, log, now=NOW)
    assert _recent_jobs(as_epochs)[-1] == "newer-40", "the twelfth newest handoff"
    assert _relay_jobs(as_epochs, "40") == ["newer-40", "older-40"]
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET finished_at = '2027-01-15 07:59:55.750' WHERE id = 'newer-40'")
    snapshot = collect_snapshot(db, log, now=NOW)
    assert _recent_jobs(snapshot) == _recent_jobs(as_epochs)
    assert _relay_jobs(snapshot, "40") == ["newer-40", "older-40"]


def test_the_queue_lists_jobs_by_the_subsecond_instant_they_were_created(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM jobs WHERE state = 'queued'")
        add_job(connection, "first-50", state="queued", ticket_id="50", created_at="2027-01-15 07:59:29.250")
        add_job(connection, "next-51", state="queued", ticket_id="51", created_at=NOW - 30.25)
    queued = collect_snapshot(db, log, now=NOW)["queued"]
    assert [item["job"] for item in queued] == ["first-50", "next-51"]


def test_read_db_orders_by_instant_on_a_connection_it_did_not_open(tmp_path: Path) -> None:
    db, _ = _cleffy_213(tmp_path, created_at="2026-11-26 22:05:58")
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        queue = _read_db(connection, STAMP_EPOCH + 60)["queue"]
    assert queue["totals"]["queued"] == 2


def _with_webhook_stamps(db: Path, *, found: list[tuple[str, object]], started: object, received: object, since: object) -> None:
    with sqlite3.connect(db) as connection:
        linear_webhook.ensure_tables(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS linear_webhook_polling (id INTEGER PRIMARY KEY, since REAL NOT NULL)"
        )
        connection.execute("INSERT INTO linear_webhook_polling (id, since) VALUES (1, ?)", (since,))
        connection.execute(
            "INSERT INTO linear_webhook_health (id, started_at, last_received_at, last_event) "
            "VALUES (1, ?, ?, 'Comment')",
            (started, received),
        )
        for identifier, found_at in found:
            connection.execute(
                "INSERT INTO linear_webhook_missed (issue_id, identifier, event_at, found_at) "
                "VALUES (?, ?, ?, ?)",
                (f"id-{identifier}", identifier, STAMP_EPOCH - 900, found_at),
            )


def test_a_push_publishes_with_a_missed_delivery_found_at_datetime_string(
    tmp_path: Path, monkeypatch
) -> None:
    db, log = _cleffy_213(tmp_path)
    _with_webhook_stamps(
        db, found=[("AUT-7", "2026-11-26 22:14:58")],
        started=STAMP_EPOCH - 9000, received=STAMP_EPOCH - 600, since=STAMP_EPOCH - 9000,
    )
    code, capture = _push(tmp_path, db, log, monkeypatch)
    assert code == 0
    assert len(capture.sent) == 1


def test_webhook_health_reads_every_clock_as_an_epoch_or_a_datetime_string(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    _with_webhook_stamps(
        db,
        found=[
            ("AUT-1", STAMP_EPOCH - 3600),
            ("AUT-2", "2026-11-26 22:14:58"),
            ("AUT-3", int(STAMP_EPOCH - 120)),
        ],
        started="2026-11-26 19:45:58",
        received="2026-11-26T22:05:58Z",
        since=STAMP,
    )
    with sqlite3.connect(db) as connection:
        health = linear_webhook.read_health(connection)
    assert health is not None
    assert health.started_at == STAMP_EPOCH - 9000
    assert health.last_received_at == STAMP_EPOCH - 600
    assert health.polling_since == STAMP_EPOCH
    assert health.missed == (
        ("AUT-2", STAMP_EPOCH - 60), ("AUT-3", STAMP_EPOCH - 120), ("AUT-1", STAMP_EPOCH - 3600),
    )
    webhook = collect_snapshot(db, log, now=STAMP_EPOCH + 60)["poller"]["linear_webhook"]
    assert webhook["last_received_age_seconds"] == 660
    assert webhook["listener_age_seconds"] == 9060
    assert webhook["warning"] == (
        "Linear webhook missed 3 deliveries in 24h (newest AUT-2, found 2m ago)"
    )


def _with_feeder_stamps(db: Path, polled: object, items: list[tuple[str, object, object]]) -> None:
    with sqlite3.connect(db) as connection:
        feeder.ensure_tables(connection)
        connection.execute("INSERT INTO feeder_poll (id, at) VALUES (1, ?)", (polled,))
        for issue, created, fed in items:
            connection.execute(
                "INSERT INTO feeder_items (repo, issue, tracker, title, harness, state, risk, "
                "issue_created_at, first_seen_at, seen_at, fed_at) "
                "VALUES ('o/r', ?, 'github', 't', 'claude', 'eligible', 'low', ?, ?, ?, ?)",
                (issue, created, polled, polled, fed),
            )


def test_a_push_publishes_with_feeder_clocks_as_datetime_strings(tmp_path: Path, monkeypatch) -> None:
    db, log = _cleffy_213(tmp_path)
    _with_feeder_stamps(db, STAMP, [("7", "2026-11-20 10:00:00", None)])
    code, capture = _push(tmp_path, db, log, monkeypatch)
    assert code == 0
    assert len(capture.sent) == 1


@pytest.mark.parametrize("polled", [STAMP, STAMP_EPOCH, int(STAMP_EPOCH)])
def test_feeder_items_read_every_clock_as_an_epoch_or_a_datetime_string(
    tmp_path: Path, polled: object
) -> None:
    db, log = seeded(tmp_path)
    _with_feeder_stamps(db, polled, [
        ("1", "2026-11-21 10:00:00", None),
        ("2", STAMP_EPOCH - 7 * 86400, "2026-11-26 22:14:58"),
        ("3", int(STAMP_EPOCH - 3 * 86400), None),
    ])
    with sqlite3.connect(db) as connection:
        assert feeder.last_feeder_poll(connection) == STAMP_EPOCH
    snapshot = collect_snapshot(db, log, now=STAMP_EPOCH + 60)["feeder"]
    assert snapshot["eligible"] == 3
    assert [(item["issue"], item["fed_age_seconds"]) for item in snapshot["items"]] == [
        ("2", 120), ("1", None), ("3", None),
    ]


def test_account_loads_read_placed_at_as_an_epoch_or_a_datetime_string(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS account_placements "
            "(job_id TEXT NOT NULL, account TEXT NOT NULL, harness TEXT NOT NULL, "
            "device TEXT NOT NULL, placed_at REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO account_placements VALUES (?, ?, 'claude', 'box', ?)",
            [
                ("j1", "a", STAMP_EPOCH - 120),
                ("j2", "a", STAMP),
                ("j3", "b", int(STAMP_EPOCH - 60)),
                ("j4", "b", "2026-11-01 00:00:00"),
            ],
        )
        loads = device_pool.account_loads(connection, None, STAMP_EPOCH + 60)
    assert loads.recent_placements == {"a": 2, "b": 1}
    assert {account: at for account, (at, _rowid) in loads.last_placed_at.items()} == {
        "a": STAMP_EPOCH, "b": STAMP_EPOCH - 60,
    }


LIMIT_UNTIL = 2140000000


def _with_limit(db: Path, table: str, limited_at: object, until: object = LIMIT_UNTIL) -> None:
    with sqlite3.connect(db) as connection:
        if table == lane_limit.ACCOUNT_TABLE:
            connection.execute(lane_limit.ACCOUNT_DDL)
            connection.execute(
                f"INSERT INTO {table} VALUES ('acct', 'codex', 'codex-bot', 'codex', ?, ?, 'stream', 'j')",
                (limited_at, until),
            )
        else:
            connection.execute(lane_limit.DDL)
            connection.execute(
                f"INSERT INTO {table} VALUES ('codex', 'codex', 'codex-bot', ?, ?, 'stream', 'j')",
                (limited_at, until),
            )


@pytest.mark.parametrize("table", [lane_limit.ACCOUNT_TABLE, lane_limit.TABLE])
def test_a_push_publishes_with_an_active_limit_limited_at_a_datetime_string(
    tmp_path: Path, monkeypatch, table: str
) -> None:
    sent = {}
    for limited_at in (STAMP_EPOCH, STAMP):
        run = tmp_path / str(limited_at)
        run.mkdir()
        db, log = _cleffy_213(run)
        _with_limit(db, table, limited_at)
        code, capture = _push(run, db, log, monkeypatch)
        assert code == 0
        assert len(capture.sent) == 1
        sent[limited_at] = sorted(path.name for path in capture.sent[0])
    assert len(sent[STAMP]) == 14
    assert sent[STAMP] == sent[STAMP_EPOCH]


LIMIT_STAMPS = [
    (STAMP_EPOCH, LIMIT_UNTIL),
    (int(STAMP_EPOCH), str(LIMIT_UNTIL)),
    (STAMP, LIMIT_UNTIL),
    (STAMP, "2037-10-24 12:26:40"),
]


@pytest.mark.parametrize(("limited_at", "until"), LIMIT_STAMPS)
def test_account_limits_read_limited_at_and_until_as_an_epoch_or_a_datetime_string(
    tmp_path: Path, limited_at: object, until: object
) -> None:
    db, _log = seeded(tmp_path)
    _with_limit(db, lane_limit.ACCOUNT_TABLE, limited_at, until)
    with sqlite3.connect(db) as connection:
        limits = lane_limit.account_limits(connection, STAMP_EPOCH + 60)
    assert {account: (limit.limited_at, limit.until) for account, limit in limits.items()} == {
        "acct": (STAMP_EPOCH, LIMIT_UNTIL),
    }


@pytest.mark.parametrize(("limited_at", "until"), LIMIT_STAMPS)
def test_legacy_lane_limits_read_limited_at_and_until_as_an_epoch_or_a_datetime_string(
    tmp_path: Path, limited_at: object, until: object
) -> None:
    db, _log = seeded(tmp_path)
    _with_limit(db, lane_limit.TABLE, limited_at, until)
    with sqlite3.connect(db) as connection:
        limits = lane_limit.all_active(connection, STAMP_EPOCH + 60)
    assert [(limit.recipient, limit.limited_at, limit.until) for limit in limits] == [
        ("codex", STAMP_EPOCH, LIMIT_UNTIL),
    ]


@pytest.mark.parametrize("table", [lane_limit.ACCOUNT_TABLE, lane_limit.TABLE])
def test_a_limit_that_lifted_at_a_datetime_string_no_longer_holds(tmp_path: Path, table: str) -> None:
    db, _log = seeded(tmp_path)
    _with_limit(db, table, STAMP_EPOCH - 3600, "2026-11-26 22:00:00")
    with sqlite3.connect(db) as connection:
        assert lane_limit.account_limits(connection, STAMP_EPOCH) == {}
        assert lane_limit.all_active(connection, STAMP_EPOCH) == []


STAMP_COLUMN = re.compile(r"(^at|_at|^until|since)$")


def _insert(connection: sqlite3.Connection, table: str, **values: object) -> None:
    for name, kind, notnull, default in (
        (row[1], row[2], row[3], row[4]) for row in connection.execute(f"PRAGMA table_info({table})")
    ):
        if notnull and default is None and name not in values:
            values[name] = "" if kind == "TEXT" else 0
    connection.execute(
        f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _stamped_world(tmp_path: Path) -> tuple[Path, Path]:
    db, machines = world(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/cleffy", "50", state=HANDED_OFF_EXTERNALLY)
        add_ticket(connection, "maxharris1/cleffy", "51", state="building")
        connection.execute(
            "UPDATE ticket_state SET external_at = ?, closed_at = ? WHERE ticket_id = '50'", (NOW - 600, NOW - 500)
        )
        for at, before, after in ((NOW - 3000, None, "building"), (NOW - 600, "building", HANDED_OFF_EXTERNALLY)):
            _insert(connection, "ticket_transitions", repo="maxharris1/cleffy", ticket_id="50",
                    from_state=before, to_state=after, at=at, source="test")
        for check, status, conclusion, created, started, completed in (
            ("unit", "completed", "failure", NOW - 900, NOW - 880, NOW - 700),
            ("lint", "in_progress", None, NOW - 400, NOW - 300, None),
            ("e2e", "queued", None, NOW - 500, None, None),
        ):
            _insert(connection, "ci_jobs", repo="maxharris1/cleffy", ticket_id="51", head="a" * 40,
                    workflow="ci", check_name=check, status=status, conclusion=conclusion,
                    created_at=created, started_at=started, completed_at=completed, seen_at=NOW - 30)
        for reason, raised in (("early", NOW - 5000), ("late", NOW - 50)):
            _insert(connection, "needs_attention", repo="maxharris1/cleffy", ticket_id="51",
                    reason=reason, detail=reason, raised_at=raised)
        connection.execute(telemetry.DDL)
        for job_id, updated, finished in (("job-b-pc", NOW - 40, None), ("job-codex-pc", NOW - 90, NOW - 80)):
            _insert(connection, telemetry.TABLE, job_id=job_id, bot="claude-bot", harness="claude",
                    repo="maxharris1/cleffy", started_at=NOW - 1000, updated_at=updated,
                    last_event_at=updated, finished_at=finished, tool_calls="{}", steps="[]")
        connection.execute(account_doctor._SIGNOUTS_SCHEMA)
        _insert(connection, account_doctor.SIGNOUTS, account="claude-b", device="pc", since=NOW - 30)
        connection.execute(lane_limit.ACCOUNT_DDL)
        _insert(connection, lane_limit.ACCOUNT_TABLE, account="claude-b", harness="claude", bot="claude-bot",
                recipient="claude", limited_at=NOW - 100, until=int(NOW) + 900, source="stream", job_id="j")
    return db, machines


def _all_as_datetime_strings(db: Path, where: str = "1") -> list[str]:
    converted = []
    with sqlite3.connect(db) as connection:
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for column in [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]:
                if STAMP_COLUMN.search(column):
                    changed = connection.execute(
                        f"UPDATE {table} SET {column} = datetime({column}, 'unixepoch') "
                        f"WHERE typeof({column}) IN ('integer', 'real') AND {where}"
                    ).rowcount
                    if changed:
                        converted.append(f"{table}.{column}")
    return converted


def test_a_snapshot_is_the_same_when_every_timestamp_column_holds_a_datetime_string(tmp_path: Path) -> None:
    db, machines = _stamped_world(tmp_path)
    log = tmp_path / "poll.log"
    as_epochs = collect_snapshot(db, log, now=NOW, machines_path=machines)
    assert as_epochs["attention"]["count"] == 2
    assert as_epochs["ci_runs"]["runs"] and as_epochs["ci_runs"]["failures"]
    assert as_epochs["telemetry"]["recent"]
    assert next(row for row in as_epochs["tickets"] if row["ticket_id"] == "50")["shipped_at"] == NOW - 600
    usage = {entry["account"]: entry for entry in as_epochs["usage"]["accounts"]}
    assert usage["claude-default"]["observed_at"] == NOW - 60
    assert usage["claude-b"]["limited_until"] == int(NOW) + 900
    converted = _all_as_datetime_strings(db)
    assert {
        "ticket_state.external_at", "ticket_state.closed_at", "ticket_transitions.at", "tracked_tickets.tracked_at",
        "ci_jobs.created_at", "ci_jobs.started_at", "ci_jobs.completed_at", "ci_jobs.seen_at",
        "needs_attention.raised_at", "job_telemetry.updated_at", "job_telemetry.finished_at",
        "job_telemetry.last_event_at", "usage_readings.observed_at", "usage_readings.resets_at",
        "account_signins.checked_at", "signout_locks.since", "lane_account_limits.limited_at",
        "lane_account_limits.until", "device_slots.placed_at", "jobs.created_at", "jobs.last_activity_at",
    } <= set(converted)
    assert collect_snapshot(db, log, now=NOW, machines_path=machines) == as_epochs


def test_a_snapshot_is_the_same_when_some_rows_of_each_timestamp_column_hold_datetime_strings(
    tmp_path: Path,
) -> None:
    db, machines = _stamped_world(tmp_path)
    log = tmp_path / "poll.log"
    as_epochs = collect_snapshot(db, log, now=NOW, machines_path=machines)
    assert {"ci_jobs.created_at", "needs_attention.raised_at"} <= set(_all_as_datetime_strings(db, "rowid % 2 = 1"))
    assert collect_snapshot(db, log, now=NOW, machines_path=machines) == as_epochs


def test_a_push_publishes_the_same_files_when_every_timestamp_column_holds_a_datetime_string(
    tmp_path: Path, monkeypatch
) -> None:
    sent = {}
    for form in ("epoch", "datetime"):
        run = tmp_path / form
        run.mkdir()
        db, machines = _stamped_world(run)
        if form == "datetime":
            _all_as_datetime_strings(db)
        capture = _Capture()
        monkeypatch.delenv("ORCH_DB", raising=False)
        monkeypatch.setattr(dashboard_cli, "transport_for", lambda config, capture=capture: capture)
        code = dashboard_cli.main(
            ["push", "--once", "--db", str(db), "--log", str(run / "poll.log"),
             "--out", str(run / "site"), "--orch", str(_orch(run, 60)), "--machines", str(machines)]
        )
        assert code == 0
        sent[form] = sorted(path.name for path in capture.sent[0])
    assert sent["datetime"] == sent["epoch"]


@pytest.mark.parametrize("stamp", [STAMP_EPOCH, int(STAMP_EPOCH), STAMP])
def test_device_sync_reads_since_and_checked_at_as_an_epoch_or_a_datetime_string(
    tmp_path: Path, stamp: object
) -> None:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        device_sync.ensure(connection)
        _insert(connection, device_sync.TABLE, device="pc", bot="claude-bot", harness="claude",
                checkout="/srv/factory", state=device_sync.SYNCED, detail="", since=stamp, checked_at=stamp)
        rows = device_sync.read(connection)
    assert [(row.since, row.checked_at) for row in rows] == [(STAMP_EPOCH, STAMP_EPOCH)]


def test_qa_evidence_orders_settled_runs_by_instant_across_mixed_stamps(tmp_path: Path) -> None:
    db, _log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        for run_id, started in (("old", STAMP_EPOCH - 60), ("new", STAMP), ("mid", int(STAMP_EPOCH - 30))):
            _insert(connection, "qa_runs", run_id=run_id, repo="o/r", ticket_id="1", head=run_id, state="reported", scope="none", arm="primary",
                    evidence_dir=f"/e/{run_id}", started_at=started)
        runs = qa_evidence_push.settled_runs(connection)
    assert [(run.run_id, run.started_at) for run in runs] == [
        ("new", STAMP_EPOCH), ("mid", STAMP_EPOCH - 30), ("old", STAMP_EPOCH - 60),
    ]


@pytest.mark.parametrize(
    ("since", "held"),
    [
        (STAMP_EPOCH - 60, True),
        ("2026-11-26 22:14:58", True),
        (STAMP_EPOCH - 3600, False),
        ("2026-11-26 21:15:58", False),
    ],
)
def test_a_signout_lock_holds_for_its_lease_whether_since_is_an_epoch_or_a_datetime_string(
    tmp_path: Path, since: object, held: bool
) -> None:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.execute(account_doctor._SIGNOUTS_SCHEMA)
        _insert(connection, account_doctor.SIGNOUTS, account="claude-b", device="pc", since=since)
        locked = account_doctor.signing_out(connection, STAMP_EPOCH)
    assert (("claude-b", "pc") in locked) is held
