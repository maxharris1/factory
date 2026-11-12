"""The static server for the pushed dashboard (deploy/factory-dashboard-http.py).

Each test serves a scratch directory on loopback through `make_server`
(the tailnet-only bind is enforced by `load_config`, tested in
tests/test_deploy.py) and checks that only the snapshot files (status.json and the four pages)
are served, never anything outside the directory, never a listing.
"""

import http.client
import importlib.util
import os
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "deploy" / "factory-dashboard-http.py"

spec = importlib.util.spec_from_file_location("factory_dashboard_http", SERVER_PATH)
assert spec is not None and spec.loader is not None
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)

PRIVATE = "private-outside-the-site " + "Zq8" * 8
INDEX = "<!doctype html><title>factory status</title>"
STATUS = '{"schema": "factory-dashboard/v1"}\n'


@pytest.fixture
def site(tmp_path: Path) -> Path:
    (tmp_path / "private.txt").write_text(PRIVATE, encoding="utf-8")
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "status.json").write_text(STATUS, encoding="utf-8")
    return root


@pytest.fixture
def get(site: Path):
    server = server_module.make_server("127.0.0.1", 0, site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.putrequest(method, path, skip_accept_encoding=True)
            connection.endheaders()
            response = connection.getresponse()
            body = response.read()
            headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, headers, body
        finally:
            connection.close()

    yield request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_serves_index_at_root_and_by_name(get) -> None:
    for path in ("/", "/index.html", "/?t=1"):
        status, headers, body = get(path)
        assert status == 200, path
        assert body.decode() == INDEX
        assert headers["content-type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize("name", ["activity.html", "efficiency.html", "job.html"])
def test_serves_the_other_pages(site: Path, get, name: str) -> None:
    status, _, _ = get(f"/{name}")
    assert status == 404  # not written yet
    (site / name).write_text(f"<!doctype html><title>{name}</title>", encoding="utf-8")
    status, headers, body = get(f"/{name}")
    assert status == 200
    assert body.decode() == f"<!doctype html><title>{name}</title>"
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert get(f"/{name}/x")[0] == 404


def test_serves_status_json(get) -> None:
    status, headers, body = get("/status.json")
    assert status == 200
    assert body.decode() == STATUS
    assert headers["content-type"] == "application/json"


def test_responses_carry_locked_down_headers(get) -> None:
    _, headers, _ = get("/")
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "Python" not in headers["server"]


def test_head_sends_headers_only(get) -> None:
    status, headers, body = get("/status.json", method="HEAD")
    assert status == 200
    assert body == b""
    assert headers["content-length"] == str(len(STATUS))


def test_symlink_out_of_the_directory_is_not_followed(site: Path, get) -> None:
    (site / "leak").symlink_to(site.parent / "private.txt")
    status, _, body = get("/leak")
    assert status == 404
    assert PRIVATE.encode() not in body


def test_snapshot_name_that_is_a_symlink_out_is_refused(site: Path, get) -> None:
    (site / "status.json").unlink()
    (site / "status.json").symlink_to(site.parent / "private.txt")
    status, _, body = get("/status.json")
    assert status == 404
    assert PRIVATE.encode() not in body


def test_snapshot_name_that_is_a_hard_link_is_refused(site: Path, get) -> None:
    (site / "index.html").unlink()
    os.link(site.parent / "private.txt", site / "index.html")
    status, _, body = get("/")
    assert status == 404
    assert PRIVATE.encode() not in body


@pytest.mark.parametrize(
    "path",
    [
        "/../private.txt",
        "/%2e%2e/private.txt",
        "/..%2fprivate.txt",
        "//private.txt",
        "/site/../private.txt",
        "/status.json/..",
        "/private.txt",
    ],
)
def test_paths_outside_the_two_files_are_not_found(get, path: str) -> None:
    status, _, body = get(path)
    assert status == 404, path
    assert PRIVATE.encode() not in body


def test_directories_are_never_listed(site: Path, get) -> None:
    sub = site / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("inner", encoding="utf-8")
    for path in ("/sub/", "/sub", "/sub/inner.txt"):
        status, _, body = get(path)
        assert status == 404, path
        assert b"inner" not in body
    (site / "index.html").unlink()
    status, _, body = get("/")
    assert status == 404
    assert b"status.json" not in body and b"sub" not in body


def test_push_temp_files_are_not_served(site: Path, get) -> None:
    (site / ".status.json.push-tmp").write_text("half", encoding="utf-8")
    status, _, body = get("/.status.json.push-tmp")
    assert status == 404
    assert b"half" not in body


def test_a_fifo_in_place_of_a_snapshot_is_refused_without_hanging(site: Path, get) -> None:
    (site / "status.json").unlink()
    os.mkfifo(site / "status.json")
    status, _, _ = get("/status.json")
    assert status == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_get_and_head_are_answered(site: Path, get, method: str) -> None:
    status, _, _ = get("/status.json", method=method)
    assert status in (405, 501)
    assert (site / "status.json").read_text(encoding="utf-8") == STATUS


def _fetch(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_root_stays_pinned_when_an_ancestor_is_swapped_for_a_symlink(tmp_path: Path) -> None:
    """The directory is opened once at startup and every file is opened
    relative to that handle, so renaming an ancestor and putting a symlink
    in its place after startup does not change what is served (r2 F2.3)."""
    parent = tmp_path / "parent"
    site = parent / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(INDEX, encoding="utf-8")
    (site / "status.json").write_text(STATUS, encoding="utf-8")
    decoy = tmp_path / "decoy" / "site"
    decoy.mkdir(parents=True)
    (decoy / "index.html").write_text(PRIVATE, encoding="utf-8")
    (decoy / "status.json").write_text(PRIVATE, encoding="utf-8")

    server = server_module.make_server("127.0.0.1", 0, site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert _fetch(port, "/status.json") == (200, STATUS.encode())
        parent.rename(tmp_path / "moved")
        parent.symlink_to(tmp_path / "decoy", target_is_directory=True)
        for path in ("/status.json", "/"):
            status, body = _fetch(port, path)
            assert PRIVATE.encode() not in body, path
            assert status == 200, path
        assert _fetch(port, "/status.json") == (200, STATUS.encode())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_refuses_a_root_that_is_a_symlink(tmp_path: Path) -> None:
    # The root is opened (O_NOFOLLOW) once, when the server is made, so a
    # bad root fails at startup instead of 404ing every request.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        server_module.make_server("127.0.0.1", 0, link)
