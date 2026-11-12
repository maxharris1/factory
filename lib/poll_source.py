"""Production GitHub and Linear source for the dispatcher poll loop."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from lib.dispatcher import (
    CIResult,
    Comment,
    RetryableStateError,
    ReviewActions,
    TicketRef,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig, parse_factory_md
from lib.github_actions import (
    GhResult,
    GitHubReviewActions,
    LinearReviewActions,
    Runner,
    _is_read,
    run_gh,
)
from lib.handoff import HandoffError, parse_comment
from lib.linear_webhook import DirtyRow
from lib.orch_yml import OrchConfig, Project, resolve_job_timeout

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
      nodes { id body createdAt user { id } }
      pageInfo { hasNextPage }
    }
    attachments(first: 50) {
      nodes { url }
      pageInfo { hasNextPage }
    }
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
LINEAR_REQUESTS_PER_HOUR = 2_500
LINEAR_COMPLEXITY_PER_HOUR = 3_000_000
LINEAR_SHARE = 0.5
LINEAR_SEARCH_COMPLEXITY = 116
LINEAR_ISSUE_COMPLEXITY = 672  # +12 for the issue state and state order (#47)
LINEAR_PROCESS_HEADROOM = 2
LINEAR_SEARCH_MAX_PAGES = 2
LINEAR_TRACKED_RESERVE = 7
_HTTP_STATUS = re.compile(r"(?:\(|\b)HTTP\s+([0-9]{3})\)?")
_PR_URL = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/"
    r"(?P<number>[1-9][0-9]*)/?$"
)


class PollSourceError(RuntimeError):
    pass


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
DeferralSink = Callable[[str], None]
TerminalSink = Callable[[str, str, str], None]


class DirtyQueue(Protocol):
    """The linear_dirty rows the webhook listener writes (lib/linear_webhook)."""

    def pending(self) -> tuple[DirtyRow, ...]: ...

    def ack(self, rows: Sequence[DirtyRow]) -> None: ...

    def record_polling(self, now: float) -> None: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


class _TerminalTicket(RuntimeError):
    pass


def post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> HTTPResult:
    try:
        request = Request(url, data=body, method="POST", headers=dict(headers))
        with _OPENER.open(request, timeout=timeout) as response:
            return HTTPResult(
                response.status,
                response.read().decode("utf-8"),
                dict(response.headers.items()),
            )
    except HTTPError as exc:
        return HTTPResult(exc.code, "", dict(exc.headers.items()))
    except (OSError, URLError, ValueError) as exc:
        raise PollSourceError(
            f"Linear request failed: {type(exc).__name__}"
        ) from None


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
    ):
        self.config = config
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
        self._factory_cache: dict[str, FactoryConfig] = {}
        self._ticket_cache: dict[tuple[str, str, str], TrackedTicket] = {}
        self._linear_issue_cache: dict[str, dict[str, Any] | None] = {}
        self._etag_cache: dict[tuple[str, ...], tuple[str, str]] = {}
        self._backoff_until = 0.0
        self._linear_backoff_until = 0.0
        self._linear_requests = 0
        self._linear_complexity = 0
        self._linear_candidate_cursor: tuple[str, str] | None = None
        self._linear_tracked_cursor: tuple[str, str] | None = None
        self._cycle_errors = 0
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
            if project.linear_project_id is None:
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
        self._linear_requests = 0
        self._linear_complexity = 0
        self._cycle_errors = 0
        self._cycle_deferrals = {}
        if self.config.linear_webhook is not None:
            self._begin_webhook_cycle()

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
            raise PollCycleError(
                f"poll cycle recorded {self._cycle_errors} source error(s)"
            )

    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]:
        tickets: list[TrackedTicket] = []
        for project in self.config.projects:
            if project.linear_project_id is not None:
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
            project.linear_project_id: project
            for project in self.config.projects
            if project.linear_project_id is not None
        }
        if not projects:
            return ()
        if self.config.linear_webhook is None:
            return self._searched_candidates(projects, query, exclude, reserve)
        found: tuple[TrackedTicket, ...] = ()
        if self._reconcile_due():
            errors = self._cycle_errors
            deferrals = sum(self._cycle_deferrals.values())
            found = self._searched_candidates(projects, query, exclude, reserve)
            if (
                self._cycle_errors == errors
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
        linear_refs = sorted(
            (ref for ref in tickets if ref.tracker == "linear"),
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
                if ref.tracker == "linear":
                    cached = ref.ticket_id in self._linear_issue_cache
                    issue = None if cached else self._reusable_linear_issue(ref)
                    if issue is None:
                        if not cached and not self._linear_request_available(
                            LINEAR_ISSUE_QUERY,
                            reserve=LINEAR_PROCESS_HEADROOM,
                        ):
                            self._defer("Linear poll budget deferred tracked ticket")
                            continue
                        if not cached:
                            self._linear_tracked_cursor = _linear_ref_key(ref)
                        issue = self._linear_issue(ref.ticket_id)
                    if issue is None:
                        raise PollSourceError("Linear issue was not found")
                    if not _matches_linear_project(issue, project):
                        raise PollSourceError(
                            "Linear issue is not in its configured project"
                        )
                    found.append(self._linear_ticket(project, issue))
                    continue
                if ref.tracker != "github":
                    continue
                factory = self._factory(project)
                found.append(
                    self._github_ticket(
                        project,
                        factory,
                        ref.ticket_id,
                        allow_merged=ref.allow_merged,
                    )
                )
            except Exception as exc:
                if isinstance(exc, _TerminalTicket):
                    continue
                self._record(ref.repo, ref.ticket_id, exc)
        return tuple(found)

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
                project.linear_project_id
                for project in self.config.projects
                if project.linear_project_id is not None
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
        else:
            for key in keys:
                self._linear_documents.pop(key, None)
        for row in self._dirty.values():
            if row.issue_id in keys or row.identifier in keys:
                self._dirty_fetched[row.issue_id] = row

    def _linear_request(self, document: Mapping[str, object]) -> dict[str, Any]:
        if self.clock() < self._linear_backoff_until:
            raise RetryablePollSourceError(
                "Linear rate-limit backoff is active"
            )
        query = document.get("query")
        complexity = _linear_request_complexity(query)
        if not self._linear_request_available(query):
            raise RetryablePollSourceError("Linear poll budget is exhausted")
        self._linear_requests += 1
        self._linear_complexity += complexity
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        try:
            result = self.linear_transport(
                LINEAR_ENDPOINT,
                {
                    "Authorization": self._linear_key(),
                    "Content-Type": "application/json",
                },
                payload,
                self.timeout_seconds,
            )
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
        interval = self.config.poll_seconds
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
        factory = self._factory_at(project.repo, project.factory_ref)
        if factory.base_branch != project.factory_ref:
            raise PollSourceError(
                f"{project.repo} factory.md base_branch is {factory.base_branch}, "
                f"not configured factory_ref {project.factory_ref}"
            )
        self._factory_cache[identity] = factory
        return factory

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
        factory, findings = parse_factory_md(text)
        errors = [item.message for item in findings if item.severity == "error"]
        if factory is None:
            detail = errors[0] if errors else "invalid factory.md"
            raise PollSourceError(f"{repo} factory.md: {detail}")
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
        if pull.get("merged") is True and not allow_merged:
            if self.terminal_sink is not None:
                self.terminal_sink(project.repo, number, "merged")
            raise _TerminalTicket()
        head = _object(pull.get("head"), "GitHub pull request head").get("sha")
        if not isinstance(head, str):
            raise PollSourceError(f"{project.repo}#{number} has no head")
        if comments is None:
            comments = self.github.list_comments(project.repo, number)
        ticket = TrackedTicket(
            repo=project.repo,
            ticket_id=number,
            factory=factory,
            comments=comments,
            current_head=head,
            ci={head: self.github.ci_status(project.repo, head)},
            risk=_risk(comments),
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(pull.get("html_url")),
            git_branch_name=_optional_string(
                _object(pull.get("head"), "GitHub pull request head").get("ref")
            ),
            pr_number=number,
            brief=_brief(comments),
            grok_bot=project.grok_bot,
            ready_starts_review=project.ready_starts_review,
        )
        self._ticket_cache[cache_key] = ticket
        return ticket

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
    ) -> TrackedTicket:
        identifier = issue.get("identifier")
        if not isinstance(identifier, str) or not identifier:
            raise PollSourceError("Linear issue has no identifier")
        cache_key = ("linear", project.repo.lower(), identifier)
        cached = None if fresh else self._ticket_cache.get(cache_key)
        if cached is not None:
            return cached
        comments_data = _object(issue.get("comments"), "Linear comments")
        comments_page = _object(
            comments_data.get("pageInfo"),
            "Linear comments pageInfo",
        )
        if comments_page.get("hasNextPage") is True:
            raise PollSourceError("Linear issue has more than 250 comments")
        linear_comments = tuple(
            comment
            for item in _array(comments_data.get("nodes"), "Linear comment nodes")
            if (comment := _linear_comment(item)) is not None
        )
        comments = linear_comments
        head = _latest_head(linear_comments)
        ci: dict[str, CIResult] = {}
        pr_number = self._linked_pr_number(
            project.repo,
            identifier,
            issue,
            fresh=fresh,
        )
        if pr_number is not None:
            pull = self._github_pull(project.repo, pr_number, fresh=fresh)
            pr_head = _object(
                pull.get("head"),
                "GitHub pull request head",
            ).get("sha")
            if not isinstance(pr_head, str):
                raise PollSourceError(
                    f"{project.repo}#{pr_number} has no head"
                )
            head = pr_head
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
        ticket = TrackedTicket(
            repo=project.repo,
            ticket_id=identifier,
            factory=self._factory(project),
            comments=comments,
            current_head=head,
            ci=ci,
            risk=_risk(comments),
            job_timeout_minutes=resolve_job_timeout(self.config, project.repo),
            ticket_url=_optional_string(issue.get("url")),
            git_branch_name=_optional_string(issue.get("gitBranchName")),
            pr_number=pr_number,
            brief=_brief(linear_comments),
            grok_bot=project.grok_bot,
            linear_issue_id=_optional_string(issue.get("id")),
            linear_ready_state_id=_linear_ready_state_id(issue),
            linear_ready_for_review=project.linear_ready_for_review,
            linear_state_phase=_linear_state_phase(issue),
            ready_starts_review=project.ready_starts_review,
        )
        self._ticket_cache[cache_key] = ticket
        return ticket

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
        """Retry the poll adapter's GETs; never repeat a write."""
        call = list(argv)
        if _is_read(call[call.index("api") + 1 :]):
            return self._run_gh_read(call)
        return self.gh_runner(call, self.timeout_seconds)

    def _run_gh_read(self, argv: list[str]) -> GhResult:
        """Every _gh_call is a GET, so a timed-out call is safe to repeat."""
        attempt = 1
        while True:
            try:
                return self.gh_runner(argv, self.gh_call_timeout_seconds)
            except subprocess.TimeoutExpired:
                if attempt >= GH_CALL_ATTEMPTS:
                    raise RetryablePollSourceError(
                        f"gh api timed out after {attempt} attempts"
                    ) from None
            except OSError as exc:
                raise PollSourceError(
                    f"gh could not run: {type(exc).__name__}"
                ) from exc
            self.sleep(GH_RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))
            attempt += 1

    def _defer(self, reason: str) -> None:
        """A budget or page-limit cutoff is expected load shedding, not an error."""
        self._cycle_deferrals[reason] = self._cycle_deferrals.get(reason, 0) + 1

    def _record(self, repo: str, ticket_id: str, exc: Exception) -> None:
        self._cycle_errors += 1
        if self.error_sink is None:
            return
        self.error_sink(repo, ticket_id, _safe_reason(exc))


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
    if isinstance(exc, PollSourceError):
        return f"PollSourceError:{str(exc)}"
    status = _status_code(str(exc))
    if status is not None:
        return f"{type(exc).__name__}:HTTP-{status}"
    return type(exc).__name__


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


def _has_label(value: object, prefix: str) -> bool:
    return any(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].startswith(prefix)
        for item in _array(value, "GitHub labels")
    )


def _matches_linear_project(issue: Mapping[str, Any], project: Project) -> bool:
    configured = project.linear_project_id
    if configured is None:
        return False
    value = _object(issue.get("project"), "Linear issue project")
    return configured == value.get("id")


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
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise PollSourceError("Linear comment createdAt is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PollSourceError("Linear comment createdAt is not ISO-8601")
    return Comment(
        id=comment_id,
        author=author,
        body=body,
        created_at=parsed.timestamp(),
    )


def _risk(comments: Sequence[Comment]) -> str:
    risk = "high"
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if "risk" in fields:
            risk = fields["risk"]
    return risk


def _brief(comments: Sequence[Comment]) -> str | None:
    for comment in comments:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            continue
        if fields["kind"] == "brief":
            return comment.url or comment.body.strip()
    return None


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
