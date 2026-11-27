import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import dashboard_cli
from lib.dashboard import _age, _read_db, collect_snapshot
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


def test_a_push_publishes_with_cleffy_213s_finished_at_left_as_is(
    tmp_path: Path, monkeypatch
) -> None:
    db, log = _cleffy_213(tmp_path)
    capture = _Capture()
    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(dashboard_cli, "transport_for", lambda config: capture)
    code = dashboard_cli.main(
        ["push", "--once", "--db", str(db), "--log", str(log),
         "--out", str(tmp_path / "site"), "--orch", str(_orch(tmp_path, 60)),
         "--machines", str(tmp_path / "no-machines.yml")]
    )
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
