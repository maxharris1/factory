"""#108 Unit 7 (#66): the dispatcher database is backed up every poll cycle
and restored when it is missing."""

from __future__ import annotations

import gc
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

from lib import cli
from lib.queue import JobRequest
from lib.state_backup import (
    BackupError,
    CycleBackup,
    backup,
    default_backup_path,
    labelled_backup,
    prune_labelled,
    restore,
    weekly_snapshot,
)
from tests.test_dispatcher import setup, ticket


def _seeded(tmp_path: Path):
    dispatcher, queue = setup(tmp_path / "state")
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="go-1",
            timeout_seconds=3600,
        ),
        dedupe_key="t1:build",
    )
    return dispatcher, queue


def _snapshot(path: Path) -> tuple[int, list[str]]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return path.stat().st_ino, list(connection.iterdump())
    finally:
        connection.close()


def _dedupe_keys(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT dedupe_key FROM jobs")]


class OneCycle:
    def __init__(self, dispatcher, source) -> None:
        del dispatcher, source

    def run(self, *, after_poll=None, **_kwargs) -> None:
        after_poll()


def _poll_once(monkeypatch, dispatcher, db: Path, backup_path: Path) -> int:
    monkeypatch.setenv("ORCH_DB_BACKUP", str(backup_path))
    monkeypatch.setattr(cli, "_runtime", lambda path, orch, create, **_: (dispatcher, object()))
    monkeypatch.setattr(cli, "PollLoop", OneCycle)
    monkeypatch.setattr(cli, "_dashboard_pusher", lambda *_args: (None, 0.0))
    return cli.main(["poll", "--db", str(db), "--once"])


def test_poll_cycle_writes_a_backup(tmp_path: Path, monkeypatch) -> None:
    dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "backup" / "orch.db"

    assert _poll_once(monkeypatch, dispatcher, queue.path, dest) == 0

    assert _dedupe_keys(dest) == ["t1:build"]
    assert not dest.with_name("orch.db.tmp").exists()


def test_backup_failure_is_a_poll_error_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    dispatcher, queue = _seeded(tmp_path)
    inside = queue.path.parent / "orch-backup.db"  # the directory a state loss takes

    assert _poll_once(monkeypatch, dispatcher, queue.path, inside) == 0

    assert not inside.exists()
    with queue.connect() as connection:
        errors = [row[0] for row in connection.execute("SELECT error FROM poll_errors")]
    assert errors == ["backup:BackupError", "weekly-snapshot:BackupError"]


def test_restore_db_restores_a_missing_db(tmp_path: Path, capsys) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "orch.db"
    backup(queue.path, dest)
    lost = tmp_path / "rebooted" / "orch.db"

    code = cli.main(["restore-db", "--db", str(lost), "--backup", str(dest)])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"restored {lost} from {dest} (backup taken ")
    assert out.rstrip().endswith("UTC)")
    assert _dedupe_keys(lost) == ["t1:build"]


def test_restore_db_refuses_an_existing_db(tmp_path: Path, capsys) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "orch.db"
    backup(queue.path, dest)
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="43",
            action="build",
            recipient="claude",
            source_comment_id="go-2",
            timeout_seconds=3600,
        ),
        dedupe_key="t2:build",
    )
    before = _snapshot(queue.path)

    code = cli.main(["restore-db", "--db", str(queue.path), "--backup", str(dest)])
    gc.collect()

    assert code == 1
    assert "exists; refusing to overwrite it" in capsys.readouterr().err
    assert sorted(_dedupe_keys(queue.path)) == ["t1:build", "t2:build"]
    assert _snapshot(queue.path) == before


def test_restore_db_rejects_a_corrupt_backup(tmp_path: Path, capsys) -> None:
    dest = tmp_path / "share" / "orch.db"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    lost = tmp_path / "rebooted" / "orch.db"

    code = cli.main(["restore-db", "--db", str(lost), "--backup", str(dest)])

    assert code == 1
    assert "backup" in capsys.readouterr().err
    assert not lost.exists()


