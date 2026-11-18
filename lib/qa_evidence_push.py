"""Copy settled QA runs' evidence folders to the dashboard PC (#910).

A full `orch dashboard push` calls `push_evidence` once the snapshot is out.
Each settled run's lane files and a rendered index.html go as one tar over
one ssh, unpacked into a temp folder beside `<remote_dir>/qa/<run_id>` and
moved into place only when whole. A run whose files have not changed since
its last send to this host and remote_dir (`.qa-push.json` beside the
snapshot) is not sent again. A symlink, a hard-linked file or a folder outside
the evidence root is never sent: the run is refused with one log line and the
others still go. A sent run the box no longer lists, or whose folder is gone,
is removed from the PC. Over `budget_bytes`, the oldest closed tickets' runs
are removed from the PC too, and are not sent again.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from lib.dashboard import redact
from lib.dashboard_config import valid_remote_dir
from lib.dashboard_pages.qa_evidence import run_page
from lib.dashboard_push import SEND_TIMEOUT_SECONDS, TEMP_SUFFIX, SshTransport
from lib.load import is_strict_int
from lib.qa_evidence import CLOSED_STATES, EvidenceRefused, RunFile, _open_root, _resolve, walk_run, write_run_tar
from lib.qa_storage import check_id

EVIDENCE_DEADLINE_SECONDS = 120
STORE_NAME = ".qa-push.json"
_MIB = 1024 * 1024


@dataclass(frozen=True)
class Sent:
    mark: str
    bytes: int


@dataclass
class Store:
    """What this box put on one destination, `<host>:<remote_dir>`."""

    dest: str
    runs: dict[str, Sent] = field(default_factory=dict)
    # Removed for budget; not sent again while the box lists them.
    evicted: set[str] = field(default_factory=set)


def settled_runs(connection: sqlite3.Connection) -> list[tuple[str, str, str, bool]]:
    """(run_id, repo, evidence_dir, ticket closed), newest first."""
    closed = ", ".join(f"'{state}'" for state in CLOSED_STATES)
    return [
        (run_id, repo, evidence_dir, bool(is_closed))
        for run_id, repo, evidence_dir, is_closed in connection.execute(
            f"""
            SELECT r.run_id, r.repo, r.evidence_dir, EXISTS (
                SELECT 1 FROM ticket_state s
                WHERE lower(s.repo) = r.repo AND s.ticket_id = r.ticket_id
                    AND s.workflow_state IN ({closed})
            )
            FROM qa_runs r
            WHERE r.evidence_dir IS NOT NULL AND r.state NOT IN ('queued', 'running')
            ORDER BY r.started_at DESC, r.rowid DESC
            """
        )
    ]


def fingerprint(files: Iterable[RunFile]) -> str:
    lines = sorted(f"{file.path}\t{file.size}\t{file.mtime_ns}\n" for file in files)
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def upload_script(remote_dir: str, run_id: str) -> str:
    check_id(run_id, "run_id")
    if not valid_remote_dir(remote_dir):
        raise ValueError("remote_dir must be a plain path")
    temp, old = f".{run_id}{TEMP_SUFFIX}", f".{run_id}.push-old"
    return " && ".join(
        [
            f"cd -- {remote_dir}",
            "mkdir -p -- qa",
            "cd -- qa",
            f"rm -rf -- {temp} {old}",
            f"mkdir -- {temp}",
            f"tar -x -f - -C {temp}",
            f"test -f {temp}/index.html",
            f"{{ ! test -e {run_id} || mv -- {run_id} {old}; }}",
            f"mv -- {temp} {run_id}",
            f"rm -rf -- {old}",
        ]
    )


def remove_script(remote_dir: str, run_id: str) -> str:
    check_id(run_id, "run_id")
    if not valid_remote_dir(remote_dir):
        raise ValueError("remote_dir must be a plain path")
    return f"cd -- {remote_dir} && rm -rf -- qa/{run_id}"


def push_evidence(
    transport: SshTransport,
    *,
    connection: sqlite3.Connection,
    roots: Mapping[str, Path],
    store: Path,
    log: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
    deadline: float = EVIDENCE_DEADLINE_SECONDS,
    budget_bytes: int | None = None,
) -> None:
    """Send each settled run that changed, newest first, until `deadline`;
    then remove gone runs from the PC, then closed tickets' runs, oldest
    first, while the sent runs total more than `budget_bytes`.

    A transport PushError propagates; what was done before it stays recorded."""
    started = clock()
    state = _load(store, f"{transport.host}:{transport.remote_dir}")
    rows = settled_runs(connection)
    gone: set[str] = set()
    opened: dict[str, tuple[str, int | None]] = {}
    try:
        for run_id, repo, evidence_dir, _closed in rows:
            if clock() - started >= deadline:
                log("dashboard push: qa evidence deadline reached; the rest goes next push")
                break
            if run_id in state.evicted:
                continue
            if repo.lower() not in opened:
                opened[repo.lower()] = _open(run_id, repo, roots.get(repo.lower()), log)
            real_root, root_fd = opened[repo.lower()]
            if root_fd is None:
                continue
            parts = _resolve(run_id, evidence_dir, real_root)
            try:
                if isinstance(parts, str):
                    raise EvidenceRefused(parts)
                files = walk_run(root_fd, parts)
                mark = fingerprint(files)
                if run_id in state.runs and state.runs[run_id].mark == mark:
                    continue
                script = upload_script(transport.remote_dir, run_id)
                page = run_page(Path(real_root, *parts), run_id).encode()
                with tempfile.TemporaryFile() as spool:
                    write_run_tar(spool, root_fd, parts, files, page)
                    size = spool.tell()
                    spool.seek(0)
                    transport.remote(
                        script, feed=spool, timeout=SEND_TIMEOUT_SECONDS + math.ceil(size / _MIB)
                    )
            except FileNotFoundError:
                # A file that vanished mid-walk is not a run recycled whole.
                if not os.path.lexists(os.path.join(real_root, *parts)):
                    gone.add(run_id)
                continue
            except (EvidenceRefused, OSError, ValueError) as exc:
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                log(f"dashboard push: qa run {run_id} refused: {redact(' '.join(reason.split()))}")
                continue
            state.runs[run_id] = Sent(mark, size)
            _save(store, state)
    finally:
        for _root, fd in opened.values():
            if fd is not None:
                os.close(fd)

    listed = {row[0] for row in rows}
    for run_id in [run_id for run_id in state.runs if run_id not in listed or run_id in gone]:
        _remove(transport, store, state, run_id)
    if state.evicted - listed:
        state.evicted &= listed
        _save(store, state)
    if budget_bytes is None:
        return
    total = sum(sent.bytes for sent in state.runs.values())
    for run_id, _repo, _dir, closed in reversed(rows):
        if total <= budget_bytes:
            return
        if closed and run_id in state.runs:
            total -= state.runs[run_id].bytes
            state.evicted.add(run_id)
            _remove(transport, store, state, run_id)


def _remove(transport: SshTransport, store: Path, state: Store, run_id: str) -> None:
    transport.remote(remove_script(transport.remote_dir, run_id))
    del state.runs[run_id]
    _save(store, state)


def _open(run_id: str, repo: str, root: Path | None, log: Callable[[str], None]) -> tuple[str, int | None]:
    if root is None:
        log(f"dashboard push: qa run {run_id} held: no qa_storage for {repo}")
        return "", None
    real_root = os.path.realpath(root)
    try:
        return real_root, _open_root(real_root)
    except OSError as exc:
        log(f"dashboard push: qa run {run_id} held: cannot open evidence root ({exc.strerror})")
        return real_root, None


def _load(store: Path, dest: str) -> Store:
    """Anything but a version 2 store for `dest` is empty: everything is sent
    again, and nothing is removed from a destination this box no longer owns."""
    try:
        data = json.loads(store.read_bytes())
    except (OSError, ValueError, RecursionError):
        return Store(dest)
    if not isinstance(data, dict) or data.get("version") != 2 or data.get("dest") != dest:
        return Store(dest)
    runs, evicted = data.get("runs"), data.get("evicted")
    return Store(
        dest,
        runs={
            run: Sent(entry["mark"], entry["bytes"])
            for run, entry in (runs.items() if isinstance(runs, dict) else ())
            if isinstance(run, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("mark"), str)
            and is_strict_int(entry.get("bytes"))
        },
        evicted={run for run in (evicted if isinstance(evicted, list) else ()) if isinstance(run, str)},
    )


def _save(store: Path, state: Store) -> None:
    data = {
        "version": 2,
        "dest": state.dest,
        "runs": {run: {"mark": sent.mark, "bytes": sent.bytes} for run, sent in state.runs.items()},
        "evicted": sorted(state.evicted),
    }
    fd, temp = tempfile.mkstemp(prefix=f".{store.name}.", dir=store.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
        os.replace(temp, store)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temp)
        raise
