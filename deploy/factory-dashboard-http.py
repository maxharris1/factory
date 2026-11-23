#!/usr/bin/env python3
"""Read-only static server for the pushed factory dashboard (pc-wsl-bots).

An example for the pc-wsl-bots WSL node, run by
deploy/factory-dashboard-http.service. Nothing in this repository installs
or starts it. Standard library only, so it runs from a single copied file.

It replaces `python3 -m http.server`, which serves any path under its
directory, follows symlinks out of it, lists directories, and serves the
working directory (for a user unit, the home directory) when --directory is
empty. This server:

- binds only TAILNET_IP, which must be an IPv4 address in Tailscale's
  100.64.0.0/10 range (never a wildcard, loopback or LAN address);
- serves DASHBOARD_DIR, which must be set, absolute, an existing directory,
  and neither / nor $HOME nor any directory that contains $HOME (it is
  resolved through symlinks before those checks);
- answers GET and HEAD for the snapshot, and only for /, /status.json, the
  pages (/index.html, /activity.html, /efficiency.html, /job.html,
  /health.html, /ticket.html, /settings.html) and /tickets.json (the ticket
  records), the files `orch dashboard push` writes. Every other path, including
  dot-files such as the push's temp files and anything with `..` or an
  encoded slash, is 404, and no directory is ever listed;
- also answers GET and HEAD for a QA run's evidence (qa/<run_id>/ under the
  directory, as qa_evidence.collect() lays out lanes): /qa/runs/<run_id>
  serves that run's index.html, and /qa/runs/<run_id>/lane-<n>/<path>
  serves one file of lane n. The raw path is split on / before each part is
  decoded, and a part that is empty, starts with a dot, or decodes to hold
  /, a backslash or NUL is 404, as is a run id outside [A-Za-z0-9._-]{1,64}.
  Only .png .jpg .jpeg .webp .webm .mp4 .json get their own type; any other
  file is text/plain with Content-Disposition: attachment, so an uploaded
  page never renders. Evidence is streamed whole, past the snapshot size cap, and
  those 200s alone add img-src and media-src 'self' to the policy;
- opens the directory once at startup (O_NOFOLLOW) and keeps that
  descriptor, so swapping the directory or an ancestor for a symlink later
  changes nothing;
- opens each file relative to that descriptor with O_NOFOLLOW and serves it
  only if it is a regular file with one link, so neither a symlink nor a
  hard link to a file elsewhere is served.

Settings come from the environment (the unit's EnvironmentFile):
TAILNET_IP, DASHBOARD_DIR, and DASHBOARD_PORT (default 8787, 1024-65535).
`--check` validates them, prints one line and exits.

GitHub actions (comments and the hold OK as the approver) are off unless
DASHBOARD_GITHUB_CLIENT_ID is set. Then DASHBOARD_APPROVER (a GitHub login)
and DASHBOARD_ACTION_CLIENTS (a comma list of tailnet IPv4 addresses, never
this node's own) are required, the process must not run as a bot account,
and POST is answered under /api only, to those clients only. Sign-in is
GitHub's OAuth device flow; the token stays in this process's memory and is
never sent to a browser, written to disk or logged. Comments and OKs go
only to tickets in the served status.json. With actions off, /api/* is 404
and POST is refused exactly as before.

Device settings (enabled, slot_max, drain) are on only with actions on and
DASHBOARD_SETTINGS_SPOOL set: an absolute, existing directory that is not a
symlink and not group- or world-writable, opened once at startup like
DASHBOARD_DIR. POST /api/settings, from the signed-in approver, checks the
change against the device row in the served status.json and only writes a
request file <id>.json into that directory. The orch host applies it
(`orch settings apply`); this server never touches machines.yml or runs
anything. Setting the spool without actions is a configuration error, and
without it /api/settings is 404.

Max's actions on a ticket that waits on him (merge OK, approve or split an
oversize plan, another round, answer, requeue, resume, close) are on only
with actions on and DASHBOARD_ACTIONS_AUDIT set: an absolute path to an
append-only JSONL file (if it exists, a regular file and not a symlink;
neither it nor its directory group- or world-writable), opened once at
startup. POST /api/act, from the signed-in approver, checks the ticket row
in the served status.json still has the state and wait reason the page
showed and still lists the action, builds the comment on the server, and
posts it as Max on the row's PR, else its issue. A merge OK re-reads the PR
head first and posts nothing if it moved. Every request that reaches the
route with a valid session and CSRF, posted or refused, appends one audit
line (time, login, ticket, action, expected state, head, result, url; never
the words, token, cookie or CSRF). A request refused before that (no
session, bad CSRF, wrong content type or origin) writes none. Setting the
audit file without actions is a configuration error, and without it
/api/act is 404.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

TAILNET = ipaddress.ip_network("100.64.0.0/10")
DEFAULT_PORT = 8787
MAX_BYTES = 8 * 1024 * 1024
PAGES = (
    "index.html", "activity.html", "efficiency.html", "job.html", "health.html", "ticket.html",
    "settings.html",
)
ROUTES = {
    "/": "index.html",
    "/status.json": "status.json",
    "/tickets.json": "tickets.json",
    **{f"/{name}": name for name in PAGES},
}
CONTENT_TYPES = {
    "status.json": "application/json",
    "tickets.json": "application/json",
    **{name: "text/html; charset=utf-8" for name in PAGES},
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    # The snapshot pages are self-contained: inline style and script only.
    # Their script fetches this server's own status.json (connect-src 'self') to
    # update in place; nothing else is ever loaded.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}
QA_RUNS = "/qa/runs/"
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
LANE = re.compile(r"lane-[1-9][0-9]*")
QA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".webm": "video/webm", ".mp4": "video/mp4", ".json": "application/json",
}
QA_HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": SECURITY_HEADERS["Content-Security-Policy"] + "; img-src 'self'; media-src 'self'",
}
QA_DOWNLOAD = ("text/plain; charset=utf-8", {**QA_HEADERS, "Content-Disposition": "attachment"})
QA_CHUNK = 1024 * 1024


class ConfigError(ValueError):
    pass


def check_address(value: str | None) -> str:
    text = value or ""
    try:
        ok = (
            re.fullmatch(r"[0-9.]{7,15}", text) is not None
            and ipaddress.IPv4Address(text) in TAILNET
        )
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError(
            "TAILNET_IP must be this node's tailnet IPv4 address (100.64.0.0/10)"
        )
    return text


def check_root(value: str | None, home: str | None) -> Path:
    if not value:
        raise ConfigError("DASHBOARD_DIR must be set to the pushed snapshot directory")
    if not os.path.isabs(value):
        raise ConfigError("DASHBOARD_DIR must be an absolute path")
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ConfigError(f"DASHBOARD_DIR does not exist: {value}") from None
    if not root.is_dir():
        raise ConfigError(f"DASHBOARD_DIR is not a directory: {value}")
    if root == Path(root.anchor):
        raise ConfigError("DASHBOARD_DIR must not be /")
    if home:
        home_path = Path(home).resolve()
        if root == home_path or root in home_path.parents:
            raise ConfigError("DASHBOARD_DIR must not be $HOME or a directory above it")
    return root


def check_port(value: str | None) -> int:
    if value is None:
        return DEFAULT_PORT
    if re.fullmatch(r"[0-9]{1,5}", value) is None or not 1024 <= int(value) <= 65535:
        raise ConfigError("DASHBOARD_PORT must be a number from 1024 to 65535")
    return int(value)


GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
BOT_USER = re.compile(r".*-bot|grok|box")


class ActionsConfig(NamedTuple):
    client_id: str
    approver: str
    clients: frozenset[ipaddress.IPv4Address]
    api_base: str = GITHUB_API
    web_base: str = GITHUB_WEB
    settings_spool: Path | None = None
    audit: Path | None = None


def check_spool(value: str) -> Path:
    if not os.path.isabs(value):
        raise ConfigError("DASHBOARD_SETTINGS_SPOOL must be an absolute path")
    try:
        info = os.lstat(value)
    except OSError:
        raise ConfigError(f"DASHBOARD_SETTINGS_SPOOL does not exist: {value}") from None
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError("DASHBOARD_SETTINGS_SPOOL must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"DASHBOARD_SETTINGS_SPOOL is not a directory: {value}")
    if info.st_mode & 0o022:
        raise ConfigError("DASHBOARD_SETTINGS_SPOOL must not be group- or world-writable")
    return Path(value)


def check_audit(value: str) -> Path:
    if not os.path.isabs(value):
        raise ConfigError("DASHBOARD_ACTIONS_AUDIT must be an absolute path")
    path = Path(value)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError:
        raise ConfigError(f"DASHBOARD_ACTIONS_AUDIT cannot be read: {value}") from None
    if info is not None:
        if stat.S_ISLNK(info.st_mode):
            raise ConfigError("DASHBOARD_ACTIONS_AUDIT must not be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"DASHBOARD_ACTIONS_AUDIT is not a regular file: {value}")
        if info.st_mode & 0o022:
            raise ConfigError("DASHBOARD_ACTIONS_AUDIT must not be group- or world-writable")
    try:
        parent = os.stat(path.parent)
    except OSError:
        raise ConfigError(f"DASHBOARD_ACTIONS_AUDIT's directory does not exist: {value}") from None
    if parent.st_mode & 0o022:
        raise ConfigError(
            "DASHBOARD_ACTIONS_AUDIT's directory must not be group- or world-writable"
        )
    return path


def check_actions(
    environ: dict[str, str], own_address: str, process_user: str
) -> ActionsConfig | None:
    client_id = environ.get("DASHBOARD_GITHUB_CLIENT_ID", "")
    spool = environ.get("DASHBOARD_SETTINGS_SPOOL", "")
    audit = environ.get("DASHBOARD_ACTIONS_AUDIT", "")
    if not client_id:
        for name, value in (("DASHBOARD_SETTINGS_SPOOL", spool),
                            ("DASHBOARD_ACTIONS_AUDIT", audit)):
            if value:
                raise ConfigError(f"{name} needs GitHub actions (DASHBOARD_GITHUB_CLIENT_ID)")
        return None
    if CLIENT_ID.fullmatch(client_id) is None:
        raise ConfigError("DASHBOARD_GITHUB_CLIENT_ID is not an OAuth App client id")
    for user in (environ.get("USER", ""), process_user):
        if BOT_USER.fullmatch(user):
            raise ConfigError(
                f"GitHub actions must not run as the bot account {user!r}; "
                "run this server as its own user (README \"Dashboard\")"
            )
    approver = environ.get("DASHBOARD_APPROVER", "")
    if LOGIN.fullmatch(approver) is None:
        raise ConfigError("DASHBOARD_APPROVER must be the approver's GitHub login")
    entries = [item.strip() for item in environ.get("DASHBOARD_ACTION_CLIENTS", "").split(",")]
    if entries == [""]:
        raise ConfigError("DASHBOARD_ACTION_CLIENTS must list the approver's tailnet IPv4 addresses")
    clients = set()
    for entry in entries:
        try:
            check_address(entry)
        except ConfigError:
            raise ConfigError(
                f"DASHBOARD_ACTION_CLIENTS entry {entry!r} is not a tailnet IPv4 address"
            ) from None
        if entry == own_address:
            raise ConfigError(
                "DASHBOARD_ACTION_CLIENTS must not hold this node's own TAILNET_IP"
            )
        clients.add(ipaddress.IPv4Address(entry))
    return ActionsConfig(client_id, approver, frozenset(clients),
                         settings_spool=check_spool(spool) if spool else None,
                         audit=check_audit(audit) if audit else None)


def current_user() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def load_config(
    environ: dict[str, str], process_user: str | None = None
) -> tuple[str, int, Path, ActionsConfig | None]:
    host = check_address(environ.get("TAILNET_IP"))
    return (
        host,
        check_port(environ.get("DASHBOARD_PORT")),
        check_root(environ.get("DASHBOARD_DIR"), environ.get("HOME")),
        check_actions(environ, host, current_user() if process_user is None else process_user),
    )


NOFOLLOW = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def open_root(root: Path) -> int:
    """Open the served directory once, without following a symlink at its
    last component, and return the descriptor. Files are then opened
    relative to it, so replacing the directory or any ancestor of it with a
    symlink after startup cannot change what is served."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        if not os.path.samestat(os.fstat(fd), os.stat(root, follow_symlinks=False)):
            raise OSError(f"{root} changed while it was opened")
    except BaseException:
        os.close(fd)
        raise
    return fd


