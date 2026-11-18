from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lib.poll_source import LINEAR_ENDPOINT, LinearTransport, post_json

T = TypeVar("T")
CACHE_CONTROL = "public, max-age=31536000"
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
