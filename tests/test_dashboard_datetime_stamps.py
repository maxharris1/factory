import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib import dashboard_cli
from lib.dashboard import _age, collect_snapshot
from tests.test_dashboard import NOW, add_job, seeded
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
