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
database, manual/orch-<UTC stamp>-<label>.db beside the rolling copy (#654). A
stamp already taken moves to the next free second, so no copy replaces another.
Each one, and the poller once a day, keeps the newest KEEP_LABELLED and any
younger than LABELLED_MIN_AGE and deletes the rest with their -wal and -shm
files. The stamp in the name, not the mtime, is a copy's age.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from lib import job_detail

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
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.unlink(missing_ok=True)
    _write_copy(source, temporary, prepare)
    os.replace(temporary, target)
    _fsync_directory(target.parent)


def _write_copy(
    source: Path,
    temporary: Path,
    prepare: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
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


def _fsync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def backup(db: Path, dest: Path) -> None:
    """Copy the live database db and its job-detail/ files to dest, replacing atomically."""
    _check_apart(db, dest)
    if not db.exists():
        raise BackupError(f"database {db} does not exist")
    _sync_job_detail(db, dest)
    _copy(db, dest)
    _sync_job_detail(db, dest)


def _sync_job_detail(source_db: Path, target_db: Path) -> None:
    src_dir = source_db.parent / job_detail.DIRECTORY
    if not src_dir.is_dir():
        return
    dst_dir = target_db.parent / job_detail.DIRECTORY
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create job-detail directory {dst_dir}: {exc}") from exc
    for src in sorted(src_dir.glob("*.json")):
        if src.is_symlink() or not src.is_file():
            continue
        try:
            data = src.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupError(f"cannot read job-detail {src}: {exc}") from exc
        dst = dst_dir / src.name
        try:
            if dst.is_file() and dst.read_bytes() == data:
                continue
            tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, dst)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise BackupError(f"cannot write job-detail {dst}: {exc}") from exc


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
    _refuse_spilled_without_file(db)
    manifest = {
        "sha256": facts.sha256,
        "job_detail": _job_detail_files(db),
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
    present = _job_detail_files(db)
    changed = sorted(
        name
        for name, digest in (recorded.get("job_detail") or {}).items()
        if present.get(name) != digest
    )
    if changed:
        raise BackupError(
            f"{len(changed)} job-detail files beside {db} are missing or differ from "
            f"{manifest} (first: {', '.join(changed[:3])})"
        )
    _refuse_spilled_without_file(db)
    return facts


def _job_detail_files(db: Path) -> dict[str, str]:
    directory = db.parent / job_detail.DIRECTORY
    if not directory.is_dir():
        return {}
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.json"))
    }


def _refuse_spilled_without_file(db: Path) -> None:
    connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone() is None:
            return
        rows = connection.execute("SELECT id, detail FROM jobs ORDER BY rowid").fetchall()
    finally:
        connection.close()
    lacking = []
    for job_id, detail in rows:
        data = job_detail.as_object(detail)
        if data is None or job_detail.MARKER not in data:
            continue
        try:
            merged = json.loads(job_detail.load(db.parent, str(job_id), detail))
        except ValueError:
            merged = {}
        if not set(data[job_detail.MARKER]) <= merged.keys():
            lacking.append(str(job_id))
    if lacking:
        raise BackupError(
            f"{len(lacking)} spilled job rows in {db} have no complete job-detail file "
            f"beside it (first: {', '.join(lacking[:3])})"
        )


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
    """Copy db to directory/orch-<stamp>-<label>.db, stamped with the first
    free second at or after now, then prune_labelled; return the copy and the
    backups pruned."""
    if _LABEL.fullmatch(label) is None:
        raise BackupError(
            f"label {label!r} must be 1-64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    _check_apart(db, directory / "orch.db")
    if not db.exists():
        raise BackupError(f"database {db} does not exist")
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=directory, prefix=".orch-", suffix=".db.tmp")
    os.close(handle)
    temporary = Path(name)
    try:
        _write_copy(db, temporary)
        target = _link_to_first_free_stamp(temporary, directory, label, now)
    finally:
        temporary.unlink(missing_ok=True)
    _fsync_directory(directory)
    return target, prune_labelled(directory, now)


def _link_to_first_free_stamp(
    temporary: Path, directory: Path, label: str, now: datetime
) -> Path:
    stamp = now.astimezone(UTC).replace(microsecond=0)
    while True:
        target = directory / f"orch-{stamp:{_STAMP}}-{label}.db"
        try:
            os.link(temporary, target)
            return target
        except FileExistsError:
            stamp += timedelta(seconds=1)


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
    try:
        _sync_job_detail(backup_path, db)
    except BackupError:
        db.unlink(missing_ok=True)
        raise
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
    seconds once one has succeeded), take the weekly snapshot, prune the
    labelled backups once a day, record a failure, go on."""

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
        self.labelled = dest.parent / "manual"
        self.pruned_at: datetime | None = None
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
        now = self.clock()
        if self.pruned_at is None or now - self.pruned_at >= LABELLED_PRUNE_EVERY:
            self.pruned_at = now
            try:
                prune_labelled(self.labelled, now)
            except Exception as exc:
                self.record_error("backup", "-", f"labelled-prune:{type(exc).__name__}")


def db_mtime_ns(path: Path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def advance_wal_close_mtime(path: Path, prev_mtime_ns: int | None) -> None:
    """Advance db mtime when closing the last WAL connection unlinks -wal.

    Linux's coarse filesystem clock (~4ms) can otherwise leave st_mtime_ns
    identical across rapid back-to-back checkpointed commits, causing
    stat-based watchers (db_fingerprint) to miss a write.
    """
    wal = Path(f"{path}-wal")
    try:
        wal_active = wal.exists() and wal.stat().st_size > 0
    except OSError:
        wal_active = False
    if wal_active:
        return
    try:
        st = os.stat(path)
        floor = max(
            st.st_mtime_ns + 1,
            (prev_mtime_ns + 1) if prev_mtime_ns is not None else 0,
        )
        os.utime(path, ns=(st.st_atime_ns, max(time.time_ns(), floor)))
    except OSError:
        pass
