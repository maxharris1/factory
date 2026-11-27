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
   issue, or a Comment carrying an `orch:v1` handoff marker on an issue a
   signed Issue event placed in a project that is still configured, upserts
   one `linear_dirty` row per issue in the dispatcher database. Everything
   else that is signed and fresh gets 200 and is ignored.

Health (#79, #107): every delivery whose signature verifies, whatever
happens to it next, updates `last_received_at`; it proves the path works.
Each signed, fresh delivery that names an issue also records when that
issue was last delivered (`linear_webhook_seen`). The poller's direct reads
compare an issue's newest comment with that record (`missed_delivery`) and
record a real miss in `linear_webhook_missed`. Only a miss in the last 24 h
is a warning; a quiet hour with nothing missed is a soft note.

At most MAX_WORKERS requests are worked on at once (a connection beyond
that is closed unread), and each must arrive within one total deadline of
REQUEST_DEADLINE_SECONDS, however slowly its bytes trickle in.

It never runs handoff logic and never calls Linear. The poller reads the
dirty rows (lib/poll_source.py). Log lines carry the outcome, the event type
and the issue identifier only: never bodies (they can hold PHI), headers,
signatures or the secret. The secret is read once from the configured file
and is never logged, printed or included in an error.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from lib import epoch
from lib.health import age_text

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
REQUEST_DEADLINE_SECONDS = 10
MAX_WORKERS = 8
BUSY_LOG_SECONDS = 60
QUIET_NOTE_SECONDS = 3600
MISSED_WINDOW_SECONDS = 86400
MISSED_KEEP_SECONDS = 7 * 86400
MISS_AFTER_START_SECONDS = 60
MISS_GRACE_SECONDS = 120
MISS_SLACK_SECONDS = 60
DIRTY_TABLE = "linear_dirty"
HEALTH_TABLE = "linear_webhook_health"
MEMBER_TABLE = "linear_project_issues"
POLLING_TABLE = "linear_webhook_polling"
SEEN_TABLE = "linear_webhook_seen"
MISSED_TABLE = "linear_webhook_missed"
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
    # When the poller first ran in webhook mode on this database; the
    # quiet clock for a listener that never started.
    polling_since: float | None = None
    # Missed deliveries the poller found, newest first: (identifier, found_at).
    missed: tuple[tuple[str | None, float], ...] = ()

    def warning(self, now: float) -> str | None:
        """A delivery the poller's reconcile found missing in the last 24 h."""
        recent = [item for item in self.missed if now - item[1] <= MISSED_WINDOW_SECONDS]
        if not recent:
            return None
        identifier, found_at = recent[0]
        count = len(recent)
        return (
            f"Linear webhook missed {count} deliver{'y' if count == 1 else 'ies'} in 24h "
            f"(newest {identifier or 'an issue'}, found {age_text(now - found_at)} ago)"
        )

    def quiet_note(self, now: float) -> str | None:
        """Over an hour with no delivery while Linear tickets are active, and
        nothing missed: Linear may simply have had nothing to send."""
        if self.active_linear < 1 or self.warning(now) is not None:
            return None
        since = self.last_received_at
        if since is None:
            since = self.started_at
        if since is None:
            since = self.polling_since
        if since is None or now - since <= QUIET_NOTE_SECONDS:
            return None
        return (
            f"quiet: no Linear webhook for {age_text(now - since)} "
            "(nothing missed by the last reconcile)"
        )


def missed_delivery(
    newest_comment_at: float | None,
    seen_at: float | None,
    listener_started_at: float | None,
    now: float,
) -> bool:
    """A comment the listener should have been sent and was not: made at
    least a minute after the listener started, at least two minutes ago,
    and no delivery for its issue since a minute before it."""
    if newest_comment_at is None or listener_started_at is None:
        return False
    if newest_comment_at < listener_started_at + MISS_AFTER_START_SECONDS:
        return False
    if now - newest_comment_at < MISS_GRACE_SECONDS:
        return False
    return seen_at is None or seen_at < newest_comment_at - MISS_SLACK_SECONDS


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
    # Issues a signed Issue event last placed in a configured project, and
    # which project: the only scope proof for a Comment on an issue the
    # factory does not track, valid while that project is still configured.
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MEMBER_TABLE} (
            issue_id TEXT PRIMARY KEY,
            seen_at REAL NOT NULL,
            project_id TEXT
        )
        """
    )
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({MEMBER_TABLE})")}
    if "project_id" not in columns:
        connection.execute(f"ALTER TABLE {MEMBER_TABLE} ADD COLUMN project_id TEXT")
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SEEN_TABLE} (
            issue_id TEXT PRIMARY KEY,
            received_at REAL NOT NULL
        )
        """
    )
    _ensure_missed(connection)


