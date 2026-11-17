from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import socket
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote_to_bytes, urlsplit

import pytest

from lib.qa_storage import (
    BUNDLE_CAP,
    RESULT_CAP,
    DownloadLink,
    LaneLinks,
    S3Storage,
    StorageError,
    UploadLink,
    _presign_query,
    links_for_lane,
)

AWS_ACCESS = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
NOW = datetime(2026, 11, 17, 12, 30, 5, tzinfo=timezone.utc)
KEY = "qa/factory/run-1/lane-2/result.json"

# AWS S3 docs, "Authenticating Requests: Using Query Parameters (AWS Signature Version 4)".
AWS_GET_EXAMPLE = (
    "https://examplebucket.s3.amazonaws.com/test.txt"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str) -> bytes:
    return _hmac(_hmac(_hmac(_hmac(f"AWS4{secret}".encode(), date), region), "s3"), "aws4_request")


def _expected_get_signature(url: str, secret: str) -> str:
    parts = urlsplit(url)
    default_port = {"http": 80, "https": 443}[parts.scheme]
    host = parts.hostname if parts.port in (None, default_port) else f"{parts.hostname}:{parts.port}"
    params = dict(parse_qsl(parts.query))
    params.pop("X-Amz-Signature")
    query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))
    path = quote(unquote_to_bytes(parts.path), safe="/-._~")
    canonical = f"GET\n{path}\n{query}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
    _, date, region, _, _ = params["X-Amz-Credential"].split("/")
    to_sign = (f"AWS4-HMAC-SHA256\n{params['X-Amz-Date']}\n{date}/{region}/s3/aws4_request\n"
               f"{hashlib.sha256(canonical.encode()).hexdigest()}")
    return hmac.new(_signing_key(secret, date, region), to_sign.encode(), hashlib.sha256).hexdigest()


def _signature(url: str) -> str:
    return dict(parse_qsl(urlsplit(url).query))["X-Amz-Signature"]


class FakeStorage:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def sign_upload(self, key: str, max_bytes: int, expires_s: int) -> UploadLink:
        self.calls.append(("upload", key))
        return UploadLink(key, max_bytes, expires_s, url=f"mem://{key}", fields={"key": key})

    def sign_download(self, key: str, expires_s: int) -> DownloadLink:
        self.calls.append(("download", key))
        return DownloadLink(key, expires_s, url=f"mem://{key}")

    def fetch(self, key: str, dest: Path, max_bytes: int) -> bool:
        raise AssertionError("not used")


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = io.BytesIO(body)
        self.served = 0

    def read(self, n: int) -> bytes:
        chunk = self.body.read(n)
        self.served += len(chunk)
        return chunk

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class Opener:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.urls: list[str] = []
        self.response: FakeResponse | None = None

    def __call__(self, url: str, timeout: float) -> FakeResponse:
        assert timeout > 0
        self.urls.append(url)
        if isinstance(self.outcome, bytes):
            self.response = FakeResponse(self.outcome)
            return self.response
        raise self.outcome(url)


def _storage(opener=None, endpoint: str | None = None) -> S3Storage:
    return S3Storage(bucket="qa-bucket", region="us-east-1", endpoint=endpoint,
                     access_key_id=AWS_ACCESS, secret_key=AWS_SECRET,
                     opener=opener or Opener(b""), now=lambda: NOW)


def test_ac1_links_for_lane_signs_two_capped_uploads_and_one_download_per_pack():
    storage = FakeStorage()

    links = links_for_lane("run-42", 3, ["pack-a", "pack-b"], 1800, storage=storage, repo="acme/widgets")

    uploads = [links.result, links.bundle]
    assert all("run-42" in link.key and "lane-3" in link.key for link in uploads)
    assert links.result.key != links.bundle.key
    assert [link.max_bytes for link in uploads] == [1024 * 1024, 500 * 1024 * 1024]
    assert (RESULT_CAP, BUNDLE_CAP) == (1024 * 1024, 500 * 1024 * 1024)
    assert [link.expires_s for link in uploads] == [1800, 1800]
    assert [(link.key, link.expires_s) for link in links.packs] == [("packs/pack-a", 1800),
                                                                    ("packs/pack-b", 1800)]


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "."])
@pytest.mark.parametrize("which", ["run_id", "pack_id"])
def test_ac1_bad_ids_raise_before_anything_is_signed(bad, which):
    storage = FakeStorage()
    run_id, packs = (bad, ["ok"]) if which == "run_id" else ("run-1", ["ok", bad])

    with pytest.raises(ValueError):
        links_for_lane(run_id, 1, packs, 600, storage=storage, repo="acme/widgets")
    assert storage.calls == []


