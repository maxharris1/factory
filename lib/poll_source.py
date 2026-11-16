"""Production GitHub and Linear source for the dispatcher poll loop."""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import socket
import sqlite3
import subprocess
import threading
import time
from bisect import bisect_right
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

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
from lib.dispatch.constants import STOPPED_POLL_SECONDS
from lib.dispatch.cycle_clock import CycleClock
from lib.factory_md import FactoryConfig, FactoryContext, parse_factory_context, parse_factory_md
from lib.github_actions import (
    GhResult,
    GitHubReviewActions,
    PULL_FILES_LIMIT,
    LinearReviewActions,
    Runner,
    _is_read,
    run_gh,
)
from lib.handoff import HandoffError, parse_comment
from lib.intake_risk import INTAKE_RISK_SCHEMA, INTAKE_RISK_TABLE, stored_risk
from lib.linear_webhook import DirtyRow, missed_delivery
from lib.orch_yml import (
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
    attachments(first: 50) {
      nodes { url }
      pageInfo { hasNextPage }
    }
    labels(first: 50) { nodes { name } pageInfo { hasNextPage } }
  }
}
""".strip()
LINEAR_READY_MUTATION = """
mutation FactoryReadyForReview($issue: String!, $state: String!) {
  issueUpdate(id: $issue, input: {stateId: $state}) { success }
}
""".strip()
BACKOFF_SECONDS = 300.0
GH_CALL_TIMEOUT_SECONDS = 20.0
GH_CALL_ATTEMPTS = 3
GH_RETRY_BACKOFF_SECONDS = 2.0
READ_DEADLINE_SECONDS = 30.0
HANG_STRIKES = 2
LINEAR_CUTOFF_MARGIN_SECONDS = 1.0
LINEAR_REQUESTS_PER_HOUR = 2_500
LINEAR_COMPLEXITY_PER_HOUR = 3_000_000
LINEAR_SHARE = 0.5
LINEAR_SEARCH_COMPLEXITY = 116
LINEAR_ISSUE_COMPLEXITY = 779
ORCHESTRATOR = "grok-bot"
RISKY_PATH = re.compile(r"migration|realtime")
RISKY_CHANGE = re.compile(r"realtime|\bpublication\b|replica\s+identity", re.IGNORECASE)
SQL_SUFFIXES = (".sql", ".psql", ".pgsql")
BLOB_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
LINEAR_PROCESS_HEADROOM = 2
HEAD_READ_IDLE_SECONDS = 12 * STOPPED_POLL_SECONDS
LINEAR_SEARCH_MAX_PAGES = 2
LINEAR_TRACKED_RESERVE = 7
_HTTP_STATUS = re.compile(r"(?:\(|\b)HTTP\s+([0-9]{3})\)?")
_PR_URL = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/"
    r"(?P<number>[1-9][0-9]*)/?$"
)


class PollSourceError(RuntimeError):
    pass


class FactoryParseError(PollSourceError):
    """factory.md was fetched but does not validate."""


class PollCycleError(PollSourceError):
    pass


class RetryablePollSourceError(PollSourceError, RetryableStateError):
    pass


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


_REPO_ROUTE = re.compile(r"repos/([^/?]+/[^/?]+)")


class _Cutoff:
    """urllib's timeout is per socket operation, so a reply trickling bytes
    never times out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.fired = False

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

    def _open_tracked_before_tunnel(self, *args: Any, **kwargs: Any) -> socket.socket:
        sock = socket.create_connection(*args, **kwargs)
        self._cutoff.track(sock)
        return sock


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
    cutoff = _Cutoff()
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
        sleep: Callable[[float], None] = time.sleep,
        gh_call_timeout_seconds: float = GH_CALL_TIMEOUT_SECONDS,
        read_deadline_seconds: float = READ_DEADLINE_SECONDS,
        risks: IntakeRisks | None = None,
    ):
        self.config = config
        self.risks = IntakeRisks() if risks is None else risks
        self.environ = environ
        self.gh_runner = gh_runner
        self.linear_transport = linear_transport
        self.timeout_seconds = timeout_seconds
        self.error_sink = error_sink
        self.deferral_sink = deferral_sink
        self.terminal_sink = terminal_sink
        self.clock = clock
        self.sleep = sleep
        self.gh_call_timeout_seconds = gh_call_timeout_seconds
        self.read_deadline_seconds = read_deadline_seconds
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
        self._head_reads: dict[tuple[str, str, str], tuple[tuple[str, str, str], Any]] = {}
        self._head_read_last_used: dict[tuple[str, str, str], float] = {}
        self._linear_issue_cache: dict[str, dict[str, Any] | None] = {}
        self._etag_cache: dict[tuple[str, ...], tuple[str, str]] = {}
        self._backoff_until = 0.0
        self._linear_backoff_until = 0.0
        self._linear_requests = 0
        self._linear_complexity = 0
        self._linear_candidate_cursor: tuple[str, str] | None = None
        self._linear_tracked_cursor: tuple[str, str] | None = None
        self._cycle_errors: dict[str, int] = {}
        self._cycle_deferrals: dict[str, int] = {}
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
        cleared = _secret_env_names(config)
        # list_comments and ci_status are polling reads: they get the same
        # short timeout, retries and backoff as every other gh read here.
        self.github = GitHubReviewActions(
            runner=self._adapter_runner,
            timeout_seconds=gh_call_timeout_seconds,
            cleared_variables=cleared,
        )
        self.gh = self.github.gh

    def review_actions_by_repo(self) -> dict[str, ReviewActions]:
        cleared = _secret_env_names(self.config)
        actions: dict[str, ReviewActions] = {}
        for project in self.config.projects:
            if not project.linear_project_ids:
                action: ReviewActions = GitHubReviewActions(
                    runner=self.gh_runner,
                    timeout_seconds=self.timeout_seconds,
                    cleared_variables=cleared,
                )
            else:
                action = LinearReviewActions(
                    move_ready_for_review=project.linear_ready_for_review,
                    linear_ready=self._move_linear_ready,
                    fresh_ticket=self.fresh_ticket,
                    runner=self.gh_runner,
                    timeout_seconds=self.timeout_seconds,
                    cleared_variables=cleared,
                )
            actions[project.repo.lower()] = action
        return actions

    def begin_cycle(self) -> None:
        self._factory_cache.clear()
        self._ticket_cache.clear()
        self._linear_issue_cache.clear()
        self._linear_due = self._cycles_until_linear <= 0
        if self._linear_due:
            self._cycles_until_linear = self._linear_every
            self._linear_requests = 0
            self._linear_complexity = 0
        self._cycles_until_linear -= 1
        self._cycle_errors = {}
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
            sources = ", ".join(
                f"{source} x{count}" for source, count in sorted(self._cycle_errors.items())
            )
            raise PollCycleError(
                f"poll cycle recorded {sum(self._cycle_errors.values())} source error(s): "
                f"{sources}"
            )

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
                pages = self._gh_pages(endpoint)
            except Exception as exc:
                self._record(project.repo, "-", exc)
                continue
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
                        if (project.repo.lower(), number) in exclude:
                            continue
                        comments: tuple[Comment, ...] | None = None
                        if not labelled:
                            comments = self.github.list_comments(
                                project.repo, number
                            )
                            if not _has_ready(comments):
                                continue
                        ticket = self._github_ticket(
                            project, factory, number, comments=comments
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
        return tuple(tickets)

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
            errors = sum(self._cycle_errors.values())
            deferrals = sum(self._cycle_deferrals.values())
            found = self._searched_candidates(projects, query, exclude, reserve)
            if (
                sum(self._cycle_errors.values()) == errors
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
        # Between Linear cycles tracked Linear tickets wait for the next one:
        # no Linear read, and nothing rebuilt from an older read.
        linear_refs = sorted(
            (
                ref
                for ref in tickets
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
            linear_refs[start:]
            + linear_refs[:start]
            + [ref for ref in tickets if ref.tracker != "linear"]
        )
        for ref in ordered:
            project = projects.get(ref.repo.lower())
            if project is None:
                if ref.tracker == "linear":
                    # Its repo left orch.yml: never read, so ack its dirty rows.
                    self._note_unread(ref.ticket_id)
                continue
            try:
                ticket = self._tracked_ticket(ref, project)
            except Exception as exc:
                if not isinstance(exc, _TerminalTicket):
                    self._record(ref.repo, ref.ticket_id, exc)
            else:
                if ticket is not None:
                    found.append(ticket)
        return tuple(found)

    def _tracked_ticket(
        self,
        ref: TicketRef,
        project: Project,
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
            )
        if ref.tracker != "github":
            return None
        return self._github_ticket(
            project,
            self._factory(project),
            ref.ticket_id,
            allow_merged=ref.allow_merged,
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
        self._linear_requests += 1
        self._linear_complexity += complexity
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": self._linear_key(),
            "Content-Type": "application/json",
        }
        deadline = min(self.timeout_seconds, self.read_deadline_seconds)
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
        if (
            not ticket.linear_ready_for_review
            or ticket.linear_issue_id is None
            or ticket.linear_ready_state_id is None
        ):
            raise PollSourceError("Linear Ready for Review metadata is unavailable")
        document = self._linear_request(
            {
                "query": LINEAR_READY_MUTATION,
                "variables": {
                    "issue": ticket.linear_issue_id,
                    "state": ticket.linear_ready_state_id,
                },
            }
        )
        updated = _object(
            _object(document.get("data"), "Linear data").get("issueUpdate"),
            "Linear issueUpdate",
        )
        if updated.get("success") is not True:
            raise PollSourceError("Linear Ready for Review update was not confirmed")

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
    ) -> TrackedTicket:
        cache_key = ("github", project.repo.lower(), number)
        cached = None if fresh else self._ticket_cache.get(cache_key)
        if cached is not None:
            return cached
        pull = self._github_pull(project.repo, number, fresh=fresh)
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
        if comments is None:
            comments = self.github.list_comments(project.repo, number)
        ticket = TrackedTicket(
            **self._hold_inputs(project.repo, number, factory, pull),
            repo=project.repo,
            ticket_id=number,
            factory=factory,
            comments=comments,
            current_head=head,
            ci={head: self.github.ci_status(project.repo, head)},
            risk=self._intake_risk(project.repo, number, comments) or "high",
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(pull.get("html_url")),
            git_branch_name=_optional_string(
                _object(pull.get("head"), "GitHub pull request head").get("ref")
            ),
            pr_number=number,
            brief=_brief_link(_brief(comments, self._trusted)),
            grok_bot=project.grok_bot,
            ready_starts_review=project.ready_starts_review,
            intake_label=project.intake.label if project.intake else None,
            source_issue=github_source(pull, project.repo, number),
            pr_title=_open_title(pull),
            **_pull_facts(pull, fresh=fresh),
        )
        self._ticket_cache[cache_key] = ticket
        return ticket

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
        return found

    def _per_head(
        self,
        kind: str,
        repo: str,
        number: str,
        pull: Mapping[str, Any],
        read: Callable[[], Any],
    ) -> Any:
        key = (kind, repo.lower(), number)
        self._head_read_last_used[key] = self.clock()
        base = _object(pull.get("base") or {}, "GitHub pull request base")
        version = (_pr_head(pull, repo, number), str(base.get("ref")), str(base.get("sha")))
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
    ) -> TrackedTicket:
        identifier = issue.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            raise PollSourceError("Linear issue has no identifier")
        cache_key = ("linear", project.repo.lower(), identifier)
        cached = None if fresh else self._ticket_cache.get(cache_key)
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
        head = _latest_head(linear_comments)
        ci: dict[str, CIResult] = {}
        pr_title: str | None = None
        pr_number = self._linked_pr_number(
            project.repo,
            identifier,
            issue,
            fresh=fresh,
        )
        pull_facts: dict[str, Any] = {"merged": False}
        pull: Mapping[str, Any] = {}
        if pr_number is not None:
            pull = self._github_pull(project.repo, pr_number, fresh=fresh)
            # #108: a cached discovery read still carries the merge.
            pull_facts = {**_pull_facts(pull, fresh=fresh), "merged": pull.get("merged") is True}
            if pull_facts["merged"] and tracked:
                self._linear_merged(project, identifier, allow_merged)
            head = _pr_head(pull, project.repo, pr_number)
            pr_title = _open_title(pull)
            ci[head] = self.github.ci_status(project.repo, head)
            github_comments = self.github.list_comments(project.repo, pr_number)
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
        factory = self._factory(project)
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
            risk=self._linear_risk(project.repo, identifier, issue, comments, pr_number, pull),
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(issue.get("url")),
            git_branch_name=_optional_string(issue.get("gitBranchName")),
            pr_number=pr_number,
            brief=_brief_body(_brief(linear_comments, self._trusted)),
            grok_bot=project.grok_bot,
            linear_issue_id=_optional_string(issue.get("id")),
            linear_ready_state_id=_linear_ready_state_id(issue),
            linear_ready_for_review=project.linear_ready_for_review,
            linear_state_phase=_linear_state_phase(issue),
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

    def _linked_pr_number(
        self,
        repo: str,
        identifier: str,
        issue: Mapping[str, Any],
        *,
        fresh: bool = False,
    ) -> str | None:
        attachments = _object(issue.get("attachments"), "Linear attachments")
        page = _object(
            attachments.get("pageInfo"),
            "Linear attachments pageInfo",
        )
        if page.get("hasNextPage") is True:
            raise PollSourceError("Linear issue has more than 50 attachments")
        numbers = {
            match.group("number")
            for item in _array(
                attachments.get("nodes"),
                "Linear attachment nodes",
            )
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and (match := _PR_URL.fullmatch(item["url"])) is not None
            and match.group("repo").lower() == repo.lower()
        }
        if len(numbers) > 1:
            raise PollSourceError("Linear issue links more than one GitHub PR")
        if numbers:
            return next(iter(numbers))
        result = _json_object(
            self._gh_call(
                "-X",
                "GET",
                "search/issues",
                "-f",
                f"q={identifier} repo:{repo} is:pr is:open",
                fresh=fresh,
            ),
            "GitHub pull request search",
        )
        matches = [
            str(_positive_int(item.get("number"), "pull request number"))
            for item in _array(result.get("items"), "GitHub search items")
            if isinstance(item, dict)
        ]
        return matches[0] if len(matches) == 1 else None

    def _gh_pages(self, endpoint: str) -> list[object]:
        text = self._gh_call("--paginate", endpoint)
        decoder = json.JSONDecoder()
        pages: list[object] = []
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text):
                break
            try:
                page, index = decoder.raw_decode(text, index)
            except json.JSONDecodeError as exc:
                raise PollSourceError("gh returned malformed JSON") from exc
            pages.append(page)
        if not pages:
            raise PollSourceError("gh returned no JSON")
        return pages

    def _gh_call(
        self,
        *args: str,
        fresh: bool = False,
        _unconditional: bool = False,
    ) -> str:
        now = self.clock()
        if now < self._backoff_until:
            raise PollSourceError("GitHub rate-limit backoff is active")
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
                return self._not_modified(args, _unconditional)
        if result.returncode != 0:
            status = (
                included[0]
                if included is not None
                else _status_code(result.stderr)
            )
            if _is_rate_limited(status, result.stdout, result.stderr):
                self._backoff_until = now + BACKOFF_SECONDS
            if status is not None:
                raise PollSourceError(f"gh returned HTTP {status}")
            raise PollSourceError(f"gh exited {result.returncode}")
        if not conditional:
            return result.stdout
        status, headers, body = (
            included if included is not None else _included_response(result.stdout)
        )
        if status == 304:
            return self._not_modified(args, _unconditional)
        etag = headers.get("etag")
        if etag is not None:
            self._etag_cache[cache_key] = (etag, body)
        return body

    def _not_modified(self, args: tuple[str, ...], unconditional: bool) -> str:
        """Serve a 304 from the ETag cache, or refetch once when it is empty.

        After a restart the in-memory cache is empty, yet gh's own --cache
        can replay a 304 it stored for the previous process's conditional
        request (gh does not key its cache on If-None-Match). Refetch once
        with neither --cache nor If-None-Match, which cannot return 304.
        """
        cached = self._etag_cache.get(tuple(args))
        if cached is not None and not unconditional:
            return cached[1]
        if unconditional:
            raise PollSourceError("gh returned 304 without a cached response")
        return self._gh_call(*args, _unconditional=True)

    def _adapter_runner(self, argv: Sequence[str], timeout: float) -> GhResult:
        """Retry the poll adapter's reads (GETs and GraphQL queries); never repeat a write."""
        call = list(argv)
        args = call[call.index("api") + 1 :]
        if _is_read(args) or _is_graphql_query(args):
            return self._run_gh_read(call)
        with self._timed(_gh_source(call)):
            return self.gh_runner(call, self.timeout_seconds)

    def _run_gh_read(self, argv: list[str]) -> GhResult:
        self._check_hangs("github")
        with self._timed(_gh_source(argv)):
            deadline = self.clock() + self.read_deadline_seconds
            attempt = 1
            while True:
                left = deadline - self.clock()
                try:
                    return self.gh_runner(argv, min(self.gh_call_timeout_seconds, left))
                except subprocess.TimeoutExpired:
                    pause = GH_RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    if attempt >= GH_CALL_ATTEMPTS or deadline - self.clock() <= pause:
                        self._hung("github")
                        raise RetryablePollSourceError(
                            f"gh api timed out after {attempt} attempts"
                        ) from None
                except OSError as exc:
                    raise PollSourceError(
                        f"gh could not run: {type(exc).__name__}"
                    ) from exc
                self.sleep(pause)
                attempt += 1

    def _timed(self, name: str) -> AbstractContextManager[None]:
        return nullcontext() if self.cycle_clock is None else self.cycle_clock.source(name)

    def _check_hangs(self, service: str) -> None:
        if self._hangs.get(service, 0) >= HANG_STRIKES:
            raise RetryablePollSourceError(
                f"{service} skipped for the rest of this cycle after "
                f"{HANG_STRIKES} timed-out reads"
            )

    def _hung(self, service: str) -> None:
        self._hangs[service] = self._hangs.get(service, 0) + 1
        if self._hangs[service] == HANG_STRIKES and self.warning_sink is not None:
            self.warning_sink(
                f"{service} skipped for the rest of this cycle after "
                f"{HANG_STRIKES} timed-out reads; read again next cycle"
            )

    def _defer(self, reason: str) -> None:
        """A budget or page-limit cutoff is expected load shedding, not an error."""
        self._cycle_deferrals[reason] = self._cycle_deferrals.get(reason, 0) + 1

    def _record(self, repo: str, ticket_id: str, exc: Exception) -> None:
        self._cycle_errors[repo] = self._cycle_errors.get(repo, 0) + 1
        if self.error_sink is None:
            return
        reason = _safe_reason(exc)
        for name in _secret_env_names(self.config):
            value = self.environ.get(name)
            if value:
                reason = reason.replace(value, "<secret>")
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


