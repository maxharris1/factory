"""Dispatcher transitions T1, T1P and T2 through T6, plus project hooks."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.factory_md import FactoryConfig
from lib.grok_delivery import GrokDeliveryError
from lib.handoff import HandoffError, parse_comment
from lib.hold import HoldDecision, LabelEvent, decide
from lib.job_state import (
    DEAD,
    RELEASED_UNSTARTED,
    RUN_DIED,
    STALE,
    JobStateConfig,
    Liveness,
    claimed_too_long,
    derive,
    last_run_end,
    read_placements,
    requeue_unstarted,
    unstarted_too_long,
)
from lib.orch_yml import DEFAULT_JOB_TIMEOUT_MINUTES, POLL_SECONDS
from lib.queue import (
    SLOTS_TABLE,
    Job,
    JobRequest,
    QueueError,
    SQLiteJobQueue,
    _job,
    restored_claim,
    retire_requeued,
)
from lib.state_backup import RESTORED

GITHUB_STATE_LABEL_PREFIX = "orch:state/"
# merged and done are absorbing (#108): no later write moves a ticket out.
FINISHED_STATES = ("merged", "done")
_NOT_FINISHED = "COALESCE(workflow_state, '') NOT IN ('merged', 'done')"
# #108 (#42 N4): states where no bot is working and no person is waiting on
# the factory. Their tickets are read once every STOPPED_POLL_SECONDS.
STOPPED_STATES = ("handed-off", "capped", "blocked", "refused")
STOPPED_POLL_SECONDS = 300
MERGED_BEFORE_HANDOFF = "merged before handoff"
LINEAR_HANDOFF_QUERY = "orch:v1 kind=brief OR orch:v1 kind=go"
GROK_WAKE_REASONS = (
    "blocked",
    "capped",
    "refused",
    "stuck",
    "unparsed",
    "ship-failed",
    "rejected",
    "stale",
    "done",
    "awaiting-ok",
)
CORE_KINDS = ("go", "plan", "ready")
SETTLE_FAILURES_TO_WAKE = 3
_QUESTIONS_HEADING = re.compile(
    r"(?:#{1,6}\s*)?(?:\*\*)?questions(?::\*\*|\*\*:?|:)?(?:\s+(?P<rest>.*))?",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_FENCE_OPEN = re.compile(r" {0,3}(?P<fence>`{3,}(?=[^`]*$)|~{3,})")
_NO_QUESTIONS = frozenset({"none", "n/a", "no questions"})


@dataclass(frozen=True)
class Comment:
    id: str
    author: str
    body: str
    created_at: float
    url: str | None = None


@dataclass(frozen=True)
class CIResult:
    status: str
    log: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ("pending", "success", "failure"):
            raise ValueError(f"unknown CI status {self.status!r}")


# Where a Linear issue stands relative to Ready for Review, read with the
# ticket: still before review (backlog, triage, unstarted, or started before
# Ready for Review; the only phase the handoff moves), already in Ready for
# Review, moved on by a human (completed, canceled, or a started state after
# Ready for Review), or on a team with no Ready for Review state at all (the
# PR is still marked ready and the done wake reports the unmoved issue).
LINEAR_STATE_PHASES = (
    "before-review",
    "ready-for-review",
    "moved-on",
    "no-ready-state",
)


@dataclass(frozen=True)
class TrackedTicket:
    repo: str
    ticket_id: str
    factory: FactoryConfig
    comments: tuple[Comment, ...]
    current_head: str | None
    ci: Mapping[str, CIResult]
    risk: str
    job_timeout_minutes: int = DEFAULT_JOB_TIMEOUT_MINUTES
    ticket_url: str | None = None
    git_branch_name: str | None = None
    pr_number: str | None = None
    brief: str | None = None
    grok_bot: str = "grok-bot"
    linear_issue_id: str | None = None
    linear_ready_state_id: str | None = None
    linear_ready_for_review: bool = False
    linear_state_phase: str | None = None
    ready_starts_review: bool = False
    # Issue #59: the PR had no orch:state/ label and was a candidate only
    # because it carries a parseable ready. Only a trusted builder ready
    # naming the PR head may then start tracking (Codex PR #64 r1 F1.1).
    admitted_by_ready: bool = False
    # Issue #86: read only when factory.md has hold: on a ship: merge repo.
    # None means not read; the hold then fails closed (no merge, retry).
    # labels are the PR's labels, changed_files the paths its diff against
    # base touches at current_head, label_events its labeled / unlabeled
    # events.
    labels: tuple[str, ...] | None = None
    changed_files: tuple[str, ...] | None = None
    label_events: tuple[LabelEvent, ...] | None = None
    # #108 (#42 N3): the PR is merged. A fresh read never records the merge
    # itself: its caller decides.
    merged: bool = False

    def __post_init__(self) -> None:
        if not self.repo or not self.ticket_id:
            raise ValueError("repo and ticket_id must not be empty")
        if self.risk not in ("low", "high"):
            raise ValueError("risk must be low or high")
        if self.job_timeout_minutes < 1:
            raise ValueError("job_timeout_minutes must be positive")


@dataclass(frozen=True)
class TicketRef:
    repo: str
    ticket_id: str
    tracker: str
    allow_merged: bool = False


class PollSource(Protocol):
    def github_candidates(
        self,
        label_prefix: str,
        *,
        exclude: frozenset[tuple[str, str]],
    ) -> tuple[TrackedTicket, ...]: ...

    def linear_candidates(
        self,
        query: str,
        *,
        exclude: frozenset[str],
        reserve: int,
    ) -> tuple[TrackedTicket, ...]: ...

    def tracked_tickets(
        self,
        tickets: tuple[TicketRef, ...],
    ) -> tuple[TrackedTicket, ...]: ...

    def fresh_ticket(self, ticket: TicketRef) -> TrackedTicket | None: ...


class ReviewActions(Protocol):
    def set_state(self, ticket: TrackedTicket, state: str) -> None: ...

    def stable_patch_id(self, ticket: TrackedTicket, head: str) -> str: ...

    def is_merged(self, ticket: TrackedTicket, head: str) -> bool:
        """True only when the PR was merged with exactly this head commit.

        Reads GitHub's PR state at call time, never a local clone. Squash
        and merge-commit merges also count, so this never proves where the
        base branch points.
        """
        ...

    def merge(
        self,
        ticket: TrackedTicket,
        *,
        squash: bool,
        match_head_commit: str,
    ) -> None: ...

    def fast_forward(
        self,
        ticket: TrackedTicket,
        *,
        branch: str,
        head: str,
    ) -> None:
        """Move branch to exactly head, adding no commit.

        Must be a fast-forward-only update of GitHub's remote ref (for
        example PATCH git/refs/heads/<branch> with force=false), never a
        push from a local clone. Raises when head is not a fast-forward of
        branch's current tip (branch moved, or history diverged). Never
        merges, squashes or force-pushes instead.
        """
        ...

    def branch_tip(self, ticket: TrackedTicket, branch: str) -> str:
        """The full SHA of GitHub's remote ref for branch, read at call time.

        Never a local clone or a cached value: a ship counts only when this
        equals the approved head.
        """
        ...

    def handoff(self, ticket: TrackedTicket, head: str) -> str | None:
        """Hand the PR off. A returned note is reported in the done wake."""
        ...

    def await_approver(
        self,
        ticket: TrackedTicket,
        head: str,
        approver: str,
    ) -> None: ...


class GrokDelivery(Protocol):
    """Push a Grok job and return a non-empty delivery receipt.

    Any failure after the push but before queue completion can repeat the wake.
    Implementations must deduplicate on logical_key, which is stable across
    T6 retries, rather than on job.id.
    """

    def deliver(self, job: Job, *, logical_key: str) -> str: ...


class RetryableStateError(RuntimeError):
    pass


class RetryableShipError(RetryableStateError):
    pass


class ShipError(RuntimeError):
    """A ship that did not complete.

    write_confirmed is True unless the raiser knows that no external write
    it sent was confirmed before it failed. A handoff failure with
    write_confirmed False, or a merge failure the raiser marked retryable
    (a draft or not-yet-mergeable PR, a GitHub 5xx, a timeout: issue #83),
    is eligible for the one ship re-drive. safe_detail, when set, is the
    only text recorded in ship_intents.error.
    """

    write_confirmed: bool = True
    retryable: bool = False
    safe_detail: str | None = None


class AlreadyMergedShip(ShipError):
    """The PR was merged before the ship could hand it off (#108, #42 N3):
    the ship has nothing left to do, so it ends as merged, not failed."""


SHIP_ERROR_LIMIT = 240
_SECRET_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://\S*")
_SECRET_HOST_PATH = re.compile(
    r"\b(?:[A-Za-z0-9-]+\.)+(?:com|io|net|org|dev|app)(?:[:/]\S*)?",
    re.IGNORECASE,
)
_SECRET_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|lin_(?:api|oauth)_[A-Za-z0-9_]{8,}|xox[abprs]-[A-Za-z0-9-]{8,}"
    r"|sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)*)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:bearer|basic|token|authorization|password|passwd|secret"
    r"|api[_-]?key|[A-Za-z_]*(?:_TOKEN|_SECRET|_KEY|_AUTH))\b\s*[:=]?\s*\S+"
)
_SECRET_BODY = re.compile(r"[{\[<].*", re.DOTALL)


def safe_ship_error(exc: BaseException) -> str:
    """The exception class plus a detail with no URL, token or body.

    A ShipError's safe_detail is used when present; otherwise the message.
    Either way, URLs, host paths, token shapes, credential assignments and
    everything from the first {, [ or < on are cut, and the text is capped.
    """
    name = type(exc).__name__
    detail = getattr(exc, "safe_detail", None)
    if not isinstance(detail, str):
        detail = str(exc)
    detail = _SECRET_BODY.sub(" <body omitted>", detail)
    detail = _SECRET_URL.sub("<url>", detail)
    detail = _SECRET_HOST_PATH.sub("<host>", detail)
    detail = _SECRET_TOKEN.sub("<secret>", detail)
    detail = _SECRET_ASSIGNMENT.sub("<secret>", detail)
    detail = "".join(char if char.isprintable() else " " for char in detail)
    detail = " ".join(detail.split())
    text = f"{name}: {detail}" if detail else name
    if len(text) > SHIP_ERROR_LIMIT:
        text = text[: SHIP_ERROR_LIMIT - 3].rstrip() + "..."
    return text


@dataclass(frozen=True)
class ShipRequest:
    repo: str
    ticket_id: str
    head: str
    approver: str
    source_comment_id: str
    approve_comment_id: str


class Hook(Protocol):
    """A project hook (T7, T8) sees each unhandled comment before T1/T1P/T2.

    fields is None when the comment has no parseable orch:v1 header.
    handle returns None when the comment is not the hook's event, or
    (handled, outcome); handled comments go into the ledger. Hooks run in
    order and the first answer wins, so (False, outcome) leaves the
    comment for the next poll without offering it to later hooks or the
    core rows. kind=go, kind=plan and kind=ready are never offered to hooks.
    """

    def initialize(self, dispatcher: Dispatcher) -> None: ...

    def handle(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str] | None,
    ) -> tuple[bool, str] | None: ...


class DeviceChecks(Protocol):
    """F2-02's DeviceMonitor: release gone workers' slots, find offline devices.

    A DeviceMonitor also has `liveness(placement)` (#75), which the
    dispatcher uses, when present, to find dead jobs.
    """

    def reconcile(
        self,
        now: float,
        on_offline: Callable[[str, str, float], None],
    ) -> object: ...


class Feeder(Protocol):
    """The backlog feeder (lib/feeder.py, issue #85).

    feed_once opens a draft PR plus brief and go handoffs for each eligible
    ticket that fits the free capacity and returns a report whose `fed`
    lists the tickets it fed this poll as TicketRefs.
    """

    def feed_once(self) -> object: ...


class Dispatcher:
    def __init__(
        self,
        state_path: Path,
        queue: SQLiteJobQueue,
        *,
        trusted_logins: Mapping[str, object],
        review_actions: ReviewActions | Mapping[str, ReviewActions],
        grok_delivery: GrokDelivery,
        hooks: Sequence[Hook] = (),
        devices: DeviceChecks | None = None,
        job_states: JobStateConfig | None = None,
        log: Callable[[str], None] | None = None,
        feeder: Feeder | None = None,
    ):
        if state_path.resolve() != queue.path.resolve():
            raise QueueError("dispatcher state and queue must use one database")
        if not trusted_logins:
            raise ValueError("trusted_logins must not be empty")
        if review_actions is None or (
            isinstance(review_actions, Mapping) and not review_actions
        ):
            raise ValueError("review_actions must be configured")
        if grok_delivery is None:
            raise ValueError("grok_delivery must be configured")
        self.state_path = state_path
        self.queue = queue
        self.trusted_logins = _normalize_trusted_logins(trusted_logins)
        self.review_actions = review_actions
        self.grok_delivery = grok_delivery
        self.hooks = tuple(hooks)
        self.devices = devices
        self.job_states = job_states if job_states is not None else JobStateConfig()
        self.log = log
        self.feeder = feeder
        # #91: the tickets this poll read, so T6 can see a verdict a failed
        # review job already posted. Cleared at the start of each poll.
        self._polled: dict[tuple[str, str], TrackedTicket] = {}
        # #105: the source's uncached read, for a ready naming a newer head.
        self._fresh_reader: Callable[[TicketRef], TrackedTicket | None] | None = None
        # #108: the poll cycle count and interval, for the stopped-state cadence.
        self._cycle = 0
        self._poll_seconds = POLL_SECONDS
        # Stopped tickets whose turn came and that no read has reached yet.
        self._stopped_due: set[tuple[str, str]] = set()
        self._initialize()
        for hook in self.hooks:
            hook.initialize(self)

    def poll_once(self, source: PollSource) -> None:
        begin = getattr(source, "begin_cycle", None)
        end = getattr(source, "end_cycle", None)
        if begin is not None:
            begin()
        self._polled = {}
        self._fresh_reader = getattr(source, "fresh_ticket", None)
        self._poll_seconds = getattr(
            getattr(source, "config", None), "poll_seconds", POLL_SECONDS
        )
        self._cycle += 1
        self._feed(source)
        try:
            github = source.github_candidates(
                GITHUB_STATE_LABEL_PREFIX, exclude=self._tracked_github_ids()
            )
        except Exception as exc:
            self.record_poll_error("github", "-", type(exc).__name__)
            github = ()
        self._discover(github, tracker="github")
        active_refs = self._tracked_refs()
        try:
            linear = source.linear_candidates(
                LINEAR_HANDOFF_QUERY,
                exclude=self._tracked_linear_ids(),
                reserve=sum(
                    ref.tracker == "linear"
                    for ref in active_refs
                ),
            )
        except Exception as exc:
            self.record_poll_error("linear", "-", type(exc).__name__)
            linear = ()
        self._discover(linear, tracker="linear")
        try:
            tracked = source.tracked_tickets(self._tracked_refs(self._cycle))
        except Exception as exc:
            self.record_poll_error("tracked", "-", type(exc).__name__)
            tracked = ()
        self._stopped_due.difference_update(
            (ticket.repo.lower(), ticket.ticket_id) for ticket in tracked
        )
        for ticket in tracked:
            try:
                self.process(ticket)
            except Exception as exc:
                self._record_poll_error(ticket, type(exc).__name__)
        self.reconcile_jobs()
        if end is not None:
            end()

    def _feed(self, source: PollSource) -> None:
        """Run the backlog feeder, then start tracking what it fed.

        A fed ticket is read fresh and discovered here, so its go queues
        the job in this same poll (issue #85: N eligible tickets are N
        queued jobs within one poll). A feeder error is recorded and the
        poll goes on.
        """
        if self.feeder is None:
            return
        try:
            report = self.feeder.feed_once()
        except Exception as exc:
            self.record_poll_error("feeder", "-", type(exc).__name__)
            return
        fresh_reader = getattr(source, "fresh_ticket", None)
        if fresh_reader is None:
            return
        for ref in getattr(report, "fed", ()):
            try:
                ticket = fresh_reader(ref)
            except Exception as exc:
                self.record_poll_error(ref.repo, ref.ticket_id, type(exc).__name__)
                continue
            if ticket is not None and not ticket.merged:
                self._discover((ticket,), tracker=ref.tracker)

    def record_poll_error(self, repo: str, ticket_id: str, error: str) -> None:
        self._stopped_due.discard((repo.lower(), ticket_id))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO poll_errors (
                    repo, ticket_id, error, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (repo.lower(), ticket_id, error, self.queue.now()),
            )

    def record_poll_deferral(self, detail: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
                (detail, self.queue.now()),
            )

    def record_terminal(
        self,
        repo: str,
        ticket_id: str,
        state: str,
        ticket: TrackedTicket | None = None,
    ) -> None:
        """The poll saw a terminal ticket: for a merged PR, _enter_merged.

        ticket, when the source could build it, gets the orch:state/merged
        label, best effort: a failed write is a poll_errors row and never
        undoes the state.
        """
        if state not in ("merged", "handed-off", "done"):
            raise ValueError(f"{state!r} is not a completed terminal state")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if state == "merged":
                entered = self._enter_merged(connection, repo, ticket_id, "observed")
            else:
                entered = False
                connection.execute(
                    """
                    INSERT INTO ticket_state (
                        repo, ticket_id, gate_count, stuck, workflow_state
                    ) VALUES (?, ?, 0, 0, ?)
                    ON CONFLICT(repo, ticket_id)
                    DO UPDATE SET workflow_state = excluded.workflow_state
                    WHERE COALESCE(ticket_state.workflow_state, '')
                          NOT IN ('merged', 'done')
                    """,
                    (repo.lower(), ticket_id, state),
                )
            connection.commit()
        if not entered:
            return
        if self.log is not None:
            self.log(f"merged: {repo}#{ticket_id} (merge observed by the poll)")
        if ticket is None:
            return
        try:
            self._review_actions(ticket).set_state(ticket, "merged")
        except Exception as exc:
            self.record_poll_error(repo, ticket_id, f"merged-label:{type(exc).__name__}")

    def _enter_merged(
        self,
        connection: sqlite3.Connection,
        repo: str,
        ticket_id: str,
        reason: str,
    ) -> bool:
        """The one merged transition, inside the caller's transaction.

        merged is absorbing, and nothing that keeps a ticket on a person's
        screen survives it: stuck, the paused step and every needs-attention
        flag are cleared, and the ticket's queued harness jobs are
        cancelled. Grok wakes (a ship's done wake) and running jobs are
        left alone. True when the ticket was not already merged.
        """
        key = (repo.lower(), ticket_id)
        prior = connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
            key,
        ).fetchone()
        connection.execute(
            """
            INSERT INTO ticket_state (
                repo, ticket_id, gate_count, stuck, workflow_state
            ) VALUES (?, ?, 0, 0, 'merged')
            ON CONFLICT(repo, ticket_id) DO UPDATE SET
                workflow_state = 'merged',
                stuck = 0,
                stuck_reason = NULL,
                stuck_source_comment_id = NULL,
                stuck_wake_job_id = NULL,
                paused_action = NULL,
                paused_recipient = NULL,
                paused_head = NULL,
                paused_generation = NULL,
                paused_question_id = NULL
            """,
            key,
        )
        connection.execute(
            "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ?", key
        )
        queued = connection.execute(
            """
            SELECT id FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
              AND recipient != 'grok'
            ORDER BY created_at, id
            """,
            key,
        ).fetchall()
        for row in queued:
            self.queue.cancel(str(row["id"]), f"merged:{reason}", connection=connection)
        return prior is None or prior["workflow_state"] != "merged"

    def process(self, ticket: TrackedTicket) -> None:
        self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
        comments = sorted(ticket.comments, key=_comment_order)
        try:
            self._cancel_answered_jobs(ticket)
            self._redrive_handoffs(ticket)
            self._process_comments(ticket, comments)
            self._requeue_unanswered_jobs(ticket)
        except BaseException as error:
            try:
                self._settle_external_ships(ticket)
            except Exception as settle_error:
                error.add_note(
                    f"settle also raised {type(settle_error).__name__}: {settle_error}"
                )
            raise
        self._settle_external_ships(ticket)

    def _process_comments(
        self,
        ticket: TrackedTicket,
        comments: list[Comment],
    ) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        for comment in comments:
            # A ready may re-read the ticket uncached (T2); later comments
            # then see the fresh head.
            ticket = self._polled.get(key, ticket)
            if self._is_handled(ticket, comment.id):
                continue
            fields: dict[str, str] | None
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                fields = None
            if self._after_finish(ticket, comment):
                continue
            hooked = self._run_hooks(ticket, comment, fields)
            if hooked is not None:
                handled, outcome = hooked
                if handled:
                    if outcome.startswith("rejected-"):
                        self._wake_comment_problem(
                            ticket, comment, "rejected", outcome
                        )
                    self._mark_handled(ticket, comment.id, outcome)
                continue
            if fields is None:
                continue
            kind = fields["kind"]
            if kind == "ok" and _holds(ticket):
                outcome = self._hold_ok_outcome(ticket, comment)
                if outcome.startswith("rejected-"):
                    self._wake_comment_problem(ticket, comment, "rejected", outcome)
                self._mark_handled(ticket, comment.id, outcome)
                continue
            if kind not in (
                "go",
                "plan",
                "ready",
                "verdict",
                "questions",
                "answers",
            ):
                continue
            if self._is_stuck(ticket):
                self._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
                self._mark_handled(ticket, comment.id, "ticket-stuck")
                continue
            try:
                if kind == "go":
                    handled, outcome = self._t1(ticket, comment, fields)
                elif kind == "plan":
                    handled, outcome = self._t_plan(ticket, comment, fields)
                elif kind == "ready":
                    handled, outcome = self._t2(ticket, comment, fields)
                elif kind == "verdict":
                    verdict = fields["verdict"]
                    if verdict in ("REVISE", "APPROVE") and self._duplicate_verdict(
                        ticket, comment, fields, comments
                    ):
                        # One verdict per review job (#80): a repeat of a
                        # handled verdict starts no job, ship or wake.
                        self._mark_handled(ticket, comment.id, "duplicate-verdict")
                        continue
                    if (
                        self._review_context(ticket)[0] == "handed-off"
                        and self._ship_intent(ticket, comment.id) is None
                    ):
                        # #108: a verdict with no re-entry behind it wakes nobody.
                        self._mark_handled(ticket, comment.id, "after-handoff")
                        continue
                    if verdict == "REVISE":
                        handled, outcome = self._t3(ticket, comment, fields)
                    elif verdict == "APPROVE":
                        handled, outcome = self._t4(ticket, comment, fields)
                    else:
                        continue
                elif kind == "questions":
                    handled, outcome = self._t5_questions(ticket, comment, fields)
                else:
                    handled, outcome = self._t5_answers(ticket, comment, fields)
            except Exception as exc:
                if isinstance(exc, RetryableShipError):
                    self._discard_pending_ship_intent(
                        ticket,
                        comment.id,
                    )
                    raise
                if isinstance(exc, RetryableStateError):
                    raise
                if kind not in ("verdict", "questions", "answers"):
                    raise
                error = safe_ship_error(exc)
                eligible = _redrive_eligible(exc, ticket.factory.ship)
                if self._mark_ship_intent_failed(
                    ticket,
                    comment.id,
                    error,
                    redrive=eligible,
                ):
                    self._wake_ship_failed(
                        ticket,
                        comment,
                        f"ship raised {error}",
                    )
                    if not eligible:
                        # Issue #83: no retry is coming, so a person must look.
                        self.flag_attention(
                            ticket.repo,
                            ticket.ticket_id,
                            "ship-failed",
                            f"{ticket.factory.ship} ship failed: {error}",
                        )
                self._mark_handled(
                    ticket,
                    comment.id,
                    f"failed-{kind}-{type(exc).__name__}",
                )
                continue
            if handled:
                if outcome.startswith("rejected-"):
                    self._wake_comment_problem(ticket, comment, "rejected", outcome)
                self._mark_handled(ticket, comment.id, outcome)

    def _after_finish(self, ticket: TrackedTicket, comment: Comment) -> bool:
        """merged and done are absorbing (#108): on a finished ticket only a
        ship intent's replay acts; any other comment, a hook's included, is
        handled with no job, ship, state write or wake."""
        if (
            self._review_context(ticket)[0] not in FINISHED_STATES
            or self._ship_intent(ticket, comment.id) is not None
        ):
            return False
        self._mark_handled(ticket, comment.id, "after-merge")
        return True

    def reconcile_jobs(self) -> None:
        deferred_grok_jobs: set[str] = set()
        self._deliver_grok_jobs(deferred_grok_jobs)
        self._check_devices()
        self._check_job_states()
        now = self.queue.now()
        for job in self.queue.outstanding():
            # A queued job's last_activity_at is its queue-wait start: when
            # it was queued, or when a claim that never started was released
            # (#77 F1.2), so a release never times out a job for the wait
            # before its claim. A running job's is its last heartbeat.
            if now - job.last_activity_at >= job.request.timeout_seconds:
                if not self.queue.mark_timed_out(job) and job.state == "running":
                    self._note_timeout_held(job, now)
        for job in self.queue.unhandled_failures():
            self._t6(job)
        self._ensure_all_stuck_wakes()
        self._deliver_grok_jobs(deferred_grok_jobs)

    def _note_timeout_held(self, job: Job, now: float) -> None:
        """#87 r3 F2.1: a timed-out job whose worker may still run stays running.

        The queue refuses to time out a running job whose slot records a
        remote start, so T6 never queues attempt 2 beside it. The job
        leaves running through the dead path once its runner and worker are
        confirmed gone (then T6 retries as before), or as stuck when its
        device stays offline for an hour. This only records the hold, once
        per job, so `orch status` and the dashboard's job events show why a
        stale job has not been retried. A failure here never stops the poll.
        """
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                placement = read_placements(connection).get(job.id)
                started = placement is not None and placement.remote_started
                noted = connection.execute(
                    "SELECT 1 FROM job_events WHERE job_id = ? AND event = 'timeout-held'",
                    (job.id,),
                ).fetchone()
                row = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
                if not started or noted is not None or row is None or row[0] != "running":
                    connection.commit()
                    return
                assert placement is not None
                detail = (
                    f"no heartbeat for {int((now - job.last_activity_at) // 60)} minutes, "
                    f"past its {int(job.request.timeout_seconds // 60)}-minute timeout, "
                    f"but its worker on {placement.device} is not confirmed gone; "
                    "no retry until it is"
                )
                self._record_job_event(connection, job, "timeout-held", detail, now)
                connection.commit()
        except Exception as exc:
            self.record_poll_error(
                job.request.repo, job.request.ticket_id, f"timeout-held:{type(exc).__name__}"
            )
            return
        self._log_job_event(job, "timeout-held", detail)

    def _check_devices(self) -> None:
        """F2-02: free gone workers' slots; a device offline 60 minutes is stuck.

        Runs before T6 looks at timeouts, so a job on a device that has been
        offline for the full hour goes stuck at once instead of being retried.
        A failure here is recorded and never stops the poll.
        """
        if self.devices is None:
            return
        try:
            self.devices.reconcile(self.queue.now(), self._device_offline)
        except Exception as exc:
            self.record_poll_error("devices", "-", type(exc).__name__)

    def _check_job_states(self) -> None:
        """#75: requeue jobs claimed but never started; fail dead jobs.

        #91: a placed job whose worker never started and that no live runner
        holds is requeued the same way (lib/job_state.py unstarted_too_long,
        within the free-requeue budget), and a job whose `orch job run`
        recorded that its command died (run-died) and freed its slot is
        failed for T6 once its heartbeat is stale.

        Each running job's state comes from lib/job_state.py. A job claimed
        for more than job_states.release_claimed_seconds goes back to
        queued as the same job row (same round, same T6 attempt). A dead
        job is failed now, so T6 handles it as it would a timeout. Both
        writes are guarded, so a runner that starts or heartbeats in
        between wins. Runs after the device checks and before the timeouts.
        A failure is recorded and never stops the poll.
        """
        try:
            now = self.queue.now()
            with self._connect() as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE state = 'running' ORDER BY created_at, id"
                ).fetchall()
                placements = read_placements(connection)
                connection.commit()
        except Exception as exc:
            self.record_poll_error("job-states", "-", type(exc).__name__)
            return
        check = getattr(self.devices, "liveness", None)
        for row in rows:
            job = _job(row)
            try:
                placement = placements.get(job.id)
                facts = (
                    check(placement)
                    if check is not None
                    and placement is not None
                    and placement.runner_pid is not None
                    else Liveness()
                )
                state = derive(
                    last_activity_at=job.last_activity_at,
                    now=now,
                    placement=placement,
                    stale_seconds=self.job_states.stale_seconds,
                    liveness=facts,
                )
                if claimed_too_long(
                    state, placement, now, self.job_states.release_claimed_seconds
                ):
                    assert placement is not None
                    self._release_claimed(job, placement.placed_at, now)
                elif unstarted_too_long(
                    state,
                    placement,
                    facts,
                    last_activity_at=job.last_activity_at,
                    now=now,
                    release_claimed_seconds=self.job_states.release_claimed_seconds,
                ):
                    assert placement is not None
                    self._release_unstarted(job, placement.placed_at, placement.runner_pid, now)
                elif state == DEAD:
                    assert placement is not None
                    self._fail_dead(job, placement.runner_pid, now)
                elif placement is None and state == STALE:
                    self._fail_died_run(job, now)
            except Exception as exc:
                self.record_poll_error(
                    job.request.repo, job.request.ticket_id, f"job-states:{type(exc).__name__}"
                )

    def _release_claimed(self, job: Job, placed_at: float, now: float) -> None:
        """Requeue a job its bot claimed and never started (no round charged).

        One transaction, guarded: the job is still running with no
        heartbeat since the claim, and its slot is the same placement with
        no runner recorded. Its slot is freed; a late `orch job run` then
        finds the job queued and refuses it before anything starts. Its
        queue-wait clock (last_activity_at) restarts now, so the timeout
        sweep never fails it for time spent before the release; created_at
        is kept, so it keeps its place in line. On a finished ticket, or for
        a restored claim, the job is cancelled instead (retire_requeued, #108).
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE jobs SET state = 'queued', last_activity_at = ?
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                  AND EXISTS (
                    SELECT 1 FROM {SLOTS_TABLE}
                    WHERE job_id = ? AND placed_at = ? AND runner_pid IS NULL
                  )
                """,
                (now, job.id, job.last_activity_at, job.id, placed_at),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            connection.execute(f"DELETE FROM {SLOTS_TABLE} WHERE job_id = ?", (job.id,))
            reason = (
                f"{job.request.recipient} claimed it {int((now - placed_at) // 60)} "
                "minutes ago and never started it"
            )
            retired = retire_requeued(connection, job.id, reason, now=now)
            if retired is not None:
                event, detail = "cancelled", retired
            else:
                event, detail = "released-claimed", f"{reason}; requeued, no round charged"
                self._record_job_event(connection, job, event, detail, now)
            connection.commit()
        self._log_job_event(job, event, detail)

    def _release_unstarted(
        self, job: Job, placed_at: float, runner_pid: int | None, now: float
    ) -> None:
        """#91: requeue a placed job whose worker never started (no round charged).

        Guarded like `_release_claimed`: still running, no heartbeat since it
        was read, the same placement and runner, and still no worker pid or
        remote start. Past the free-requeue budget it is failed for T6.
        """
        if runner_pid is None:
            reason = (
                f"{job.request.recipient}'s run never recorded a runner or started a worker "
                f"and nothing has heartbeated for {int((now - job.last_activity_at) // 60)} minutes"
            )
        else:
            reason = f"runner pid {runner_pid} is gone and never started a worker"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = requeue_unstarted(
                connection,
                job.id,
                now=now,
                event=RELEASED_UNSTARTED,
                reason=reason,
                slot_guard=(
                    "placed_at = ? AND runner_pid IS ? AND worker_pid IS NULL "
                    "AND COALESCE(remote_started, 0) = 0"
                ),
                slot_params=(placed_at, runner_pid),
                last_activity_at=job.last_activity_at,
            )
            connection.commit()
        if result is not None:
            self._log_job_event(job, *result)

    def _fail_died_run(self, job: Job, now: float) -> None:
        """#91: fail a job whose `orch job run` recorded that its command died.

        The run freed the slot (its worker was confirmed gone) and the job is
        still running with a stale heartbeat. Only a RUN_DIED recorded at or
        after the last heartbeat counts: a command that exited 0 is left to
        its bot's `orch job done` (or the timeout). Guarded: still running,
        no heartbeat since it was read, and still no slot.
        """
        with self._connect() as connection:
            ended = last_run_end(connection, job.id, job.last_activity_at)
        if ended is None or ended[0] != RUN_DIED:
            return  # read only: most stale slotless jobs (Grok wakes) stop here
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                  AND NOT EXISTS (SELECT 1 FROM {SLOTS_TABLE} WHERE job_id = ?)
                """,
                (now, now, job.id, job.last_activity_at, job.id),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            detail = (
                f"orch job run's command ended ({ended[1]}) without orch job done "
                "and its slot is free; failed for T6"
            )
            self._record_job_event(connection, job, "dead", detail, now)
            connection.commit()
        self._log_job_event(job, "dead", detail)

    def _fail_dead(self, job: Job, runner_pid: int | None, now: float) -> None:
        """Fail a job whose runner and worker are gone; T6 takes it from here.

        Guarded like a timeout (no heartbeat since it was read) and on the
        same recorded runner, so a new `orch job run` that took the job
        over in between is left alone.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?
                WHERE id = ? AND state = 'running' AND last_activity_at = ?
                  AND EXISTS (
                    SELECT 1 FROM {SLOTS_TABLE} WHERE job_id = ? AND runner_pid = ?
                  )
                """,
                (now, now, job.id, job.last_activity_at, job.id, runner_pid),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            detail = f"runner pid {runner_pid} and its worker are gone; failed for T6"
            ended = last_run_end(connection, job.id, job.last_activity_at)
            if ended is not None:
                detail += f" (its command: {ended[1]})"  # #91: the exit reason
            self._record_job_event(connection, job, "dead", detail, now)
            connection.commit()
        self._log_job_event(job, "dead", detail)

    def _record_job_event(
        self,
        connection: sqlite3.Connection,
        job: Job,
        event: str,
        detail: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.request.repo.lower(),
                job.request.ticket_id,
                job.request.action,
                job.request.recipient,
                event,
                detail,
                now,
            ),
        )

    def _log_job_event(self, job: Job, event: str, detail: str) -> None:
        if self.log is None:
            return
        self.log(
            f"job {job.id[:8]} {job.request.repo}#{job.request.ticket_id} "
            f"{job.request.action}: {event}: {detail}"
        )

    def _device_offline(self, job_id: str, device: str, seconds: float) -> None:
        """Fail a job running on an offline device and mark its ticket stuck.

        One transaction: the job leaves running (failure already handled, so
        T6 never retries it) and the ticket's stuck flag wakes Grok Bot once.
        The job is never done, and its slot stays held.
        """
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None or str(row["state"]) != "running":
                connection.commit()
                return
            job = _job(row)
            connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', finished_at = ?, last_activity_at = ?,
                    failure_handled = 1
                WHERE id = ? AND state = 'running'
                """,
                (now, now, job_id),
            )
            self._set_stuck_in_transaction(
                connection,
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                source_comment_id=job.request.source_comment_id,
                timeout_seconds=job.request.timeout_seconds,
                reason="device-offline",
            )
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                detail=json.dumps(
                    {
                        "project_bot": _detail_project_bot(job.request.detail),
                        "detail": (
                            f"device {device} offline {int(seconds // 60)} minutes "
                            f"during the {job.request.action} job"
                        ),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            connection.commit()

    def _deliver_grok_jobs(self, deferred: set[str]) -> None:
        while (
            job := self.queue.claim(
                "grok",
                excluded_job_ids=deferred,
            )
        ) is not None:
            try:
                receipt = self.grok_delivery.deliver(
                    job,
                    logical_key=_grok_logical_key(job),
                )
                if not isinstance(receipt, str) or not receipt:
                    raise ValueError("Grok delivery receipt must be a non-empty string")
                self.queue.finish(
                    job.id,
                    owner="grok",
                    handoff_comment_id=receipt,
                )
            except Exception as exc:
                reason = type(exc).__name__
                if isinstance(exc, GrokDeliveryError):
                    reason = f"{reason}:{exc.detail}"
                self._record_job_error(job, f"grok-delivery:{reason}")
                try:
                    self.queue.release(
                        job.id,
                        owner="grok",
                    )
                    deferred.add(job.id)
                except Exception as release_exc:
                    self._record_job_error(
                        job,
                        f"grok-delivery-release:{type(release_exc).__name__}",
                    )

    def gate_count(self, ticket: TrackedTicket) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT gate_count FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return int(row["gate_count"]) if row is not None else 0

    def is_stuck(self, ticket: TrackedTicket) -> bool:
        return self._is_stuck(ticket)

    def wake_grok_once(
        self,
        *,
        repo: str,
        ticket_id: str,
        reason: str,
        source_comment_id: str,
        timeout_seconds: int,
        detail: str,
        head: str | None = None,
    ) -> None:
        """T7: one Grok wake per stop, across restarts.

        refused wakes once per ticket. blocked and capped wake once per
        ticket and head. unparsed and ship-failed wake once per comment.
        stuck goes through the ticket's stuck flag, which wakes once.
        """
        if reason not in GROK_WAKE_REASONS:
            raise ValueError(f"unknown wake reason {reason!r}")
        if reason in ("unparsed", "ship-failed", "rejected", "stale"):
            once_key = source_comment_id
        elif reason in ("blocked", "capped", "done", "awaiting-ok"):
            once_key = head or "-"
        else:
            once_key = "-"
        if reason == "stuck":
            self._mark_stuck_and_wake(
                repo=repo,
                ticket_id=ticket_id,
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                reason="t7-stuck",
                detail=detail,
            )
            return
        self.queue.enqueue(
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action=reason,
                recipient="grok",
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                generation=self._generation(repo, ticket_id),
                head=head,
                detail=detail,
            ),
            dedupe_key=f"t7:{_scope(repo, ticket_id)}:{reason}:{once_key}",
        )

    def _run_hooks(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str] | None,
    ) -> tuple[bool, str] | None:
        if fields is not None and fields["kind"] in CORE_KINDS:
            return None
        for hook in self.hooks:
            result = hook.handle(self, ticket, comment, fields)
            if result is not None:
                return result
        return None

    def _t1(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted(
            ticket, comment, fields, "grok-bot", role="orchestrator"
        ):
            return True, "rejected-go-author"
        if self._review_context(ticket)[0] == "handed-off":
            # #108: a handed-off ticket re-enters only through the builder's
            # ready, inside the cap; a go changes nothing.
            return True, "go-after-handoff"
        state = fields.get("state")
        action = {"planning": "plan", "building": "build"}.get(state)
        if action is not None:
            resumed = self._resume_after_cap(ticket, comment)
            if resumed is not None:
                return resumed
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if action is None:
            self.queue.enqueue(
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="clarify",
                    recipient="grok",
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    detail=self._job_detail(
                        ticket,
                        action="clarify",
                        instruction=(
                            "kind=go must set state=planning or state=building"
                        ),
                    ),
                ),
                dedupe_key=(
                    f"t1-clarify:{_scope(ticket.repo, ticket.ticket_id)}:"
                    f"g{generation}"
                ),
            )
            return True, "queued-go-clarification"
        answered = self._enqueue_or_flag(
            ticket,
            comment,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action=action,
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                detail=self._job_detail(
                    ticket,
                    action=action,
                    round_number=(
                        None
                        if action == "plan"
                        else self._review_context(ticket)[1] + 1
                    ),
                ),
            ),
            dedupe_key=(
                f"t1:{_scope(ticket.repo, ticket.ticket_id)}:{action}:"
                f"g{generation}"
            ),
        )
        self._set_workflow_state(ticket, state)
        return True, f"answered-{action}" if answered else f"queued-{action}"

    def _resume_after_cap(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        """A trusted go on a capped ticket: Max's OK for one more round (#105).

        One transaction raises cap_extra by 1, moves the ticket to fixing
        and queues the fix, keyed on the go comment so every go Max relays
        queues exactly one fix. None when the ticket is not capped.
        """
        scope = _scope(ticket.repo, ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                self._write_state_label(ticket, "fixing")
                return True, str(prior["outcome"])
            row = connection.execute(
                """
                SELECT workflow_state, review_round, cap_extra, generation
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if row is None or row["workflow_state"] != "capped":
                connection.commit()
                return None
            cap_extra = int(row["cap_extra"]) + 1
            self._enqueue_step(
                connection,
                ticket,
                comment,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="fix",
                    recipient=ticket.factory.builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(row["generation"]),
                    head=ticket.current_head,
                    detail=self._job_detail(
                        ticket,
                        action="fix",
                        head=ticket.current_head,
                        round_number=int(row["review_round"]) + 1,
                        cap=self._config_cap(ticket) + cap_extra,
                        resume=True,
                    ),
                ),
                dedupe_key=f"t1:{scope}:fix:{comment.id}",
            )
            connection.execute(
                """
                UPDATE ticket_state
                SET cap_extra = ?, workflow_state = 'fixing'
                WHERE repo = ? AND ticket_id = ?
                """,
                (cap_extra, ticket.repo.lower(), ticket.ticket_id),
            )
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, 'queued-fix')
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            )
            connection.commit()
        self.clear_attention(ticket.repo, ticket.ticket_id, "ready-after-cap")
        self._write_state_label(ticket, "fixing")
        return True, "queued-fix"

    def _over_cap(self, ticket: TrackedTicket, reported_round: int | None) -> bool:
        """A verdict round above the effective cap: no extra round covers it."""
        return reported_round is not None and reported_round > self._effective_cap(ticket)

    def _config_cap(self, ticket: TrackedTicket) -> int:
        return (
            ticket.factory.rounds_low
            if ticket.risk == "low"
            else ticket.factory.rounds_high
        )

    def _effective_cap(
        self,
        ticket: TrackedTicket,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """The ticket's review round cap: factory.md's for its risk, plus
        one per extra round Max approved (ticket_state.cap_extra)."""
        query = """
            SELECT cap_extra FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
        """
        key = (ticket.repo.lower(), ticket.ticket_id)
        if connection is None:
            with self._connect() as own:
                row = own.execute(query, key).fetchone()
        else:
            row = connection.execute(query, key).fetchone()
        return self._config_cap(ticket) + (int(row["cap_extra"]) if row else 0)

    def _t_plan(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        plan_step = self._latest_plan_step(ticket)
        if plan_step is None:
            return True, "rejected-plan-without-plan-job"
        builder_harness, generation = plan_step
        if not self._trusted(
            ticket,
            comment,
            fields,
            f"{builder_harness}-bot",
            role="builder",
        ):
            return True, "rejected-plan-author"
        ac = fields.get("ac")
        if ac not in ("met", "unmet"):
            return True, "rejected-plan-result"
        reported_round = _round_number(fields.get("round"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM plan_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                outcome = str(prior["outcome"])
                state = "stuck" if outcome == "stuck-plan-gaps" else (
                    "planning" if outcome == "queued-plan-gap" else "building"
                )
                self._set_workflow_state(ticket, "plan-check")
                self._set_workflow_state(ticket, state)
                return True, outcome
            state = connection.execute(
                """
                SELECT workflow_state, plan_gap_count, plan_check_comment_id
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            resuming = (
                state is not None
                and state["workflow_state"] == "plan-check"
                and state["plan_check_comment_id"] == comment.id
            )
            if (
                state is None
                or (state["workflow_state"] != "planning" and not resuming)
                or reported_round != int(state["plan_gap_count"]) + 1
            ):
                connection.commit()
                return True, "stale-plan"
            gap_count = int(state["plan_gap_count"])
            if not resuming:
                connection.execute(
                    """
                    UPDATE ticket_state
                    SET workflow_state = 'plan-check', plan_check_comment_id = ?
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (comment.id, ticket.repo.lower(), ticket.ticket_id),
                )
            connection.commit()
        self._write_state_label(ticket, "plan-check")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                """
                SELECT workflow_state, plan_gap_count, plan_check_comment_id,
                    review_round
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if (
                state is None
                or state["workflow_state"] != "plan-check"
                or state["plan_check_comment_id"] != comment.id
                or reported_round != int(state["plan_gap_count"]) + 1
            ):
                connection.commit()
                return True, "stale-plan"
            gap_count = int(state["plan_gap_count"])
            if ac == "met":
                next_state = "building"
                outcome = "queued-plan-build"
                request = JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="build",
                    recipient=builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    detail=self._job_detail(
                        ticket,
                        action="build",
                        round_number=int(state["review_round"]) + 1,
                        cap=self._effective_cap(ticket, connection),
                        instruction=_comment_link(comment),
                    ),
                )
                self._enqueue_step(
                    connection,
                    ticket,
                    comment,
                    request,
                    dedupe_key=(
                        f"plan-build:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{comment.id}"
                    ),
                )
            elif gap_count == 0:
                next_state = "planning"
                outcome = "queued-plan-gap"
                gap_count = 1
                self._enqueue_step(
                    connection,
                    ticket,
                    comment,
                    JobRequest(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        action="plan",
                        recipient=builder_harness,
                        source_comment_id=comment.id,
                        timeout_seconds=ticket.job_timeout_minutes * 60,
                        generation=generation,
                        detail=self._job_detail(
                            ticket,
                            action="plan",
                            instruction=(
                                "round=2/2; previous plan "
                                f"{_comment_link(comment)}"
                            ),
                        ),
                    ),
                    dedupe_key=(
                        f"plan-gap:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{comment.id}"
                    ),
                )
            else:
                next_state = "stuck"
                outcome = "stuck-plan-gaps"
                gap_count = 2
                connection.execute(
                    """
                    UPDATE ticket_state
                    SET stuck = 1, stuck_reason = 'plan-gaps-2',
                        stuck_source_comment_id = ?, timeout_seconds = ?
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (
                        comment.id,
                        ticket.job_timeout_minutes * 60,
                        ticket.repo.lower(),
                        ticket.ticket_id,
                    ),
                )
                self._ensure_stuck_wake_in_transaction(
                    connection,
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    detail=self._project_wake_detail(
                        ticket,
                        (
                            "plan has gaps a second time, "
                            f"{_comment_link(comment)}"
                        ),
                    ),
                )
            connection.execute(
                """
                UPDATE ticket_state
                SET workflow_state = ?, plan_gap_count = ?,
                    plan_check_comment_id = NULL
                WHERE repo = ? AND ticket_id = ?
                """,
                (
                    next_state,
                    gap_count,
                    ticket.repo.lower(),
                    ticket.ticket_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO plan_events (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment.id,
                    outcome,
                ),
            )
            connection.commit()
        self._write_state_label(ticket, next_state)
        return True, outcome

    def _t2(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        builder_harness = self._job_builder_harness(ticket)
        if builder_harness is None and ticket.ready_starts_review:
            # The project builds its PRs outside the queue (orch.yml
            # projects[].ready_starts_review): factory.md's builder, trusted
            # as usual below, may start review with no build job.
            builder_harness = ticket.factory.builder_harness
        if builder_harness is None:
            return True, "rejected-ready-without-builder-job"
        expected = f"{builder_harness}-bot"
        if not self._trusted(ticket, comment, fields, expected, role="builder"):
            return True, "rejected-ready-author"
        state, review_round = self._review_context(ticket)
        if state in ("planning", "plan-check"):
            return True, "rejected-ready-in-planning"
        if state == "capped":
            self._flag_ready_after_cap(ticket, comment, fields["head"])
            return True, "capped-ready"
        reported_head = fields["head"]
        reentry: int | None = None
        if state == "handed-off":
            shipped = self._shipped_approval(ticket)
            if shipped is not None and _matching_head(reported_head, shipped[0]):
                return True, "ready-already-shipped"
            reentry = (
                shipped[1]
                if shipped is not None and shipped[1] is not None
                else review_round + 1
            )
            if reentry + 1 > self._effective_cap(ticket):
                self._flag_ready_after_cap(ticket, comment, reported_head)
                return True, "ready-after-cap"
        head = _matching_head(reported_head, ticket.current_head)
        if head is None:
            try:
                fresh = self._fresh_ticket(ticket)
            except Exception:
                return False, "pr-head-unavailable"
            if fresh is not None and fresh.merged:
                # #108: the next poll's own read records the merge.
                return True, "ready-on-merged-pr"
            if fresh is not None:
                head = _matching_head(reported_head, fresh.current_head)
            if head is None:
                return True, "stale-ready-head"
            assert fresh is not None
            ticket = fresh
            self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
        result = ticket.ci.get(head) or ticket.ci.get(reported_head)
        if result is None or result.status == "pending":
            return False, "ci-pending"
        if reentry is not None:
            if not self._reenter_after_handoff(ticket, reentry):
                return True, "stale-ready"
            review_round = reentry
        timeout_seconds = ticket.job_timeout_minutes * 60
        recorded_status, count = self._record_ci_result(ticket, head, result.status)
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if recorded_status == "success":
            queued, answered = self._queue_review(
                ticket, comment, head=head, generation=generation
            )
            if not queued:
                return True, "capped-ready"
            self._set_workflow_state(ticket, "review")
            return True, "answered-review" if answered else "queued-review"
        if count >= 3:
            self._mark_stuck_and_wake(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                source_comment_id=comment.id,
                timeout_seconds=timeout_seconds,
                reason="ci-red-3",
                detail=self._project_wake_detail(
                    ticket,
                    f"CI failed three times in a row; latest head {head}",
                ),
            )
            return True, "stuck-ci-red-3"
        self._enqueue_or_flag(
            ticket,
            comment,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="fix",
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=timeout_seconds,
                generation=generation,
                head=head,
                detail=self._job_detail(
                    ticket,
                    action="fix",
                    head=head,
                    round_number=review_round + 1,
                    instruction=result.log or f"CI failed on {head}",
                ),
            ),
            dedupe_key=(
                f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}:"
                f"g{generation}"
            ),
        )
        if reentry is not None:
            self._set_workflow_state(ticket, "fixing")
        return True, "queued-ci-fix"

    def _shipped_approval(self, ticket: TrackedTicket) -> tuple[str, int | None] | None:
        """(head, round) of the APPROVE whose ship intent completed last."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT comment_id, head FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND status = 'completed'
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        approve = next(
            (item for item in ticket.comments if item.id == row["comment_id"]), None
        )
        approved_round = None
        if approve is not None:
            try:
                fields = parse_comment(approve.body).as_dict()
            except HandoffError:
                fields = {}
            approved_round = _round_number(fields.get("round"))
        return str(row["head"]), approved_round

    def _reenter_after_handoff(self, ticket: TrackedTicket, approved_round: int) -> bool:
        """#108: a trusted ready after handoff re-enters review. The approved
        round counts as completed, so the next review is approved_round + 1.
        False when the ticket left handed-off meanwhile."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE ticket_state SET review_round = ?
                WHERE repo = ? AND ticket_id = ? AND workflow_state = 'handed-off'
                """,
                (approved_round, ticket.repo.lower(), ticket.ticket_id),
            ).rowcount
            connection.commit()
        if changed == 1 and self.log is not None:
            self.log(
                f"re-entry: {ticket.repo}#{ticket.ticket_id} after handoff, "
                f"next review round {approved_round + 1}"
            )
        return changed == 1

    def _t3(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-revise-author"
        reported_round = _round_number(fields.get("round"))
        if self._over_cap(ticket, reported_round):
            return True, "rejected-over-cap"
        verdict_head = fields.get("head")
        if _matching_head(verdict_head or "", ticket.current_head) is None:
            return True, "stale-revise"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                outcome = str(prior["outcome"])
                replay_state = (
                    "capped" if outcome == "capped-review" else "fixing"
                )
                if replay_state == "capped":
                    self._wake_capped(ticket, comment)
                self._write_state_label(ticket, replay_state)
                return True, outcome
            connection.execute(
                """
                INSERT OR IGNORE INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation
                ) VALUES (?, ?, 0, 0, 1)
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
            state = connection.execute(
                """
                SELECT workflow_state, review_round, generation
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            assert state is not None
            round_number = int(state["review_round"]) + 1
            cap = self._effective_cap(ticket, connection)
            if (
                state["workflow_state"] not in ("review", "awaiting-ok")
                or reported_round != round_number
            ):
                connection.commit()
                self._wake_comment_problem(
                    ticket, comment, "stale", "stale-revise"
                )
                return True, "stale-revise"
            next_state = "capped" if round_number >= cap else "fixing"
            answered = None
            if next_state == "fixing":
                request = JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="fix",
                    recipient=ticket.factory.builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(state["generation"]),
                    head=ticket.current_head,
                    detail=self._job_detail(
                        ticket,
                        action="fix",
                        head=ticket.current_head,
                        round_number=round_number + 1,
                        cap=cap,
                        instruction=_comment_link(comment),
                    ),
                )
                _, answered = self._enqueue_step(
                    connection,
                    ticket,
                    comment,
                    request,
                    dedupe_key=(
                        f"t3:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                    ),
                )
            connection.execute(
                """
                UPDATE ticket_state
                SET review_round = ?, workflow_state = ?
                WHERE repo = ? AND ticket_id = ?
                """,
                (
                    round_number,
                    next_state,
                    ticket.repo.lower(),
                    ticket.ticket_id,
                ),
            )
            outcome = (
                "capped-review"
                if next_state == "capped"
                else "answered-fix"
                if answered is not None
                else "queued-review-fix"
            )
            connection.execute(
                """
                INSERT INTO review_events (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment.id,
                    outcome,
                ),
            )
            connection.commit()
        if next_state == "capped":
            self._wake_capped(ticket, comment)
        self._write_state_label(ticket, next_state)
        return True, outcome

    def _wake_capped(self, ticket: TrackedTicket, comment: Comment) -> None:
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="capped",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=self._project_wake_detail(
                ticket,
                f"review round cap reached, {_comment_link(comment)}",
            ),
            head=ticket.current_head,
        )

    def _t4(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-approve-author"
        current_head = ticket.current_head
        if current_head is None:
            return False, "pr-head-unavailable"
        ship = ticket.factory.ship
        intent = self._ship_intent(ticket, comment.id)
        if intent is not None:
            return self._replay_ship_intent(
                ticket,
                comment,
                current_head,
                ship,
                intent,
            )
        approved_head = fields["head"]
        state, review_round = self._review_context(ticket)
        reported_round = _round_number(fields.get("round"))
        if self._over_cap(ticket, reported_round):
            return True, "rejected-over-cap"
        if _holds(ticket) and self._superseded_by_revise(
            ticket, comment, reported_round
        ):
            return True, "superseded-approve"
        review_head = self._latest_review_head(ticket)
        if state not in ("review", "awaiting-ok") or reported_round != review_round + 1:
            self._wake_comment_problem(
                ticket, comment, "stale", "stale-approve"
            )
            return True, "stale-approve"
        if review_head is None or _matching_head(approved_head, review_head) is None:
            return True, "stale-approve"
        if not _is_full_sha(current_head):
            self._wake_ship_failed(
                ticket,
                comment,
                f"PR head {current_head!r} is not a lowercase 40-character SHA",
            )
            return True, "refused-ship-head"
        result = ticket.ci.get(current_head)
        if result is None or result.status == "pending":
            return False, "approve-ci-not-green"
        if result.status == "failure":
            held = state == "awaiting-ok" or self._held(ticket, current_head)
            return not held, "approve-ci-red"
        covered = _matching_head(approved_head, current_head) is not None
        if not covered:
            try:
                actions = self._review_actions(ticket)
                approved_patch = actions.stable_patch_id(
                    ticket,
                    approved_head,
                )
                current_patch = actions.stable_patch_id(
                    ticket,
                    current_head,
                )
            except Exception:
                return self._queue_patch_review(ticket, comment, current_head)
            covered = (
                _is_patch_id(approved_patch)
                and _is_patch_id(current_patch)
                and approved_patch.lower() == current_patch.lower()
            )
        if not covered:
            return self._queue_patch_review(ticket, comment, current_head)
        if ship == "merge" and ticket.factory.hold is not None:
            held = self._hold_gate(ticket, comment, current_head)
            if held is not None:
                return held
        intent_status, replay = self._prepare_ship_intent(
            ticket,
            comment.id,
            current_head,
            ship,
        )
        if replay:
            intent = self._ship_intent(ticket, comment.id)
            assert intent is not None
            return self._replay_ship_intent(
                ticket,
                comment,
                current_head,
                ship,
                intent,
            )
        assert intent_status == "pending"
        if ship == "merge":
            actions = self._review_actions(ticket)
            if actions.is_merged(ticket, current_head):
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                self._wake_done(ticket, comment, "merged")
                return True, "merge-confirmed"
            self._ship_head(ticket, current_head)
            self._complete_ship_intent(ticket, comment.id, "merged")
            self._write_state_label(ticket, "merged")
            self._wake_done(ticket, comment, "merged")
            return True, "merged"
        if ship == "handoff":
            try:
                note = self._review_actions(ticket).handoff(ticket, current_head)
            except AlreadyMergedShip:
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                self._wake_done(ticket, comment, "merged", note=MERGED_BEFORE_HANDOFF)
                return True, "merge-confirmed"
            self._complete_ship_intent(ticket, comment.id, "handed-off")
            self._write_state_label(ticket, "handed-off")
            self._wake_done(ticket, comment, "handed-off", note=note)
            return True, "handed-off"
        approver = ship.removeprefix("after:")
        self._review_actions(ticket).await_approver(ticket, current_head, approver)
        self._complete_ship_intent(ticket, comment.id, "waiting-external")
        self._write_state_label(ticket, "waiting-external")
        return True, "waiting-external"

    def _superseded_by_revise(
        self,
        ticket: TrackedTicket,
        approve: Comment,
        reported_round: int | None,
    ) -> bool:
        """Issue #86: a held APPROVE can wait long enough for the reviewer
        to post a REVISE for the same round at the current head. That
        REVISE replaces the APPROVE; T3 acts on it in the same pass."""
        for item in ticket.comments:
            if _comment_order(item) <= _comment_order(approve):
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            round_field = fields.get("round")
            if (
                fields["kind"] == "verdict"
                and fields.get("verdict") == "REVISE"
                and round_field is not None
                and int(round_field.split("/", 1)[0]) == reported_round
                and _matching_head(fields.get("head") or "", ticket.current_head)
                is not None
                and self._trusted_reviewer(ticket, item, fields)
            ):
                return True
        return False

    def _hold_gate(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
    ) -> tuple[bool, str] | None:
        """Issue #86: None lets T4 merge; otherwise the APPROVE stays
        unhandled and T4 re-checks it on every poll, at whatever the head
        then is. A held PR moves to awaiting-ok once per head and wakes
        Grok Bot once per head with the PR and head."""
        hold = ticket.factory.hold
        assert hold is not None
        decision = self._hold_decision(ticket, head)
        if decision.unread is not None:
            return False, "hold-unread"
        if not decision.blocks:
            return None
        scope = (ticket.repo.lower(), ticket.ticket_id)
        reasons = ",".join(decision.reasons)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head, approver, reasons FROM ship_holds
                WHERE repo = ? AND ticket_id = ?
                """,
                scope,
            ).fetchone()
        state, _round = self._review_context(ticket)
        stored = None if row is None else (row["head"], row["approver"], row["reasons"])
        if state != "awaiting-ok" or stored != (head, hold.approver, reasons):
            if state != "awaiting-ok":
                self._write_state_label(ticket, "awaiting-ok")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO ship_holds (
                        repo, ticket_id, head, approver, reasons,
                        approve_comment_id, held_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo, ticket_id) DO UPDATE SET
                        head = excluded.head,
                        approver = excluded.approver,
                        reasons = excluded.reasons,
                        approve_comment_id = excluded.approve_comment_id,
                        held_at = CASE WHEN ship_holds.head = excluded.head
                            THEN ship_holds.held_at ELSE excluded.held_at END
                    """,
                    (
                        *scope,
                        head,
                        hold.approver,
                        reasons,
                        comment.id,
                        self.queue.now(),
                    ),
                )
                connection.execute(
                    """
                    UPDATE ticket_state SET workflow_state = 'awaiting-ok'
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    scope,
                )
                connection.commit()
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="awaiting-ok",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            head=head,
            detail=json.dumps(
                {
                    "project_bot": ticket.grok_bot,
                    "outcome": "awaiting-ok",
                    "pr": ticket.ticket_url or ticket.pr_number,
                    "pr_number": ticket.pr_number,
                    "post_to": ticket.ticket_url,
                    "head": head,
                    "approver": hold.approver,
                    "reasons": list(decision.reasons),
                    "held_paths": list(decision.paths),
                    "label": hold.label,
                    "clears_with": (
                        f"<!-- orch:v1 kind=ok head={head} --> from "
                        f"{hold.approver}"
                        + (
                            f", or {hold.approver} removing {hold.label}"
                            if decision.reasons == ("label",)
                            else ""
                        )
                    ),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return False, "awaiting-ok"

    def _hold_decision(self, ticket: TrackedTicket, head: str) -> HoldDecision:
        hold = ticket.factory.hold
        assert hold is not None
        return decide(
            hold,
            labels=ticket.labels,
            changed_files=ticket.changed_files,
            label_events=ticket.label_events,
            comments=ticket.comments,
            approver_trusted=self._trusted_login(hold.approver),
            covers=lambda ok_head: self._approval_covers(ticket, ok_head, head),
        )

    def _held(self, ticket: TrackedTicket, head: str) -> bool:
        if ticket.factory.ship != "merge" or ticket.factory.hold is None:
            return False
        decision = self._hold_decision(ticket, head)
        return decision.unread is not None or bool(decision.reasons)

    def _hold_ok_outcome(self, ticket: TrackedTicket, comment: Comment) -> str:
        hold = ticket.factory.hold
        assert hold is not None
        if (
            comment.author.casefold() == hold.approver.casefold()
            and self._trusted_login(hold.approver)
        ):
            return "hold-ok"
        return "rejected-hold-ok-author"

    def _trusted_login(self, login: str) -> bool:
        wanted = login.casefold()
        return any(
            key.casefold() == wanted for key in self.trusted_logins.get("github", {})
        )

    def _ship_head(self, ticket: TrackedTicket, head: str) -> None:
        """Ship exactly head in the repo's ship_mode, or raise. No fallback."""
        if not _is_full_sha(head):
            raise ShipError(f"ship head {head!r} is not a 40-character SHA")
        mode = ship_mode(ticket.factory.ship)
        if mode == "fast-forward":
            self._review_actions(ticket).fast_forward(
                ticket,
                branch=ticket.factory.base_branch,
                head=head,
            )
        elif mode == "squash":
            self._review_actions(ticket).merge(
                ticket,
                squash=True,
                match_head_commit=head,
            )
        else:
            raise ShipError(f"unknown ship mode {mode!r}")

    def _t5_questions(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_session(ticket, comment, fields):
            return True, "rejected-questions-author"
        if fields.get("state") != "waiting-max" and (
            fields.get("role") == "orchestrator"
            or not _has_questions(parse_comment(comment.body).body)
        ):
            return True, "no-questions"
        step = self._latest_step(ticket)
        if step is None:
            action = "clarify"
            recipient = "grok"
            head = ticket.current_head
            generation = self._generation(ticket.repo, ticket.ticket_id)
        else:
            action, recipient, head, generation = step
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            waiting = connection.execute(
                """
                SELECT workflow_state, paused_question_id FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if waiting is not None and waiting["workflow_state"] == "waiting-max":
                connection.commit()
                if waiting["paused_question_id"] == comment.id:
                    self._write_state_label(ticket, "waiting-max")
                    return True, "waiting-max"
                return True, "already-waiting-max"
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation,
                    workflow_state, paused_action, paused_recipient,
                    paused_head, paused_generation, paused_question_id
                ) VALUES (?, ?, 0, 0, ?, 'waiting-max', ?, ?, ?, ?, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET
                    workflow_state = 'waiting-max',
                    paused_action = excluded.paused_action,
                    paused_recipient = excluded.paused_recipient,
                    paused_head = excluded.paused_head,
                    paused_generation = excluded.paused_generation,
                    paused_question_id = excluded.paused_question_id
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    generation,
                    action,
                    recipient,
                    head,
                    generation,
                    comment.id,
                ),
            )
            self.queue.enqueue_in_transaction(
                connection,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="clarify",
                    recipient="grok",
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    head=head,
                    detail=self._project_wake_detail(
                        ticket,
                        _comment_link(comment),
                    ),
                ),
                dedupe_key=(
                    f"t5-wait:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                ),
            )
            connection.commit()
        self._write_state_label(ticket, "waiting-max")
        return True, "waiting-max"

    def _t5_answers(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted(
            ticket, comment, fields, "grok-bot", role="orchestrator"
        ):
            return True, "rejected-answers-author"
        with self._connect() as connection:
            paused = connection.execute(
                """
                SELECT workflow_state, paused_action, paused_recipient,
                       paused_head, paused_generation, paused_question_id
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            resumed = connection.execute(
                """
                SELECT action FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND source_comment_id = ? AND dedupe_key LIKE 't5:%'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
        if (
            paused is not None
            and paused["workflow_state"] != "waiting-max"
            and resumed is not None
        ):
            resumed_state = str(paused["workflow_state"])
            self._write_state_label(ticket, resumed_state)
            return True, f"resumed-{resumed['action']}"
        if (
            paused is None
            or paused["workflow_state"] != "waiting-max"
            or paused["paused_action"] is None
        ):
            return True, "rejected-answers-without-question"
        action = str(paused["paused_action"])
        recipient = str(paused["paused_recipient"])
        generation = int(paused["paused_generation"])
        head = str(paused["paused_head"]) if paused["paused_head"] is not None else None
        request = JobRequest(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            action=action,
            recipient=recipient,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            generation=generation,
            head=head,
            detail=self._job_detail(
                ticket,
                action=action,
                head=head,
                round_number=(
                    None if action == "plan" else self._review_context(ticket)[1] + 1
                ),
                instruction=_comment_link(comment),
                resume=True,
            ),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enqueue_step(
                connection,
                ticket,
                comment,
                request,
                dedupe_key=(
                    f"t5:{_scope(ticket.repo, ticket.ticket_id)}:"
                    f"{paused['paused_question_id']}:{comment.id}"
                ),
            )
            connection.commit()
        resumed_state = _state_for_action(action)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ticket_state
                SET workflow_state = ?, paused_action = NULL,
                    paused_recipient = NULL, paused_head = NULL,
                    paused_generation = NULL, paused_question_id = NULL
                WHERE repo = ? AND ticket_id = ?
                """,
                (resumed_state, ticket.repo.lower(), ticket.ticket_id),
            )
        self._write_state_label(ticket, resumed_state)
        return True, f"resumed-{action}"

    def _trusted_reviewer(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> bool:
        return self._trusted(
            ticket,
            comment,
            fields,
            f"{ticket.factory.reviewer_harness}-bot",
            role="reviewer",
        )

    def _trusted_session(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> bool:
        del ticket
        sender = fields.get("from")
        return (
            sender is not None
            and sender in self._trusted_bots(comment)
        )

    def _latest_step(
        self,
        ticket: TrackedTicket,
    ) -> tuple[str, str, str | None, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT action, recipient, head, generation
                FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND action IN ('plan', 'build', 'review', 'fix', 'clarify')
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        return (
            str(row["action"]),
            str(row["recipient"]),
            str(row["head"]) if row["head"] is not None else None,
            int(row["generation"]),
        )

    def _queue_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        *,
        head: str,
        generation: int,
        detail: str | None = None,
    ) -> tuple[bool, Comment | None]:
        """Queue the next review round for comment, with the later trusted
        verdict that already answered it (_enqueue_step); (False, None), with
        nothing queued, past the cap."""
        _state, completed_rounds = self._review_context(ticket)
        review_round = completed_rounds + 1
        if review_round > self._effective_cap(ticket):
            self.flag_attention(
                ticket.repo,
                ticket.ticket_id,
                "ready-after-cap",
                f"review round {review_round} for head {head[:12]} is past the cap "
                f"and no extra round was approved (comment {comment.id})",
            )
            return False, None
        answered = self._enqueue_or_flag(
            ticket,
            comment,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="review",
                recipient=ticket.factory.reviewer_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                head=head,
                detail=self._job_detail(
                    ticket,
                    action="review",
                    head=head,
                    round_number=review_round,
                    instruction=detail,
                ),
            ),
            dedupe_key=(
                f"review:{_scope(ticket.repo, ticket.ticket_id)}:"
                f"{head}:r{review_round}"
            ),
        )
        return True, answered

    def _flag_ready_after_cap(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
    ) -> None:
        self.flag_attention(
            ticket.repo,
            ticket.ticket_id,
            "ready-after-cap",
            f"ready {_comment_link(comment)} for head {head[:12]} came after the "
            "review cap with no extra round approved; nothing was queued",
        )

    def _fresh_ticket(self, ticket: TrackedTicket) -> TrackedTicket | None:
        """The ticket read again, uncached, or None when the source cannot."""
        if self._fresh_reader is None:
            return None
        tracker = self._tracker(ticket.repo, ticket.ticket_id)
        if tracker is None:
            return None
        return self._fresh_reader(TicketRef(ticket.repo, ticket.ticket_id, tracker))

    def _enqueue_or_flag(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> Comment | None:
        """Enqueue comment's step; when dedupe drops the job, flag the ticket
        (issue #84). Returns the later trusted comment that already answered
        the step, which _enqueue_step cancels in the same transaction.

        A drop is benign while the job already holding the key is still
        queued or running. When that job is finished, the comment queues
        nothing and the ticket would stall silently, so it is flagged
        needs-attention instead. A job that is queued clears the flag.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job, answered = self._enqueue_step(
                connection, ticket, comment, request, dedupe_key=dedupe_key
            )
            connection.commit()
        if answered is not None:
            return answered
        if job.request.source_comment_id == request.source_comment_id:
            self.clear_attention(ticket.repo, ticket.ticket_id, "dedupe-dropped")
        elif job.state not in ("queued", "running"):
            self.flag_attention(
                ticket.repo,
                ticket.ticket_id,
                "dedupe-dropped",
                f"{request.action} for comment {request.source_comment_id} matched "
                f"finished job {job.id} ({job.state}); nothing was queued",
            )
        return None

    def _enqueue_step(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> tuple[Job, Comment | None]:
        """Enqueue the step comment asks for in the caller's open transaction,
        and cancel it in that transaction when a later trusted comment in the
        read already answered it (#108). Every plan, build, fix and review a
        comment queues goes through here, so no claim sees an answered step
        queued. Returns the job and that answer.
        """
        job = self.queue.enqueue_in_transaction(connection, request, dedupe_key=dedupe_key)
        if request.action not in _ANSWERS:
            return job, None
        answered = self._answered_by(
            ticket,
            comment,
            request.action,
            request.recipient,
            head=request.head,
            round_number=_detail_round_number(request.detail),
        )
        if answered is not None:
            self._cancel_answered(connection, job, request, answered)
        return job, answered

    def _answered_by(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        action: str,
        recipient: str,
        *,
        head: str | None = None,
        round_number: int | None = None,
    ) -> Comment | None:
        """The first later trusted orch:v1 comment that already answered the
        action job comment queues for recipient, or None (#108, #66).

        Only the answer that job's step accepts counts: a plan for a plan, a
        ready for a build or fix (at a new head when the job has one, as
        _posted_handoff reads it), and for a review a verdict naming head at
        round_number. It must come from a login trusted for recipient's bot
        and role, and not have been handled as rejected. With an intact
        database no such answer exists yet when a comment is handled; it is
        found on a replay (a lost or restored database) or when both
        comments landed while the poller was down.
        """
        kind, role = _ANSWERS[action]
        after = _comment_order(comment)
        for item in sorted(ticket.comments, key=_comment_order):
            if _comment_order(item) <= after:
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] != kind:
                continue
            if not self._trusted(ticket, item, fields, f"{recipient}-bot", role=role):
                continue
            reported = fields.get("head", "")
            if action == "review":
                if (
                    _matching_head(reported, head) is None
                    or _round_number(fields.get("round")) != round_number
                ):
                    continue
            elif head is not None and _matching_head(reported, head) is not None:
                continue
            handled = self._handled_outcome(ticket, item.id)
            if handled is not None and handled.startswith(_REJECTED_OUTCOMES):
                continue
            return item
        return None

    def _cancel_answered_jobs(self, ticket: TrackedTicket) -> None:
        """Cancel this ticket's queued steps that a later trusted comment
        already answered (#108).

        Catches a job queued before its answer was read: one restored from
        a backup taken before the builder or reviewer posted, whose
        triggering comment is already handled and so is never replayed.
        """
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        sources = {comment.id: comment for comment in ticket.comments}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
                  AND recipient != 'grok'
                  AND action IN ({", ".join("?" for _ in _ANSWERS)})
                ORDER BY created_at, id
                """,
                (ticket.repo.lower(), ticket.ticket_id, *_ANSWERS),
            ).fetchall()
        answered: list[tuple[Job, Comment]] = []
        for row in rows:
            job = _job(row)
            source = sources.get(job.request.source_comment_id)
            if source is None:
                continue
            answer = self._answered_by(
                ticket,
                source,
                job.request.action,
                job.request.recipient,
                head=job.request.head,
                round_number=_detail_round_number(job.request.detail),
            )
            if answer is not None:
                answered.append((job, answer))
        if not answered:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for job, answer in answered:
                current = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
                if current is not None and current["state"] == "queued":
                    self._cancel_answered(connection, job, job.request, answer)
            connection.commit()

    def _requeue_unanswered_jobs(self, ticket: TrackedTicket) -> None:
        """Queue again a step cancelled as answered when that answer was then
        handled as rejected, or held by a restore (state_backup.RESTORED),
        when no trusted answer stands (#108).

        The cancel relied on that answer being accepted. Without this, a
        replay that met a plan of another round, or a ready at a head the PR
        does not have, left the ticket with no runnable work. A held step
        whose source comment the read lacks is queued again: nothing shows
        it was answered.
        """
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        if self._is_stuck(ticket) or self._review_context(ticket)[0] in FINISHED_STATES:
            return
        sources = {comment.id: comment for comment in ticket.comments}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT jobs.*, (
                    SELECT detail FROM job_events
                    WHERE job_id = jobs.id AND event = 'cancelled'
                    ORDER BY id DESC LIMIT 1
                ) AS cancel_reason
                FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'cancelled'
                  AND recipient != 'grok'
                  AND action IN ({", ".join("?" for _ in _ANSWERS)})
                ORDER BY created_at, id
                """,
                (ticket.repo.lower(), ticket.ticket_id, *_ANSWERS),
            ).fetchall()
        unanswered: list[tuple[Job, str]] = []
        for row in rows:
            reason = row["cancel_reason"] or ""
            source = sources.get(row["source_comment_id"])
            if reason == RESTORED:
                requeue_reason = "restored-unanswered"
            elif reason.startswith("answered-by:") and source is not None:
                answer_id = reason.removeprefix("answered-by:")
                outcome = self._handled_outcome(ticket, answer_id)
                if outcome is None or not outcome.startswith(_REJECTED_OUTCOMES):
                    continue
                requeue_reason = f"answer-rejected:{answer_id}"
            else:
                continue
            job = _job(row)
            if source is None or self._answered_by(
                ticket,
                source,
                job.request.action,
                job.request.recipient,
                head=job.request.head,
                round_number=_detail_round_number(job.request.detail),
            ) is None:
                unanswered.append((job, requeue_reason))
        if not unanswered:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            requeued = [
                (job, reason)
                for job, reason in unanswered
                if self.queue.requeue(job.id, reason, connection=connection)
            ]
            connection.commit()
        if self.log is not None:
            for job, reason in requeued:
                self.log(
                    f"requeued {job.request.action} ({reason}) "
                    f"({ticket.repo}#{ticket.ticket_id})"
                )

    def _cancel_answered(
        self,
        connection: sqlite3.Connection,
        job: Job,
        request: JobRequest,
        answered: Comment,
    ) -> None:
        """Cancel the job this comment just queued: answered already."""
        if job.state != "queued" or job.request.source_comment_id != request.source_comment_id:
            return
        self.queue.cancel(job.id, f"answered-by:{answered.id}", connection=connection)
        if self.log is not None:
            self.log(
                f"skipped {request.action}: answered by {answered.id} "
                f"({request.repo}#{request.ticket_id})"
            )

    def flag_attention(
        self,
        repo: str,
        ticket_id: str,
        reason: str,
        detail: str,
    ) -> None:
        """Show a ticket as needs-attention in orch status and the dashboard."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo, ticket_id, reason) DO UPDATE SET
                    detail = excluded.detail, raised_at = excluded.raised_at
                """,
                (repo.lower(), ticket_id, reason, detail[:SHIP_ERROR_LIMIT], self.queue.now()),
            )
        if self.log is not None:
            self.log(f"needs attention: {repo}#{ticket_id} {reason}")

    def clear_attention(self, repo: str, ticket_id: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM needs_attention
                WHERE repo = ? AND ticket_id = ? AND reason = ?
                """,
                (repo.lower(), ticket_id, reason),
            )

    def _queue_patch_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        current_head: str,
    ) -> tuple[bool, str]:
        queued, _answered = self._queue_review(
            ticket,
            comment,
            head=current_head,
            generation=self._generation(ticket.repo, ticket.ticket_id),
            detail=f"Approval does not cover current head {current_head}",
        )
        if not queued:
            return True, "capped-patch-review"
        self._set_workflow_state(ticket, "review")
        return True, "queued-patch-review"

    def _review_context(self, ticket: TrackedTicket) -> tuple[str | None, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT workflow_state, review_round FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None, 0
        state = (
            str(row["workflow_state"])
            if row["workflow_state"] is not None
            else None
        )
        return state, int(row["review_round"])

    def _latest_review_head(self, ticket: TrackedTicket) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None or row["head"] is None:
            return None
        return str(row["head"])

    def _ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> tuple[str, str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head, policy, status FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["head"]), str(row["policy"]), str(row["status"])

    def _replay_ship_intent(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        current_head: str,
        policy: str,
        intent: tuple[str, str, str],
    ) -> tuple[bool, str]:
        intent_head, intent_policy, status = intent
        if intent_head != current_head or intent_policy != policy:
            raise RuntimeError("ship intent does not match approval")
        if status == "completed":
            merged = self._review_context(ticket)[0] == "merged"
            self._write_state_label(ticket, "merged" if merged else _ship_state(policy))
            return True, f"ship-already-completed-{policy}"
        if status == "pending":
            if policy == "merge" and self._review_actions(ticket).is_merged(
                ticket,
                current_head,
            ):
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                return True, "merge-confirmed"
            self._hold_ship_intent(ticket, comment.id)
            self._wake_ship_failed(ticket, comment, f"{policy} ship is unconfirmed")
            return True, "ship-unconfirmed"
        if status == "unconfirmed":
            self._wake_ship_failed(ticket, comment, f"{policy} ship is unconfirmed")
            return True, "ship-unconfirmed"
        if status == "failed":
            self._wake_ship_failed(ticket, comment, f"{policy} ship failed")
            return True, "ship-failed"
        raise RuntimeError(f"unknown ship intent status {status!r}")

    def _prepare_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        head: str,
        policy: str,
    ) -> tuple[str, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT head, policy, status FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
            if existing is not None:
                if existing["head"] != head or existing["policy"] != policy:
                    raise RuntimeError("ship intent does not match approval")
                connection.commit()
                return str(existing["status"]), True
            connection.execute(
                """
                INSERT INTO ship_intents (
                    repo, ticket_id, comment_id, head, policy, status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment_id,
                    head,
                    policy,
                ),
            )
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = 'ship'
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
            connection.commit()
        return "pending", False

    def _hold_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents SET status = 'unconfirmed'
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            )
            connection.execute(
                f"""
                UPDATE ticket_state SET workflow_state = 'ship'
                WHERE repo = ? AND ticket_id = ? AND {_NOT_FINISHED}
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
            connection.commit()

    def _complete_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        state: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents
                SET status = 'completed', error = NULL
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            )
            self._enter_ship_state(connection, ticket.repo, ticket.ticket_id, state)
            connection.commit()
        self.clear_attention(ticket.repo, ticket.ticket_id, "ship-failed")

    def _mark_ship_intent_failed(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        error: str,
        *,
        redrive: bool = False,
    ) -> bool:
        """pending -> failed. A handoff that failed before any confirmed
        write, or a merge that failed with a retryable error (issue #83), is
        marked redrive='eligible' for one re-drive."""
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE ship_intents
                SET status = 'failed', error = ?,
                    redrive = CASE
                        WHEN ? AND policy IN ('handoff', 'merge') THEN 'eligible'
                        ELSE NULL
                    END
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (
                    error,
                    1 if redrive else 0,
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment_id,
                ),
            ).rowcount
        return changed == 1

    def _redrive_handoffs(self, ticket: TrackedTicket) -> None:
        """Re-drive, once, a ship that failed in a way that may be retried.

        A handoff that failed before any confirmed write, or (issue #83) a
        merge that failed with a retryable error. For a merge the re-drive
        first checks whether the PR is already merged at the head, and then
        ships through the same path as T4 (which marks a draft PR ready
        before the squash merge). A re-drive that fails again flags the
        ticket needs-attention: no further retry is made.

        The intent's comment must still be the APPROVE in force: the latest
        trusted reviewer verdict on the ticket, picked by the same rule the
        live path uses (_latest_review_verdict). A newer trusted verdict, a
        REVISE or another APPROVE, spends the re-drive with no write, so of
        several eligible intents on one ticket only the latest APPROVE can
        hand off. That APPROVE's head field must also still cover the
        intent's head, by T4's rule (_approval_covers), so an APPROVE
        edited in place to name another head spends the re-drive. A ticket
        already merged, handed off or done, or one with a completed handoff
        intent at the intent's head, is never handed off again: its
        re-drives are spent with no write.

        Then it checks what else can have changed since T4: the ship policy
        is still handoff, the PR head is still the intent's head and CI on
        it is still green. A moved head or red CI spends the re-drive with
        no write. Pending CI or an unread head waits for a later poll.

        The re-drive is claimed (eligible -> spent) before the adapter
        runs, so a crash mid-handoff never repeats it. A retryable error
        hands the claim back. Any other error keeps the intent failed and
        records its safe detail; the ship-failed wake is once per comment,
        so Grok keeps the wake from the first failure. Success completes
        the intent, wakes done and lets T4 replay the state label.
        """
        policy = ticket.factory.ship
        if policy not in ("handoff", "merge"):
            return
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT comment_id, head FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND policy = ?
                  AND status = 'failed' AND redrive = 'eligible'
                ORDER BY comment_id
                """,
                (ticket.repo.lower(), ticket.ticket_id, policy),
            ).fetchall()
        if not rows:
            return
        by_id = {comment.id: comment for comment in ticket.comments}
        in_force = self._latest_review_verdict(ticket)
        approve_in_force = (
            in_force
            if in_force is not None and in_force[1].get("verdict") == "APPROVE"
            else None
        )
        for row in rows:
            comment_id = str(row["comment_id"])
            comment = by_id.get(comment_id)
            if comment is None:
                continue
            if (
                approve_in_force is None
                or comment_id != approve_in_force[0].id
                or self._review_context(ticket)[0] in ("merged", "handed-off", "done")
                or self._has_completed_ship_at(ticket, str(row["head"]), policy)
                or not self._approval_covers(
                    ticket, approve_in_force[1].get("head", ""), str(row["head"])
                )
            ):
                self._set_redrive(ticket, comment_id, "spent", "eligible")
                continue
            head = ticket.current_head
            if head is None:
                continue
            if head != row["head"]:
                self._set_redrive(ticket, comment_id, "spent", "eligible")
                continue
            ci = ticket.ci.get(head)
            if ci is None or ci.status == "pending":
                continue
            if ci.status != "success":
                self._set_redrive(ticket, comment_id, "spent", "eligible")
                continue
            if policy == "merge" and self._held(ticket, head):
                # Issue #86 on the #83 re-drive: a hold that applies at this
                # head (a label put back after the OK, or an unread hold
                # input) keeps the merge waiting; the re-drive is kept.
                continue
            if not self._set_redrive(ticket, comment_id, "spent", "eligible"):
                continue
            try:
                if policy == "handoff":
                    try:
                        note = self._review_actions(ticket).handoff(ticket, head)
                    except AlreadyMergedShip:
                        self._complete_redrive(ticket, comment_id, "merged")
                        self._wake_done(
                            ticket, comment, "merged", note=MERGED_BEFORE_HANDOFF
                        )
                        continue
                else:
                    note = None
                    if not self._review_actions(ticket).is_merged(ticket, head):
                        self._ship_head(ticket, head)
            except RetryableStateError:
                self._set_redrive(ticket, comment_id, "eligible", "spent")
                raise
            except Exception as exc:
                error = safe_ship_error(exc)
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE ship_intents SET error = ?
                        WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                          AND status = 'failed'
                        """,
                        (error, ticket.repo.lower(), ticket.ticket_id, comment_id),
                    )
                self._wake_ship_failed(
                    ticket,
                    comment,
                    f"{policy} re-drive raised {error}",
                )
                self.flag_attention(
                    ticket.repo,
                    ticket.ticket_id,
                    "ship-failed",
                    f"{policy} ship failed again on its one retry: {error}",
                )
                continue
            state = _ship_state(policy)
            self._complete_redrive(ticket, comment_id, state)
            self._wake_done(ticket, comment, state, note=note)

    def _approval_covers(
        self,
        ticket: TrackedTicket,
        approved_head: str,
        head: str,
    ) -> bool:
        """Whether an APPROVE naming approved_head covers head, by T4's rule:
        the same commit (a 7+ character prefix either way) or the same
        stable patch id. A patch id that cannot be read does not cover."""
        if _matching_head(approved_head, head) is not None:
            return True
        try:
            actions = self._review_actions(ticket)
            approved_patch = actions.stable_patch_id(ticket, approved_head)
            current_patch = actions.stable_patch_id(ticket, head)
        except Exception:
            return False
        return (
            _is_patch_id(approved_patch)
            and _is_patch_id(current_patch)
            and approved_patch.lower() == current_patch.lower()
        )

    def _complete_redrive(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        state: str = "handed-off",
    ) -> None:
        """failed -> completed, and hand the APPROVE back to T4.

        T4 then finds the completed intent and replays its label write, as
        it does after any completed ship, retrying it on a later poll if the
        write fails. One transaction, so a crash leaves neither half.
        """
        scope = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents SET status = 'completed', error = NULL
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'failed'
                """,
                (*scope, comment_id),
            )
            self._enter_ship_state(connection, ticket.repo, ticket.ticket_id, state)
            connection.execute(
                """
                DELETE FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*scope, comment_id),
            )
            connection.commit()
        self.clear_attention(ticket.repo, ticket.ticket_id, "ship-failed")

    def _enter_ship_state(
        self,
        connection: sqlite3.Connection,
        repo: str,
        ticket_id: str,
        state: str,
    ) -> None:
        """A completed ship's state: merged goes through _enter_merged; any
        other ship state never overwrites merged or done."""
        if state == "merged":
            self._enter_merged(connection, repo, ticket_id, "shipped")
            return
        connection.execute(
            f"""
            UPDATE ticket_state SET workflow_state = ?
            WHERE repo = ? AND ticket_id = ? AND {_NOT_FINISHED}
            """,
            (state, repo.lower(), ticket_id),
        )

    def _has_completed_ship_at(
        self,
        ticket: TrackedTicket,
        head: str,
        policy: str = "handoff",
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND policy = ?
                  AND head = ? AND status = 'completed'
                """,
                (ticket.repo.lower(), ticket.ticket_id, policy, head),
            ).fetchone()
        return row is not None

    def _set_redrive(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        value: str,
        expected: str,
    ) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE ship_intents SET redrive = ?
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'failed' AND redrive = ?
                """,
                (value, ticket.repo.lower(), ticket.ticket_id, comment_id, expected),
            ).rowcount
        return changed == 1

    def _discard_pending_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                DELETE FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).rowcount
            if changed == 1:
                connection.execute(
                    """
                    UPDATE ticket_state SET workflow_state = 'review'
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            connection.commit()

    def _wake_ship_failed(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        detail: str,
    ) -> None:
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="ship-failed",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=self._project_wake_detail(ticket, detail),
        )

    def _has_completed_ship_intent(
        self,
        repo: str,
        ticket_id: str,
        comment_id: str,
        head: str,
        policy: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND head = ? AND policy = ?
                  AND status = 'completed'
                """,
                (repo.lower(), ticket_id, comment_id, head, policy),
            ).fetchone()
        return row is not None

    def _latest_review_verdict(
        self,
        ticket: TrackedTicket,
    ) -> tuple[Comment, dict[str, str]] | None:
        reviewer = f"{ticket.factory.reviewer_harness}-bot"
        latest: tuple[Comment, dict[str, str]] | None = None
        for item in sorted(ticket.comments, key=_comment_order):
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] != "verdict":
                continue
            if not self._trusted(
                ticket, item, fields, reviewer, role="reviewer"
            ):
                continue
            latest = (item, fields)
        return latest

    def _tracker(self, repo: str, ticket_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT tracker FROM tracked_tickets
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo.lower(), ticket_id),
            ).fetchone()
        return str(row["tracker"]) if row is not None else None

    def _prepare_external_ship(self, request: ShipRequest) -> tuple[str, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT head, status FROM external_ship_intents
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                """,
                (request.repo.lower(), request.ticket_id, request.source_comment_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                if existing["head"] != request.head:
                    raise ShipError("ship intent does not match the request head")
                return str(existing["status"]), False
            connection.execute(
                """
                INSERT INTO external_ship_intents (
                    repo, ticket_id, source_comment_id, head, status
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    request.repo.lower(),
                    request.ticket_id,
                    request.source_comment_id,
                    request.head,
                ),
            )
            connection.commit()
        return "pending", True

    def _finish_external_ship(
        self,
        request: ShipRequest,
        status: str,
        error: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE external_ship_intents SET status = ?, error = ?
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                  AND status = 'pending'
                """,
                (
                    status,
                    error,
                    request.repo.lower(),
                    request.ticket_id,
                    request.source_comment_id,
                ),
            )
            connection.commit()

    def _settle_external_ship(
        self,
        ticket: TrackedTicket,
        source_comment_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enter_merged(connection, ticket.repo, ticket.ticket_id, "shipped")
            connection.commit()
        self._write_state_label(ticket, "merged")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE external_ship_intents SET settled = 1
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, source_comment_id),
            )

    def _settle_external_ships(self, ticket: TrackedTicket) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_comment_id FROM external_ship_intents
                WHERE repo = ? AND ticket_id = ?
                  AND status = 'completed' AND settled = 0
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        for row in rows:
            self._try_settle_external_ship(ticket, str(row["source_comment_id"]))

    def _try_settle_external_ship(
        self,
        ticket: TrackedTicket,
        source_comment_id: str,
    ) -> None:
        try:
            self._settle_external_ship(ticket, source_comment_id)
        except Exception as exc:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE external_ship_intents
                    SET settle_failures = settle_failures + 1
                    WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, source_comment_id),
                )
                failures = int(
                    connection.execute(
                        """
                        SELECT settle_failures FROM external_ship_intents
                        WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                        """,
                        (ticket.repo.lower(), ticket.ticket_id, source_comment_id),
                    ).fetchone()[0]
                )
            if failures >= SETTLE_FAILURES_TO_WAKE:
                self.wake_grok_once(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    reason="ship-failed",
                    source_comment_id=source_comment_id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    detail=self._project_wake_detail(
                        ticket,
                        (
                            f"merged, but the merged state or label write failed "
                            f"{failures} times: {type(exc).__name__}: {exc}"
                        ),
                    ),
                )

    def _write_state_label(self, ticket: TrackedTicket, state: str) -> None:
        try:
            self._review_actions(ticket).set_state(ticket, state)
        except Exception as exc:
            raise RetryableStateError(state) from exc

    def _set_workflow_state(
        self,
        ticket: TrackedTicket,
        state: str,
    ) -> None:
        if self._review_context(ticket)[0] in FINISHED_STATES:
            return
        self._write_state_label(ticket, state)
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, workflow_state
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET workflow_state = excluded.workflow_state
                WHERE {_NOT_FINISHED.replace("workflow_state", "ticket_state.workflow_state")}
                """,
                (ticket.repo.lower(), ticket.ticket_id, state),
            )

    def _job_builder_harness(self, ticket: TrackedTicket) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recipient FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND action IN ('plan', 'build', 'fix')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["recipient"])

    def _latest_plan_step(self, ticket: TrackedTicket) -> tuple[str, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recipient, generation FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'plan'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["recipient"]), int(row["generation"])

    def _t6(self, job: Job) -> None:
        if job.request.action == "stuck":
            self.queue.mark_failure_handled(job.id)
            return
        if job.request.recipient != "grok" and self._finished_identity(
            job.request.repo, job.request.ticket_id
        ):
            # #108: a running job left alone at the merge fails into a
            # finished ticket: no retry, no stuck.
            self._record_job_event_once(
                job, "after-merge", "the ticket is finished; no T6 retry"
            )
            self.queue.mark_failure_handled(job.id)
            return
        if self._is_stuck_identity(job.request.repo, job.request.ticket_id):
            self._ensure_stuck_wake(job.request.repo, job.request.ticket_id)
            self.queue.mark_failure_handled(job.id)
            return
        # #91 (PR #88), #105: the job may have posted its handoff (a review's
        # verdict, a build or fix's ready) and then failed. That handoff
        # stands: done, never retried.
        handoff = self._posted_handoff(job)
        if handoff is _HANDOFF_UNREAD:
            self._record_job_event_once(
                job,
                "done-deferred",
                "a posted builder ready's head is not yet checked against the "
                "PR head; T6 waits for T2 to route it or call it stale",
            )
            return
        if handoff is not None:
            self._done_from_handoff(job, handoff)
            return
        if self._restored_failure_waits(job):
            return
        if job.request.attempt == 1:
            if job.request.action == "review" and self._review_retry_waits(job):
                return
            generation = job.request.generation + 1
            request = JobRequest(
                repo=job.request.repo,
                ticket_id=job.request.ticket_id,
                action=job.request.action,
                recipient=job.request.recipient,
                source_comment_id=job.request.source_comment_id,
                timeout_seconds=job.request.timeout_seconds,
                generation=generation,
                attempt=2,
                head=job.request.head,
                detail=_retry_detail(job),
            )
            self.queue.enqueue_retry(
                job.id,
                request,
                dedupe_key=(
                    f"t6-retry:{job.id}:g{generation}"
                ),
            )
            return
        self._mark_stuck_and_wake(
            repo=job.request.repo,
            ticket_id=job.request.ticket_id,
            source_comment_id=job.request.source_comment_id,
            timeout_seconds=job.request.timeout_seconds,
            reason="job-failed-2",
            detail=json.dumps(
                {
                    "project_bot": _detail_project_bot(job.request.detail),
                    "detail": f"{job.request.action} job failed twice",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        self.queue.mark_failure_handled(job.id)

    def _restored_failure_waits(self, job: Job) -> bool:
        """#108: settle a failed job restore() marked against its thread
        before T6 retries it or calls the ticket stuck.

        The backup may predate the job's answer. Until a poll reads the
        ticket, T6 waits. A read that shows the job's step answered marks it
        done with that answer. True when T6 does no more with the job now.
        """
        with self._connect() as connection:
            if not restored_claim(connection, job.id):
                return False
        ticket = self._polled.get((job.request.repo.lower(), job.request.ticket_id))
        if ticket is None:
            self._record_job_event_once(
                job,
                "restore-deferred",
                "restored from a backup that may predate its answer; T6 waits "
                "for a poll to read the thread",
            )
            return True
        source = next(
            (item for item in ticket.comments if item.id == job.request.source_comment_id),
            None,
        )
        if source is None:
            return False
        answer = self._answered_by(
            ticket,
            source,
            job.request.action,
            job.request.recipient,
            head=job.request.head,
            round_number=_detail_round_number(job.request.detail),
        )
        if answer is None:
            return False
        self._done_from_handoff(job, answer)
        return True

    def _posted_handoff(self, job: Job) -> Comment | object | None:
        """The handoff a failed review, build or fix job already posted.

        Read from the ticket this poll fetched (None when it fetched none),
        posted no earlier than the job was queued, from a login trusted for
        its `from` and `role`, and not already rejected when it was handled
        (stale, superseded, ...).

        A review's is a reviewer verdict for the job's head (as a prefix,
        like every head compare) and round; a review job with no round or no
        head takes none. A build or fix job's is a builder ready from the
        job's recipient at a new head: one other than the job's own, or any
        head for a job that has none. That ready counts once T2 routed it,
        or while it waits on CI at the polled PR head. One T2 left unhandled
        at another head (its fresh PR read failed) may yet be stale:
        _HANDOFF_UNREAD when no counted handoff comes first.
        """
        ticket = self._polled.get((job.request.repo.lower(), job.request.ticket_id))
        if ticket is None:
            return None
        action = job.request.action
        if action == "review":
            round_number = _detail_round_number(job.request.detail)
            if job.request.head is None or round_number is None:
                return None
            kind, bot, role = "verdict", f"{ticket.factory.reviewer_harness}-bot", "reviewer"
        elif action in ("build", "fix"):
            round_number = None
            kind, bot, role = "ready", f"{job.request.recipient}-bot", "builder"
        else:
            return None
        for item in sorted(ticket.comments, key=_comment_order):
            if item.created_at < job.created_at:
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            if fields.get("kind") != kind:
                continue
            if not self._trusted(ticket, item, fields, bot, role=role):
                continue
            head = fields.get("head") or ""
            if kind == "verdict":
                if _matching_head(head, job.request.head) is None:
                    continue
                if _round_number(fields.get("round")) != round_number:
                    continue
            elif job.request.head is not None and _matching_head(head, job.request.head):
                continue
            outcome = self._handled_outcome(ticket, item.id)
            if outcome is not None and outcome.startswith(_REJECTED_OUTCOMES):
                continue
            if (
                kind == "ready"
                and outcome is None
                and _matching_head(head, ticket.current_head) is None
            ):
                return _HANDOFF_UNREAD
            return item
        return None

    def _done_from_handoff(self, job: Job, handoff: Comment) -> None:
        """Mark a failed job done with the handoff it already posted."""
        kind = _ANSWERS[job.request.action][0]
        event = f"done-from-{kind}"
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET state = 'done', handoff_comment_id = ?, failure_handled = 1
                WHERE id = ? AND state = 'failed' AND failure_handled = 0
                """,
                (handoff.id, job.id),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            where = "for" if kind == "verdict" else "after"
            detail = (
                f"{kind} comment {handoff.id} {where} head {(job.request.head or 'none')[:12]} "
                "was already posted; marked done with it, no T6 retry"
            )
            self._record_job_event(connection, job, event, detail, now)
            connection.commit()
        self._log_job_event(job, event, detail)

    def _open_fix_for_head(self, job: Job) -> bool:
        """A fix job on the same ticket for this job's head that is still open.

        Open is queued, running, or failed with its T6 handling still to
        come (PR #99 r3): T6 handles failures oldest first, so a review that
        failed before its fix would otherwise be retried in the same pass
        that queues the fix's retry, and both would run on the same head.
        """
        if job.request.head is None:
            return False
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT head FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'fix'
                  AND head IS NOT NULL
                  AND (state IN ('queued', 'running')
                       OR (state = 'failed' AND failure_handled = 0))
                """,
                (job.request.repo.lower(), job.request.ticket_id),
            ).fetchall()
        return any(
            _matching_head(str(row["head"]), job.request.head) is not None for row in rows
        )

    def _review_retry_waits(self, job: Job) -> bool:
        """#91: hold a failed review's T6 retry while a fix for its head is open.

        Open includes a failed fix awaiting its own T6 retry. The retry is
        deferred, not dropped: the failure stays unhandled and is looked at
        again every poll. Once no fix is open, the retry goes ahead if the
        ticket still has this head (a fix that changed nothing and readied
        the same head needs it: that ready's review dedupes onto this failed
        job), and is dropped for good once the head was replaced.
        True when the retry is not queued now.
        """
        if self._open_fix_for_head(job):
            self._record_job_event_once(
                job,
                "retry-deferred",
                f"a fix job for head {(job.request.head or '')[:12]} is open; "
                "T6 retry review waits for it",
            )
            return True
        if not self._job_event_recorded(job, "retry-deferred"):
            return False
        ticket = self._polled.get((job.request.repo.lower(), job.request.ticket_id))
        if ticket is None:
            return True  # the head is not known this poll; look again next poll
        if _matching_head(job.request.head or "", ticket.current_head) is not None:
            return False
        self._skip_review_retry(job, ticket.current_head)
        return True

    def _job_event_recorded(self, job: Job, event: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM job_events WHERE job_id = ? AND event = ? LIMIT 1",
                (job.id, event),
            ).fetchone()
        return row is not None

    def _record_job_event_once(self, job: Job, event: str, detail: str) -> None:
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            seen = connection.execute(
                "SELECT 1 FROM job_events WHERE job_id = ? AND event = ? LIMIT 1",
                (job.id, event),
            ).fetchone()
            if seen is None:
                self._record_job_event(connection, job, event, detail, now)
            connection.commit()
        if seen is None:
            self._log_job_event(job, event, detail)

    def _skip_review_retry(self, job: Job, current_head: str | None) -> None:
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET failure_handled = 1
                WHERE id = ? AND state = 'failed' AND failure_handled = 0
                """,
                (job.id,),
            ).rowcount
            if changed != 1:
                connection.commit()
                return
            detail = (
                f"head {(job.request.head or '')[:12]} was replaced by "
                f"{(current_head or 'none')[:12]} while its fix ran; no T6 retry review"
            )
            self._record_job_event(connection, job, "retry-skipped", detail, now)
            connection.commit()
        self._log_job_event(job, "retry-skipped", detail)

    def _mark_stuck_and_wake(
        self,
        *,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        timeout_seconds: int,
        reason: str,
        detail: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._set_stuck_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                reason=reason,
            )
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=detail,
            )
            connection.commit()

    def _set_stuck_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        timeout_seconds: int,
        reason: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ticket_state (
                repo, ticket_id, gate_count, stuck, stuck_reason,
                stuck_source_comment_id, timeout_seconds, generation
            ) VALUES (?, ?, 0, 1, ?, ?, ?, 1)
            ON CONFLICT(repo, ticket_id)
            DO UPDATE SET
                stuck = 1,
                stuck_reason = excluded.stuck_reason,
                stuck_source_comment_id = excluded.stuck_source_comment_id,
                timeout_seconds = excluded.timeout_seconds
            WHERE ticket_state.workflow_state IS NULL
               OR ticket_state.workflow_state NOT IN ('merged', 'done')
            """,
            (
                repo.lower(),
                ticket_id,
                reason,
                source_comment_id,
                timeout_seconds,
            ),
        )

    def _discover(
        self,
        candidates: tuple[TrackedTicket, ...],
        *,
        tracker: str,
    ) -> None:
        for ticket in candidates:
            if ticket.factory.tickets != tracker:
                continue
            for item in sorted(
                ticket.comments,
                key=lambda value: (value.created_at, value.id),
            ):
                try:
                    fields = parse_comment(item.body).as_dict()
                except HandoffError:
                    continue
                if not self._starts_tracking(ticket, item, fields):
                    continue
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO tracked_tickets (
                            repo, ticket_id, tracker, tracked_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            ticket.repo.lower(),
                            ticket.ticket_id,
                            tracker,
                            self.queue.now(),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO ticket_state (
                            repo, ticket_id, gate_count, stuck, generation
                        ) VALUES (?, ?, 0, 0, 1)
                        """,
                        (ticket.repo.lower(), ticket.ticket_id),
                    )
                break

    def _starts_tracking(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> bool:
        """A trusted Grok brief/go, or (ready_starts_review) the builder's ready.

        The ready path applies T2's own trust rule (factory.md's builder,
        role=builder, a trusted_logins author) and needs the ready head to
        name the PR head, so T2 can then queue the review in the same cycle.
        An unlabelled PR admitted through the issue #59 ready exception
        starts tracking only that way: its Grok brief or go still needs the
        orch:state/ label.
        """
        if fields["kind"] in ("brief", "go"):
            if ticket.admitted_by_ready:
                return False
            return self._trusted(
                ticket,
                comment,
                fields,
                "grok-bot",
                role="orchestrator",
            )
        if fields["kind"] != "ready" or not ticket.ready_starts_review:
            return False
        return (
            self._trusted(
                ticket,
                comment,
                fields,
                f"{ticket.factory.builder_harness}-bot",
                role="builder",
            )
            and _matching_head(fields["head"], ticket.current_head) is not None
        )

    def _tracked_refs(self, cycle: int | None = None) -> tuple[TicketRef, ...]:
        """The tracked tickets poll_once reads (#108).

        Every ticket that is not merged, done or abandoned, plus a merged or
        handed-off one with a completed ship intent left to replay or an
        external ship left to settle. A handed-off ticket stays polled, so
        its merge is seen and a trusted ready may re-enter review.

        With cycle, a ticket in a stopped state (handed-off, capped, blocked,
        refused, or stuck) with nothing to replay is offered only once its
        turn came: once every STOPPED_POLL_SECONDS, spread across cycles by a
        hash of its identity. It stays offered until a read reaches it (a
        ticket or a poll error), so a cycle that reads no Linear, or defers
        the read, only delays it. Every other ticket is offered every cycle.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repo, ticket_id, tracker, workflow_state, stuck, replay,
                       unsettled
                FROM (
                    SELECT tracked.repo, tracked.ticket_id, tracked.tracker,
                           state.workflow_state,
                           COALESCE(state.stuck, 0) AS stuck,
                           EXISTS (
                               SELECT 1
                               FROM ship_intents AS ship
                               LEFT JOIN handled_comments AS handled
                                 ON handled.repo = ship.repo
                                AND handled.ticket_id = ship.ticket_id
                                AND handled.comment_id = ship.comment_id
                               WHERE ship.repo = tracked.repo
                                 AND ship.ticket_id = tracked.ticket_id
                                 AND ship.status = 'completed'
                                 AND handled.comment_id IS NULL
                           ) AS replay,
                           EXISTS (
                               SELECT 1
                               FROM external_ship_intents AS external
                               WHERE external.repo = tracked.repo
                                 AND external.ticket_id = tracked.ticket_id
                                 AND external.settled = 0
                           ) AS unsettled
                    FROM tracked_tickets AS tracked
                    LEFT JOIN ticket_state AS state
                      ON state.repo = tracked.repo
                     AND state.ticket_id = tracked.ticket_id
                )
                WHERE workflow_state IS NULL
                   OR workflow_state NOT IN ('merged', 'done', 'abandoned')
                   OR (workflow_state = 'merged' AND (replay OR unsettled))
                ORDER BY repo, ticket_id
                """
            ).fetchall()
        every = max(1, STOPPED_POLL_SECONDS // max(1, self._poll_seconds))
        refs: list[TicketRef] = []
        stopped_keys: set[tuple[str, str]] = set()
        for row in rows:
            state = row["workflow_state"]
            catching_up = bool(row["replay"]) or bool(row["unsettled"])
            stopped = bool(row["stuck"]) or state in STOPPED_STATES
            if cycle is not None and stopped and not catching_up:
                key = (str(row["repo"]).lower(), str(row["ticket_id"]))
                stopped_keys.add(key)
                if (cycle + _spread(row["repo"], row["ticket_id"])) % every == 0:
                    self._stopped_due.add(key)
                if key not in self._stopped_due:
                    continue
            refs.append(
                TicketRef(
                    repo=str(row["repo"]),
                    ticket_id=str(row["ticket_id"]),
                    tracker=str(row["tracker"]),
                    allow_merged=state == "merged"
                    or (state == "handed-off" and bool(row["replay"])),
                )
            )
        if cycle is not None:
            self._stopped_due &= stopped_keys
        return tuple(refs)

    def active_job_tickets(self) -> frozenset[tuple[str, str]]:
        """(repo, ticket) pairs with a queued or running job, repo lowercased.

        Only the Linear webhook poller reads this: those tickets are reread
        in full every cycle (docs/SPEC.md section 7).
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT repo, ticket_id
                FROM jobs
                WHERE state IN ('queued', 'running')
                """
            ).fetchall()
        return frozenset(
            (str(row["repo"]).lower(), str(row["ticket_id"])) for row in rows
        )

    def unpolled_linear_tickets(self) -> frozenset[str]:
        """Tracked Linear tickets that poll_once no longer reads.

        Merged, done or abandoned with nothing left to replay or settle
        (_tracked_refs); a handed-off ticket is still polled (#108). Only the Linear webhook poller reads this, to ack
        their dirty rows unread (docs/SPEC.md section 7).
        """
        polled = {
            ref.ticket_id for ref in self._tracked_refs() if ref.tracker == "linear"
        }
        return self._tracked_linear_ids() - polled

    def _tracked_github_ids(self) -> frozenset[tuple[str, str]]:
        """(repo, PR number) of every tracked GitHub ticket, repo lowercased."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT repo, ticket_id FROM tracked_tickets WHERE tracker = 'github'"
            ).fetchall()
        return frozenset(
            (str(row["repo"]).lower(), str(row["ticket_id"])) for row in rows
        )

    def _tracked_linear_ids(self) -> frozenset[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ticket_id
                FROM tracked_tickets
                WHERE tracker = 'linear'
                """
            ).fetchall()
        return frozenset(str(row["ticket_id"]) for row in rows)

    def _trusted(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
        expected_bot: str,
        *,
        role: str,
    ) -> bool:
        del ticket
        return (
            fields.get("from") == expected_bot
            and fields.get("role") == role
            and expected_bot in self._trusted_bots(comment)
        )

    def _trusted_bots(self, comment: Comment) -> frozenset[str]:
        return trusted_bots(self.trusted_logins, comment.author)

    def _review_actions(self, ticket: TrackedTicket) -> ReviewActions:
        if isinstance(self.review_actions, Mapping):
            try:
                return self.review_actions[ticket.repo.lower()]
            except KeyError as exc:
                raise ShipError(
                    f"no ReviewActions configured for {ticket.repo}"
                ) from exc
        return self.review_actions

    def _job_detail(
        self,
        ticket: TrackedTicket,
        *,
        action: str,
        head: str | None = None,
        round_number: int | None = None,
        instruction: str | None = None,
        resume: bool = False,
        cap: int | None = None,
    ) -> str:
        if round_number is not None and cap is None:
            cap = self._effective_cap(ticket)
        context: dict[str, object] = {
            "linear_url": ticket.ticket_url,
            "gitBranchName": ticket.git_branch_name,
            "base_branch": ticket.factory.base_branch,
            "pr_number": ticket.pr_number,
            "brief": ticket.brief or ticket.ticket_url,
            "risk": ticket.risk,
            "round": f"{round_number}/{cap}" if round_number is not None else None,
            "reviewer_harness": ticket.factory.reviewer_harness,
            "verify_skill": ticket.factory.verify,
            "repo_skill_paths": [
                f".claude/skills/{skill}/SKILL.md"
                for skill in ticket.factory.skills
            ],
            "post_to": (
                f"PR #{ticket.pr_number} issue comments (not reviews or inline comments)"
                if ticket.pr_number is not None
                else "the linked PR issue comments (not reviews or inline comments)"
            ),
            "action": action,
            "head": head,
            "project_bot": ticket.grok_bot,
        }
        if instruction is not None:
            context["instruction"] = instruction
        if resume:
            # Claim order (lib/queue.py): a resumed paused step goes before
            # bug and ordinary work, after urgent (issue #85).
            context["resume"] = True
        return json.dumps(context, separators=(",", ":"), sort_keys=True)

    def _wake_comment_problem(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reason: str,
        outcome: str,
    ) -> None:
        if not self._trusted_bots(comment):
            return
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason=reason,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=json.dumps(
                {
                    "project_bot": ticket.grok_bot,
                    "outcome": outcome,
                    "comment": _comment_link(comment),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            head=ticket.current_head,
        )

    def _project_wake_detail(
        self,
        ticket: TrackedTicket,
        detail: str,
    ) -> str:
        return json.dumps(
            {
                "project_bot": ticket.grok_bot,
                "detail": detail,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _wake_done(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        outcome: str,
        *,
        note: str | None = None,
    ) -> None:
        detail: dict[str, object] = {
            "project_bot": ticket.grok_bot,
            "outcome": outcome,
            "pr_number": ticket.pr_number,
            "post_to": ticket.ticket_url,
        }
        if note is not None:
            detail["note"] = note
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="done",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=json.dumps(
                detail,
                separators=(",", ":"),
                sort_keys=True,
            ),
            head=ticket.current_head,
        )

    def _duplicate_verdict(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
        comments: Sequence[Comment],
    ) -> bool:
        """An earlier verdict that was accepted and acted on says the same thing.

        Same author, `from`, `role`, head (as a prefix match, as the rows
        compare heads), round and verdict. The first one already took its
        action (a fix job, a ship, a wake); a repeat must take none.

        Codex PR #82 r2 F2.3: only an earlier verdict whose recorded outcome
        is in ACTED_VERDICT_OUTCOMES counts. A rejected (wrong role or
        author), stale, refused or failed one took no action, so it never
        suppresses a later verdict, such as its corrected repost.
        """
        head = fields.get("head") or ""
        for earlier in comments:
            if earlier.id == comment.id:
                return False
            if earlier.author.lower() != comment.author.lower():
                continue
            try:
                other = parse_comment(earlier.body).as_dict()
            except HandoffError:
                continue
            if (
                other.get("kind") == "verdict"
                and other.get("from") == fields.get("from")
                and other.get("role") == fields.get("role")
                and other.get("verdict") == fields.get("verdict")
                and other.get("round") == fields.get("round")
                and _matching_head(other.get("head") or "", head) is not None
                and _acted_verdict(self._handled_outcome(ticket, earlier.id))
            ):
                return True
        return False

    def _handled_outcome(self, ticket: TrackedTicket, comment_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT outcome FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        return None if row is None else str(row["outcome"])

    def _is_handled(self, ticket: TrackedTicket, comment_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        return row is not None

    def _mark_handled(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        outcome: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO handled_comments (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id, outcome),
            )

    def _record_poll_error(self, ticket: TrackedTicket, error: str) -> None:
        self.record_poll_error(ticket.repo, ticket.ticket_id, error)

    def _record_job_error(self, job: Job, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO poll_errors (
                    repo, ticket_id, error, recorded_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    job.request.repo.lower(),
                    job.request.ticket_id,
                    error,
                    self.queue.now(),
                ),
            )

    def _is_stuck(self, ticket: TrackedTicket) -> bool:
        return self._is_stuck_identity(ticket.repo, ticket.ticket_id)

    def _finished_identity(self, repo: str, ticket_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (repo.lower(), ticket_id),
            ).fetchone()
        return row is not None and row["workflow_state"] in FINISHED_STATES

    def _is_stuck_identity(self, repo: str, ticket_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT stuck FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo.lower(), ticket_id),
            ).fetchone()
        return row is not None and bool(row["stuck"])

    def _generation(self, repo: str, ticket_id: str) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation
                ) VALUES (?, ?, 0, 0, 1)
                """,
                (repo.lower(), ticket_id),
            )
            row = connection.execute(
                """
                SELECT generation FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo.lower(), ticket_id),
            ).fetchone()
        assert row is not None
        return int(row["generation"])

    def _ensure_all_stuck_wakes(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repo, ticket_id FROM ticket_state
                WHERE stuck = 1
                """
            ).fetchall()
        for row in rows:
            self._ensure_stuck_wake(str(row["repo"]), str(row["ticket_id"]))

    def _ensure_stuck_wake(self, repo: str, ticket_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=None,
            )
            connection.commit()

    def _ensure_stuck_wake_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        repo: str,
        ticket_id: str,
        detail: str | None,
    ) -> None:
        state = connection.execute(
            """
            SELECT stuck, stuck_reason, stuck_source_comment_id,
                   timeout_seconds, generation, stuck_wake_job_id
            FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            (repo.lower(), ticket_id),
        ).fetchone()
        if state is None or not bool(state["stuck"]):
            return
        wake_job_id = state["stuck_wake_job_id"]
        previous_detail: str | None = None
        if wake_job_id is not None:
            wake = connection.execute(
                "SELECT state, detail FROM jobs WHERE id = ?",
                (wake_job_id,),
            ).fetchone()
            if wake is None or str(wake["state"]) != "failed":
                return
            if isinstance(wake["detail"], str):
                previous_detail = str(wake["detail"])
        generation = int(state["generation"]) + 1
        reason = (
            detail
            or previous_detail
            or str(state["stuck_reason"] or "ticket is stuck")
        )
        queued = self.queue.enqueue_in_transaction(
            connection,
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action="stuck",
                recipient="grok",
                source_comment_id=str(state["stuck_source_comment_id"]),
                timeout_seconds=int(state["timeout_seconds"]),
                generation=generation,
                detail=reason,
            ),
            dedupe_key=f"stuck:{_scope(repo, ticket_id)}:g{generation}",
        )
        connection.execute(
            """
            UPDATE ticket_state
            SET generation = ?, stuck_wake_job_id = ?
            WHERE repo = ? AND ticket_id = ?
            """,
            (generation, queued.id, repo.lower(), ticket_id),
        )

    def _record_ci_result(
        self,
        ticket: TrackedTicket,
        head: str,
        status: str,
    ) -> tuple[str, int]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT status, gate_count_after FROM ci_events
                WHERE repo = ? AND ticket_id = ? AND head = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "failure" and status == "success":
                    # Issue #84: CI was rerun at this head and its latest
                    # attempts all passed. The recorded failure no longer
                    # holds; green resets the gate count as a new head would.
                    connection.execute(
                        """
                        UPDATE ci_events SET status = 'success', gate_count_after = 0
                        WHERE repo = ? AND ticket_id = ? AND head = ?
                        """,
                        (ticket.repo.lower(), ticket.ticket_id, head),
                    )
                    connection.execute(
                        """
                        UPDATE ticket_state SET gate_count = 0
                        WHERE repo = ? AND ticket_id = ?
                        """,
                        (ticket.repo.lower(), ticket.ticket_id),
                    )
                    connection.commit()
                    return "success", 0
                connection.commit()
                return str(existing["status"]), int(existing["gate_count_after"])
            row = connection.execute(
                """
                SELECT gate_count FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            previous = int(row["gate_count"]) if row is not None else 0
            count = previous + 1 if status == "failure" else 0
            connection.execute(
                """
                INSERT INTO ci_events (
                    repo, ticket_id, head, status, gate_count_after
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, head, status, count),
            )
            connection.execute(
                """
                INSERT INTO ticket_state (repo, ticket_id, gate_count, stuck)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET gate_count = excluded.gate_count
                """,
                (ticket.repo.lower(), ticket.ticket_id, count),
            )
            connection.commit()
        return status, count

    def _connect(self) -> sqlite3.Connection:
        return self.queue.connect()

    def _migrate_ship_intent_redrive(self) -> None:
        """Add ship_intents.redrive. Once, mark legacy handoff failures eligible.

        Before issue #47 every handoff failure was recorded by class name
        only. None had a confirmed write: draft -> ready went to a REST
        route GitHub does not have, so it never succeeded, and an
        unconfirmed Linear move confirmed nothing. Those rows get the one
        re-drive. The column add and the backfill share one transaction.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ship_intents)"
                ).fetchall()
            }
            if "redrive" not in columns:
                connection.execute("ALTER TABLE ship_intents ADD COLUMN redrive TEXT")
                connection.execute(
                    """
                    UPDATE ship_intents SET redrive = 'eligible'
                    WHERE policy = 'handoff' AND status = 'failed'
                      AND error IN ('GitHubAdapterError', 'HeadMismatchError')
                    """
                )
            connection.commit()

    def _initialize(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handled_comments (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_state (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    gate_count INTEGER NOT NULL DEFAULT 0,
                    stuck INTEGER NOT NULL DEFAULT 0,
                    workflow_state TEXT,
                    review_round INTEGER NOT NULL DEFAULT 0,
                    plan_gap_count INTEGER NOT NULL DEFAULT 0,
                    plan_check_comment_id TEXT,
                    paused_action TEXT,
                    paused_recipient TEXT,
                    paused_head TEXT,
                    paused_generation INTEGER,
                    paused_question_id TEXT,
                    stuck_reason TEXT,
                    stuck_source_comment_id TEXT,
                    stuck_wake_job_id TEXT,
                    timeout_seconds INTEGER,
                    generation INTEGER NOT NULL DEFAULT 1,
                    cap_extra INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(ticket_state)"
                ).fetchall()
            }
            if "stuck_wake_job_id" not in columns:
                connection.execute(
                    "ALTER TABLE ticket_state ADD COLUMN stuck_wake_job_id TEXT"
                )
            migrations = {
                "workflow_state": "TEXT",
                "review_round": "INTEGER NOT NULL DEFAULT 0",
                "plan_gap_count": "INTEGER NOT NULL DEFAULT 0",
                "plan_check_comment_id": "TEXT",
                "paused_action": "TEXT",
                "paused_recipient": "TEXT",
                "paused_head": "TEXT",
                "paused_generation": "INTEGER",
                "paused_question_id": "TEXT",
                "cap_extra": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE ticket_state ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ci_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    status TEXT NOT NULL,
                    gate_count_after INTEGER NOT NULL,
                    PRIMARY KEY (repo, ticket_id, head)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_tickets (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    tracker TEXT NOT NULL,
                    tracked_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ship_holds (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    approve_comment_id TEXT NOT NULL,
                    held_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ship_intents (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    redrive TEXT,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS external_ship_intents (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    source_comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    settled INTEGER NOT NULL DEFAULT 0,
                    settle_failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (repo, ticket_id, source_comment_id)
                )
                """
            )
            ship_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(external_ship_intents)"
                ).fetchall()
            }
            for name in ("settled", "settle_failures"):
                if name not in ship_columns:
                    connection.execute(
                        f"ALTER TABLE external_ship_intents ADD COLUMN {name} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
        self._migrate_ship_intent_redrive()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_events (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    error TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS poll_deferrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detail TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )
            # #83/#84: tickets that need a person (a ship that failed and
            # will not be retried, a job dedupe dropped). Shown by orch
            # status and the dashboard; cleared when the ticket moves on.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS needs_attention (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT,
                    raised_at REAL NOT NULL,
                    PRIMARY KEY (repo, ticket_id, reason)
                )
                """
            )
            # #75: what the poller did to a job because of its state
            # (released-claimed, dead; #94 requeued-unstarted from orch job run), one row each, for the log and audit.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                )
                """
            )


class ReviewShipper:
    """F1-05's ship for T8's after:<approver> ok (the hook_t8 Shipper).

    ship raises unless F1-05 completed an after:<approver> ship intent for
    request.approve_comment_id on request.head, CI on request.head is
    green, and request.head is a full SHA that is still the PR head. The
    ticket is re-read from source, and in those fresh comments the latest
    trusted reviewer verdict must still be that APPROVE, before the ok.
    A verdict posted between that read and the merge is not seen; that
    gap is accepted. Then ship lands exactly that head in the repo's
    ship_mode. An after: repo fast-forwards base_branch to it, raising if
    that is not a fast-forward, and counts as shipped only when
    base_branch's tip is exactly that head; a squash repo squash-merges it
    or confirms it is already merged. Its own intent is keyed on the ok
    comment, so
    one request never merges twice. Once the completed write for that
    intent has committed, ship no longer raises: a failed merged state or
    label write is retried by Dispatcher.process, and wakes Grok after
    SETTLE_FAILURES_TO_WAKE failures. Before that commit, any failure
    raises.
    """

    def __init__(self, dispatcher: Dispatcher, source: PollSource):
        self.dispatcher = dispatcher
        self.source = source

    def ship(self, request: ShipRequest) -> None:
        dispatcher = self.dispatcher
        head = request.head
        policy = f"after:{request.approver}"
        if not _is_full_sha(head):
            raise ShipError(f"ship head {head!r} is not a 40-character SHA")
        if not dispatcher._has_completed_ship_intent(
            request.repo,
            request.ticket_id,
            request.approve_comment_id,
            head,
            policy,
        ):
            raise ShipError(f"no completed {policy} ship intent for {head}")
        ticket = self._ticket(request)
        latest = dispatcher._latest_review_verdict(ticket)
        if (
            latest is None
            or latest[0].id != request.approve_comment_id
            or latest[1].get("verdict") != "APPROVE"
        ):
            raise ShipError(
                f"latest review verdict is not APPROVE {request.approve_comment_id}"
            )
        ok = next(
            (item for item in ticket.comments if item.id == request.source_comment_id),
            None,
        )
        if ok is None or _comment_order(latest[0]) >= _comment_order(ok):
            raise ShipError(
                f"APPROVE {request.approve_comment_id} does not precede "
                f"ok {request.source_comment_id}"
            )
        if ticket.factory.ship != policy:
            raise ShipError(f"ticket ship policy is {ticket.factory.ship}")
        if ticket.current_head != head:
            raise ShipError(f"PR head is {ticket.current_head}, not {head}")
        result = ticket.ci.get(head)
        if result is None or result.status != "success":
            raise ShipError(f"CI on {head} is not green")
        status, fresh = dispatcher._prepare_external_ship(request)
        if status == "pending":
            if ship_mode(ticket.factory.ship) == "fast-forward":
                self._fast_forward(request, ticket, fresh)
            elif dispatcher.review_actions.is_merged(ticket, head):
                pass
            elif not fresh:
                raise ShipError(f"ship of {head} has no recorded result")
            else:
                try:
                    dispatcher._ship_head(ticket, head)
                except Exception as exc:
                    dispatcher._finish_external_ship(
                        request,
                        "failed",
                        safe_ship_error(exc),
                    )
                    raise
            dispatcher._finish_external_ship(request, "completed", None)
        elif status != "completed":
            raise ShipError(f"ship of {head} is {status}; not shipping again")
        try:
            dispatcher._try_settle_external_ship(ticket, request.source_comment_id)
        except Exception:
            # The completed write has committed; Dispatcher.process retries.
            pass

    def _fast_forward(
        self,
        request: ShipRequest,
        ticket: TrackedTicket,
        fresh: bool,
    ) -> None:
        """Confirm only when base_branch's tip is exactly request.head.

        is_merged(head) is not enough: a squash or a merge commit made
        elsewhere also merges the PR at that head, and a merge commit even
        makes head reachable, but the branch is then not the approved SHA.
        """
        actions = self.dispatcher._review_actions(ticket)
        head = request.head
        branch = ticket.factory.base_branch
        try:
            tip = actions.branch_tip(ticket, branch)
            if tip == head:
                return
            if not fresh:
                raise ShipError(f"ship of {head} has no recorded result")
            if actions.is_merged(ticket, head):
                raise ShipError(
                    f"{branch} is {tip}, not {head}; the PR was merged another way"
                )
            self.dispatcher._ship_head(ticket, head)
            try:
                tip = actions.branch_tip(ticket, branch)
            except Exception as exc:
                # Only the inner safe detail or the class name: str(exc) of
                # an adapter error can hold gh's stdout or stderr.
                inner = getattr(exc, "safe_detail", None)
                tip = (
                    f"unreadable ({type(exc).__name__}: {inner})"
                    if isinstance(inner, str) and inner
                    else f"unreadable ({type(exc).__name__})"
                )
            if tip != head:
                raise ShipError(f"{branch} is {tip}, not {head}, after fast-forward")
        except Exception as exc:
            if fresh:
                self.dispatcher._finish_external_ship(
                    request,
                    "failed",
                    safe_ship_error(exc),
                )
            raise

    def _ticket(self, request: ShipRequest) -> TrackedTicket:
        tracker = self.dispatcher._tracker(request.repo, request.ticket_id)
        if tracker is None:
            raise ShipError(f"{request.repo}#{request.ticket_id} is not tracked")
        # #108 (#42 N3): a merged PR is read, not dropped; the is_merged
        # check in ship confirms it.
        ref = TicketRef(request.repo, request.ticket_id, tracker, allow_merged=True)
        fresh_reader = getattr(self.source, "fresh_ticket", None)
        if fresh_reader is not None:
            fresh = fresh_reader(ref)
            found = () if fresh is None else (fresh,)
        else:
            found = self.source.tracked_tickets((ref,))
        found = [
            item
            for item in found
            if item.repo.lower() == request.repo.lower()
            and item.ticket_id == request.ticket_id
        ]
        if len(found) != 1:
            raise ShipError(f"{request.repo}#{request.ticket_id} was not found")
        return found[0]


class PollLoop:
    def __init__(
        self,
        dispatcher: Dispatcher,
        source: PollSource,
        *,
        sleep: Callable[[float], None] = time.sleep,
        interval_seconds: float | None = None,
    ):
        self.dispatcher = dispatcher
        self.source = source
        self.sleep = sleep
        if interval_seconds is None:
            # orch.yml poll_seconds (30 or 60) when the source carries it.
            config = getattr(source, "config", None)
            interval_seconds = getattr(config, "poll_seconds", POLL_SECONDS)
        self.interval_seconds = interval_seconds

    def run(
        self,
        *,
        max_polls: int | None = None,
        on_error: Callable[[Exception], None] | None = None,
        after_poll: Callable[[], None] | None = None,
    ) -> None:
        """Poll forever (or `max_polls` times), sleeping between cycles.

        The sleep (`interval_seconds`, orch.yml poll_seconds) starts after a
        cycle finishes, so cycle start to cycle start is the cycle's own work
        plus the interval.

        `after_poll` (the dashboard push, when configured) runs after every
        cycle, including one whose error went to `on_error`. It must not block; if it raises, one line
        naming only the exception class goes to stderr and polling goes on.
        """
        if max_polls is not None and max_polls < 1:
            raise ValueError("max_polls must be positive")
        polls = 0
        while True:
            try:
                self.dispatcher.poll_once(self.source)
            except Exception as exc:
                if on_error is None:
                    raise
                on_error(exc)
            # Not in a `finally`: a stop (SIGTERM, Ctrl-C) or a fatal --once
            # error must not start a push on the way out.
            if after_poll is not None:
                _after_poll(after_poll)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return
            self.sleep(self.interval_seconds)


def _after_poll(hook: Callable[[], None]) -> None:
    try:
        hook()
    except Exception as exc:  # a dashboard push never breaks polling
        print(f"dashboard push: {type(exc).__name__}", file=sys.stderr, flush=True)


def _spread(repo: str, ticket_id: str) -> int:
    return zlib.crc32(f"{str(repo).lower()}#{ticket_id}".encode())


def _scope(repo: str, ticket_id: str) -> str:
    return f"{repo.lower()}\x1f{ticket_id}"


def trusted_bots(
    trusted_logins: Mapping[str, Mapping[str, frozenset[str]]],
    author: str,
) -> frozenset[str]:
    """The bots a comment author may speak for: GitHub logins from
    trusted_logins, `linear:<id>` authors from trusted_linear_users."""
    tracker = "linear" if author.startswith("linear:") else "github"
    return frozenset(trusted_logins.get(tracker, {}).get(author, frozenset()))


def _normalize_trusted_logins(
    value: Mapping[str, object],
) -> dict[str, dict[str, frozenset[str]]]:
    normalized: dict[str, dict[str, frozenset[str]]] = {}
    if not all(key in ("github", "linear") for key in value):
        raise ValueError("trusted_logins must be keyed by tracker")
    for tracker, entries in value.items():
        if not isinstance(entries, Mapping):
            raise ValueError("trusted_logins tracker entries must be mappings")
        normalized[tracker] = {}
        for login, bots in entries.items():
            if not isinstance(login, str) or not isinstance(
                bots, (set, frozenset, tuple, list)
            ):
                raise ValueError("trusted_logins entries must be bot sets")
            normalized[tracker][login] = frozenset(str(bot) for bot in bots)
    return normalized


def _grok_logical_key(job: Job) -> str:
    request = job.request
    return (
        f"{_scope(request.repo, request.ticket_id)}"
        f"\x1f{request.action}\x1f{request.source_comment_id}"
    )


# Verdict outcomes that took the verdict's action (#80, Codex PR #82 r2
# F2.3): a fix job queued, the review capped, a re-review queued, or a ship
# begun or done. Only these make a later identical verdict a duplicate.
ACTED_VERDICT_OUTCOMES = frozenset(
    {
        "queued-review-fix",
        "capped-review",
        "queued-patch-review",
        "merged",
        "merge-confirmed",
        "handed-off",
        "waiting-external",
        "ship-unconfirmed",
        "ship-failed",
    }
)


def _acted_verdict(outcome: str | None) -> bool:
    return outcome is not None and (
        outcome in ACTED_VERDICT_OUTCOMES
        or outcome.startswith("ship-already-completed-")
    )


def _round_number(value: object) -> int | None:
    """The N of a round field "N/CAP" (or "N"), or None."""
    if not isinstance(value, str):
        return None
    head = value.split("/", 1)[0].strip()
    return int(head) if head.isdigit() else None


# _posted_handoff: a ready whose head T2 could not yet check against the PR.
_HANDOFF_UNREAD = object()

# The handoff kind and role that answers each harness step (#108).
_ANSWERS = {
    "plan": ("plan", "builder"),
    "build": ("ready", "builder"),
    "fix": ("ready", "builder"),
    "review": ("verdict", "reviewer"),
}

# Handled outcomes that rejected a verdict: such a comment is no job's handoff.
_REJECTED_OUTCOMES = (
    "rejected-",
    "stale-",
    "superseded-",
    "duplicate-",
    "refused-",
    "failed-",
    "ticket-stuck",
)


def _retry_detail(job: Job) -> str | None:
    """The failed job's detail naming it as `retry_of`, so the retry can
    take over a worktree the first attempt kept (#105)."""
    try:
        context = json.loads(job.request.detail) if job.request.detail else {}
    except ValueError:
        return job.request.detail
    if not isinstance(context, dict):
        return job.request.detail
    context["retry_of"] = job.id
    return json.dumps(context, separators=(",", ":"), sort_keys=True)


def _detail_round_number(detail: str | None) -> int | None:
    """The round number a job's detail JSON carries ("round": "N/CAP")."""
    if not detail:
        return None
    try:
        context = json.loads(detail)
    except ValueError:
        return None
    return _round_number(context.get("round")) if isinstance(context, dict) else None


def _matching_head(reported: str, current: str | None) -> str | None:
    if current is None or min(len(reported), len(current)) < 7:
        return None
    if current.startswith(reported) or reported.startswith(current):
        return current
    return None


def _redrive_eligible(exc: BaseException, policy: str) -> bool:
    """Whether a failed ship gets its one re-drive (issue #47, issue #83)."""
    if not isinstance(exc, ShipError):
        return False
    if policy == "handoff":
        return exc.write_confirmed is False
    if policy == "merge":
        return exc.retryable is True
    return False


def _holds(ticket: TrackedTicket) -> bool:
    """Issue #86: the hold applies only on a ship: merge repo."""
    return ticket.factory.ship == "merge" and ticket.factory.hold is not None


def ship_mode(policy: str) -> str:
    """after:<approver> repos fast-forward; every other repo squash-merges."""
    return "fast-forward" if policy.startswith("after:") else "squash"


def _comment_order(comment: Comment) -> tuple[float, int, int | str]:
    """(created_at, id) with numeric ids compared as integers, so 9 < 10."""
    if re.fullmatch(r"[0-9]+", comment.id):
        return comment.created_at, 0, int(comment.id)
    return comment.created_at, 1, comment.id


def _has_questions(body: str) -> bool:
    """Whether a trusted questions comment asks anything.

    Fail closed: every questions comment counts unless it explicitly says
    there are none. Fenced code blocks (``` or ~~~, closed) are example text:
    a Questions heading inside one is ignored. Every Questions heading outside
    fences (any style: ``## Questions``, ``**Questions:**``, ``Questions: ...``)
    opens a section, and the comment has questions if any section does. In a
    section only the first content after the heading decides, whether that
    is the inline remainder or the next non-blank line: a none-marker
    ("None.", "N/A", "No questions") or no content at all means none; anything
    else, headings, bold labels and fenced blocks included, means questions.
    Headings never close a section, because a heading right under Questions
    can itself be a question or a label for one.
    Without a Questions heading, any line that is not a none-marker counts.
    """
    lines = body.splitlines()
    unfenced = _collapse_fences(lines)
    sections = [
        (heading.group("rest") or "", unfenced[index + 1 :])
        for index, line in enumerate(unfenced)
        if line is not None
        and (heading := _QUESTIONS_HEADING.fullmatch(line.strip())) is not None
    ]
    if sections:
        return any(_section_asks(rest, after) for rest, after in sections)
    return any(
        item and item not in _NO_QUESTIONS
        for item in map(_question_item, lines)
    )


def _section_asks(rest: str, after: list[str | None]) -> bool:
    """Whether a Questions section's first content is not a none-marker.

    A fenced block (None) is never a none-marker, so it counts as asking.
    """
    for content in [rest, *after]:
        if content is None:
            return True
        item = _question_item(content)
        if item:
            return item not in _NO_QUESTIONS
    return False


def _collapse_fences(lines: list[str]) -> list[str | None]:
    """The lines with each closed fenced code block replaced by one None.

    An opening fence with no closing fence is left as ordinary text, so an
    unclosed fence never hides anything (fail closed).
    """
    collapsed: list[str | None] = []
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN.match(lines[index])
        if opening is not None:
            fence = opening.group("fence")
            closing = re.compile(
                rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*"
            )
            end = next(
                (
                    later
                    for later in range(index + 1, len(lines))
                    if closing.fullmatch(lines[later])
                ),
                None,
            )
            if end is not None:
                collapsed.append(None)
                index = end + 1
                continue
        collapsed.append(lines[index])
        index += 1
    return collapsed


def _question_item(line: str) -> str:
    """A line reduced to its words: no heading, bold or list markup.

    Markup-only lines ("##", "**", "---") reduce to "" and count as blank.
    """
    text = re.sub(r"^#{1,6}(?=\s|$)|\s#+$", "", line.strip())
    text = _LIST_MARKER.sub("", text.strip("*: ")).strip("*: ")
    text = text.rstrip(".").strip().lower()
    return "" if re.fullmatch(r"[-*_#=\s]*", text) else text


def _comment_link(comment: Comment) -> str:
    return comment.url or f"comment:{comment.id}"


def _detail_project_bot(detail: str | None) -> str | None:
    if detail is None:
        return None
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    project_bot = parsed.get("project_bot")
    return project_bot if isinstance(project_bot, str) else None


def _is_patch_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def _is_full_sha(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _ship_state(policy: str) -> str:
    if policy == "merge":
        return "merged"
    if policy == "handoff":
        return "handed-off"
    return "waiting-external"


def _state_for_action(action: str) -> str:
    return {
        "plan": "planning",
        "build": "building",
        "review": "review",
        "fix": "fixing",
        "clarify": "questions",
    }[action]
