"""The static server for the pushed dashboard (deploy/factory-dashboard-http.py).

Each test serves a scratch directory on loopback through `make_server`
(the tailnet-only bind is enforced by `load_config`, tested in
tests/test_deploy.py) and checks that only the snapshot files (status.json, the pages and tickets.json)
are served, never anything outside the directory, never a listing.
"""

import http.client
import importlib.util
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import pytest

from lib.dashboard_pages.qa_evidence import run_page
from lib.qa_storage import check_id
from tests.test_qa_evidence import RUN as QA_RUN
from tests.test_qa_evidence_page import LANES, PNG, WEBM, Page, collect_run

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


@pytest.mark.parametrize("name", ["activity.html", "efficiency.html", "evals.html", "job.html", "health.html",
                                  "ticket.html", "settings.html"])
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


def test_serves_tickets_json_with_the_locked_down_headers(site: Path, get) -> None:
    assert get("/tickets.json")[0] == 404
    tickets = '{"generated_at_epoch":1.0,"schema":1,"tickets":{}}\n'
    (site / "tickets.json").write_text(tickets, encoding="utf-8")
    status, headers, body = get("/tickets.json")
    assert status == 200
    assert body.decode() == tickets
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in headers["content-security-policy"]


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
OAUTH_VIA_APP_TOKEN = "gho_LEAKCANARYoauthViaAppTok3n0000000000"
CLAUDE_BOT_TOKEN = "gho_LEAKCANARYclaudeBotTok3n00000000000"
GROK_TOKEN = "gho_LEAKCANARYgrokTok3n000000000000000000"
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
    "slots": {"devices": [
        {"name": "box", "local": True, "enabled": True, "slot_max": 6, "drain": False},
        {"name": "pc-wsl-bots", "local": False, "enabled": False, "slot_max": 2, "drain": True},
    ]},
    "settings": {"results": []},
}
IDENTITIES = {
    MAX_TOKEN: (200, {"login": "maxharris1", "type": "User"}),
    OTHER_TOKEN: (200, {"login": "someone-else", "type": "User"}),
    APP_TOKEN: (403, {"message": "Resource not accessible by integration"}),
    BOT_TYPE_TOKEN: (200, {"login": "maxharris1", "type": "Bot"}),
    BOT_LOGIN_TOKEN: (200, {"login": "maxharris1[bot]", "type": "User"}),
    VIA_APP_TOKEN: (200, {"login": "MaxHarris1", "type": "User"}),
    OAUTH_VIA_APP_TOKEN: (200, {"login": "maxharris1", "type": "User"}),
    CLAUDE_BOT_TOKEN: (200, {"login": "claude-bot", "type": "User"}),
    GROK_TOKEN: (200, {"login": "grok", "type": "User"}),
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
            app = {"slug": "some-app"} if token in (VIA_APP_TOKEN, OAUTH_VIA_APP_TOKEN) else None
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

    def start(clients: tuple[str, ...] = ("127.0.0.1",), spool: Path | None = None,
              approver: str = "maxharris1", audit: Path | None = None,
              relay: Path | None = None, own_address: str | None = None) -> Client:
        config = server_module.ActionsConfig(
            client_id="Ov23liTESTCLIENT",
            approver=approver,
            clients=frozenset(server_module.ipaddress.IPv4Address(c) for c in clients),
            api_base=fake_github.base,
            web_base=fake_github.base,
            settings_spool=spool,
            audit=audit,
            signin_relay=relay,
            own_address=own_address,
        )
        server = server_module.make_server("127.0.0.1", 0, actions_site, config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        client = Client(server.server_address[1])
        client.server = server
        return client

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
                                     "csrf": max_client.csrf, "account_signin": False}
    status, answer = comment(max_client)
    assert status == 201
    assert answer["url"].startswith(f"https://github.com/{REPO}/issues/12#issuecomment-")
    status, _ = comment(max_client, "On the PR.", kind="pull", number=34)
    assert status == 201
    assert fake_github.posts == [(REPO, 12, "Looks good, thanks.", "maxharris1"),
                                 (REPO, 34, "On the PR.", "maxharris1")]


@pytest.mark.parametrize("token", [OTHER_TOKEN, APP_TOKEN, BOT_TYPE_TOKEN, BOT_LOGIN_TOKEN,
                                   VIA_APP_TOKEN, CLAUDE_BOT_TOKEN, GROK_TOKEN, "denied"])
def test_a_token_that_is_not_the_approver_is_refused(serve_actions, fake_github, token) -> None:
    client = serve_actions()
    assert client.sign_in(fake_github, token) == "refused"
    assert client.session() == {"signed_in": False, "account_signin": False}
    assert "factory_session" not in client.cookies
    assert comment(client)[0] == 401
    assert fake_github.posts == []


def test_a_post_github_attributes_to_an_app_ends_the_session(serve_actions, fake_github) -> None:
    client = serve_actions()
    assert client.sign_in(fake_github, OAUTH_VIA_APP_TOKEN) == "signed-in"
    assert comment(client) == (502, {"error": "wrong-author"})
    assert client.session() == {"signed_in": False, "account_signin": False}
    assert comment(client)[0] == 401
    assert ok(client)[0] == 401
    assert len(fake_github.posts) == 1


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


OWN = "100.101.102.103"
LOCAL = (OWN, 8090)
LISTED = "100.64.0.7"
HAS_TCP_TABLE = Path("/proc/net/tcp").exists()


def proxy_client(monkeypatch, uid: int | None, peer: str, xff: str, own: str | None = OWN) -> str:
    monkeypatch.setattr(server_module, "tcp_socket_uid", lambda peer, local: uid)
    return server_module.action_client((peer, 40000), LOCAL, xff, own)


def test_action_client_uses_the_last_hop_only_from_roots_socket_on_own_address(monkeypatch) -> None:
    assert proxy_client(monkeypatch, 0, OWN, LISTED) == LISTED
    assert proxy_client(monkeypatch, 0, OWN, f"203.0.113.9, {LISTED}") == LISTED
    assert proxy_client(monkeypatch, 0, OWN, f"  {LISTED}  ") == LISTED


@pytest.mark.parametrize("own", [OWN, None])
def test_action_client_uses_the_last_hop_from_roots_socket_on_loopback(monkeypatch, own) -> None:
    for peer in ("127.0.0.1", "127.0.0.2"):
        assert proxy_client(monkeypatch, 0, peer, LISTED, own=own) == LISTED, peer
        assert proxy_client(monkeypatch, 0, peer, f"100.64.0.9, {LISTED}", own=own) == LISTED, peer


def test_action_client_ignores_xff_from_a_non_root_socket_on_a_proxy_address(monkeypatch) -> None:
    for peer in (OWN, "127.0.0.1", "127.0.0.2"):
        assert proxy_client(monkeypatch, 1001, peer, LISTED) == peer
        assert proxy_client(monkeypatch, None, peer, LISTED) == peer


def test_action_client_ignores_xff_from_any_other_peer(monkeypatch) -> None:
    assert proxy_client(monkeypatch, 0, "100.64.0.8", LISTED) == "100.64.0.8"
    assert proxy_client(monkeypatch, 0, "100.64.0.8", f"100.64.0.9, {LISTED}") == "100.64.0.8"
    assert proxy_client(monkeypatch, 0, OWN, LISTED, own=None) == OWN


def test_action_client_falls_back_to_the_peer_on_an_unusable_hop(monkeypatch) -> None:
    for xff in ("", "not-an-ip", "2001:db8::1", f"{LISTED}, "):
        assert proxy_client(monkeypatch, 0, OWN, xff) == OWN, xff


def test_tcp_socket_uid_reads_the_peer_sockets_owner(tmp_path) -> None:
    def addr(ip: str, port: int) -> str:
        packed = server_module.ipaddress.IPv4Address(ip).packed
        return f"{int.from_bytes(packed, sys.byteorder):08X}:{port:04X}"

    header = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
              "retrnsmt   uid  timeout inode")
    rows = [
        (LOCAL, (OWN, 40000), "01", 1001),
        ((OWN, 40000), LOCAL, "01", 0),
        ((OWN, 40001), LOCAL, "01", 1002),
        ((OWN, 40003), LOCAL, "05", 0),
        ((OWN, 40004), LOCAL, "06", 0),
    ]
    table = tmp_path / "tcp"
    table.write_text("\n".join([header] + [
        f"   {n}: {addr(*a)} {addr(*b)} {st} 00000000:00000000 00:00000000 00000000 {uid:>5} 0 {1000 + n} 1"
        for n, (a, b, st, uid) in enumerate(rows)
    ]) + "\n")
    fn = server_module.tcp_socket_uid
    assert fn((OWN, 40000), LOCAL, table) == 0
    assert fn((OWN, 40001), LOCAL, table) == 1002
    assert fn((OWN, 40002), LOCAL, table) is None
    assert fn((OWN, 40003), LOCAL, table) is None
    assert fn((OWN, 40004), LOCAL, table) is None
    assert fn((OWN, 40000), LOCAL, tmp_path / "missing") is None


@pytest.mark.skipif(not HAS_TCP_TABLE, reason="needs /proc/net/tcp")
@pytest.mark.skipif(os.geteuid() == 0, reason="test process is the proxy uid")
def test_tcp_socket_uid_does_not_report_a_disconnected_peer_as_root() -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    client = socket.create_connection(listener.getsockname())
    accepted, _ = listener.accept()
    try:
        peer = accepted.getpeername()[:2]
        local = accepted.getsockname()[:2]
        assert server_module.tcp_socket_uid(peer, local) == os.geteuid()
        client.close()
        deadline = time.monotonic() + 2
        uid = os.geteuid()
        while uid == os.geteuid() and time.monotonic() < deadline:
            time.sleep(0.01)
            uid = server_module.tcp_socket_uid(peer, local)
        assert uid is None
    finally:
        client.close()
        accepted.close()
        listener.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="test process is the proxy uid")
def test_forged_xff_from_a_local_non_root_process_is_ignored(serve_actions, fake_github) -> None:
    for own in ("127.0.0.1", None):
        client = serve_actions(clients=(LISTED,), own_address=own)
        assert client.post("/api/signin", **{"X-Forwarded-For": LISTED})[0] == 404, own
    assert fake_github.calls == []