def test_presign_matches_aws_published_example():
    query = _presign_query(host="examplebucket.s3.amazonaws.com", encoded_path="/test.txt", region="us-east-1",
                           access_key=AWS_ACCESS, secret_key=AWS_SECRET,
                           when=datetime(2013, 5, 24, tzinfo=timezone.utc), expires_s=86400)

    assert "https://examplebucket.s3.amazonaws.com/test.txt?" + query == AWS_GET_EXAMPLE


def test_reference_verifier_agrees_with_aws_example():
    assert _expected_get_signature(AWS_GET_EXAMPLE, AWS_SECRET) == _signature(AWS_GET_EXAMPLE)


@pytest.mark.parametrize("endpoint, prefix", [
    (None, "https://s3.us-east-1.amazonaws.com/qa-bucket/"),
    ("http://127.0.0.1:9000/", "http://127.0.0.1:9000/qa-bucket/"),
    ("https://minio.internal:443", "https://minio.internal:443/qa-bucket/"),
])
def test_sign_download_is_path_style_and_verifies(endpoint, prefix):
    link = _storage(endpoint=endpoint).sign_download(KEY, 900)

    assert link.url.startswith(prefix + KEY + "?")
    params = dict(parse_qsl(urlsplit(link.url).query))
    assert params["X-Amz-Expires"] == "900"
    assert params["X-Amz-Date"] == "20261117T123005Z"
    assert params["X-Amz-Credential"] == f"{AWS_ACCESS}/20261117/us-east-1/s3/aws4_request"
    assert _signature(link.url) == _expected_get_signature(link.url, AWS_SECRET)


@pytest.mark.parametrize("endpoint, bucket_path", [
    ("https://store.invalid/proxy", "/proxy/qa-bucket"),
    ("https://store.invalid/s3%20proxy", "/s3%20proxy/qa-bucket"),
    ("http://127.0.0.1:9000/a%2Fb/", "/a%2Fb/qa-bucket"),
    ("https://store.invalid/proxy+prefix", "/proxy+prefix/qa-bucket"),
    ("https://store.invalid/a%7Eb", "/a%7Eb/qa-bucket"),
])
def test_upload_and_download_address_the_same_bucket_path(endpoint, bucket_path):
    storage = _storage(endpoint=endpoint)
    upload = storage.sign_upload(KEY, RESULT_CAP, 60)
    download = storage.sign_download(KEY, 60)

    assert urlsplit(upload.url).path == bucket_path
    assert urlsplit(download.url).path == f"{bucket_path}/{KEY}"
    assert _signature(download.url) == _expected_get_signature(download.url, AWS_SECRET)


def test_sign_upload_policy_pins_bucket_key_size_and_expiry():
    link = _storage().sign_upload(KEY, RESULT_CAP, 3600)

    assert link.url == "https://s3.us-east-1.amazonaws.com/qa-bucket"
    fields = link.fields
    policy = json.loads(base64.b64decode(fields["policy"]))
    assert policy["expiration"] == "2026-11-17T13:30:05Z"
    conditions = policy["conditions"]
    assert {"bucket": "qa-bucket"} in conditions
    assert {"key": KEY} in conditions
    assert ["content-length-range", 0, RESULT_CAP] in conditions
    for name in ("x-amz-algorithm", "x-amz-credential", "x-amz-date"):
        assert {name: fields[name]} in conditions
    assert fields["key"] == KEY
    assert fields["x-amz-algorithm"] == "AWS4-HMAC-SHA256"
    assert fields["x-amz-date"] == "20261117T123005Z"
    assert fields["x-amz-credential"] == f"{AWS_ACCESS}/20261117/us-east-1/s3/aws4_request"
    expected = hmac.new(_signing_key(AWS_SECRET, "20261117", "us-east-1"), fields["policy"].encode(),
                        hashlib.sha256).hexdigest()
    assert fields["x-amz-signature"] == expected


@pytest.mark.parametrize("expires", [0, -1, 604801, True])
def test_expiry_outside_sigv4_range_is_refused(expires):
    storage = _storage()
    with pytest.raises(ValueError):
        storage.sign_upload(KEY, 10, expires)
    with pytest.raises(ValueError):
        storage.sign_download(KEY, expires)


def test_seven_day_expiry_is_allowed():
    assert _storage().sign_download(KEY, 604800).expires_s == 604800


