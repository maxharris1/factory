"""Production GitHub and Linear source for the dispatcher poll loop."""

from __future__ import annotations

import base64
import json
import math
import os
import queue
import re
import socket
import sqlite3
import subprocess
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)
from zoneinfo import ZoneInfo

from lib.dispatcher import (
    CIResult,
    Comment,
    RetryableStateError,
    ReviewActions,
    TicketRef,
    TrackedTicket,
    scrub_detail,
    trusted_bots,
)
from lib.dispatch.types import (
    AlreadyDeferred,
    CIChange,
    LinearState,
    LinearStateRead,
    PullReview,
    ci_fresh,
    pr_ci,
)
from lib.brief_comment import before_factory_md
from lib.dispatch.constants import STOPPED_POLL_SECONDS
from lib.dispatch.errors_text import kept_name, redacted_fragment
from lib.dispatch.cycle_clock import CallKind, CycleClock
from lib.feeder import FEEDER_TABLE
from lib.factory_md import (
    FactoryConfig,
    FactoryContext,
    parse_factory_context,
    parse_factory_md,
    trust_front_matter_changed,
)
from lib.evidence.criteria import canonical_brief
from lib.github_actions import (
    GhResult,
    GitHubAdapterError,
    GitHubReviewActions,
    PULL_FILES_LIMIT,
    PULL_FINGERPRINT_CHUNK,
    LinearReviewActions,
    Runner,
    _is_read,
    _route,
    rate_limited,
    run_gh,
)
from lib.github_app_token import BrokerError, owner_from_args, reads_prefix
from lib.handoff import HandoffError, parse_comment
from lib.hold import FACTORY_MD, glob_matches
from lib.intake_risk import INTAKE_RISK_SCHEMA, INTAKE_RISK_TABLE, stored_risk
from lib.linear_webhook import DirtyRow, missed_delivery
from lib.orch_yml import (
    DEFAULT_LINEAR_HANDOFF_STATES,
    DEFAULT_LINEAR_REOPEN_STATE,
    LINEAR_POLL_SECONDS,
    OrchConfig,
    Project,
    resolve_job_timeout,
)
from lib.pr_title import SOURCE, github_source
from lib.project_rules import FactoryRead

LINEAR_ENDPOINT = "https://api.linear.app/graphql"
TIMEOUT_SECONDS = 60.0
LINEAR_SEARCH_QUERY = """
query FactoryPoll(
  $term: String!,
  $first: Int!,
  $after: String,
  $projectIds: [ID!]!,
  $includeComments: Boolean!
) {
  searchIssues(
    term: $term,
    first: $first,
    after: $after,
    filter: {
      project: { id: { in: $projectIds } }
      state: { type: { nin: ["completed", "canceled"] } }
    },
    includeComments: $includeComments
  ) {
    nodes {
      id
      identifier
      project { id }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip()
LINEAR_ISSUE_QUERY = """
query FactoryIssue($id: String!) {
  issue(id: $id) {
    id
    identifier
    url
    description
    gitBranchName: branchName
    project { id }
    state { id type position }
    team {
      states { nodes { id name type position } }
    }
    comments(first: 250) {
      nodes { id body createdAt editedAt url user { id } }
      pageInfo { hasNextPage }
    }
    labels(first: 50) { nodes { name } pageInfo { hasNextPage } }
  }
}
""".strip()
LINEAR_STATES_QUERY = """
query FactoryStates($team: String!, $numbers: [Float!]!, $first: Int!) {
  issues(
    first: $first,
    filter: { team: { key: { eq: $team } }, number: { in: $numbers } }
  ) {
    nodes { identifier branchName state { name type } canceledAt completedAt }
  }
}
""".strip()
LINEAR_CLOSED_AT = {"canceled": "canceledAt", "completed": "completedAt"}
LINEAR_READY_MUTATION = """
mutation FactoryReadyForReview($issue: String!, $state: String!) {
  issueUpdate(id: $issue, input: {stateId: $state}) { success }
}
""".strip()
BACKOFF_SECONDS = 300.0
SECONDARY_BACKOFF_SECONDS = 60.0
SECONDARY_BACKOFF_MAX_SECONDS = 300.0
RATE_RESET_GRACE_SECONDS = 5.0
RATE_SHED_FRACTION = 0.20
RATE_PAUSE_REMAINING = 50
MAX_POLL_INTERVAL_SECONDS = 120.0
LOW_PRIORITY_PHASES = frozenset({"feed", "github-candidates", "ci-board", "factory-reads"})
GH_CALL_TIMEOUT_SECONDS = 20.0
GH_CALL_ATTEMPTS = 3
GH_RETRY_BACKOFF_SECONDS = 2.0
CACHE_REPLAY_SLACK_SECONDS = 2.0
READ_DEADLINE_SECONDS = 30.0
TRACKED_READ_BUDGET_SECONDS = 120.0
CYCLE_ERROR_DETAILS = 5
HANG_STRIKES = 2
LINEAR_CUTOFF_MARGIN_SECONDS = 1.0
LINEAR_REQUESTS_PER_HOUR = 2_500
LINEAR_COMPLEXITY_PER_HOUR = 3_000_000
LINEAR_SHARE = 0.5
LINEAR_SEARCH_COMPLEXITY = 116
LINEAR_ISSUE_COMPLEXITY = 779
LINEAR_STATES_COMPLEXITY = 250
LINEAR_STATES_PAGE = 100
BRANCH_LOOKUPS_PER_REPO = 10
ORCHESTRATOR = "grok-bot"
RISKY_PATH = re.compile(r"migration|realtime")
RISKY_CHANGE = re.compile(r"realtime|\bpublication\b|replica\s+identity", re.IGNORECASE)
SQL_SUFFIXES = (".sql", ".psql", ".pgsql")
BLOB_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
LINEAR_PROCESS_HEADROOM = 2
HEAD_READ_IDLE_SECONDS = 12 * STOPPED_POLL_SECONDS
FULL_READ_SECONDS = 600.0
LINEAR_SEARCH_MAX_PAGES = 2
LINEAR_TRACKED_RESERVE = 7
_HTTP_STATUS = re.compile(r"(?:\(|\b)HTTP\s+([0-9]{3})\)?")


class PollSourceError(RuntimeError):
    pass


class FactoryParseError(PollSourceError):
    """factory.md was fetched but does not validate."""


class PollCycleError(PollSourceError):
    scrubbed_message: str | None = None

    @classmethod
    def of(cls, errors: list[tuple[str, str, str]]) -> PollCycleError:
        counts = Counter(repo for repo, _, _ in errors)
        sources = ", ".join(
            f"{kept_name(source)} x{count}" for source, count in sorted(counts.items())
        )
        details = [
            _cycle_error_detail(repo, ticket_id, reason)
            for repo, ticket_id, reason in errors[:CYCLE_ERROR_DETAILS]
        ]
        more = len(errors) - CYCLE_ERROR_DETAILS
        if more > 0:
            details.append(f"and {more} more")
        error = cls(
            f"poll cycle recorded {len(errors)} source error(s): "
            f"{sources}; " + "; ".join(details)
        )
        error.scrubbed_message = str(error)
        return error


class GhHttpError(PollSourceError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class _BudgetSpent(PollSourceError):
    pass


class RetryablePollSourceError(PollSourceError, RetryableStateError):
    pass


class QuotaDeferred(RetryablePollSourceError):
    pass


class HangSkipped(RetryablePollSourceError, AlreadyDeferred):
    def __init__(self, service: str) -> None:
        super().__init__(
            f"{service} skipped for the rest of this cycle after {HANG_STRIKES} timed-out reads"
        )


@dataclass(frozen=True)
class RateWindow:
    limit: int
    remaining: int
    reset_epoch: float


Account = str | None
LOGIN: Account = None
PacingReason = Literal["normal", "stretch", "shed", "pause"]
QuotaState = Literal["normal", "shed", "pause"]


@dataclass(frozen=True)
class Pacing:
    interval_seconds: float
    shed_low_priority: bool
    reason: PacingReason


def quota_state(window: RateWindow | None, wall_now: float) -> QuotaState:
    if window is None or window.reset_epoch <= wall_now:
        return "normal"
    if window.remaining <= RATE_PAUSE_REMAINING:
        return "pause"
    return "shed" if window.remaining < RATE_SHED_FRACTION * window.limit else "normal"


def pace(
    window: RateWindow | None,
    wall_now: float,
    base_interval: float,
    recent_billed: int,
    cycle_seconds: float,
) -> Pacing:
    state = quota_state(window, wall_now)
    if state == "pause":
        return Pacing(MAX_POLL_INTERVAL_SECONDS, True, "pause")
    if window is None or window.reset_epoch <= wall_now:
        return Pacing(base_interval, False, "normal")
    spendable = window.remaining - RATE_PAUSE_REMAINING
    needed = (window.reset_epoch - wall_now) * max(recent_billed, 1) / spendable - cycle_seconds
    interval = min(max(needed, base_interval), MAX_POLL_INTERVAL_SECONDS)
    if state == "shed":
        return Pacing(interval, True, "shed")
    return Pacing(interval, False, "stretch" if interval > base_interval else "normal")


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)


LinearTransport = Callable[[str, Mapping[str, str], bytes, float], HTTPResult]
ErrorSink = Callable[[str, str, str], None]
WarningSink = Callable[[str], None]
DeferralSink = Callable[[str], None]
TerminalSink = Callable[[str, str, str, "TrackedTicket | None"], None]


class DirtyQueue(Protocol):
    """The linear_dirty rows the webhook listener writes (lib/linear_webhook)."""

    def pending(self) -> tuple[DirtyRow, ...]: ...

    def ack(self, rows: Sequence[DirtyRow]) -> None: ...

    def record_polling(self, now: float) -> None: ...

    def seen_at(self, issue_id: str) -> float | None: ...

    def listener_started_at(self) -> float | None: ...

    def record_missed(
        self, issue_id: str, identifier: str | None, event_at: float, found_at: float
    ) -> None: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NUMBERED_PART = re.compile(r"[^/ ]*[0-9][^/ ]*")
_BRANCH_TAIL = re.compile(r"([ /](?:heads|compare)/).+")
_REPO_ROUTE = re.compile(r"repos/([^/?]+/[^/?]+)")
_TICKET_ROUTE = re.compile(r"repos/[^/?]+/[^/?]+/(?:issues|pulls)/([0-9]+)")
_WHITESPACE = re.compile(r"\s*")
_TTL_PART = re.compile(r"([0-9]+)([hms])")
_TTL = re.compile(r"(?:[0-9]+[hms])+")
_TTL_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}
_GH_READ_SUBCOMMANDS = (("run", "view"),)
_REVIEW_ACTIONS = tuple(name for name in vars(ReviewActions) if not name.startswith("_"))


class _Cutoff:
    """urllib's timeout is per socket operation, so a reply trickling bytes
    never times out."""

    def __init__(self, timeout: float) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.fired = False
        self.deadline = time.monotonic() + timeout

    def track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            if self.fired:
                _shut(sock)

    def fire(self) -> None:
        with self._lock:
            self.fired = True
            for sock in self._sockets:
                _shut(sock)


def _shut(sock: socket.socket) -> None:
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


class _Tracked:
    def __init__(self, *args: Any, cutoff: _Cutoff, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cutoff = cutoff
        self._create_connection = self._open_tracked_before_tunnel

    def _open_tracked_before_tunnel(
        self,
        address: tuple[str, int],
        _timeout: object = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        """socket.create_connection gives each resolved address the full
        timeout and returns no socket to cut off until one connects."""
        host, port = address
        error = OSError("getaddrinfo returns an empty list")
        for family, kind, proto, _, peer in socket.getaddrinfo(
            host, port, 0, socket.SOCK_STREAM
        ):
            remaining = self._cutoff.deadline - time.monotonic()
            if self._cutoff.fired or remaining <= 0:
                raise TimeoutError
            sock = None
            try:
                sock = socket.socket(family, kind, proto)
                self._cutoff.track(sock)
                sock.settimeout(remaining)
                if source_address:
                    sock.bind(source_address)
                sock.connect(peer)
                return sock
            except OSError as exc:
                if sock is not None:
                    sock.close()
                error = exc
        raise error


class _TrackedHTTP(_Tracked, HTTPConnection):
    pass


class _TrackedHTTPS(_Tracked, HTTPSConnection):
    def connect(self) -> None:
        HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self._tunnel_host or self.host,
            do_handshake_on_connect=False,
        )
        self._cutoff.track(self.sock)
        self.sock.do_handshake()


class _CutoffHTTP(HTTPHandler):
    def __init__(self, cutoff: _Cutoff) -> None:
        super().__init__()
        self._cutoff = cutoff

    def http_open(self, req: Request) -> Any:
        return self.do_open(_TrackedHTTP, req, cutoff=self._cutoff)


class _CutoffHTTPS(HTTPSHandler):
    def __init__(self, cutoff: _Cutoff) -> None:
        super().__init__()
        self._cutoff = cutoff

    def https_open(self, req: Request) -> Any:
        return self.do_open(
            _TrackedHTTPS, req, context=self._context, cutoff=self._cutoff
        )


class _TerminalTicket(RuntimeError):
    pass


def post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> HTTPResult:
    """POST with timeout as the whole request's deadline, not per socket read."""
    cutoff = _Cutoff(timeout)
    timer = threading.Timer(timeout, cutoff.fire)
    timer.daemon = True
    timer.start()
    opener = build_opener(_NoRedirect, _CutoffHTTP(cutoff), _CutoffHTTPS(cutoff))
    try:
        request = Request(url, data=body, method="POST", headers=dict(headers))
        with opener.open(request, timeout=timeout) as response:
            result = HTTPResult(
                response.status,
                response.read().decode("utf-8"),
                dict(response.headers.items()),
            )
    except HTTPError as exc:
        return HTTPResult(exc.code, "", dict(exc.headers.items()))
    except TimeoutError:
        raise
    except URLError as exc:
        if cutoff.fired or isinstance(exc.reason, TimeoutError):
            raise TimeoutError from None
        raise PollSourceError(
            f"Linear request failed: {type(exc).__name__}"
        ) from None
    except (OSError, ValueError, HTTPException) as exc:
        if cutoff.fired:
            raise TimeoutError from None
        raise PollSourceError(
            f"Linear request failed: {type(exc).__name__}"
        ) from None
    finally:
        timer.cancel()
    if cutoff.fired:
        raise TimeoutError
    return result


class IntakeRisks:
    """#195: each ticket's stored risk. Written once from intake and only
    ever raised to high, in the dispatcher database so a restart keeps it."""

    def __init__(self, path: Path | None = None):
        self._connection = sqlite3.connect(
            ":memory:" if path is None else str(path), timeout=30, isolation_level=None
        )
        self._connection.execute(INTAKE_RISK_SCHEMA)

    def get(self, repo: str, ticket_id: str) -> str | None:
        return stored_risk(self._connection, repo, ticket_id)

    def keep(self, repo: str, ticket_id: str, risk: str) -> str:
        self._connection.execute(
            f"""
            INSERT INTO {INTAKE_RISK_TABLE} (repo, ticket_id, risk) VALUES (?, ?, ?)
            ON CONFLICT (repo, ticket_id) DO UPDATE SET risk = 'high'
            WHERE excluded.risk = 'high'
            """,
            (repo.lower(), ticket_id, risk),
        )
        return self.get(repo, ticket_id) or "high"