class QaRoute(NamedTuple):
    parts: tuple[str, ...]
    content_type: str
    headers: dict[str, str]


def qa_route(path: str) -> QaRoute | None:
    parts = [urllib.parse.unquote(part) for part in path[len(QA_RUNS):].split("/")]
    if any(not part or part.startswith(".") or any(c in part for c in "/\\\0") for part in parts):
        return None
    run_id, *rest = parts
    if RUN_ID.fullmatch(run_id) is None:
        return None
    if not rest:
        return QaRoute(("qa", run_id, "index.html"), "text/html; charset=utf-8", QA_HEADERS)
    if len(rest) < 2 or LANE.fullmatch(rest[0]) is None:
        return None
    suffix = os.path.splitext(rest[-1])[1].lower()
    content_type, headers = (QA_TYPES[suffix], QA_HEADERS) if suffix in QA_TYPES else QA_DOWNLOAD
    return QaRoute(("qa", run_id, *rest), content_type, headers)


def open_under(root_fd: int, parts: tuple[str, ...]) -> int | None:
    fd = root_fd
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            if fd != root_fd:
                os.close(fd)
            fd = child
        # O_NONBLOCK so a FIFO planted under the name cannot hang the open.
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=fd)
    except OSError:
        return None
    finally:
        if fd != root_fd:
            os.close(fd)
    info = os.fstat(leaf)
    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        return leaf
    os.close(leaf)
    return None