@pytest.mark.parametrize("key", ["../a", "a/../b", "a..b", "/a", "a b", "a%2Fb", "a?b", "", "ä"])
def test_unsafe_keys_are_refused_before_signing_or_fetching(key, tmp_path):
    opener = Opener(b"x")
    storage = _storage(opener)
    with pytest.raises(ValueError):
        storage.sign_upload(key, 10, 60)
    with pytest.raises(ValueError):
        storage.sign_download(key, 60)
    with pytest.raises(ValueError):
        storage.fetch(key, tmp_path / "out", 10)
    assert opener.urls == []


@pytest.mark.parametrize("size", [0, 5, 10])
def test_fetch_writes_body_within_cap(size, tmp_path):
    opener = Opener(b"y" * size)
    dest = tmp_path / "result.json"

    assert _storage(opener).fetch(KEY, dest, 10) is True
    assert dest.read_bytes() == b"y" * size
    assert _signature(opener.urls[0]) == _expected_get_signature(opener.urls[0], AWS_SECRET)
    assert list(tmp_path.iterdir()) == [dest]


def test_fetch_over_cap_raises_and_leaves_no_file(tmp_path):
    opener = Opener(b"z" * 5000)

    with pytest.raises(StorageError):
        _storage(opener).fetch(KEY, tmp_path / "bundle", 1000)
    assert opener.response.served <= 1001
    assert list(tmp_path.iterdir()) == []


def _http_error(code: int):
    return lambda url: HTTPError(url, code, f"denied {url}", {}, None)


FAILURES = {
    "http-403": _http_error(403),
    "http-500": _http_error(500),
    "url-error": lambda url: URLError(f"cannot reach {url}"),
    "timeout": lambda url: socket.timeout(f"timed out on {url}"),
    "reset": lambda url: ConnectionResetError(f"reset by {url}"),
}


def _assert_no_link(text: str, url: str) -> None:
    assert _signature(url) not in text
    assert url not in text
    assert "X-Amz-Signature" not in text


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_ac5_fetch_failures_never_carry_the_signed_link(name, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    opener = Opener(FAILURES[name])

    with pytest.raises(StorageError) as caught:
        _storage(opener).fetch(KEY, tmp_path / "out", 100)

    err = caught.value
    assert KEY in str(err)
    assert err.__cause__ is None and err.__context__ is None
    for text in (str(err), repr(err), "".join(traceback.format_exception(err)), caplog.text):
        _assert_no_link(text, opener.urls[0])
    assert list(tmp_path.iterdir()) == []


def test_ac5_fetch_404_returns_false_and_writes_nothing(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    opener = Opener(_http_error(404))

    assert _storage(opener).fetch(KEY, tmp_path / "out", 100) is False
    assert list(tmp_path.iterdir()) == []
    _assert_no_link(caplog.text, opener.urls[0])


def test_ac5_reprs_hold_no_url_or_signature():
    storage = _storage()
    upload = storage.sign_upload(KEY, 10, 60)
    download = storage.sign_download("packs/p1", 60)
    links = LaneLinks(result=upload, bundle=upload, packs=(download,))

    for text in (repr(upload), repr(download), repr(links), repr(storage)):
        assert "https://" not in text
        assert upload.fields["x-amz-signature"] not in text
        assert upload.fields["policy"] not in text
        assert _signature(download.url) not in text
        assert AWS_SECRET not in text


@dataclass
class Entry:
    bucket: str = "qa-bucket"
    region: str = "eu-west-1"
    endpoint: str | None = None
    access_key_env: str = "QA_ACCESS"
    secret_key_env: str = "QA_SECRET"


@pytest.mark.parametrize("missing", ["QA_ACCESS", "QA_SECRET"])
@pytest.mark.parametrize("value", [None, ""])
def test_from_entry_missing_env_names_the_variable_only(missing, value):
    environ = {"QA_ACCESS": "AKIDVALUE123", "QA_SECRET": "SECRETVALUE456"}
    if value is None:
        del environ[missing]
    else:
        environ[missing] = value

    with pytest.raises(StorageError) as caught:
        S3Storage.from_entry(Entry(), environ)

    text = "".join(traceback.format_exception(caught.value))
    assert missing in str(caught.value)
    assert "AKIDVALUE123" not in text and "SECRETVALUE456" not in text


def test_from_entry_signs_with_env_keys_and_default_endpoint():
    environ = {"QA_ACCESS": "AKIDVALUE123", "QA_SECRET": "SECRETVALUE456"}

    link = S3Storage.from_entry(Entry(), environ, now=lambda: NOW).sign_download("packs/p1", 60)

    assert link.url.startswith("https://s3.eu-west-1.amazonaws.com/qa-bucket/packs/p1?")
    assert "AKIDVALUE123%2F20261117%2Feu-west-1" in link.url
    assert _signature(link.url) == _expected_get_signature(link.url, "SECRETVALUE456")