def _ensure_missed(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MISSED_TABLE} (
            issue_id TEXT PRIMARY KEY,
            identifier TEXT,
            event_at REAL NOT NULL,
            found_at REAL NOT NULL
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
    """Webhook health, or None when neither a listener nor a webhook-mode
    poller ever ran on this database.

    Read-only: it only selects, so `orch status` and the dashboard can use it.
    """
    has_health = _has_table(connection, HEALTH_TABLE)
    has_polling = _has_table(connection, POLLING_TABLE)
    if not has_health and not has_polling:
        return None
    row = (
        connection.execute(
            f"SELECT started_at, last_received_at, last_event FROM {HEALTH_TABLE} "
            "WHERE id = 1"
        ).fetchone()
        if has_health
        else None
    )
    polling = (
        connection.execute(
            f"SELECT since FROM {POLLING_TABLE} WHERE id = 1"
        ).fetchone()
        if has_polling
        else None
    )
    polling_since = epoch.cell(polling[0]) if polling is not None else None
    dirty = (
        int(connection.execute(f"SELECT count(*) FROM {DIRTY_TABLE}").fetchone()[0])
        if _has_table(connection, DIRTY_TABLE)
        else 0
    )
    active = _active_linear(connection)
    epoch.register(connection)
    missed = (
        tuple(
            (str(identifier) if identifier is not None else None, epoch.read(found_at))
            for identifier, found_at in connection.execute(
                f"SELECT identifier, found_at FROM {MISSED_TABLE} "
                f"ORDER BY {epoch.sql('found_at')} DESC, issue_id LIMIT 100"
            )
        )
        if _has_table(connection, MISSED_TABLE)
        else ()
    )
    if row is None:
        return WebhookHealth(None, None, None, dirty, active, polling_since, missed)
    return WebhookHealth(
        started_at=epoch.cell(row[0]),
        last_received_at=epoch.cell(row[1]),
        last_event=str(row[2]) if row[2] is not None else None,
        dirty=dirty,
        active_linear=active,
        polling_since=polling_since,
        missed=missed,
    )


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

    def record_polling(self, now: float) -> None:
        """Note that a poller runs in webhook mode, keeping the first time.

        `orch status` and the dashboard then report a listener that never
        started, instead of showing nothing.
        """
        connection = _connect_existing(self.path)
        try:
            with connection:
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {POLLING_TABLE} (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        since REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    f"INSERT OR IGNORE INTO {POLLING_TABLE} (id, since) VALUES (1, ?)",
                    (now,),
                )
        finally:
            connection.close()

    def seen_at(self, issue_id: str) -> float | None:
        """When a signed, fresh delivery last named this issue."""
        connection = _connect_existing(self.path)
        try:
            if not _has_table(connection, SEEN_TABLE):
                return None
            row = connection.execute(
                f"SELECT received_at FROM {SEEN_TABLE} WHERE issue_id = ?", (issue_id,)
            ).fetchone()
        finally:
            connection.close()
        return epoch.cell(row[0]) if row is not None else None

    def listener_started_at(self) -> float | None:
        connection = _connect_existing(self.path)
        try:
            if not _has_table(connection, HEALTH_TABLE):
                return None
            row = connection.execute(
                f"SELECT started_at FROM {HEALTH_TABLE} WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        return epoch.cell(row[0]) if row is not None else None

    def record_missed(
        self, issue_id: str, identifier: str | None, event_at: float, found_at: float
    ) -> None:
        """Record a missed delivery once per comment: a later read of the
        same miss keeps its first found_at."""
        connection = _connect_existing(self.path)
        try:
            with connection:
                _ensure_missed(connection)
                connection.execute(
                    f"""
                    INSERT INTO {MISSED_TABLE} (issue_id, identifier, event_at, found_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                      identifier = COALESCE(excluded.identifier, {MISSED_TABLE}.identifier),
                      found_at = CASE WHEN excluded.event_at > {MISSED_TABLE}.event_at
                                 THEN excluded.found_at ELSE {MISSED_TABLE}.found_at END,
                      event_at = MAX(excluded.event_at, {MISSED_TABLE}.event_at)
                    """,
                    (issue_id, identifier, event_at, found_at),
                )
                connection.execute(
                    f"DELETE FROM {MISSED_TABLE} WHERE found_at < ?",
                    (found_at - MISSED_KEEP_SECONDS,),
                )
        finally:
            connection.close()

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
        self._received(now)
        return self._record(event, now)

    def _received(self, now: float) -> None:
        """A verified signature proves the path works, whatever comes next."""
        connection = _connect_existing(self.db_path)
        try:
            with connection:
                connection.execute(
                    f"""
                    INSERT INTO {HEALTH_TABLE} (id, started_at, last_received_at, last_event)
                    VALUES (1, ?, ?, '-')
                    ON CONFLICT(id) DO UPDATE SET
                      last_received_at = excluded.last_received_at,
                      last_event = excluded.last_event
                    """,
                    (now, now),
                )
        finally:
            connection.close()

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
                    if issue_id is not None:
                        self._note_project(
                            connection, issue_id, data, event.get("action"), now
                        )
                elif kind == "Comment":
                    issue_id, identifier, reason = self._comment(connection, data)
                else:
                    issue_id, identifier, reason = None, None, "event type"
                if issue_id is not None:
                    connection.execute(
                        f"""
                        INSERT INTO {SEEN_TABLE} (issue_id, received_at) VALUES (?, ?)
                        ON CONFLICT(issue_id) DO UPDATE SET received_at = excluded.received_at
                        """,
                        (issue_id, now),
                    )
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

    def _note_project(
        self,
        connection: sqlite3.Connection,
        issue_id: str,
        data: dict,
        action: object,
        now: float,
    ) -> None:
        """Remember whether this issue is now in a configured project."""
        project = data.get("projectId")
        if (
            action != "remove"
            and isinstance(project, str)
            and project in self.project_ids
        ):
            connection.execute(
                f"""
                INSERT INTO {MEMBER_TABLE} (issue_id, seen_at, project_id) VALUES (?, ?, ?)
                ON CONFLICT(issue_id) DO UPDATE SET
                  seen_at = excluded.seen_at, project_id = excluded.project_id
                """,
                (issue_id, now, project),
            )
        else:
            connection.execute(
                f"DELETE FROM {MEMBER_TABLE} WHERE issue_id = ?", (issue_id,)
            )

    def _comment(
        self,
        connection: sqlite3.Connection,
        data: dict,
    ) -> tuple[str | None, str | None, str | None]:
        """A Comment payload names its issue but not the issue's project.

        Accept it when the factory tracks that issue (tracking implies the
        poller saw it in a configured project), or when it carries an orch:v1
        marker (a brief or go may start tracking) and a signed Issue event
        last placed the issue in a configured project. The marker alone is
        comment text anyone can type and proves no scope. A handoff on an
        issue the listener has not seen is found by the reconcile search.
        """
        issue = data.get("issue")
        issue = issue if isinstance(issue, dict) else {}
        issue_id = _issue_id(data.get("issueId")) or _issue_id(issue.get("id"))
        identifier = _identifier(issue.get("identifier"))
        if issue_id is None:
            return None, identifier, "not on an issue"
        if identifier is not None and _is_tracked(connection, identifier):
            return issue_id, identifier, None
        body = data.get("body")
        if not (isinstance(body, str) and HANDOFF_MARKER in body):
            return issue_id, identifier, "untracked issue"
        if not _in_configured_project(connection, issue_id, self.project_ids):
            return issue_id, identifier, "not a configured project"
        return issue_id, identifier, None


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


def _in_configured_project(
    connection: sqlite3.Connection, issue_id: str, project_ids: frozenset[str]
) -> bool:
    """A signed Issue event placed it in a project that is still configured."""
    row = connection.execute(
        f"SELECT project_id FROM {MEMBER_TABLE} WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    return row is not None and row[0] in project_ids


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


class DeadlineReader(io.RawIOBase):
    """Socket reads that share one deadline for the whole request.

    A per-read socket timeout lets a client that sends a byte just inside
    each timeout hold a worker forever. Here every wait is capped at the
    time left, and a read once the deadline has passed raises TimeoutError.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__()
        self._sock = sock
        self._deadline = deadline
        self._clock = clock

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("request deadline passed")
        self._sock.settimeout(remaining)
        return self._sock.recv_into(buffer)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    app: WebhookApp
    slots: threading.BoundedSemaphore
    deadline_seconds: float
    clock: Callable[[], float]
    busy_logged_at: float | None = None

    def process_request(self, request, client_address) -> None:
        """Start a worker only while one of MAX_WORKERS slots is free.

        A connection beyond that is closed on the accepting thread before
        anything is read from it; Linear retries a failed delivery and the
        poller's reconcile covers anything still missed.
        """
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            now = self.clock()
            if self.busy_logged_at is None or now - self.busy_logged_at >= BUSY_LOG_SECONDS:
                self.busy_logged_at = now
                self.app.log("linear-webhook: refused connection (all workers busy)")
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "factory-linear-webhook"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS

    def setup(self) -> None:
        super().setup()
        # Request line, headers and body all read through one deadline.
        self.rfile.close()
        self.rfile = io.BufferedReader(
            DeadlineReader(
                self.connection,
                deadline=self.server.clock() + self.server.deadline_seconds,
                clock=self.server.clock,
            )
        )

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
        # Reads may have left a near-zero timeout; the reply gets a normal one.
        self.connection.settimeout(self.timeout)
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


def utc_log(line: str) -> None:
    """Print one log line with a UTC ISO 8601 prefix (#79)."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
    print(f"{stamp} {line}", flush=True)


def make_server(
    app: WebhookApp,
    host: str,
    port: int,
    *,
    max_workers: int = MAX_WORKERS,
    deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ThreadingHTTPServer:
    server_class = type(
        "_BoundServer",
        (_Server,),
        {"address_family": _family(host)},
    )
    server = server_class((host, port), _Handler)
    server.app = app
    server.slots = threading.BoundedSemaphore(max_workers)
    server.deadline_seconds = deadline_seconds
    server.clock = clock
    return server


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET
