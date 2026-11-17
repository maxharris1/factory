from __future__ import annotations

import hashlib
import io
import json
import tarfile
import traceback
from pathlib import Path

import pytest

from lib import qa_evidence
from lib.dispatch.errors_text import scrub_detail
from lib.qa_evidence import EvidenceRefused, collect
from lib.qa_storage import BUNDLE_NAME, RESULT_NAME, StorageError, lane_prefix

REPO = "acme/shop"
RUN = "run-7"
TICKET = "787"
PREFIX = lane_prefix(REPO, RUN, 1)
SIGNED_LOG_LINE = "GET https://bucket.example/x?X-Amz-Signature=abc123 failed"


class FakeStorage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def fetch(self, key: str, dest: Path, max_bytes: int) -> bool:
        data = self.objects.get(key)
        if data is None:
            return False
        if len(data) > max_bytes:
            raise StorageError(f"fetch {key}: over {max_bytes} bytes")
        dest.write_bytes(data)
        return True


def _file(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _special(name: str, kind: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


def _bundle(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, None if data is None else io.BytesIO(data))
    return buf.getvalue()


def _result(**fields: object) -> dict[str, object]:
    return {"run_id": RUN, "lane": 1, "commit": "c0ffee", "status": "pass", "artifacts": [], **fields}


def _storage(result: dict[str, object] | None, bundle: bytes | None) -> FakeStorage:
    objects = {}
    if result is not None:
        objects[PREFIX + RESULT_NAME] = json.dumps(result).encode()
    if bundle is not None:
        objects[PREFIX + BUNDLE_NAME] = bundle
    return FakeStorage(objects)


def _collect(evidence_root: Path, storage: FakeStorage, **overrides: object) -> dict | None:
    args: dict = {"storage": storage, "repo": REPO, "ticket": TICKET, "root": evidence_root}
    return collect(RUN, 1, **{**args, **overrides})


def _lane_dir(root: Path) -> Path:
    return root / "acme" / "shop" / TICKET / RUN / "lane-1"


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "sentinel.txt").write_text("keep me")
    return root


def _assert_nothing_stored(root: Path) -> None:
    assert not (root / "acme").exists()
    assert list((root / ".staging").iterdir()) == []
    assert (root / "sentinel.txt").read_text() == "keep me"


def test_happy_path_stores_evidence_with_manifest(root: Path) -> None:
    shot = b"\x89PNG fake screenshot"
    raw_log = f"booting\n{SIGNED_LOG_LINE}\n".encode()
    bundle = _bundle(
        _special("screens", tarfile.DIRTYPE),
        _file("screens/home.png", shot),
        _file("app.log", raw_log),
    )
    artifacts = [
        {"path": "screens/home.png", "sha256": _sha(shot), "size": len(shot)},
        {"path": "app.log", "sha256": _sha(raw_log), "size": len(raw_log)},
    ]

    returned = _collect(root, _storage(_result(artifacts=artifacts), bundle))

    assert returned is not None
    assert returned["status"] == "pass"
    assert returned["mismatches"] == [], "artifact checks ran on the uploaded bytes, before scrubbing"
    lane = _lane_dir(root)
    assert (lane / "screens" / "home.png").read_bytes() == shot
    assert json.loads((lane / RESULT_NAME).read_text())["artifacts"] == artifacts
    manifest = json.loads((lane / "manifest.json").read_text())
    assert (manifest["run_id"], manifest["lane"], manifest["commit"]) == (RUN, 1, "c0ffee")
    assert manifest["mismatches"] == []
    assert [f["path"] for f in manifest["files"]] == ["app.log", RESULT_NAME, "screens/home.png"]
    for entry in manifest["files"]:
        stored = (lane / entry["path"]).read_bytes()
        assert (entry["sha256"], entry["size"]) == (_sha(stored), len(stored))
    assert manifest["files"][0]["sha256"] != _sha(raw_log), "manifest hashes are post-scrub"
    assert manifest["note"]
    assert list((root / ".staging").iterdir()) == []


def test_second_collect_for_same_lane_is_refused_and_first_is_untouched(root: Path) -> None:
    storage = _storage(_result(), _bundle(_file("a.txt", b"first")))
    _collect(root, storage)
    before = _tree(root / "acme")

    storage.objects[PREFIX + BUNDLE_NAME] = _bundle(_file("a.txt", b"second"))
    with pytest.raises(EvidenceRefused, match="already stored"):
        _collect(root, storage)

    assert _tree(root / "acme") == before
    assert list((root / ".staging").iterdir()) == []


@pytest.mark.parametrize(
    "members",
    [
        pytest.param([_file("../x", b"escaped")], id="dotdot"),
        pytest.param([_file("../../../sentinel.txt", b"overwritten")], id="dotdot-onto-sentinel"),
        pytest.param([_file("/tmp/qa-evidence-abs", b"escaped")], id="absolute"),
        pytest.param(
            [_special("link", tarfile.SYMTYPE, "../../../sentinel.txt"), _file("link", b"overwritten")],
            id="symlink",
        ),
        pytest.param(
            [_special("hard", tarfile.LNKTYPE, "../../../sentinel.txt"), _file("hard", b"overwritten")],
            id="hardlink",
        ),
        pytest.param([_special("dev/tty0", tarfile.CHRTYPE)], id="char-device"),
        pytest.param([_special("pipe", tarfile.FIFOTYPE)], id="fifo"),
    ],
)
def test_unsafe_member_is_refused_and_nothing_is_written(root: Path, members: list) -> None:
    bundle = _bundle(_file("ok.txt", b"fine"), *members)

    with pytest.raises(EvidenceRefused) as refused:
        _collect(root, _storage(_result(), bundle))

    assert members[0][0].name in str(refused.value)
    _assert_nothing_stored(root)
    assert not (root / "x").exists()
    assert not (root / ".staging" / "x").exists()
    assert not Path("/tmp/qa-evidence-abs").exists()


def test_bundle_over_twice_the_cap_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa_evidence, "BUNDLE_CAP", 1024)
    bundle = _bundle(_file("a.bin", bytes(1500)), _file("b.bin", bytes(1500)))
    assert len(bundle) < 1024, "the compressed bundle itself is under the cap"

    with pytest.raises(EvidenceRefused, match="more than 2048 bytes"):
        _collect(root, _storage(_result(), bundle))

    _assert_nothing_stored(root)


def test_bundle_under_twice_the_cap_is_stored(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa_evidence, "BUNDLE_CAP", 1024)
    bundle = _bundle(_file("a.bin", bytes(1000)), _file("b.bin", bytes(1000)))

    _collect(root, _storage(_result(), bundle))

    assert (_lane_dir(root) / "b.bin").read_bytes() == bytes(1000)


def test_bundle_with_too_many_members_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa_evidence, "MAX_MEMBERS", 2)
    bundle = _bundle(_file("a", b""), _file("b", b""), _file("c", b""))

    with pytest.raises(EvidenceRefused, match="more than 2 members"):
        _collect(root, _storage(_result(), bundle))

    _assert_nothing_stored(root)


@pytest.mark.parametrize(
    "members",
    [
        pytest.param([_file("a.txt", b"one"), _file("a.txt", b"two")], id="duplicate"),
        pytest.param([_file("a", b"file"), _file("a/b", b"child")], id="file-as-dir"),
        pytest.param([_file("manifest.json/x", b"x")], id="reserved-as-dir"),
    ],
)
def test_colliding_members_are_refused(root: Path, members: list) -> None:
    with pytest.raises(EvidenceRefused):
        _collect(root, _storage(_result(), _bundle(*members)))
    _assert_nothing_stored(root)


def test_unreadable_bundle_is_refused(root: Path) -> None:
    with pytest.raises(EvidenceRefused, match="not a readable tar.gz"):
        _collect(root, _storage(_result(), b"not a tarball"))
    _assert_nothing_stored(root)


def test_missing_bundle_is_refused(root: Path) -> None:
    with pytest.raises(EvidenceRefused, match="bundle.tar.gz is missing"):
        _collect(root, _storage(_result(), None))
    _assert_nothing_stored(root)


def test_absent_result_returns_none_and_stores_nothing(root: Path) -> None:
    assert _collect(root, _storage(None, _bundle(_file("a.txt", b"x")))) is None
    _assert_nothing_stored(root)


@pytest.mark.parametrize(
    "result",
    [_result(run_id="run-8"), _result(lane=2), _result(lane=True), ["not", "an", "object"]],
    ids=["other-run", "other-lane", "bool-lane", "not-object"],
)
def test_result_for_another_run_or_lane_is_refused(root: Path, result: object) -> None:
    storage = _storage(None, _bundle(_file("a.txt", b"x")))
    storage.objects[PREFIX + RESULT_NAME] = json.dumps(result).encode()
    with pytest.raises(EvidenceRefused):
        _collect(root, storage)
    _assert_nothing_stored(root)


def test_artifact_mismatches_reach_manifest_and_result(root: Path) -> None:
    shot = b"screenshot bytes"
    bundle = _bundle(_file("shot.png", shot), _file("short.png", b"abc"))
    artifacts = [
        {"path": "shot.png", "sha256": "0" * 64, "size": len(shot)},
        {"path": "gone.png", "sha256": _sha(b""), "size": 0},
        {"path": "short.png", "sha256": _sha(b"abc"), "size": 99},
        {"path": "../../../sentinel.txt", "sha256": _sha(b"keep me"), "size": 7},
        {"path": "/etc/hostname", "sha256": "0" * 64, "size": 1},
    ]

    returned = _collect(root, _storage(_result(artifacts=artifacts), bundle))

    expected = [
        {"path": "shot.png", "reason": "sha256"},
        {"path": "gone.png", "reason": "missing"},
        {"path": "short.png", "reason": "size"},
        {"path": "../../../sentinel.txt", "reason": "bad path"},
        {"path": "/etc/hostname", "reason": "bad path"},
    ]
    assert returned is not None and returned["mismatches"] == expected
    manifest = json.loads((_lane_dir(root) / "manifest.json").read_text())
    assert manifest["mismatches"] == expected


