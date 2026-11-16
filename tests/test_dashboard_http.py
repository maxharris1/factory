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


@pytest.mark.parametrize("name", ["activity.html", "efficiency.html", "job.html", "health.html"])
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


# GitHub actions (issue #563). The real server runs on loopback with an
# ActionsConfig pointing at FakeGitHub below, never at github.com.

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(ROOT))
from lib.hold import _ok_head  # noqa: E402

MAX_TOKEN = "gho_LEAKCANARYmaxharris1Tok3n000000000000"
OTHER_TOKEN = "gho_LEAKCANARYsomeoneElseTok3n0000000000"
APP_TOKEN = "ghs_LEAKCANARYinstallationTok3n000000000"
BOT_TYPE_TOKEN = "gho_LEAKCANARYbotTypeTok3n00000000000000"
BOT_LOGIN_TOKEN = "gho_LEAKCANARYbotLoginTok3n0000000000000"
VIA_APP_TOKEN = "ghu_LEAKCANARYviaAppTok3n00000000000000"
DEVICE_CODE = "devcode_LEAKCANARY3584f1e2b0c9d7a6"
USER_CODE = "LKCN-7Q2Z"
REPO = "maxharris1/widgets"
HEAD = "a" * 40
MOVED = "b" * 40
SNAPSHOT = {
    "schema": "factory-dashboard/v1",
    "tickets": [
        {"repo": REPO, "ticket_id": "12", "source": "#12", "pr": "34",
         "state": "awaiting-ok", "waiting_on": "maxharris1"},
        {"repo": REPO, "ticket_id": "13", "source": "#13", "pr": "35", "state": "building"},
    ],
}
IDENTITIES = {
    MAX_TOKEN: (200, {"login": "maxharris1", "type": "User"}),
    OTHER_TOKEN: (200, {"login": "someone-else", "type": "User"}),
    APP_TOKEN: (403, {"message": "Resource not accessible by integration"}),
    BOT_TYPE_TOKEN: (200, {"login": "maxharris1", "type": "Bot"}),
    BOT_LOGIN_TOKEN: (200, {"login": "maxharris1[bot]", "type": "User"}),
    VIA_APP_TOKEN: (200, {"login": "MaxHarris1", "type": "User"}),
}


class FakeGitHub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FakeGitHubHandler)
        self.grant: str | None = None
        self.expires_in = 900
        self.heads = {(REPO, 34): HEAD, (REPO, 35): HEAD}
        self.posts: list[tuple[str, int, str, str]] = []
        self.calls: list[str] = []
        self.lock = threading.Lock()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class FakeGitHubHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def do_GET(self) -> None:
        fake: FakeGitHub = self.server  # type: ignore[assignment]
        with fake.lock:
            fake.calls.append(f"GET {self.path}")
        if self.path == "/user":
            self._reply(*IDENTITIES.get(self._token(), (401, {"message": "Bad credentials"})))
            return
        found = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls/(\d+)", self.path)
        if found and self._token() in IDENTITIES:
            self._reply(200, {"head": {"sha": fake.heads[(found[1], int(found[2]))]}})
            return
        self._reply(404, {"message": "Not Found"})

    def do_POST(self) -> None:
        fake: FakeGitHub = self.server  # type: ignore[assignment]
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with fake.lock:
            fake.calls.append(f"POST {self.path}")
        if self.path == "/login/device/code":
            assert b"client_id=Ov23liTESTCLIENT" in raw
            self._reply(200, {"device_code": DEVICE_CODE, "user_code": USER_CODE,
                              "verification_uri": "https://github.com/login/device",
                              "interval": 0, "expires_in": fake.expires_in})
            return
        if self.path == "/login/oauth/access_token":
            assert DEVICE_CODE.encode() in raw
            if fake.grant is None:
                self._reply(200, {"error": "authorization_pending"})
            elif fake.grant == "denied":
                self._reply(200, {"error": "access_denied"})
            else:
                self._reply(200, {"access_token": fake.grant, "token_type": "bearer"})
            return
        found = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments", self.path)
        token = self._token()
        if found and token in IDENTITIES and IDENTITIES[token][0] == 200:
            login = IDENTITIES[token][1]["login"]
            body = json.loads(raw)["body"]
            with fake.lock:
                fake.posts.append((found[1], int(found[2]), body, login))
                number = len(fake.posts)
            app = {"slug": "some-app"} if token == VIA_APP_TOKEN else None
            self._reply(201, {
                "html_url": f"https://github.com/{found[1]}/issues/{found[2]}#issuecomment-{number}",
                "user": {"login": login}, "performed_via_github_app": app,
            })
            return
        self._reply(401, {"message": "Bad credentials"})


