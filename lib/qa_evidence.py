"""Fetch, check, scrub and store a QA lane's evidence (issue #787).

A lane ran pull-request code, so everything it uploaded is untrusted:
result.json, every bundle member's name, type and size, and every byte.
The bundle is unpacked into a staging dir under `root`, checked, scrubbed
and renamed into place only when whole; a refusal leaves nothing behind.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from lib.dispatch.errors_text import scrub_detail
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
# Each header costs a decompression pass and a TarInfo in memory, so a
# bundle of millions of empty members is refused before it costs either.
MAX_MEMBERS = 10_000
_RESERVED = {RESULT_NAME, MANIFEST_NAME}
_CHUNK = 1024 * 1024
_NOTE = (
    "File hashes and sizes are taken after scrubbing. "
    "Artifact checks ran on the bytes the lane uploaded."
)


class EvidenceRefused(Exception):
    """Text is a reason and a scrubbed member or file name, never a link."""


def collect(
    run_id: str, lane: int, *, storage: Storage, repo: str, ticket: str, root: Path
) -> dict[str, Any] | None:
    """Store lane `lane`'s evidence at root/<owner>/<name>/<ticket>/<run_id>/lane-<n>.

    None when the lane uploaded no result.json. Returns the parsed result
    with "mismatches" set to the artifact and scrub findings."""
    check_id(run_id, "run_id")
    check_id(ticket, "ticket")
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise ValueError("repo must be owner/name")
    owner, name = repo.split("/")
    check_id(owner, "repo owner")
    prefix = lane_prefix(repo, run_id, lane)
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("root must be an absolute Path")
    dest = root / owner / name / ticket / run_id / f"lane-{lane}"

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
        unpack.mkdir()
        _unpack(bundle, unpack, BUNDLE_CAP)
        mismatches = _check_artifacts(result.get("artifacts", []), unpack)
        mismatches += _scrub(unpack)
        shutil.copyfile(result_file, unpack / RESULT_NAME)
        _write_manifest(unpack, run_id, lane, result.get("commit"), mismatches)
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
            mismatches.append({"path": path if isinstance(path, str) else "", "reason": "bad path"})
            continue
        file = unpack.joinpath(*parts)
        if not file.is_file():
            mismatches.append({"path": path, "reason": "missing"})
            continue
        if file.stat().st_size != entry.get("size"):
            mismatches.append({"path": path, "reason": "size"})
        if _sha256(file) != entry.get("sha256"):
            mismatches.append({"path": path, "reason": "sha256"})
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
