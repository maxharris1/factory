"""DispatcherBase: Dispatcher.__init__ plus shared trust, state-label, stuck and hook helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from lib.dispatch.bookkeeping import LockLedger
from lib.dispatch.cycle_clock import cycle_alarms
from lib.dispatch.constants import (
    CORE_KINDS,
    FINISHED_STATES,
    ORCHESTRATOR,
    WORKER_KINDS,
    _NOT_FINISHED,
)
from lib.dispatch.helpers import (
    _REGISTERING_ACTIONS,
    _base_mismatch,
    _detail_round,
    _detail_routing,
    _family_errors,
    _matching_head,
    _normalize_trusted_logins,
    _route,
    trusted_bots,
)
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import (
    BaseCheck,
    BaseMismatch,
    Comment,
    DeviceChecks,
    Feeder,
    GrokDelivery,
    Hook,
    HoldCarry,
    PoolRunner,
    QaDiff,
    RebaseClassifier,
    Registration,
    RerunOutcome,
    RetryableStateError,
    ReviewActions,
    RouteError,
    RunJob,
    ShipError,
    TicketRef,
    TrackedTicket,
    UpdateClassifier,
)
from lib.handoff import HandoffError, parse_comment
from lib.job_state import JobStateConfig
from lib.orch_yml import Approvals, Project
from lib.queue import QueueError, SQLiteJobQueue


class DispatcherBase:
    def __init__(
        self,
        state_path: Path,
        queue: SQLiteJobQueue,
        *,
        trusted_logins: Mapping[str, object],
        review_actions: ReviewActions | Mapping[str, ReviewActions],
        grok_delivery: GrokDelivery,
        projects: Sequence[Project] = (),
        hooks: Sequence[Hook] = (),
        devices: DeviceChecks | None = None,
        job_states: JobStateConfig | None = None,
        log: Callable[[str], None] | None = None,
        feeder: Feeder | None = None,
        update_classifier: UpdateClassifier | None = None,
        rebase_classifier: RebaseClassifier | None = None,
        approvals: Approvals | None = None,
        ci_logs: Callable[[str, str], Mapping[str, str]] | None = None,
        ci_rerun: Callable[[str, Sequence[str]], RerunOutcome] | None = None,
        ci_full_rerun: Callable[[str, Sequence[str]], RerunOutcome] | None = None,
        ci_base_checks: Callable[[str, str], Mapping[str, BaseCheck]] | None = None,
        ci_runs: Callable[[str, str], Sequence[RunJob]] | None = None,
        ci_job_log: Callable[[str, str], str] | None = None,
        ci_pool: Callable[[str], Sequence[PoolRunner]] | None = None,
        hold_carry: HoldCarry | None = None,
        qa_diff: QaDiff | None = None,
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
        self.wake_routes = {project.repo.lower(): project for project in projects}
        self.hooks = tuple(hooks)
        self.devices = devices
        self.job_states = job_states if job_states is not None else JobStateConfig()
        self.log = log
        self.feeder = feeder
        self.lock_ledger = LockLedger()
        self.attention_outbox: dict[tuple[str, str, str], tuple[str, float] | None] = {}
        self.cycle_alarms = cycle_alarms()
        # #130: decides whether an update-from-main kept the approval. None,
        # or any error, sends the update back to review (fail closed).
        self.update_classifier = update_classifier
        self.rebase_classifier = rebase_classifier
        self.hold_carry = hold_carry
        self.qa_diff = qa_diff
        self._hold_carries: dict[tuple[str, str, str, str, tuple[str, ...]], bool] = {}
        self.approvals = approvals if approvals is not None else Approvals()
        self.ci_logs = ci_logs
        self._ci_log_reads: dict[tuple[str, str, tuple[str | None, ...]], Mapping[str, str]] = {}
        self._ci_log_failures: set[tuple[str, str, tuple[str | None, ...]]] = set()
        self._ci_step_reads: dict[tuple[str, str, tuple[str, ...]], dict[str, int | None]] = {}
        self._ci_step_failures: set[tuple[str, str, tuple[str, ...]]] = set()
        self.ci_rerun = ci_rerun
        self.ci_full_rerun = ci_full_rerun
        self.ci_base_checks = ci_base_checks
        self._base_checks: dict[tuple[str, str], Mapping[str, BaseCheck] | None] = {}
        self.ci_runs = ci_runs
        self.ci_job_log = ci_job_log
        self.ci_pool = ci_pool
        # #91: the tickets this poll read, so T6 can see a verdict a failed
        # review job already posted. Cleared at the start of each poll.
        self._polled: dict[tuple[str, str], TrackedTicket] = {}
        self._fresh_reads: dict[tuple[str, str], TrackedTicket] = {}
        self._read_started: float | None = None
        # #109: tickets whose read the source reported failed, by error.
        self._source_errors: dict[tuple[str, str], str] = {}
        # #105: the source's uncached read, for a ready naming a newer head.
        self._fresh_reader: Callable[[TicketRef], TrackedTicket | None] | None = None
        # Repos (lowercased) that merged a PR this poll: one merge each (#130).
        self._merged_repos: set[str] = set()
        self._logged_queue_waits: dict[tuple[str, str], tuple[str | None, set[str]]] = {}
        # #219: repos (lowercased) with a ticket that entered merged this
        # poll, for the self-deploy kick after the cycle.
        self.merged_this_poll: set[str] = set()
        # #130: while the per-ticket pass runs, an approval later in it may
        # not be recorded yet, so an after: ok's ship waits for the pass end.
        self._ticket_pass = False
        self._deferred_ships: dict[tuple[str, str], TrackedTicket] = {}
        self._initialize()
        for hook in self.hooks:
            hook.initialize(self)

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

    def _counted_verdict(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> bool:
        return (
            self._trusted_reviewer(ticket, comment, fields)
            and self._registration(ticket, comment, "verdict") is not Registration.UNREGISTERED
        )

    def _registration(
        self, ticket: TrackedTicket, comment: Comment, kind: str
    ) -> Registration:
        actions = _REGISTERING_ACTIONS[kind]
        marks = ", ".join("?" for _ in actions)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, state, failure_handled, handoff_comment_id, recipient, head, detail
                FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action IN ({marks})
                """,
                (ticket.repo.lower(), ticket.ticket_id, *actions),
            ).fetchall()
        if any(row["handoff_comment_id"] == comment.id for row in rows):
            return Registration.REGISTERED
        if any(
            row["state"] == "running" and self._can_register(row, comment, kind) for row in rows
        ):
            return Registration.PENDING
        failed = {
            row["id"] for row in rows if row["state"] == "failed" and not row["failure_handled"]
        }
        for job in self.queue.unhandled_failures():
            if job.id not in failed:
                continue
            candidate = self._handoff_candidate(job)
            if candidate is not None and candidate.comment.id == comment.id:
                return Registration.REGISTERED if candidate.unread else Registration.PENDING
        return Registration.UNREGISTERED

    @staticmethod
    def _can_register(job: sqlite3.Row, comment: Comment, kind: str) -> bool:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            return False
        if fields.get("from") != f"{job['recipient']}-bot":
            return False
        if kind != "verdict":
            return True
        job_round = _detail_round(job["detail"])
        return _matching_head(fields.get("head") or "", job["head"]) is not None and (
            job_round is None or fields.get("round") == job_round
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
            and self._app_may_post(comment, fields)
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
                  AND action IN (
                      'plan', 'build', 'review', 'fix', 'update-from-main', 'clarify'
                  )
                  AND attempt = 1
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

    def _fresh_ticket(self, ticket: TrackedTicket) -> TrackedTicket | None:
        """The ticket read again, uncached, or None when the source cannot."""
        if self._fresh_reader is None:
            return None
        tracker = self._tracker(ticket.repo, ticket.ticket_id)
        if tracker is None:
            return None
        fresh = self._observed(
            self._fresh_reader(TicketRef(ticket.repo, ticket.ticket_id, tracker))
        )
        return None if fresh is None else self._routed(fresh)

    def _observed(self, fresh: TrackedTicket | None) -> TrackedTicket | None:
        if fresh is None:
            return None
        self._fresh_reads[(fresh.repo.lower(), fresh.ticket_id)] = fresh
        if fresh.merged:
            return fresh
        if fresh.closed:
            if self._closed_unfinished(fresh):
                self._abandon_closed(fresh)
            return fresh
        mismatch = self._sync_base_mismatch(fresh)
        if mismatch is not None:
            self._polled[(fresh.repo.lower(), fresh.ticket_id)] = fresh
            raise BaseMismatch(mismatch, fresh)
        return fresh

    def _sync_base_mismatch(self, ticket: TrackedTicket) -> str | None:
        mismatch = _base_mismatch(ticket)
        if mismatch is not None:
            with self._connect() as connection:
                self._flag_attention_once(
                    connection, ticket.repo, ticket.ticket_id, "base-mismatch", mismatch
                )
        elif ticket.pr_base is not None:
            self.clear_attention(ticket.repo, ticket.ticket_id, "base-mismatch")
        return mismatch

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
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                state,
                source="set-state",
                at=self.queue.now(),
                guard=_NOT_FINISHED,
                insert={"gate_count": 0, "stuck": 0},
            )

    def _routed(self, ticket: TrackedTicket) -> TrackedTicket:
        """The ticket with the builder and reviewer its latest plan, build or
        fix step was routed to (issue #206), or as read when that step names
        no routing. A T6 retry is not a step: it repeats an older one. Raises RouteError when the restored pair, with today's
        factory.md model hints, fails the cross-family rule."""
        if ticket.factory_md is not None:
            return ticket
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT detail, source_comment_id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND action IN ('plan', 'build', 'fix') AND attempt = 1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        routing = _detail_routing(None if row is None else row["detail"])
        if routing is None:
            return ticket
        routed = _route(ticket, *routing)
        errors = _family_errors(routed.factory)
        if errors:
            raise RouteError(
                f"{ticket.repo}#{ticket.ticket_id} routing "
                f"{routing[0]} / {routing[1]}: {'; '.join(errors)}",
                str(row["source_comment_id"]),
            )
        return routed

    def _job_builder_harness(self, ticket: TrackedTicket) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recipient FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND action IN ('plan', 'build', 'fix', 'update-from-main')
                  AND attempt = 1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["recipient"])

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
            and self._app_may_post(comment, fields)
        )

    def _trusted_bots(self, comment: Comment) -> frozenset[str]:
        bots = trusted_bots(self.trusted_logins, comment.author)
        app_bot = self.approvals.app_bot(comment.author)
        if app_bot is None:
            return bots
        return bots & {app_bot}

    def _app_may_post(self, comment: Comment, fields: Mapping[str, str]) -> bool:
        """#170: with the switch on, a worker bot's App login speaks only
        its own handoff kinds; answers, go and ok stay Factory's."""
        app_bot = self.approvals.app_bot(comment.author)
        return app_bot in (None, ORCHESTRATOR) or fields.get("kind") in WORKER_KINDS

    def _review_actions(self, ticket: TrackedTicket) -> ReviewActions:
        if isinstance(self.review_actions, Mapping):
            try:
                return self.review_actions[ticket.repo.lower()]
            except KeyError as exc:
                raise ShipError(
                    f"no ReviewActions configured for {ticket.repo}"
                ) from exc
        return self.review_actions

    def _workflow_state(self, repo: str, ticket_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (repo.lower(), ticket_id),
            ).fetchone()
        return None if row is None else row["workflow_state"]

    def _is_abandoned(self, repo: str, ticket_id: str) -> bool:
        return self._workflow_state(repo, ticket_id) == "abandoned"

    def _is_stuck(self, ticket: TrackedTicket) -> bool:
        return self._is_stuck_identity(ticket.repo, ticket.ticket_id)

    def _finished_identity(self, repo: str, ticket_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (repo.lower(), ticket_id),
            ).fetchone()
        return row is not None and row["workflow_state"] in (*FINISHED_STATES, "abandoned")

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