@pytest.mark.skipif(not HAS_TCP_TABLE, reason="needs /proc/net/tcp")
def test_xff_from_the_proxy_socket_can_sign_in(serve_actions, fake_github, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "TAILSCALED_UID", os.geteuid())
    client = serve_actions(clients=(LISTED,), own_address="127.0.0.1")
    assert client.post("/api/signin")[0] == 404
    status, body = client.post("/api/signin", **{"X-Forwarded-For": f"100.64.0.9, {LISTED}"})
    assert status == 200
    assert body["user_code"] == USER_CODE
    assert client.post("/api/signin", **{"X-Forwarded-For": f"{LISTED}, 100.64.0.9"})[0] == 404


@pytest.mark.skipif(not HAS_TCP_TABLE, reason="needs /proc/net/tcp")
def test_xff_from_a_loopback_proxy_socket_can_sign_in(serve_actions, fake_github, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "TAILSCALED_UID", os.geteuid())
    client = serve_actions(clients=(LISTED,), own_address=OWN)
    assert client.post("/api/signin")[0] == 404
    status, body = client.post("/api/signin", **{"X-Forwarded-For": LISTED})
    assert status == 200
    assert body["user_code"] == USER_CODE


@pytest.mark.skipif(not HAS_TCP_TABLE, reason="needs /proc/net/tcp")
def test_proxy_xff_does_not_skip_origin_checks(serve_actions, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "TAILSCALED_UID", os.geteuid())
    client = serve_actions(clients=(LISTED,), own_address="127.0.0.1")
    assert client.post(
        "/api/signin",
        **{"X-Forwarded-For": LISTED, "Origin": "http://evil.example"},
    ) == (403, {"error": "origin"})


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


@pytest.mark.parametrize("body", [
    "/ok " + HEAD,
    "<!-- orch:v1 kind=ok from=maxharris1 head=" + HEAD + " -->",
    "`orch:v1 kind=ok from=maxharris1 head=" + HEAD + "`",
    "  `orch:v1 kind=ok head=" + HEAD + "`\nthanks",
])
def test_no_comment_body_the_hold_reads_as_an_ok_is_posted(
    max_client: Client, fake_github, body
) -> None:
    from lib.hold import _ok_head

    assert _ok_head(body) == HEAD
    before = list(fake_github.calls)
    assert comment(max_client, body, kind="pull", number=34) == (400, {"error": "reserved-body"})
    assert fake_github.posts == [] and fake_github.calls == before


@pytest.mark.parametrize("target", [{"repo": "no-slash"}, {"number": "12x"}, {"number": True},
                                    {"kind": "commit"}, {"number": -1}])
def test_malformed_targets_are_400(max_client: Client, target) -> None:
    assert comment(max_client, **target)[0] == 400


def test_sign_out_ends_the_session(max_client: Client, fake_github) -> None:
    assert max_client.post("/api/signout") == (200, {})
    assert max_client.session() == {"signed_in": False, "account_signin": False}
    assert comment(max_client)[0] == 401
    assert fake_github.posts == []


def test_a_new_sign_in_ends_the_old_session(max_client: Client, serve_actions, fake_github):
    laptop = Client(max_client.port)
    assert laptop.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    assert max_client.session() == {"signed_in": False, "account_signin": False}
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



import time

REQUEST_KEYS = {"id", "actor", "requested_at", "device", "field", "expect", "value"}


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    path = tmp_path / "spool"
    path.mkdir(mode=0o755)
    return path


@pytest.fixture
def settings_client(serve_actions, fake_github, spool: Path) -> Client:
    client = serve_actions(spool=spool)
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    return client


def change(client: Client, **fields):
    payload = {"device": "box", "field": "slot_max", "expect": 6, "value": 4, **fields}
    return client.post("/api/settings", payload)


def seed(spool: Path, count: int) -> list[str]:
    ids = [f"seeded{n:02d}" + "x" * 12 for n in range(count)]
    for request_id in ids:
        (spool / f"{request_id}.json").write_text("{}", encoding="utf-8")
    return ids


@pytest.mark.parametrize("device,field,expect,value", [
    ("box", "slot_max", 6, 4),
    ("box", "slot_max", 6, 64),
    ("box", "enabled", True, False),
    ("box", "drain", False, True),
    ("pc-wsl-bots", "enabled", False, True),
    ("pc-wsl-bots", "slot_max", 2, 1),
])
def test_the_approver_queues_exactly_the_request(
    settings_client: Client, spool: Path, device, field, expect, value
) -> None:
    status, answer = change(settings_client, device=device, field=field, expect=expect,
                            value=value)
    assert status == 202 and set(answer) == {"id"}
    (path,) = spool.iterdir()
    assert path.name == f"{answer['id']}.json"
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", answer["id"])
    assert path.stat().st_mode & 0o777 == 0o644
    text = path.read_text(encoding="utf-8")
    request = json.loads(text)
    assert set(request) == REQUEST_KEYS
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", request.pop("requested_at"))
    assert request == {"id": answer["id"], "actor": "maxharris1", "device": device,
                       "field": field, "expect": expect, "value": value}
    session = settings_client.server.actions.session
    for secret in (MAX_TOKEN, session.session_id, session.csrf, "127.0.0.1"):
        assert secret not in text


@pytest.mark.parametrize("fields,status,code", [
    ({"field": "name"}, 400, "bad-field"),
    ({"field": "local"}, 400, "bad-field"),
    ({"field": None}, 400, "bad-field"),
    ({"field": ["slot_max"]}, 400, "bad-field"),
    ({"value": 0}, 400, "bad-value"),
    ({"value": 65}, 400, "bad-value"),
    ({"value": True}, 400, "bad-value"),
    ({"value": "6"}, 400, "bad-value"),
    ({"value": 4.0}, 400, "bad-value"),
    ({"value": None}, 400, "bad-value"),
    ({"expect": "6"}, 400, "bad-value"),
    ({"expect": True}, 400, "bad-value"),
    ({"field": "enabled", "expect": True, "value": 0}, 400, "bad-value"),
    ({"field": "drain", "expect": 0, "value": True}, 400, "bad-value"),
    ({"field": "drain", "expect": False, "value": "true"}, 400, "bad-value"),
    ({"device": "nope"}, 404, "not-in-snapshot"),
    ({"device": "BOX"}, 404, "not-in-snapshot"),
    ({"device": None}, 404, "not-in-snapshot"),
    ({"expect": 5}, 409, "changed"),
    ({"field": "enabled", "expect": False, "value": True}, 409, "changed"),
    ({"field": "drain", "expect": True, "value": False}, 409, "changed"),
])
def test_a_bad_or_stale_change_is_refused_and_writes_nothing(
    settings_client: Client, spool: Path, fields, status, code
) -> None:
    assert change(settings_client, **fields) == (status, {"error": code})
    assert list(spool.iterdir()) == []


def test_a_settings_post_needs_a_session_and_csrf(settings_client: Client, spool: Path) -> None:
    payload = {"device": "box", "field": "slot_max", "expect": 6, "value": 4}
    for overrides in ({"X-Factory-CSRF": None}, {"X-Factory-CSRF": "not-the-csrf"}):
        assert settings_client.post("/api/settings", payload, **overrides) == (
            403, {"error": "csrf"})
    settings_client.cookies.pop("factory_session")
    assert change(settings_client) == (401, {"error": "no-session"})
    assert list(spool.iterdir()) == []


def test_an_unlisted_peer_gets_404_for_settings(serve_actions, spool: Path) -> None:
    client = serve_actions(clients=("100.64.0.9",), spool=spool)
    assert change(client) == (404, {"error": "not-found"})
    assert list(spool.iterdir()) == []


def test_without_a_spool_settings_is_an_unknown_route(max_client: Client) -> None:
    assert change(max_client) == (404, {"error": "not-found"})


def _forge(client: Client, login: str) -> None:
    session = server_module.Session("forged-session-" + login, login, "gho_forged",
                                    "forged-csrf", time.monotonic() + 60)
    client.server.actions.session = session
    client.cookies["factory_session"] = session.session_id
    client.csrf = session.csrf


@pytest.mark.parametrize("login", ["claude-bot", "grok", "someone-else", "x[bot]",
                                   "maxharris1[bot]"])
def test_a_session_for_another_login_is_not_the_approver(
    serve_actions, spool: Path, login: str
) -> None:
    client = serve_actions(spool=spool)
    _forge(client, login)
    assert change(client) == (403, {"error": "not-approver"})
    assert list(spool.iterdir()) == []


def test_a_bot_login_is_refused_even_as_the_configured_approver(
    serve_actions, spool: Path
) -> None:
    client = serve_actions(spool=spool, approver="claude-bot")
    _forge(client, "claude-bot")
    assert change(client) == (403, {"error": "not-approver"})
    assert list(spool.iterdir()) == []


def test_the_approver_check_is_casefolded(serve_actions, spool: Path) -> None:
    client = serve_actions(spool=spool)
    _forge(client, "MaxHarris1")
    status, answer = change(client)
    assert status == 202
    assert json.loads((spool / f"{answer['id']}.json").read_text())["actor"] == "MaxHarris1"


def test_more_than_twenty_pending_requests_is_busy(settings_client: Client, spool: Path) -> None:
    seed(spool, 19)
    assert change(settings_client)[0] == 202
    assert change(settings_client) == (429, {"error": "busy"})
    assert len(list(spool.iterdir())) == 20