def read_snapshot_file(root_fd: int, name: str) -> bytes | None:
    fd = open_under(root_fd, (name,))
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > MAX_BYTES:
            return None
        return handle.read(MAX_BYTES + 1)[:MAX_BYTES]


SESSION_SECONDS = 12 * 3600
MAX_FLOWS = 4
MAX_COMMENT = 65536
MAX_REQUEST = 512 * 1024
GITHUB_TIMEOUT = 10
REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")
REFUSED_FIRST_LINE = re.compile(r"\s*(?:<!--\s*orch:v1\b|`orch:v1\b|/ok\b)")
OAUTH_APP_TOKEN = re.compile(r"gho_[A-Za-z0-9]+")
MAX_PENDING = 20
REQUEST_FILE = re.compile(r"([A-Za-z0-9_-]{16,64})\.json")
DEVICE_ERRORS = {
    "authorization_pending": "pending",
    "slow_down": "pending",
    "expired_token": "expired",
    "access_denied": "refused",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, **extra: str):
        super().__init__(code)
        self.status = status
        self.body = {"error": code, **extra}


class GitHubError(Exception):
    def __init__(self, status: int):
        super().__init__(f"github answered {status}")
        self.status = status


class Target(NamedTuple):
    repo: str
    number: int
    kind: str  # "issue" or "pull"


class Flow:
    def __init__(self, device_code: str, interval: float, expires_at: float):
        self.device_code = device_code
        self.interval = interval
        self.expires_at = expires_at
        self.next_poll = 0.0


class Session(NamedTuple):
    session_id: str
    login: str
    token: str
    csrf: str
    expires_at: float


class GitHubClient:
    """The few GitHub calls the actions need. Errors carry the HTTP status
    only, never a body, so nothing GitHub echoes back reaches a log."""

    def __init__(self, config: ActionsConfig):
        self.config = config

    def _call(self, method: str, url: str, *, token: str | None = None,
              form: dict[str, str] | None = None, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "factory-dashboard"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT) as response:
                body = json.loads(response.read(MAX_BYTES))
        except urllib.error.HTTPError as exc:
            raise GitHubError(exc.code) from None
        except (OSError, ValueError):
            raise GitHubError(0) from None
        if not isinstance(body, dict):
            raise GitHubError(0)
        return body

    def device_code(self) -> dict:
        return self._call("POST", f"{self.config.web_base}/login/device/code",
                          form={"client_id": self.config.client_id, "scope": "repo"})

    def poll_token(self, device_code: str) -> dict:
        return self._call("POST", f"{self.config.web_base}/login/oauth/access_token", form={
            "client_id": self.config.client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })

    def user(self, token: str) -> dict:
        return self._call("GET", f"{self.config.api_base}/user", token=token)

    def pull_head(self, token: str, repo: str, number: int) -> str:
        pull = self._call("GET", f"{self.config.api_base}/repos/{repo}/pulls/{number}", token=token)
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
            raise GitHubError(0)
        return sha

    def post_comment(self, token: str, repo: str, number: int, body: str) -> dict:
        return self._call("POST", f"{self.config.api_base}/repos/{repo}/issues/{number}/comments",
                          token=token, payload={"body": body})


