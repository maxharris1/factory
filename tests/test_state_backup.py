"""#108 Unit 7 (#66): the dispatcher database is backed up every poll cycle
and restored when it is missing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lib import cli
from lib.queue import JobRequest
from lib.state_backup import BackupError, backup, default_backup_path, restore
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


def _dedupe_keys(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT dedupe_key FROM jobs")]


def _dump(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def _checkpoint(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


class OneCycle:
    def __init__(self, dispatcher, source) -> None:
        del dispatcher, source

    def run(self, *, after_poll=None, **_kwargs) -> None:
        after_poll()


def _poll_once(monkeypatch, dispatcher, db: Path, backup_path: Path) -> int:
    monkeypatch.setenv("ORCH_DB_BACKUP", str(backup_path))
    monkeypatch.setattr(cli, "_runtime", lambda path, orch, create, **_: (dispatcher, object()))
    monkeypatch.setattr(cli, "PollLoop", OneCycle)
    monkeypatch.setattr(cli, "_dashboard_pusher", lambda *_args: None)
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
    assert errors == ["backup:BackupError"]


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
    before = (queue.path.stat().st_ino, _dump(queue.path))

    code = cli.main(["restore-db", "--db", str(queue.path), "--backup", str(dest)])
    _checkpoint(queue.path)

    assert code == 1
    assert "exists; refusing to overwrite it" in capsys.readouterr().err
    assert (queue.path.stat().st_ino, _dump(queue.path)) == before


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
