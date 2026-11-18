"""Copy settled QA runs' evidence folders to the dashboard PC (#910).

A full `orch dashboard push` calls `push_evidence` once the snapshot is out.
Each settled run's lane files and a rendered index.html go as one tar over
one ssh, unpacked into a temp folder beside `<remote_dir>/qa/<run_id>` and
moved into place only when whole. A run whose files have not changed since
its last send (`.qa-push.json` beside the snapshot) is not sent again. A
symlink, a hard-linked file or a folder outside the evidence root is never
sent: the run is refused with one log line and the others still go.
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
from pathlib import Path

from lib.dashboard import redact
from lib.dashboard_config import valid_remote_dir
from lib.dashboard_pages.qa_evidence import run_page
from lib.dashboard_push import SEND_TIMEOUT_SECONDS, TEMP_SUFFIX, SshTransport
from lib.qa_evidence import EvidenceRefused, RunFile, _open_root, _resolve, walk_run, write_run_tar
from lib.qa_storage import check_id

EVIDENCE_DEADLINE_SECONDS = 120
STORE_NAME = ".qa-push.json"
_MIB = 1024 * 1024


def settled_runs(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return connection.execute(
        """
        SELECT run_id, repo, evidence_dir FROM qa_runs
        WHERE evidence_dir IS NOT NULL AND state NOT IN ('queued', 'running')
        ORDER BY started_at DESC, rowid DESC
        """
    ).fetchall()


def fingerprint(files: Iterable[RunFile]) -> str:
    lines = sorted(f"{file.path}\t{file.size}\t{file.mtime_ns}\n" for file in files)
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def upload_script(remote_dir: str, run_id: str) -> str:
    """Unpack the tar on stdin beside qa/<run_id>, then swap it in."""
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


def push_evidence(
    transport: SshTransport,
    *,
    connection: sqlite3.Connection,
    roots: Mapping[str, Path],
    store: Path,
    log: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
    deadline: float = EVIDENCE_DEADLINE_SECONDS,
) -> None:
    """Send each settled run that changed, newest first, until `deadline`.

    A transport PushError propagates; runs sent before it stay recorded."""
    started = clock()
    sent = _load(store)
    opened: dict[str, tuple[str, int | None]] = {}
    try:
        for run_id, repo, evidence_dir in settled_runs(connection):
            if clock() - started >= deadline:
                log("dashboard push: qa evidence deadline reached; the rest goes next push")
                return
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
                if sent.get(run_id) == mark:
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
                continue
            except (EvidenceRefused, OSError, ValueError) as exc:
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                log(f"dashboard push: qa run {run_id} refused: {redact(' '.join(reason.split()))}")
                continue
            sent[run_id] = mark
            _save(store, sent)
    finally:
        for _root, fd in opened.values():
            if fd is not None:
                os.close(fd)


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


def _load(store: Path) -> dict[str, str]:
    try:
        data = json.loads(store.read_bytes())
    except (OSError, ValueError, RecursionError):
        return {}
    sent = data.get("sent") if isinstance(data, dict) and data.get("version") == 1 else None
    if not isinstance(sent, dict):
        return {}
    return {run: mark for run, mark in sent.items() if isinstance(run, str) and isinstance(mark, str)}


def _save(store: Path, sent: Mapping[str, str]) -> None:
    fd, temp = tempfile.mkstemp(prefix=f".{store.name}.", dir=store.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "sent": dict(sent)}, handle, sort_keys=True)
        os.replace(temp, store)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temp)
        raise
