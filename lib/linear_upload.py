from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, TypeVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lib.brief.qa import AFTER_SCREENSHOT
from lib.poll_source import LINEAR_ENDPOINT, LinearTransport, post_json
from lib.qa_evidence import MANIFEST_NAME, kind

T = TypeVar("T")
CACHE_CONTROL = "public, max-age=31536000"
UPLOAD_CAP_BYTES = 100 * 2**20
CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".webm": "video/webm", ".mp4": "video/mp4",
}
_ISSUE = "query($id: String!) { issue(id: $id) { id } }"
_COMMENTS = """query($id: String!, $after: String) { issue(id: $id) { comments(first: 100, after: $after) {
nodes { id body } pageInfo { hasNextPage endCursor } } } }"""
_FILE_UPLOAD = """mutation($contentType: String!, $filename: String!, $size: Int!) {
fileUpload(contentType: $contentType, filename: $filename, size: $size) {
success uploadFile { uploadUrl assetUrl headers { key value } } } }"""
_COMMENT_CREATE = """mutation($issueId: String!, $body: String!) {
commentCreate(input: {issueId: $issueId, body: $body}) { success comment { id } } }"""


class LinearError(Exception):
    """Text is the operation and an HTTP status or exception type name at most."""


class LinearClient(Protocol):
    def issue_id(self, identifier: str) -> str: ...
    def find_comment(self, issue_id: str, marker: str) -> str | None: ...
    def upload(self, path: Path, filename: str, content_type: str, size: int) -> str: ...
    def comment(self, issue_id: str, body: str) -> str: ...


