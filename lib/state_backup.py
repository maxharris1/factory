"""Back up the dispatcher database every poll cycle, and restore it (#66).

The #66 reboot lost ~/.local/state/factory, and the fresh database rebuilt
finished tickets. The backup lives outside that directory (default
~/.local/share/factory/backup/orch.db, beside the venv, which survived), is
taken with SQLite's online backup API (consistent under WAL, and it never
holds the poller's database for long), and is swapped into place with an
atomic rename, so a reader never sees half a backup.

Beside that rolling copy, the first cycle of each week in America/New_York
takes a dated snapshot, weekly/orch-<Monday>.db, the baseline that
`orch eval-report --db --baseline` compares against (#545). A poller down
over Monday takes it on its first later cycle that week. Snapshots dated
more than KEEP_WEEKS weeks before the current Monday are deleted; the
newest is always kept.

`orch db-backup <label>` takes a labelled copy before a manual write to the
database, manual/orch-<UTC stamp>-<label>.db beside the rolling copy (#654).
Each one, and the poller once a day, keeps the newest KEEP_LABELLED and any
younger than LABELLED_MIN_AGE and deletes the rest with their -wal and -shm
files. The stamp in the name, not the mtime, is a copy's age.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BACKUP_ENV = "ORCH_DB_BACKUP"
# The job_events detail of a step restore() holds; the dispatcher's first
# pass over the ticket queues it again unless the thread answered it (#108).
RESTORED = "restored"
# The job_events event restore() records on each running step it copies: a
# claim that later goes back to the queue is held as RESTORED (#108).
RESTORED_CLAIM = "restored-claim"
STEPS = ("plan", "build", "fix", "review")
KEEP_WEEKS = 4
WEEK_ZONE = ZoneInfo("America/New_York")
_WEEKLY_NAME = re.compile(r"orch-(\d{4}-\d{2}-\d{2})\.db")
KEEP_LABELLED = 5
LABELLED_MIN_AGE = timedelta(hours=48)
LABELLED_PRUNE_EVERY = timedelta(days=1)
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_STAMP = "%Y%m%dT%H%M%SZ"
_LABELLED_NAME = re.compile(rf"orch-(\d{{8}}T\d{{6}}Z)-({_LABEL.pattern})\.db")
_SIBLINGS_THEN_BACKUP = ("-wal", "-shm", "")


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


def week_monday(now: datetime) -> date:
    local = now.astimezone(WEEK_ZONE).date()
    return local - timedelta(days=local.weekday())


def weekly_snapshot(db: Path, directory: Path, now: datetime) -> Path:
    """Take this week's dated snapshot of db into directory unless it exists,
    then prune snapshots past KEEP_WEEKS."""
    _check_apart(db, directory / "orch.db")
    monday = week_monday(now)
    target = directory / f"orch-{monday.isoformat()}.db"
    if not target.exists():
        if not db.exists():
            raise BackupError(f"database {db} does not exist")
        _copy(db, target)
    _prune(directory, monday - timedelta(weeks=KEEP_WEEKS))
    return target


def _prune(directory: Path, oldest: date) -> None:
    dated = sorted(
        (date.fromisoformat(match.group(1)), path)
        for path in directory.iterdir()
        if (match := _WEEKLY_NAME.fullmatch(path.name)) is not None
    )
    for day, path in dated[:-1]:
        if day < oldest:
            path.unlink()


def labelled_backup(
    db: Path, directory: Path, label: str, now: datetime
) -> tuple[Path, list[Path]]:
    """Copy db to directory/orch-<now>-<label>.db, then prune_labelled;
    return the copy and the backups pruned."""
    if _LABEL.fullmatch(label) is None:
        raise BackupError(
            f"label {label!r} must be 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    target = directory / f"orch-{now.astimezone(UTC):{_STAMP}}-{label}.db"
    _check_apart(db, target)
    if not db.exists():
        raise BackupError(f"database {db} does not exist")
    _copy(db, target)
    return target, prune_labelled(directory, now)


def prune_labelled(directory: Path, now: datetime) -> list[Path]:
    """Delete the labelled backups past the newest KEEP_LABELLED that are at
    least LABELLED_MIN_AGE old, with their -wal and -shm files; return them."""
    if not directory.exists():
        return []
    labelled = sorted(
        (stamp, path)
        for path in directory.iterdir()
        if (stamp := _labelled_stamp(path.name)) is not None
    )
    deleted = []
    for stamp, path in labelled[:-KEEP_LABELLED]:
        if now - stamp >= LABELLED_MIN_AGE:
            for suffix in _SIBLINGS_THEN_BACKUP:
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            deleted.append(path)
    return deleted


def _labelled_stamp(name: str) -> datetime | None:
    match = _LABELLED_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


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
    """The poll loop's after-cycle hook: back up, take the weekly snapshot,
    prune the labelled backups once a day, record a failure, go on."""

    def __init__(
        self,
        db: Path,
        dest: Path,
        record_error,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.dest = dest
        self.weekly = dest.parent / "weekly"
        self.labelled = dest.parent / "manual"
        self.pruned_at: datetime | None = None
        self.record_error = record_error
        self.clock = clock

    def __call__(self) -> None:
        try:
            backup(self.db, self.dest)
        except Exception as exc:
            self.record_error("backup", "-", f"backup:{type(exc).__name__}")
        try:
            weekly_snapshot(self.db, self.weekly, self.clock())
        except Exception as exc:
            self.record_error("backup", "-", f"weekly-snapshot:{type(exc).__name__}")
        now = self.clock()
        if self.pruned_at is None or now - self.pruned_at >= LABELLED_PRUNE_EVERY:
            self.pruned_at = now
            try:
                prune_labelled(self.labelled, now)
            except Exception as exc:
                self.record_error("backup", "-", f"labelled-prune:{type(exc).__name__}")