class Client:
    """One browser: keeps its cookies and the csrf value the API handed it."""

    def __init__(self, port: int):
        self.port = port
        self.cookies: dict[str, str] = {}
        self.csrf: str | None = None
        self.seen: list[tuple[int, list[tuple[str, str]], bytes]] = []

    def raw(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            sent = dict(headers or {})
            if self.cookies:
                sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            connection.request(method, path, body=body or None, headers=sent)
            response = connection.getresponse()
            data = response.read()
            pairs = response.getheaders()
        finally:
            connection.close()
        self.seen.append((response.status, pairs, data))
        for key, value in pairs:
            if key.lower() == "set-cookie":
                name, _, rest = value.partition("=")
                cookie_value = rest.split(";")[0]
                assert "HttpOnly" in value and "SameSite=Strict" in value and "Path=/api" in value
                if cookie_value:
                    self.cookies[name] = cookie_value
                else:
                    self.cookies.pop(name, None)
        return response.status, data

    def post(self, path: str, payload: dict | None = None, **overrides: str | None):
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}",
            "X-Factory-CSRF": self.csrf,
        }
        headers.update(overrides)
        status, data = self.raw("POST", path, json.dumps(payload or {}).encode(),
                                {k: v for k, v in headers.items() if v is not None})
        return status, json.loads(data)

    def session(self) -> dict:
        status, data = self.raw("GET", "/api/session")
        assert status == 200
        return json.loads(data)

    def sign_in(self, fake: FakeGitHub, token: str) -> str:
        fake.grant = None
        status, grant = self.post("/api/signin")
        assert status == 200
        assert grant["user_code"] == USER_CODE and grant["interval"] == 0
        assert self.post("/api/signin/poll") == (200, {"state": "pending"})
        fake.grant = token
        status, answer = self.post("/api/signin/poll")
        assert status == 200
        self.csrf = answer.get("csrf")
        return answer["state"]


@pytest.fixture
def fake_github():
    fake = FakeGitHub()
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    yield fake
    fake.shutdown()
    fake.server_close()
    thread.join(timeout=5)


@pytest.fixture
def actions_site(site: Path) -> Path:
    (site / "status.json").write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return site


