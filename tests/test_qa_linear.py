"""#792: after a primary-arm QA run is reported, the worker posts one evidence comment on the Linear ticket."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib import cli
from lib.linear_upload import FakeLinear, HttpLinear
from lib.orch_yml import LinearConfig, OrchConfig, Project
from lib.poll_source import HTTPResult
from lib.qa_evidence import MANIFEST_NAME, lane_dir
from tests.qa_support import REPO
from tests.test_qa_cloud import Lab, _ready_thread

pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

TICKET = "AUT-42"
ISSUE = "issue-42"


def linear_lab(
    tmp_path: Path, *, tickets: str = "linear", arm: str = "primary", linear: FakeLinear | HttpLinear | None = None
) -> tuple[Lab, FakeLinear | HttpLinear, str]:
    linear = linear or FakeLinear({TICKET: ISSUE})
    lab = Lab(tmp_path, lanes=1, linear=linear)
    run_id = _ready_thread(lab)
    base = lab.run.thread.base
    lab.run.thread.base = replace(base, ticket_id=TICKET, factory=replace(base.factory, tickets=tickets))
    with lab.run.queue.connect() as connection:
        row = connection.execute("SELECT id, detail FROM jobs WHERE action = 'qa'").fetchone()
        detail = {**json.loads(row["detail"]), "arm": arm}
        connection.execute("UPDATE jobs SET ticket_id = ?, detail = ? WHERE id = ?", (TICKET, json.dumps(detail), row["id"]))
        connection.execute("UPDATE qa_runs SET ticket_id = ?, arm = ?", (TICKET, arm))
    return lab, linear, run_id


def report(lab: Lab, run_id: str) -> None:
    lab.run.post("q1", lab.only_comment())
    lab.run.dispatcher.process(lab.run.thread.snapshot())
    assert lab.run.rows("SELECT state FROM qa_runs WHERE run_id = ?", run_id) == [("reported",)]


def ledger(lab: Lab, run_id: str) -> list[tuple]:
    return lab.run.rows("SELECT state, reason, comment_id FROM qa_linear WHERE run_id = ?", run_id)


def lane_folder(lab: Lab, run_id: str) -> Path:
    return lane_dir(lab.root, REPO, TICKET, run_id, 1)


def add_files(folder: Path, files: dict[str, bytes], *, listed: dict[str, bytes] | None = None) -> None:
    manifest = json.loads((folder / MANIFEST_NAME).read_bytes())
    for path, data in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
    for path, data in (files if listed is None else listed).items():
        manifest["files"].append({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    (folder / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def uploaded(linear: FakeLinear, folder: Path) -> list[str]:
    return sorted(path.relative_to(folder).as_posix() for path, *_ in linear.uploads)


def only_body(linear: FakeLinear) -> str:
    assert len(linear.comments.get(ISSUE, [])) == 1, linear.comments
    return linear.comments[ISSUE][0][1]


def test_a_reported_primary_run_posts_one_evidence_comment(tmp_path: Path) -> None:
    lab, linear, run_id = linear_lab(tmp_path)
    lab.tick(4)
    lab.tick(2)
    assert linear.comments == {} and linear.uploads == []
    assert ledger(lab, run_id) == [("pending", None, None)]

    report(lab, run_id)
    lab.tick()

    body = only_body(linear)
    qa_comment = lab.run.rows("SELECT comment_id FROM qa_runs WHERE run_id = ?", run_id)[0][0]
    assert body.startswith("QA result: PASS at `aaaaaaaaaaaa`")
    assert "| qa-1 | ac-1 | Verified |\n| qa-2 | ac-1 | Verified |" in body
    for n, url in ((1, "https://uploads.linear.invalid/1/"), (2, "https://uploads.linear.invalid/2/")):
        assert f"![lane-1/s-{n}/after-screenshot.png]({url}after-screenshot.png)" in body
    assert f"https://github.com/{REPO}/pull/812#issuecomment-{qa_comment}" in body
    assert f"\nQA run {run_id}\n" in body
    assert ledger(lab, run_id) == [("posted", None, "comment-1")]

    lab.tick(3)
    with lab.run.queue.connect() as connection:
        connection.execute("UPDATE qa_linear SET state = 'pending', comment_id = NULL")
    lab.tick(2)

    assert len(linear.comments[ISSUE]) == 1
    assert len(linear.uploads) == 2
    assert ledger(lab, run_id) == [("posted", None, "comment-1")]


def test_a_github_ticket_project_gets_no_linear_row(tmp_path: Path) -> None:
    lab, linear, run_id = linear_lab(tmp_path, tickets="github")
    lab.tick(4)
    report(lab, run_id)
    lab.tick(2)

    assert lab.run.rows("SELECT COUNT(*) FROM qa_linear") == [(0,)]
    assert linear.comments == {} and linear.uploads == []


def test_only_listed_screenshots_and_videos_are_uploaded(tmp_path: Path) -> None:
    lab, linear, run_id = linear_lab(tmp_path)
    lab.tick(4)
    folder = lane_folder(lab, run_id)
    listed = {
        "s-1/video.webm": b"webm-bytes",
        "s-2/video.mp4": b"mp4-bytes",
        "s-1/trace.zip": b"trace",
        "s-1/console.log": b"console",
        "network.json": b"[]",
    }
    add_files(folder, {**listed, "s-2/video.mp4": b"mp4-BYTES", "s-1/extra.png": b"extra"}, listed=listed)
    report(lab, run_id)
    lab.tick()

    assert uploaded(linear, folder) == ["s-1/after-screenshot.png", "s-1/video.webm", "s-2/after-screenshot.png"]
    assert [(name, kind) for _, name, kind, _ in linear.uploads if kind.startswith("video")] == [
        ("video.webm", "video/webm")
    ]
    body = only_body(linear)
    assert "- [lane-1/s-1/video.webm](https://uploads.linear.invalid/" in body
    assert "- lane-1/s-2/video.mp4: missing, or not the file its manifest lists" in body
    for name in ("trace.zip", "console.log", "network.json", "extra.png"):
        assert name not in body


def test_a_manifest_path_outside_the_lane_is_never_read(tmp_path: Path) -> None:
    lab, linear, run_id = linear_lab(tmp_path)
    lab.tick(4)
    folder = lane_folder(lab, run_id)
    (folder.parent / "outside.webm").write_bytes(b"outside")
    add_files(folder, {}, listed={"../outside.webm": b"outside"})
    report(lab, run_id)
    lab.tick()

    assert uploaded(linear, folder) == ["s-1/after-screenshot.png", "s-2/after-screenshot.png"]
    assert "- lane-1/../outside.webm: not a safe manifest entry" in only_body(linear)


def test_a_file_over_the_limit_is_named_and_not_uploaded(tmp_path: Path) -> None:
    lab, linear, run_id = linear_lab(tmp_path)
    lab.worker.upload_cap = 40
    lab.tick(4)
    folder = lane_folder(lab, run_id)
    add_files(folder, {"s-1/video.webm": b"v" * 64})
    report(lab, run_id)
    lab.tick()

    assert uploaded(linear, folder) == ["s-1/after-screenshot.png", "s-2/after-screenshot.png"]
    assert "Left out:\n- lane-1/s-1/video.webm (64 bytes): over the upload limit" in only_body(linear)


@pytest.mark.parametrize("pack_keys", ['["packs/other"]', None])
def test_a_pack_without_a_signed_link_uploads_nothing(tmp_path: Path, pack_keys: str | None) -> None:
    lab, linear, run_id = linear_lab(tmp_path)
    lab.silent.add((0, 1))
    lab.tick()
    with lab.run.queue.connect() as connection:
        connection.execute("UPDATE qa_lanes SET pack_keys = ?", (pack_keys,))
    lab.silent.clear()
    lab.tick(4)

    assert ledger(lab, run_id) == [("skipped", "pack-not-signed: demo", None)]
    report(lab, run_id)
    lab.tick(2)
    assert linear.comments == {} and linear.uploads == []
    assert ledger(lab, run_id) == [("skipped", "pack-not-signed: demo", None)]


def test_a_shootout_arm_run_uploads_nothing(tmp_path: Path) -> None:
    lab, linear, run_id = linear_lab(tmp_path, arm="shootout")
    lab.tick(4)

    assert ledger(lab, run_id) == [("skipped", "shootout-arm", None)]
    report(lab, run_id)
    lab.tick(2)
    assert linear.comments == {} and linear.uploads == []


def test_a_failing_linear_call_is_retried_three_times_and_logged_without_the_key(tmp_path: Path) -> None:
    calls: list[dict] = []

    def refuse(url: str, headers: dict, body: bytes, timeout: float) -> HTTPResult:
        calls.append(headers)
        return HTTPResult(500, "lin_api_SECRET echoed")

    lab, _, run_id = linear_lab(tmp_path, linear=HttpLinear("lin_api_SECRET", post=refuse))
    lab.tick(4)
    report(lab, run_id)
    lab.tick(5)

    assert len(calls) == 3
    assert ledger(lab, run_id) == [("failed", "LinearError: issue: HTTP 500", None)]
    assert [line for line in lab.logs if "qa linear" in line] == [f"qa linear {run_id}: LinearError: issue: HTTP 500"] * 3
    assert all("lin_api_SECRET" not in line for line in lab.logs)


def test_the_worker_holds_the_linear_key_only_when_it_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = OrchConfig(
        poll_seconds=60, projects=(Project(REPO, factory_ref="main"),), linear=LinearConfig("LINEAR_KEY_FOR_TEST")
    )
    db = Lab(tmp_path).run.queue.path
    monkeypatch.delenv("LINEAR_KEY_FOR_TEST", raising=False)
    assert cli._qa_runtime(db, config).linear is None

    monkeypatch.setenv("LINEAR_KEY_FOR_TEST", "lin_api_SECRET")
    worker = cli._qa_runtime(db, config)

    assert isinstance(worker.linear, HttpLinear)
    assert "lin_api_SECRET" not in repr(worker)