class FedPulls:
    def __init__(self, path: Path | None = None):
        self._connection = sqlite3.connect(
            ":memory:" if path is None else str(path), timeout=30, isolation_level=None
        )

    def get(self, repo: str, identifier: str) -> str | None:
        return self._read("pr_number", repo, identifier)

    def base(self, repo: str, identifier: str) -> str | None:
        return self._read("base_branch", repo, identifier)

    def ticket_base(self, repo: str, ticket_id: str) -> str | None:
        columns = {row[1] for row in self._connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
        if "target_source" not in columns:
            return None
        row = self._connection.execute(
            f"SELECT base_branch FROM {FEEDER_TABLE} "
            "WHERE repo = ? AND ticket_id = ? AND target_source = 'ticket'",
            (repo.lower(), ticket_id),
        ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def _read(self, column: str, repo: str, identifier: str) -> str | None:
        columns = {row[1] for row in self._connection.execute(f"PRAGMA table_info({FEEDER_TABLE})")}
        if column not in columns:
            return None
        row = self._connection.execute(
            f"SELECT {column} FROM {FEEDER_TABLE} WHERE repo = ? AND issue = ?",
            (repo.lower(), identifier),
        ).fetchone()
        return None if row is None or row[0] is None else str(row[0])


class GhCacheExpiries:
    def __init__(self, path: Path | None = None):
        self._connection = sqlite3.connect(
            ":memory:" if path is None else str(path), timeout=30, isolation_level=None
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS gh_cache_expiry (key TEXT PRIMARY KEY, expiry REAL NOT NULL)"
        )

    def replays(self, key: tuple[str, ...], now: float) -> bool:
        row = self._connection.execute(
            "SELECT expiry FROM gh_cache_expiry WHERE key = ?", (json.dumps(key),)
        ).fetchone()
        return row is not None and row[0] > now

    def keep(self, key: tuple[str, ...], expiry: float, now: float) -> None:
        self._connection.execute("DELETE FROM gh_cache_expiry WHERE expiry <= ?", (now,))
        self._connection.execute(
            "INSERT INTO gh_cache_expiry (key, expiry) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET expiry = excluded.expiry",
            (json.dumps(key), expiry),
        )


_ALL_CHECKS = ""


def _dated(
    stored: Mapping[str, float | None], wanted: Sequence[str], added_at: float | None = None
) -> tuple[CIChange, ...]:
    return tuple(
        CIChange("required-set", at, () if glob == _ALL_CHECKS else (glob,))
        for glob in wanted
        if (at := stored.get(glob, added_at)) is not None
    )


class RequiredGlobs:
    def __init__(self, path: Path | None = None):
        self._connection = sqlite3.connect(
            ":memory:" if path is None else str(path), timeout=30, isolation_level=None
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS ci_required_bases (repo TEXT, base TEXT, PRIMARY KEY (repo, base))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS ci_required_globs "
            "(repo TEXT, base TEXT, glob TEXT, added_at REAL, PRIMARY KEY (repo, base, glob))"
        )

    def observe(
        self, repo: str, base: str, globs: Sequence[str], now: float
    ) -> tuple[CIChange, ...]:
        repo = repo.lower()
        wanted = tuple(dict.fromkeys(globs)) or (_ALL_CHECKS,)
        stored = self._stored(repo, base)
        if stored is not None and set(stored) == set(wanted):
            return _dated(stored, wanted)
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            stored = self._stored(repo, base)
            added_at = None if stored is None else now
            stored = stored or {}
            connection.execute(
                "INSERT OR IGNORE INTO ci_required_bases (repo, base) VALUES (?, ?)", (repo, base)
            )
            connection.executemany(
                "DELETE FROM ci_required_globs WHERE repo = ? AND base = ? AND glob = ?",
                [(repo, base, glob) for glob in stored if glob not in wanted],
            )
            connection.executemany(
                "INSERT INTO ci_required_globs (repo, base, glob, added_at) VALUES (?, ?, ?, ?)",
                [(repo, base, glob, added_at) for glob in wanted if glob not in stored],
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return _dated(stored, wanted, added_at)

    def _stored(self, repo: str, base: str) -> dict[str, float | None] | None:
        if (
            self._connection.execute(
                "SELECT 1 FROM ci_required_bases WHERE repo = ? AND base = ?", (repo, base)
            ).fetchone()
            is None
        ):
            return None
        return dict(
            self._connection.execute(
                "SELECT glob, added_at FROM ci_required_globs WHERE repo = ? AND base = ?",
                (repo, base),
            ).fetchall()
        )


class LastRead(NamedTuple):
    probe_before_read: str | None
    read_at: float
    pull: dict[str, Any]
    comments: tuple[Comment, ...]
    ci: CIResult
    hold_inputs: dict[str, Any]
    reviews: tuple[PullReview, ...] = ()


def reusable(last: LastRead | None, probed: str | None, now: float) -> bool:
    return (
        last is not None
        and probed is not None
        and probed == last.probe_before_read
        and now - last.read_at < FULL_READ_SECONDS
    )


class LivePollSource:
    def __init__(
        self,
        config: OrchConfig,
        *,
        environ: Mapping[str, str] = os.environ,
        gh_runner: Runner = run_gh,
        linear_transport: LinearTransport = post_json,
        timeout_seconds: float = TIMEOUT_SECONDS,
        error_sink: ErrorSink | None = None,
        deferral_sink: DeferralSink | None = None,
        terminal_sink: TerminalSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        gh_call_timeout_seconds: float = GH_CALL_TIMEOUT_SECONDS,
        read_deadline_seconds: float = READ_DEADLINE_SECONDS,
        tracked_read_budget_seconds: float = TRACKED_READ_BUDGET_SECONDS,
        risks: IntakeRisks | None = None,
        fed_pulls: FedPulls | None = None,
        gh_cache: GhCacheExpiries | None = None,
        required_globs: RequiredGlobs | None = None,
    ):
        self.config = config
        self.risks = IntakeRisks() if risks is None else risks
        self.fed_pulls = FedPulls() if fed_pulls is None else fed_pulls
        self.gh_cache = GhCacheExpiries() if gh_cache is None else gh_cache
        self.required_globs = RequiredGlobs() if required_globs is None else required_globs
        self.environ = environ
        self.gh_runner = gh_runner
        self.linear_transport = linear_transport
        self.timeout_seconds = timeout_seconds
        self.error_sink = error_sink
        self.deferral_sink = deferral_sink
        self.terminal_sink = terminal_sink
        self.clock = clock
        self.wall = wall
        self.sleep = sleep
        self.gh_call_timeout_seconds = gh_call_timeout_seconds
        self.read_deadline_seconds = read_deadline_seconds
        self.tracked_read_budget_seconds = tracked_read_budget_seconds
        self._hangs: dict[str, int] = {}
        self._linear_calls = _Abandoning(HANG_STRIKES)
        self._trusted = {
            "github": config.trusted_logins,
            "linear": config.trusted_linear_users,
        }
        self.warning_sink: WarningSink | None = None
        self._factory_cache: dict[str, FactoryConfig] = {}
        self._last_good_factory: dict[str, FactoryConfig] = {}
        self._last_good_context: dict[str, FactoryContext] = {}
        self._factory_warned: dict[str, str] = {}
        self._ticket_cache: dict[tuple[str, str, str], TrackedTicket] = {}
        self.cycle_clock: CycleClock | None = None
        self._write_ticket: str | None = None
        self._head_reads: dict[tuple[str, str, str], tuple[tuple[str, str, str], Any]] = {}
        self._head_read_last_used: dict[tuple[str, str, str], float] = {}
        self._linear_issue_cache: dict[str, dict[str, Any] | None] = {}
        self._etag_cache: dict[tuple[str, ...], tuple[str, str, bool]] = {}
        self._candidate_comments: dict[tuple[str, str], tuple[str, int, tuple[Comment, ...]]] = {}
        self._last_reads: dict[tuple[str, str], LastRead] = {}
        self._linear_branches: dict[str, str] = {}
        self._linear_branch_pulls: dict[str, str | None] = {}
        self._closed_reads: set[tuple[str, str]] = set()
        self._backoffs: dict[Account, float] = {}
        self._rate_windows: dict[Account, RateWindow] = {}
        self._throttled = False
        self._floor_lifted: set[Account] = set()
        self._secondary_waits: dict[Account, float] = {}
        self._secondary_calm_at: dict[Account, float] = {}
        self._linear_backoff_until = 0.0
        self._linear_requests = 0
        self._linear_complexity = 0
        self._linear_candidate_cursor: tuple[str, str] | None = None
        self._linear_tracked_cursor: tuple[str, str] | None = None
        self._linear_states_cursor: tuple[str, int] | None = None
        self._cycle_errors: list[tuple[str, str, str]] = []
        self._cycle_deferrals: dict[str, int] = {}
        self._budget_deadline: float | None = None
        self._budget_spent = False
        self._tracked_turns: dict[tuple[str, str], int] = {}
        self._tracked_turn = 0
        # Linear webhook mode (orch.yml linear_webhook). With no webhook
        # configured none of this is read and every cycle is as before.
        self.linear_dirty: DirtyQueue | None = None
        self.active_tickets: Callable[[], frozenset[tuple[str, str]]] | None = None
        self.unpolled_tickets: Callable[[], frozenset[str]] | None = None
        self._dirty: dict[str, DirtyRow] = {}
        self._dirty_fetched: dict[str, DirtyRow] = {}
        self._active: frozenset[tuple[str, str]] = frozenset()
        self._unpolled: frozenset[str] = frozenset()
        self._polling_recorded = False
        self._webhook_full_cycle = False
        self._last_reconcile: float | None = None
        self._linear_documents: dict[str, tuple[float, dict[str, Any]]] = {}
        # Linear cadence (GitHub may poll every 30 seconds). GitHub is read
        # every cycle; Linear work runs on every `_linear_every`-th cycle, so
        # Linear cycles are at least LINEAR_POLL_SECONDS apart (the sleeps
        # alone add up to that). Its budget is sized for that window, and its counters
        # reset only when a Linear window opens, so a direct read between
        # Linear cycles (fresh_ticket, the Ready for Review move) still
        # counts against the window. At poll_seconds 60 every cycle is a
        # Linear cycle, exactly as before.
        self._linear_every = max(
            1, -(-LINEAR_POLL_SECONDS // max(1, config.poll_seconds))
        )
        self._linear_window_seconds = self._linear_every * config.poll_seconds
        self._cycles_until_linear = 0
        self._linear_due = True
        cleared = config.secret_env_names
        # list_comments and ci_status are polling reads: they get the same
        # short timeout, retries and backoff as every other gh read here.
        self.github = GitHubReviewActions(
            runner=self._adapter_runner,
            timeout_seconds=gh_call_timeout_seconds,
            cleared_variables=cleared,
        )
        self.gh = self.github.gh
        self._read_prefix: tuple[str, ...] = self.gh
        bot = config.poller_reads_app
        if bot is not None:
            try:
                self._read_prefix = reads_prefix(self.gh[:-1], bot, environ)
            except BrokerError as exc:
                raise PollSourceError(f"poller_reads_app {bot}: {exc}") from None

    def review_actions_by_repo(self) -> dict[str, ReviewActions]:
        cleared = self.config.secret_env_names
        actions: dict[str, ReviewActions] = {}
        for project in self.config.projects:
            if not project.linear_project_ids:
                action: ReviewActions = GitHubReviewActions(
                    runner=self._write_runner,
                    timeout_seconds=self.timeout_seconds,
                    cleared_variables=cleared,
                )
            else:
                action = LinearReviewActions(
                    move_ready_for_review=project.linear_ready_for_review,
                    linear_ready=self._move_linear_ready,
                    linear_reopen=self._move_linear_reopen,
                    fresh_ticket=self.fresh_ticket,
                    runner=self._write_runner,
                    timeout_seconds=self.timeout_seconds,
                    cleared_variables=cleared,
                )
            actions[project.repo.lower()] = self._ticket_scoped(action)
        return actions

    def _ticket_scoped(self, actions: ReviewActions) -> ReviewActions:
        for name in _REVIEW_ACTIONS:
            setattr(actions, name, self._in_ticket(getattr(actions, name)))
        return actions

    def _in_ticket(self, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(ticket: TrackedTicket, *args: Any, **kwargs: Any) -> Any:
            outer = self._write_ticket
            self._write_ticket = f"{ticket.repo.lower()}#{ticket.ticket_id}"
            try:
                return method(ticket, *args, **kwargs)
            finally:
                self._write_ticket = outer

        return call

    def begin_cycle(self) -> None:
        self._factory_cache.clear()
        self._ticket_cache.clear()
        self._linear_issue_cache.clear()
        self._linear_branches.clear()
        self._linear_due = self._cycles_until_linear <= 0
        if self._linear_due:
            self._cycles_until_linear = self._linear_every
            self._linear_requests = 0
            self._linear_complexity = 0
        self._cycles_until_linear -= 1
        self._cycle_errors = []
        self._cycle_deferrals = {}
        self._hangs = {}
        if self.config.linear_webhook is not None:
            if self._linear_due:
                self._begin_webhook_cycle()
            else:
                # GitHub-only cycle: the dirty queue is left for the next
                # Linear cycle, untouched and unacked.
                self._dirty = {}
                self._dirty_fetched = {}

    def _begin_webhook_cycle(self) -> None:
        """Snapshot the dirty queue and the tickets with active jobs.

        If either cannot be read, record it and run this cycle the way a
        poller with no webhook would: full search, every tracked read.
        """
        webhook = self.config.linear_webhook
        assert webhook is not None
        self._dirty = {}
        self._dirty_fetched = {}
        self._active = frozenset()
        self._unpolled = frozenset()
        self._webhook_full_cycle = False
        now = self.clock()
        self._linear_documents = {
            key: entry
            for key, entry in self._linear_documents.items()
            if now - entry[0] < 2 * webhook.reconcile_seconds
        }
        try:
            if self.linear_dirty is not None and not self._polling_recorded:
                self.linear_dirty.record_polling(now)
                self._polling_recorded = True
            rows = self.linear_dirty.pending() if self.linear_dirty is not None else ()
            active = (
                self.active_tickets()
                if self.active_tickets is not None
                else frozenset()
            )
            unpolled = (
                self.unpolled_tickets()
                if self.unpolled_tickets is not None
                else frozenset()
            )
        except Exception as exc:
            self._record("linear-webhook", "-", exc)
            self._webhook_full_cycle = True
            return
        self._dirty = {row.issue_id: row for row in rows}
        self._active = frozenset((repo.lower(), ticket) for repo, ticket in active)
        self._unpolled = frozenset(unpolled)

    def _reconcile_due(self) -> bool:
        webhook = self.config.linear_webhook
        assert webhook is not None
        return (
            self._webhook_full_cycle
            or self._last_reconcile is None
            or self.clock() - self._last_reconcile >= webhook.reconcile_seconds
        )

    def end_cycle(self) -> None:
        now = self.clock()
        self._head_read_last_used = {
            key: used
            for key, used in self._head_read_last_used.items()
            if now - used < HEAD_READ_IDLE_SECONDS
        }
        self._head_reads = {
            key: value
            for key, value in self._head_reads.items()
            if key in self._head_read_last_used
        }
        self._last_reads = {
            key: last
            for key, last in self._last_reads.items()
            if now - last.read_at < 2 * FULL_READ_SECONDS
        }
        if self._dirty_fetched and self.linear_dirty is not None:
            fetched = tuple(self._dirty_fetched.values())
            self._dirty_fetched = {}
            try:
                self.linear_dirty.ack(fetched)
            except Exception as exc:
                self._record("linear-webhook", "-", exc)
        if self._cycle_deferrals and self.deferral_sink is not None:
            self.deferral_sink(
                "; ".join(
                    f"{reason} x{count}"
                    for reason, count in sorted(self._cycle_deferrals.items())
                )
            )
        self._cycle_deferrals = {}
        if self._cycle_errors:
            raise PollCycleError.of(self._cycle_errors)

    def github_candidates(
        self,
        label_prefix: str,
        *,
        exclude: frozenset[tuple[str, str]] = frozenset(),
    ) -> tuple[TrackedTicket, ...]:
        """Open PRs that may start tracking; exclude, (repo lowercased, PR
        number) pairs already tracked, are skipped unread (#108: a tracked
        PR is read by tracked_tickets on its own cadence)."""
        tickets: list[TrackedTicket] = []
        for project in self.config.projects:
            if project.linear_project_ids:
                continue
            try:
                factory = self._factory(project)
            except Exception as exc:
                self._record(project.repo, "-", exc)
                continue
            if factory.tickets != "github":
                continue
            endpoint = f"repos/{project.repo}/issues?state=open&per_page=100"
            try:
                pages = self._gh_list(endpoint)
            except Exception as exc:
                self._record(project.repo, "-", exc)
                continue
            listed: set[str] = set()
            for item in pages:
                try:
                    raw_items = _array(item, "GitHub issue page")
                except Exception as exc:
                    self._record(project.repo, "-", exc)
                    continue
                for raw in raw_items:
                    try:
                        issue = _object(raw, "GitHub issue")
                        if "pull_request" not in issue:
                            continue
                        # Issue #59: a ready_starts_review project builds its
                        # PRs outside the queue, so nothing writes the
                        # orch:state/ label before tracking starts. Its open
                        # PRs are candidates on a parseable ready;
                        # Dispatcher._starts_tracking then admits them only
                        # on a trusted builder ready naming the PR head
                        # (admitted_by_ready: a Grok brief/go does not count). Other projects stay label-only and
                        # pay no comment read here.
                        labelled = _has_label(issue.get("labels"), label_prefix)
                        if not labelled and not project.ready_starts_review:
                            continue
                        number = str(
                            _positive_int(issue.get("number"), "issue number")
                        )
                        listed.add(number)
                        if (project.repo.lower(), number) in exclude:
                            continue
                        comments = (
                            None
                            if labelled
                            else self._comments_for(project.repo, number, issue)
                        )
                        if comments is not None and not _has_ready(comments):
                            continue
                        ticket = self._github_ticket(
                            project,
                            factory,
                            number,
                            comments=comments,
                            listed=issue,
                        )
                        if not labelled:
                            # Not cached: the flag describes this
                            # discovery, not the PR snapshot.
                            ticket = replace(ticket, admitted_by_ready=True)
                        tickets.append(ticket)
                    except Exception as exc:
                        if isinstance(exc, _TerminalTicket):
                            continue
                        ticket_id = _ticket_hint(raw)
                        self._record(project.repo, ticket_id, exc)
            self._candidate_comments = {
                key: held
                for key, held in self._candidate_comments.items()
                if key[0] != project.repo.lower() or key[1] in listed
            }
        return tuple(tickets)

    def _comments_for(
        self, repo: str, number: str, issue: dict[str, Any]
    ) -> tuple[Comment, ...]:
        key = (repo.lower(), number)
        stamp = (issue.get("updated_at"), issue.get("comments"))
        valid = isinstance(stamp[0], str) and type(stamp[1]) is int
        held = self._candidate_comments.get(key)
        if valid and held is not None and held[:2] == stamp:
            return held[2]
        comments = self.github.list_comments(repo, number)
        if valid:
            self._candidate_comments[key] = (*stamp, comments)
        return comments

    def linear_candidates(
        self,
        query: str,
        *,
        exclude: frozenset[str] = frozenset(),
        reserve: int = 0,
    ) -> tuple[TrackedTicket, ...]:
        projects = {
            project_id: project
            for project in self.config.projects
            for project_id in project.linear_project_ids
        }
        if not projects or not self._linear_due:
            return ()
        if self.config.linear_webhook is None:
            return self._searched_candidates(projects, query, exclude, reserve)
        found: tuple[TrackedTicket, ...] = ()
        if self._reconcile_due():
            errors = len(self._cycle_errors)
            deferrals = sum(self._cycle_deferrals.values())
            found = self._searched_candidates(projects, query, exclude, reserve)
            if (
                len(self._cycle_errors) == errors
                and sum(self._cycle_deferrals.values()) == deferrals
            ):
                self._last_reconcile = self.clock()
        return found + self._dirty_candidates(projects, exclude, reserve)

    def _dirty_candidates(
        self,
        projects: Mapping[str, Project],
        exclude: frozenset[str],
        reserve: int,
    ) -> tuple[TrackedTicket, ...]:
        """Read each dirty untracked issue directly, oldest event first.

        A tracked issue is left to tracked_tickets, except one the
        dispatcher no longer polls (merged, done, abandoned): nothing reads
        those, with or without a webhook, so its row is acked unread. An
        issue outside the configured projects, completed or canceled (the
        search's own filter), or deleted is dropped without an error and
        still acked.
        """
        tracked_reserve = min(reserve, LINEAR_TRACKED_RESERVE)
        tracked_ids = {
            entry[1].get("id")
            for identifier in exclude
            if (entry := self._linear_documents.get(identifier)) is not None
        }
        unpolled_ids = {
            entry[1].get("id")
            for identifier in self._unpolled
            if (entry := self._linear_documents.get(identifier)) is not None
        }
        tickets: list[TrackedTicket] = []
        for row in sorted(
            self._dirty.values(),
            key=lambda item: (item.received_at, item.issue_id),
        ):
            if row.identifier in exclude or row.issue_id in tracked_ids:
                if row.identifier in self._unpolled or row.issue_id in unpolled_ids:
                    self._dirty_fetched[row.issue_id] = row
                continue
            if row.issue_id in self._linear_issue_cache or (
                row.identifier is not None
                and row.identifier in self._linear_issue_cache
            ):
                continue
            if not self._linear_request_available(
                LINEAR_ISSUE_QUERY,
                reserve=tracked_reserve + LINEAR_PROCESS_HEADROOM,
            ):
                self._defer("Linear poll budget deferred discovery")
                break
            project: Project | None = None
            try:
                complete = self._linear_issue(row.issue_id)
                if complete is None or complete.get("identifier") in exclude:
                    continue
                project = _linear_project(complete, projects)
                if project is None or _linear_closed(complete):
                    continue
                ticket = self._linear_ticket(project, complete)
                if ticket.factory.tickets == "linear":
                    tickets.append(ticket)
            except Exception as exc:
                repo = project.repo if project is not None else "linear"
                self._record(repo, row.identifier or row.issue_id, exc)
        return tuple(tickets)

    def _searched_candidates(
        self,
        projects: Mapping[str, Project],
        query: str,
        exclude: frozenset[str],
        reserve: int,
    ) -> tuple[TrackedTicket, ...]:
        tickets: list[TrackedTicket] = []
        issues: dict[str, dict[str, Any]] = {}
        terms = _linear_terms(query)
        tracked_reserve = min(reserve, LINEAR_TRACKED_RESERVE)
        for index, term in enumerate(terms):
            try:
                found = self._linear_issues(
                    term,
                    project_ids=tuple(projects),
                    reserve=(
                        tracked_reserve
                        + LINEAR_PROCESS_HEADROOM
                        + len(terms)
                        - index
                        - 1
                    ),
                )
            except Exception as exc:
                self._record("linear", "-", exc)
                continue
            for issue in found:
                issue_id = issue.get("id")
                if isinstance(issue_id, str):
                    issues[issue_id] = issue
        candidate_items = sorted(
            issues.values(),
            key=_linear_candidate_key,
        )
        candidate_keys = [_linear_candidate_key(issue) for issue in candidate_items]
        start = (
            bisect_right(candidate_keys, self._linear_candidate_cursor)
            if self._linear_candidate_cursor is not None
            else 0
        )
        ordered_candidates = candidate_items[start:] + candidate_items[:start]
        for issue in ordered_candidates:
            ticket_id = _ticket_hint(issue)
            if ticket_id in exclude:
                continue
            project: Project | None = None
            try:
                project_value = issue.get("project")
                if not isinstance(project_value, dict):
                    continue
                project = projects.get(project_value.get("id"))
                if project is None:
                    continue
                issue_id = issue.get("id")
                if not isinstance(issue_id, str) or not issue_id:
                    raise PollSourceError("Linear search issue has no id")
                if not self._linear_request_available(
                    LINEAR_ISSUE_QUERY,
                    reserve=tracked_reserve + LINEAR_PROCESS_HEADROOM,
                ):
                    self._defer("Linear poll budget deferred discovery")
                    break
                self._linear_candidate_cursor = _linear_candidate_key(issue)
                complete = self._linear_issue(issue_id)
                if complete is None:
                    raise PollSourceError("Linear search issue was not found")
                if not _matches_linear_project(complete, project):
                    raise PollSourceError(
                        "Linear issue is not in its configured project"
                    )
                ticket = self._linear_ticket(project, complete)
                if ticket.factory.tickets == "linear":
                    tickets.append(ticket)
            except Exception as exc:
                repo = project.repo if project is not None else "linear"
                self._record(repo, ticket_id, exc)
        return tuple(tickets)

    def tracked_tickets(
        self,
        tickets: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]:
        found: list[TrackedTicket] = []
        projects = {project.repo.lower(): project for project in self.config.projects}
        urgent = [
            ref for ref in tickets if ref.closed and _closed_key(ref) not in self._closed_reads
        ]
        rest = [ref for ref in tickets if ref not in urgent]
        # Between Linear cycles tracked Linear tickets wait for the next one:
        # no Linear read, and nothing rebuilt from an older read.
        linear_refs = sorted(
            (
                ref
                for ref in rest
                if ref.tracker == "linear" and self._linear_due
            ),
            key=_linear_ref_key,
        )
        linear_keys = [_linear_ref_key(ref) for ref in linear_refs]
        start = (
            bisect_right(linear_keys, self._linear_tracked_cursor)
            if self._linear_tracked_cursor is not None
            else 0
        )
        ordered = (
            urgent
            + linear_refs[start:]
            + linear_refs[:start]
            + sorted(
                (ref for ref in rest if ref.tracker != "linear"),
                key=lambda ref: self._tracked_turns.get((ref.repo.lower(), ref.ticket_id), 0),
            )
        )
        probes: dict[tuple[str, str], str] = {}
        probed: set[tuple[str, str]] = set()
        unprobed: set[str] = set()
        spent: dict[str, float] = {}
        for index, ref in enumerate(ordered):
            project = projects.get(ref.repo.lower())
            if project is None:
                if ref.tracker == "linear":
                    # Its repo left orch.yml: never read, so ack its dirty rows.
                    self._note_unread(ref.ticket_id)
                continue
            repo = ref.repo.lower()
            first_closed_read = ref in urgent
            if self.tracked_read_budget_seconds - spent.get(repo, 0.0) > 0 and (
                repo not in unprobed
                and self._wants_probe(ref)
                and (repo, ref.ticket_id) not in probed
            ):
                started = self.clock()
                try:
                    probes.update(self._pull_fingerprints(project, ordered[index:], probed))
                except Exception:
                    unprobed.add(repo)
                    self._defer(f"GitHub probe failed for {project.repo}, read in full")
                finally:
                    spent[repo] = spent.get(repo, 0.0) + self.clock() - started
            left = self.tracked_read_budget_seconds - spent.get(repo, 0.0)
            if left <= 0 and not first_closed_read:
                self._defer(f"tracked-read budget deferred {project.repo}")
                continue
            started = self.clock()
            self._budget_deadline = started + (
                self.read_deadline_seconds if first_closed_read else left
            )
            self._budget_spent = False
            cursor = self._linear_tracked_cursor
            try:
                ticket = self._tracked_ticket(ref, project, probes.get((repo, ref.ticket_id)))
            except Exception as exc:
                if not isinstance(exc, _TerminalTicket):
                    self._record(ref.repo, ref.ticket_id, exc)
                elif first_closed_read:
                    self._closed_reads.add(_closed_key(ref))
            else:
                if ticket is not None and not self._budget_spent:
                    found.append(ticket)
                    if not (ticket.closed or ticket.merged):
                        self._closed_reads.discard(_closed_key(ref))
                    elif first_closed_read:
                        self._closed_reads.add(_closed_key(ref))
            finally:
                self._budget_deadline = None
                spent[repo] = spent.get(repo, 0.0) + self.clock() - started
                if first_closed_read:
                    self._linear_tracked_cursor = cursor
            if self._budget_spent:
                self._budget_spent = False
                self._defer(f"tracked-read budget deferred {project.repo}")
            self._tracked_turn += 1
            self._tracked_turns[(repo, ref.ticket_id)] = self._tracked_turn
        return tuple(found)

    def _wants_probe(self, ref: TicketRef) -> bool:
        repo = ref.repo.lower()
        return (
            ref.tracker == "github"
            and (repo, ref.ticket_id) in self._last_reads
            and ("github", repo, ref.ticket_id) not in self._ticket_cache
        )

    def _pull_fingerprints(
        self,
        project: Project,
        refs: Sequence[TicketRef],
        probed: set[tuple[str, str]],
    ) -> dict[tuple[str, str], str]:
        repo = project.repo.lower()
        numbers = [
            ref.ticket_id
            for ref in refs
            if ref.repo.lower() == repo
            and (repo, ref.ticket_id) not in probed
            and self._wants_probe(ref)
        ][:PULL_FINGERPRINT_CHUNK]
        probed.update((repo, number) for number in numbers)
        found = self.github.pull_fingerprints(project.repo, numbers)
        return {(repo, number): fingerprint for number, fingerprint in found.items()}

    def closed_pulls(self, refs: tuple[TicketRef, ...]) -> frozenset[tuple[str, str]]:
        projects = {project.repo.lower(): project for project in self.config.projects}
        wanted: dict[str, dict[str, list[str]]] = {}
        lookups: dict[str, int] = {}
        closed: set[tuple[str, str]] = set()
        for ref in refs:
            repo = ref.repo.lower()
            project = projects.get(repo)
            if project is None or ref.tracker not in ("github", "linear"):
                continue
            number = self._probe_number(project.repo, ref)
            if number is not None:
                wanted.setdefault(repo, {}).setdefault(number, []).append(ref.ticket_id)
            elif (
                ref.ticket_id in self._linear_branches
                and ref.ticket_id not in self._linear_branch_pulls
                and lookups.get(repo, 0) < BRANCH_LOOKUPS_PER_REPO
            ):
                lookups[repo] = lookups.get(repo, 0) + 1
                if self._branch_pull_closed(project.repo, ref.ticket_id):
                    closed.add((repo, ref.ticket_id))
        for repo, numbers in wanted.items():
            try:
                states = self.github.pull_states(projects[repo].repo, list(numbers))
            except Exception:
                self._defer(f"GitHub closed probe failed for {projects[repo].repo}")
                continue
            for number, state in states.items():
                for ticket_id in numbers[number]:
                    if state == "OPEN":
                        self._closed_reads.discard((repo, ticket_id))
                    elif state in ("CLOSED", "MERGED"):
                        closed.add((repo, ticket_id))
        return frozenset(closed)

    def _probe_number(self, repo: str, ref: TicketRef) -> str | None:
        if ref.tracker == "github":
            return ref.ticket_id
        return (
            ref.pr_number
            or self.fed_pulls.get(repo, ref.ticket_id)
            or self._linear_branch_pulls.get(ref.ticket_id)
        )

    def _branch_pull_closed(self, repo: str, identifier: str) -> bool:
        try:
            pull = self._branch_pull(repo, self._linear_branches[identifier], fresh=True)
        except Exception:
            self._defer(f"GitHub branch lookup failed for {repo}")
            return False
        self._linear_branch_pulls[identifier] = None if pull is None else str(pull["number"])
        if pull is not None and pull.get("state") == "open":
            self._closed_reads.discard((repo.lower(), identifier))
        return pull is not None and pull.get("state") != "open"

    def linear_states(self, refs: tuple[TicketRef, ...]) -> LinearStateRead | None:
        if not self._linear_due:
            return None
        handoff = {
            project.repo.lower(): frozenset(project.linear_handoff_states)
            for project in self.config.projects
            if project.linear_project_ids
        }
        numbered: dict[tuple[str, int], TicketRef] = {}
        errors: dict[str, str] = {}
        for ref in refs:
            parsed = _team_number(ref.ticket_id)
            if ref.repo.lower() not in handoff:
                continue
            if parsed is None:
                errors[ref.ticket_id] = "not a Linear identifier"
                continue
            numbered[parsed] = ref
        keys = sorted(numbered)
        start = (
            bisect_left(keys, self._linear_states_cursor)
            if self._linear_states_cursor is not None
            else 0
        )
        pages: list[list[tuple[str, int]]] = []
        for key in keys[start:] + keys[:start]:
            if pages and pages[-1][0][0] == key[0] and len(pages[-1]) < LINEAR_STATES_PAGE:
                pages[-1].append(key)
            else:
                pages.append([key])
        reserve = min(len(refs), LINEAR_TRACKED_RESERVE) + LINEAR_PROCESS_HEADROOM
        states: dict[str, LinearState] = {}
        self._linear_states_cursor = None
        for page in pages:
            team = page[0][0]
            if not self._linear_request_available(LINEAR_STATES_QUERY, reserve=reserve):
                self._linear_states_cursor = page[0]
                self._defer("Linear poll budget deferred state reads")
                return LinearStateRead(states, errors)
            try:
                document = self._linear_request(
                    {
                        "query": LINEAR_STATES_QUERY,
                        "variables": {
                            "team": team,
                            "numbers": [number for _, number in page],
                            "first": len(page),
                        },
                    }
                )
                nodes = _object(
                    _object(document.get("data"), "Linear data").get("issues"),
                    "Linear issues",
                ).get("nodes")
                if not isinstance(nodes, list):
                    raise PollSourceError("Linear issues has no nodes")
            except HangSkipped:
                if self._linear_states_cursor is None:
                    self._linear_states_cursor = page[0]
                continue
            except Exception as exc:
                reason = _safe_reason(exc)
                errors.update({numbered[key].ticket_id: reason for key in page})
                continue
            for node in nodes:
                identifier = node.get("identifier") if isinstance(node, dict) else None
                state = node.get("state") if isinstance(node, dict) else None
                parsed = _team_number(identifier) if isinstance(identifier, str) else None
                ref = numbered.get(parsed) if parsed and parsed[0] == team else None
                if ref is None or not isinstance(state, dict):
                    continue
                branch = node.get("branchName")
                if isinstance(branch, str) and branch:
                    self._linear_branches[ref.ticket_id] = branch
                name, kind = state.get("name"), state.get("type")
                if isinstance(name, str) and isinstance(kind, str):
                    states[ref.ticket_id] = LinearState(
                        name,
                        kind,
                        name in handoff[ref.repo.lower()],
                        _iso_time(node.get(LINEAR_CLOSED_AT.get(kind))),
                    )
            for key in page:
                identifier = numbered[key].ticket_id
                if identifier not in states:
                    errors[identifier] = "Linear returned no state"
        return LinearStateRead(states, errors)

    def _require_budget(self, reserve: float = 0.0) -> float:
        if self._budget_deadline is None:
            return math.inf
        left = self._budget_deadline - self.clock()
        if left <= reserve:
            self._budget_spent = True
            raise _BudgetSpent("tracked-read budget spent")
        return left

    def _tracked_ticket(
        self,
        ref: TicketRef,
        project: Project,
        probed: str | None = None,
    ) -> TrackedTicket | None:
        if ref.tracker == "linear":
            cached = ref.ticket_id in self._linear_issue_cache
            issue = None if cached else self._reusable_linear_issue(ref)
            if issue is None:
                if not cached and not self._linear_request_available(
                    LINEAR_ISSUE_QUERY,
                    reserve=LINEAR_PROCESS_HEADROOM,
                ):
                    self._defer("Linear poll budget deferred tracked ticket")
                    return None
                if not cached:
                    self._linear_tracked_cursor = _linear_ref_key(ref)
                issue = self._linear_issue(ref.ticket_id)
            if issue is None:
                raise PollSourceError("Linear issue was not found")
            if not _matches_linear_project(issue, project):
                raise PollSourceError(
                    "Linear issue is not in its configured project"
                )
            return self._linear_ticket(
                project,
                issue,
                tracked=True,
                allow_merged=ref.allow_merged,
                uncached=ref.closed,
                known_pr=ref.pr_number,
            )
        if ref.tracker != "github":
            return None
        return self._github_ticket(
            project,
            self._factory(project),
            ref.ticket_id,
            allow_merged=ref.allow_merged,
            probed=probed,
            uncached=ref.closed,
        )

    def _reusable_linear_issue(self, ref: TicketRef) -> dict[str, Any] | None:
        """Webhook mode: the last direct read of a clean tracked ticket.

        None (read it now) with no webhook, on a full cycle, when there is no
        read yet or it is reconcile_seconds old, when the ticket has a queued
        or running job, or when it is dirty. The ticket is still rebuilt
        from that read every cycle, so its GitHub PR head, CI and PR
        comments stay current.
        """
        webhook = self.config.linear_webhook
        if webhook is None or self._webhook_full_cycle:
            return None
        entry = self._linear_documents.get(ref.ticket_id)
        if entry is None:
            return None
        fetched_at, issue = entry
        if self.clock() - fetched_at >= webhook.reconcile_seconds:
            return None
        if (ref.repo.lower(), ref.ticket_id) in self._active:
            return None
        issue_id = issue.get("id")
        if any(
            row.identifier == ref.ticket_id or row.issue_id == issue_id
            for row in self._dirty.values()
        ):
            return None
        return issue

    def fresh_ticket(self, ref: TicketRef) -> TrackedTicket | None:
        project = next(
            (
                item
                for item in self.config.projects
                if item.repo.lower() == ref.repo.lower()
            ),
            None,
        )
        if project is None:
            return None
        if ref.tracker == "linear":
            if not self._linear_request_available(
                LINEAR_ISSUE_QUERY,
                reserve=1,
            ):
                raise RetryablePollSourceError(
                    "Linear poll budget is exhausted"
                )
            issue = self._linear_issue(ref.ticket_id, fresh=True)
            if issue is None or not _matches_linear_project(issue, project):
                return None
            return self._linear_ticket(project, issue, fresh=True)
        if ref.tracker != "github":
            return None
        return self._github_ticket(
            project,
            self._factory(project),
            ref.ticket_id,
            fresh=True,
            allow_merged=ref.allow_merged,
        )

    def _linear_issues(
        self,
        query: str,
        *,
        project_ids: tuple[str, ...] | None = None,
        reserve: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        issues: list[dict[str, Any]] = []
        after: str | None = None
        pages = 0
        if project_ids is None:
            project_ids = tuple(
                project_id
                for project in self.config.projects
                for project_id in sorted(project.linear_project_ids)
            )
        while True:
            if not self._linear_request_available(
                LINEAR_SEARCH_QUERY,
                reserve=reserve,
            ):
                self._defer("Linear poll budget deferred discovery")
                break
            document = self._linear_request(
                {
                    "query": LINEAR_SEARCH_QUERY,
                    "variables": {
                        "term": query,
                        "first": 50,
                        "after": after,
                        "projectIds": project_ids,
                        "includeComments": True,
                    },
                }
            )
            pages += 1
            connection = _object(
                _object(document.get("data"), "Linear data").get("searchIssues"),
                "Linear searchIssues",
            )
            for item in _array(connection.get("nodes"), "Linear issue nodes"):
                if isinstance(item, dict):
                    issues.append(item)
                else:
                    self._record(
                        "linear",
                        "-",
                        PollSourceError("Linear issue is not an object"),
                    )
            page = _object(connection.get("pageInfo"), "Linear pageInfo")
            if page.get("hasNextPage") is not True:
                break
            if pages >= LINEAR_SEARCH_MAX_PAGES:
                self._defer("Linear search page limit deferred discovery")
                break
            after_value = page.get("endCursor")
            if not isinstance(after_value, str) or not after_value:
                raise PollSourceError("Linear pagination has no endCursor")
            after = after_value
        return tuple(issues)

    def _linear_issue(
        self,
        identifier: str,
        *,
        fresh: bool = False,
    ) -> dict[str, Any] | None:
        if not fresh and identifier in self._linear_issue_cache:
            return self._linear_issue_cache[identifier]
        document = self._linear_request(
            {
                "query": LINEAR_ISSUE_QUERY,
                "variables": {"id": identifier},
            }
        )
        value = _object(document.get("data"), "Linear data").get("issue")
        if value is None:
            self._linear_issue_cache[identifier] = None
            self._note_linear_read(identifier, None)
            return None
        issue = _object(value, "Linear issue")
        self._linear_issue_cache[identifier] = issue
        for key in ("id", "identifier"):
            alias = issue.get(key)
            if isinstance(alias, str) and alias:
                self._linear_issue_cache[alias] = issue
        self._note_linear_read(identifier, issue)
        return issue

    def _note_unread(self, identifier: str) -> None:
        """Webhook mode: ack the dirty rows of a ticket nothing will read."""
        for row in self._dirty.values():
            if row.identifier == identifier:
                self._dirty_fetched[row.issue_id] = row

    def _note_linear_read(
        self,
        requested: str,
        issue: dict[str, Any] | None,
    ) -> None:
        """Webhook mode: keep the read for reuse and mark its dirty rows read.

        A null read is authoritative (deleted, or no longer visible): every
        alias of the older cached read is dropped before its rows are acked,
        so no later cycle can rebuild the ticket from it.
        """
        if self.config.linear_webhook is None:
            return
        keys = {requested}
        document = issue
        if document is None:
            stale = self._linear_documents.get(requested)
            document = stale[1] if stale is not None else None
        if document is not None:
            keys.update(
                alias
                for alias in (document.get("id"), document.get("identifier"))
                if isinstance(alias, str) and alias
            )
        if issue is not None:
            entry = (self.clock(), issue)
            for key in keys:
                self._linear_documents[key] = entry
            self._check_missed(issue)
        else:
            for key in keys:
                self._linear_documents.pop(key, None)
        for row in self._dirty.values():
            if row.issue_id in keys or row.identifier in keys:
                self._dirty_fetched[row.issue_id] = row

    def _check_missed(self, issue: Mapping[str, Any]) -> None:
        """Webhook mode (#79): record a comment on a configured project's
        issue that no delivery covered (lib/linear_webhook.missed_delivery)."""
        if self.linear_dirty is None:
            return
        issue_id = issue.get("id")
        project = issue.get("project")
        configured = {
            project_id
            for item in self.config.projects
            for project_id in item.linear_project_ids
        }
        if not (
            isinstance(issue_id, str)
            and isinstance(project, dict)
            and project.get("id") in configured
        ):
            return
        newest = _newest_comment_at(issue)
        if newest is None:
            return
        now = self.clock()
        try:
            seen = self.linear_dirty.seen_at(issue_id)
            started = self.linear_dirty.listener_started_at()
            if missed_delivery(newest, seen, started, now):
                identifier = issue.get("identifier")
                self.linear_dirty.record_missed(
                    issue_id,
                    identifier if isinstance(identifier, str) else None,
                    newest,
                    now,
                )
        except Exception as exc:
            self._record("linear-webhook", "-", exc)

    def _linear_request(self, document: Mapping[str, object]) -> dict[str, Any]:
        if self.clock() < self._linear_backoff_until:
            raise RetryablePollSourceError(
                "Linear rate-limit backoff is active"
            )
        self._check_hangs("linear")
        query = document.get("query")
        complexity = _linear_request_complexity(query)
        if not self._linear_request_available(query):
            raise RetryablePollSourceError("Linear poll budget is exhausted")
        deadline = min(self.timeout_seconds, self.read_deadline_seconds, self._require_budget())
        self._linear_requests += 1
        self._linear_complexity += complexity
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": self._linear_key(),
            "Content-Type": "application/json",
        }
        cutoff = deadline - min(LINEAR_CUTOFF_MARGIN_SECONDS, deadline / 4)
        try:
            with self._timed("linear"):
                result = self._linear_calls.run(
                    deadline,
                    lambda: self.linear_transport(
                        LINEAR_ENDPOINT, headers, payload, cutoff
                    ),
                )
        except TimeoutError:
            self._require_budget()
            self._hung("linear")
            raise RetryablePollSourceError(
                f"Linear request timed out after {deadline:g}s"
            ) from None
        except PollSourceError:
            raise
        except Exception as exc:
            raise PollSourceError(
                f"Linear request failed: {type(exc).__name__}"
            ) from None
        if result.status == 429:
            self._linear_backoff_until = (
                self.clock() + _retry_after_seconds(result.headers)
            )
            raise RetryablePollSourceError("Linear returned HTTP 429")
        if result.status != 200:
            raise PollSourceError(f"Linear returned HTTP {result.status}")
        parsed = _json_object(result.body, "Linear")
        if parsed.get("errors"):
            if _linear_rate_limited(parsed["errors"]):
                self._linear_backoff_until = (
                    self.clock() + _retry_after_seconds(result.headers)
                )
                raise RetryablePollSourceError(
                    "Linear returned RATELIMITED"
                )
            raise PollSourceError("Linear returned a GraphQL error")
        return parsed

    def _linear_request_available(
        self,
        query: object,
        *,
        reserve: int = 0,
    ) -> bool:
        interval = self._linear_window_seconds
        request_limit = int(
            LINEAR_REQUESTS_PER_HOUR * LINEAR_SHARE
        ) * interval // 3600
        complexity_limit = int(
            LINEAR_COMPLEXITY_PER_HOUR * LINEAR_SHARE
        ) * interval // 3600
        complexity = _linear_request_complexity(query)
        return (
            self._linear_requests + 1 + reserve <= request_limit
            and self._linear_complexity
            + complexity
            + reserve * LINEAR_ISSUE_COMPLEXITY
            <= complexity_limit
        )

    def _move_linear_ready(self, ticket: TrackedTicket) -> None:
        self._move_linear_issue(ticket, ticket.linear_ready_state_id, "Ready for Review")

    def _move_linear_reopen(self, ticket: TrackedTicket) -> None:
        self._move_linear_issue(ticket, ticket.linear_reopen_state_id, "reopen")

    def _move_linear_issue(
        self, ticket: TrackedTicket, state_id: str | None, move: str
    ) -> None:
        if (
            not ticket.linear_ready_for_review
            or ticket.linear_issue_id is None
            or state_id is None
        ):
            raise PollSourceError(f"Linear {move} metadata is unavailable")
        document = self._linear_request(
            {
                "query": LINEAR_READY_MUTATION,
                "variables": {"issue": ticket.linear_issue_id, "state": state_id},
            }
        )
        updated = _object(
            _object(document.get("data"), "Linear data").get("issueUpdate"),
            "Linear issueUpdate",
        )
        if updated.get("success") is not True:
            raise PollSourceError(f"Linear {move} update was not confirmed")

    def _linear_key(self) -> str:
        if self.config.linear is None:
            raise PollSourceError("linear.api_key_env is required for Linear projects")
        key_name = self.config.linear.api_key_env
        key = self.environ.get(key_name)
        if not key:
            raise PollSourceError(f"set {key_name} for Linear polling")
        return key

    def _factory(self, project: Project) -> FactoryConfig:
        identity = project.repo.lower()
        cached = self._factory_cache.get(identity)
        if cached is not None:
            return cached
        if project.factory_ref is None:
            raise PollSourceError(
                f"{project.repo} has no projects[].factory_ref in orch.yml"
            )
        try:
            factory = self._factory_at(project.repo, project.factory_ref)
            fresh = True
        except FactoryParseError as exc:
            factory = self._last_good(identity, exc, self._last_good_factory)
            fresh = False
        if factory.base_branch != project.factory_ref:
            raise PollSourceError(
                f"{project.repo} factory.md base_branch is {factory.base_branch}, "
                f"not configured factory_ref {project.factory_ref}"
            )
        if fresh:
            self._last_good_factory[identity] = factory
            self._factory_warned.pop(identity, None)
        self._factory_cache[identity] = factory
        return factory

    def factory_reads(self) -> tuple[FactoryRead, ...]:
        """Each orch.yml project's factory.md for this cycle (issue #152).

        Through _factory, so it shares the cycle cache and every check the
        poll applies. A PollSourceError keeps its message as the reason;
        any other failure keeps only what _safe_reason does. Nothing is
        recorded as a cycle error: the poll records its own reads.
        """
        reads: list[FactoryRead] = []
        for project in self.config.projects:
            try:
                reads.append(FactoryRead(project.repo, self._factory(project), None))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, PollSourceError) else _safe_reason(exc)
                reads.append(FactoryRead(project.repo, None, reason or type(exc).__name__))
        return tuple(reads)

    def factory_context(self, project: Project) -> FactoryContext:
        """factory.md (front block and prose) from the project's factory_ref.

        For the backlog feeder's brief (issue #85). The same trusted ref and
        base_branch check as _factory; never a PR head.
        """
        if project.factory_ref is None:
            raise PollSourceError(f"{project.repo} has no projects[].factory_ref in orch.yml")
        identity = project.repo.lower()
        text = self._factory_text(project.repo, project.factory_ref)
        parsed, findings = parse_factory_context(text, strict=False)
        if parsed is None:
            errors = [item.message for item in findings if item.severity == "error"]
            context = self._last_good(
                identity,
                FactoryParseError(
                    f"{project.repo} factory.md: {errors[0] if errors else 'invalid factory.md'}"
                ),
                self._last_good_context,
            )
        else:
            context = parsed
        if context.config.base_branch != project.factory_ref:
            raise PollSourceError(
                f"{project.repo} factory.md base_branch is {context.config.base_branch}, "
                f"not configured factory_ref {project.factory_ref}"
            )
        if parsed is not None:
            self._last_good_context[identity] = parsed
            self._factory_warned.pop(identity, None)
        return context

    def _last_good(self, identity: str, exc: FactoryParseError, cache: dict):
        """A factory.md parse failure falls back to the last good parse (#288).

        With none, the failure stands. Each distinct failure warns once.
        """
        last = cache.get(identity)
        if last is None:
            raise exc
        if self._factory_warned.get(identity) != str(exc):
            self._factory_warned[identity] = str(exc)
            if self.warning_sink is not None:
                self.warning_sink(f"{exc}; keeping the last good factory.md")
        return last

    def _factory_text(self, repo: str, branch: str) -> str:
        content = _json_object(
            self._gh_call(
                "-X",
                "GET",
                f"repos/{repo}/contents/factory.md",
                "-f",
                f"ref={branch}",
            ),
            "GitHub factory.md",
        )
        encoded = content.get("content")
        if content.get("encoding") != "base64" or not isinstance(encoded, str):
            raise PollSourceError(f"{repo} factory.md is not base64 content")
        try:
            return base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise PollSourceError(f"{repo} factory.md content is invalid") from exc

    def _factory_at(self, repo: str, branch: str) -> FactoryConfig:
        content = _json_object(
            self._gh_call(
                "-X",
                "GET",
                f"repos/{repo}/contents/factory.md",
                "-f",
                f"ref={branch}",
            ),
            "GitHub factory.md",
        )
        encoded = content.get("content")
        if content.get("encoding") != "base64" or not isinstance(encoded, str):
            raise PollSourceError(f"{repo} factory.md is not base64 content")
        try:
            compact = "".join(encoded.split())
            text = base64.b64decode(compact, validate=True).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise PollSourceError(f"{repo} factory.md content is invalid") from exc
        factory, findings = parse_factory_md(text, strict=False)
        errors = [item.message for item in findings if item.severity == "error"]
        if factory is None:
            detail = errors[0] if errors else "invalid factory.md"
            raise FactoryParseError(f"{repo} factory.md: {detail}")
        return factory

    def _github_ticket(
        self,
        project: Project,
        factory: FactoryConfig,
        number: str,
        *,
        fresh: bool = False,
        allow_merged: bool = False,
        comments: tuple[Comment, ...] | None = None,
        listed: dict[str, Any] | None = None,
        probed: str | None = None,
        uncached: bool = False,
    ) -> TrackedTicket:
        recorded = self.fed_pulls.ticket_base(project.repo, number)
        if recorded is not None:
            factory = replace(factory, base_branch=recorded)
        cache_key = ("github", project.repo.lower(), number)
        cached = None if fresh or uncached else self._ticket_cache.get(cache_key)
        if cached is not None:
            return cached
        read_at = self.clock()
        last = None if fresh or uncached else self._last_reads.get((project.repo.lower(), number))
        if not reusable(last, probed, read_at) or bool(last.hold_inputs) != factory.hold_applies:
            last = None
        if last is None:
            # gh's --cache could serve a body older than the probe, which
            # would then be stored under the probe's fingerprint.
            pull = self._github_pull(
                project.repo, number, fresh=fresh or uncached or probed is not None
            )
        else:
            pull = last.pull
        merged = pull.get("merged") is True
        head = _pr_head(pull, project.repo, number)
        if merged and not fresh:
            # #108: every merge the poll sees ends the ticket; the fresh read
            # is a caller's mid-step read and records nothing (#42 N3).
            if self.terminal_sink is not None:
                self.terminal_sink(
                    project.repo,
                    number,
                    "merged",
                    self._merged_ticket(project, factory, number, pull, head),
                )
            if not allow_merged:
                raise _TerminalTicket()
        if last is not None:
            comments, ci, hold_inputs, reviews = last.comments, last.ci, last.hold_inputs, last.reviews
        else:
            if comments is None:
                comments = (
                    self.github.list_comments(project.repo, number)
                    if listed is None
                    else self._comments_for(project.repo, number, listed)
                )
            hold_inputs = self._hold_inputs(project.repo, number, factory, pull)
            ci = self.github.ci_status(project.repo, head)
            reviews = self._reviews(project.repo, number, factory)
            if not fresh and None not in hold_inputs.values():
                self._last_reads[(project.repo.lower(), number)] = LastRead(
                    probed, read_at, pull, comments, ci, hold_inputs, reviews
                )
        brief = canonical_brief(comments, self._trusted)
        source_issue = github_source(pull, project.repo, number)
        ticket = TrackedTicket(
            **hold_inputs,
            repo=project.repo,
            ticket_id=number,
            factory=factory,
            comments=comments,
            current_head=head,
            ci={
                head: self._gated_ci(
                    project.repo, number, number, factory, pull, ci, hold_inputs, fresh=fresh
                )
            },
            reviews=reviews,
            risk=self._intake_risk(project.repo, number, comments) or "high",
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(pull.get("html_url")),
            git_branch_name=_optional_string(
                _object(pull.get("head"), "GitHub pull request head").get("ref")
            ),
            pr_number=number,
            **_github_brief(brief),
            brief_comment=brief,
            issue_body=(
                None
                if source_issue is None
                else self._source_issue_body(project.repo, number, source_issue, pull, comments)
            ),
            read_issue_body=(
                None if source_issue is None else self._issue_reader(project.repo, number, source_issue)
            ),
            pr_body=_optional_string(pull.get("body")),
            grok_bot=project.grok_bot,
            ready_starts_review=project.ready_starts_review,
            intake_label=project.intake.label if project.intake else None,
            source_issue=source_issue,
            pr_title=_open_title(pull),
            **_pull_facts(pull, fresh=fresh),
        )
        self._ticket_cache[cache_key] = ticket
        return ticket

    def _reviews(self, repo: str, number: str, factory: FactoryConfig) -> tuple[PullReview, ...]:
        return self.github.list_reviews(repo, number) if factory.ship == "handoff" else ()

    def _merged_ticket(
        self,
        project: Project,
        factory: FactoryConfig,
        number: str,
        pull: Mapping[str, Any],
        head: str,
    ) -> TrackedTicket:
        """Enough of a merged PR for its orch:state/merged label: no comment
        or CI read."""
        return TrackedTicket(
            repo=project.repo,
            ticket_id=number,
            factory=factory,
            comments=(),
            current_head=head,
            ci={},
            risk="high",
            ticket_url=_optional_string(pull.get("html_url")),
            pr_number=number,
            grok_bot=project.grok_bot,
            merged=True,
        )

    def _gated_ci(
        self,
        repo: str,
        ticket_id: str,
        number: str,
        factory: FactoryConfig,
        pull: Mapping[str, Any],
        raw: CIResult,
        hold_inputs: Mapping[str, Any],
        *,
        fresh: bool = False,
    ) -> CIResult:
        ci = pr_ci(raw, factory, _pr_base(pull))
        changes = list(
            self.required_globs.observe(
                repo, factory.base_branch, factory.ci_required_checks, self.wall()
            )
        )
        if ci.status != "success" or not factory.ci_force_rerun_paths:
            return ci_fresh(ci, tuple(changes), factory.ci_required_checks)
        unread = CIResult("pending", pending=("CI freshness (unread)",), checks=ci.checks)
        try:
            files = (
                hold_inputs["changed_files"]
                if "changed_files" in hold_inputs
                else self._per_head(
                    "files", repo, number, pull, lambda: self.github.pull_files(repo, number)
                )
            )
            if files is None:
                return unread
            head = _pr_head(pull, repo, number)
            if any(
                glob_matches(pattern, path)
                for pattern in factory.ci_force_rerun_paths
                for path in files
            ):
                at = self._per_head(
                    "committed", repo, number, pull, lambda: self.github.committed_at(repo, head)
                )
                changes.append(CIChange("head-workflow", at))
            if fresh:
                missing = [
                    at
                    for commit, at in self.github.base_workflow_commits(
                        repo, _pr_base(pull) or factory.base_branch, factory.ci_force_rerun_paths
                    )
                    if not self.github.contains_commit(repo, head, commit)
                ]
                if missing:
                    changes.append(CIChange("base-workflow", max(missing)))
        except Exception as exc:
            self._record(repo, ticket_id, exc)
            return unread
        return ci_fresh(ci, tuple(changes), factory.ci_required_checks)

    def _hold_inputs(
        self,
        repo: str,
        number: str,
        factory: FactoryConfig,
        pull: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Issue #86: labels, diff paths and label events for a hold.

        Read only on a repo whose hold applies (FactoryConfig.hold_applies).
        Labels and label events are read every cycle, so a label from the
        last minute is seen. Diff paths change only with the head or base,
        so they are read once per head (#440). A read that fails is recorded
        and left None, which T4 treats as held (no ship) while every other
        row still runs.
        """
        if not factory.hold_applies:
            return {}
        reads = {
            "labels": self.github.pull_labels,
            "changed_files": lambda r, n: self._per_head(
                "files", r, n, pull, lambda: self.github.pull_files(r, n)
            ),
            "label_events": self.github.label_events,
        }
        found: dict[str, Any] = {}
        for name, read in reads.items():
            try:
                found[name] = read(repo, number)
            except Exception as exc:
                self._record(repo, number, exc)
                found[name] = None
        found["factory_md_trust_changed"] = self._factory_md_trust_changed(
            repo, number, pull, found["changed_files"]
        )
        return found

    def _factory_md_trust_changed(
        self,
        repo: str,
        number: str,
        pull: Mapping[str, Any],
        changed_files: Sequence[str] | None,
    ) -> bool | None:
        if changed_files is None:
            return None
        if FACTORY_MD not in changed_files:
            return False

        def read() -> bool:
            base = _object(pull.get("base") or {}, "GitHub pull request base").get("sha")
            if not isinstance(base, str):
                raise PollSourceError(f"{repo}#{number} has no base sha")
            return trust_front_matter_changed(
                self._factory_text_at(repo, base),
                self._factory_text_at(repo, _pr_head(pull, repo, number)),
            )

        try:
            return self._per_head("factory-trust", repo, number, pull, read)
        except Exception as exc:
            self._record(repo, number, exc)
            return None

    def _factory_text_at(self, repo: str, sha: str) -> str | None:
        try:
            return self._factory_text(repo, sha)
        except GhHttpError as exc:
            if exc.status == 404:
                return None
            raise

    def _source_issue_body(
        self,
        repo: str,
        number: str,
        source: str,
        pull: Mapping[str, Any],
        comments: Sequence[Comment],
    ) -> str | None:
        try:
            newest = comments[-1].id if comments else None
            return self._per_head(
                f"issue{source}", repo, number, pull, lambda: self._issue_body(repo, source), newest
            )
        except Exception as exc:
            self._record(repo, number, exc)
            return None

    def _issue_reader(self, repo: str, number: str, source: str) -> Callable[[], str | None]:
        def read() -> str | None:
            try:
                return self._issue_body(repo, source, fresh=True)
            except Exception as exc:
                self._record(repo, number, exc)
                return None

        return read

    def _issue_body(self, repo: str, source: str, *, fresh: bool = False) -> str:
        issue = _json_object(
            self._gh_call(f"repos/{repo}/issues/{source.removeprefix('#')}", fresh=fresh),
            "GitHub issue",
        )
        return _optional_string(issue.get("body")) or ""

    def _per_head(
        self,
        kind: str,
        repo: str,
        number: str,
        pull: Mapping[str, Any],
        read: Callable[[], Any],
        extra: str | None = None,
    ) -> Any:
        key = (kind, repo.lower(), number)
        self._head_read_last_used[key] = self.clock()
        base = _object(pull.get("base") or {}, "GitHub pull request base")
        version = (_pr_head(pull, repo, number), str(base.get("ref")), str(base.get("sha")), extra)
        cached = self._head_reads.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
        value = read()
        if value is not None:
            self._head_reads[key] = (version, value)
        return value

    def _github_pull(
        self,
        repo: str,
        number: str,
        *,
        fresh: bool = False,
    ) -> dict[str, Any]:
        return _json_object(
            self._gh_call(f"repos/{repo}/pulls/{number}", fresh=fresh),
            "GitHub pull request",
        )

    def _linear_ticket(
        self,
        project: Project,
        issue: Mapping[str, Any],
        *,
        fresh: bool = False,
        tracked: bool = False,
        allow_merged: bool = False,
        uncached: bool = False,
        known_pr: str | None = None,
    ) -> TrackedTicket:
        identifier = issue.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            raise PollSourceError("Linear issue has no identifier")
        cache_key = ("linear", project.repo.lower(), identifier)
        cached = None if fresh or uncached else self._ticket_cache.get(cache_key)
        if cached is not None:
            if cached.merged and tracked:
                self._linear_merged(project, identifier, allow_merged)
            return cached
        comments_data = _object(issue.get("comments"), "Linear comments")
        comments_page = _object(
            comments_data.get("pageInfo"),
            "Linear comments pageInfo",
        )
        if comments_page.get("hasNextPage") is True:
            raise PollSourceError("Linear issue has more than 250 comments")
        linear_comments = tuple(
            sorted(
                (
                    comment
                    for item in _array(comments_data.get("nodes"), "Linear comment nodes")
                    if (comment := _linear_comment(item)) is not None
                ),
                key=lambda item: (item.created_at, item.id),
            )
        )
        comments = linear_comments
        brief = canonical_brief(linear_comments, self._trusted)
        head = _latest_head(linear_comments)
        factory = self._factory(project)
        linear_project = issue.get("project")
        factory = replace(
            factory,
            base_branch=self.fed_pulls.base(project.repo, identifier)
            or project.base_for(linear_project.get("id") if isinstance(linear_project, dict) else None),
        )
        ci: dict[str, CIResult] = {}
        pr_title: str | None = None
        pr_number = self._factory_pr_number(
            project.repo,
            identifier,
            issue,
            fresh=fresh or uncached,
            known_pr=known_pr,
        )
        self._linear_branch_pulls[identifier] = pr_number
        pull_facts: dict[str, Any] = {"merged": False}
        pull: Mapping[str, Any] = {}
        reviews: tuple[PullReview, ...] = ()
        if pr_number is not None:
            pull = self._github_pull(project.repo, pr_number, fresh=fresh or uncached)
            # #108: a cached discovery read still carries the merge.
            pull_facts = {**_pull_facts(pull, fresh=fresh), "merged": pull.get("merged") is True}
            if pull_facts["merged"] and tracked:
                self._linear_merged(project, identifier, allow_merged)
            head = _pr_head(pull, project.repo, pr_number)
            pr_title = _open_title(pull)
            raw = self.github.ci_status(project.repo, head)
            ci[head] = self._gated_ci(
                project.repo, identifier, pr_number, factory, pull, raw, {}, fresh=fresh
            )
            github_comments = self.github.list_comments(project.repo, pr_number)
            reviews = self._reviews(project.repo, pr_number, factory)
            comments = tuple(
                sorted(
                    (*linear_comments, *github_comments),
                    key=lambda item: (item.created_at, item.id),
                )
            )
        elif _has_ready(linear_comments):
            self._record(
                project.repo,
                identifier,
                PollSourceError("Linear issue has no linked GitHub PR"),
            )
        hold = (
            self._hold_inputs(project.repo, pr_number, factory, pull)
            if pr_number is not None
            else {}
        )
        ticket = TrackedTicket(
            **hold,
            repo=project.repo,
            ticket_id=identifier,
            factory=factory,
            comments=comments,
            current_head=head,
            ci=ci,
            reviews=reviews,
            risk=self._linear_risk(project.repo, identifier, issue, comments, pr_number, pull),
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(issue.get("url")),
            git_branch_name=_optional_string(issue.get("gitBranchName")),
            pr_number=pr_number,
            brief=_brief_body(brief),
            brief_comment=brief,
            issue_body=_optional_string(issue.get("description")),
            pr_body=_optional_string(pull.get("body")),
            grok_bot=project.grok_bot,
            linear_issue_id=_optional_string(issue.get("id")),
            linear_ready_state_id=_linear_ready_state_id(
                issue, project.linear_handoff_states[0]
            ),
            linear_reopen_state_id=_linear_reopen_state_id(
                issue,
                project.linear_handoff_states[0],
                frozenset(project.linear_handoff_states),
                frozenset(project.linear_rework_states),
            ),
            linear_ready_for_review=project.linear_ready_for_review,
            linear_state_phase=_linear_state_phase(
                issue,
                project.linear_handoff_states[0],
                frozenset(project.linear_rework_states),
            ),
            ready_starts_review=project.ready_starts_review,
            source_issue=identifier if pr_number is not None and SOURCE.fullmatch(identifier) else None,
            pr_title=pr_title,
            **pull_facts,
        )
        self._ticket_cache[cache_key] = ticket
        return ticket

    def _intake_risk(
        self, repo: str, ticket_id: str, comments: Sequence[Comment]
    ) -> str | None:
        """#195: the stored risk, else the intake header's, which is then
        stored. None while no intake header has been read."""
        stored = self.risks.get(repo, ticket_id)
        if stored is not None:
            return stored
        header = _risk_header(comments, self._trusted)
        return None if header is None else self.risks.keep(repo, ticket_id, header)

    def _linear_risk(
        self,
        repo: str,
        identifier: str,
        issue: Mapping[str, Any],
        comments: Sequence[Comment],
        pr_number: str | None,
        pull: Mapping[str, Any],
    ) -> str:
        """#195: the intake risk, raised to high for good by a `migrations`
        label or a migration or realtime change in the linked PR. Labels or
        changes that cannot be read count as high for this poll."""
        intake = self._intake_risk(repo, identifier, comments)
        if intake == "high":
            return "high"
        label = _linear_migrations_label(issue)
        if label is True:
            return self.risks.keep(repo, identifier, "high")
        change = (
            False
            if pr_number is None
            else self._per_head(
                "risk",
                repo,
                pr_number,
                pull,
                lambda: self._risky_change(repo, identifier, pr_number),
            )
        )
        if change is True:
            return self.risks.keep(repo, identifier, "high")
        return "low" if intake == "low" and label is False and change is False else "high"

    def _risky_change(self, repo: str, identifier: str, number: str) -> bool | None:
        """True when the PR touches a migration or realtime path, or its patch
        changes a realtime table; None when that cannot be read or decided."""
        try:
            pages = self._gh_pages(f"repos/{repo}/pulls/{number}/files?per_page=100")
            files = [
                _object(item, "pull request file")
                for page in pages
                for item in _array(page, "pull request files page")
            ]
        except Exception as exc:
            self._record(repo, identifier, exc)
            return None
        if len(files) >= PULL_FILES_LIMIT:
            return None
        undecided = False
        for item in files:
            paths = [item.get(key) for key in ("filename", "previous_filename") if key in item]
            if not paths or not all(isinstance(path, str) and path for path in paths):
                return None
            if any(RISKY_PATH.search(path.lower()) for path in paths):
                return True
            patch = item.get("patch")
            if isinstance(patch, str):
                if any(
                    line[:1] in "+-" and RISKY_CHANGE.search(line)
                    for line in patch.splitlines()
                ):
                    return True
                if paths[0].lower().endswith(SQL_SUFFIXES):
                    statement = _sql_patch_risk(
                        patch, lambda: self._blob_text(repo, identifier, item.get("sha"))
                    )
                    if statement is True:
                        return True
                    undecided = undecided or statement is None
            elif item.get("changes") != 0:
                undecided = True
        return None if undecided else False

    def _blob_text(self, repo: str, identifier: str, sha: object) -> str | None:
        if not isinstance(sha, str) or not BLOB_SHA.fullmatch(sha):
            return None
        try:
            content = _json_object(self._gh_call(f"repos/{repo}/git/blobs/{sha}"), "GitHub blob")
            encoded = content.get("content")
            if content.get("encoding") != "base64" or not isinstance(encoded, str):
                raise PollSourceError(f"{repo} blob {sha} is not base64 content")
            return base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8")
        except Exception as exc:
            self._record(repo, identifier, exc)
            return None

    def _linear_merged(
        self, project: Project, identifier: str, allow_merged: bool
    ) -> None:
        """#108: a tracked Linear ticket whose PR is merged ends, from a new
        read or from the one discovery cached earlier in the cycle."""
        if self.terminal_sink is not None:
            self.terminal_sink(project.repo, identifier, "merged", None)
        if not allow_merged:
            raise _TerminalTicket()

    def _factory_pr_number(
        self,
        repo: str,
        identifier: str,
        issue: Mapping[str, Any],
        *,
        fresh: bool = False,
        known_pr: str | None = None,
    ) -> str | None:
        recorded = self.fed_pulls.get(repo, identifier)
        if recorded is not None:
            return recorded
        branch = _optional_string(issue.get("gitBranchName"))
        factory_pull = None if branch is None else self._branch_pull(repo, branch, fresh=fresh)
        if factory_pull is not None and (known_pr is None or factory_pull.get("state") == "open"):
            return str(factory_pull["number"])
        return known_pr

    def _branch_pull(self, repo: str, branch: str, *, fresh: bool) -> dict[str, Any] | None:
        pulls = _array(
            _json(
                self._gh_call(
                    "-X",
                    "GET",
                    f"repos/{repo}/pulls",
                    "-f",
                    f"head={repo.split('/', 1)[0]}:{branch}",
                    "-f",
                    "state=all",
                    fresh=fresh,
                ),
                "GitHub branch pulls",
            ),
            "GitHub branch pulls",
        )
        return max(
            (item for item in pulls if isinstance(item, dict) and _on_branch(item, repo, branch)),
            key=_open_then_newest,
            default=None,
        )

    def _gh_pages(self, endpoint: str) -> list[object]:
        text = self._gh_call("--paginate", endpoint)
        try:
            pages = list(_json_documents(text))
        except json.JSONDecodeError as exc:
            raise PollSourceError("gh returned malformed JSON") from exc
        if not pages:
            raise PollSourceError("gh returned no JSON")
        return pages

    def _gh_list(self, endpoint: str) -> list[object]:
        body, has_next = self._gh_response(endpoint, _refetch=f"{endpoint}&page=1")
        try:
            pages = [json.loads(body)]
        except json.JSONDecodeError as exc:
            raise PollSourceError("gh returned malformed JSON") from exc
        if has_next:
            pages.extend(self._gh_pages(f"{endpoint}&page=2"))
        return pages

    def _gh_call(self, *args: str, fresh: bool = False) -> str:
        return self._gh_response(*args, fresh=fresh)[0]

    def _gh_response(
        self,
        *args: str,
        fresh: bool = False,
        _unconditional: bool = False,
        _refetch: str | None = None,
    ) -> tuple[str, bool]:
        conditional = "--paginate" not in args and not fresh
        cache_key = tuple(args)
        argv = [*self.gh, "api"]
        if not fresh and not _unconditional:
            argv.extend(("--cache", "60s"))
        if conditional:
            argv.append("--include")
            cached = self._etag_cache.get(cache_key)
            if cached is not None and not _unconditional:
                argv.extend(("-H", f"If-None-Match: {cached[0]}"))
        argv.extend(args)
        result = self._run_gh_read(argv)
        included: tuple[int, dict[str, str], str] | None = None
        if conditional and result.stdout.startswith("HTTP/"):
            included = _included_response(result.stdout)
            if included[0] == 304:
                return self._not_modified(args, _unconditional, _refetch)
        if result.returncode != 0:
            status = (
                included[0]
                if included is not None
                else _status_code(result.stderr)
            )
            route = f"gh api {_route(args)}"
            if status is not None:
                raise GhHttpError(f"{route} returned HTTP {status}", status)
            raise PollSourceError(f"{route} exited {result.returncode}")
        if not conditional:
            return result.stdout, False
        status, headers, body = (
            included if included is not None else _included_response(result.stdout)
        )
        if status == 304:
            return self._not_modified(args, _unconditional, _refetch)
        has_next = 'rel="next"' in headers.get("link", "")
        etag = headers.get("etag")
        if etag is not None:
            self._etag_cache[cache_key] = (etag, body, has_next)
        return body, has_next

    def _not_modified(
        self, args: tuple[str, ...], unconditional: bool, refetch: str | None
    ) -> tuple[str, bool]:
        """Serve a 304 from the ETag cache, or refetch once when it is empty.

        After a restart the in-memory cache is empty, yet gh's own --cache
        can replay a 304 it stored for the previous process's conditional
        request (gh does not key its cache on If-None-Match). Refetch once
        with neither --cache nor If-None-Match, which cannot return 304.
        A list refetches its explicit page=1 URL through --cache instead,
        which gh caches under a separate key from the bare endpoint.
        """
        cached = self._etag_cache.get(tuple(args))
        if cached is not None and not unconditional:
            return cached[1], cached[2]
        if unconditional:
            raise PollSourceError("gh returned 304 without a cached response")
        if refetch is None:
            return self._gh_response(*args, _unconditional=True)
        response = self._gh_response(refetch)
        stored = self._etag_cache.pop((refetch,), None)
        if stored is not None:
            self._etag_cache[tuple(args)] = stored
        return response

    def _adapter_runner(self, argv: Sequence[str], timeout: float) -> GhResult:
        call = list(argv)
        if _is_gh_read(call[call.index("gh") + 1 :] if "gh" in call else []):
            return self._run_gh_read(call)
        return self._write_runner(call, self.timeout_seconds)

    def _write_runner(self, argv: Sequence[str], timeout: float) -> GhResult:
        call = list(argv)
        name = _gh_source(call)
        began = self.clock()
        try:
            result = self.gh_runner(call, timeout)
        except subprocess.TimeoutExpired:
            name = self._write_ticket or _gh_ticket(call)
            raise
        finally:
            if self.cycle_clock is not None:
                self.cycle_clock.add_source(name, self.clock() - began)
        route = _call_route(call)
        kind = "graphql" if route.endswith(" graphql") else "billed"
        self._spend(kind, 1, LOGIN)
        if self.cycle_clock is not None:
            self.cycle_clock.add_call(route, kind)
        return result

    def _account(self, argv: list[str]) -> Account:
        if self.config.poller_reads_app is None:
            return LOGIN
        owner = owner_from_args(argv[argv.index("gh") + 1 :])
        return LOGIN if owner is None else owner.casefold()

    def _run_gh_read(self, argv: list[str]) -> GhResult:
        account = self._account(argv)
        left = self._backoffs.get(account, 0.0) - self.clock()
        if left > 0:
            raise QuotaDeferred(f"GitHub rate-limit backoff is active ({left:.0f}s left)")
        window = self._gate_window(account)
        state = quota_state(window, self.wall())
        if state == "pause":
            if not self._throttled:
                self._throttled = True
                if self.warning_sink is not None:
                    self.warning_sink(
                        f"github throttle: core {window.remaining}/{window.limit} left; "
                        + self._reads_paused(window.reset_epoch - self.wall(), "reset")
                    )
            raise QuotaDeferred(
                f"GitHub core quota at the floor ({window.remaining}/{window.limit} left)"
            )
        clock = self.cycle_clock
        if state == "shed" and clock is not None and clock.open_phase in LOW_PRIORITY_PHASES:
            raise QuotaDeferred(
                f"shedding {clock.open_phase} reads: GitHub core "
                f"{window.remaining}/{window.limit} left"
            )
        self._check_hangs("github")
        route = f"gh api {_route(argv[argv.index('api') + 1 :])}" if "api" in argv else "gh"
        with self._timed(_gh_source(argv)):
            deadline = self.clock() + self.read_deadline_seconds
            attempt = 1
            while True:
                left = deadline - self.clock()
                timeout = min(self.gh_call_timeout_seconds, left, self._require_budget())
                started_wall = self.wall()
                try:
                    result = self.gh_runner(self._for_reads(argv, account), timeout)
                except subprocess.TimeoutExpired:
                    self._require_budget()
                    pause = GH_RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    if attempt >= GH_CALL_ATTEMPTS or deadline - self.clock() <= pause:
                        self._hung("github")
                        raise RetryablePollSourceError(
                            f"{route} timed out after {attempt} attempts"
                        ) from None
                    self._require_budget(pause)
                except OSError as exc:
                    raise PollSourceError(
                        f"{route} could not run: {type(exc).__name__}"
                    ) from exc
                else:
                    self._observe_quota(argv, account, result, started_wall)
                    return result
                self.sleep(pause)
                attempt += 1

    def _for_reads(self, argv: list[str], account: Account) -> list[str]:
        if account is LOGIN:
            return list(argv)
        return [*self._read_prefix, *argv[argv.index("gh") + 1 :]]

    def _observe_quota(
        self, argv: list[str], account: Account, result: GhResult, started_wall: float
    ) -> None:
        route = _call_route(argv)
        status = _status_code(result.stderr)
        headers: dict[str, str] = {}
        if result.stdout.startswith("HTTP/"):
            try:
                status, headers, _ = _included_response(result.stdout)
            except PollSourceError:
                pass
        cacheable = result.returncode == 0 or (
            status is not None and status < 500 and status != 403
        )
        replayed = self._refetch_from_gh_cache(argv, cacheable, started_wall)
        own = _rate_window(
            headers.get("x-ratelimit-limit"),
            headers.get("x-ratelimit-remaining"),
            headers.get("x-ratelimit-reset"),
        )
        kind = _call_kind(route, headers, status, started_wall, replayed)
        requests = _request_count(argv, result)
        if own is not None:
            self._rate_windows[account] = _merged_window(self._rate_windows.get(account), own)
            self._floor_lifted.discard(account)
        if self.cycle_clock is not None:
            self.cycle_clock.add_call(route, kind, requests)
            if own is not None:
                window = self._rate_windows[account]
                self.cycle_clock.note_core(window.remaining, window.limit, window.reset_epoch)
        if own is None:
            self._spend(kind, requests, account)
        if result.returncode == 0 or not rate_limited(status, result.stdout, result.stderr):
            self._throttled = False
            return
        seconds, basis = self._quota_wait(
            headers.get("retry-after"), account, f"{result.stdout}\n{result.stderr}"
        )
        self._backoffs[account] = max(self._backoffs.get(account, 0.0), self.clock() + seconds)
        if basis == "retry-after":
            self._floor_lifted.add(account)
        else:
            self._floor_lifted.discard(account)
        if self._throttled:
            return
        self._throttled = True
        if self.warning_sink is None:
            return
        window = self._live_window(account)
        core = "" if window is None else f"; core {window.remaining}/{window.limit} left"
        http = "" if status is None else f"HTTP {status} "
        self.warning_sink(
            f"github throttle: {http}rate limit on {route}{core}; "
            + self._reads_paused(seconds, basis)
        )

    def _reads_paused(self, seconds: float, basis: str) -> str:
        minutes, secs = divmod(round(seconds), 60)
        until = datetime.fromtimestamp(
            self.wall() + seconds, ZoneInfo("America/New_York")
        ).strftime("%H:%M:%S")
        return f"github reads paused {minutes}m{secs:02d}s until {until} ET ({basis})"

    def _refetch_from_gh_cache(
        self, argv: Sequence[str], cacheable: bool, started_wall: float
    ) -> bool:
        cached = _gh_cache_key(argv)
        if cached is None:
            return False
        key, ttl = cached
        if self.gh_cache.replays(key, started_wall):
            return True
        if cacheable:
            self.gh_cache.keep(key, started_wall + ttl, started_wall)
        return False

    def _quota_wait(
        self, retry_after: str | None, account: Account, text: str
    ) -> tuple[float, str]:
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError):
            seconds = math.nan
        if 0 <= seconds < math.inf:
            return seconds, "retry-after"
        basis = "reset"
        window = self._live_window(account)
        if window is None:
            self._probe_rate_window(account)
            basis = "probe"
            window = self._live_window(account)
        if window is not None and window.remaining <= RATE_PAUSE_REMAINING:
            return window.reset_epoch - self.wall() + RATE_RESET_GRACE_SECONDS, basis
        if window is None and "secondary rate limit" not in text.lower():
            return BACKOFF_SECONDS, "default"
        now = self.clock()
        if now < self._secondary_calm_at.get(account, 0.0):
            wait = min(2 * self._secondary_waits[account], SECONDARY_BACKOFF_MAX_SECONDS)
        else:
            wait = SECONDARY_BACKOFF_SECONDS
        self._secondary_waits[account] = wait
        self._secondary_calm_at[account] = now + wait + SECONDARY_BACKOFF_MAX_SECONDS
        return wait, "secondary"

    def _spend(self, kind: CallKind, requests: int, account: Account) -> None:
        window = self._live_window(account)
        if kind == "billed" and window is not None:
            self._rate_windows[account] = replace(
                window, remaining=max(window.remaining - requests, 0)
            )

    def pacing(self, base_interval: float, recent_billed: int, cycle_seconds: float) -> Pacing:
        windows = [self._gate_window(account) for account in self._rate_windows] or [None]
        paces = [
            pace(window, self.wall(), base_interval, recent_billed, cycle_seconds)
            for window in windows
        ]
        return max(
            paces, key=lambda p: (p.interval_seconds, p.shed_low_priority, p.reason == "pause")
        )

    def _gate_window(self, account: Account) -> RateWindow | None:
        window = self._rate_windows.get(account)
        if account in self._floor_lifted and quota_state(window, self.wall()) == "pause":
            return None
        return window

    def _live_window(self, account: Account) -> RateWindow | None:
        window = self._rate_windows.get(account)
        return window if window is not None and window.reset_epoch > self.wall() else None

    def _probe_rate_window(self, account: Account) -> None:
        argv = self._for_reads([*self.gh, "api", "rate_limit"], account)
        if account is not LOGIN:
            # rate_limit names no owner, so the helper takes it from GH_REPO.
            argv.insert(len(self.gh) - 1, f"GH_REPO={account}/{account}")
        try:
            result = self.gh_runner(argv, self.gh_call_timeout_seconds)
        except (subprocess.TimeoutExpired, OSError):
            return
        if result.returncode != 0:
            return
        try:
            core = json.loads(result.stdout)["resources"]["core"]
        except (ValueError, KeyError, TypeError):
            return
        if not isinstance(core, dict):
            return
        window = _rate_window(core.get("limit"), core.get("remaining"), core.get("reset"))
        if window is not None:
            self._rate_windows[account] = _merged_window(self._rate_windows.get(account), window)

    def _timed(self, name: str) -> AbstractContextManager[None]:
        return nullcontext() if self.cycle_clock is None else self.cycle_clock.source(name)

    def _check_hangs(self, service: str) -> None:
        if self._hangs.get(service, 0) >= HANG_STRIKES:
            self._defer(f"{service} hang-skip")
            raise HangSkipped(service)

    def _hung(self, service: str) -> None:
        self._hangs[service] = self._hangs.get(service, 0) + 1
        if self._hangs[service] != HANG_STRIKES:
            return
        self._error(service, "-", HangSkipped(service))
        if self.warning_sink is not None:
            self.warning_sink(
                f"{service} skipped for the rest of this cycle after "
                f"{HANG_STRIKES} timed-out reads; read again next cycle"
            )

    def _defer(self, reason: str) -> None:
        self._cycle_deferrals[reason] = self._cycle_deferrals.get(reason, 0) + 1
        if self.cycle_clock is not None:
            self.cycle_clock.add_deferral(reason)

    def _record(self, repo: str, ticket_id: str, exc: Exception) -> None:
        if isinstance(exc, (_BudgetSpent, HangSkipped)):
            return
        if isinstance(exc, QuotaDeferred):
            self._defer("github quota")
            return
        self._error(repo, ticket_id, exc)

    def _error(self, repo: str, ticket_id: str, exc: Exception) -> None:
        reason = _safe_reason(exc)
        for name in self.config.secret_env_names:
            value = self.environ.get(name)
            if value:
                reason = reason.replace(value, "<secret>")
        self._cycle_errors.append((repo, ticket_id, reason))
        if self.error_sink is not None:
            self.error_sink(repo, ticket_id, reason)


class _Abandoning:
    def __init__(self, max_running: int) -> None:
        self.max_running = max_running
        self._running = 0
        self._lock = threading.Lock()

    def run(self, seconds: float, call: Callable[[], HTTPResult]) -> HTTPResult:
        with self._lock:
            if self._running >= self.max_running:
                raise RetryablePollSourceError(
                    f"{self._running} timed-out Linear requests are still running"
                )
            self._running += 1
        done: queue.SimpleQueue[tuple[bool, Any]] = queue.SimpleQueue()

        def target() -> None:
            try:
                outcome: tuple[bool, Any] = (True, call())
            except BaseException as exc:
                outcome = (False, exc)
            with self._lock:
                self._running -= 1
            done.put(outcome)

        threading.Thread(target=target, daemon=True).start()
        try:
            ok, value = done.get(timeout=seconds)
        except queue.Empty:
            raise TimeoutError from None
        if ok:
            return value
        raise value


def _is_graphql_query(args: Sequence[str]) -> bool:
    if "graphql" not in args or "--input" in args:
        return False
    fields: list[str] = []
    for index, arg in enumerate(args):
        if arg in ("-f", "-F", "--field", "--raw-field"):
            fields.append(args[index + 1] if index + 1 < len(args) else "")
        elif arg.startswith(("--field=", "--raw-field=")):
            fields.append(arg.partition("=")[2])
        elif arg.startswith(("-f", "-F")):
            fields.append(arg[2:])
    documents = [value[len("query="):].lstrip() for value in fields if value.startswith("query=")]
    return len(documents) == 1 and documents[0].startswith(("query", "{"))


def _is_gh_read(args: Sequence[str]) -> bool:
    if tuple(args[:1]) == ("api",):
        return _is_read(args[1:]) or _is_graphql_query(args[1:])
    return tuple(args[:2]) in _GH_READ_SUBCOMMANDS


def _gh_source(argv: Sequence[str]) -> str:
    if "graphql" in argv:
        return "github-graphql"
    for index, part in enumerate(argv):
        match = _REPO_ROUTE.match(part)
        if match:
            return match.group(1).lower()
        if part == "--repo" and index + 1 < len(argv):
            return argv[index + 1].lower()
    return "github"


def _call_route(argv: Sequence[str]) -> str:
    if "api" in argv:
        route = _route(argv[argv.index("api") + 1 :])
        return _NUMBERED_PART.sub("<value>", _BRANCH_TAIL.sub(r"\1<value>", route))
    at = argv.index("gh") if "gh" in argv else -1
    return " ".join(["gh", *argv[at + 1 : at + 3]])


def _call_kind(
    route: str,
    headers: Mapping[str, str],
    status: int | None,
    started_wall: float,
    replayed: bool,
) -> CallKind:
    if route.endswith(" graphql"):
        return "graphql"
    try:
        sent = parsedate_to_datetime(headers["date"]).timestamp()
        replayed = sent < started_wall - CACHE_REPLAY_SLACK_SECONDS
    except (KeyError, TypeError, ValueError):
        pass
    if replayed:
        return "cached"
    return "304" if status == 304 else "billed"


def _json_documents(text: str) -> Iterator[object]:
    decoder = json.JSONDecoder()
    index = _WHITESPACE.match(text).end()
    while index < len(text):
        document, index = decoder.raw_decode(text, index)
        yield document
        index = _WHITESPACE.match(text, index).end()


def _request_count(argv: Sequence[str], result: GhResult) -> int:
    if "--paginate" not in argv:
        return 1
    pages = 0
    try:
        for _ in _json_documents(result.stdout):
            pages += 1
    except json.JSONDecodeError:
        pass
    return max(1, pages + (result.returncode != 0))


def _gh_cache_key(argv: Sequence[str]) -> tuple[tuple[str, ...], float] | None:
    """gh keys its cache on the request alone, ignoring --include and If-None-Match."""
    if "--cache" not in argv:
        return None
    key: list[str] = []
    ttl = None
    index = 0
    while index < len(argv):
        part = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else ""
        if part == "--cache":
            ttl = _cache_ttl(value)
            index += 2
        elif part == "-H" and value.lower().startswith("if-none-match:"):
            index += 2
        else:
            if part != "--include":
                key.append(part)
            index += 1
    return None if ttl is None else (tuple(key), ttl)


def _cache_ttl(text: str) -> float | None:
    if not _TTL.fullmatch(text):
        return None
    return sum(int(amount) * _TTL_UNIT_SECONDS[unit] for amount, unit in _TTL_PART.findall(text))


def _gh_ticket(argv: Sequence[str]) -> str:
    source = _gh_source(argv)
    for part in argv:
        match = _TICKET_ROUTE.match(part)
        if match:
            return f"{source}#{match.group(1)}"
    return source


def _json(text: str, source: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PollSourceError(f"{source} returned malformed JSON") from exc


def _json_object(text: str, source: str) -> dict[str, Any]:
    return _object(_json(text, source), source)


def _open_then_newest(pull: Mapping[str, Any]) -> tuple[bool, int]:
    return pull.get("state") == "open", _positive_int(pull.get("number"), "pull request number")


def _on_branch(pull: Mapping[str, Any], repo: str, branch: str) -> bool:
    head = pull.get("head")
    head = head if isinstance(head, dict) else {}
    head_repo = head.get("repo")
    full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    return head.get("ref", branch) == branch and str(full_name or repo).lower() == repo.lower()


def _rate_window(limit: object, remaining: object, reset: object) -> RateWindow | None:
    try:
        return RateWindow(int(limit), int(remaining), float(int(reset)))
    except (TypeError, ValueError):
        return None


def _merged_window(old: RateWindow | None, new: RateWindow) -> RateWindow:
    if (
        old is None
        or new.reset_epoch > old.reset_epoch
        or (new.reset_epoch == old.reset_epoch and new.remaining < old.remaining)
    ):
        return new
    return old


def _included_response(text: str) -> tuple[int, dict[str, str], str]:
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("HTTP/"):
        return 200, {}, text
    raw_headers, separator, body = normalized.partition("\n\n")
    if not separator:
        raise PollSourceError("gh returned malformed response headers")
    lines = raw_headers.splitlines()
    status_match = re.match(r"^HTTP/\S+\s+([0-9]{3})\b", lines[0])
    if status_match is None:
        raise PollSourceError("gh returned malformed HTTP status")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return int(status_match.group(1)), headers, body


def _object(value: object, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PollSourceError(f"{source} is not an object")
    return value


def _array(value: object, source: str) -> list[object]:
    if not isinstance(value, list):
        raise PollSourceError(f"{source} is not a list")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PollSourceError(f"{field} is not a positive integer")
    return value


def _ticket_hint(value: object) -> str:
    if not isinstance(value, dict):
        return "-"
    for key in ("identifier", "number", "id"):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
            return str(candidate)
    return "-"


def _linear_terms(query: str) -> tuple[str, ...]:
    terms = tuple(
        term.strip()
        for term in query.split(" OR ")
        if term.strip()
    )
    return terms if terms else (query,)


def _linear_candidate_key(issue: Mapping[str, Any]) -> tuple[str, str]:
    return (_ticket_hint(issue), str(issue.get("id", "")))


def _closed_key(ref: TicketRef) -> tuple[str, str]:
    return ref.repo.lower(), ref.ticket_id


def _linear_ref_key(ref: TicketRef) -> tuple[str, str]:
    return (ref.repo.lower(), ref.ticket_id)


def _linear_request_complexity(query: object) -> int:
    if query == LINEAR_SEARCH_QUERY:
        return LINEAR_SEARCH_COMPLEXITY
    if query == LINEAR_STATES_QUERY:
        return LINEAR_STATES_COMPLEXITY
    return LINEAR_ISSUE_COMPLEXITY


def _team_number(identifier: str) -> tuple[str, int] | None:
    team, _, number = identifier.rpartition("-")
    if not team or not number.isdigit():
        return None
    return team, int(number)


def _linear_rate_limited(errors: object) -> bool:
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        code = (
            extensions.get("code")
            if isinstance(extensions, dict)
            else error.get("code")
        )
        if code == "RATELIMITED":
            return True
    return False


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    value = next(
        (
            header_value
            for name, header_value in headers.items()
            if name.lower() == "retry-after"
        ),
        None,
    )
    if value is None:
        return BACKOFF_SECONDS
    try:
        return max(0.0, float(value))
    except ValueError:
        return BACKOFF_SECONDS


def _status_code(value: str) -> int | None:
    found = _HTTP_STATUS.search(value)
    return int(found.group(1)) if found is not None else None


def _cycle_error_detail(repo: str, ticket_id: str, reason: str) -> str:
    where = (
        kept_name(repo) if ticket_id == "-" else f"{kept_name(repo)} ticket {kept_name(ticket_id)}"
    )
    return f"{where} {redacted_fragment(reason)}".rstrip()


def _safe_reason(exc: Exception) -> str:
    """The poll_errors text for a source error. A PollSourceError's message
    is built here from safe parts; any other error keeps its kind and gets
    its message scrubbed."""
    if isinstance(exc, PollSourceError):
        return f"PollSourceError:{str(exc)}"
    text = exc.safe_detail if isinstance(exc, GitHubAdapterError) else str(exc)
    status = _status_code(text)
    kind = type(exc).__name__ if status is None else f"{type(exc).__name__}:HTTP-{status}"
    detail = scrub_detail(text)
    return f"{kind}: {detail}" if detail else kind


def _open_title(pull: Mapping[str, Any]) -> str | None:
    """An open PR's title; None for a closed or merged one, which is left alone."""
    title = pull.get("title")
    return title if pull.get("state") == "open" and isinstance(title, str) else None


def _has_label(value: object, prefix: str) -> bool:
    return any(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].startswith(prefix)
        for item in _array(value, "GitHub labels")
    )


def _matches_linear_project(issue: Mapping[str, Any], project: Project) -> bool:
    if not project.linear_project_ids:
        return False
    value = _object(issue.get("project"), "Linear issue project")
    return value.get("id") in project.linear_project_ids


def _linear_project(
    issue: Mapping[str, Any],
    projects: Mapping[str, Project],
) -> Project | None:
    value = issue.get("project")
    if not isinstance(value, dict):
        return None
    project_id = value.get("id")
    return projects.get(project_id) if isinstance(project_id, str) else None


def _linear_closed(issue: Mapping[str, Any]) -> bool:
    state = issue.get("state")
    return isinstance(state, dict) and state.get("type") in ("completed", "canceled")


def _pull_facts(pull: Mapping[str, Any], *, fresh: bool) -> dict[str, Any]:
    merged = pull.get("merged") is True
    mergeable = pull.get("mergeable")
    sizes = (pull.get("additions"), pull.get("deletions"))
    closed = pull.get("state") == "closed" and not merged
    return {
        "merged": merged and fresh,
        "mergeable": mergeable if isinstance(mergeable, bool) else None,
        "mergeable_state": _optional_string(pull.get("mergeable_state")),
        "closed": closed,
        "closed_at": _iso_time(pull.get("closed_at")) if closed else None,
        "draft": pull["draft"] if isinstance(pull.get("draft"), bool) else None,
        "changed_lines": sum(sizes) if all(type(size) is int for size in sizes) else None,
        "pr_base": _pr_base(pull),
        "pr_commits": pull["commits"] if type(pull.get("commits")) is int else None,
    }


def _pr_base(pull: Mapping[str, Any]) -> str | None:
    base = pull.get("base")
    return _optional_string(base.get("ref")) if isinstance(base, Mapping) else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _linear_team_states(issue: Mapping[str, Any]) -> list[dict[str, Any]]:
    team = issue.get("team")
    if team is None:
        return []
    states = _object(_object(team, "Linear team").get("states"), "Linear states")
    nodes = _array(states.get("nodes"), "Linear state nodes")
    return [_object(raw, "Linear state") for raw in nodes]


def _linear_team_state(issue: Mapping[str, Any], key: str, value: object) -> dict[str, Any] | None:
    return next((state for state in _linear_team_states(issue) if state.get(key) == value), None)


def _linear_ready_state(
    issue: Mapping[str, Any], name: str = DEFAULT_LINEAR_HANDOFF_STATES[0]
) -> dict[str, Any] | None:
    return _linear_team_state(issue, "name", name)


def _linear_ready_state_id(
    issue: Mapping[str, Any], name: str = DEFAULT_LINEAR_HANDOFF_STATES[0]
) -> str | None:
    state = _linear_ready_state(issue, name)
    return _optional_string(state.get("id")) if state is not None else None


def _linear_reopen_state_id(
    issue: Mapping[str, Any],
    ready_name: str = DEFAULT_LINEAR_HANDOFF_STATES[0],
    handoff_names: frozenset[str] = frozenset(DEFAULT_LINEAR_HANDOFF_STATES),
    rework: frozenset[str] = frozenset(),
) -> str | None:
    if DEFAULT_LINEAR_REOPEN_STATE not in handoff_names:
        named = _linear_team_state(issue, "name", DEFAULT_LINEAR_REOPEN_STATE)
        if named is not None and _linear_state_phase(
            {**issue, "state": named}, ready_name, rework
        ) in ("before-review", "rework", "no-ready-state"):
            return _optional_string(named.get("id"))
    ready = _linear_ready_state(issue, ready_name)
    if ready is None or not _is_position(ready.get("position")):
        return None
    before = [
        state
        for state in _linear_team_states(issue)
        if state.get("type") == "started"
        and state.get("name") not in handoff_names
        and _is_position(state.get("position"))
        and state["position"] < ready["position"]
    ]
    if not before:
        return None
    return _optional_string(min(before, key=lambda state: state["position"]).get("id"))


def _linear_state_phase(
    issue: Mapping[str, Any],
    ready_name: str = DEFAULT_LINEAR_HANDOFF_STATES[0],
    rework: frozenset[str] = frozenset(),
) -> str | None:
    state = issue.get("state")
    if not isinstance(state, dict):
        return None
    state_id = _optional_string(state.get("id"))
    kind = state.get("type")
    if state_id is None or not isinstance(kind, str):
        return None
    if not isinstance(issue.get("team"), dict):
        return None
    ready = _linear_ready_state(issue, ready_name)
    if ready is not None and ready.get("id") == state_id:
        return "ready-for-review"
    if kind in ("completed", "canceled"):
        return "moved-on"
    if kind not in ("backlog", "triage", "unstarted", "started"):
        return None
    if ready is None:
        return "no-ready-state"
    current = _linear_team_state(issue, "id", state_id)
    if current is not None and current.get("name") in rework:
        return "rework"
    if kind != "started" or ready.get("type") != "started":
        return "before-review"
    here, there = state.get("position"), ready.get("position")
    if not _is_position(here) or not _is_position(there):
        return None
    return "moved-on" if here > there else "before-review"


def _is_position(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _linear_comment(value: object) -> Comment | None:
    item = _object(value, "Linear comment")
    comment_id = item.get("id")
    body = item.get("body")
    created_at = item.get("createdAt")
    user = item.get("user")
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    author = f"linear:{user_id}" if isinstance(user_id, str) else None
    required = (comment_id, body, created_at, author)
    if not all(isinstance(field, str) and field for field in required):
        raise PollSourceError("Linear comment is missing a required field")
    edited_at = item.get("editedAt")
    url = item.get("url")
    return Comment(
        id=comment_id,
        author=author,
        body=body,
        created_at=_linear_time(created_at, "createdAt"),
        url=url if isinstance(url, str) and url else None,
        edited_at=_linear_time(edited_at, "editedAt") if edited_at is not None else None,
    )


def _linear_time(value: object, field: str) -> float:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise PollSourceError(f"Linear comment {field} is not ISO-8601") from exc
    if parsed is None or parsed.tzinfo is None:
        raise PollSourceError(f"Linear comment {field} is not ISO-8601")
    return parsed.timestamp()


def _iso_time(value: object) -> float | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return parsed.timestamp() if parsed is not None and parsed.tzinfo is not None else None


def _newest_comment_at(issue: Mapping[str, Any]) -> float | None:
    comments = issue.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    newest = None
    for node in nodes if isinstance(nodes, list) else ():
        created = node.get("createdAt") if isinstance(node, dict) else None
        try:
            parsed = datetime.fromisoformat(created) if isinstance(created, str) else None
        except ValueError:
            continue
        if parsed is not None and parsed.tzinfo is not None:
            newest = max(newest or 0.0, parsed.timestamp())
    return newest


def _risk_header(
    comments: Sequence[Comment],
    trusted: Mapping[str, Mapping[str, frozenset[str]]],
) -> str | None:
    """#195: the risk= of the first brief or go the trusted orchestrator
    wrote. None when there is none, or that comment was edited."""
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if (
            "risk" in fields
            and fields["kind"] in ("brief", "go")
            and fields.get("role") == "orchestrator"
            and fields.get("from") == ORCHESTRATOR
            and ORCHESTRATOR in trusted_bots(trusted, comment.author)
        ):
            return None if comment.edited_at is not None else fields["risk"]
    return None


def _risk(
    comments: Sequence[Comment],
    trusted: Mapping[str, Mapping[str, frozenset[str]]],
) -> str:
    """The intake header's risk, else high (#68)."""
    return _risk_header(comments, trusted) or "high"


class Hunk(NamedTuple):
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    body: tuple[str, ...]


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$", re.MULTILINE)


def _hunks(patch: str) -> list[Hunk]:
    matches = list(HUNK_HEADER.finditer(patch))
    hunks = []
    for match, end in zip(matches, [*(found.start() for found in matches[1:]), len(patch)]):
        lines = patch[match.end() : end].split("\n")[1:]
        if lines and lines[-1] == "":
            lines.pop()
        old_start, old_count, new_start, new_count, header = match.groups()
        body = tuple(line or " " for line in lines if not line.startswith("\\"))
        hunks.append(
            Hunk(
                int(old_start),
                1 if old_count is None else int(old_count),
                int(new_start),
                1 if new_count is None else int(new_count),
                header,
                body,
            )
        )
    return hunks


def _sql_patch_risk(patch: str, read_source: Callable[[], str | None]) -> bool | None:
    """#195: True when a changed line falls in a SQL statement that changes a
    realtime table, or a changed hunk shows a realtime word at all. #292: a
    patch that does not start at line 1 is lexed over the whole head file
    from `read_source`; None when that file cannot be read or does not match
    the patch."""
    hunks = _hunks(patch)
    for hunk in hunks:
        if any(line.startswith(("+", "-")) for line in hunk.body) and any(
            RISKY_CHANGE.search(text) for text in (hunk.header, *hunk.body)
        ):
            return True
    if not hunks:
        return False
    whole = not (len(hunks) == 1 and hunks[0].old_start <= 1 and hunks[0].new_start <= 1)
    if whole:
        source = read_source()
        body = None if source is None else _patch_over_source(source, hunks)
        if body is None:
            return None
    else:
        body = list(hunks[0].body)
    sides = {mark: _sql_side(body, mark) for mark in "-+"}
    before, after = (_sql_statements_risk(sides[mark], mark, whole=whole)[1] for mark in "-+")
    also_changed = frozenset(
        at for at, line in enumerate(body) if line.startswith(" ") and before[at] != after[at]
    )
    verdicts = [
        _sql_statements_risk(sides[mark], mark, whole=whole, also_changed=also_changed)[0] for mark in "-+"
    ]
    if True in verdicts:
        return True
    return None if None in verdicts else False


def _patch_over_source(source: str, hunks: Sequence[Hunk]) -> list[str] | None:
    lines = source.split("\n")
    if lines[-1] == "":
        lines.pop()
    body: list[str] = []
    old = new = 0
    for hunk in hunks:
        old_first = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        new_first = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        if not new <= new_first <= len(lines) or old_first - new_first != old - new:
            return None
        body += [" " + line for line in lines[new:new_first]]
        old, new = old_first, new_first
        for line in hunk.body:
            if line.startswith("-"):
                old += 1
            elif line.startswith((" ", "+")) and new < len(lines) and line[1:] == lines[new]:
                old += line.startswith(" ")
                new += 1
            else:
                return None
            body.append(line)
        if (old - old_first, new - new_first) != (hunk.old_count, hunk.new_count):
            return None
    return body + [" " + line for line in lines[new:]]


def _sql_side(body: Sequence[str], mark: str) -> list[str]:
    """#292: one side of a diff body. The other side's lines become empty
    changed lines, so a change that leaves nothing on this side still marks
    the statement it falls in."""
    return [line if line.startswith((" ", mark)) else mark for line in body]


SQL_IDENT_START = "A-Za-z_\x80-\U0010ffff"
SQL_TOKEN = re.compile(
    rf"--|/\*|;|(?<![{SQL_IDENT_START}0-9$])[eE]'|'|\""
    rf"|(?<![{SQL_IDENT_START}0-9$])\$(?:[{SQL_IDENT_START}][{SQL_IDENT_START}0-9]*)?\$"
)
SQL_COMMENT = re.compile(r"/\*|\*/")


class SqlLexState(NamedTuple):
    comment_depth: int
    quote: str | None


def _sql_quote(opener: str) -> tuple[re.Pattern[str], str]:
    """The pattern that scans a quoted run, and the match that closes it."""
    if opener in ("'", '"'):
        return re.compile(re.escape(opener * 2) + "|" + re.escape(opener)), opener
    if opener in ("e'", "E'"):
        return re.compile(r"\\.|''|'", re.DOTALL), "'"
    return re.compile(re.escape(opener)), opener


def _sql_statements_risk(
    lines: Sequence[str], mark: str, *, whole: bool, also_changed: Collection[int] = ()
) -> tuple[bool | None, list[SqlLexState]]:
    """#195: lexes one side of a diff body from line 1 as PostgreSQL. Comments
    are dropped; quoted strings, identifiers and dollar-quoted bodies keep
    their words but never end a statement. #292: a whole file that ends inside
    a comment or quote, or a lone hunk that ends inside a changed statement,
    cannot be classified."""
    text, changed, risky, bare_cr_or_escape_string = "", False, False, False
    depth, quote, close = 0, None, ""
    states: list[SqlLexState] = []
    for index, line in enumerate(lines):
        states.append(SqlLexState(depth, None if quote is None else quote.pattern))
        mine = line.startswith(mark) or index in also_changed
        changed = changed or mine
        bare_cr_or_escape_string = bare_cr_or_escape_string or "\r" in line.removesuffix("\r")
        code, at = line[1:] + "\n", 0
        while at < len(code):
            if depth:
                found = SQL_COMMENT.search(code, at)
                if found is None:
                    break
                at = found.end()
                depth += 1 if found.group() == "/*" else -1
                text += "" if depth else " "
                continue
            if quote is not None:
                found = quote.search(code, at)
                piece = code[at : len(code) if found is None else found.end()]
                text += piece
                changed = changed or (mine and bool(piece.strip()))
                if found is None:
                    break
                at = found.end()
                if found.group() == close:
                    quote = None
                continue
            found = SQL_TOKEN.search(code, at)
            piece = code[at : len(code) if found is None else found.start()]
            text += piece
            changed = changed or (mine and bool(piece.strip()))
            if found is None:
                break
            token, at = found.group(), found.end()
            if token == ";":
                risky = risky or (changed and bool(RISKY_CHANGE.search(text)))
                text, changed = "", False
            elif token == "--":
                text, at = text + " ", len(code)
            elif token == "/*":
                depth = 1
            else:
                bare_cr_or_escape_string = bare_cr_or_escape_string or token in ("e'", "E'")
                quote, close = _sql_quote(token)
                text += token
    if risky or (changed and RISKY_CHANGE.search(text)):
        return True, states
    realtime_possible = not whole or any(RISKY_CHANGE.search(line) for line in lines)
    if bare_cr_or_escape_string and realtime_possible:
        return None, states
    unfinished = bool(depth) or quote is not None
    begun = changed and (unfinished or bool(text.strip()))
    return (None if (unfinished if whole else begun) else False), states


def _linear_migrations_label(issue: Mapping[str, Any]) -> bool | None:
    """Whether the issue has a `migrations` label; None when its labels
    cannot be read."""
    labels = issue.get("labels")
    if not isinstance(labels, dict):
        return None
    page = labels.get("pageInfo")
    nodes = labels.get("nodes")
    if not isinstance(page, dict) or not isinstance(nodes, list):
        return None
    names = [node.get("name") if isinstance(node, dict) else None for node in nodes]
    if not all(isinstance(name, str) for name in names):
        return None
    if any(name.strip().lower() == "migrations" for name in names):
        return True
    return None if page.get("hasNextPage") is not False else False


def _github_brief(brief: Comment | None) -> dict[str, str | None]:
    if brief is None:
        return {"brief": None, "brief_text": None, "brief_url": None}
    return {
        "brief": brief.url or brief.body.strip(),
        "brief_text": before_factory_md(parse_comment(brief.body).body).strip(),
        "brief_url": brief.url,
    }


def _brief_body(brief: Comment | None) -> str | None:
    return None if brief is None else brief.body.strip()


def _pr_head(pull: Mapping[str, Any], repo: str, number: str) -> str:
    head = _object(pull.get("head"), "GitHub pull request head").get("sha")
    if not isinstance(head, str):
        raise PollSourceError(f"{repo}#{number} has no head")
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise PollSourceError(f"{repo}#{number} head {head[:40]!r} is not a full SHA")
    return head


def _latest_head(comments: Sequence[Comment]) -> str | None:
    head: str | None = None
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if "head" in fields:
            head = fields["head"]
    return head


def _has_ready(comments: Sequence[Comment]) -> bool:
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if fields["kind"] == "ready":
            return True
    return False