def test_log_lines_are_scrubbed(root: Path) -> None:
    bundle = _bundle(_file("logs/app.log", f"server ready\n{SIGNED_LOG_LINE}\n".encode()))

    _collect(root, _storage(_result(), bundle))

    stored = (_lane_dir(root) / "logs" / "app.log").read_text()
    placeholder = scrub_detail("https://bucket.example/x")
    assert placeholder != "https://bucket.example/x"
    assert "abc123" not in stored
    assert "bucket.example" not in stored
    assert stored.splitlines() == ["server ready", f"GET {placeholder} failed"]


def _har_entry() -> dict[str, object]:
    return {
        "request": {
            "method": "GET",
            "url": "/api/cart",
            "headers": [{"name": "Authorization", "value": "Bearer s3cret-request"}],
        },
        "requestHeaders": {"Cookie": "session=s3cret-cookie"},
        "response": {"status": 200, "headers": [{"name": "Content-Type", "value": "text/html"}]},
    }


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda e: [e], id="list"),
        pytest.param(lambda e: {"entries": [e]}, id="entries"),
        pytest.param(lambda e: e, id="dict"),
        pytest.param(lambda e: {"log": {"pages": [], "entries": [e]}}, id="nested"),
    ],
)
def test_network_json_loses_request_headers_and_keeps_response(root: Path, shape) -> None:
    bundle = _bundle(_file("trace/network.json", json.dumps(shape(_har_entry())).encode()))

    _collect(root, _storage(_result(), bundle))

    text = (_lane_dir(root) / "trace" / "network.json").read_text()
    assert "s3cret" not in text
    assert "requestHeaders" not in text
    assert shape({"request": {"method": "GET", "url": "/api/cart"}, "response": _har_entry()["response"]}) == (
        json.loads(text)
    )


def test_unparseable_network_json_is_removed_and_recorded(root: Path) -> None:
    bundle = _bundle(_file("network.json", b'{"request": {"headers": "Bearer s3cret"'))

    returned = _collect(root, _storage(_result(), bundle))

    assert not (_lane_dir(root) / "network.json").exists()
    expected = [{"path": "network.json", "reason": "invalid network.json"}]
    assert returned is not None and returned["mismatches"] == expected
    assert json.loads((_lane_dir(root) / "manifest.json").read_text())["mismatches"] == expected


def test_fetched_result_wins_over_bundle_copy(root: Path) -> None:
    bundle = _bundle(_file(RESULT_NAME, b'{"status": "forged"}'), _file("manifest.json", b"{}"))

    _collect(root, _storage(_result(), bundle))

    assert json.loads((_lane_dir(root) / RESULT_NAME).read_text())["status"] == "pass"
    assert json.loads((_lane_dir(root) / "manifest.json").read_text())["run_id"] == RUN


@pytest.mark.parametrize(
    "overrides",
    [{"repo": "shop"}, {"repo": "acme/shop/x"}, {"repo": "../shop"}, {"ticket": ".."}, {"root": Path("rel")}],
    ids=["no-owner", "extra-part", "dotdot-owner", "dotdot-ticket", "relative-root"],
)
def test_bad_arguments_raise_value_error(root: Path, overrides: dict) -> None:
    with pytest.raises(ValueError):
        _collect(root, _storage(_result(), _bundle()), **overrides)


SIGNATURE = "LEAKED_SIGNATURE"
CAPABILITY = "https://bucket.example/object?X-Amz-Signature=" + SIGNATURE
OVERLONG = "X-Amz-Signature=" + SIGNATURE + "S" * 300


def _sparse_map_member() -> tuple[tarfile.TarInfo, bytes]:
    info, data = _file("artifact.bin", b"x")
    info.pax_headers = {"GNU.sparse.map": CAPABILITY}
    return info, data


@pytest.mark.parametrize(
    "result,members",
    [
        pytest.param(_result(), [_sparse_map_member()], id="tar-parser-error"),
        pytest.param(_result(), [_file(OVERLONG, b"x")], id="overlong-member-name"),
        pytest.param(
            _result(),
            [_file("X-Amz-Signature=" + SIGNATURE, b"one"), _file("X-Amz-Signature=" + SIGNATURE, b"two")],
            id="colliding-member-context",
        ),
        pytest.param(
            _result(artifacts=[{"path": OVERLONG, "sha256": "0", "size": 1}]),
            [_file("ok.txt", b"fine")],
            id="overlong-artifact-path",
        ),
    ],
)
def test_untrusted_failure_text_never_reaches_the_refusal(root: Path, result: dict, members: list) -> None:
    with pytest.raises(EvidenceRefused) as refused:
        _collect(root, _storage(result, _bundle(*members)))

    error = refused.value
    assert SIGNATURE not in str(error)
    assert SIGNATURE not in "".join(traceback.format_exception(error))
    assert error.__context__ is None and error.__cause__ is None
    _assert_nothing_stored(root)