class Actions:
    """Sign-in flows and the one signed-in session, shared by the server's
    request threads."""

    def __init__(self, config: ActionsConfig):
        self.config = config
        self.github = GitHubClient(config)
        self.lock = threading.Lock()
        self.flows: dict[str, Flow] = {}
        self.session: Session | None = None

    def allows(self, peer: str) -> bool:
        try:
            return ipaddress.ip_address(peer) in self.config.clients
        except ValueError:
            return False

    def current(self, session_id: str | None) -> Session | None:
        with self.lock:
            session = self.session
            if session is None or session.expires_at <= time.monotonic():
                self.session = None
                return None
            if session_id is None or not secrets.compare_digest(session_id, session.session_id):
                return None
            return session

    def start_flow(self) -> tuple[str, dict]:
        try:
            grant = self.github.device_code()
        except GitHubError:
            raise ApiError(502, "github") from None
        fields = {key: grant.get(key) for key in ("device_code", "user_code", "verification_uri")}
        interval, expires_in = grant.get("interval", 5), grant.get("expires_in", 900)
        if not all(isinstance(value, str) and value for value in fields.values()) or not (
            isinstance(interval, int) and isinstance(expires_in, int)
        ):
            raise ApiError(502, "github")
        flow_id = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self.lock:
            self.flows = {key: flow for key, flow in self.flows.items() if flow.expires_at > now}
            while len(self.flows) >= MAX_FLOWS:
                self.flows.pop(next(iter(self.flows)))
            self.flows[flow_id] = Flow(str(fields["device_code"]), interval, now + expires_in)
        return flow_id, {
            "user_code": fields["user_code"],
            "verification_uri": fields["verification_uri"],
            "interval": interval,
            "expires_in": expires_in,
        }

    def poll_flow(self, flow_id: str | None) -> tuple[str, Session | None]:
        now = time.monotonic()
        with self.lock:
            flow = self.flows.get(flow_id or "")
            if flow is None:
                raise ApiError(401, "no-flow")
            if flow.expires_at <= now:
                del self.flows[flow_id]
                return "expired", None
            if now < flow.next_poll:
                return "pending", None
            flow.next_poll = now + flow.interval
        try:
            answer = self.github.poll_token(flow.device_code)
        except GitHubError:
            raise ApiError(502, "github") from None
        token = answer.get("access_token")
        if isinstance(token, str) and token:
            state, session = self._sign_in(token)
        else:
            state, session = DEVICE_ERRORS.get(str(answer.get("error")), "refused"), None
            if answer.get("error") == "slow_down":
                with self.lock:
                    flow.interval += 5
                    flow.next_poll = time.monotonic() + flow.interval
        if state != "pending":
            with self.lock:
                self.flows.pop(flow_id or "", None)
        return state, session

    def _sign_in(self, token: str) -> tuple[str, Session | None]:
        if OAUTH_APP_TOKEN.fullmatch(token) is None:
            return "refused", None
        try:
            user = self.github.user(token)
        except GitHubError:
            return "refused", None
        login = user.get("login")
        if (
            user.get("type") != "User"
            or not isinstance(login, str)
            or login.endswith("[bot]")
            or login.casefold() != self.config.approver.casefold()
        ):
            return "refused", None
        session = Session(secrets.token_urlsafe(32), login, token, secrets.token_urlsafe(32),
                          time.monotonic() + SESSION_SECONDS)
        with self.lock:
            self.session = session
        return "signed-in", session

    def sign_out(self) -> None:
        with self.lock:
            self.session = None

    def post_as_approver(self, session: Session, target: Target, body: str) -> str:
        try:
            comment = self.github.post_comment(session.token, target.repo, target.number, body)
        except GitHubError:
            raise ApiError(502, "github") from None
        author = comment.get("user")
        login = author.get("login") if isinstance(author, dict) else None
        url = comment.get("html_url")
        if (
            not isinstance(login, str)
            or login.casefold() != self.config.approver.casefold()
            or comment.get("performed_via_github_app") is not None
        ):
            self.sign_out()
            raise ApiError(502, "wrong-author")
        if not isinstance(url, str):
            raise ApiError(502, "github")
        return url

    def head(self, session: Session, target: Target) -> str:
        try:
            return self.github.pull_head(session.token, target.repo, target.number)
        except GitHubError:
            raise ApiError(502, "github") from None


def _number(value: object) -> int:
    if isinstance(value, bool):
        raise ApiError(400, "bad-number")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        return int(value)
    raise ApiError(400, "bad-number")


def parse_target(payload: dict, kind: str, key: str) -> Target:
    repo = payload.get("repo")
    if not isinstance(repo, str) or REPO.fullmatch(repo) is None:
        raise ApiError(400, "bad-repo")
    if kind not in ("issue", "pull"):
        raise ApiError(400, "bad-kind")
    return Target(repo, _number(payload.get(key)), kind)


def read_status(root_fd: int) -> dict:
    try:
        status = json.loads(read_snapshot_file(root_fd, "status.json") or b"null")
    except ValueError:
        return {}
    return status if isinstance(status, dict) else {}