@pytest.fixture
def serve_actions(actions_site: Path, fake_github: FakeGitHub):
    servers = []

    def start(clients: tuple[str, ...] = ("127.0.0.1",)) -> Client:
        config = server_module.ActionsConfig(
            client_id="Ov23liTESTCLIENT",
            approver="maxharris1",
            clients=frozenset(server_module.ipaddress.IPv4Address(c) for c in clients),
            api_base=fake_github.base,
            web_base=fake_github.base,
        )
        server = server_module.make_server("127.0.0.1", 0, actions_site, config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return Client(server.server_address[1])

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def max_client(serve_actions, fake_github: FakeGitHub) -> Client:
    client = serve_actions()
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    return client


def comment(client: Client, body: str = "Looks good, thanks.", **target):
    return client.post("/api/comment", {"repo": REPO, "kind": "issue", "number": 12,
                                        "body": body, **target})


def test_actions_off_answers_api_like_any_other_path(get) -> None:
    assert get("/api/session")[0] == 404
    assert get("/api/signin", method="POST")[0] == 501


def test_the_approver_signs_in_and_comments_as_himself(max_client: Client, fake_github) -> None:
    assert max_client.session() == {"signed_in": True, "login": "maxharris1",
                                     "csrf": max_client.csrf}
    status, answer = comment(max_client)
    assert status == 201
    assert answer["url"].startswith(f"https://github.com/{REPO}/issues/12#issuecomment-")
    status, _ = comment(max_client, "On the PR.", kind="pull", number=34)
    assert status == 201
    assert fake_github.posts == [(REPO, 12, "Looks good, thanks.", "maxharris1"),
                                 (REPO, 34, "On the PR.", "maxharris1")]


@pytest.mark.parametrize("token", [OTHER_TOKEN, APP_TOKEN, BOT_TYPE_TOKEN, BOT_LOGIN_TOKEN,
                                   "denied"])
def test_a_token_that_is_not_the_approver_is_refused(serve_actions, fake_github, token) -> None:
    client = serve_actions()
    assert client.sign_in(fake_github, token) == "refused"
    assert client.session() == {"signed_in": False}
    assert "factory_session" not in client.cookies
    assert comment(client)[0] == 401
    assert fake_github.posts == []


def test_a_post_github_attributes_to_an_app_is_an_error(serve_actions, fake_github) -> None:
    client = serve_actions()
    assert client.sign_in(fake_github, VIA_APP_TOKEN) == "signed-in"
    assert comment(client) == (502, {"error": "wrong-author"})


def test_an_expired_device_flow_reports_expired(serve_actions, fake_github) -> None:
    fake_github.expires_in = 0
    client = serve_actions()
    assert client.post("/api/signin")[0] == 200
    assert client.post("/api/signin/poll") == (200, {"state": "expired"})
    assert client.post("/api/signin/poll") == (401, {"error": "no-flow"})


def test_poll_without_a_flow_cookie_is_refused(serve_actions) -> None:
    assert serve_actions().post("/api/signin/poll") == (401, {"error": "no-flow"})


@pytest.mark.parametrize(
    "overrides,status,code",
    [
        ({"X-Factory-CSRF": None}, 403, "csrf"),
        ({"X-Factory-CSRF": "not-the-csrf"}, 403, "csrf"),
        ({"Origin": "http://evil.example"}, 403, "origin"),
        ({"Origin": None}, 403, "origin"),
        ({"Content-Type": "application/x-www-form-urlencoded"}, 403, "content-type"),
        ({"Content-Type": "text/plain"}, 403, "content-type"),
    ],
)
def test_cross_site_and_unauthenticated_posts_are_refused(
    max_client: Client, fake_github, overrides, status, code
) -> None:
    payload = {"repo": REPO, "kind": "issue", "number": 12, "body": "x"}
    assert max_client.post("/api/comment", payload, **overrides) == (status, {"error": code})
    assert fake_github.posts == []


def test_a_post_without_the_session_cookie_is_401(max_client: Client, fake_github) -> None:
    max_client.cookies.pop("factory_session")
    assert comment(max_client) == (401, {"error": "no-session"})
    assert fake_github.posts == []


def test_an_unlisted_peer_gets_404_everywhere(serve_actions, fake_github) -> None:
    client = serve_actions(clients=("100.64.0.9",))
    assert client.raw("GET", "/api/session")[0] == 404
    for path in ("/api/signin", "/api/signin/poll", "/api/comment", "/api/ok"):
        assert client.post(path)[0] == 404, path
    assert fake_github.calls == []
    assert client.raw("GET", "/status.json")[0] == 200


@pytest.mark.parametrize(
    "target",
    [
        {"number": 99},
        {"repo": "maxharris1/other"},
        {"kind": "pull", "number": 12},
        {"kind": "issue", "number": 34},
    ],
)
def test_a_target_not_in_the_snapshot_is_404(max_client: Client, fake_github, target) -> None:
    assert comment(max_client, **target) == (404, {"error": "not-in-snapshot"})
    assert fake_github.posts == []


def test_repo_matches_the_snapshot_casefolded(max_client: Client, fake_github) -> None:
    assert comment(max_client, repo="MaxHarris1/Widgets")[0] == 201


@pytest.mark.parametrize(
    "body",
    ["/ok " + HEAD, "  /ok", "<!-- orch:v1 kind=ready -->\nhi", "", "   ", "x" * 65537],
)
def test_comment_bodies_that_could_skip_the_head_check_are_400(
    max_client: Client, fake_github, body
) -> None:
    assert comment(max_client, body)[0] == 400
    assert fake_github.posts == []


@pytest.mark.parametrize("target", [{"repo": "no-slash"}, {"number": "12x"}, {"number": True},
                                    {"kind": "commit"}, {"number": -1}])
def test_malformed_targets_are_400(max_client: Client, target) -> None:
    assert comment(max_client, **target)[0] == 400


def test_sign_out_ends_the_session(max_client: Client, fake_github) -> None:
    assert max_client.post("/api/signout") == (200, {})
    assert max_client.session() == {"signed_in": False}
    assert comment(max_client)[0] == 401
    assert fake_github.posts == []


def test_a_new_sign_in_ends_the_old_session(max_client: Client, serve_actions, fake_github):
    laptop = Client(max_client.port)
    assert laptop.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    assert max_client.session() == {"signed_in": False}
    assert comment(max_client)[0] == 401
    assert comment(laptop)[0] == 201


def ok(client: Client, head: str = HEAD, pr: object = 34):
    return client.post("/api/ok", {"repo": REPO, "pr": pr, "head": head})


def test_ok_posts_slash_ok_on_the_head_max_saw(max_client: Client, fake_github) -> None:
    assert max_client.post("/api/head", {"repo": REPO, "pr": 34}) == (200, {"head": HEAD})
    pulls_before = fake_github.calls.count(f"GET /repos/{REPO}/pulls/34")
    status, answer = ok(max_client)
    assert status == 201 and answer["url"].startswith(f"https://github.com/{REPO}/issues/34#")
    assert fake_github.calls.count(f"GET /repos/{REPO}/pulls/34") == pulls_before + 1
    (post,) = fake_github.posts
    assert post == (REPO, 34, f"/ok {HEAD}", "maxharris1")
    assert _ok_head(post[2]) == HEAD


def test_ok_on_a_moved_head_is_409_and_posts_nothing(max_client: Client, fake_github) -> None:
    assert max_client.post("/api/head", {"repo": REPO, "pr": "34"}) == (200, {"head": HEAD})
    fake_github.heads[(REPO, 34)] = MOVED
    assert ok(max_client) == (409, {"error": "head-changed", "head": MOVED})
    assert fake_github.posts == []


@pytest.mark.parametrize("head", ["abc1234", HEAD[:39], HEAD + "a", HEAD.upper(), "g" * 40, 7])
def test_ok_needs_a_full_lowercase_sha(max_client: Client, fake_github, head) -> None:
    assert ok(max_client, head) == (400, {"error": "bad-head"})
    assert fake_github.posts == []


def test_ok_on_a_ticket_not_awaiting_ok_is_refused(max_client: Client, fake_github) -> None:
    assert ok(max_client, pr=35) == (409, {"error": "not-awaiting-ok"})
    assert ok(max_client, pr=36) == (404, {"error": "not-in-snapshot"})
    assert max_client.post("/api/head", {"repo": REPO, "pr": 36})[0] == 404
    assert fake_github.posts == []


def test_ok_needs_a_session_and_csrf(max_client: Client, fake_github) -> None:
    assert max_client.post("/api/ok", {"repo": REPO, "pr": 34, "head": HEAD},
                           **{"X-Factory-CSRF": None})[0] == 403
    max_client.cookies.pop("factory_session")
    assert ok(max_client)[0] == 401
    assert fake_github.posts == []


LOG_LINE = re.compile(r"(GET|HEAD|POST) /[A-Za-z0-9./_-]* \d{3} 127\.0\.0\.1")


def test_no_credential_reaches_a_response_page_or_log(
    serve_actions, fake_github, actions_site: Path, capsys
) -> None:
    (actions_site / "activity.html").write_text("<!doctype html><title>a</title>", "utf-8")
    client = serve_actions()
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    client.session()
    assert comment(client, "canary run")[0] == 201
    assert client.post("/api/head", {"repo": REPO, "pr": 34})[0] == 200
    assert ok(client)[0] == 201
    for path in ("/", "/index.html", "/activity.html", "/status.json", "/api/session?x=1"):
        client.raw("GET", path)
    assert client.post("/api/signout")[0] == 200
    sign_in_reply = client.seen[0][2]
    assert USER_CODE.encode() in sign_in_reply
    assert DEVICE_CODE.encode() not in sign_in_reply

    session_ids = set()
    for status, headers, body in client.seen:
        exposed = body + "".join(f"{k}: {v}\n" for k, v in headers).encode()
        for secret in (MAX_TOKEN, DEVICE_CODE):
            assert secret.encode() not in exposed
        if body is not sign_in_reply:
            assert USER_CODE.encode() not in exposed
        session_ids |= {v.split(";")[0].partition("=")[2] for k, v in headers
                        if k.lower() == "set-cookie" and v.startswith("factory_session=")}
    session_ids.discard("")
    assert len(session_ids) == 1 and MAX_TOKEN not in session_ids.pop()

    for name in ("index.html", "activity.html", "status.json"):
        text = (actions_site / name).read_text(encoding="utf-8")
        assert "LEAKCANARY" not in text and USER_CODE not in text

    log = capsys.readouterr().err
    assert "LEAKCANARY" not in log and USER_CODE not in log and "canary run" not in log
    lines = log.splitlines()
    assert lines and all(LOG_LINE.fullmatch(line) for line in lines), lines
    assert "?" not in log


def _env(site: Path, **extra: str) -> dict[str, str]:
    return {"TAILNET_IP": "100.101.102.103", "DASHBOARD_DIR": str(site),
            "HOME": "/nonexistent-home", **extra}


ACTIONS_ENV = {"DASHBOARD_GITHUB_CLIENT_ID": "Ov23liTESTCLIENT",
               "DASHBOARD_APPROVER": "maxharris1",
               "DASHBOARD_ACTION_CLIENTS": "100.64.0.7, 100.64.0.8"}


def test_load_config_turns_actions_on_only_with_a_client_id(site: Path) -> None:
    *_, actions = server_module.load_config(_env(site), process_user="cursor-bot")
    assert actions is None
    *_, actions = server_module.load_config(_env(site, **ACTIONS_ENV, USER="factory-dash"),
                                            process_user="factory-dash")
    assert actions == server_module.ActionsConfig(
        "Ov23liTESTCLIENT", "maxharris1",
        frozenset({server_module.ipaddress.IPv4Address("100.64.0.7"),
                   server_module.ipaddress.IPv4Address("100.64.0.8")}),
        "https://api.github.com", "https://github.com",
    )


@pytest.mark.parametrize(
    "change,user,needle",
    [
        ({}, "cursor-bot", "bot account"),
        ({"USER": "claude-bot"}, "factory-dash", "bot account"),
        ({}, "grok", "bot account"),
        ({}, "box", "bot account"),
        ({"DASHBOARD_APPROVER": ""}, "factory-dash", "DASHBOARD_APPROVER"),
        ({"DASHBOARD_APPROVER": "max harris"}, "factory-dash", "DASHBOARD_APPROVER"),
        ({"DASHBOARD_ACTION_CLIENTS": ""}, "factory-dash", "DASHBOARD_ACTION_CLIENTS"),
        ({"DASHBOARD_ACTION_CLIENTS": "192.168.1.5"}, "factory-dash", "DASHBOARD_ACTION_CLIENTS"),
        ({"DASHBOARD_ACTION_CLIENTS": "100.64.0.7,"}, "factory-dash", "DASHBOARD_ACTION_CLIENTS"),
        ({"DASHBOARD_ACTION_CLIENTS": "100.64.0.7,100.101.102.103"}, "factory-dash",
         "own TAILNET_IP"),
        ({"DASHBOARD_GITHUB_CLIENT_ID": "id with spaces"}, "factory-dash", "client id"),
    ],
)
def test_load_config_refuses_unsafe_actions_settings(site: Path, change, user, needle) -> None:
    env = _env(site, **{**ACTIONS_ENV, "USER": "factory-dash", **change})
    with pytest.raises(server_module.ConfigError, match=needle):
        server_module.load_config(env, process_user=user)
