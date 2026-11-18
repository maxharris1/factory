"""Fetch, check, scrub and store a QA lane's evidence (docs/SPEC.md section 7).

Everything a lane uploads is untrusted. The bundle is staged under `root`
and renamed into place only when whole; a refusal leaves nothing behind.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, BinaryIO, Literal, get_args

from lib.dispatch.errors_text import scrub_detail
from lib.qa_audit import canonical_path
from lib.qa_storage import (
    BUNDLE_CAP,
    BUNDLE_NAME,
    RESULT_CAP,
    RESULT_NAME,
    Storage,
    check_id,
    lane_prefix,
)

MANIFEST_NAME = "manifest.json"
NETWORK_NAME = "network.json"
MAX_MEMBERS = 10_000
_RESERVED = {RESULT_NAME, MANIFEST_NAME}
_CHUNK = 1024 * 1024
_NOTE = (
    "File hashes and sizes are taken after scrubbing. "
    "Artifact checks ran on the bytes the lane uploaded."
)


RETENTION = 7 * 86400.0
CLOSED_STATES = ("merged", "done", "abandoned")
_LANE = re.compile(r"lane-[1-9][0-9]*")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SEARCH_DIR_FLAGS = getattr(os, "O_PATH", getattr(os, "O_SEARCH", os.O_RDONLY)) | os.O_DIRECTORY | os.O_NOFOLLOW
_NOFOLLOW_ANY = getattr(os, "O_NOFOLLOW_ANY", 0)
_NO_LINK_PATH_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW_ANY

Standing = Literal["latest", "superseded", "expired"]
Kind = Literal["screenshot", "video", "trace", "log", "manifest", "other"]
Outcome = Literal["deleted", "symlink-left", "lane-refused"]
_LEFT_IN_PLACE: dict[Outcome, str] = {
    "symlink-left": "symlink left in place: {}",
    "lane-refused": "result.json repeats a scenario id, lane left in place: {}",
}

_SUFFIXES: dict[Kind, str] = {
    "screenshot": ".png .jpg .jpeg .webp",
    "video": ".webm .mp4",
    "trace": ".zip",
    "log": ".log",
}
_KIND_BY_SUFFIX = {suffix: kind for kind, suffixes in _SUFFIXES.items() for suffix in suffixes.split()}
_ALL_KINDS: frozenset[Kind] = frozenset(get_args(Kind))
_MANIFEST_ONLY: frozenset[Kind] = frozenset({"manifest"})
_KEPT: dict[tuple[Standing, str | None], frozenset[Kind]] = {
    ("latest", "verified"): _ALL_KINDS - {"trace", "log"},
    ("superseded", "failed"): _ALL_KINDS,
}
_KEPT_OTHERWISE: dict[Standing, frozenset[Kind]] = {
    "latest": _ALL_KINDS,
    "superseded": _MANIFEST_ONLY,
    "expired": _MANIFEST_ONLY,
}


class EvidenceRefused(Exception):
    """Text is a reason and a scrubbed member or file name, never a link."""


@dataclass(frozen=True)
class Recycled:
    """`deleted` and `removed` hold posix paths relative to the evidence root."""

    deleted: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunFile:
    """`path` is posix and relative to the run folder: lane-1/s-1/shot.png."""

    path: str
    size: int
    mtime_ns: int


def run_dir(root: Path, repo: str, ticket: str, run_id: str) -> Path:
    check_id(run_id, "run_id")
    check_id(ticket, "ticket")
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise ValueError("repo must be owner/name")
    owner, name = repo.split("/")
    check_id(owner, "repo owner")
    check_id(name, "repo name")
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("root must be an absolute Path")
    return root / owner / name / ticket / run_id


def lane_dir(root: Path, repo: str, ticket: str, run_id: str, lane: int) -> Path:
    return run_dir(root, repo, ticket, run_id) / PurePosixPath(lane_prefix(repo, run_id, lane)).name


def stored(folder: Path) -> dict[str, Any]:
    result = json.loads((folder / RESULT_NAME).read_bytes())
    manifest = json.loads((folder / MANIFEST_NAME).read_bytes())
    return {**result, "mismatches": manifest["mismatches"]}


def record_evidence_dir(connection: sqlite3.Connection, run_id: str, folder: Path) -> None:
    connection.execute(
        "UPDATE qa_runs SET evidence_dir = ? WHERE run_id = ? AND evidence_dir IS NULL",
        (str(folder), run_id),
    )


def collect(
    run_id: str, lane: int, *, storage: Storage, repo: str, ticket: str, root: Path
) -> dict[str, Any] | None:
    """Store lane `lane`'s evidence at root/<owner>/<name>/<ticket>/<run_id>/lane-<n>.

    None when the lane uploaded no result.json. Returns the parsed result
    with "mismatches" set to the artifact and scrub findings."""
    dest = lane_dir(root, repo, ticket, run_id, lane)
    prefix = lane_prefix(repo, run_id, lane)

    staging_root = root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=staging_root))
    try:
        result_file = staging / RESULT_NAME
        if not storage.fetch(prefix + RESULT_NAME, result_file, RESULT_CAP):
            return None
        result = _read_result(result_file, run_id, lane)
        if dest.exists():
            raise EvidenceRefused(f"evidence for lane-{lane} is already stored")
        bundle = staging / BUNDLE_NAME
        if not storage.fetch(prefix + BUNDLE_NAME, bundle, BUNDLE_CAP):
            raise EvidenceRefused(f"{BUNDLE_NAME} is missing")
        unpack = staging / "unpack"
        mismatches = _intake(bundle, result_file, result, unpack, run_id, lane)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(unpack, dest)
        except OSError:
            if dest.exists():
                raise EvidenceRefused(f"evidence for lane-{lane} is already stored") from None
            raise
        return {**result, "mismatches": mismatches}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _intake(
    bundle: Path, result_file: Path, result: dict[str, Any], unpack: Path, run_id: str, lane: int
) -> list[dict[str, str]]:
    try:
        unpack.mkdir()
        _unpack(bundle, unpack, BUNDLE_CAP)
        mismatches = _check_artifacts(result.get("artifacts", []), unpack)
        mismatches += _scrub(unpack)
        shutil.copyfile(result_file, unpack / RESULT_NAME)
        _write_manifest(unpack, run_id, lane, result.get("commit"), mismatches)
        return mismatches
    except Exception as exc:
        reason = str(exc) if isinstance(exc, EvidenceRefused) else f"evidence intake failed ({type(exc).__name__})"
    raise EvidenceRefused(reason)


def check_members(tar: tarfile.TarFile, cap: int) -> list[tarfile.TarInfo]:
    """Every header in `tar`, read before anything is written.

    Refuses absolute names, `..` parts, links, anything but regular files
    and directories, a reserved top-level name that is not a plain file,
    and declared sizes summing over twice `cap`."""
    members: list[tarfile.TarInfo] = []
    total = 0
    for member in tar:
        if len(members) == MAX_MEMBERS:
            raise EvidenceRefused(f"bundle has more than {MAX_MEMBERS} members")
        shown = _shown(member.name)
        path = PurePosixPath(member.name)
        if path.is_absolute():
            raise EvidenceRefused(f"absolute member name: {shown}")
        if ".." in path.parts:
            raise EvidenceRefused(f"member name with ..: {shown}")
        if member.issym():
            raise EvidenceRefused(f"symlink member: {shown}")
        if member.islnk():
            raise EvidenceRefused(f"hardlink member: {shown}")
        if not (member.isfile() or member.isdir()):
            raise EvidenceRefused(f"member is not a regular file or directory: {shown}")
        if member.isfile() and not path.parts:
            raise EvidenceRefused("file member with an empty name")
        if path.parts and path.parts[0] in _RESERVED and (len(path.parts) > 1 or member.isdir()):
            raise EvidenceRefused(f"reserved name used as a directory: {shown}")
        if member.isfile():
            total += member.size
            if total > 2 * cap:
                raise EvidenceRefused(f"bundle unpacks to more than {2 * cap} bytes at member: {shown}")
        members.append(member)
    return members


def _unpack(bundle: Path, unpack: Path, cap: int) -> None:
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            _extract(tar, check_members(tar, cap), unpack, 2 * cap)
    except (tarfile.ReadError, tarfile.CompressionError, gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise EvidenceRefused(f"{BUNDLE_NAME} is not a readable tar.gz ({type(exc).__name__})") from None


def _extract(tar: tarfile.TarFile, members: list[tarfile.TarInfo], unpack: Path, limit: int) -> None:
    written = 0
    for member in members:
        shown = _shown(member.name)
        try:
            member = tarfile.data_filter(member, str(unpack))
            target = unpack.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, open(target, "xb") as out:
                while chunk := source.read(_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        raise EvidenceRefused(f"bundle unpacks to more than {limit} bytes at member: {shown}")
                    out.write(chunk)
        except tarfile.FilterError:
            raise EvidenceRefused(f"data filter refused member: {shown}") from None
        except (FileExistsError, NotADirectoryError, IsADirectoryError):
            raise EvidenceRefused(f"member collides with another member: {shown}") from None


def _read_result(file: Path, run_id: str, lane: int) -> dict[str, Any]:
    try:
        result = json.loads(file.read_bytes())
    except (ValueError, RecursionError):
        raise EvidenceRefused(f"{RESULT_NAME} is not valid JSON") from None
    if not isinstance(result, dict):
        raise EvidenceRefused(f"{RESULT_NAME} is not a JSON object")
    if result.get("run_id") != run_id or type(result.get("lane")) is not int or result["lane"] != lane:
        raise EvidenceRefused(f"{RESULT_NAME} names another run or lane")
    if not isinstance(result.get("artifacts", []), list):
        raise EvidenceRefused(f"{RESULT_NAME} artifacts is not a list")
    return result


def _check_artifacts(artifacts: list[Any], unpack: Path) -> list[dict[str, str]]:
    mismatches = []
    for entry in artifacts:
        path = entry.get("path") if isinstance(entry, dict) else None
        parts = _safe_parts(path)
        if parts is None:
            mismatches.append({"path": canonical_path(path) if isinstance(path, str) else "", "reason": "bad path"})
            continue
        canonical = canonical_path(path)
        file = unpack.joinpath(*parts)
        if not file.is_file():
            mismatches.append({"path": canonical, "reason": "missing"})
            continue
        if file.stat().st_size != entry.get("size"):
            mismatches.append({"path": canonical, "reason": "size"})
        if _sha256(file) != entry.get("sha256"):
            mismatches.append({"path": canonical, "reason": "sha256"})
    return mismatches


def _safe_parts(path: object) -> tuple[str, ...] | None:
    if not isinstance(path, str) or "\x00" in path:
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    return pure.parts


def _scrub(unpack: Path) -> list[dict[str, str]]:
    mismatches = []
    for file in sorted(p for p in unpack.rglob("*") if p.is_file()):
        if file.name.endswith(".log"):
            _scrub_log(file)
        elif file.name == NETWORK_NAME and not _scrub_network(file):
            file.unlink()
            mismatches.append({"path": file.relative_to(unpack).as_posix(), "reason": "invalid network.json"})
    return mismatches


def _scrub_log(file: Path) -> None:
    fd, scrubbed = tempfile.mkstemp(dir=file.parent)
    with open(file, encoding="utf-8", errors="replace") as source, open(fd, "w", encoding="utf-8") as out:
        for line in source:
            out.write(scrub_detail(line) + "\n")
    os.replace(scrubbed, file)


def _scrub_network(file: Path) -> bool:
    try:
        data = json.loads(file.read_bytes())
        _drop_request_headers(data)
    except (ValueError, RecursionError):
        return False
    file.write_text(json.dumps(data), encoding="utf-8")
    return True


def _drop_request_headers(node: object, in_request: bool = False) -> None:
    if isinstance(node, dict):
        node.pop("requestHeaders", None)
        if in_request:
            node.pop("headers", None)
        for key, value in node.items():
            _drop_request_headers(value, key == "request")
    elif isinstance(node, list):
        for item in node:
            _drop_request_headers(item)


def _write_manifest(
    unpack: Path, run_id: str, lane: int, commit: object, mismatches: list[dict[str, str]]
) -> None:
    files = sorted(
        (
            {"path": file.relative_to(unpack).as_posix(), "sha256": _sha256(file), "size": file.stat().st_size}
            for file in unpack.rglob("*")
            if file.is_file() and file != unpack / MANIFEST_NAME
        ),
        key=lambda item: item["path"],
    )
    manifest = {
        "run_id": run_id,
        "lane": lane,
        "commit": commit,
        "files": files,
        "mismatches": mismatches,
        "note": _NOTE,
    }
    (unpack / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _sha256(file: Path) -> str:
    with open(file, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _shown(name: str) -> str:
    return scrub_detail(name) or "<empty name>"


def kind(lane_rel: PurePosixPath) -> Kind:
    if len(lane_rel.parts) == 1 and lane_rel.name in _RESERVED:
        return "manifest"
    if lane_rel.name == NETWORK_NAME:
        return "log"
    kind = _KIND_BY_SUFFIX.get(lane_rel.suffix.lower())
    if kind is None and lane_rel.name.startswith("trace"):
        return "trace"
    return kind or "other"


def keeps(standing: Standing, scenario_result: str | None, kind: Kind) -> bool:
    return kind in _KEPT.get((standing, scenario_result), _KEPT_OTHERWISE[standing])


def recycle(
    connection: sqlite3.Connection, *, root: Path, repo: str, now: float, budget_bytes: int | None = None
) -> Recycled:
    """Delete what the keep table rejects from each finished run's folder.

    Then, while the repo's run folders total more than `budget_bytes`,
    remove whole runs of closed tickets, oldest first. A run is found by its
    recorded folder only. A folder that is not exactly
    root/<owner>/<name>/<ticket>/<run_id>, with no link on the way, is
    skipped and reported. A missing folder is nothing to do."""
    closed = ", ".join(f"'{state}'" for state in CLOSED_STATES)
    rows = connection.execute(
        f"""
        SELECT r.run_id, r.evidence_dir, CASE
            WHEN EXISTS (
                SELECT 1 FROM ticket_state s
                WHERE lower(s.repo) = r.repo AND s.ticket_id = r.ticket_id
                    AND s.workflow_state IN ({closed})
            ) AND (
                SELECT max(t.at) FROM ticket_transitions t
                WHERE lower(t.repo) = r.repo AND t.ticket_id = r.ticket_id
                    AND t.to_state IN ({closed})
            ) <= :cutoff THEN 'expired'
            WHEN EXISTS (
                SELECT 1 FROM qa_runs n
                WHERE n.repo = r.repo AND n.ticket_id = r.ticket_id AND n.arm = r.arm
                    AND n.state = 'reported' AND n.started_at > r.started_at
            ) THEN 'superseded'
            ELSE 'latest'
        END
        FROM qa_runs r
        WHERE r.repo = :repo AND r.evidence_dir IS NOT NULL
            AND r.state NOT IN ('queued', 'running')
        ORDER BY r.started_at, r.rowid
        """,
        {"repo": repo.lower(), "cutoff": now - RETENTION},
    ).fetchall()
    deleted: list[str] = []
    skipped: list[tuple[str, str]] = []
    real_root = os.path.realpath(root)
    try:
        root_fd = _open_root(real_root)
    except OSError as exc:
        reason = f"cannot open evidence root ({exc.strerror})"
        return Recycled(deleted=(), skipped=tuple((run_id, reason) for run_id, _folder, _standing in rows))
    removed: list[str] = []
    try:
        for run_id, evidence_dir, standing in rows:
            parts = _resolve(run_id, evidence_dir, real_root)
            if isinstance(parts, str):
                skipped.append((run_id, parts))
                continue
            try:
                run_fd = _open_unlinked(root_fd, parts)
            except FileNotFoundError:
                continue
            except OSError as exc:
                skipped.append((run_id, f"cannot open evidence folder ({exc.strerror})"))
                continue
            try:
                for outcome, rel in _recycle_run(run_fd, standing):
                    shown = PurePosixPath(*parts, rel).as_posix()
                    if outcome == "deleted":
                        deleted.append(shown)
                    else:
                        skipped.append((run_id, _LEFT_IN_PLACE[outcome].format(shown)))
            except OSError as exc:
                skipped.append((run_id, f"stopped partway ({exc.strerror})"))
            finally:
                os.close(run_fd)
        if budget_bytes is not None:
            removed, refused = _remove_oldest_closed_runs(connection, repo, root_fd, real_root, budget_bytes)
            skipped += refused
    finally:
        os.close(root_fd)
    return Recycled(deleted=tuple(deleted), skipped=tuple(dict.fromkeys(skipped)), removed=tuple(removed))


def _remove_oldest_closed_runs(
    connection: sqlite3.Connection, repo: str, root_fd: int, real_root: str, budget: int
) -> tuple[list[str], list[tuple[str, str]]]:
    removed: list[str] = []
    skipped: list[tuple[str, str]] = []
    closed = ", ".join(f"'{state}'" for state in CLOSED_STATES)
    rows = connection.execute(
        f"""
        SELECT r.run_id, r.evidence_dir, r.state NOT IN ('queued', 'running') AND EXISTS (
            SELECT 1 FROM ticket_state s
            WHERE lower(s.repo) = r.repo AND s.ticket_id = r.ticket_id
                AND s.workflow_state IN ({closed})
        ) AS removable
        FROM qa_runs r
        WHERE r.repo = ? AND r.evidence_dir IS NOT NULL
        ORDER BY r.started_at, r.rowid
        """,
        (repo.lower(),),
    ).fetchall()
    runs = [(run_id, _resolve(run_id, folder, real_root), removable) for run_id, folder, removable in rows]
    sizes: dict[str, int] = {}
    for run_id, parts, _ in runs:
        if isinstance(parts, str):
            continue
        try:
            sizes[run_id] = _size(root_fd, parts)
        except OSError as exc:
            skipped.append((run_id, f"cannot measure evidence folder ({exc.strerror or exc})"))
    usage = sum(sizes.values())
    for run_id, parts, removable in runs:
        if usage <= budget:
            break
        if not removable:
            continue
        if isinstance(parts, str):
            skipped.append((run_id, parts))
            continue
        if run_id not in sizes:
            continue
        try:
            ticket_fd = _open_unlinked(root_fd, parts[:-1])
            try:
                shutil.rmtree(run_id, dir_fd=ticket_fd)
            finally:
                os.close(ticket_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            skipped.append((run_id, f"cannot remove evidence folder ({exc.strerror or exc})"))
            try:
                remaining = _size(root_fd, parts)
            except OSError:
                remaining = 0
            usage -= sizes[run_id] - remaining
            continue
        usage -= sizes[run_id]
        removed.append(PurePosixPath(*parts).as_posix())
    return removed, skipped


def _size(root_fd: int, parts: tuple[str, ...]) -> int:
    try:
        fd = _open_unlinked(root_fd, parts)
    except FileNotFoundError:
        return 0
    try:
        return _tree_bytes(fd)
    finally:
        os.close(fd)


def _tree_bytes(fd: int) -> int:
    total = 0
    with os.scandir(fd) as scan:
        entries = list(scan)
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            child = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
            try:
                total += _tree_bytes(child)
            finally:
                os.close(child)
        elif entry.is_file(follow_symlinks=False):
            total += entry.stat(follow_symlinks=False).st_size
    return total


def _resolve(run_id: str, evidence_dir: object, real_root: str) -> tuple[str, ...] | str:
    if not isinstance(evidence_dir, str) or "\x00" in evidence_dir or not os.path.isabs(evidence_dir):
        return "evidence folder is not an absolute path"
    if os.path.realpath(evidence_dir) != evidence_dir:
        return "evidence folder path has a link or .. in it"
    folder = PurePath(evidence_dir)
    if not folder.is_relative_to(real_root):
        return "evidence folder is outside the evidence root"
    parts = folder.relative_to(real_root).parts
    if len(parts) != 4:
        return "evidence folder is not at <owner>/<name>/<ticket>/<run_id>"
    if parts[-1] != run_id:
        return "evidence folder belongs to another run id"
    return parts


def _open_root(real_root: str) -> int:
    if _NOFOLLOW_ANY:
        return os.open(real_root, _NO_LINK_PATH_FLAGS)
    top = os.open("/", _SEARCH_DIR_FLAGS)
    try:
        return _open_unlinked(top, PurePath(real_root).parts[1:])
    finally:
        os.close(top)


def _open_unlinked(start_fd: int, parts: tuple[str, ...]) -> int:
    if _NOFOLLOW_ANY:
        return os.open(os.path.join(".", *parts), _NO_LINK_PATH_FLAGS, dir_fd=start_fd)
    fd = os.dup(start_fd)
    try:
        for depth, part in enumerate(parts, 1):
            child = os.open(part, _DIR_FLAGS if depth == len(parts) else _SEARCH_DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _recycle_run(run_fd: int, standing: Standing) -> Iterator[tuple[Outcome, PurePosixPath]]:
    with os.scandir(run_fd) as entries:
        lanes = sorted(e.name for e in entries if _LANE.fullmatch(e.name) and e.is_dir(follow_symlinks=False))
    for lane in lanes:
        lane_fd = os.open(lane, _DIR_FLAGS, dir_fd=run_fd)
        try:
            results = _scenario_results(lane_fd)
            if results is None:
                yield "lane-refused", PurePosixPath(lane)
                continue

            def keep(rel: PurePosixPath) -> bool:
                result = results.get(rel.parts[0]) if len(rel.parts) > 1 else None
                return keeps(standing, result, kind(rel))

            for outcome, rel in _sweep(lane_fd, PurePosixPath(), keep):
                yield outcome, PurePosixPath(lane, rel)
        finally:
            os.close(lane_fd)


def _sweep(
    fd: int, rel: PurePosixPath, keep: Callable[[PurePosixPath], bool]
) -> Iterator[tuple[Outcome, PurePosixPath]]:
    with os.scandir(fd) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        path = rel / entry.name
        if entry.is_symlink():
            yield "symlink-left", path
        elif entry.is_dir(follow_symlinks=False):
            child = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
            try:
                yield from _sweep(child, path, keep)
            finally:
                os.close(child)
        elif entry.is_file(follow_symlinks=False) and not keep(path):
            os.unlink(entry.name, dir_fd=fd)
            yield "deleted", path


def _scenario_results(lane_fd: int) -> dict[str, str] | None:
    try:
        fd = os.open(RESULT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=lane_fd)
    except OSError:
        return {}
    try:
        with open(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {}
            data = json.loads(handle.read(RESULT_CAP + 1))
    except (OSError, ValueError, RecursionError):
        return {}
    scenarios = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(scenarios, list):
        return {}
    entries = [
        (item["id"], item["result"])
        for item in scenarios
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("result"), str)
    ]
    results = dict(entries)
    return results if len(results) == len(entries) else None


def walk_run(root_fd: int, parts: tuple[str, ...]) -> tuple[RunFile, ...]:
    """Every file in the run folder's lane-N folders, sorted. Anything but a
    directory or a regular file with one link is refused."""
    run_fd = _open_unlinked(root_fd, parts)
    try:
        return tuple(sorted(_walk(run_fd, PurePosixPath(), top=True), key=lambda file: file.path))
    finally:
        os.close(run_fd)


def _walk(fd: int, rel: PurePosixPath, top: bool = False) -> Iterator[RunFile]:
    with os.scandir(fd) as scan:
        entries = list(scan)
    for entry in entries:
        if top and not _LANE.fullmatch(entry.name):
            continue
        path = rel / entry.name
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(entry.name, _DIR_FLAGS, dir_fd=fd)
            try:
                yield from _walk(child, path)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            yield RunFile(path.as_posix(), info.st_size, info.st_mtime_ns)
        else:
            raise EvidenceRefused(f"{_not_plain(info)}: {_shown(path.as_posix())}")


def _not_plain(info: os.stat_result) -> str:
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISREG(info.st_mode):
        return "hard-linked file"
    return "not a regular file"


def write_run_tar(
    out: BinaryIO, root_fd: int, parts: tuple[str, ...], files: tuple[RunFile, ...], index_html: bytes
) -> None:
    """An uncompressed tar of `files` as walked, then `index.html` last.
    A file that is no longer the one walked is refused."""
    run_fd = _open_unlinked(root_fd, parts)
    try:
        with tarfile.open(fileobj=out, mode="w|") as tar:
            for file in files:
                with _reopen(run_fd, file) as handle:
                    tar.addfile(_member(file.path, file.size, file.mtime_ns // 1_000_000_000), handle)
            tar.addfile(_member("index.html", len(index_html), int(time.time())), io.BytesIO(index_html))
    finally:
        os.close(run_fd)


def _reopen(run_fd: int, file: RunFile) -> BinaryIO:
    *folders, name = file.path.split("/")
    try:
        dir_fd = _open_unlinked(run_fd, tuple(folders))
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise EvidenceRefused(f"cannot reopen ({exc.strerror}): {_shown(file.path)}") from None
    info = os.fstat(fd)
    if not (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and (info.st_size, info.st_mtime_ns) == (file.size, file.mtime_ns)
    ):
        os.close(fd)
        raise EvidenceRefused(f"changed since it was listed: {_shown(file.path)}")
    return open(fd, "rb")


def _member(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = 0o644
    member.mtime = mtime
    return member