def test_applied_requests_are_cleared_before_counting(
    settings_client: Client, spool: Path, actions_site: Path
) -> None:
    ids = seed(spool, 20)
    (spool / ".half-written.tmp").write_text("", encoding="utf-8")
    assert change(settings_client) == (429, {"error": "busy"})
    snapshot = {**SNAPSHOT, "settings": {"results": [{"id": ids[0]}, {"id": "not-here" * 3}]}}
    (actions_site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    status, answer = change(settings_client)
    assert status == 202
    names = {path.name for path in spool.iterdir()}
    assert f"{ids[0]}.json" not in names
    assert names == {f"{i}.json" for i in ids[1:]} | {f"{answer['id']}.json", ".half-written.tmp"}


LOG_LINE = re.compile(r"(GET|HEAD|POST) /[A-Za-z0-9./_-]* \d{3} 127\.0\.0\.1")


GITHUB_CREDENTIAL = re.compile(r"gh[opsu]_|github_pat_")


def test_no_credential_reaches_a_response_page_or_log(
    serve_actions, fake_github, actions_site: Path, spool: Path, capsys
) -> None:
    for name in ("activity.html", "settings.html"):
        (actions_site / name).write_text(f"<!doctype html><title>{name}</title>", "utf-8")
    (actions_site / "status.json").write_text(json.dumps(ACT_SNAPSHOT), encoding="utf-8")
    audit = spool.parent / "actions-audit.jsonl"
    client = serve_actions(spool=spool, audit=audit)
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    client.session()
    assert comment(client, "canary run")[0] == 201
    assert client.post("/api/head", {"repo": REPO, "pr": 34})[0] == 200
    assert ok(client)[0] == 201
    assert act(client, "43", "answer", "canary run")[0] == 201
    assert act(client, "43", "answer", "canary run", expect_state="capped")[0] == 409
    assert act(client, "12", "ok", head=HEAD)[0] == 201
    settings_from = len(client.seen)
    assert change(client)[0] == 202
    assert change(client, expect=5)[0] == 409
    assert change(client, field="token")[0] == 400
    settings_replies = [body for _, _, body in client.seen[settings_from:]]
    session = client.server.actions.session
    for path in ("/", "/index.html", "/activity.html", "/settings.html", "/status.json",
                 "/api/session?x=1"):
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

    secrets = (session.session_id, session.csrf, DEVICE_CODE, USER_CODE, "LEAKCANARY")
    for reply in settings_replies:
        text = reply.decode()
        assert not any(secret in text for secret in secrets) and not GITHUB_CREDENTIAL.search(text)

    written = [path.read_text(encoding="utf-8") for path in spool.iterdir()]
    pages = [(actions_site / name).read_text(encoding="utf-8")
             for name in ("index.html", "activity.html", "settings.html", "status.json")]
    audited = audit.read_text(encoding="utf-8")
    assert len(written) == 1
    assert len(audited.splitlines()) == 3 and "canary run" not in audited
    for text in written + pages + [audited]:
        assert not any(secret in text for secret in secrets), text
        assert not GITHUB_CREDENTIAL.search(text), text

    log = capsys.readouterr().err
    assert not any(secret in log for secret in secrets) and "canary run" not in log
    assert not GITHUB_CREDENTIAL.search(log)
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
        own_address="100.101.102.103",
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


def test_load_config_takes_the_settings_spool_with_actions(site: Path, spool: Path) -> None:
    env = _env(site, **ACTIONS_ENV, USER="factory-dash", DASHBOARD_SETTINGS_SPOOL=str(spool))
    *_, actions = server_module.load_config(env, process_user="factory-dash")
    assert actions.settings_spool == spool


@pytest.mark.parametrize("case,needle", [
    ("relative", "absolute"),
    ("missing", "does not exist"),
    ("file", "not a directory"),
    ("symlink", "symlink"),
    ("group-writable", "group- or world-writable"),
    ("world-writable", "group- or world-writable"),
    ("without-actions", "needs GitHub actions"),
])
def test_load_config_refuses_an_unsafe_settings_spool(
    site: Path, spool: Path, case: str, needle: str
) -> None:
    (spool.parent / "plain-file").write_text("x", encoding="utf-8")
    (spool.parent / "spool-link").symlink_to(spool, target_is_directory=True)
    if case.endswith("-writable"):
        spool.chmod(0o775 if case == "group-writable" else 0o757)
    value = {
        "relative": "spool",
        "missing": str(spool.parent / "nope"),
        "file": str(spool.parent / "plain-file"),
        "symlink": str(spool.parent / "spool-link"),
    }.get(case, str(spool))
    actions = {} if case == "without-actions" else ACTIONS_ENV
    env = _env(site, **actions, USER="factory-dash", DASHBOARD_SETTINGS_SPOOL=value)
    with pytest.raises(server_module.ConfigError, match=needle):
        server_module.load_config(env, process_user="factory-dash")



from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from lib.handoff import parse_comment  # noqa: E402
from lib.hold import _approver_ok  # noqa: E402

LINEAR_REPO = "maxharris1/aut0"


def _row(ticket_id: str, state: str, why: str | None, actions: list[str], *,
         repo: str = REPO, pr: str | None = None, source: str | None = None,
         comment_id: str | None = None) -> dict:
    wait = None if why is None else {"why": why, "comment_id": comment_id, "detail": None}
    return {"repo": repo, "ticket_id": ticket_id, "source": source or f"#{ticket_id}", "pr": pr,
            "state": state, "wait": wait, "actions": actions}


ROWS = {
    "12": _row("12", "awaiting-ok", "awaiting-ok", ["ok", "close", "hand-off"], pr="34", comment_id="7001"),
    "13": _row("13", "building", None, [], pr="35"),
    "40": _row("40", "waiting-max", "plan-size", ["approve-plan", "split", "close", "hand-off"],
               comment_id="9001"),
    "41": _row("41", "waiting-max", "plan-size", ["approve-plan", "split", "close", "hand-off"]),
    "42": _row("42", "capped", "capped", ["another-round", "close", "hand-off"], pr="52"),
    "43": _row("43", "waiting-max", "question", ["answer", "close", "hand-off"], comment_id="9003"),
    "44": _row("44", "fixing", "stuck", ["retry", "close", "hand-off"]),
    "AUT-7": _row("AUT-7", "waiting-external", "waiting-external", ["close", "hand-off"],
                  repo=LINEAR_REPO, source="AUT-7"),
    "AUT-8": _row("AUT-8", "blocked", "blocked", ["resume", "close", "hand-off"], repo=LINEAR_REPO,
                  pr="56", source="AUT-8"),
}
ACT_SNAPSHOT = {**SNAPSHOT, "tickets": list(ROWS.values())}
WINTER = datetime(2026, 1, 15, 20, 4, tzinfo=timezone.utc)
SUMMER = datetime(2026, 7, 15, 13, 5, tzinfo=timezone.utc)
WINTER_WORDS = "**Max's words** (Max 2026-01-15 3:04 PM ET)\n> first line\n> second line\n"
SUMMER_WORDS = "**Max's words** (Max 2026-07-15 9:05 AM ET)\n> first line\n> second line\n"
GO_LINE = "<!-- orch:v1 kind=go from=approver role=approver state=building -->"
CLOSE_LINE = "<!-- orch:v1 kind=answers from=approver role=approver state=abandoned -->"
HANDOFF_LINE = "<!-- orch:v1 kind=answers from=approver role=approver state=handed-off -->"


def act(client: Client, ticket_id: str, action: str,
        words: object = "first line\nsecond line", /, **overrides):
    row = ROWS[ticket_id]
    payload = {"repo": row["repo"], "ticket_id": ticket_id, "action": action,
               "expect_state": row["state"], "expect_why": (row["wait"] or {}).get("why"),
               "words": words, **overrides}
    return client.post("/api/act", payload)


def audit_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def audit(tmp_path: Path) -> Path:
    return tmp_path / "actions-audit.jsonl"


@pytest.fixture
def act_client(serve_actions, fake_github, actions_site: Path, audit: Path, monkeypatch):
    (actions_site / "status.json").write_text(json.dumps(ACT_SNAPSHOT), encoding="utf-8")
    monkeypatch.setattr(server_module, "clock", lambda: WINTER)
    client = serve_actions(audit=audit)
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    return client


@pytest.mark.parametrize("ticket_id,action,at,extra,repo,number,body", [
    ("12", "ok", WINTER, {"head": HEAD}, REPO, 34,
     f"<!-- orch:v1 kind=ok head={HEAD} -->\n\n{WINTER_WORDS}"),
    ("40", "approve-plan", SUMMER, {}, REPO, 40, f"{GO_LINE}\n\n{SUMMER_WORDS}"),
    ("40", "split", WINTER, {}, REPO, 40,
     f"{CLOSE_LINE}\n\nSplit per the plan's Proposed split: "
     f"https://github.com/{REPO}/issues/40#issuecomment-9001\n\n{WINTER_WORDS}"),
    ("41", "split", SUMMER, {}, REPO, 41,
     f"{CLOSE_LINE}\n\nSplit per the plan's Proposed split.\n\n{SUMMER_WORDS}"),
    ("42", "another-round", WINTER, {}, REPO, 52,
     f"{GO_LINE}\n\n**Ticket**\n{REPO}#42\n\n{WINTER_WORDS}\n"
     "**Instruction**\nRun one more round, as Max's words above say.\n"),
    ("43", "answer", SUMMER, {}, REPO, 43,
     f"<!-- orch:v1 kind=answers from=approver role=approver -->\n\n{SUMMER_WORDS}"),
    ("44", "retry", WINTER, {}, REPO, 44,
     f"<!-- orch:v1 kind=retry from=approver role=approver -->\n\n{WINTER_WORDS}"),
    ("AUT-8", "resume", SUMMER, {}, LINEAR_REPO, 56, f"{GO_LINE}\n\n{SUMMER_WORDS}"),
    ("43", "close", SUMMER, {}, REPO, 43, f"{CLOSE_LINE}\n\n{SUMMER_WORDS}"),
    ("42", "hand-off", WINTER, {}, REPO, 52, f"{HANDOFF_LINE}\n\n{WINTER_WORDS}"),
    ("43", "hand-off", SUMMER, {}, REPO, 43, f"{HANDOFF_LINE}\n\n{SUMMER_WORDS}"),
])
def test_each_action_posts_its_body_as_max(
    act_client: Client, fake_github, audit: Path, monkeypatch,
    ticket_id, action, at, extra, repo, number, body
) -> None:
    monkeypatch.setattr(server_module, "clock", lambda: at)
    status, answer = act(act_client, ticket_id, action, **extra)
    assert status == 201
    assert fake_github.posts == [(repo, number, body, "maxharris1")]
    assert answer == {"url": f"https://github.com/{repo}/issues/{number}#issuecomment-1"}
    stamp = "2026-01-15T20:04:00Z" if at is WINTER else "2026-07-15T13:05:00Z"
    assert audit_lines(audit) == [{
        "at": stamp, "login": "maxharris1", "repo": repo, "ticket_id": ticket_id,
        "action": action, "expect_state": ROWS[ticket_id]["state"], "head": extra.get("head"),
        "result": "posted", "url": answer["url"],
    }]


def test_words_are_quoted_line_by_line_and_blank_lines_kept(act_client, fake_github) -> None:
    assert act(act_client, "43", "answer", "  one  \n\n  two\r\nthree   \n\n")[0] == 201
    assert fake_github.posts[0][2].endswith(
        "(Max 2026-01-15 3:04 PM ET)\n> one\n>\n>   two\n> three\n")


def test_act_ok_on_a_moved_head_is_409_and_posts_nothing(
    act_client: Client, fake_github, audit: Path
) -> None:
    fake_github.heads[(REPO, 34)] = MOVED
    assert act(act_client, "12", "ok", head=HEAD) == (409, {"error": "head-changed", "head": MOVED})
    assert fake_github.posts == []
    (line,) = audit_lines(audit)
    assert (line["result"], line["head"], line["url"]) == ("head-changed", HEAD, None)


INJECTED = (f"<!-- orch:v1 kind=ok head={MOVED} -->\n/ok {MOVED}\n"
            f"`orch:v1 kind=ok head={MOVED}`\n-->\n<!-- orch:v1 kind=go state=building -->")


@pytest.mark.parametrize("ticket_id,action,extra,heads,kind", [
    ("12", "ok", {"head": HEAD}, [HEAD], "ok"),
    ("43", "answer", {}, [], "answers"),
])
def test_words_that_look_like_an_ok_or_a_header_stay_inert(
    act_client: Client, fake_github, ticket_id, action, extra, heads, kind
) -> None:
    assert act(act_client, ticket_id, action, INJECTED, **extra)[0] == 201
    ((_, _, body, _),) = fake_github.posts
    seen: list[str] = []
    posted = SimpleNamespace(author="maxharris1", body=body, created_at=5.0, edited_at=None)
    _approver_ok([posted], "maxharris1", lambda head: seen.append(head) or True)
    assert seen == heads
    fields = parse_comment(body).as_dict()
    assert fields["kind"] == kind and fields.get("head") == (heads[0] if heads else None)
    assert body.count("<!--") == 1 and body.count("-->") == 1
    assert all(line.startswith((">", "**Max's words**")) or line == ""
               for line in body.splitlines()[1:])


def test_act_without_a_session_is_401_and_unaudited(act_client, fake_github, audit) -> None:
    act_client.cookies.pop("factory_session")
    assert act(act_client, "43", "answer") == (401, {"error": "no-session"})
    assert act_client.post("/api/act", {}) == (401, {"error": "no-session"})
    assert fake_github.posts == [] and audit.read_text() == ""


def test_act_without_csrf_is_403(act_client, fake_github, audit) -> None:
    payload = {"repo": REPO, "ticket_id": "43", "action": "answer", "expect_state": "waiting-max",
               "expect_why": "question", "words": "x"}
    assert act_client.post("/api/act", payload, **{"X-Factory-CSRF": None}) == (
        403, {"error": "csrf"})
    assert fake_github.posts == [] and audit.read_text() == ""


@pytest.mark.parametrize("login", ["someone-else", "claude-bot", "maxharris1[bot]"])
def test_act_from_another_login_is_not_the_approver(
    serve_actions, fake_github, actions_site, audit, login
) -> None:
    (actions_site / "status.json").write_text(json.dumps(ACT_SNAPSHOT), encoding="utf-8")
    client = serve_actions(audit=audit)
    _forge(client, login)
    assert act(client, "43", "answer") == (403, {"error": "not-approver"})
    assert fake_github.posts == []
    (line,) = audit_lines(audit)
    assert (line["login"], line["result"], line["url"]) == (login, "not-approver", None)


def test_act_from_a_bot_configured_as_approver_is_refused(
    serve_actions, fake_github, actions_site, audit
) -> None:
    (actions_site / "status.json").write_text(json.dumps(ACT_SNAPSHOT), encoding="utf-8")
    client = serve_actions(audit=audit, approver="claude-bot")
    _forge(client, "claude-bot")
    assert act(client, "43", "answer") == (403, {"error": "not-approver"})
    assert fake_github.posts == []


def test_act_from_an_unlisted_peer_is_404(serve_actions, fake_github, audit) -> None:
    client = serve_actions(clients=("100.64.0.9",), audit=audit)
    assert act(client, "43", "answer") == (404, {"error": "not-found"})
    assert fake_github.calls == [] and audit.read_text() == ""


def test_without_an_audit_file_act_is_an_unknown_route(max_client: Client, fake_github) -> None:
    assert act(max_client, "43", "answer") == (404, {"error": "not-found"})
    assert fake_github.posts == []


@pytest.mark.parametrize("ticket_id,action,overrides,status,code", [
    ("43", "answer", {"expect_state": "capped"}, 409, "changed"),
    ("43", "answer", {"expect_why": "plan-size"}, 409, "changed"),
    ("43", "answer", {"expect_why": None}, 409, "changed"),
    ("40", "answer", {}, 409, "not-applicable"),
    ("13", "close", {}, 409, "not-applicable"),
    ("AUT-7", "close", {}, 409, "no-target"),
    ("43", "answer", {"ticket_id": "99"}, 404, "not-in-snapshot"),
    ("43", "answer", {"ticket_id": 43}, 404, "not-in-snapshot"),
    ("43", "answer", {"repo": "maxharris1/other"}, 404, "not-in-snapshot"),
    ("43", "answer", {"repo": LINEAR_REPO}, 404, "not-in-snapshot"),
    ("43", "answer", {"words": ""}, 400, "bad-words"),
    ("43", "answer", {"words": " \n\t "}, 400, "bad-words"),
    ("43", "answer", {"words": ["x"]}, 400, "bad-words"),
    ("43", "answer", {"words": "x" * 32769}, 400, "bad-words"),
    ("43", "comment", {}, 400, "bad-action"),
    ("43", "answer", {"action": None}, 400, "bad-action"),
    ("12", "ok", {}, 400, "bad-head"),
    ("12", "ok", {"head": HEAD.upper()}, 400, "bad-head"),
    ("12", "ok", {"head": HEAD[:12]}, 400, "bad-head"),
])
def test_a_bad_or_stale_act_is_refused_posts_nothing_and_is_audited(
    act_client: Client, fake_github, audit, ticket_id, action, overrides, status, code
) -> None:
    assert act(act_client, ticket_id, action, **overrides) == (status, {"error": code})
    assert fake_github.posts == []
    (line,) = audit_lines(audit)
    assert (line["result"], line["url"], line["login"]) == (code, None, "maxharris1")
    assert set(line) == {"at", "login", "repo", "ticket_id", "action", "expect_state", "head",
                         "result", "url"}


def test_the_longest_accepted_words_post(act_client: Client, fake_github) -> None:
    assert act(act_client, "43", "answer", "x" * 32768)[0] == 201


def test_the_repo_matches_the_snapshot_casefolded(act_client: Client, fake_github) -> None:
    assert act(act_client, "43", "answer", repo="MaxHarris1/Widgets")[0] == 201
    assert fake_github.posts[0][:2] == (REPO, 43)


def test_every_attempt_appends_one_line(act_client: Client, audit: Path) -> None:
    assert act(act_client, "43", "answer", expect_state="capped")[0] == 409
    assert act(act_client, "43", "answer")[0] == 201
    assert act(act_client, "40", "split")[0] == 201
    assert [(line["ticket_id"], line["result"]) for line in audit_lines(audit)] == [
        ("43", "changed"), ("43", "posted"), ("40", "posted")]
    assert audit.stat().st_mode & 0o777 == 0o600
    assert all(" " not in line for line in audit.read_text().splitlines())


def test_load_config_takes_the_actions_audit_with_actions(site: Path, audit: Path) -> None:
    env = _env(site, **ACTIONS_ENV, USER="factory-dash", DASHBOARD_ACTIONS_AUDIT=str(audit))
    *_, actions = server_module.load_config(env, process_user="factory-dash")
    assert actions.audit == audit
    audit.write_text("", encoding="utf-8")
    audit.chmod(0o600)
    *_, actions = server_module.load_config(env, process_user="factory-dash")
    assert actions.audit == audit


@pytest.mark.parametrize("case,needle", [
    ("relative", "absolute"),
    ("symlink", "symlink"),
    ("directory", "not a regular file"),
    ("group-writable", "group- or world-writable"),
    ("world-writable", "group- or world-writable"),
    ("writable-directory", "directory must not be group- or world-writable"),
    ("missing-directory", "directory does not exist"),
    ("without-actions", "needs GitHub actions"),
])
def test_load_config_refuses_an_unsafe_actions_audit(
    site: Path, tmp_path: Path, case: str, needle: str
) -> None:
    folder = tmp_path / "audit"
    folder.mkdir(mode=0o755)
    path = folder / "actions.jsonl"
    path.write_text("", encoding="utf-8")
    (folder / "link.jsonl").symlink_to(path)
    (folder / "dir.jsonl").mkdir()
    path.chmod({"group-writable": 0o620, "world-writable": 0o602}.get(case, 0o600))
    if case == "writable-directory":
        folder.chmod(0o775)
    value = {
        "relative": "audit/actions.jsonl",
        "symlink": str(folder / "link.jsonl"),
        "directory": str(folder / "dir.jsonl"),
        "missing-directory": str(tmp_path / "nope" / "actions.jsonl"),
    }.get(case, str(path))
    actions = {} if case == "without-actions" else ACTIONS_ENV
    env = _env(site, **actions, USER="factory-dash", DASHBOARD_ACTIONS_AUDIT=value)
    with pytest.raises(server_module.ConfigError, match=needle):
        server_module.load_config(env, process_user="factory-dash")


QA_PAGE = f"/qa/runs/{QA_RUN}"


@pytest.fixture
def qa_run(site: Path, tmp_path: Path) -> Path:
    run = site / "qa" / QA_RUN
    run.parent.mkdir()
    collect_run(tmp_path / "evidence").rename(run)
    (run / "index.html").write_text(run_page(run, QA_RUN), encoding="utf-8")
    return run


def _without_date(headers: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key != "date"}


def test_a_qa_run_page_and_its_evidence_are_served(qa_run: Path, get) -> None:
    status, headers, body = get(QA_PAGE)
    assert (status, headers["content-type"]) == (200, "text/html; charset=utf-8")
    assert "img-src 'self'" in headers["content-security-policy"]
    page = Page(body.decode())
    for scenarios in LANES.values():
        for scenario, outcome in scenarios.items():
            assert scenario in page.text and outcome in page.text

    served = {"img": ("image/png", PNG), "video": ("video/webm", WEBM)}
    linked = [(tag, link) for tag, link in page.links if tag in served]
    assert {tag for tag, _ in linked} == set(served)
    for tag, link in linked:
        path = urlsplit(urljoin(QA_PAGE, link)).path
        status, headers, body = get(path)
        assert (status, headers["content-type"], body) == (200, *served[tag]), path
        assert "media-src 'self'" in headers["content-security-policy"]
        head_status, head_headers, head_body = get(path, method="HEAD")
        assert (head_status, head_body, _without_date(head_headers)) == (200, b"", _without_date(headers))

    for name in ("app.log", "login/dom.html"):
        status, headers, _ = get(f"{QA_PAGE}/lane-1/{name}")
        assert (status, headers["content-type"]) == (200, "text/plain; charset=utf-8"), name
        assert headers["content-disposition"] == "attachment"
    snapshot_csp = get("/")[1]["content-security-policy"]
    assert snapshot_csp == server_module.SECURITY_HEADERS["Content-Security-Policy"]


def test_evidence_over_the_snapshot_cap_is_served_whole(
    qa_run: Path, get, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "MAX_BYTES", 16)
    data = os.urandom(3 * 1024 * 1024 + 7)
    (qa_run / "lane-1" / "login" / "long.webm").write_bytes(data)
    status, headers, body = get(f"{QA_PAGE}/lane-1/login/long.webm")
    assert (status, headers["content-length"], body == data) == (200, str(len(data)), True)


def _plant(relative: str, make: Callable[[Path, Path], object]) -> Callable[[Path], None]:
    def plant(run: Path) -> None:
        target = run / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        make(target, run.parents[2])
    return plant


def _write_private(target: Path, _outside: Path) -> None:
    target.write_text(PRIVATE, encoding="utf-8")


@pytest.mark.parametrize("path,plant", [
    pytest.param(f"{QA_PAGE}/lane-1/../../../../private.txt", None, id="dot-dot"),
    pytest.param(f"{QA_PAGE}/lane-1/%2e%2e/%2e%2e/%2e%2e/%2e%2e/private.txt", None, id="encoded-dot-dot"),
    pytest.param(f"{QA_PAGE}/lane-1/login%2fshot.png", None, id="encoded-slash"),
    pytest.param(f"{QA_PAGE}/lane-1/login%5cshot.png", None, id="encoded-backslash"),
    pytest.param(f"{QA_PAGE}/lane-1/login/shot.png%00", None, id="encoded-nul"),
    pytest.param("/qa/runs/../../private.txt", None, id="run-dot-dot"),
    pytest.param(f"{QA_PAGE}/lane-1/.shot.png.push-tmp",
                 _plant("lane-1/.shot.png.push-tmp", _write_private), id="dot-file"),
    pytest.param(f"{QA_PAGE}/lane-1/login/leak.png",
                 _plant("lane-1/login/leak.png", lambda t, out: t.symlink_to(out / "private.txt")),
                 id="file-symlink"),
    pytest.param(f"{QA_PAGE}/lane-1/outside/private.txt",
                 _plant("lane-1/outside", lambda t, out: t.symlink_to(out, target_is_directory=True)),
                 id="directory-symlink"),
    pytest.param(f"{QA_PAGE}/lane-1/login/hard.png",
                 _plant("lane-1/login/hard.png", lambda t, out: os.link(out / "private.txt", t)),
                 id="hard-link"),
    pytest.param(f"{QA_PAGE}/lane-0/x", _plant("lane-0/x", _write_private), id="lane-0"),
    pytest.param(f"{QA_PAGE}/notes/x", _plant("notes/x", _write_private), id="not-a-lane"),
    pytest.param(f"{QA_PAGE}/lane-1/login", None, id="directory"),
    pytest.param("/qa/runs/run-8", None, id="unknown-run"),
    pytest.param(f"{QA_PAGE}/", None, id="trailing-slash"),
    pytest.param(f"{QA_PAGE}/lane-1", None, id="lane-folder"),
    pytest.param(f"{QA_PAGE}/index.html", None, id="page-by-name"),
    pytest.param("/qa", None, id="qa"),
    pytest.param("/qa/runs", None, id="runs"),
    pytest.param("/qa/runs/", None, id="runs-slash"),
])
def test_qa_paths_outside_a_run_s_evidence_are_not_found(qa_run: Path, get, path: str, plant) -> None:
    if plant is not None:
        plant(qa_run)
    for method in ("GET", "HEAD"):
        status, _, body = get(path, method=method)
        assert status == 404, (method, path)
        assert PRIVATE.encode() not in body


@pytest.mark.parametrize("run_id", [".", "..", "a" * 64, "a" * 65, "a/b", "run-7", "a.b_c"])
def test_served_run_ids_are_exactly_the_ids_qa_storage_accepts(site: Path, get, run_id: str) -> None:
    page = site / "qa" / run_id / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(INDEX, encoding="utf-8")
    try:
        accepted = check_id(run_id, "run_id") == run_id
    except ValueError:
        accepted = False
    status, _, _ = get(f"/qa/runs/{quote(run_id, safe='')}")
    assert status == (200 if accepted else 404)


from lib import account_requests, account_settings, machines_yml, schema, settings_apply

ACCOUNTS = [
    {"id": "claude-max", "harness": "claude", "max_jobs": 2, "paused": False, "read_only": None,
     "signed_in_on": [{"device": "box", "status": "ok"}, {"device": "pc-wsl-bots", "status": None}]},
    {"id": "codex-pro", "harness": "codex", "max_jobs": None, "paused": True, "read_only": None,
     "signed_in_on": [{"device": "box", "status": "missing"}]},
    {"id": "claude-default", "harness": "claude", "max_jobs": None, "paused": False,
     "read_only": "default", "signed_in_on": [{"device": "box", "status": "ok"}]},
    {"id": "api-key", "harness": "external", "max_jobs": 1, "paused": False,
     "read_only": "external", "signed_in_on": []},
]
CLAUDE_MAX = {"harness": "claude", "max_jobs": 2, "paused": False,
              "signed_in_on": ["pc-wsl-bots", "box"]}


@pytest.fixture
def account_client(settings_client: Client, actions_site: Path) -> Client:
    snapshot = {**SNAPSHOT, "accounts": ACCOUNTS}
    (actions_site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return settings_client


def account(client: Client, op: str, name: str, **fields):
    return client.post("/api/settings", {"target": "account", "op": op, "account": name, **fields})


ACCEPTED = [
    ("add", "claude-team", {"harness": "claude", "max_jobs": 3, "signed_in_on": ["box"]}),
    ("add", "cursor-1", {"harness": "cursor", "max_jobs": 64, "signed_in_on": []}),
    ("set", "claude-max", {"field": "max_jobs", "expect": 2, "value": 5}),
    ("set", "codex-pro", {"field": "max_jobs", "expect": None, "value": 1}),
    ("set", "claude-max", {"field": "paused", "expect": False, "value": True}),
    ("set", "codex-pro", {"field": "paused", "expect": True, "value": False}),
    ("set", "claude-max", {"field": "signed_in_on", "expect": ["box", "pc-wsl-bots"],
                           "value": ["box"]}),
    ("remove", "codex-pro", {"expect": {"harness": "codex", "max_jobs": None, "paused": True,
                                        "signed_in_on": ["box"]}}),
    ("remove", "claude-max", {"expect": CLAUDE_MAX}),
    ("prepare", "claude-max", {"device": "pc-wsl-bots"}),
    ("doctor", "codex-pro", {"device": "box"}),
    ("signout", "claude-max", {"device": "pc-wsl-bots"}),
]


@pytest.mark.parametrize("op,name,fields", ACCEPTED)
def test_the_approver_queues_exactly_the_account_request(
    account_client: Client, spool: Path, op, name, fields
) -> None:
    status, answer = account(account_client, op, name, **fields)
    assert status == 202 and set(answer) == {"id"}
    (path,) = spool.iterdir()
    assert path.name == f"{answer['id']}.json"
    request = json.loads(path.read_text(encoding="utf-8"))
    assert set(request) == account_requests.COMMON_KEYS | account_requests.OPS[op]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", request.pop("requested_at"))
    assert request == {"id": answer["id"], "actor": "maxharris1", "target": "account",
                       "op": op, "account": name, **fields}


REFUSED = [
    ("rename", "claude-max", {}, 400, "bad-op"),
    (None, "claude-max", {}, 400, "bad-op"),
    (["set"], "claude-max", {}, 400, "bad-op"),
    ("set", "claude-max", {"field": "max_jobs", "expect": 2}, 400, "bad-keys"),
    ("set", "claude-max", {"field": "max_jobs", "expect": 2, "value": 3, "device": "box"},
     400, "bad-keys"),
    ("prepare", "claude-max", {"device": "box", "id": "chosen-by-the-page0"}, 400, "bad-keys"),
    ("add", "claude-team", {"harness": "claude", "max_jobs": 3}, 400, "bad-keys"),
    ("add", "..", {"harness": "claude", "max_jobs": 3, "signed_in_on": []}, 400, "bad-value"),
    ("add", "has space", {"harness": "claude", "max_jobs": 3, "signed_in_on": []}, 400, "bad-value"),
    ("add", "x" * 65, {"harness": "claude", "max_jobs": 3, "signed_in_on": []}, 400, "bad-value"),
    ("add", "claude-team", {"harness": "unknown", "max_jobs": 3, "signed_in_on": []}, 400, "bad-value"),
    ("add", "claude-team", {"harness": "claude", "max_jobs": 0, "signed_in_on": []}, 400, "bad-value"),
    ("add", "claude-team", {"harness": "claude", "max_jobs": 65, "signed_in_on": []}, 400, "bad-value"),
    ("add", "claude-team", {"harness": "claude", "max_jobs": True, "signed_in_on": []}, 400, "bad-value"),
    ("add", "claude-team", {"harness": "claude", "max_jobs": 3, "signed_in_on": ["box", "box"]},
     400, "bad-value"),
    ("add", "claude-team", {"harness": "claude", "max_jobs": 3, "signed_in_on": "box"}, 400, "bad-value"),
    ("set", 7, {"field": "max_jobs", "expect": 2, "value": 3}, 400, "bad-value"),
    ("set", "claude-max", {"field": "harness", "expect": "claude", "value": "codex"}, 400, "bad-field"),
    ("set", "claude-max", {"field": "max_jobs", "expect": 2, "value": 65}, 400, "bad-value"),
    ("set", "claude-max", {"field": "max_jobs", "expect": 2, "value": None}, 400, "bad-value"),
    ("set", "claude-max", {"field": "max_jobs", "expect": "2", "value": 3}, 400, "bad-value"),
    ("set", "claude-max", {"field": "paused", "expect": 0, "value": True}, 400, "bad-value"),
    ("set", "claude-max", {"field": "signed_in_on", "expect": ["box"], "value": [1]}, 400, "bad-value"),
    ("remove", "claude-max", {"expect": {"harness": "claude"}}, 400, "bad-value"),
    ("remove", "claude-max", {"expect": "claude-max"}, 400, "bad-value"),
    ("prepare", "claude-max", {"device": None}, 400, "bad-value"),
    ("set", "nobody", {"field": "max_jobs", "expect": 2, "value": 3}, 404, "not-in-snapshot"),
    ("remove", "nobody", {"expect": CLAUDE_MAX}, 404, "not-in-snapshot"),
    ("doctor", "nobody", {"device": "box"}, 404, "not-in-snapshot"),
    ("add", "claude-team", {"harness": "claude", "max_jobs": 3, "signed_in_on": ["laptop"]},
     404, "not-in-snapshot"),
    ("set", "claude-max", {"field": "signed_in_on", "expect": ["box", "pc-wsl-bots"],
                           "value": ["box", "laptop"]}, 404, "not-in-snapshot"),
    ("prepare", "claude-max", {"device": "laptop"}, 404, "not-in-snapshot"),
    ("prepare", "codex-pro", {"device": "pc-wsl-bots"}, 404, "not-in-snapshot"),
    ("set", "claude-default", {"field": "max_jobs", "expect": None, "value": 3}, 403, "read-only"),
    ("set", "api-key", {"field": "paused", "expect": False, "value": True}, 403, "read-only"),
    ("remove", "claude-default", {"expect": {"harness": "claude", "max_jobs": None, "paused": False,
                                             "signed_in_on": ["box"]}}, 403, "read-only"),
    ("doctor", "claude-default", {"device": "box"}, 403, "read-only"),
    ("signout", "claude-default", {"device": "box"}, 403, "read-only"),
    ("signout", "claude-max", {"device": "laptop"}, 404, "not-in-snapshot"),
    ("signout", "claude-max", {"device": ["box"]}, 400, "bad-value"),
    ("add", "claude-default", {"harness": "claude", "max_jobs": 3, "signed_in_on": []}, 403, "read-only"),
    ("add", "codex-default", {"harness": "codex", "max_jobs": 3, "signed_in_on": []}, 403, "read-only"),
    ("add", "api-two", {"harness": "external", "max_jobs": 3, "signed_in_on": []}, 403, "read-only"),
    ("set", "claude-max", {"field": "max_jobs", "expect": 3, "value": 4}, 409, "changed"),
    ("set", "claude-max", {"field": "max_jobs", "expect": None, "value": 4}, 409, "changed"),
    ("set", "codex-pro", {"field": "paused", "expect": False, "value": True}, 409, "changed"),
    ("set", "claude-max", {"field": "signed_in_on", "expect": ["box"], "value": []}, 409, "changed"),
    ("remove", "claude-max", {"expect": {**CLAUDE_MAX, "paused": True}}, 409, "changed"),
    ("remove", "claude-max", {"expect": {**CLAUDE_MAX, "signed_in_on": ["box"]}}, 409, "changed"),
    ("add", "claude-max", {"harness": "claude", "max_jobs": 3, "signed_in_on": []}, 409, "exists"),
]


BIG = {"id": "claude-big", "harness": "claude", "max_jobs": 100, "paused": False,
       "read_only": None, "signed_in_on": [{"device": "box", "status": "ok"}]}
BIG_VIEW = {"harness": "claude", "max_jobs": 100, "paused": False, "signed_in_on": ["box"]}


@pytest.mark.parametrize("op,fields", [
    ("set", {"field": "max_jobs", "expect": 100, "value": 1}),
    ("set", {"field": "max_jobs", "expect": 100, "value": 64}),
    ("remove", {"expect": BIG_VIEW}),
])
def test_a_max_jobs_above_64_is_a_valid_expectation(
    account_client: Client, actions_site: Path, spool: Path, op, fields
) -> None:
    (actions_site / "status.json").write_text(json.dumps({**SNAPSHOT, "accounts": [BIG]}))
    status, answer = account(account_client, op, "claude-big", **fields)
    assert status == 202 and set(answer) == {"id"}
    assert [path.name for path in spool.iterdir()] == [f"{answer['id']}.json"]


def test_a_max_jobs_above_64_is_still_not_a_valid_value(
    account_client: Client, actions_site: Path, spool: Path
) -> None:
    (actions_site / "status.json").write_text(json.dumps({**SNAPSHOT, "accounts": [BIG]}))
    assert account(account_client, "set", "claude-big", field="max_jobs", expect=100,
                   value=100) == (400, {"error": "bad-value"})
    assert list(spool.iterdir()) == []


DECLARED_DEFAULT = {"id": "claude-default", "harness": "claude", "max_jobs": 2, "paused": False,
                    "read_only": None, "signed_in_on": [{"device": "box", "status": "ok"}]}
DECLARED_DEFAULT_VIEW = {"harness": "claude", "max_jobs": 2, "paused": False,
                         "signed_in_on": ["box"]}


@pytest.mark.parametrize("op,fields", [
    ("set", {"field": "max_jobs", "expect": 2, "value": 3}),
    ("set", {"field": "paused", "expect": False, "value": True}),
    ("set", {"field": "signed_in_on", "expect": ["box"], "value": []}),
    ("remove", {"expect": DECLARED_DEFAULT_VIEW}),
    ("prepare", {"device": "box"}),
    ("doctor", {"device": "box"}),
    ("signout", {"device": "box"}),
])
def test_a_declared_default_account_is_refused_and_writes_nothing(
    account_client: Client, actions_site: Path, spool: Path, op, fields
) -> None:
    snapshot = {**SNAPSHOT, "accounts": [DECLARED_DEFAULT]}
    (actions_site / "status.json").write_text(json.dumps(snapshot))
    assert account(account_client, op, "claude-default", **fields) == (403, {"error": "read-only"})
    assert list(spool.iterdir()) == []


@pytest.mark.parametrize("op,name,fields,status,code", REFUSED)
def test_a_bad_or_stale_account_request_is_refused_and_writes_nothing(
    account_client: Client, spool: Path, op, name, fields, status, code
) -> None:
    assert account(account_client, op, name, **fields) == (status, {"error": code})
    assert list(spool.iterdir()) == []


PREPARE = {"target": "account", "op": "prepare", "account": "claude-max", "device": "box"}


def test_an_account_request_needs_a_session_csrf_and_origin(
    account_client: Client, spool: Path
) -> None:
    for overrides, code in (({"X-Factory-CSRF": None}, "csrf"),
                            ({"X-Factory-CSRF": "not-the-csrf"}, "csrf"),
                            ({"Origin": "http://evil.example"}, "origin"),
                            ({"Origin": None}, "origin")):
        assert account_client.post("/api/settings", PREPARE, **overrides) == (403, {"error": code})
    account_client.cookies.pop("factory_session")
    assert account_client.post("/api/settings", PREPARE) == (401, {"error": "no-session"})
    assert list(spool.iterdir()) == []


@pytest.mark.parametrize("login", ["claude-bot", "someone-else", "maxharris1[bot]"])
def test_an_account_request_from_a_non_approver_is_refused(
    serve_actions, actions_site: Path, spool: Path, login: str
) -> None:
    (actions_site / "status.json").write_text(json.dumps({**SNAPSHOT, "accounts": ACCOUNTS}))
    client = serve_actions(spool=spool)
    _forge(client, login)
    assert client.post("/api/settings", PREPARE) == (403, {"error": "not-approver"})
    assert list(spool.iterdir()) == []


@pytest.mark.parametrize("extra", [{}, {"target": "device"}])
def test_a_device_request_spools_the_recorded_bytes(
    account_client: Client, spool: Path, monkeypatch, extra
) -> None:
    monkeypatch.setattr(server_module.secrets, "token_urlsafe", lambda n: "recordedRequestId000000")
    monkeypatch.setattr(server_module.time, "gmtime", lambda: time.struct_time(
        (2026, 11, 22, 9, 30, 0, 6, 326, 0)))
    status, answer = change(account_client, **extra)
    assert (status, answer) == (202, {"id": "recordedRequestId000000"})
    assert (spool / "recordedRequestId000000.json").read_bytes() == (
        b'{"actor": "maxharris1", "device": "box", "expect": 6, "field": "slot_max", '
        b'"id": "recordedRequestId000000", "requested_at": "2026-11-22T09:30:00Z", "value": 4}\n'
    )


def test_the_server_account_rules_match_lib() -> None:
    assert server_module.ACCOUNT_OPS == account_requests.OPS
    assert server_module.ACCOUNT_REQUEST_KEYS == account_requests.COMMON_KEYS
    assert set(server_module.ACCOUNT_FIELDS) == account_requests.SET_FIELDS
    assert server_module.REMOVE_EXPECT_KEYS == account_requests.REMOVE_EXPECT_KEYS
    assert server_module.ACCOUNT_ID.pattern == machines_yml.ACCOUNT_ID.pattern
    assert server_module.ACCOUNT_HARNESSES == machines_yml.ACCOUNT_HARNESSES
    candidates = [*range(-2, 130), True, False, None, 4.0, "4"]
    accepted = [value for value in candidates if server_module._is_slot_max(value)]
    assert accepted == [v for v in candidates if settings_apply.FIELDS["slot_max"](v)]
    assert accepted == list(range(1, settings_apply.SLOT_MAX_LIMIT + 1))
    value_ok, expect_ok = server_module.ACCOUNT_FIELDS["max_jobs"]
    lib_value_ok, lib_expect_ok, _ = account_settings.FIELDS["max_jobs"]
    assert [v for v in candidates if value_ok(v)] == [v for v in candidates if lib_value_ok(v)]
    assert [v for v in candidates if expect_ok(v)] == [v for v in candidates if lib_expect_ok(v)]
    assert server_module.DEFAULT_ACCOUNT_IDS == {
        machines_yml.default_account(harness) for harness in schema.HARNESSES}


from lib import account_signin  # noqa: E402

BUSY = [{"ticket": "12", "kind": "build", "device": "box"}]
SIGNIN_ACCOUNTS = [
    {**ACCOUNTS[0], "running": BUSY},
    {**ACCOUNTS[1], "running": []},
    {**ACCOUNTS[2], "running": []},
    {**ACCOUNTS[3], "signed_in_on": [{"device": "box", "status": None}], "running": []},
    {"id": "cursor-1", "harness": "cursor", "max_jobs": 1, "paused": False, "read_only": None,
     "signed_in_on": [{"device": "box", "status": None}], "running": []},
]
EMPTY_VIEW = {"state": "accepted", "url": None, "code": None, "expires_at": None,
              "result": None, "error": None}
CLAUDE_URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c&state=Zq8_x-1"
CODEX_URL = "https://auth.openai.com/codex/device"


@pytest.fixture
def relay(tmp_path: Path) -> Path:
    path = tmp_path / "relay"
    path.mkdir()
    path.chmod(0o770)
    return path


@pytest.fixture
def signin_site(actions_site: Path) -> Path:
    snapshot = {**SNAPSHOT, "accounts": SIGNIN_ACCOUNTS}
    (actions_site / "status.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return actions_site


@pytest.fixture
def signin_client(serve_actions, fake_github, spool: Path, relay: Path, signin_site) -> Client:
    client = serve_actions(spool=spool, relay=relay)
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    return client


def start_signin(client: Client, account: str = "claude-max", device: str = "pc-wsl-bots"):
    return client.post("/api/accounts/signin", {"account": account, "device": device})


def started(client: Client, account: str = "claude-max", device: str = "pc-wsl-bots") -> str:
    status, answer = start_signin(client, account, device)
    assert status == 202 and set(answer) == {"id"}
    return answer["id"]


def poll_signin(client: Client, rid: str, csrf: str | None = "sent") -> tuple[int, dict]:
    headers = {"X-Factory-CSRF": client.csrf if csrf == "sent" else csrf}
    status, data = client.raw("GET", f"/api/accounts/signin?id={rid}",
                              headers={k: v for k, v in headers.items() if v is not None})
    return status, json.loads(data)


def publish(relay: Path, rid: str, view: dict) -> None:
    (relay / f"{rid}.relay").write_text(json.dumps(view), encoding="utf-8")


def _drop_session(client: Client) -> dict:
    client.cookies.pop("factory_session")
    return {}


SIGNIN_REFUSED = [
    pytest.param(_drop_session, {}, "claude-max", "pc-wsl-bots", 401, "no-session",
                 id="missing-session"),
    pytest.param(None, {"X-Factory-CSRF": "not-the-csrf"}, "claude-max", "pc-wsl-bots",
                 403, "csrf", id="bad-csrf"),
    pytest.param(None, {"X-Factory-CSRF": None}, "claude-max", "pc-wsl-bots", 403, "csrf",
                 id="no-csrf"),
    pytest.param(None, {"Origin": "http://evil.example"}, "claude-max", "pc-wsl-bots",
                 403, "origin", id="bad-origin"),
    pytest.param(lambda client: _forge(client, "someone-else"), {}, "claude-max", "pc-wsl-bots",
                 403, "not-approver", id="non-approver"),
    pytest.param(lambda client: _forge(client, "claude-bot"), {}, "claude-max", "pc-wsl-bots",
                 403, "not-approver", id="bot"),
    pytest.param(None, {}, "nobody", "box", 404, "not-in-snapshot", id="undeclared-account"),
    pytest.param(None, {}, "claude-max", "laptop", 404, "not-in-snapshot",
                 id="undeclared-device"),
    pytest.param(None, {}, "codex-pro", "pc-wsl-bots", 404, "not-in-snapshot",
                 id="device-of-another-account"),
    pytest.param(None, {}, "claude-default", "box", 403, "read-only", id="default-account"),
    pytest.param(None, {}, "api-key", "box", 403, "read-only", id="external-account"),
    pytest.param(None, {}, "cursor-1", "box", 403, "no-signin", id="cursor-account"),
    pytest.param(None, {}, "claude-max", "box", 409, "running-job", id="running-job"),
    pytest.param(None, {}, "has space", "box", 400, "bad-value", id="bad-account"),
    pytest.param(None, {}, "claude-max", ["box"], 400, "bad-value", id="bad-device"),
]


@pytest.mark.parametrize("prepare,overrides,name,device,status,code", SIGNIN_REFUSED)
def test_account_signin_refusals(
    signin_client: Client, spool: Path, relay: Path, prepare, overrides, name, device, status, code
) -> None:
    if prepare is not None:
        prepare(signin_client)
    answer = signin_client.post("/api/accounts/signin", {"account": name, "device": device},
                                **overrides)
    assert answer == (status, {"error": code})
    assert list(spool.iterdir()) == [] and list(relay.iterdir()) == []


@pytest.mark.parametrize("payload", [
    {"account": "claude-max"},
    {"account": "claude-max", "device": "pc-wsl-bots", "id": "chosen-by-the-page0"},
])
def test_account_signin_needs_exactly_account_and_device(
    signin_client: Client, spool: Path, relay: Path, payload
) -> None:
    assert signin_client.post("/api/accounts/signin", payload) == (400, {"error": "bad-keys"})
    assert list(spool.iterdir()) == [] and list(relay.iterdir()) == []


def test_account_signin_spools_exact_bytes_for_the_idle_device_of_a_busy_account(
    signin_client: Client, spool: Path, relay: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server_module.secrets, "token_urlsafe", lambda n: "recordedSigninId0000000")
    monkeypatch.setattr(server_module.time, "gmtime", lambda: time.struct_time(
        (2026, 11, 22, 9, 30, 0, 6, 326, 0)))
    assert start_signin(signin_client) == (202, {"id": "recordedSigninId0000000"})
    body = (spool / "recordedSigninId0000000.json").read_bytes()
    assert body == (
        b'{"account": "claude-max", "actor": "maxharris1", "device": "pc-wsl-bots", '
        b'"id": "recordedSigninId0000000", "op": "signin", "requested_at": "2026-11-22T09:30:00Z", '
        b'"target": "account"}\n'
    )
    assert settings_apply.is_broker_request(body)
    request = settings_apply.parse_request(body, "recordedSigninId0000000")
    assert (request.op, request.account, request.device) == ("signin", "claude-max", "pc-wsl-bots")
    assert list(relay.iterdir()) == []


def test_without_a_relay_account_signin_is_an_unknown_route(settings_client: Client) -> None:
    assert settings_client.session()["account_signin"] is False
    assert start_signin(settings_client) == (404, {"error": "not-found"})
    assert poll_signin(settings_client, "a" * 22) == (404, {"error": "not-found"})
    assert settings_client.post("/api/accounts/signin/code", {"id": "a" * 22, "code": "x" * 9}) == (
        404, {"error": "not-found"})


def test_the_session_says_whether_account_signin_is_on(signin_client: Client) -> None:
    assert signin_client.session()["account_signin"] is True
    assert signin_client.post("/api/signout") == (200, {})
    assert signin_client.session() == {"signed_in": False, "account_signin": True}


def test_account_signin_poll_before_the_broker_answers_is_accepted(signin_client: Client) -> None:
    assert poll_signin(signin_client, started(signin_client)) == (200, EMPTY_VIEW)


def test_account_signin_poll_answers_only_the_starting_session(signin_client: Client) -> None:
    rid = started(signin_client)
    assert poll_signin(signin_client, rid, csrf=None) == (403, {"error": "csrf"})
    assert poll_signin(signin_client, rid, csrf="not-the-csrf") == (403, {"error": "csrf"})
    assert poll_signin(signin_client, "unknownSigninId0000000") == (404, {"error": "no-attempt"})
    _forge(signin_client, "maxharris1")
    assert poll_signin(signin_client, rid) == (404, {"error": "no-attempt"})
    signin_client.cookies.pop("factory_session")
    assert poll_signin(signin_client, rid) == (401, {"error": "no-session"})


@pytest.mark.parametrize("url,shown", [
    (CLAUDE_URL, CLAUDE_URL),
    ("https://claude.com/cai/oauth/authorize", "https://claude.com/cai/oauth/authorize"),
    ("https://claude.com.evil.example/cai/oauth/authorize", None),
    ("https://evil.example/x", None),
    ("https://claude.com/cai/oauth/authorize?next=<script>", None),
    ("https://claude.com/cai/oauth/authorize\nhttps://evil.example", None),
    (CODEX_URL, None),
    (["https://claude.com/cai/oauth/authorize"], None),
])
def test_account_signin_poll_shows_only_the_harness_url(
    signin_client: Client, relay: Path, url, shown
) -> None:
    rid = started(signin_client)
    publish(relay, rid, {"state": "awaiting-url", "url": url, "code": "ABCD-1234"})
    assert poll_signin(signin_client, rid) == (200, {**EMPTY_VIEW, "state": "awaiting-url",
                                                     "url": shown})


def test_account_signin_poll_shows_a_codex_url_and_code_and_drops_other_keys(
    signin_client: Client, relay: Path
) -> None:
    rid = started(signin_client, "codex-pro", "box")
    publish(relay, rid, {"state": "awaiting-approval", "url": CODEX_URL, "code": "ABCD-12345",
                         "expires_at": 1795000000, "output": "raw terminal LEAKCANARY",
                         "home": "/home/codex-bot"})
    assert poll_signin(signin_client, rid) == (200, {**EMPTY_VIEW, "state": "awaiting-approval",
                                                     "url": CODEX_URL, "code": "ABCD-12345",
                                                     "expires_at": 1795000000})


@pytest.mark.parametrize("view,shown", [
    ({"code": "abcd-1234; rm -rf"}, {}),
    ({"code": "ABCD-1234", "state": "pwned"}, {"code": "ABCD-1234"}),
    ({"state": ["verifying"]}, {}),
    ({"expires_at": True}, {}),
    ({"expires_at": "1795000000"}, {}),
    ({"expires_at": 1795000000.5}, {"expires_at": 1795000000.5}),
    ({"result": "ok", "error": None}, {"result": "ok"}),
    ({"result": "mismatch", "error": "shared-identity"},
     {"result": "mismatch", "error": "shared-identity"}),
    ({"result": "won", "error": "<b>" + "x" * 10}, {}),
    ({"result": "failed", "error": "x" * 81}, {"result": "failed"}),
])
def test_account_signin_poll_parses_every_field(
    signin_client: Client, relay: Path, view, shown
) -> None:
    rid = started(signin_client, "codex-pro", "box")
    publish(relay, rid, view)
    assert poll_signin(signin_client, rid) == (200, {**EMPTY_VIEW, **shown})


@pytest.mark.parametrize("planted", [b"not json", b"[1, 2]", b"{" + b" " * 5000 + b"}"])
def test_account_signin_poll_ignores_an_unreadable_view(
    signin_client: Client, relay: Path, planted: bytes
) -> None:
    rid = started(signin_client)
    (relay / f"{rid}.relay").write_bytes(planted)
    assert poll_signin(signin_client, rid) == (200, EMPTY_VIEW)


def test_account_signin_poll_never_follows_a_linked_view(
    signin_client: Client, relay: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"state": "verifying", "url": CLAUDE_URL}), encoding="utf-8")
    rid = started(signin_client)
    (relay / f"{rid}.relay").symlink_to(outside)
    assert poll_signin(signin_client, rid) == (200, EMPTY_VIEW)
    (relay / f"{rid}.relay").unlink()
    os.link(outside, relay / f"{rid}.relay")
    assert poll_signin(signin_client, rid) == (200, EMPTY_VIEW)


def test_a_claude_attempt_never_shows_a_code(signin_client: Client, relay: Path) -> None:
    rid = started(signin_client)
    publish(relay, rid, {"state": "awaiting-code", "url": CLAUDE_URL, "code": "ABCD-1234"})
    assert poll_signin(signin_client, rid) == (200, {**EMPTY_VIEW, "state": "awaiting-code",
                                                     "url": CLAUDE_URL})


def test_only_the_newest_attempts_are_kept(signin_client: Client) -> None:
    ids = [started(signin_client) for _ in range(server_module.MAX_SIGNINS + 1)]
    assert poll_signin(signin_client, ids[0]) == (404, {"error": "no-attempt"})
    assert all(poll_signin(signin_client, rid)[0] == 200 for rid in ids[1:])


def send_code(client: Client, rid: str, code: object = "pasted.Code_1~#-abc"):
    return client.post("/api/accounts/signin/code", {"id": rid, "code": code})


def test_account_signin_code_is_written_once(signin_client: Client, relay: Path) -> None:
    rid = started(signin_client)
    assert send_code(signin_client, rid, "  pasted.Code_1~#-abc\n") == (202, {})
    path = relay / f"{rid}.code"
    assert path.read_bytes() == b"pasted.Code_1~#-abc"
    assert path.stat().st_mode & 0o7777 == 0o640
    assert send_code(signin_client, rid, "anotherCode0000") == (409, {"error": "code-used"})
    assert path.read_bytes() == b"pasted.Code_1~#-abc"
    path.unlink()
    assert send_code(signin_client, rid, "anotherCode0000") == (409, {"error": "code-used"})
    assert [p.name for p in relay.iterdir()] == []


def test_account_signin_code_from_another_session_writes_nothing(
    signin_client: Client, relay: Path
) -> None:
    rid = started(signin_client)
    _forge(signin_client, "maxharris1")
    assert send_code(signin_client, rid) == (404, {"error": "no-attempt"})
    assert send_code(signin_client, "unknownSigninId0000000") == (404, {"error": "no-attempt"})
    assert list(relay.iterdir()) == []


def test_a_codex_attempt_takes_no_code(signin_client: Client, relay: Path) -> None:
    rid = started(signin_client, "codex-pro", "box")
    assert send_code(signin_client, rid) == (409, {"error": "no-code"})
    assert list(relay.iterdir()) == []


@pytest.mark.parametrize("code", ["short", "has a space inside", "x" * 513, "semi;colon00",
                                  "", 12345678, None])
def test_a_bad_code_is_refused_and_does_not_use_the_attempt(
    signin_client: Client, relay: Path, code
) -> None:
    rid = started(signin_client)
    assert send_code(signin_client, rid, code) == (400, {"error": "bad-code"})
    assert list(relay.iterdir()) == []
    assert send_code(signin_client, rid) == (202, {})


def test_a_code_post_needs_exactly_id_and_code(signin_client: Client, relay: Path) -> None:
    rid = started(signin_client)
    assert signin_client.post("/api/accounts/signin/code", {"id": rid}) == (
        400, {"error": "bad-keys"})
    assert signin_client.post("/api/accounts/signin/code",
                              {"id": rid, "code": "pasted.Code_1", "device": "box"}) == (
        400, {"error": "bad-keys"})
    assert list(relay.iterdir()) == []


def test_account_signin_cancel_writes_the_marker(signin_client: Client, relay: Path) -> None:
    rid = started(signin_client)
    cancel = {"id": rid}
    assert signin_client.post("/api/accounts/signin/cancel", cancel) == (202, {})
    path = relay / f"{rid}.cancel"
    assert path.read_bytes() == b""
    assert path.stat().st_mode & 0o7777 == 0o640
    assert signin_client.post("/api/accounts/signin/cancel", cancel) == (202, {})
    assert [p.name for p in relay.iterdir()] == [f"{rid}.cancel"]


def test_account_signin_cancel_from_another_session_writes_nothing(
    signin_client: Client, relay: Path
) -> None:
    rid = started(signin_client)
    _forge(signin_client, "maxharris1")
    assert signin_client.post("/api/accounts/signin/cancel", {"id": rid}) == (
        404, {"error": "no-attempt"})
    assert list(relay.iterdir()) == []


def _plant(relay: Path, name: str, age: float, now: float) -> None:
    (relay / name).write_bytes(b"planted")
    os.utime(relay / name, (now - age, now - age))


def test_the_relay_sweep_removes_codes_after_120_seconds(signin_client: Client, relay: Path) -> None:
    now = time.time()
    rid = "plantedSigninId0000000"
    _plant(relay, f"{rid}.code", 121, now)
    _plant(relay, f"young{rid}.code", 119, now)
    _plant(relay, f"{rid}.cancel", 121, now)
    _plant(relay, f"old{rid}.cancel", 901, now)
    _plant(relay, f"{rid}.relay", 5000, now)
    _plant(relay, f".{rid}.code.0a1b.tmp", 5000, now)
    _plant(relay, "short.code", 5000, now)
    removed = signin_client.server.relay.sweep(now)
    assert sorted(removed) == [f"old{rid}.cancel", f"{rid}.code"]
    assert sorted(p.name for p in relay.iterdir()) == sorted([
        f"young{rid}.code", f"{rid}.cancel", f"{rid}.relay", f".{rid}.code.0a1b.tmp",
        "short.code"])


def test_the_sweep_thread_removes_an_unread_code(
    serve_actions, spool: Path, relay: Path, signin_site, monkeypatch
) -> None:
    monkeypatch.setattr(server_module, "SWEEP_SECONDS", 0.05)
    _plant(relay, "plantedSigninId0000000.code", 121, time.time())
    serve_actions(spool=spool, relay=relay)
    deadline = time.monotonic() + 2
    while list(relay.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert list(relay.iterdir()) == []


def test_the_server_signin_patterns_match_lib() -> None:
    assert {harness: pattern.pattern for harness, pattern in server_module.SIGNIN_URLS.items()} == {
        harness: allow.url.pattern for harness, allow in account_signin._ALLOW.items()}
    assert server_module.SIGNIN_CODE.pattern == account_signin._ALLOW["codex"].code.pattern
    assert account_signin._ALLOW["claude"].code is None


def test_load_config_takes_the_signin_relay_with_the_spool(
    site: Path, spool: Path, relay: Path
) -> None:
    env = _env(site, **ACTIONS_ENV, USER="factory-dash", DASHBOARD_SETTINGS_SPOOL=str(spool),
               DASHBOARD_SIGNIN_RELAY=str(relay))
    *_, actions = server_module.load_config(env, process_user="factory-dash")
    assert actions.signin_relay == relay


@pytest.mark.parametrize("case,needle", [
    ("relative", "absolute"),
    ("missing", "does not exist"),
    ("file", "not a directory"),
    ("symlink", "symlink"),
    ("world-readable", "world"),
    ("world-writable", "world"),
    ("without-spool", "DASHBOARD_SIGNIN_RELAY needs DASHBOARD_SETTINGS_SPOOL"),
    ("without-actions", "needs GitHub actions"),
])
def test_load_config_refuses_an_unsafe_signin_relay(
    site: Path, spool: Path, relay: Path, case: str, needle: str
) -> None:
    (relay.parent / "plain-file").write_text("x", encoding="utf-8")
    (relay.parent / "relay-link").symlink_to(relay, target_is_directory=True)
    if case.startswith("world-"):
        relay.chmod(0o774 if case == "world-readable" else 0o772)
    value = {
        "relative": "relay",
        "missing": str(relay.parent / "nope"),
        "file": str(relay.parent / "plain-file"),
        "symlink": str(relay.parent / "relay-link"),
    }.get(case, str(relay))
    env = _env(site, **({} if case == "without-actions" else ACTIONS_ENV), USER="factory-dash",
               DASHBOARD_SIGNIN_RELAY=value)
    if case not in ("without-spool", "without-actions"):
        env["DASHBOARD_SETTINGS_SPOOL"] = str(spool)
    with pytest.raises(server_module.ConfigError, match=needle):
        server_module.load_config(env, process_user="factory-dash")
