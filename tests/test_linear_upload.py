from __future__ import annotations

import json
import traceback

import pytest

from lib.linear_upload import CACHE_CONTROL, HttpLinear, LinearError
from lib.poll_source import LINEAR_ENDPOINT, HTTPResult

KEY = "lin_api_SECRET123"
UPLOAD_URL = "https://storage.googleapis.com/linear-uploads/org/a1?X-Goog-Signature=SIGNED456"
ASSET = "https://uploads.linear.app/org/a1/b2"
SIGNED = {"x-goog-content-length-range": "5,5", "Content-Disposition": 'attachment; filename="clip.mp4"'}


class Recorder:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def ok(data):
    return HTTPResult(200, json.dumps({"data": data}))


def granted(signed=SIGNED):
    upload = {"uploadUrl": UPLOAD_URL, "assetUrl": ASSET, "headers": [{"key": k, "value": v} for k, v in signed.items()]}
    return ok({"fileUpload": {"success": True, "uploadFile": upload}})


def page(nodes, cursor=None):
    return ok({"issue": {"comments": {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}})


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"hello")
    return path


def test_upload_asks_file_upload_then_puts_the_exact_bytes_with_every_signed_header(clip):
    post, put = Recorder(granted(), granted({"cache-control": "no-store"})), Recorder(200, 200)
    client = HttpLinear(KEY, post=post, put=put)

    assert client.upload(clip, "clip.mp4", "video/mp4", 5) == ASSET
    client.upload(clip, "clip.mp4", "video/mp4", 5)

    url, headers, body = post.calls[0]
    assert url == LINEAR_ENDPOINT and headers["Authorization"] == KEY and "fileUpload(" in json.loads(body)["query"]
    assert json.loads(body)["variables"] == {"contentType": "video/mp4", "filename": "clip.mp4", "size": 5}
    assert put.calls == [
        (UPLOAD_URL, {"Content-Type": "video/mp4", "Cache-Control": CACHE_CONTROL, **SIGNED}, b"hello"),
        (UPLOAD_URL, {"Content-Type": "video/mp4", "cache-control": "no-store"}, b"hello"),
    ]


def test_a_wrong_size_or_a_symlink_sends_nothing(clip, tmp_path):
    (tmp_path / "link.mp4").symlink_to(clip)
    post, put = Recorder(), Recorder()
    for path, size in ((clip, 6), (tmp_path / "link.mp4", 5)):
        with pytest.raises(LinearError):
            HttpLinear(KEY, post=post, put=put).upload(path, "clip.mp4", "video/mp4", size)
    assert post.calls == put.calls == []


def test_comment_create_posts_the_body_and_returns_the_comment_id():
    post = Recorder(ok({"commentCreate": {"success": True, "comment": {"id": "c-9"}}}))

    assert HttpLinear(KEY, post=post).comment("issue-1", "QA run run-1\nall green") == "c-9"
    body = json.loads(post.calls[0][2])
    assert "commentCreate(" in body["query"]
    assert body["variables"] == {"issueId": "issue-1", "body": "QA run run-1\nall green"}


def test_the_marker_search_pages_and_matches_whole_lines_only():
    post = Recorder(
        page([{"id": "c1", "body": "QA run run-12\nsee QA run run-1 below"}], cursor="cur-1"),
        page([{"id": "c2", "body": "intro\n  QA run run-1  \nevidence"}]),
        page([{"id": "c3", "body": "QA run run-12"}]),
    )
    client = HttpLinear(KEY, post=post)

    assert client.find_comment("issue-1", "QA run run-1") == "c2"
    assert client.find_comment("issue-1", "QA run run-1") is None
    assert [json.loads(call[2])["variables"]["after"] for call in post.calls] == [None, "cur-1", None]


FAILURES = {
    "http 500": ([HTTPResult(500, f"denied {KEY}")], []),
    "graphql errors": ([HTTPResult(200, json.dumps({"errors": [{"message": KEY}]}))], []),
    "transport raises": ([RuntimeError(f"{KEY} at {LINEAR_ENDPOINT}")], []),
    "put 403": ([granted()], [403]),
    "put raises": ([granted()], [OSError(f"cannot reach {UPLOAD_URL}")]),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_no_failure_carries_the_key_or_a_signed_url(name, clip):
    replies, puts = FAILURES[name]
    client = HttpLinear(KEY, post=Recorder(*replies), put=Recorder(*puts))

    with pytest.raises(LinearError) as caught:
        client.upload(clip, "clip.mp4", "video/mp4", 5) if puts else client.issue_id("AUT-1")

    err = caught.value
    assert err.__cause__ is None and err.__context__ is None
    for text in (str(err), repr(err), "".join(traceback.format_exception(err)), repr(client)):
        assert KEY not in text and "SIGNED456" not in text
