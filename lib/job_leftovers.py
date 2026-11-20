"""What a finished job leaves under `<workdir_root>/jobs-kept/<id>/` (issue #1202).

The archive mirrors `jobs/`: the job folder's kept files go to
`jobs-kept/<id>/<id>/`, and `<id>.run`, `<id>.brief.md` and `<id>.launched`
to `jobs-kept/<id>/` under their own names. A folder keeps each top-level
regular file that HEAD does not track, every `.orch-worker*` record, and the
same one level down in a subfolder that is a checkout (the local layout's
`repo/`). `venv/` and `interrogate-*` trees are scratch and never kept, even
when they hold their own `.git`. Other trees, symlinks and the checkout itself
are not kept. The record goes to `kept.json.tmp` before the folder is deleted
and is renamed to `kept.json` last, so a `kept.json` means the archive is whole.
A crash after the `.gone` rename leaves `jobs/<id>.gone` or a `kept.json.tmp`
behind for the next pass to finish.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import time
from pathlib import Path

from lib import scratch

KEPT_ROOT = "jobs-kept"
GONE = ".gone"
KEPT_JSON = "kept.json"
PENDING = f"{KEPT_JSON}.tmp"
WORKER_RECORD = ".orch-worker"
ARCHIVED_SIBLINGS = (".run", ".brief.md", ".launched")
DELETED_SIBLINGS = (".claimed",)
SCRATCH_TREES = ("venv", "interrogate-*")


def _tracked(folder: Path) -> frozenset[str]:
    if not os.path.lexists(folder / ".git"):
        return frozenset()
    try:
        listed = subprocess.run(
            ["git", f"--git-dir={folder / '.git'}", "ls-tree", "-z", "--name-only", "HEAD"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if listed.returncode != 0:
        return frozenset()
    return frozenset(os.fsdecode(name) for name in listed.stdout.split(b"\0") if name)


def _kept_files(folder: Path) -> list[Path]:
    tracked = _tracked(folder)
    return [entry for entry in folder.iterdir()
            if entry.name != ".git" and not entry.is_symlink() and entry.is_file()
            and (entry.name not in tracked or entry.name.startswith(WORKER_RECORD))]


def _scratch(sub: Path) -> bool:
    return any(fnmatch.fnmatchcase(sub.name, pattern) for pattern in SCRATCH_TREES)


def kept_entries(folder: Path) -> list[Path]:
    kept = _kept_files(folder)
    for sub in folder.iterdir():
        if (not _scratch(sub) and not sub.is_symlink() and sub.is_dir()
                and os.path.lexists(sub / ".git")):
            kept += _kept_files(sub)
    return sorted(kept)


def cross_device(root: Path) -> str | None:
    jobs = root / "jobs"
    probe = root / KEPT_ROOT if (root / KEPT_ROOT).exists() else root
    if os.stat(probe).st_dev != os.stat(jobs).st_dev:
        return f"error: {probe} is on another device than {jobs}"
    return None


def _free(path: Path) -> Path:
    candidate, n = path, 0
    while os.path.lexists(candidate):
        n += 1
        candidate = path.with_name(f"{path.name}.{n}")
    return candidate


def archive(root: Path, job_id: str, gone: Path, *, siblings: bool) -> None:
    jobs = root / "jobs"
    dest = root / KEPT_ROOT / job_id
    moves = [(entry, dest / job_id / entry.relative_to(gone)) for entry in kept_entries(gone)]
    if siblings:
        moves += [(jobs / f"{job_id}{suffix}", dest / f"{job_id}{suffix}")
                  for suffix in ARCHIVED_SIBLINGS]
    for source, target in moves:
        if not os.path.lexists(source):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, _free(target))


def retire(root: Path, job_id: str, gone: Path, *, siblings: bool, finished_at: float,
           removed_bytes: int) -> None:
    archive(root, job_id, gone, siblings=siblings)
    if siblings:
        for suffix in DELETED_SIBLINGS:
            (root / "jobs" / f"{job_id}{suffix}").unlink(missing_ok=True)
    dest = root / KEPT_ROOT / job_id
    dest.mkdir(parents=True, exist_ok=True)
    record, pending = dest / KEPT_JSON, dest / PENDING
    archived = sorted(
        path.relative_to(dest).as_posix()
        for base, _, names in os.walk(dest) for path in (Path(base, name) for name in names)
        if path not in (record, pending) and not path.is_symlink() and path.is_file())
    pending.write_text(json.dumps({
        "id": job_id, "finished_at": finished_at, "archived": archived,
        "removed_bytes": removed_bytes, "at": time.time()}))
    failed = scratch.remove(gone)
    if failed is not None:
        raise OSError(failed)
    commit(root, job_id)


def commit(root: Path, job_id: str) -> int:
    dest = root / KEPT_ROOT / job_id
    os.replace(dest / PENDING, dest / KEPT_JSON)
    return int(json.loads((dest / KEPT_JSON).read_text())["removed_bytes"])


def pending(root: Path) -> set[str]:
    return {path.parent.name for path in (root / KEPT_ROOT).glob(f"*/{PENDING}")}