def test_restore_db_with_no_backup_exits_3(tmp_path: Path) -> None:
    lost = tmp_path / "rebooted" / "orch.db"
    missing = tmp_path / "share" / "orch.db"

    assert cli.main(["restore-db", "--db", str(lost), "--backup", str(missing)]) == 3
    assert not lost.exists()


def test_backup_refuses_a_path_in_the_databases_own_directory(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    with pytest.raises(BackupError, match="own directory"):
        backup(queue.path, queue.path.parent / "copy.db")


def test_default_backup_path_survives_a_lost_state_dir() -> None:
    assert default_backup_path({"HOME": "/home/bot"}) == Path(
        "/home/bot/.local/share/factory/backup/orch.db"
    )
    assert default_backup_path({"HOME": "/h", "ORCH_DB_BACKUP": "/b/x.db"}) == Path("/b/x.db")


def test_a_restored_backup_is_a_working_dispatcher_database(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "orch.db"
    backup(queue.path, dest)
    lost = tmp_path / "rebooted" / "d.db"
    restore(dest, lost)

    restored_dispatcher, restored = setup(lost.parent)

    assert restored.outstanding() == ()
    assert restored_dispatcher._tracked_refs() == ()
    restored_dispatcher.process(ticket())
    assert [job.request.action for job in restored.outstanding()] == ["build"]


def test_backup_refuses_a_path_nested_in_the_databases_directory(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    with pytest.raises(BackupError, match="own directory"):
        backup(queue.path, queue.path.parent / "backup" / "orch.db")


ET = ZoneInfo("America/New_York")


def _cycle(queue, dest: Path, at: datetime) -> list[str]:
    errors: list[str] = []
    CycleBackup(queue.path, dest, lambda *error: errors.append(error[2]), clock=lambda: at)()
    return errors


def _weekly(dest: Path) -> list[str]:
    directory = dest.parent / "weekly"
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def test_weekly_snapshot_is_taken_monday_morning_beside_the_rolling_copy(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "backup" / "orch.db"

    assert _cycle(queue, dest, datetime(2026, 11, 15, 23, 0, tzinfo=ET)) == []
    assert _cycle(queue, dest, datetime(2026, 11, 15, 23, 59, tzinfo=ET)) == []
    assert _weekly(dest) == ["orch-2026-11-09.db"]

    assert _cycle(queue, dest, datetime(2026, 11, 16, 0, 5, tzinfo=ET)) == []
    snapshot = dest.parent / "weekly" / "orch-2026-11-16.db"
    assert _weekly(dest) == ["orch-2026-11-09.db", "orch-2026-11-16.db"]
    assert _dedupe_keys(snapshot) == ["t1:build"]
    with closing(sqlite3.connect(snapshot)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert _dedupe_keys(dest) == ["t1:build"]

    taken = snapshot.stat().st_mtime_ns
    assert _cycle(queue, dest, datetime(2026, 11, 16, 8, 0, tzinfo=ET)) == []
    assert snapshot.stat().st_mtime_ns == taken


def test_a_poller_down_over_monday_takes_the_snapshot_on_its_next_cycle(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "backup" / "orch.db"

    assert _cycle(queue, dest, datetime(2026, 11, 18, 14, 0, tzinfo=ET)) == []

    assert _weekly(dest) == ["orch-2026-11-16.db"]
    assert dest.exists()


def test_retention_keeps_four_weeks_plus_the_new_snapshot(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    weekly = tmp_path / "share" / "backup" / "weekly"
    weekly.mkdir(parents=True)
    for day in ("10-05", "10-12", "10-19", "10-26", "11-02", "11-09"):
        (weekly / f"orch-2026-{day}.db").write_bytes(b"")
    (weekly / "notes.txt").write_text("kept")

    weekly_snapshot(queue.path, weekly, datetime(2026, 11, 16, 0, 5, tzinfo=ET))

    assert sorted(path.name for path in weekly.iterdir()) == [
        "notes.txt",
        "orch-2026-10-19.db",
        "orch-2026-10-26.db",
        "orch-2026-11-02.db",
        "orch-2026-11-09.db",
        "orch-2026-11-16.db",
    ]


def test_retention_never_deletes_the_newest_snapshot(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    weekly = tmp_path / "share" / "backup" / "weekly"
    weekly.mkdir(parents=True)
    (weekly / "orch-2027-01-04.db").write_bytes(b"")
    (weekly / "orch-2026-01-05.db").write_bytes(b"")

    weekly_snapshot(queue.path, weekly, datetime(2026, 11, 16, 0, 5, tzinfo=ET))

    assert sorted(path.name for path in weekly.iterdir()) == [
        "orch-2026-11-16.db",
        "orch-2027-01-04.db",
    ]


NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def _labelled_name(at: datetime, label: str = "fix") -> str:
    return f"orch-{at.astimezone(UTC):%Y%m%dT%H%M%SZ}-{label}.db"


def _make(directory: Path, *ages: timedelta) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    names = [_labelled_name(NOW - age) for age in ages]
    for name in names:
        (directory / name).write_bytes(b"")
    return names


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_prune_keeps_the_newest_five_old_backups(tmp_path: Path) -> None:
    manual = tmp_path / "manual"
    names = _make(manual, *(timedelta(days=days) for days in range(3, 10)))

    deleted = prune_labelled(manual, NOW)

    assert sorted(path.name for path in deleted) == sorted(names[5:])
    assert _names(manual) == sorted(names[:5])
    assert prune_labelled(manual, NOW) == []
    assert _names(manual) == sorted(names[:5])


def test_prune_keeps_every_backup_under_48_hours_old(tmp_path: Path) -> None:
    manual = tmp_path / "manual"
    names = _make(manual, *(timedelta(hours=hours) for hours in range(1, 8)))

    assert prune_labelled(manual, NOW) == []
    assert _names(manual) == sorted(names)


def test_prune_deletes_old_backups_past_the_newest_five_and_keeps_recent_ones(
    tmp_path: Path,
) -> None:
    manual = tmp_path / "manual"
    recent = _make(manual, *(timedelta(hours=hours) for hours in range(1, 7)))
    old = _make(manual, timedelta(days=3), timedelta(days=30))

    prune_labelled(manual, NOW)

    assert _names(manual) == sorted(recent)
    assert set(old).isdisjoint(_names(manual))


def test_prune_age_boundary_is_48_hours(tmp_path: Path) -> None:
    manual = tmp_path / "manual"
    newest = _make(manual, *(timedelta(hours=hours) for hours in range(1, 6)))
    [inside] = _make(manual, timedelta(hours=48) - timedelta(seconds=1))
    [at_limit] = _make(manual, timedelta(hours=48))

    prune_labelled(manual, NOW)

    assert _names(manual) == sorted([*newest, inside])
    assert at_limit not in _names(manual)


def test_prune_deletes_wal_and_shm_siblings_with_their_backup(tmp_path: Path) -> None:
    manual = tmp_path / "manual"
    names = _make(manual, *(timedelta(days=days) for days in range(3, 9)))
    for name in names:
        (manual / f"{name}-wal").write_bytes(b"w")
        (manual / f"{name}-shm").write_bytes(b"s")

    prune_labelled(manual, NOW)

    kept = names[:5]
    assert _names(manual) == sorted(
        [*kept, *(f"{name}-wal" for name in kept), *(f"{name}-shm" for name in kept)]
    )


def test_prune_never_touches_unrelated_files(tmp_path: Path) -> None:
    manual = tmp_path / "manual"
    names = _make(manual, *(timedelta(days=days) for days in range(3, 10)))
    unrelated = [
        "orch.db",
        "orch-2020-01-06.db",
        "notes.txt",
        "orch-20200101T000000Z-x.db.tmp",
        "orch-20201399T000000Z-baddate.db",
        "orch-20200101T000000Z-.db",
    ]
    for name in unrelated:
        (manual / name).write_bytes(b"")
    (manual / "weekly").mkdir()
    (manual / "weekly" / "orch-2020-01-06.db").write_bytes(b"")

    prune_labelled(manual, NOW)

    assert _names(manual) == sorted([*names[:5], *unrelated, "weekly"])
    assert _names(manual / "weekly") == ["orch-2020-01-06.db"]


def test_prune_of_a_missing_directory_deletes_nothing(tmp_path: Path) -> None:
    assert prune_labelled(tmp_path / "absent", NOW) == []


def _wal_source(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    db = tmp_path / "state" / "orch.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.executemany("INSERT INTO notes VALUES (?)", [("one",), ("two",)])
    connection.commit()
    return db, connection


def _rows(path: Path) -> list[str]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return [row[0] for row in connection.execute("SELECT body FROM notes ORDER BY body")]


def test_labelled_backup_copies_rows_still_in_the_wal(tmp_path: Path) -> None:
    db, connection = _wal_source(tmp_path)
    manual = tmp_path / "share" / "manual"
    try:
        assert Path(f"{db}-wal").stat().st_size > 0

        target, pruned = labelled_backup(db, manual, "pre-fix", NOW)
    finally:
        connection.close()

    assert target == manual / "orch-20261117T120000Z-pre-fix.db"
    assert _rows(target) == ["one", "two"]
    assert _names(manual) == [target.name]
    assert pruned == []


def test_labelled_backup_prunes_on_each_call(tmp_path: Path) -> None:
    db, connection = _wal_source(tmp_path)
    connection.close()
    manual = tmp_path / "share" / "manual"
    old = _make(manual, *(timedelta(days=days) for days in range(3, 8)))

    first, pruned = labelled_backup(db, manual, "one", NOW)
    assert _names(manual) == sorted([first.name, *old[:4]])
    assert pruned == [manual / old[4]]

    second, pruned = labelled_backup(db, manual, "two", NOW + timedelta(minutes=1))
    assert _names(manual) == sorted([first.name, second.name, *old[:3]])
    assert pruned == [manual / old[3]]


def test_two_labelled_backups_in_one_second_keep_both(tmp_path: Path) -> None:
    db, connection = _wal_source(tmp_path)
    manual = tmp_path / "share" / "manual"
    try:
        connection.execute("DELETE FROM notes")
        connection.execute("INSERT INTO notes VALUES ('before write')")
        connection.commit()
        first, _ = labelled_backup(db, manual, "fix", NOW + timedelta(milliseconds=100))
        connection.execute("UPDATE notes SET body = 'after write'")
        connection.commit()
        second, _ = labelled_backup(db, manual, "fix", NOW + timedelta(milliseconds=900))
    finally:
        connection.close()

    assert first == manual / "orch-20261117T120000Z-fix.db"
    assert second == manual / "orch-20261117T120001Z-fix.db"
    assert _names(manual) == [first.name, second.name]
    assert _rows(first) == ["before write"]
    assert _rows(second) == ["after write"]


def test_labelled_backup_never_replaces_an_existing_file(tmp_path: Path) -> None:
    db, connection = _wal_source(tmp_path)
    connection.close()
    manual = tmp_path / "share" / "manual"
    manual.mkdir(parents=True)
    taken = manual / _labelled_name(NOW)
    taken.write_bytes(b"keep")

    target, _ = labelled_backup(db, manual, "fix", NOW)

    assert taken.read_bytes() == b"keep"
    assert target == manual / _labelled_name(NOW + timedelta(seconds=1))
    assert _rows(target) == ["one", "two"]
    assert _names(manual) == [taken.name, target.name]


def test_concurrent_labelled_backups_each_keep_a_copy(tmp_path: Path) -> None:
    db, connection = _wal_source(tmp_path)
    connection.close()
    manual = tmp_path / "share" / "manual"

    with ThreadPoolExecutor(max_workers=8) as pool:
        targets = list(pool.map(lambda _: labelled_backup(db, manual, "fix", NOW)[0], range(8)))

    expected = [_labelled_name(NOW + timedelta(seconds=second)) for second in range(8)]
    assert sorted(target.name for target in targets) == expected
    assert _names(manual) == expected
    for target in targets:
        assert _rows(target) == ["one", "two"]


def test_a_failed_labelled_backup_leaves_no_file_behind(tmp_path: Path) -> None:
    db = tmp_path / "state" / "orch.db"
    db.parent.mkdir()
    db.write_bytes(b"not a database" * 100)
    manual = tmp_path / "share" / "manual"

    with pytest.raises(sqlite3.DatabaseError):
        labelled_backup(db, manual, "fix", NOW)
    assert _names(manual) == []


@pytest.mark.parametrize("label", ["", "-x", ".x", "a/b", "../x", "a b", "x" * 65])
def test_labelled_backup_refuses_a_bad_label(tmp_path: Path, label: str) -> None:
    db, connection = _wal_source(tmp_path)
    connection.close()
    manual = tmp_path / "share" / "manual"

    with pytest.raises(BackupError, match="label"):
        labelled_backup(db, manual, label, NOW)
    assert not manual.exists()


def test_labelled_backup_refuses_a_directory_inside_the_databases(tmp_path: Path) -> None:
    db, connection = _wal_source(tmp_path)
    connection.close()

    with pytest.raises(BackupError, match="own directory"):
        labelled_backup(db, db.parent / "manual", "fix", NOW)
    assert not (db.parent / "manual").exists()


def test_cycle_prunes_labelled_backups_once_a_day(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "backup" / "orch.db"
    manual = dest.parent / "manual"
    names = _make(manual, *(timedelta(days=days) for days in range(3, 10)))
    errors: list[str] = []
    at = [NOW]
    cycle = CycleBackup(
        queue.path, dest, lambda *error: errors.append(error[2]), clock=lambda: at[0]
    )

    cycle()
    assert _names(manual) == sorted(names[:5])

    later = _make(manual, timedelta(days=20), timedelta(days=21))
    at[0] = NOW + timedelta(hours=23, minutes=59)
    cycle()
    assert _names(manual) == sorted([*names[:5], *later])

    at[0] = NOW + timedelta(hours=24)
    cycle()
    assert _names(manual) == sorted(names[:5])
    assert errors == []


def test_cycle_records_a_labelled_prune_failure_and_goes_on(tmp_path: Path) -> None:
    _dispatcher, queue = _seeded(tmp_path)
    dest = tmp_path / "share" / "backup" / "orch.db"
    dest.parent.mkdir(parents=True)
    (dest.parent / "manual").write_text("not a directory")

    assert _cycle(queue, dest, NOW) == ["labelled-prune:NotADirectoryError"]
    assert _dedupe_keys(dest) == ["t1:build"]


def test_db_backup_command_backs_up_and_prints_what_it_pruned(tmp_path: Path, capsys) -> None:
    db, connection = _wal_source(tmp_path)
    connection.close()
    manual = tmp_path / "share" / "manual"
    manual.mkdir(parents=True)
    old = [f"orch-2020010{day}T000000Z-old.db" for day in range(1, 7)]
    for name in old:
        (manual / name).write_bytes(b"")

    code = cli.main(["db-backup", "pre-fix", "--db", str(db), "--dir", str(manual)])

    assert code == 0
    [target] = [path for path in manual.iterdir() if path.name.endswith("-pre-fix.db")]
    assert _rows(target) == ["one", "two"]
    assert _names(manual) == sorted([target.name, *old[2:]])
    assert capsys.readouterr().out.splitlines() == [
        f"backed up {db} to {target}",
        f"pruned {manual / old[0]}",
        f"pruned {manual / old[1]}",
    ]


def test_db_backup_command_refuses_a_bad_label(tmp_path: Path, capsys) -> None:
    db, connection = _wal_source(tmp_path)
    connection.close()
    manual = tmp_path / "share" / "manual"

    assert cli.main(["db-backup", "../x", "--db", str(db), "--dir", str(manual)]) == 1
    assert capsys.readouterr().err.startswith("orch db-backup: ")
    assert not manual.exists()


def test_help_lists_db_backup(capsys) -> None:
    assert cli.main(["--help"]) == 0
    assert "db-backup <label> [--db PATH] [--dir PATH]" in capsys.readouterr().out