def put_bytes(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> int:
    try:
        with urlopen(Request(url, data=body, method="PUT", headers=dict(headers)), timeout=timeout) as response:
            return response.status
    except HTTPError as exc:
        exc.close()
        return exc.code
    except (OSError, ValueError, HTTPException) as exc:
        failure = type(exc).__name__
    raise LinearError(f"upload put: {failure}")


@dataclass(frozen=True)
class HttpLinear:
    api_key: str = field(repr=False)
    post: LinearTransport = post_json
    put: Callable[[str, Mapping[str, str], bytes, float], int] = put_bytes
    timeout: float = 60.0

    def issue_id(self, identifier: str) -> str:
        return self._graphql("issue", _ISSUE, {"id": identifier}, lambda data: str(data["issue"]["id"]))

    def find_comment(self, issue_id: str, marker: str) -> str | None:
        after = None
        while True:
            variables = {"id": issue_id, "after": after}
            found, after = self._graphql("comments", _COMMENTS, variables, lambda data: _scan(data, marker))
            if found is not None or after is None:
                return found

    def upload(self, path: Path, filename: str, content_type: str, size: int) -> str:
        data = _redacted("upload read", lambda: _read(path, size))
        if len(data) != size:
            raise LinearError("upload read: size mismatch")
        variables = {"contentType": content_type, "filename": filename, "size": size}
        url, asset, signed = self._graphql("fileUpload", _FILE_UPLOAD, variables, _upload_target)
        defaults = {"Content-Type": content_type, "Cache-Control": CACHE_CONTROL}
        headers = {k: v for k, v in defaults.items() if k.lower() not in {s.lower() for s in signed}} | signed
        status = _redacted("upload put", lambda: self.put(url, headers, data, self.timeout))
        if not 200 <= status < 300:
            raise LinearError(f"upload put: HTTP {status}")
        return asset

    def comment(self, issue_id: str, body: str) -> str:
        return self._graphql("commentCreate", _COMMENT_CREATE, {"issueId": issue_id, "body": body}, _created)

    def _graphql(self, operation: str, query: str, variables: dict[str, Any], read: Callable[[dict], T]) -> T:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        result = _redacted(operation, lambda: self.post(LINEAR_ENDPOINT, headers, payload, self.timeout))
        if result.status != 200:
            raise LinearError(f"{operation}: HTTP {result.status}")
        parsed = _redacted(operation, lambda: json.loads(result.body))
        if not isinstance(parsed, dict) or parsed.get("errors"):
            raise LinearError(f"{operation}: GraphQL error")
        return _redacted(operation, lambda: read(parsed["data"]))


@dataclass(frozen=True)
class Pick:
    lane: int
    path: str
    kind: Literal["screenshot", "video"]
    size: int
    sha256: str
    file: Path

    @property
    def name(self) -> str:
        return f"lane-{self.lane}/{self.path}"

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES[PurePosixPath(self.path).suffix.lower()]


@dataclass(frozen=True)
class Subset:
    uploads: tuple[Pick, ...]
    over_limit: tuple[Pick, ...]
    left_out: tuple[str, ...]


def subset(lanes: Sequence[tuple[int, Path]], cap: int = UPLOAD_CAP_BYTES) -> Subset:
    uploads: list[Pick] = []
    over_limit: list[Pick] = []
    left_out: list[str] = []
    for lane, folder in lanes:
        try:
            files = json.loads((folder / MANIFEST_NAME).read_bytes())["files"]
        except (OSError, ValueError, TypeError, KeyError):
            left_out.append(f"lane-{lane}/{MANIFEST_NAME}: unreadable")
            continue
        for entry in files if isinstance(files, list) else ():
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            rel = PurePosixPath(entry["path"])
            chosen = _chosen(rel)
            if chosen is None:
                continue
            size, sha256 = entry.get("size"), entry.get("sha256")
            if rel.is_absolute() or ".." in rel.parts or type(size) is not int or not isinstance(sha256, str):
                left_out.append(f"lane-{lane}/{entry['path']}: not a safe manifest entry")
                continue
            pick = Pick(lane, entry["path"], chosen, size, sha256, folder / rel)
            if size > cap:
                over_limit.append(pick)
            elif _matches(pick):
                uploads.append(pick)
            else:
                left_out.append(f"{pick.name}: missing, or not the file its manifest lists")
    return Subset(tuple(uploads), tuple(over_limit), tuple(left_out))


def render(
    *,
    run_id: str,
    result: str,
    head: str,
    table: str,
    assets: Sequence[tuple[Pick, str]],
    chosen: Subset,
    report: str,
) -> str:
    parts = [f"QA result: {result} at `{head[:12]}`"]
    if table:
        parts.append(table)
    shots = [f"![{pick.name}]({url})" for pick, url in assets if pick.kind == "screenshot"]
    if shots:
        parts.append("Screenshots:\n\n" + "\n\n".join(shots))
    videos = [f"- [{pick.name}]({url})" for pick, url in assets if pick.kind == "video"]
    if videos:
        parts.append("Videos:\n" + "\n".join(videos))
    left = [f"- {pick.name} ({pick.size} bytes): over the upload limit" for pick in chosen.over_limit]
    left += [f"- {line}" for line in chosen.left_out]
    if left:
        parts.append("Left out:\n" + "\n".join(left))
    parts.append(f"Full QA report: {report}")
    parts.append(f"QA run {run_id}")
    return "\n\n".join(parts) + "\n"


def _chosen(rel: PurePosixPath) -> Literal["screenshot", "video"] | None:
    if len(rel.parts) == 2 and rel.name == AFTER_SCREENSHOT:
        return "screenshot"
    return "video" if kind(rel) == "video" else None


def _matches(pick: Pick) -> bool:
    try:
        with os.fdopen(os.open(pick.file, os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
            if os.fstat(file.fileno()).st_size != pick.size:
                return False
            return hashlib.file_digest(file, "sha256").hexdigest() == pick.sha256
    except OSError:
        return False


@dataclass
class FakeLinear:
    issues: dict[str, str] = field(default_factory=dict)
    comments: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    uploads: list[tuple[Path, str, str, int]] = field(default_factory=list)

    def issue_id(self, identifier: str) -> str:
        return _redacted("issue", lambda: self.issues[identifier])

    def find_comment(self, issue_id: str, marker: str) -> str | None:
        return next((cid for cid, body in self.comments.get(issue_id, []) if _has_line(body, marker)), None)

    def upload(self, path: Path, filename: str, content_type: str, size: int) -> str:
        self.uploads.append((path, filename, content_type, size))
        return f"https://uploads.linear.invalid/{len(self.uploads)}/{filename}"

    def comment(self, issue_id: str, body: str) -> str:
        comment_id = f"comment-{sum(map(len, self.comments.values())) + 1}"
        self.comments.setdefault(issue_id, []).append((comment_id, body))
        return comment_id


def _has_line(body: str, marker: str) -> bool:
    return marker.strip() in (line.strip() for line in body.splitlines())


def _redacted(operation: str, call: Callable[[], T]) -> T:
    try:
        return call()
    except LinearError:
        raise
    except Exception as exc:
        failure = type(exc).__name__
    raise LinearError(f"{operation}: {failure}")


def _read(path: Path, size: int) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
        return file.read(size + 1)


def _scan(data: dict, marker: str) -> tuple[str | None, str | None]:
    comments = data["issue"]["comments"]
    found = next((str(node["id"]) for node in comments["nodes"] if _has_line(str(node["body"]), marker)), None)
    page = comments["pageInfo"]
    return found, str(page["endCursor"]) if found is None and page["hasNextPage"] is True else None


def _upload_target(data: dict) -> tuple[str, str, dict[str, str]]:
    granted = data["fileUpload"]
    if granted["success"] is not True or not granted["uploadFile"]:
        raise LinearError("fileUpload: not granted")
    target = granted["uploadFile"]
    return str(target["uploadUrl"]), str(target["assetUrl"]), {str(h["key"]): str(h["value"]) for h in target["headers"]}


def _created(data: dict) -> str:
    if data["commentCreate"]["success"] is not True:
        raise LinearError("commentCreate: not created")
    return str(data["commentCreate"]["comment"]["id"])
