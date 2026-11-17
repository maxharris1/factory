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
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BACKUP_ENV = "ORCH_DB_BACKUP"
BACKUP_EVERY_ENV = "ORCH_DB_BACKUP_EVERY"
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


class BackupError(RuntimeError):
    pass


def default_backup_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get(BACKUP_ENV)
    if explicit:
        return Path(explicit)
    return Path(env.get("HOME", str(Path.home()))) / ".local/share/factory/backup/orch.db"


def backup_every(environ: dict[str, str] | None = None) -> int:
    """Seconds between rolling backups from ORCH_DB_BACKUP_EVERY; 0, the
    default, backs up every cycle."""
    env = os.environ if environ is None else environ
    text = env.get(BACKUP_EVERY_ENV, "").strip() or "0"
    if not text.isdigit():
        raise ValueError(f"{BACKUP_EVERY_ENV} must be a whole number of seconds, not {text!r}")
    return int(text)


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


@dataclass(frozen=True)
class DbFacts:
    sha256: str
    integrity: str
    tables: dict[str, int]
    running_jobs: int
    held_slots: int


def db_facts(db: Path) -> DbFacts:
    """Read db's DbFacts through a read-only connection; BackupError when it
    is missing or SQLite cannot read it."""
    if not db.is_file():
        raise BackupError(f"database {db} does not exist")
    with open(db, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    try:
        connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            integrity = "; ".join(
                str(row[0]) for row in connection.execute("PRAGMA integrity_check")
            )
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            tables = {name: _count(connection, name) for name in names}
            running = 0
            if "jobs" in tables:
                running = connection.execute(
                    "SELECT count(*) FROM jobs WHERE state = 'running'"
                ).fetchone()[0]
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{db} is not a readable database: {exc}") from exc
    return DbFacts(digest, integrity, tables, running, tables.get("device_slots", 0))


def _count(connection: sqlite3.Connection, table: str) -> int:
    quoted = table.replace('"', '""')
    return int(connection.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0])


def write_manifest(db: Path, out: Path, *, source_host: str, to: str, retired: str) -> None:
    """Write the move manifest for db to out; refuse, writing nothing, unless
    db passes integrity_check."""
    facts = db_facts(db)
    if facts.integrity != "ok":
        raise BackupError(f"{db} failed integrity_check: {facts.integrity}")
    manifest = {
        "sha256": facts.sha256,
        "tables": facts.tables,
        "integrity": facts.integrity,
        "source_host": source_host,
        "to": to,
        "retired": retired,
        "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_manifest(db: Path, manifest: Path) -> DbFacts:
    """db's DbFacts when db is the copy manifest describes, passes
    integrity_check, and its source host was retired; BackupError otherwise."""
    try:
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"cannot read manifest {manifest}: {exc}") from exc
    if not isinstance(recorded, dict):
        raise BackupError(f"manifest {manifest} is not a JSON object")
    facts = db_facts(db)
    if facts.sha256 != recorded.get("sha256"):
        raise BackupError(
            f"{db} is not the copy {manifest} describes "
            f"(sha256 {facts.sha256}, manifest {recorded.get('sha256')})"
        )
    if facts.integrity != "ok":
        raise BackupError(f"{db} failed integrity_check: {facts.integrity}")
    if not str(recorded.get("retired") or "").strip():
        raise BackupError(
            f"manifest {manifest} names no retired line; the source host never retired"
        )
    return facts


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
    """The poll loop's after-cycle hook: back up (at most once per `every`
    seconds once one has succeeded), take the weekly snapshot, record a
    failure, go on."""

    def __init__(
        self,
        db: Path,
        dest: Path,
        record_error,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        every: float = 0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.dest = dest
        self.weekly = dest.parent / "weekly"
        self.record_error = record_error
        self.clock = clock
        self.every = every
        self.monotonic = monotonic
        self.last_success: float | None = None

    def _due(self, now: float) -> bool:
        return self.last_success is None or now - self.last_success >= self.every

    def __call__(self) -> None:
        now = self.monotonic()
        if self._due(now):
            try:
                backup(self.db, self.dest)
                self.last_success = now
            except Exception as exc:
                self.record_error("backup", "-", f"backup:{type(exc).__name__}")
        try:
            weekly_snapshot(self.db, self.weekly, self.clock())
        except Exception as exc:
            self.record_error("backup", "-", f"weekly-snapshot:{type(exc).__name__}")
