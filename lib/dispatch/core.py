"""DispatcherBase: Dispatcher.__init__ plus shared trust, state-label, stuck and hook helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import replace
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
    _comment_order,
    _detail_round,
    _detail_routing,
    _family_errors,
    _matching_head,
    _normalize_trusted_logins,
    _route,
    _route_go,
    _route_picks,
    _review_pick,
    _router_picks,
    trusted_bots,
)
from lib.dispatch.transitions import write_workflow_state
from lib.factory_md import family_findings
from lib.role_router import Candidate, RoutingError
from lib.dispatch.types import (
    BaseCheck,
    BaseMismatch,
    Comment,
    DeviceChecks,
    Feeder,
    GrokDelivery,
    Hook,
    HoldCarry,
    MovedHeadProbe,
    PoolRunner,
    QaDiff,
    RebaseClassifier,
    Registration,
    RerunOutcome,
    RetargetDeltaProbe,
    RetryableStateError,
    ReviewActions,
    RouteError,
    RouterPicks,
    RunJob,
    ShipError,
    StackBoundsProbe,
    TicketRef,
    TrackedTicket,
    UpdateClassifier,
)
from lib.handoff import HandoffError, parse_comment
from lib.job_state import JobStateConfig
from lib.orch_yml import Approvals, Project
from lib.queue import QueueError, SQLiteJobQueue
from lib.update_check import MovedHead


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
        refused_harnesses: Collection[str] = (),
        ci_logs: Callable[[str, str], Mapping[str, str]] | None = None,
        ci_rerun: Callable[[str, Sequence[str]], RerunOutcome] | None = None,
        ci_full_rerun: Callable[[str, Sequence[str]], RerunOutcome] | None = None,
        ci_base_checks: Callable[[str, str], Mapping[str, BaseCheck]] | None = None,
        ci_runs: Callable[[str, str], Sequence[RunJob]] | None = None,
        ci_job_log: Callable[[str, str], str] | None = None,
        ci_pool: Callable[[str], Sequence[PoolRunner]] | None = None,
        hold_carry: HoldCarry | None = None,
        qa_diff: QaDiff | None = None,
        moved_head: MovedHeadProbe | None = None,
        stack_bounds: StackBoundsProbe | None = None,
        retarget_delta: RetargetDeltaProbe | None = None,
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
        self.moved_head = moved_head
        self.stack_bounds = stack_bounds
        self.retarget_delta = retarget_delta
        self._moved_heads: dict[tuple[str, str, str, str], MovedHead | None] = {}
        self.approvals = approvals if approvals is not None else Approvals()
        self.refused_harnesses = frozenset(refused_harnesses)
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
        return (
            self._trusted(
                ticket,
                comment,
                fields,
                f"{ticket.factory.reviewer_harness}-bot",
                role="reviewer",
            )
            or self._rerouted_reviewer(ticket, comment, fields) is Registration.REGISTERED
        )

    def _rerouted_reviewer(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> Registration | None:
        sender = fields.get("from") or ""
        harness = sender.removesuffix("-bot")
        if (
            harness == sender
            or harness in self.refused_harnesses
            or not self._trusted(ticket, comment, fields, sender, role="reviewer")
        ):
            return None
        registration, job = self._registering_job(ticket, comment, "verdict", recipient=harness)
        if job is None:
            return None
        routing = _detail_routing(job["detail"])
        model = routing[2].reviewer.model if routing is not None and routing[2] else None
        if any(
            finding.severity == "error"
            for finding in family_findings(
                ticket.factory.builder_harness,
                ticket.factory.builder_model,
                reviewer_harness=harness,
                reviewer_model=model,
            )
        ):
            return None
        return registration

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
        return self._registering_job(ticket, comment, kind)[0]

    def _registering_job(
        self, ticket: TrackedTicket, comment: Comment, kind: str, recipient: str | None = None
    ) -> tuple[Registration, sqlite3.Row | None]:
        actions = _REGISTERING_ACTIONS[kind]
        marks = ", ".join("?" for _ in actions)
        with self._connect() as connection:
            rows = [
                row
                for row in connection.execute(
                    f"""
                    SELECT id, state, failure_handled, handoff_comment_id, recipient, head, detail
                    FROM jobs
                    WHERE lower(repo) = ? AND ticket_id = ? AND action IN ({marks})
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, *actions),
                ).fetchall()
                if recipient is None or row["recipient"] == recipient
            ]
        for row in rows:
            if row["handoff_comment_id"] == comment.id:
                return Registration.REGISTERED, row
        for row in rows:
            if row["state"] == "running" and self._can_register(row, comment, kind):
                return Registration.PENDING, row
        failed = {
            row["id"]: row
            for row in rows
            if row["state"] == "failed" and not row["failure_handled"]
        }
        for job in self.queue.unhandled_failures():
            if job.id not in failed:
                continue
            candidate = self._handoff_candidate(job)
            if candidate is not None and candidate.comment.id == comment.id:
                registration = (
                    Registration.REGISTERED if candidate.unread else Registration.PENDING
                )
                return registration, failed[job.id]
        return Registration.UNREGISTERED, None

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
        held = False
        if _base_mismatch(fresh) is not None:
            fresh, held = self._advance_stack(fresh)
        self._fresh_reads[(fresh.repo.lower(), fresh.ticket_id)] = fresh
        if held:
            raise BaseMismatch(f"PR #{fresh.pr_number} waits for its stack retarget", fresh)
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

    def _moved_head(self, ticket: TrackedTicket, ok: str, head: str) -> MovedHead | None:
        base = ticket.factory.base_branch
        key = (ticket.repo.lower(), base, ok, head)
        if key not in self._moved_heads:
            self._moved_heads[key] = None
            if self.moved_head is not None:
                try:
                    self._moved_heads[key] = self.moved_head(ticket.repo, base, ok, head)
                except Exception as exc:
                    if self.log is not None:
                        self.log(
                            f"moved head unread: {ticket.repo}#{ticket.ticket_id}: "
                            f"{type(exc).__name__}"
                        )
        return self._moved_heads[key]

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
        fix step was routed to (issue #206). A step that names no routing keeps
        the harness it was queued for as builder. A T6 retry is not a step: it repeats an older one. Raises RouteError when the restored pair, with today's
        factory.md model hints, fails the cross-family rule."""
        if ticket.factory_md is not None:
            return ticket
        row = self._stored_route(ticket)
        routing = _detail_routing(None if row is None else row["detail"])
        if routing is None:
            return self._fresh_route(ticket, None if row is None else str(row["recipient"]))
        builder, reviewer, picks = routing
        if reviewer in self.refused_harnesses:
            base = ticket.factory_md or ticket.factory
            built_by = (
                picks.builder
                if picks is not None
                else Candidate(
                    builder, base.builder_model if builder == base.builder_harness else None
                )
            )
            fresh = _review_pick(
                ticket,
                self.refused_harnesses,
                built_by,
                drawn=picks.sample.review if picks is not None and picks.sample is not None else None,
            )
            if fresh is not None:
                reviewer = fresh.harness
                picks = (
                    RouterPicks(built_by, fresh, None)
                    if picks is None
                    else replace(picks, reviewer=fresh)
                )
        routed = (
            _route(ticket, builder, reviewer)
            if picks is None
            else _route_picks(ticket, picks)
        )
        errors = _family_errors(routed.factory)
        if errors:
            raise RouteError(
                f"{ticket.repo}#{ticket.ticket_id} routing "
                f"{builder} / {reviewer}: {'; '.join(errors)}",
                str(row["source_comment_id"]),
            )
        return routed

    def _stored_route(self, ticket: TrackedTicket) -> sqlite3.Row | None:
        """The latest plan, build or fix job, else the latest review job that
        names its routing: a ticket built outside the queue stores its route
        on its first review."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT detail, recipient, source_comment_id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND action IN ('plan', 'build', 'fix') AND attempt = 1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if row is not None:
                return row
            reviews = connection.execute(
                """
                SELECT detail, recipient, source_comment_id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                ORDER BY created_at DESC, id DESC
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        return next((review for review in reviews if _detail_routing(review["detail"]) is not None), None)

    def _fresh_route(
        self, ticket: TrackedTicket, built_by: str | None, *, built_outside: bool = False
    ) -> TrackedTicket:
        try:
            picks = _router_picks(
                ticket,
                self.refused_harnesses,
                builder_only=built_by,
                guards=self._gemini_guards(ticket) if built_by is None else None,
                built_outside=built_outside,
            )
        except RoutingError as error:
            named = self._routed_by_pending_go(ticket)
            if named is not None:
                return named
            raise RouteError(
                f"{ticket.repo}#{ticket.ticket_id} routing: {error}",
                ticket.comments[-1].id if ticket.comments else "",
            ) from error
        return ticket if picks is None else _route_picks(ticket, picks)

    def _routed_outside_queue(self, ticket: TrackedTicket) -> TrackedTicket:
        """A ticket with no stored route, routed for a build the queue did not
        run: its review is drawn and its builder is not."""
        if ticket.factory_md is None or self._stored_route(ticket) is not None:
            return ticket
        unrouted = replace(ticket, factory=ticket.factory_md, factory_md=None, router_picks=None)
        return self._fresh_route(unrouted, None, built_outside=True)

    def _routed_by_pending_go(self, ticket: TrackedTicket) -> TrackedTicket | None:
        for comment in sorted(ticket.comments, key=_comment_order, reverse=True):
            if comment.edited_at is not None or self._is_handled(ticket, comment.id):
                continue
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if (
                fields["kind"] != "go"
                or fields.get("state") not in ("planning", "building")
                or not {"builder", "reviewer"} & fields.keys()
                or not self._trusted(
                    ticket, comment, fields, ORCHESTRATOR, role="orchestrator"
                )
            ):
                continue
            try:
                routed = _route_go(ticket, fields, self.refused_harnesses)
            except RoutingError:
                continue
            if routed is not None and not {
                routed.factory.builder_harness,
                routed.factory.reviewer_harness,
            } & self.refused_harnesses:
                return routed
        return None

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
        if (
            expected_bot == ORCHESTRATOR
            and role == "orchestrator"
            and fields.get("from") == "approver"
            and fields.get("role") == "approver"
            and self._may_approve(comment.author)
            and self.approvals.app_bot(comment.author) is None
        ):
            return True
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
