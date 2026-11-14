"""Back up the dispatcher database every poll cycle, and restore it (#66).

The #66 reboot lost ~/.local/state/factory, and the fresh database rebuilt
finished tickets. The backup lives outside that directory (default
~/.local/share/factory/backup/orch.db, beside the venv, which survived), is
taken with SQLite's online backup API (consistent under WAL, and it never
holds the poller's database for long), and is swapped into place with an
atomic rename, so a reader never sees half a backup.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

BACKUP_ENV = "ORCH_DB_BACKUP"
# The job_events detail of a step restore() holds; the dispatcher's first
# pass over the ticket queues it again unless the thread answered it (#108).
RESTORED = "restored"
# The job_events event restore() records on each running step it copies: a
# claim that later goes back to the queue is held as RESTORED (#108).
RESTORED_CLAIM = "restored-claim"
STEPS = ("plan", "build", "fix", "review")


class BackupError(RuntimeError):
    pass


def default_backup_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get(BACKUP_ENV)
    if explicit:
        return Path(explicit)
    return Path(env.get("HOME", str(Path.home()))) / ".local/share/factory/backup/orch.db"


def _check_apart(db: Path, backup: Path) -> None:
    if backup.resolve().parent.is_relative_to(db.resolve().parent):
        raise BackupError(
            f"backup {backup} is in the database's own directory; that directory "
            "is what a state loss takes"
        )


def _copy(
    source: Path,
    target: Path,
    prepare: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    """source -> target.tmp with the backup API, prepare(copy), fsync, then
    rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.unlink(missing_ok=True)
    reader = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        writer = sqlite3.connect(temporary)
        try:
            reader.backup(writer)
            if prepare is not None:
                prepare(writer)
            writer.execute("PRAGMA journal_mode = DELETE")
        finally:
            writer.close()
    finally:
        reader.close()
    with open(temporary, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def backup(db: Path, dest: Path) -> None:
    """Copy the live database db to dest, replacing it atomically."""
    _check_apart(db, dest)
    if not db.exists():
        raise BackupError(f"database {db} does not exist")
    _copy(db, dest)


def restore(backup_path: Path, db: Path) -> datetime:
    """Restore a missing db from backup_path; returns when the backup was taken.

    Refuses an existing db, a missing backup (FileNotFoundError) and a
    backup that fails PRAGMA integrity_check. Queued harness steps arrive
    held (_hold_steps).
    """
    if db.exists():
        raise BackupError(f"database {db} exists; refusing to overwrite it")
    if not backup_path.is_file():
        raise FileNotFoundError(f"no backup at {backup_path}")
    try:
        connection = sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            check = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"backup {backup_path} is not a valid database: {exc}") from exc
    if check is None or check[0] != "ok":
        raise BackupError(f"backup {backup_path} failed integrity_check")
    _copy(backup_path, db, _hold_steps)
    return datetime.fromtimestamp(backup_path.stat().st_mtime, UTC)


def _hold_steps(connection: sqlite3.Connection) -> None:
    """Cancel the copy's queued plan, build, fix and review jobs as RESTORED,
    and mark its running ones and its failed ones T6 has yet to handle
    RESTORED_CLAIM, before the copy is renamed into place (#108).

    The backup is up to a cycle old: such a step may since have run and been
    answered. Restored queued, an `orch wait` could claim it before the
    poller reads its thread. Held, it waits for the dispatcher's next pass
    over the ticket, which queues it again unless the thread answered it. A
    running one is left to its runner; if it is released back to the queue,
    queue.retire_requeued holds it the same way. T6 settles a marked failed
    one against its thread before it retries it or calls the ticket stuck.
    """
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not {"jobs", "job_events"} <= tables:
        return
    now = time.time()
    steps = f"""
        FROM jobs WHERE state = ? AND recipient != 'grok'
          AND action IN ({", ".join("?" for _ in STEPS)})
    """
    connection.execute(
        f"""
        INSERT INTO job_events (
            job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
        )
        SELECT id, lower(repo), ticket_id, action, recipient, ?, ?, ? {steps}
        """,
        (RESTORED_CLAIM, RESTORED, now, "running", *STEPS),
    )
    connection.execute(
        f"""
        INSERT INTO job_events (
            job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
        )
        SELECT id, lower(repo), ticket_id, action, recipient, ?, ?, ? {steps}
          AND failure_handled = 0
        """,
        (RESTORED_CLAIM, RESTORED, now, "failed", *STEPS),
    )
    connection.execute(
        f"""
        INSERT INTO job_events (
            job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
        )
        SELECT id, lower(repo), ticket_id, action, recipient, 'cancelled', ?, ? {steps}
        """,
        (RESTORED, now, "queued", *STEPS),
    )
    connection.execute(
        f"UPDATE jobs SET state = 'cancelled', finished_at = ?, last_activity_at = ? "
        f"WHERE id IN (SELECT id {steps})",
        (now, now, "queued", *STEPS),
    )
    connection.commit()


class CycleBackup:
    """The poll loop's after-cycle hook: back up, record a failure, go on."""

    def __init__(self, db: Path, dest: Path, record_error) -> None:
        self.db = db
        self.dest = dest
        self.record_error = record_error

    def __call__(self) -> None:
        try:
            backup(self.db, self.dest)
        except Exception as exc:
            self.record_error("backup", "-", f"backup:{type(exc).__name__}")