def snapshot_row(root_fd: int, target: Target) -> dict:
    """The status.json ticket row that names `target`. The snapshot is the
    allowlist, so the server never acts on a ticket the dashboard does not show."""
    tickets = read_status(root_fd).get("tickets")
    for row in tickets if isinstance(tickets, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("repo"), str):
            continue
        if row["repo"].casefold() != target.repo.casefold():
            continue
        if target.kind == "issue" and row.get("source") == f"#{target.number}":
            return row
        if target.kind == "pull" and row.get("pr") == str(target.number):
            return row
    raise ApiError(404, "not-in-snapshot")


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_slot_max(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 64


FIELDS = {"enabled": _is_bool, "drain": _is_bool, "slot_max": _is_slot_max}


def _is_devices(value: object) -> bool:
    return (isinstance(value, list) and all(isinstance(item, str) for item in value)
            and len(set(value)) == len(value))


def _is_max_jobs_expect(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


ACCOUNT_ID = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]{1,64}$")
ACCOUNT_HARNESSES = ("claude", "codex", "cursor", "external")
DEFAULT_ACCOUNT_IDS = frozenset(
    f"{harness}-default" for harness in ACCOUNT_HARNESSES if harness != "external"
)
ACCOUNT_REQUEST_KEYS = frozenset({"id", "actor", "requested_at", "target", "op", "account"})
ACCOUNT_OPS = {
    "add": frozenset({"harness", "max_jobs", "signed_in_on"}),
    "set": frozenset({"field", "expect", "value"}),
    "remove": frozenset({"expect"}),
    "prepare": frozenset({"device"}),
    "doctor": frozenset({"device"}),
}
ACCOUNT_FIELDS = {
    "max_jobs": (_is_slot_max, _is_max_jobs_expect),
    "paused": (_is_bool, _is_bool),
    "signed_in_on": (_is_devices, _is_devices),
}
REMOVE_EXPECT_KEYS = frozenset({"harness", "max_jobs", "paused", "signed_in_on"})


def _is_account_view(value: object) -> bool:
    return (isinstance(value, dict) and set(value) == REMOVE_EXPECT_KEYS
            and value["harness"] in ACCOUNT_HARNESSES
            and all(expect_ok(value[key]) for key, (_, expect_ok) in ACCOUNT_FIELDS.items()))


ACCOUNT_SHAPES: dict[str, Callable[[dict], bool]] = {
    "add": lambda p: (p["harness"] in ACCOUNT_HARNESSES and _is_slot_max(p["max_jobs"])
                      and _is_devices(p["signed_in_on"])),
    "set": lambda p: (ACCOUNT_FIELDS[p["field"]][0](p["value"])
                      and ACCOUNT_FIELDS[p["field"]][1](p["expect"])),
    "remove": lambda p: _is_account_view(p["expect"]),
    "prepare": lambda p: isinstance(p["device"], str),
    "doctor": lambda p: isinstance(p["device"], str),
}
ACCOUNT_DEVICES: dict[str, Callable[[dict], list]] = {
    "add": lambda p: p["signed_in_on"],
    "set": lambda p: p["value"] if p["field"] == "signed_in_on" else [],
    "remove": lambda p: [],
    "prepare": lambda p: [p["device"]],
    "doctor": lambda p: [p["device"]],
}


def is_bot_login(login: str) -> bool:
    return login.endswith("[bot]") or BOT_USER.fullmatch(login.casefold()) is not None


def require_approver(actions: Actions, session: Session) -> None:
    if session.login.casefold() != actions.config.approver.casefold() or is_bot_login(
        session.login
    ):
        raise ApiError(403, "not-approver")


def device_row(status: dict, device: object) -> dict:
    """The status.json `slots.devices[]` row named `device`: only devices the
    dashboard shows can be changed."""
    slots = status.get("slots")
    devices = slots.get("devices") if isinstance(slots, dict) else None
    for row in devices if isinstance(devices, list) else []:
        if isinstance(row, dict) and isinstance(device, str) and row.get("name") == device:
            return row
    raise ApiError(404, "not-in-snapshot")


def account_row(status: dict, account: str) -> dict | None:
    accounts = status.get("accounts")
    for row in accounts if isinstance(accounts, list) else []:
        if isinstance(row, dict) and row.get("id") == account:
            return row
    return None


def account_view(row: dict) -> dict:
    signed_in = row.get("signed_in_on")
    return {
        "harness": row.get("harness"),
        "max_jobs": row.get("max_jobs"),
        "paused": row.get("paused"),
        "signed_in_on": frozenset(
            entry["device"] for entry in (signed_in if isinstance(signed_in, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("device"), str)
        ),
    }


def applied_ids(status: dict) -> set[str]:
    settings = status.get("settings")
    results = settings.get("results") if isinstance(settings, dict) else None
    return {
        entry["id"] for entry in (results if isinstance(results, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


EASTERN = ZoneInfo("America/New_York")
MAX_WORDS = MAX_COMMENT // 2
COMMENT_ID = re.compile(r"[0-9]{1,20}")
ISSUE_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
AUDIT_FIELDS = ("repo", "ticket_id", "action", "expect_state", "head")
GO_HEADER = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->"
ANSWERS_HEADER = "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator -->"
CLOSE_HEADER = "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator state=abandoned -->"
RETRY_HEADER = "<!-- orch:v1 kind=retry from=grok-bot role=orchestrator -->"
HANDOFF_HEADER = "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator state=handed-off -->"


def clock() -> datetime:
    return datetime.now(EASTERN)


def words_block(words: str, at: datetime) -> str:
    """Max's words, each line quoted and any HTML comment marker escaped, so
    nothing he types can be a comment's first line or close a header."""
    at = at.astimezone(EASTERN)
    stamp = f"{at:%Y-%m-%d} {at.hour % 12 or 12}:{at:%M %p} ET"
    quoted = []
    for line in words.strip().splitlines():
        line = line.rstrip().replace("<!--", "&lt;!--").replace("-->", "--&gt;")
        quoted.append(f"> {line}" if line else ">")
    return f"**Max's words** (Max {stamp})\n" + "\n".join(quoted) + "\n"


class ActFacts(NamedTuple):
    repo: str
    ticket_id: str
    number: int
    head: str | None
    plan_comment_id: str | None
    words: str


def _split_body(facts: ActFacts) -> str:
    link = (
        f": {GITHUB_WEB}/{facts.repo}/issues/{facts.number}#issuecomment-{facts.plan_comment_id}"
        if facts.plan_comment_id
        else "."
    )
    return f"{CLOSE_HEADER}\n\nSplit per the plan's Proposed split{link}\n\n{facts.words}"


def _another_round_body(facts: ActFacts) -> str:
    return (
        f"{GO_HEADER}\n\n**Ticket**\n{facts.repo}#{facts.ticket_id}\n\n{facts.words}\n"
        "**Instruction**\nRun one more round, as Max's words above say.\n"
    )


class ActionSpec(NamedTuple):
    head_bound: bool
    body: Callable[[ActFacts], str]


# Mirrors lib/max_actions.py ACTIONS; tests/test_max_actions.py checks they match.
ACTIONS: dict[str, ActionSpec] = {
    "ok": ActionSpec(
        True, lambda facts: f"<!-- orch:v1 kind=ok head={facts.head} -->\n\n{facts.words}"
    ),
    "approve-plan": ActionSpec(False, lambda facts: f"{GO_HEADER}\n\n{facts.words}"),
    "split": ActionSpec(False, _split_body),
    "another-round": ActionSpec(False, _another_round_body),
    "answer": ActionSpec(False, lambda facts: f"{ANSWERS_HEADER}\n\n{facts.words}"),
    "retry": ActionSpec(False, lambda facts: f"{RETRY_HEADER}\n\n{facts.words}"),
    "resume": ActionSpec(False, lambda facts: f"{GO_HEADER}\n\n{facts.words}"),
    "close": ActionSpec(False, lambda facts: f"{CLOSE_HEADER}\n\n{facts.words}"),
    "hand-off": ActionSpec(False, lambda facts: f"{HANDOFF_HEADER}\n\n{facts.words}"),
}


def ticket_row(status: dict, repo: object, ticket_id: object) -> dict:
    tickets = status.get("tickets")
    if isinstance(repo, str) and isinstance(ticket_id, str):
        for row in tickets if isinstance(tickets, list) else []:
            if (
                isinstance(row, dict)
                and isinstance(row.get("repo"), str)
                and row["repo"].casefold() == repo.casefold()
                and row.get("ticket_id") == ticket_id
            ):
                return row
    raise ApiError(404, "not-in-snapshot")


def comment_target(row: dict) -> Target | None:
    """The row's PR, else its GitHub issue. A Linear ticket has only its PR."""
    repo, pr, source = row["repo"], row.get("pr"), row.get("source")
    if REPO.fullmatch(repo) is None:
        return None
    if isinstance(pr, (str, int)) and not isinstance(pr, bool) and ISSUE_NUMBER.fullmatch(str(pr)):
        return Target(repo, int(pr), "pull")
    if isinstance(source, str) and source.startswith("#") and ISSUE_NUMBER.fullmatch(source[1:]):
        return Target(repo, int(source[1:]), "issue")
    return None


def _audit_value(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 256 else None


class Audit:
    """The append-only action record. Each line is one os.write on an
    O_APPEND descriptor, so concurrent request threads never interleave."""

    def __init__(self, path: Path):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(f"{path} is not a regular file")
        self.fd = fd

    def append(self, entry: dict) -> None:
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
        try:
            os.write(self.fd, line)
        except OSError:
            # The reply must still reach the page: the comment may already be posted.
            sys.stderr.write("factory-dashboard-http: audit write failed\n")

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class Spool:
    """The settings request directory, pinned like the served root. This
    process is its only writer, so the lock makes the pending cap exact."""

    def __init__(self, path: Path):
        self.fd = open_root(path)
        self.lock = threading.Lock()

    def submit(self, request: dict, applied: set[str]) -> None:
        with self.lock:
            pending = 0
            for name in os.listdir(self.fd):
                found = REQUEST_FILE.fullmatch(name)
                if found is None:
                    continue
                if found[1] in applied:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=self.fd)
                else:
                    pending += 1
            if pending >= MAX_PENDING:
                raise ApiError(429, "busy")
            self._write(request)

    def _write(self, request: dict) -> None:
        temp, final = f".{request['id']}.tmp", f"{request['id']}.json"
        data = (json.dumps(request, sort_keys=True) + "\n").encode()
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW
            with os.fdopen(os.open(temp, flags, 0o644, dir_fd=self.fd), "wb") as handle:
                os.fchmod(handle.fileno(), 0o644)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temp, final, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=self.fd)
            raise ApiError(500, "spool") from None

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def cookie(name: str, value: str, max_age: int) -> str:
    return f"{name}={value}; HttpOnly; SameSite=Strict; Path=/api; Max-Age={max_age}"


class SnapshotHandler(BaseHTTPRequestHandler):
    server_version = "factory-dashboard"
    sys_version = ""
    timeout = 15  # a client that stalls mid-request is dropped

    def log_message(self, format: str, *args: object) -> None:
        pass

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        path = urlsplit(getattr(self, "path", "") or "").path
        status = int(code) if isinstance(code, int) else "-"
        method = getattr(self, "command", None) or "-"
        sys.stderr.write(f"{method} {path or '-'} {status} {self.client_address[0]}\n")

    def do_GET(self) -> None:
        if self._api_get():
            return
        self._serve(body=True)

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def do_POST(self) -> None:
        actions: Actions | None = self.server.actions  # type: ignore[attr-defined]
        if actions is None:
            self.send_error(501, f"Unsupported method ({self.command!r})")
            return
        route = self.server.post_routes.get(urlsplit(self.path).path)  # type: ignore[attr-defined]
        if route is None or not actions.allows(self.client_address[0]):
            self._json(404, {"error": "not-found"})
            return
        handler, needs_session = route
        try:
            payload = self._read_json()
            session = self._session(actions) if needs_session else None
            status, body, cookies = handler(self, actions, session, payload)
        except ApiError as exc:
            self._json(exc.status, exc.body)
            return
        self._json(status, body, cookies)

    def _api_get(self) -> bool:
        actions: Actions | None = self.server.actions  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if actions is None or not (path == "/api" or path.startswith("/api/")):
            return False
        if path != "/api/session" or not actions.allows(self.client_address[0]):
            self._json(404, {"error": "not-found"})
            return True
        session = actions.current(self._cookie("factory_session"))
        if session is None:
            self._json(200, {"signed_in": False})
        else:
            self._json(200, {"signed_in": True, "login": session.login, "csrf": session.csrf})
        return True

    def _read_json(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(403, "content-type")
        host = self.headers.get("Host", "")
        if not host or self.headers.get("Origin") != f"http://{host}":
            raise ApiError(403, "origin")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > MAX_REQUEST:
            raise ApiError(400, "bad-length")
        try:
            payload = json.loads(self.rfile.read(int(length)) or b"{}")
        except ValueError:
            raise ApiError(400, "bad-json") from None
        if not isinstance(payload, dict):
            raise ApiError(400, "bad-json")
        return payload

    def _session(self, actions: Actions) -> Session:
        session = actions.current(self._cookie("factory_session"))
        if session is None:
            raise ApiError(401, "no-session")
        sent = self.headers.get("X-Factory-CSRF", "")
        if not secrets.compare_digest(sent.encode(), session.csrf.encode()):
            raise ApiError(403, "csrf")
        return session

    def _cookie(self, name: str) -> str | None:
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                key, _, value = part.strip().partition("=")
                if key == name and value:
                    return value
        return None

    def _signin(self, actions: Actions, session: Session | None, payload: dict):
        flow_id, grant = actions.start_flow()
        return 200, grant, [cookie("factory_flow", flow_id, int(grant["expires_in"]))]

    def _signin_poll(self, actions: Actions, session: Session | None, payload: dict):
        state, signed = actions.poll_flow(self._cookie("factory_flow"))
        if state == "pending":
            return 200, {"state": state}, []
        cookies = [cookie("factory_flow", "", 0)]
        if signed is None:
            return 200, {"state": state}, cookies
        cookies.append(cookie("factory_session", signed.session_id, SESSION_SECONDS))
        return 200, {"state": state, "login": signed.login, "csrf": signed.csrf}, cookies

    def _signout(self, actions: Actions, session: Session | None, payload: dict):
        actions.sign_out()
        return 200, {}, [cookie("factory_session", "", 0)]

    def _comment(self, actions: Actions, session: Session | None, payload: dict):
        assert session is not None
        target = parse_target(payload, str(payload.get("kind")), "number")
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_COMMENT:
            raise ApiError(400, "bad-body")
        if REFUSED_FIRST_LINE.match(body.lstrip().splitlines()[0]):
            raise ApiError(400, "reserved-body")
        snapshot_row(self.server.root_fd, target)  # type: ignore[attr-defined]
        return 201, {"url": actions.post_as_approver(session, target, body)}, []

    def _head(self, actions: Actions, session: Session | None, payload: dict):
        assert session is not None
        target = parse_target(payload, "pull", "pr")
        snapshot_row(self.server.root_fd, target)  # type: ignore[attr-defined]
        return 200, {"head": actions.head(session, target)}, []

    def _ok(self, actions: Actions, session: Session | None, payload: dict):
        assert session is not None
        target = parse_target(payload, "pull", "pr")
        seen = payload.get("head")
        if not isinstance(seen, str) or SHA.fullmatch(seen) is None:
            raise ApiError(400, "bad-head")
        row = snapshot_row(self.server.root_fd, target)  # type: ignore[attr-defined]
        if row.get("state") != "awaiting-ok":
            raise ApiError(409, "not-awaiting-ok")
        current = actions.head(session, target)
        if current != seen:
            raise ApiError(409, "head-changed", head=current)
        return 201, {"url": actions.post_as_approver(session, target, f"/ok {seen}")}, []

    def _act(self, actions: Actions, session: Session | None, payload: dict):
        assert session is not None
        at = clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {"at": at, "login": session.login,
                 **{key: _audit_value(payload.get(key)) for key in AUDIT_FIELDS}}
        audit: Audit = self.server.audit  # type: ignore[attr-defined]
        try:
            url = self._post_act(actions, session, payload)
        except ApiError as exc:
            audit.append({**entry, "result": exc.body["error"], "url": None})
            raise
        audit.append({**entry, "result": "posted", "url": url})
        return 201, {"url": url}, []

    def _post_act(self, actions: Actions, session: Session, payload: dict) -> str:
        require_approver(actions, session)
        name = payload.get("action")
        spec = ACTIONS.get(name) if isinstance(name, str) else None
        if spec is None:
            raise ApiError(400, "bad-action")
        words = payload.get("words")
        if not isinstance(words, str) or not words.strip() or len(words) > MAX_WORDS:
            raise ApiError(400, "bad-words")
        head = payload.get("head") if spec.head_bound else None
        if spec.head_bound and (not isinstance(head, str) or SHA.fullmatch(head) is None):
            raise ApiError(400, "bad-head")
        status = read_status(self.server.root_fd)  # type: ignore[attr-defined]
        row = ticket_row(status, payload.get("repo"), payload.get("ticket_id"))
        wait = row.get("wait") if isinstance(row.get("wait"), dict) else {}
        if row.get("state") != payload.get("expect_state") or wait.get("why") != payload.get(
            "expect_why"
        ):
            raise ApiError(409, "changed")
        listed = row.get("actions")
        if not isinstance(listed, list) or name not in listed:
            raise ApiError(409, "not-applicable")
        target = comment_target(row)
        if target is None or (spec.head_bound and target.kind != "pull"):
            raise ApiError(409, "no-target")
        if spec.head_bound:
            current = actions.head(session, target)
            if current != head:
                raise ApiError(409, "head-changed", head=current)
        plan = wait.get("comment_id")
        facts = ActFacts(
            target.repo, row["ticket_id"], target.number, head,
            plan if isinstance(plan, str) and COMMENT_ID.fullmatch(plan) else None,
            words_block(words, clock()),
        )
        return actions.post_as_approver(session, target, spec.body(facts))

    def _settings(self, actions: Actions, session: Session | None, payload: dict):
        assert session is not None
        require_approver(actions, session)
        if payload.get("target") == "account":
            return self._account_settings(session, payload)
        field = payload.get("field")
        valid = FIELDS.get(field) if isinstance(field, str) else None
        if valid is None:
            raise ApiError(400, "bad-field")
        expect, value = payload.get("expect"), payload.get("value")
        if not (valid(expect) and valid(value)):
            raise ApiError(400, "bad-value")
        status = read_status(self.server.root_fd)  # type: ignore[attr-defined]
        row = device_row(status, payload.get("device"))
        if row.get(field) != expect:
            raise ApiError(409, "changed")
        request = {
            "id": secrets.token_urlsafe(16),
            "actor": session.login,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device": row["name"],
            "field": field,
            "expect": expect,
            "value": value,
        }
        self.server.spool.submit(request, applied_ids(status))  # type: ignore[attr-defined]
        return 202, {"id": request["id"]}, []

    def _account_settings(self, session: Session, payload: dict):
        op = payload.get("op")
        if not isinstance(op, str) or op not in ACCOUNT_OPS:
            raise ApiError(400, "bad-op")
        if set(payload) != {"target", "op", "account"} | ACCOUNT_OPS[op]:
            raise ApiError(400, "bad-keys")
        name, field = payload["account"], payload.get("field")
        if not isinstance(name, str) or ACCOUNT_ID.fullmatch(name) is None:
            raise ApiError(400, "bad-value")
        if op == "set" and (not isinstance(field, str) or field not in ACCOUNT_FIELDS):
            raise ApiError(400, "bad-field")
        if not ACCOUNT_SHAPES[op](payload):
            raise ApiError(400, "bad-value")
        status = read_status(self.server.root_fd)  # type: ignore[attr-defined]
        row = account_row(status, name)
        if row is None and op != "add":
            raise ApiError(404, "not-in-snapshot")
        for device in ACCOUNT_DEVICES[op](payload):
            device_row(status, device)
        view = account_view(row) if row is not None else {}
        if "device" in payload and payload["device"] not in view["signed_in_on"]:
            raise ApiError(404, "not-in-snapshot")
        if name in DEFAULT_ACCOUNT_IDS or (row or {}).get("read_only") is not None or (
            op == "add" and payload["harness"] == "external"
        ):
            raise ApiError(403, "read-only")
        if op == "add" and row is not None:
            raise ApiError(409, "exists")
        claimed = {field: payload["expect"]} if op == "set" else payload.get("expect", {})
        if any(view[key] != (frozenset(value) if isinstance(value, list) else value)
               for key, value in claimed.items()):
            raise ApiError(409, "changed")
        request = {
            "id": secrets.token_urlsafe(16),
            "actor": session.login,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **payload,
        }
        self.server.spool.submit(request, applied_ids(status))  # type: ignore[attr-defined]
        return 202, {"id": request["id"]}, []

    def _json(self, code: int, payload: dict, cookies: list[str] | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for value in cookies or []:
            self.send_header("Set-Cookie", value)
        self.end_headers()
        self.wfile.write(data)

    def _serve(self, *, body: bool) -> None:
        path = urlsplit(self.path).path
        if path.startswith(QA_RUNS):
            self._serve_qa(path, body=body)
            return
        name = ROUTES.get(path)
        data = read_snapshot_file(self.server.root_fd, name) if name else None  # type: ignore[attr-defined]
        if name is None or data is None:
            self._send(404, b"not found\n", "text/plain; charset=utf-8", body=body)
            return
        self._send(200, data, CONTENT_TYPES[name], body=body)

    def _serve_qa(self, path: str, *, body: bool) -> None:
        route = qa_route(path)
        fd = open_under(self.server.root_fd, route.parts) if route else None  # type: ignore[attr-defined]
        if route is None or fd is None:
            self._send(404, b"not found\n", "text/plain; charset=utf-8", body=body)
            return
        with os.fdopen(fd, "rb") as handle:
            remaining = os.fstat(handle.fileno()).st_size
            self._send_head(200, route.content_type, remaining, route.headers)
            while body and remaining > 0 and (chunk := handle.read(min(QA_CHUNK, remaining))):
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send(self, code: int, data: bytes, content_type: str, *, body: bool) -> None:
        self._send_head(code, content_type, len(data), SECURITY_HEADERS)
        if body:
            self.wfile.write(data)

    def _send_head(self, code: int, content_type: str, length: int, headers: dict[str, str]) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()


POST_ROUTES = {
    "/api/signin": (SnapshotHandler._signin, False),
    "/api/signin/poll": (SnapshotHandler._signin_poll, False),
    "/api/signout": (SnapshotHandler._signout, True),
    "/api/comment": (SnapshotHandler._comment, True),
    "/api/head": (SnapshotHandler._head, True),
    "/api/ok": (SnapshotHandler._ok, True),
}
SETTINGS_ROUTES = {"/api/settings": (SnapshotHandler._settings, True)}
ACT_ROUTES = {"/api/act": (SnapshotHandler._act, True)}


class SnapshotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path, actions: ActionsConfig | None = None):
        self.root = root
        self.actions = Actions(actions) if actions is not None else None
        self.spool: Spool | None = None
        self.audit: Audit | None = None
        self.root_fd = open_root(root)
        try:
            if actions is not None and actions.settings_spool is not None:
                self.spool = Spool(actions.settings_spool)
            if actions is not None and actions.audit is not None:
                self.audit = Audit(actions.audit)
            self.post_routes = {
                **POST_ROUTES,
                **(SETTINGS_ROUTES if self.spool else {}),
                **(ACT_ROUTES if self.audit else {}),
            }
            super().__init__(address, SnapshotHandler)
        except BaseException:
            os.close(self.root_fd)
            for held in (self.spool, self.audit):
                if held is not None:
                    held.close()
            raise

    def server_close(self) -> None:
        super().server_close()
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1
        for held in (self.spool, self.audit):
            if held is not None:
                held.close()


def make_server(
    host: str, port: int, root: Path, actions: ActionsConfig | None = None
) -> SnapshotServer:
    """Serve `root` on host:port. Callers outside tests go through load_config."""
    return SnapshotServer((host, port), root, actions)


def main(argv: list[str]) -> int:
    if argv not in ([], ["--check"]):
        print("usage: factory-dashboard-http.py [--check]", file=sys.stderr)
        return 2
    try:
        host, port, root, actions = load_config(dict(os.environ))
    except ConfigError as exc:
        print(f"factory-dashboard-http: {exc}", file=sys.stderr)
        return 1
    if argv == ["--check"]:
        suffix = f" with GitHub actions for {actions.approver}" if actions else ""
        if actions and actions.settings_spool:
            suffix += f" and settings requests to {actions.settings_spool}"
        if actions and actions.audit:
            suffix += f" and Max's actions audited to {actions.audit}"
        print(f"ok: would serve {root} on {host}:{port}{suffix}")
        return 0
    server = make_server(host, port, root, actions)
    print(f"factory-dashboard-http: serving {root} on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
