"""Dispatcher transitions T1, T1P and T2 through T6, plus project hooks."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lib.factory_md import FactoryConfig
from lib.grok_delivery import GrokDeliveryError
from lib.handoff import HandoffError, parse_comment
from lib.orch_yml import DEFAULT_JOB_TIMEOUT_MINUTES
from lib.queue import Job, JobRequest, QueueError, SQLiteJobQueue

POLL_SECONDS = 60
GITHUB_STATE_LABEL_PREFIX = "orch:state/"
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
    def github_candidates(self, label_prefix: str) -> tuple[TrackedTicket, ...]: ...

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
    it sent was confirmed before it failed. Only a handoff failure with
    write_confirmed False is eligible for the one handoff re-drive.
    safe_detail, when set, is the only text recorded in ship_intents.error.
    """

    write_confirmed: bool = True
    safe_detail: str | None = None


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
        self._initialize()
        for hook in self.hooks:
            hook.initialize(self)

    def poll_once(self, source: PollSource) -> None:
        begin = getattr(source, "begin_cycle", None)
        end = getattr(source, "end_cycle", None)
        if begin is not None:
            begin()
        try:
            github = source.github_candidates(GITHUB_STATE_LABEL_PREFIX)
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
            tracked = source.tracked_tickets(self._tracked_refs())
        except Exception as exc:
            self.record_poll_error("tracked", "-", type(exc).__name__)
            tracked = ()
        for ticket in tracked:
            try:
                self.process(ticket)
            except Exception as exc:
                self._record_poll_error(ticket, type(exc).__name__)
        self.reconcile_jobs()
        if end is not None:
            end()

    def record_poll_error(self, repo: str, ticket_id: str, error: str) -> None:
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

    def record_terminal(self, repo: str, ticket_id: str, state: str) -> None:
        if state not in ("merged", "handed-off", "done"):
            raise ValueError(f"{state!r} is not a completed terminal state")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, workflow_state
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET workflow_state = excluded.workflow_state
                """,
                (repo.lower(), ticket_id, state),
            )

    def process(self, ticket: TrackedTicket) -> None:
        comments = sorted(ticket.comments, key=_comment_order)
        try:
            self._redrive_handoffs(ticket)
            self._process_comments(ticket, comments)
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
        for comment in comments:
            if self._is_handled(ticket, comment.id):
                continue
            fields: dict[str, str] | None
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                fields = None
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
                if self._mark_ship_intent_failed(
                    ticket,
                    comment.id,
                    error,
                    redrive=(
                        isinstance(exc, ShipError)
                        and exc.write_confirmed is False
                    ),
                ):
                    self._wake_ship_failed(
                        ticket,
                        comment,
                        f"ship raised {error}",
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

    def reconcile_jobs(self) -> None:
        deferred_grok_jobs: set[str] = set()
        self._deliver_grok_jobs(deferred_grok_jobs)
        now = self.queue.now()
        for job in self.queue.outstanding():
            activity = (
                job.created_at if job.state == "queued" else job.last_activity_at
            )
            if now - activity >= job.request.timeout_seconds:
                self.queue.mark_timed_out(job)
        for job in self.queue.unhandled_failures():
            self._t6(job)
        self._ensure_all_stuck_wakes()
        self._deliver_grok_jobs(deferred_grok_jobs)

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
        elif reason in ("blocked", "capped", "done"):
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
        state = fields.get("state")
        action = {"planning": "plan", "building": "build"}.get(state)
        workflow_state = self._workflow_state(ticket.repo, ticket.ticket_id)
        if workflow_state == "capped" and action is not None:
            action = "fix"
            state = "fixing"
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
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action=action,
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                detail=self._job_detail(ticket, action=action),
            ),
            dedupe_key=(
                f"t1:{_scope(ticket.repo, ticket.ticket_id)}:{action}:"
                f"g{generation}"
            ),
        )
        self._set_workflow_state(ticket, state)
        return True, f"queued-{action}"

    def _workflow_state(self, repo: str, ticket_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT workflow_state FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (repo.lower(), ticket_id),
            ).fetchone()
        if row is None or row["workflow_state"] is None:
            return None
        return str(row["workflow_state"])

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
        round_field = fields.get("round")
        reported_round = (
            int(round_field.split("/", 1)[0]) if round_field is not None else None
        )
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
                SELECT workflow_state, plan_gap_count, plan_check_comment_id
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
                self.queue.enqueue_in_transaction(
                    connection,
                    JobRequest(
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
                            instruction=_comment_link(comment),
                        ),
                    ),
                    dedupe_key=(
                        f"plan-build:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{comment.id}"
                    ),
                )
            elif gap_count == 0:
                next_state = "planning"
                outcome = "queued-plan-gap"
                gap_count = 1
                self.queue.enqueue_in_transaction(
                    connection,
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
        state, _review_round = self._review_context(ticket)
        if state in ("planning", "plan-check"):
            return True, "rejected-ready-in-planning"
        reported_head = fields["head"]
        head = _matching_head(reported_head, ticket.current_head)
        if head is None:
            return True, "stale-ready-head"
        result = ticket.ci.get(head) or ticket.ci.get(reported_head)
        if result is None or result.status == "pending":
            return False, "ci-pending"
        timeout_seconds = ticket.job_timeout_minutes * 60
        recorded_status, count = self._record_ci_result(ticket, head, result.status)
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if recorded_status == "success":
            self._queue_review(
                ticket,
                source_comment_id=comment.id,
                head=head,
                generation=generation,
            )
            self._set_workflow_state(ticket, "review")
            return True, "queued-review"
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
        self.queue.enqueue(
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
                    instruction=result.log or f"CI failed on {head}",
                ),
            ),
            dedupe_key=(
                f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}:"
                f"g{generation}"
            ),
        )
        return True, "queued-ci-fix"

    def _t3(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-revise-author"
        verdict_head = fields.get("head")
        if _matching_head(verdict_head or "", ticket.current_head) is None:
            return True, "stale-revise"
        round_field = fields.get("round")
        reported_round = (
            int(round_field.split("/", 1)[0]) if round_field is not None else None
        )
        cap = (
            ticket.factory.rounds_low
            if ticket.risk == "low"
            else ticket.factory.rounds_high
        )
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
            if (
                state["workflow_state"] != "review"
                or reported_round != round_number
            ):
                connection.commit()
                self._wake_comment_problem(
                    ticket, comment, "stale", "stale-revise"
                )
                return True, "stale-revise"
            next_state = "capped" if round_number >= cap else "fixing"
            if next_state == "fixing":
                self.queue.enqueue_in_transaction(
                    connection,
                    JobRequest(
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
                            instruction=_comment_link(comment),
                        ),
                    ),
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
        round_field = fields.get("round")
        reported_round = (
            int(round_field.split("/", 1)[0]) if round_field is not None else None
        )
        review_head = self._latest_review_head(ticket)
        if state != "review" or reported_round != review_round + 1:
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
            return True, "approve-ci-red"
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
            note = self._review_actions(ticket).handoff(ticket, current_head)
            self._complete_ship_intent(ticket, comment.id, "handed-off")
            self._write_state_label(ticket, "handed-off")
            self._wake_done(ticket, comment, "handed-off", note=note)
            return True, "handed-off"
        approver = ship.removeprefix("after:")
        self._review_actions(ticket).await_approver(ticket, current_head, approver)
        self._complete_ship_intent(ticket, comment.id, "waiting-external")
        self._write_state_label(ticket, "waiting-external")
        return True, "waiting-external"

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
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action=action,
                recipient=recipient,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                head=(
                    str(paused["paused_head"])
                    if paused["paused_head"] is not None
                    else None
                ),
                detail=self._job_detail(
                    ticket,
                    action=action,
                    head=(
                        str(paused["paused_head"])
                        if paused["paused_head"] is not None
                        else None
                    ),
                    instruction=_comment_link(comment),
                ),
            ),
            dedupe_key=(
                f"t5:{_scope(ticket.repo, ticket.ticket_id)}:"
                f"{paused['paused_question_id']}:{comment.id}"
            ),
        )
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
        *,
        source_comment_id: str,
        head: str,
        generation: int,
        detail: str | None = None,
    ) -> None:
        _state, completed_rounds = self._review_context(ticket)
        review_round = completed_rounds + 1
        self.queue.enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="review",
                recipient=ticket.factory.reviewer_harness,
                source_comment_id=source_comment_id,
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

    def _queue_patch_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        current_head: str,
    ) -> tuple[bool, str]:
        self._queue_review(
            ticket,
            source_comment_id=comment.id,
            head=current_head,
            generation=self._generation(ticket.repo, ticket.ticket_id),
            detail=f"Approval does not cover current head {current_head}",
        )
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
            self._write_state_label(ticket, _ship_state(policy))
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
                """
                UPDATE ticket_state SET workflow_state = 'ship'
                WHERE repo = ? AND ticket_id = ?
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
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = ?
                WHERE repo = ? AND ticket_id = ?
                """,
                (state, ticket.repo.lower(), ticket.ticket_id),
            )
            connection.commit()

    def _mark_ship_intent_failed(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        error: str,
        *,
        redrive: bool = False,
    ) -> bool:
        """pending -> failed. A handoff that failed before any confirmed
        write is marked redrive='eligible' for one re-drive."""
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE ship_intents
                SET status = 'failed', error = ?,
                    redrive = CASE
                        WHEN ? AND policy = 'handoff' THEN 'eligible'
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
        """Re-drive, once, a handoff that failed before any confirmed write.

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
        if ticket.factory.ship != "handoff":
            return
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT comment_id, head FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND policy = 'handoff'
                  AND status = 'failed' AND redrive = 'eligible'
                ORDER BY comment_id
                """,
                (ticket.repo.lower(), ticket.ticket_id),
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
                or self._has_completed_handoff_at(ticket, str(row["head"]))
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
            if not self._set_redrive(ticket, comment_id, "spent", "eligible"):
                continue
            try:
                note = self._review_actions(ticket).handoff(ticket, head)
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
                    f"handoff re-drive raised {error}",
                )
                continue
            self._complete_redrive(ticket, comment_id)
            self._wake_done(ticket, comment, "handed-off", note=note)

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

    def _complete_redrive(self, ticket: TrackedTicket, comment_id: str) -> None:
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
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = 'handed-off'
                WHERE repo = ? AND ticket_id = ?
                """,
                scope,
            )
            connection.execute(
                """
                DELETE FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*scope, comment_id),
            )
            connection.commit()

    def _has_completed_handoff_at(self, ticket: TrackedTicket, head: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND policy = 'handoff'
                  AND head = ? AND status = 'completed'
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
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
            connection.execute(
                """
                UPDATE ticket_state SET workflow_state = 'merged'
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
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
        self._write_state_label(ticket, state)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, workflow_state
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET workflow_state = excluded.workflow_state
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
        if self._is_stuck_identity(job.request.repo, job.request.ticket_id):
            self._ensure_stuck_wake(job.request.repo, job.request.ticket_id)
            self.queue.mark_failure_handled(job.id)
            return
        if job.request.attempt == 1:
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
                detail=job.request.detail,
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
                """,
                (
                    repo.lower(),
                    ticket_id,
                    reason,
                    source_comment_id,
                    timeout_seconds,
                ),
            )
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=detail,
            )
            connection.commit()

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
        """
        if fields["kind"] in ("brief", "go"):
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

    def _tracked_refs(self) -> tuple[TicketRef, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tracked.repo, tracked.ticket_id, tracked.tracker,
                       CASE
                         WHEN state.workflow_state IN ('merged', 'handed-off')
                         THEN 1
                         ELSE 0
                       END AS allow_merged
                FROM tracked_tickets AS tracked
                LEFT JOIN ticket_state AS state
                  ON state.repo = tracked.repo
                 AND state.ticket_id = tracked.ticket_id
                WHERE state.workflow_state IS NULL
                   OR state.workflow_state NOT IN (
                       'merged', 'handed-off', 'done', 'abandoned'
                   )
                   OR (
                       state.workflow_state IN ('merged', 'handed-off')
                       AND EXISTS (
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
                       )
                   )
                   OR (
                       state.workflow_state = 'merged'
                       AND EXISTS (
                           SELECT 1
                           FROM external_ship_intents AS external
                           WHERE external.repo = tracked.repo
                             AND external.ticket_id = tracked.ticket_id
                             AND external.settled = 0
                       )
                   )
                ORDER BY tracked.repo, tracked.ticket_id
                """
            ).fetchall()
        return tuple(
            TicketRef(
                repo=str(row["repo"]),
                ticket_id=str(row["ticket_id"]),
                tracker=str(row["tracker"]),
                allow_merged=bool(row["allow_merged"]),
            )
            for row in rows
        )

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

        Merged, handed off, done or abandoned with nothing left to settle
        (_tracked_refs). Only the Linear webhook poller reads this, to ack
        their dirty rows unread (docs/SPEC.md section 7).
        """
        polled = {
            ref.ticket_id for ref in self._tracked_refs() if ref.tracker == "linear"
        }
        return self._tracked_linear_ids() - polled

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
        tracker = "linear" if comment.author.startswith("linear:") else "github"
        return self.trusted_logins.get(tracker, {}).get(
            comment.author,
            frozenset(),
        )

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
    ) -> str:
        cap = (
            ticket.factory.rounds_low
            if ticket.risk == "low"
            else ticket.factory.rounds_high
        )
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
        ref = TicketRef(request.repo, request.ticket_id, tracker)
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
    ):
        self.dispatcher = dispatcher
        self.source = source
        self.sleep = sleep

    def run(
        self,
        *,
        max_polls: int | None = None,
        on_error: Callable[[Exception], None] | None = None,
        after_poll: Callable[[], None] | None = None,
    ) -> None:
        """Poll forever (or `max_polls` times), sleeping between cycles.

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
            self.sleep(POLL_SECONDS)


def _after_poll(hook: Callable[[], None]) -> None:
    try:
        hook()
    except Exception as exc:  # a dashboard push never breaks polling
        print(f"dashboard push: {type(exc).__name__}", file=sys.stderr, flush=True)


def _scope(repo: str, ticket_id: str) -> str:
    return f"{repo.lower()}\x1f{ticket_id}"


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


def _matching_head(reported: str, current: str | None) -> str | None:
    if current is None or min(len(reported), len(current)) < 7:
        return None
    if current.startswith(reported) or reported.startswith(current):
        return current
    return None


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
