"""Copy settled QA runs' evidence folders to the dashboard PC (#910).

A full `orch dashboard push` calls `push_evidence` once the snapshot is out.
Each settled run's lane files and a rendered index.html go as one tar over
one ssh, unpacked into a temp folder beside `<remote_dir>/qa/<run_id>` and
moved into place only when whole. A run on the PC whose files have not
changed since its last send to this destination and remote_dir
(`.qa-push.json` beside the snapshot) is not sent again. A symlink, a
hard-linked file or a folder outside the evidence root is never sent: the run
is refused with one log line and the others still go. `plan()` decides which
runs leave the PC first (#921): runs gone from the box, then closed tickets'
runs, oldest first, while over budget.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from lib import epoch
from lib.dashboard import redact
from lib.dashboard_config import valid_remote_dir
from lib.dashboard_pages.qa_evidence import run_page
from lib.dashboard_push import SEND_TIMEOUT_SECONDS, TEMP_SUFFIX, PushError, SshTransport
from lib.qa_evidence import CLOSED_STATES, EvidenceRefused, RunFile, _open_root, _resolve, walk_run, write_run_tar
from lib.qa_storage import check_id

EVIDENCE_DEADLINE_SECONDS = 120
STORE_NAME = ".qa-push.json"
LOCK_NAME = ".qa-push.lock"
_MIB = 1024 * 1024
_DU_LINE = re.compile(r"(\d+)\t([^/\t]+)/?")


@dataclass
class Store:
    dest: str
    sent: dict[str, str] = field(default_factory=dict)


class SettledRun(NamedTuple):
    run_id: str
    repo: str
    evidence_dir: str
    ticket_closed: bool
    started_at: float
    rowid: int


@dataclass(frozen=True)
class Ready:
    real_root: str
    root_fd: int
    parts: tuple[str, ...]
    files: tuple[RunFile, ...]
    mark: str
    bytes: int


@dataclass(frozen=True)
class Gone:
    pass


@dataclass(frozen=True)
class Held:
    reason: str


Local = Ready | Gone | Held


@dataclass(frozen=True)
class BoxRun:
    run_id: str
    started_at: float
    rowid: int
    evictable: bool
    local: Local


@dataclass(frozen=True)
class Plan:
    remove: tuple[str, ...]
    sends: tuple[str, ...]
    over_budget: bool


def settled_runs(connection: sqlite3.Connection) -> list[SettledRun]:
    closed = ", ".join(f"'{state}'" for state in CLOSED_STATES)
    epoch.register(connection)
    return [
        SettledRun(run_id, repo, evidence_dir, bool(is_closed), epoch.read(started_at), rowid)
        for run_id, repo, evidence_dir, is_closed, started_at, rowid in connection.execute(
            f"""
            SELECT r.run_id, r.repo, r.evidence_dir, EXISTS (
                SELECT 1 FROM ticket_state s
                WHERE lower(s.repo) = r.repo AND s.ticket_id = r.ticket_id
                    AND s.workflow_state IN ({closed})
            ), r.started_at, r.rowid
            FROM qa_runs r
            WHERE r.evidence_dir IS NOT NULL AND r.state NOT IN ('queued', 'running')
            ORDER BY {epoch.sql('r.started_at')} DESC, r.rowid DESC
            """
        )
    ]


def fingerprint(files: Iterable[RunFile]) -> str:
    lines = sorted(f"{file.path}\t{file.size}\t{file.mtime_ns}\n" for file in files)
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def plan(
    *,
    all_ids: frozenset[str],
    runs: Sequence[BoxRun],
    pc: Mapping[str, int],
    sent: Mapping[str, str],
    budget_bytes: int | None,
) -> Plan:
    """What one push removes from the PC and sends to it. An empty `all_ids`
    (no qa_runs rows read) names nothing, and a Held run is never named."""
    local = {run.run_id: run.local for run in runs}
    remove = [
        run_id
        for run_id in pc
        if _nameable(run_id)
        and not isinstance(local.get(run_id), Held)
        and ((all_ids and run_id not in all_ids) or isinstance(local.get(run_id), Gone))
    ]
    newest_first = sorted(runs, key=lambda run: (run.started_at, run.rowid), reverse=True)
    sends = [
        run.run_id
        for run in newest_first
        if isinstance(run.local, Ready) and (run.run_id not in pc or sent.get(run.run_id) != run.local.mark)
    ]
    size = {
        run_id: ready.bytes if isinstance(ready := local.get(run_id), Ready) else disk
        for run_id, disk in pc.items()
        if run_id not in remove
    }
    size.update({run_id: local[run_id].bytes for run_id in sends})
    total = sum(size.values())
    if budget_bytes is not None and all_ids:
        for run in reversed(newest_first):
            if total <= budget_bytes:
                break
            if run.evictable and isinstance(run.local, Ready) and run.run_id in size and _nameable(run.run_id):
                total -= size.pop(run.run_id)
                if run.run_id in pc:
                    remove.append(run.run_id)
                if run.run_id in sends:
                    sends.remove(run.run_id)
    return Plan(tuple(remove), tuple(sends), budget_bytes is not None and total > budget_bytes)


def _nameable(run_id: str) -> bool:
    try:
        check_id(run_id, "run_id")
    except ValueError:
        return False
    return not run_id.startswith(".")


def inventory_script(remote_dir: str) -> str:
    if not valid_remote_dir(remote_dir):
        raise ValueError("remote_dir must be a plain path")
    return f'cd -- {remote_dir}/qa 2>/dev/null || exit 0; set -- */; test -e "$1" || exit 0; du -sk -- "$@"'


def parse_inventory(stdout: str) -> dict[str, int]:
    inventory = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        match = _DU_LINE.fullmatch(line)
        if match is None:
            raise PushError("qa inventory unparsable")
        if _nameable(match[2]):
            inventory[match[2]] = int(match[1]) * 1024
    return inventory


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


def remove_script(remote_dir: str, run_ids: Sequence[str]) -> str:
    if not valid_remote_dir(remote_dir):
        raise ValueError("remote_dir must be a plain path")
    if not run_ids or not all(_nameable(run_id) for run_id in run_ids):
        raise ValueError("run ids to remove must be check_id-valid and not dot-named")
    return "rm -rf -- " + " ".join(f"{remote_dir}/qa/{run_id}" for run_id in run_ids)


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
    """Under `<store dir>/.qa-push.lock`, read the PC's `qa` inventory, remove
    the planned runs from the PC in one ssh step, then send each planned run,
    newest first, until `deadline`. Another push holding the lock skips the
    evidence step.

    A transport PushError, or an inventory that does not parse, propagates;
    what was done before it stays recorded."""
    lock = os.open(store.parent / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("dashboard push: qa evidence skipped: another push holds the lock")
            return
        _push_locked(transport, connection, roots, store, log, clock, deadline, budget_bytes)
    finally:
        os.close(lock)


def _push_locked(
    transport: SshTransport,
    connection: sqlite3.Connection,
    roots: Mapping[str, Path],
    store: Path,
    log: Callable[[str], None],
    clock: Callable[[], float],
    deadline: float,
    budget_bytes: int | None,
) -> None:
    started = clock()
    state = _load_for_dest(store, f"{transport.destination()}:{transport.remote_dir}")
    all_ids = frozenset(run_id for (run_id,) in connection.execute("SELECT run_id FROM qa_runs"))
    pc = parse_inventory(transport.remote(inventory_script(transport.remote_dir)))
    state.sent = {run_id: mark for run_id, mark in state.sent.items() if run_id in pc}
    opened: dict[str, tuple[str, int | None]] = {}
    try:
        runs = [_box_run(row, opened, roots, log) for row in settled_runs(connection)]
        planned = plan(all_ids=all_ids, runs=runs, pc=pc, sent=state.sent, budget_bytes=budget_bytes)
        if planned.remove:
            transport.remote(remove_script(transport.remote_dir, planned.remove))
            for run_id in planned.remove:
                state.sent.pop(run_id, None)
            _save(store, state)
        if planned.over_budget:
            log("dashboard push: qa evidence stays over qa_budget_gib; nothing more can be removed for budget")
        local = {run.run_id: run.local for run in runs}
        for run_id in planned.sends:
            if clock() - started >= deadline:
                log("dashboard push: qa evidence deadline reached; the rest goes next push")
                break
            ready = local[run_id]
            try:
                script = upload_script(transport.remote_dir, run_id)
                page = run_page(Path(ready.real_root, *ready.parts), run_id).encode()
                with tempfile.TemporaryFile() as spool:
                    write_run_tar(spool, ready.root_fd, ready.parts, ready.files, page)
                    size = spool.tell()
                    spool.seek(0)
                    transport.remote(
                        script, feed=spool, timeout=SEND_TIMEOUT_SECONDS + math.ceil(size / _MIB)
                    )
            except FileNotFoundError:
                continue
            except (EvidenceRefused, OSError, ValueError) as exc:
                _refused(run_id, exc, log)
                continue
            state.sent[run_id] = ready.mark
            _save(store, state)
    finally:
        for _root, fd in opened.values():
            if fd is not None:
                os.close(fd)


def _box_run(
    row: SettledRun, opened: dict[str, tuple[str, int | None]], roots: Mapping[str, Path], log: Callable[[str], None]
) -> BoxRun:
    key = row.repo.lower()
    if key not in opened:
        opened[key] = _open(row.run_id, row.repo, roots.get(key), log)
    real_root, root_fd = opened[key]
    return BoxRun(row.run_id, row.started_at, row.rowid, row.ticket_closed, _local(row, real_root, root_fd, log))


def _local(row: SettledRun, real_root: str, root_fd: int | None, log: Callable[[str], None]) -> Local:
    if root_fd is None:
        return Held("evidence root not open")
    parts = _resolve(row.run_id, row.evidence_dir, real_root)
    try:
        if isinstance(parts, str):
            raise EvidenceRefused(parts)
        files = walk_run(root_fd, parts)
    except FileNotFoundError:
        return Gone() if _recycled(real_root, parts) else Held("run folder is not there")
    except (EvidenceRefused, OSError) as exc:
        return Held(_refused(row.run_id, exc, log))
    return Ready(real_root, root_fd, parts, files, fingerprint(files), sum(file.size for file in files))


def _refused(run_id: str, exc: Exception, log: Callable[[str], None]) -> str:
    reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
    log(f"dashboard push: qa run {run_id} refused: {redact(' '.join(reason.split()))}")
    return reason


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


def _recycled(real_root: str, parts: tuple[str, ...]) -> bool:
    ticket = os.path.join(real_root, *parts[:-1])
    return os.path.isdir(ticket) and not os.path.islink(ticket) and not os.path.lexists(os.path.join(ticket, parts[-1]))


def _load_for_dest(store: Path, dest: str) -> Store:
    try:
        data = json.loads(store.read_bytes())
    except (OSError, ValueError, RecursionError):
        return Store(dest)
    if not isinstance(data, dict) or data.get("version") != 3 or data.get("dest") != dest:
        return Store(dest)
    sent = data.get("sent")
    return Store(
        dest,
        {
            run: mark
            for run, mark in (sent.items() if isinstance(sent, dict) else ())
            if isinstance(run, str) and isinstance(mark, str)
        },
    )


def _save(store: Path, state: Store) -> None:
    data = {"version": 3, "dest": state.dest, "sent": state.sent}
    fd, temp = tempfile.mkstemp(prefix=f".{store.name}.", dir=store.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
        os.replace(temp, store)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temp)
        raise
