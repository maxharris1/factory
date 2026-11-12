"""Linear webhook listener: verify a delivery, mark its issue dirty, return.

`orch linear-webhook serve` (issue #55, docs/SPEC.md section 7) is a small
stdlib HTTP server. For each request it:

1. accepts POST on the one configured path only (404 elsewhere, 405 for
   other methods);
2. refuses a client address outside Linear's published source IPs when it
   can see one (the TCP peer, or the last X-Forwarded-For hop when the peer
   is a local proxy such as Tailscale Funnel);
3. requires a Content-Length of at most MAX_BODY_BYTES, read before the body;
4. verifies `Linear-Signature`, the hex HMAC-SHA256 of the raw body under
   the signing secret, with a constant-time compare;
5. refuses a `webhookTimestamp` more than TIMESTAMP_TOLERANCE_SECONDS from
   now (replay guard; the timestamp is inside the signed body);
6. for an Issue in a configured Linear project, or a Comment on a tracked
   issue or carrying an `orch:v1` handoff marker, upserts one
   `linear_dirty` row per issue in the dispatcher database. Everything else
   that is signed and fresh gets 200 and is ignored.

It never runs handoff logic and never calls Linear. The poller reads the
dirty rows (lib/poll_source.py). Log lines carry the outcome, the event type
and the issue identifier only: never bodies (they can hold PHI), headers,
signatures or the secret. The secret is read once from the configured file
and is never logged, printed or included in an error.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import sqlite3
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# https://linear.app/docs/security ("source IP addresses for all outbound
# traffic (webhooks, integrations, etc.)"), also published as
# https://linear.app/.well-known/appspecific/app.linear.ips.json.
LINEAR_SOURCE_IPS = (
    "34.134.222.122",
    "34.140.253.14",
    "34.185.239.137",
    "34.186.126.124",
    "34.38.87.206",
    "34.48.40.158",
    "34.60.255.158",
    "34.62.119.29",
    "35.196.141.51",
    "35.222.25.142",
    "35.231.147.226",
    "35.236.218.67",
    "35.243.134.228",
    "35.246.206.27",
    "35.246.210.220",
)
DEFAULT_PATH = "/linear-webhook"
MAX_BODY_BYTES = 1 << 20
TIMESTAMP_TOLERANCE_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10
SILENCE_WARNING_SECONDS = 3600
DIRTY_TABLE = "linear_dirty"
HEALTH_TABLE = "linear_webhook_health"
HANDOFF_MARKER = "orch:v1"

_TOKEN = re.compile(r"^[A-Za-z]{1,40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]{1,16}-[0-9]{1,9}$")
_ISSUE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")


class WebhookSecretError(RuntimeError):
    pass


@dataclass(frozen=True)
class DirtyRow:
    issue_id: str
    identifier: str | None
    event: str
    received_at: float


@dataclass(frozen=True)
class WebhookResult:
    status: int
    outcome: str  # accepted, ignored or rejected
    reason: str


@dataclass(frozen=True)
class WebhookHealth:
    started_at: float | None
    last_received_at: float | None
    last_event: str | None
    dirty: int
    active_linear: int = 0

    def silence_warning(self, now: float) -> str | None:
        """Over an hour with no delivery while Linear tickets are active."""
        if self.active_linear < 1:
            return None
        since = self.last_received_at
        if since is None:
            since = self.started_at
        if since is None or now - since <= SILENCE_WARNING_SECONDS:
            return None
        return (
            "no Linear webhook for over 1h while "
            f"{self.active_linear} Linear ticket(s) are active"
        )


def read_secret(path: Path) -> bytes:
    """The signing secret, refused if empty or readable by group or others.

    Errors name the path and the problem, never any content.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        raise WebhookSecretError(
            f"{path}: signing secret file cannot be read"
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise WebhookSecretError(f"{path}: signing secret is not a file")
        if info.st_mode & 0o077:
            raise WebhookSecretError(
                f"{path}: signing secret file is readable by group or others; "
                "chmod 600 it"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(64 * 1024 + 1)
    except OSError:
        raise WebhookSecretError(
            f"{path}: signing secret file cannot be read"
        ) from None
    finally:
        os.close(descriptor)
    secret = content.strip()
    if not secret or len(content) > 64 * 1024:
        reason = "is empty" if not secret else "is too large"
        raise WebhookSecretError(f"{path}: signing secret file {reason}")
    return secret


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {DIRTY_TABLE} (
            issue_id TEXT PRIMARY KEY,
            identifier TEXT,
            event TEXT NOT NULL,
            received_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {HEALTH_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            started_at REAL NOT NULL,
            last_received_at REAL,
            last_event TEXT
        )
        """
    )


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _connect_existing(path: Path) -> sqlite3.Connection:
    """Open the dispatcher database read-write; never create it."""
    connection = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=rw",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


_TERMINAL_STATES = ("merged", "handed-off", "done", "abandoned")


def _active_linear(connection: sqlite3.Connection) -> int:
    """Tracked Linear tickets not yet merged, handed off, done or abandoned."""
    if not _has_table(connection, "tracked_tickets"):
        return 0
    join = ""
    where = ""
    if _has_table(connection, "ticket_state"):
        marks = ",".join("?" for _ in _TERMINAL_STATES)
        join = (
            " LEFT JOIN ticket_state AS state ON state.repo = tracked.repo"
            " AND state.ticket_id = tracked.ticket_id"
        )
        where = (
            " AND (state.workflow_state IS NULL"
            f" OR state.workflow_state NOT IN ({marks}))"
        )
    return int(
        connection.execute(
            "SELECT count(*) FROM tracked_tickets AS tracked"
            + join
            + " WHERE tracked.tracker = 'linear'"
            + where,
            _TERMINAL_STATES if where else (),
        ).fetchone()[0]
    )


def read_health(connection: sqlite3.Connection) -> WebhookHealth | None:
    """Webhook health, or None when no listener ever ran on this database.

    Read-only: it only selects, so `orch status` and the dashboard can use it.
    """
    if not _has_table(connection, HEALTH_TABLE):
        return None
    row = connection.execute(
        f"SELECT started_at, last_received_at, last_event FROM {HEALTH_TABLE} "
        "WHERE id = 1"
    ).fetchone()
    dirty = (
        int(connection.execute(f"SELECT count(*) FROM {DIRTY_TABLE}").fetchone()[0])
        if _has_table(connection, DIRTY_TABLE)
        else 0
    )
    active = _active_linear(connection)
    if row is None:
        return WebhookHealth(None, None, None, dirty, active)
    return WebhookHealth(
        started_at=_float(row[0]),
        last_received_at=_float(row[1]),
        last_event=str(row[2]) if row[2] is not None else None,
        dirty=dirty,
        active_linear=active,
    )


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


class LinearDirtyQueue:
    """The poller's view of `linear_dirty`: read pending rows, ack fetched ones.

    A row is acked only if its received_at is unchanged since it was read,
    so an event that lands while the poller is fetching stays queued.
    """

    def __init__(self, path: Path):
        self.path = path

    def pending(self) -> tuple[DirtyRow, ...]:
        connection = _connect_existing(self.path)
        try:
            if not _has_table(connection, DIRTY_TABLE):
                return ()
            rows = connection.execute(
                f"SELECT issue_id, identifier, event, received_at FROM {DIRTY_TABLE} "
                "ORDER BY received_at, issue_id"
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            DirtyRow(
                issue_id=str(row["issue_id"]),
                identifier=(
                    str(row["identifier"]) if row["identifier"] is not None else None
                ),
                event=str(row["event"]),
                received_at=float(row["received_at"]),
            )
            for row in rows
        )

    def ack(self, rows: Iterable[DirtyRow]) -> None:
        rows = tuple(rows)
        if not rows:
            return
        connection = _connect_existing(self.path)
        try:
            with connection:
                if not _has_table(connection, DIRTY_TABLE):
                    return
                connection.executemany(
                    f"DELETE FROM {DIRTY_TABLE} WHERE issue_id = ? AND received_at = ?",
                    [(row.issue_id, row.received_at) for row in rows],
                )
        finally:
            connection.close()


class WebhookApp:
    """Request handling without sockets, so every rule is testable directly."""

    def __init__(
        self,
        *,
        secret: bytes,
        project_ids: frozenset[str],
        db_path: Path,
        path: str = DEFAULT_PATH,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print,
        source_ips: Iterable[str] = LINEAR_SOURCE_IPS,
        max_body_bytes: int = MAX_BODY_BYTES,
    ):
        if not secret:
            raise WebhookSecretError("signing secret is empty")
        if not project_ids:
            raise ValueError("linear_webhook needs at least one Linear project")
        self.secret = secret
        self.project_ids = frozenset(project_ids)
        self.db_path = db_path
        self.path = path
        self.clock = clock
        self.log = log
        self.source_ips = frozenset(
            ipaddress.ip_address(address) for address in source_ips
        )
        self.max_body_bytes = max_body_bytes
        connection = _connect_existing(db_path)
        try:
            with connection:
                ensure_tables(connection)
                connection.execute(
                    f"""
                    INSERT INTO {HEALTH_TABLE} (id, started_at) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET started_at = excluded.started_at
                    """,
                    (self.clock(),),
                )
        finally:
            connection.close()

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes | Callable[[int], bytes],
        client_ip: str,
    ) -> WebhookResult:
        try:
            return self._handle(method, target, headers, body, client_ip)
        except Exception as exc:  # never echo a body, header or secret
            self.log(f"linear-webhook: error {type(exc).__name__}")
            return WebhookResult(500, "rejected", "error")

    def _handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes | Callable[[int], bytes],
        client_ip: str,
    ) -> WebhookResult:
        lowered = {str(name).lower(): str(value) for name, value in headers.items()}
        if urlsplit(target).path != self.path:
            return WebhookResult(404, "rejected", "path")
        if method != "POST":
            return WebhookResult(405, "rejected", "method")
        if not self._source_allowed(client_ip, lowered.get("x-forwarded-for")):
            self.log("linear-webhook: rejected source address")
            return WebhookResult(403, "rejected", "source")
        length_text = lowered.get("content-length", "")
        if not length_text.isdigit():
            return WebhookResult(411, "rejected", "length")
        length = int(length_text)
        if length > self.max_body_bytes:
            self.log("linear-webhook: rejected oversized body")
            return WebhookResult(413, "rejected", "size")
        raw = body(length) if callable(body) else body
        if len(raw) != length:
            return WebhookResult(400, "rejected", "length")
        if not self._signed(raw, lowered.get("linear-signature")):
            self.log("linear-webhook: rejected signature")
            return WebhookResult(401, "rejected", "signature")
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            event = None
        if not isinstance(event, dict):
            self.log("linear-webhook: rejected malformed body")
            return WebhookResult(400, "rejected", "json")
        now = self.clock()
        if not _fresh(event.get("webhookTimestamp"), now):
            self.log("linear-webhook: rejected stale timestamp")
            return WebhookResult(401, "rejected", "timestamp")
        return self._record(event, now)

    def _source_allowed(self, peer: str, forwarded: str | None) -> bool:
        address = _ip(peer)
        if address is None:
            return False
        if address.is_loopback:
            # A local reverse proxy (Tailscale Funnel) appends the address it
            # saw as the last hop; earlier hops are client-controlled.
            if not forwarded:
                return True  # no client address is visible; rely on the HMAC
            address = _ip(forwarded.split(",")[-1].strip())
            if address is None:
                return False
        return address in self.source_ips

    def _signed(self, raw: bytes, provided: str | None) -> bool:
        if provided is None:
            return False
        expected = hmac.new(self.secret, raw, hashlib.sha256).hexdigest()
        candidate = provided.strip().encode("utf-8", "replace")
        matched = hmac.compare_digest(expected.encode("ascii"), candidate)
        return matched and _SIGNATURE.fullmatch(provided.strip()) is not None

    def _record(self, event: dict, now: float) -> WebhookResult:
        kind = _token(event.get("type"))
        label = f"{kind}.{_token(event.get('action'))}"
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        connection = _connect_existing(self.db_path)
        try:
            with connection:
                connection.execute(
                    f"""
                    INSERT INTO {HEALTH_TABLE} (id, started_at, last_received_at, last_event)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      last_received_at = excluded.last_received_at,
                      last_event = excluded.last_event
                    """,
                    (now, now, label),
                )
                if kind == "Issue":
                    issue_id, identifier, reason = self._issue(
                        data, event.get("updatedFrom")
                    )
                elif kind == "Comment":
                    issue_id, identifier, reason = self._comment(connection, data)
                else:
                    issue_id, identifier, reason = None, None, "event type"
                if reason is not None or issue_id is None:
                    shown = identifier or "-"
                    self.log(
                        f"linear-webhook: ignored {label} {shown} ({reason})"
                        if kind in ("Issue", "Comment")
                        else f"linear-webhook: ignored {label} ({reason})"
                    )
                    return WebhookResult(200, "ignored", reason or "issue")
                connection.execute(
                    f"""
                    INSERT INTO {DIRTY_TABLE} (issue_id, identifier, event, received_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                      identifier = COALESCE(excluded.identifier, {DIRTY_TABLE}.identifier),
                      event = excluded.event,
                      received_at = CASE
                        WHEN excluded.received_at > {DIRTY_TABLE}.received_at
                        THEN excluded.received_at
                        ELSE {DIRTY_TABLE}.received_at + 0.001
                      END
                    """,
                    (issue_id, identifier, label, now),
                )
        finally:
            connection.close()
        self.log(f"linear-webhook: accepted {label} {identifier or '-'}")
        return WebhookResult(200, "accepted", "dirty")

    def _issue(
        self,
        data: dict,
        before: object,
    ) -> tuple[str | None, str | None, str | None]:
        """An Issue in, or just moved out of, a configured project."""
        issue_id = _issue_id(data.get("id"))
        identifier = _identifier(data.get("identifier"))
        if issue_id is None:
            return None, identifier, "no issue id"
        projects = {data.get("projectId")}
        if isinstance(before, dict):
            projects.add(before.get("projectId"))
        if not any(
            isinstance(project, str) and project in self.project_ids
            for project in projects
        ):
            return issue_id, identifier, "not a configured project"
        return issue_id, identifier, None

    def _comment(
        self,
        connection: sqlite3.Connection,
        data: dict,
    ) -> tuple[str | None, str | None, str | None]:
        """A Comment payload names its issue but not the issue's project.

        Accept it when the factory tracks that issue, or when the comment
        carries an orch:v1 marker (a brief or go may start tracking). The
        poller still checks the project after its direct read.
        """
        issue = data.get("issue")
        issue = issue if isinstance(issue, dict) else {}
        issue_id = _issue_id(data.get("issueId")) or _issue_id(issue.get("id"))
        identifier = _identifier(issue.get("identifier"))
        if issue_id is None:
            return None, identifier, "not on an issue"
        body = data.get("body")
        if isinstance(body, str) and HANDOFF_MARKER in body:
            return issue_id, identifier, None
        if identifier is not None and _is_tracked(connection, identifier):
            return issue_id, identifier, None
        return issue_id, identifier, "untracked issue"


def _is_tracked(connection: sqlite3.Connection, identifier: str) -> bool:
    if not _has_table(connection, "tracked_tickets"):
        return False
    return (
        connection.execute(
            "SELECT 1 FROM tracked_tickets WHERE tracker = 'linear' AND ticket_id = ?",
            (identifier,),
        ).fetchone()
        is not None
    )


def _fresh(value: object, now: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return abs(now * 1000 - value) <= TIMESTAMP_TOLERANCE_SECONDS * 1000


def _token(value: object) -> str:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else "unknown"


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _issue_id(value: object) -> str | None:
    return value if isinstance(value, str) and _ISSUE_ID.fullmatch(value) else None


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    app: WebhookApp


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "factory-linear-webhook"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def do_POST(self) -> None:
        self._respond("POST")

    def do_GET(self) -> None:
        self._respond("GET")

    def do_HEAD(self) -> None:
        self._respond("HEAD")

    def do_PUT(self) -> None:
        self._respond("PUT")

    def do_PATCH(self) -> None:
        self._respond("PATCH")

    def do_DELETE(self) -> None:
        self._respond("DELETE")

    def _respond(self, method: str) -> None:
        headers: dict[str, str] = {}
        for name in set(self.headers.keys()):
            values = self.headers.get_all(name) or []
            headers[name.lower()] = ", ".join(values)
        result = self.server.app.handle(
            method,
            self.path,
            headers,
            self.rfile.read,
            self.client_address[0],
        )
        payload = f"{result.outcome}\n".encode("ascii")
        self.send_response(result.status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        del format, args  # WebhookApp logs outcomes; never the request line


def make_server(app: WebhookApp, host: str, port: int) -> ThreadingHTTPServer:
    server_class = type(
        "_BoundServer",
        (_Server,),
        {"address_family": _family(host)},
    )
    server = server_class((host, port), _Handler)
    server.app = app
    return server


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET
