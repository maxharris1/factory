"""Signed links for QA lanes (docs/SPEC.md section 7).

A signed link is a credential: no repr, error text or log line here ever
holds one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

RESULT_NAME = "result.json"
BUNDLE_NAME = "bundle.tar.gz"
RESULT_CAP = 1024 * 1024
BUNDLE_CAP = 500 * 1024 * 1024

_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_KEY = re.compile(r"[A-Za-z0-9._/-]+")
_ALGORITHM = "AWS4-HMAC-SHA256"
_MAX_EXPIRES = 7 * 24 * 3600
_FETCH_EXPIRES = 300
_FETCH_TIMEOUT = 60
_CHUNK = 1024 * 1024


class StorageError(Exception):
    """Text is the operation, the key and an HTTP status at most."""


def check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{what} must match [A-Za-z0-9._-]{{1,64}} and not be . or ..")
    return value


def lane_prefix(repo: str, run_id: str, lane: int) -> str:
    name = check_id(repo.split("/")[-1], "repo name")
    if not isinstance(lane, int) or isinstance(lane, bool) or lane < 1:
        raise ValueError("lane must be a positive integer")
    return f"qa/{name}/{check_id(run_id, 'run_id')}/lane-{lane}/"


def pack_key(pack_id: str) -> str:
    return f"packs/{check_id(pack_id, 'pack_id')}"


@dataclass(frozen=True)
class UploadLink:
    """A POST form: the lane posts `fields` then the file to `url`."""

    key: str
    max_bytes: int
    expires_s: int
    url: str = field(repr=False)
    fields: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class DownloadLink:
    key: str
    expires_s: int
    url: str = field(repr=False)


@dataclass(frozen=True)
class LaneLinks:
    result: UploadLink
    bundle: UploadLink
    packs: tuple[DownloadLink, ...]


class Storage(Protocol):
    def sign_upload(self, key: str, max_bytes: int, expires_s: int) -> UploadLink: ...

    def sign_download(self, key: str, expires_s: int) -> DownloadLink: ...

    def fetch(self, key: str, dest: Path, max_bytes: int) -> bool: ...


def links_for_lane(run_id: str, lane: int, pack_ids: Iterable[str], timeout: int, *,
                   storage: Storage, repo: str) -> LaneLinks:
    prefix = lane_prefix(repo, run_id, lane)
    packs = [pack_key(pack_id) for pack_id in pack_ids]
    return LaneLinks(
        result=storage.sign_upload(prefix + RESULT_NAME, RESULT_CAP, timeout),
        bundle=storage.sign_upload(prefix + BUNDLE_NAME, BUNDLE_CAP, timeout),
        packs=tuple(storage.sign_download(key, timeout) for key in packs),
    )


def _check_key(key: object) -> str:
    if (not isinstance(key, str) or _KEY.fullmatch(key) is None
            or ".." in key or key.startswith("/")):
        raise ValueError("storage key must match [A-Za-z0-9._/-]+, hold no .. and not start with /")
    return key


def _check_expires(expires_s: object) -> int:
    if not isinstance(expires_s, int) or isinstance(expires_s, bool) or not 1 <= expires_s <= _MAX_EXPIRES:
        raise ValueError(f"expires_s must be an integer in 1..{_MAX_EXPIRES}")
    return expires_s


def _uri_encode(text: str, *, keep_slash: bool) -> str:
    return quote(text, safe="-_.~/" if keep_slash else "-_.~")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str) -> bytes:
    key = _hmac(f"AWS4{secret}".encode(), date)
    for part in (region, "s3", "aws4_request"):
        key = _hmac(key, part)
    return key


def _presign_query(*, host: str, encoded_path: str, region: str, access_key: str, secret_key: str,
                   when: datetime, expires_s: int) -> str:
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    params = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{access_key}/{date}/{region}/s3/aws4_request",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires_s),
        "X-Amz-SignedHeaders": "host",
    }
    query = "&".join(f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}"
                     for k, v in sorted(params.items()))
    canonical = "\n".join(["GET", encoded_path, query, f"host:{host}", "", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join([_ALGORITHM, amz_date, f"{date}/{region}/s3/aws4_request",
                         hashlib.sha256(canonical.encode()).hexdigest()])
    signature = hmac.new(_signing_key(secret_key, date, region), to_sign.encode(),
                         hashlib.sha256).hexdigest()
    return f"{query}&X-Amz-Signature={signature}"


def _host(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    default = {"http": 80, "https": 443}[parts.scheme]
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    # http.client leaves a default port out of the Host header, so the signed host must too.
    return host if parts.port in (None, default) else f"{host}:{parts.port}"


class S3Storage:
    """S3 or an S3-compatible store, addressed path-style, signed with SigV4."""

    def __init__(self, *, bucket: str, region: str, access_key_id: str, secret_key: str,
                 endpoint: str | None = None,
                 opener: Callable[..., Any] = urlopen,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        endpoint = (endpoint or f"https://s3.{region}.amazonaws.com").rstrip("/")
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("endpoint must be an http or https URL")
        self.bucket = bucket
        self.region = region
        self._endpoint = endpoint
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._host = _host(endpoint)
        self._base_path = parts.path
        self._access_key_id = access_key_id
        self._secret_key = secret_key
        self._opener = opener
        self._now = now

    @classmethod
    def from_entry(cls, entry: Any, environ: Mapping[str, str] = os.environ, **kwargs: Any) -> S3Storage:
        def secret(name: str) -> str:
            value = environ.get(name)
            if not value:
                raise StorageError(f"environment variable {name} is not set")
            return value

        return cls(bucket=entry.bucket, region=entry.region, endpoint=entry.endpoint,
                   access_key_id=secret(entry.access_key_env),
                   secret_key=secret(entry.secret_key_env), **kwargs)

    def sign_upload(self, key: str, max_bytes: int, expires_s: int) -> UploadLink:
        _check_key(key)
        _check_expires(expires_s)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        when = self._now().astimezone(timezone.utc)
        amz_date = when.strftime("%Y%m%dT%H%M%SZ")
        date = amz_date[:8]
        credential = f"{self._access_key_id}/{date}/{self.region}/s3/aws4_request"
        policy = {
            "expiration": (when + timedelta(seconds=expires_s)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conditions": [
                {"bucket": self.bucket},
                {"key": key},
                ["content-length-range", 0, max_bytes],
                {"x-amz-algorithm": _ALGORITHM},
                {"x-amz-credential": credential},
                {"x-amz-date": amz_date},
            ],
        }
        encoded = base64.b64encode(json.dumps(policy, separators=(",", ":")).encode()).decode()
        signature = hmac.new(_signing_key(self._secret_key, date, self.region), encoded.encode(),
                             hashlib.sha256).hexdigest()
        fields = {
            "key": key,
            "policy": encoded,
            "x-amz-algorithm": _ALGORITHM,
            "x-amz-credential": credential,
            "x-amz-date": amz_date,
            "x-amz-signature": signature,
        }
        url = f"{self._endpoint}/{_uri_encode(self.bucket, keep_slash=False)}"
        return UploadLink(key=key, max_bytes=max_bytes, expires_s=expires_s, url=url, fields=fields)

    def sign_download(self, key: str, expires_s: int) -> DownloadLink:
        _check_key(key)
        _check_expires(expires_s)
        path = _uri_encode(f"{self._base_path}/{self.bucket}/{key}", keep_slash=True)
        query = _presign_query(host=self._host, encoded_path=path, region=self.region,
                               access_key=self._access_key_id, secret_key=self._secret_key,
                               when=self._now().astimezone(timezone.utc), expires_s=expires_s)
        url = f"{self._origin}{path}?{query}"
        return DownloadLink(key=key, expires_s=expires_s, url=url)

    def fetch(self, key: str, dest: Path, max_bytes: int) -> bool:
        url = self.sign_download(key, _FETCH_EXPIRES).url
        dest = Path(dest)
        partial = dest.with_name(f".{dest.name}.partial")
        total = 0
        failure: int | str | None = None
        try:
            with self._opener(url, timeout=_FETCH_TIMEOUT) as response, open(partial, "wb") as out:
                while chunk := response.read(min(_CHUNK, max_bytes + 1 - total)):
                    total += len(chunk)
                    if total > max_bytes:
                        failure = f"more than {max_bytes} bytes"
                        break
                    out.write(chunk)
        except HTTPError as err:
            failure = err.code
            err.close()
        except (OSError, HTTPException):
            failure = "failed"
        # The handlers only record what happened: an exception raised inside
        # them would chain the original, whose text holds the signed URL.
        if failure is None:
            os.replace(partial, dest)
            return True
        partial.unlink(missing_ok=True)
        if failure == 404:
            return False
        raise StorageError(f"fetch {key}: HTTP {failure}" if isinstance(failure, int)
                           else f"fetch {key}: {failure}")