def _gh_source(argv: Sequence[str]) -> str:
    for part in argv:
        match = _REPO_ROUTE.match(part)
        if match:
            return match.group(1).lower()
    return "github"


def _json_object(text: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PollSourceError(f"{source} returned malformed JSON") from exc
    return _object(value, source)


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


def _linear_ref_key(ref: TicketRef) -> tuple[str, str]:
    return (ref.repo.lower(), ref.ticket_id)


def _linear_request_complexity(query: object) -> int:
    return (
        LINEAR_SEARCH_COMPLEXITY
        if query == LINEAR_SEARCH_QUERY
        else LINEAR_ISSUE_COMPLEXITY
    )


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


def _is_rate_limited(status: int | None, stdout: str, stderr: str) -> bool:
    if status == 429:
        return True
    if status != 403:
        return False
    text = f"{stdout}\n{stderr}".lower()
    return "rate limit" in text or re.search(
        r"(?im)^x-ratelimit-remaining:\s*0\s*$",
        stdout,
    ) is not None


def _safe_reason(exc: Exception) -> str:
    """The poll_errors text for a source error. A PollSourceError's message
    is built here from safe parts; any other error keeps its kind and gets
    its message scrubbed."""
    if isinstance(exc, PollSourceError):
        return f"PollSourceError:{str(exc)}"
    status = _status_code(str(exc))
    kind = type(exc).__name__ if status is None else f"{type(exc).__name__}:HTTP-{status}"
    detail = scrub_detail(str(exc))
    return f"{kind}: {detail}" if detail else kind


def _secret_env_names(config: OrchConfig) -> tuple[str, ...]:
    names: list[str] = []
    if config.linear is not None:
        names.append(config.linear.api_key_env)
    if config.grok is not None:
        names.extend(
            (
                config.grok.webhook_url_env,
                config.grok.webhook_key_env,
                config.grok.webhook_auth_env,
            )
        )
    return tuple(dict.fromkeys(names))


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
    """The linked PR's merge facts a ticket carries, whatever its tracker
    (#130: the merge queue reads them)."""
    merged = pull.get("merged") is True
    mergeable = pull.get("mergeable")
    return {
        "merged": merged and fresh,
        "mergeable": mergeable if isinstance(mergeable, bool) else None,
        "mergeable_state": _optional_string(pull.get("mergeable_state")),
        "closed": pull.get("state") == "closed" and not merged,
    }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _linear_ready_state(issue: Mapping[str, Any]) -> dict[str, Any] | None:
    team = issue.get("team")
    if team is None:
        return None
    states = _object(_object(team, "Linear team").get("states"), "Linear states")
    for raw in _array(states.get("nodes"), "Linear state nodes"):
        state = _object(raw, "Linear state")
        if state.get("name") == "Ready for Review":
            return state
    return None


def _linear_ready_state_id(issue: Mapping[str, Any]) -> str | None:
    state = _linear_ready_state(issue)
    return _optional_string(state.get("id")) if state is not None else None


def _linear_state_phase(issue: Mapping[str, Any]) -> str | None:
    """Where the issue stands relative to Ready for Review, or None if unknown.

    before-review: backlog, triage, unstarted, or started and not after
    Ready for Review in the team's order. ready-for-review: in that state.
    moved-on: completed, canceled, or a started state after Ready for
    Review. no-ready-state: not completed or canceled, on a team that has
    no Ready for Review state, so there is nothing to move it to.
    The handoff moves only a before-review issue and fails closed on None.
    """
    state = issue.get("state")
    if not isinstance(state, dict):
        return None
    state_id = _optional_string(state.get("id"))
    kind = state.get("type")
    if state_id is None or not isinstance(kind, str):
        return None
    if not isinstance(issue.get("team"), dict):
        return None
    ready = _linear_ready_state(issue)
    if ready is not None and ready.get("id") == state_id:
        return "ready-for-review"
    if kind in ("completed", "canceled"):
        return "moved-on"
    if kind not in ("backlog", "triage", "unstarted", "started"):
        return None
    if ready is None:
        return "no-ready-state"
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


def _brief_link(brief: Comment | None) -> str | None:
    return None if brief is None else brief.url or brief.body.strip()


def _brief_body(brief: Comment | None) -> str | None:
    return None if brief is None else brief.body.strip()


def _brief(
    comments: Sequence[Comment],
    trusted: Mapping[str, Mapping[str, frozenset[str]]],
) -> Comment | None:
    latest: Comment | None = None
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if (
            fields["kind"] == "brief"
            and fields.get("from") == ORCHESTRATOR
            and fields.get("role") == "orchestrator"
            and ORCHESTRATOR in trusted_bots(trusted, comment.author)
        ):
            latest = comment
    return latest


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
