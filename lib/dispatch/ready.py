"""T2 ready handling: queue review, CI results, verdict ordering and re-entry after handoff."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from lib.dispatch.constants import (
    CI_BASE_RED,
    CI_INFRA_BLOCKED_REASON,
    CI_INFRA_WAIT,
    CI_INFRA_WAIT_SECONDS,
    CI_RERUN,
    CI_RERUN_REFUSED,
    CI_RERUN_SETTLE_SECONDS,
    CI_SILENT_REASON,
    CI_SILENT_SECONDS,
    CI_STALE_REASON,
    CI_STALE_SECONDS,
    CI_WAIT_REASONS,
    HARNESS_WORK,
    LOCAL_GATE_WAITING,
    TERMINAL_STATES,
    UNANNOUNCED_AFTER_CAP,
    CIHold,
)
from lib.dispatch.helpers import (
    _HANDOFF_UNREAD,
    _acted_verdict,
    _answering_outcome,
    _comment_link,
    _comment_order,
    _detail_field,
    _detail_project_bot,
    _detail_round_number,
    _has_questions,
    _matching_head,
    _round_number,
    _scope,
    failed_checks,
    own_close_at_head,
    record_own_close,
)
from lib.dispatch.rebase import RebaseStep, newest_rebase, parent_overlap
from lib.dispatch.retry import _harness_job_active
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import (
    CHECK_FAILED,
    BaseCheck,
    BaseMismatch,
    CIResult,
    Comment,
    FailedCheck,
    Registration,
    TrackedTicket,
    stale_units,
)
from lib.dispatch.verdict.carry import carries
from lib.dispatch.verdict.shipped import _approved_round
from lib.factory_md import padded_estimate
from lib.handoff import HandoffError, parse_comment
from lib.pre_ready import planned
from lib.queue import (
    LOCAL_GATE_FAILED,
    LOCAL_GATE_PASSED,
    LOCAL_GATE_UNAVAILABLE,
    JobRequest,
    _job,
)
from lib.update_check import RebaseScope, RetargetDelta, UpdateScope

if TYPE_CHECKING:
    from lib.evidence.ci import CIEvidence

_CI_RED_READY = frozenset({"queued-ci-fix", "ci-red-in-flight"})
_RERUN_STATES = (None, "building", "fixing", "review")
_AFTER_BUILD_STATES = frozenset({"review", "fixing", "awaiting-ok", "handed-off", "merge-queue"})
_UNANNOUNCED_GREEN_STATES = frozenset({"building", "fixing", "review"})
_UNANNOUNCED_GREEN_SOURCE = "unannounced-green-head"
_CI_LOG_READS = 64
READY_TAKEOVERS = ("queued-review", "answered-review", "queued-ci-fix")
_REENTERS_FROM = {"handoff": ("handed-off", "merge-queue"), "rebase": ("fixing",)}


class ReadyMixin:
    def _t2(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        handled, outcome = self._ready(ticket, comment, fields)
        self._record_ci_wait(ticket, comment, fields["head"], outcome)
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?
                AND NOT EXISTS (
                    SELECT 1 FROM ci_waits WHERE repo = ? AND ticket_id = ? AND reason = ?
                )
                """,
                (*key, CI_INFRA_BLOCKED_REASON, *key, CI_INFRA_WAIT),
            )
        return handled, outcome

    def _ready_at_base(self, ticket: TrackedTicket, comment: Comment) -> None:
        with self._connect() as connection:
            poster = connection.execute(
                "SELECT * FROM jobs WHERE lower(repo) = ? AND ticket_id = ? AND handoff_comment_id = ?",
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            repeated = connection.execute(
                """
                SELECT 1 FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND outcome = 'rejected-ready-at-base'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if poster is None:
                return
            job = _job(poster, connection)
        if repeated is None:
            self.queue.enqueue(
                replace(job.request, generation=job.request.generation + 1),
                dedupe_key=f"ready-at-base:{comment.id}",
            )
            return
        self._mark_stuck_and_wake(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            source_comment_id=job.request.source_comment_id,
            timeout_seconds=job.request.timeout_seconds,
            reason="ready-at-base-2",
            detail=json.dumps(
                {
                    "project_bot": _detail_project_bot(job.request.detail),
                    "detail": f"{job.request.action} posted a ready at the base twice",
                    "job_id": job.id,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    def _ready_route(self, ticket: TrackedTicket) -> TrackedTicket:
        if not ticket.ready_starts_review or self._job_builder_harness(ticket) is not None:
            return ticket
        routed = self._routed_outside_queue(ticket)
        self._polled[(routed.repo.lower(), routed.ticket_id)] = routed
        return routed

    def _ready(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        ticket = self._ready_route(ticket)
        builder_harness = (
            ticket.factory.builder_harness
            if ticket.factory_md is not None
            else self._job_builder_harness(ticket)
        )
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
        if (
            builder_harness in self.refused_harnesses
            or ticket.factory.reviewer_harness in self.refused_harnesses
        ):
            return True, "rejected-ready-refused-harness"
        registration = (
            Registration.REGISTERED
            if ticket.ready_starts_review and self._job_builder_harness(ticket) is None
            else self._registration(ticket, comment, "ready")
        )
        if registration is Registration.UNREGISTERED:
            return True, "rejected-unregistered-ready"
        if registration is Registration.PENDING:
            return False, "ready-awaiting-registration"
        if self._superseded_by_rebase(ticket, comment):
            return True, "ready-superseded-by-rebase"
        state, review_round = self._review_context(ticket)
        if state in ("planning", "plan-check"):
            return True, "rejected-ready-in-planning"
        if state == "capped":
            self._flag_ready_after_cap(ticket, comment, fields["head"])
            return True, "capped-ready"
        parked = self._parked_for_max(ticket) if state == "waiting-max" else None
        if parked is not None:
            return True, f"ready-while-{parked}"
        if state == "blocked":
            return True, "blocked-ready"
        if _at_base(ticket, fields["head"]):
            self._ready_at_base(ticket, comment)
            return True, "rejected-ready-at-base"
        reported_head = fields["head"]
        reentry: int | None = None
        rebase: RebaseStep | None = None
        rereview = False
        update: UpdateScope | None = None
        if state == "updating":
            settled = self._settle_update(ticket, comment, reported_head)
            if isinstance(settled, tuple):
                return settled
            update = settled
            ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
            rereview = True
        elif state in ("handed-off", "merge-queue"):
            shipped = self._shipped_approval(ticket)
            approved = self._queue_row(ticket)["approved_head"]
            if (shipped is not None and _matching_head(reported_head, shipped[0])) or (
                approved is not None and _matching_head(reported_head, approved)
            ):
                return True, "ready-already-shipped"
            reentry = self._completed_round(ticket)
            if reentry + 1 > self._effective_cap(ticket):
                self._flag_ready_after_cap(ticket, comment, reported_head)
                return True, "ready-after-cap"
        elif state == "fixing" and (rebase := self._reviewed_rebase(ticket)) is not None:
            reentry = rebase.review_round - 1
        if state == "awaiting-ok" and ticket.current_head is not None:
            held = self._red_ci_approval_covering(ticket, reported_head)
            if held is not None:
                handled, shipped = self._ship_covered(ticket, *held, ticket.current_head)
                return handled, f"approval-covers:{shipped}"
        head = _matching_head(reported_head, ticket.current_head)
        if head is None:
            try:
                fresh = self._fresh_ticket(ticket)
            except BaseMismatch:
                return False, "base-mismatch"
            except Exception:
                return False, "pr-head-unavailable"
            if fresh is not None and fresh.merged:
                # #108: the next poll's own read records the merge.
                return True, "ready-on-merged-pr"
            if fresh is not None and self._review_context(fresh)[0] == "abandoned":
                return True, "ready-on-closed-pr"
            if fresh is not None:
                head = _matching_head(reported_head, fresh.current_head)
            if head is None:
                return True, "stale-ready-head"
            assert fresh is not None
            if _at_base(fresh, reported_head):
                self._ready_at_base(fresh, comment)
                return True, "rejected-ready-at-base"
            self._polled[(fresh.repo.lower(), fresh.ticket_id)] = fresh
            ticket = self._ready_route(fresh)
        if state == "fixing":
            self._restart_ci(ticket, head)
        if rebase is not None and _matching_head(head, rebase.from_head) is not None:
            return self._rebase_noop(ticket, rebase)
        answered = self._verdict_after(
            ticket,
            comment,
            head,
            reentry if reentry is not None else review_round,
            reentry=reentry is not None,
        )
        if answered:
            # #71: that review already ran. A ready posted after the verdict
            # (a fix that changed nothing) is still reviewed, as round N+1.
            if max(answered) > review_round and state in (None, "questions"):
                self._set_workflow_state(ticket, "review")
            return True, "stale-ready-verdict"
        if state == "waiting-max" and self._verdict_behind_question(
            ticket, comment, head, review_round
        ):
            return True, "stale-ready-verdict"
        if state in ("fixing", "awaiting-ok") and self._held_approve_covers(ticket, head):
            return True, "held-ci-fix-ready"
        gate = self._local_gate_hold(ticket, comment.id)
        if gate is not None and self._superseded_by_later_ready(ticket, comment):
            return True, "ready-superseded-by-ready"
        if gate is not None and gate[0] == LOCAL_GATE_UNAVAILABLE:
            return False, LOCAL_GATE_WAITING
        gate_red = None if gate is None else CIResult(
            "failure",
            log=f"ci.local_gate failed at {head}: {gate[1]}",
            failed=(FailedCheck("local_gate", cause="code"),),
        )
        result = gate_red or ticket.ci.get(head) or ticket.ci.get(reported_head)
        pending = result is None or result.status == "pending"
        covering = self._red_ci_approval_covering(ticket, head)
        if pending:
            if (
                (result is None or not result.reported)
                and ticket.mergeable_state == "dirty"
                and not self._queue_owns_base_updates(ticket.repo, ticket.ticket_id)
            ):
                return False, self._queue_base_update(ticket, comment, head, review_round)
            if covering is not None:
                return False, "approved-ci-pending"
            if not ticket.factory.start_before_ci:
                return False, "ci-pending"
        elif result.clears and covering is not None:
            handled, shipped = self._ship_covered(ticket, *covering, head)
            return handled, f"approval-covers:{shipped}"
        elif result.clears and self.feeder is not None:
            base = self.feeder.review_base_unless_stacked(ticket, head)
            if base is None:
                return False, "stacked-held"
            ticket = replace(ticket, factory=replace(ticket.factory, base_branch=base))
        retarget = rebase if rebase is not None and rebase.onto_from is not None else None
        delta = None
        if retarget is not None and (pending or result.clears):
            held = self._approval_held(ticket, retarget)
            if held and pending:
                return False, "retarget-carry-ci-pending"
            delta = self._retarget_delta_read(ticket, retarget, head)
            if held and carries(delta, ticket.factory.exclusive_paths):
                return self._carry_retarget_approval(ticket, retarget, head)
        if result is not None and gate_red is None:
            settled = self._settled_red(ticket, comment, head, result)
            if isinstance(settled, str):
                return False, settled
            result = settled
        if reentry is not None:
            if not self._reenter_after_handoff(
                ticket, reentry, after="handoff" if rebase is None else "rebase"
            ):
                return True, "stale-ready"
            review_round = reentry
        recorded_status, count = (
            (result.status if result is not None else "pending", 0)
            if pending or result.status == "not-run"
            else self._record_ci_result(ticket, head, result.status)
        )
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if covering is None and recorded_status in ("pending", "success", "not-run"):
            queued, answered = self._queue_review(
                ticket,
                comment,
                head=head,
                generation=generation,
                update=update,
                source=None if rebase is None else "rebase",
                retarget=retarget,
                delta=delta,
            )
            if not queued:
                return True, "capped-ready"
            if rereview:
                self._leave_merge_queue(ticket, "update-rereview")
            self._set_workflow_state(ticket, "review")
            return True, "answered-review" if answered else "queued-review"
        assert result is not None
        if self._mark_stuck_on_third_red(ticket, comment, head, count):
            return True, "stuck-ci-red-3"
        carried = ticket.factory.start_before_ci and self._ci_failure_carried(ticket, head)
        if carried is _HANDOFF_UNREAD:
            return False, "ci-red-handoff-unread"
        if carried:
            return True, "ci-red-in-flight"
        dedupe_key = self._ci_fix_dedupe_key(ticket, head, generation)
        if result.cause == "infra" and self._ci_fix_finished(dedupe_key, comment):
            self._flag_infra_blocked(ticket, head, result)
            return False, CI_INFRA_WAIT
        self._queue_ci_fix(
            ticket, comment, head, result, review_round, generation, dedupe_key=dedupe_key
        )
        if rereview:
            self._leave_merge_queue(ticket, "update-ci-red")
        if reentry is not None or rereview:
            self._set_workflow_state(ticket, "fixing")
        return True, "queued-ci-fix"

    def _superseded_by_later_ready(self, ticket: TrackedTicket, comment: Comment) -> bool:
        with self._connect() as connection:
            handled = {
                row["comment_id"]
                for row in connection.execute(
                    f"""
                    SELECT comment_id FROM handled_comments
                    WHERE repo = ? AND ticket_id = ?
                      AND outcome IN ({", ".join("?" * len(READY_TAKEOVERS))})
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, *READY_TAKEOVERS),
                )
            }
        return any(
            _comment_order(comment) < _comment_order(later)
            for later in ticket.comments
            if later.id in handled
        )

    def _local_gate_hold(self, ticket: TrackedTicket, comment_id: str) -> tuple[str, str] | None:
        """#1464: None once the gate passed at this ready, since a pass is never undone."""
        if not any(check.name == "local_gate" for check in planned(ticket.factory, ticket.pr_base)):
            return None
        prefix = f"handoff {comment_id}:"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event, detail FROM job_events
                WHERE repo = ? AND ticket_id = ? AND event IN (?, ?, ?)
                  AND substr(detail, 1, ?) = ?
                ORDER BY id
                """,
                (
                    ticket.repo.lower(), ticket.ticket_id, LOCAL_GATE_PASSED, LOCAL_GATE_FAILED,
                    LOCAL_GATE_UNAVAILABLE, len(prefix), prefix,
                ),
            ).fetchall()
        if not rows or any(row["event"] == LOCAL_GATE_PASSED for row in rows):
            return None
        return str(rows[-1]["event"]), str(rows[-1]["detail"]).split(": ", 2)[-1]

    def _ci_fix_finished(self, dedupe_key: str, comment: Comment) -> bool:
        with self._connect() as connection:
            fix = connection.execute(
                "SELECT state, source_comment_id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
        return (
            fix is not None
            and fix["state"] not in ("queued", "running")
            and fix["source_comment_id"] != comment.id
        )

    def _flag_infra_blocked(self, ticket: TrackedTicket, head: str, result: CIResult) -> None:
        now = self.queue.now()
        with self._connect() as connection:
            wait = connection.execute(
                "SELECT since FROM ci_waits WHERE repo = ? AND ticket_id = ? AND head = ?",
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchone()
            if wait is None or now - float(wait["since"]) < CI_INFRA_WAIT_SECONDS:
                return
            signals = ", ".join(
                dict.fromkeys(check.signal for check in failed_checks(result) if check.signal)
            )
            self._flag_attention_once(
                connection,
                ticket.repo,
                ticket.ticket_id,
                CI_INFRA_BLOCKED_REASON,
                f"CI on {head} is red for infrastructure ({signals or 'infra'}) "
                f"{(now - float(wait['since'])) / 60:.0f} minutes after a CI fix; "
                "the ready waits for green",
            )

    def _record_ci_wait(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_head: str,
        outcome: str,
    ) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        current = _matching_head(reported_head, ticket.current_head)
        with self._connect() as connection:
            if outcome not in CI_WAIT_REASONS:
                connection.execute(
                    "DELETE FROM ci_waits WHERE repo = ? AND ticket_id = ? AND comment_id = ?",
                    (*key, comment.id),
                )
                if outcome in READY_TAKEOVERS:
                    connection.execute(
                        "DELETE FROM ci_waits WHERE repo = ? AND ticket_id = ? AND reason = ?",
                        (*key, LOCAL_GATE_WAITING),
                    )
                return
            head = current or reported_head
            result = ticket.ci.get(head) or ticket.ci.get(reported_head)
            connection.execute(
                """
                INSERT INTO ci_waits
                    (repo, ticket_id, comment_id, head, reason, checks, since, next_round)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo, ticket_id) DO UPDATE SET
                    comment_id = excluded.comment_id,
                    head = excluded.head,
                    reason = excluded.reason,
                    checks = excluded.checks,
                    next_round = excluded.next_round,
                    since = CASE WHEN ci_waits.head = excluded.head
                                 THEN ci_waits.since ELSE excluded.since END
                """,
                (
                    *key,
                    comment.id,
                    head,
                    outcome,
                    json.dumps(
                        ["gate unavailable"] if outcome == LOCAL_GATE_WAITING
                        else list(result.pending) if result is not None
                        else []
                    ),
                    self.queue.now(),
                    self._completed_round(ticket) + 1,
                ),
            )

    def _completed_round(self, ticket: TrackedTicket) -> int:
        state, completed = self._review_context(ticket)
        if state in ("handed-off", "merge-queue"):
            return _approved_round(self._shipped_approval(ticket), completed)
        return completed

    def _watch_ci_silence(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        head = ticket.current_head
        result = ticket.ci.get(head) if head is not None else None
        silent = (
            head is not None
            and (result is None or not result.reported)
            and ticket.pr_base is not None
            and ticket.no_ci_base is None
            and self._review_context(ticket)[0] not in TERMINAL_STATES
            and self._names_head(ticket, head, ("ready",))
        )
        now = self.queue.now()
        with self._connect() as connection:
            if not silent:
                connection.execute("DELETE FROM ci_silence WHERE repo = ? AND ticket_id = ?", key)
                connection.execute(
                    "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                    (*key, CI_SILENT_REASON),
                )
                return
            since = connection.execute(
                """
                INSERT INTO ci_silence (repo, ticket_id, head, since) VALUES (?, ?, ?, ?)
                ON CONFLICT (repo, ticket_id) DO UPDATE SET
                    head = excluded.head,
                    since = CASE WHEN ci_silence.head = excluded.head
                                 THEN ci_silence.since ELSE excluded.since END
                RETURNING since
                """,
                (*key, head, now),
            ).fetchone()["since"]
            if now - since < CI_SILENT_SECONDS:
                connection.execute(
                    "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                    (*key, CI_SILENT_REASON),
                )
                return
            where = (
                f"on CI base {ticket.pr_base}"
                if ticket.factory.ci_branches is not None
                else f"on base {ticket.pr_base} (factory.md sets no ci.branches)"
            )
            self._flag_attention_once(
                connection,
                ticket.repo,
                ticket.ticket_id,
                CI_SILENT_REASON,
                f"no check run or status on {head[:12]} after "
                f"{CI_SILENT_SECONDS / 60:.0f} minutes {where}",
            )

    def _ci_stale_state(self, ticket: TrackedTicket) -> str | None:
        if self._review_context(ticket)[0] in TERMINAL_STATES:
            return None
        key = (ticket.repo.lower(), ticket.ticket_id)
        head = ticket.current_head
        result = ticket.ci.get(head) if head is not None else None
        with self._connect() as connection:
            stuck = connection.execute(
                "SELECT stuck_reason FROM ticket_state WHERE repo = ? AND ticket_id = ? AND stuck = 1",
                key,
            ).fetchone()
            if stuck is not None:
                return "ci-red-3" if stuck["stuck_reason"] == "ci-red-3" else None
            wait = connection.execute(
                "SELECT reason FROM ci_waits WHERE repo = ? AND ticket_id = ?", key
            ).fetchone()
            if wait is not None and wait["reason"] in (CI_INFRA_WAIT, CI_BASE_RED):
                return str(wait["reason"])
            if result is None or result.status != "failure":
                return None
            held = connection.execute(
                """
                SELECT 1 FROM ci_reruns
                WHERE repo = ? AND ticket_id = ? AND head = ? AND outcome = ?
                """,
                (*key, head, CI_BASE_RED),
            ).fetchone()
            if held is not None:
                return CI_BASE_RED
            if _harness_job_active(connection, key):
                return None
        return "ci-failed"

    def _clear_recovered_base_red(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        current = ticket.current_head
        if current is None:
            return
        with self._connect() as connection:
            wait = connection.execute(
                "SELECT head FROM ci_waits WHERE repo = ? AND ticket_id = ? AND reason = ?",
                (*key, CI_BASE_RED),
            ).fetchone()
            if wait is None:
                return
            result = ticket.ci.get(current)
            if _matching_head(str(wait["head"]), current) is None or (
                result is not None and result.clears
            ):
                connection.execute(
                    "DELETE FROM ci_waits WHERE repo = ? AND ticket_id = ? AND reason = ?",
                    (*key, CI_BASE_RED),
                )

    def _watch_ci_stale(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        state = self._ci_stale_state(ticket)
        head = ticket.current_head or "-"
        now = self.queue.now()
        with self._connect() as connection:
            if state is None:
                connection.execute("DELETE FROM ci_stale WHERE repo = ? AND ticket_id = ?", key)
                connection.execute(
                    "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                    (*key, CI_STALE_REASON),
                )
                return
            prior = connection.execute(
                "SELECT state, head, since FROM ci_stale WHERE repo = ? AND ticket_id = ?", key
            ).fetchone()
            if prior is None or (prior["state"], prior["head"]) != (state, head):
                connection.execute(
                    """
                    INSERT INTO ci_stale (repo, ticket_id, state, head, since)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (repo, ticket_id) DO UPDATE SET
                        state = excluded.state, head = excluded.head, since = excluded.since
                    """,
                    (*key, state, head, now),
                )
                connection.execute(
                    "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                    (*key, CI_STALE_REASON),
                )
                return
            waited = now - float(prior["since"])
            if waited < CI_STALE_SECONDS:
                return
            text = f"CI {state} on {head[:12]} for {waited / 3600:.1f} hours with no progress"
            self._flag_attention_once(connection, ticket.repo, ticket.ticket_id, CI_STALE_REASON, text)
        source = self._latest_trusted_comment(ticket)
        self.wake_grok_once(
            ticket,
            reason=CI_STALE_REASON,
            source_comment_id=source.id if source is not None else "",
            detail=self._project_wake_detail(ticket, text),
            head=ticket.current_head,
            once_key=f"{state}:{head}",
        )

    def _names_head(self, ticket: TrackedTicket, head: str, kinds: tuple[str, ...]) -> bool:
        for comment in ticket.comments:
            if comment.edited_at is not None:
                continue
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] not in kinds or _matching_head(fields.get("head", ""), head) is None:
                continue
            if not self._trusted_session(ticket, comment, fields):
                continue
            outcome = self._handled_outcome(ticket, comment.id)
            if outcome is None or not outcome.startswith("rejected-"):
                return True
        return False

    def _settled_red(
        self, ticket: TrackedTicket, comment: Comment, head: str, result: CIResult
    ) -> CIResult | CIHold:
        if result.status != "failure":
            return result
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT outcome, job_ids, requested_at FROM ci_reruns
                WHERE repo = ? AND ticket_id = ? AND head = ?
                """,
                (*key, head),
            ).fetchone()
        if row is not None and row["outcome"] == "requested":
            # GitHub gives rerun jobs new ids: the old ids are the attempt
            # before the rerun, which a stale read can still show.
            before = set(json.loads(row["job_ids"]))
            if (
                any(check.job_id in before for check in failed_checks(result))
                and self.queue.now() - float(row["requested_at"]) < CI_RERUN_SETTLE_SECONDS
            ):
                return CI_RERUN
        from lib.evidence.ci import classify_logged

        logs = self._failed_job_logs(ticket.repo, result)
        result = classify_logged(result, logs, self._unlogged_job_steps(ticket.repo, result, logs))
        failed = failed_checks(result)
        job_ids = [check.job_id for check in failed if check.job_id is not None]
        held = row is not None and row["outcome"] == CI_BASE_RED
        if self._base_explains(ticket, result, logs, held=held):
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO ci_reruns
                        (repo, ticket_id, head, outcome, job_ids, requested_at, detail)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (repo, ticket_id, head) DO UPDATE SET
                        outcome = excluded.outcome,
                        job_ids = excluded.job_ids,
                        detail = excluded.detail,
                        requested_at = CASE WHEN ci_reruns.outcome = excluded.outcome
                                       THEN ci_reruns.requested_at
                                       ELSE excluded.requested_at END
                    """,
                    (*key, head, CI_BASE_RED, json.dumps(job_ids), self.queue.now(), ticket.pr_base),
                )
            self._record_ci_wait(ticket, comment, head, CI_BASE_RED)
            if self.log is not None and not held:
                self.log(
                    f"ci base red: {ticket.repo}#{ticket.ticket_id} at {head[:12]} fails "
                    f"as {ticket.pr_base} does; holding with no charge"
                )
            return CI_BASE_RED
        if held:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM ci_reruns WHERE repo = ? AND ticket_id = ? AND head = ?",
                    (*key, head),
                )
                connection.execute(
                    "DELETE FROM ci_waits WHERE repo = ? AND ticket_id = ? AND reason = ?",
                    (*key, CI_BASE_RED),
                )
            row = None
        if self.ci_rerun is None or row is not None:
            return result
        if result.pending:
            return CI_RERUN
        run_ids = list(dict.fromkeys(check.run_id for check in failed if check.run_id is not None))
        outcome, detail, why = "unavailable", None, ""
        if run_ids:
            try:
                if self.ci_rerun(ticket.repo, run_ids) == "running":
                    return CI_RERUN
                outcome = "requested"
            except Exception as exc:
                outcome, detail, why = "refused", *_safe_refusal(exc)
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO ci_reruns
                    (repo, ticket_id, head, outcome, job_ids, requested_at, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*key, head, outcome, json.dumps(job_ids), self.queue.now(), detail),
            ).rowcount
        runs = ", ".join(run_ids)
        if self.log is not None and outcome != "unavailable":
            self.log(
                f"ci rerun: {ticket.repo}#{ticket.ticket_id} at {head[:12]} "
                + (
                    f"rerunning the failed jobs of run {runs} (cause {result.cause})"
                    + (f" after {ticket.pr_base} went green" if held else "")
                    if outcome == "requested"
                    else f"rerun of run {runs} refused ({detail}); charging the failure"
                )
            )
        if outcome == "refused" and inserted:
            self._flag_rerun_refused(ticket, head, runs, why)
        return CI_RERUN if outcome == "requested" else result

    def _base_explains(
        self,
        ticket: TrackedTicket,
        result: CIResult,
        logs: Mapping[str, str | None],
        *,
        held: bool,
    ) -> bool:
        if not ticket.pr_base:
            return False
        if self.feeder is not None and self.feeder.main_fix_pr(ticket.repo, ticket.ticket_id):
            return False
        base = self._base_checks_for(ticket.repo, ticket.pr_base)
        if base is None:
            return held
        from lib.evidence.ci import base_red

        red = [
            FailedCheck(check.name, check.run_id, check.job_id)
            for check in base.values()
            if check.conclusion in CHECK_FAILED
        ]
        if not red:
            return False
        return base_red(failed_checks(result), logs, base, self._job_logs(ticket.repo, red))

    def _flag_rerun_refused(self, ticket: TrackedTicket, head: str, runs: str, why: str) -> None:
        self._flag_refused(
            ticket, head, f"{ticket.repo}#{ticket.ticket_id} head {head[:12]}: "
            f"rerun-failed-jobs for run {runs} refused{why}"
        )

    def _request_fresh_ci(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        head = ticket.current_head
        result = ticket.ci.get(head) if head is not None else None
        if head is None or result is None or self.ci_full_rerun is None:
            return
        # A rerun reuses the run's original merge ref, so it can never pick up
        # a base workflow change or prove CI on a retargeted base.
        changes = [
            change for change in result.stale if change.kind not in ("base-workflow", "retarget")
        ]
        if not changes or self._is_stuck(ticket):
            return
        state = self._review_context(ticket)[0]
        if state not in _AFTER_BUILD_STATES and not (
            state == "building" and self._names_head(ticket, head, ("ready",))
        ):
            return
        trigger = " ".join(sorted(change.trigger for change in changes))
        with self._connect() as connection:
            if _harness_job_active(connection, key) or connection.execute(
                """
                SELECT 1 FROM ci_fresh_runs
                WHERE repo = ? AND ticket_id = ? AND head = ? AND trigger = ?
                """,
                (*key, head, trigger),
            ).fetchone():
                return
        units = [
            (unit, rows)
            for change in changes
            for unit, rows in stale_units(result, change, ticket.factory.ci_required_checks)
        ]
        unrunnable = [unit for unit, rows in units if not any(row.run_id for row in rows)]
        run_ids = list(
            dict.fromkeys(row.run_id for _unit, rows in units for row in rows if row.run_id)
        )
        outcome, detail = "refused", None
        why = f": no Actions run to rerun for {', '.join(unrunnable)}"
        if not unrunnable:
            try:
                if self.ci_full_rerun(ticket.repo, run_ids) == "running":
                    return
                outcome, why = "requested", ""
            except Exception as exc:
                detail, why = _safe_refusal(exc)
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO ci_fresh_runs
                    (repo, ticket_id, head, trigger, outcome, run_ids, requested_at, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*key, head, trigger, outcome, json.dumps(run_ids), self.queue.now(), detail),
            ).rowcount
        stale = ", ".join(result.pending)
        if self.log is not None:
            self.log(
                f"ci fresh run: {ticket.repo}#{ticket.ticket_id} at {head[:12]} "
                + (
                    f"rerunning run {', '.join(run_ids)} in full ({stale})"
                    if outcome == "requested"
                    else f"fresh run refused ({stale}){why}; holding pending"
                )
            )
        if outcome == "refused" and inserted:
            self._flag_refused(
                ticket, head, f"{ticket.repo}#{ticket.ticket_id} head {head[:12]}: "
                f"fresh CI run for {stale} refused{why}"
            )

    def _flag_refused(self, ticket: TrackedTicket, head: str, text: str) -> None:
        self.flag_attention(ticket.repo, ticket.ticket_id, CI_RERUN_REFUSED, text)
        source = self._latest_trusted_comment(ticket)
        if source is not None:
            self.wake_grok_once(
                ticket, reason=CI_RERUN_REFUSED, head=head, source_comment_id=source.id, detail=text
            )

    def _charge_unannounced_red(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        head = ticket.current_head
        result = ticket.ci.get(head) if head is not None else None
        if head is None or result is None or result.status != "failure":
            return
        if self._review_context(ticket)[0] not in _AFTER_BUILD_STATES or self._is_stuck(ticket):
            return
        if self._handed_off_by_max(*key):
            return
        if self._names_head(ticket, head, ("ready", "verdict")):
            return
        with self._connect() as connection:
            charged = connection.execute(
                "SELECT 1 FROM ci_events WHERE repo = ? AND ticket_id = ? AND head = ?",
                (*key, head),
            ).fetchone()
            updating = connection.execute(
                """
                SELECT 1 FROM ticket_state
                WHERE repo = ? AND ticket_id = ? AND update_from_head IS NOT NULL
                """,
                key,
            ).fetchone()
            if charged or updating or _harness_job_active(connection, key):
                return
        source = self._latest_trusted_comment(ticket)
        if source is None:
            return
        settled = self._settled_red(ticket, source, head, result)
        if isinstance(settled, str):
            return
        result = settled
        _status, count = self._record_ci_result(ticket, head, "failure")
        if self._mark_stuck_on_third_red(ticket, source, head, count):
            outcome = "stuck-ci-red-3"
        else:
            generation = self._generation(ticket.repo, ticket.ticket_id)
            self._queue_ci_fix(
                ticket,
                source,
                head,
                result,
                self._completed_round(ticket),
                generation,
                dedupe_key=self._ci_fix_dedupe_key(ticket, head, generation),
            )
            outcome = "CI fix queued"
        if self.log is not None:
            self.log(
                f"unannounced red: {ticket.repo}#{ticket.ticket_id} at {head[:12]} "
                f"with no ready or verdict on it: {outcome}"
            )

    def _resume_unannounced_review(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        head = ticket.current_head
        state, completed = self._review_context(ticket)
        if head is None or state not in _UNANNOUNCED_GREEN_STATES or state == "review":
            return
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT review.head, review.detail FROM jobs AS review
                WHERE lower(review.repo) = ? AND review.ticket_id = ?
                  AND review.action = 'review'
                  AND review.state IN ('queued', 'running', 'done')
                  AND NOT EXISTS (
                      SELECT 1 FROM ticket_transitions
                      WHERE repo = ? AND ticket_id = ? AND at >= review.created_at
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs
                      WHERE lower(repo) = ? AND ticket_id = ? AND {HARNESS_WORK}
                        AND id != review.id AND created_at >= review.created_at
                  )
                """,
                key * 3,
            ).fetchall()
        if any(
            _matching_head(str(row["head"] or ""), head)
            and _detail_field(row["detail"], "source") == _UNANNOUNCED_GREEN_SOURCE
            and _detail_round_number(row["detail"]) == completed + 1
            for row in rows
        ):
            self._set_workflow_state(ticket, "review")

    def _review_unannounced_green(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._polled.get(key, ticket)
        head = ticket.current_head
        result = ticket.ci.get(head) if head is not None else None
        if head is None or result is None or result.status != "success":
            return
        state, completed = self._review_context(ticket)
        if state not in _UNANNOUNCED_GREEN_STATES:
            return
        if self._is_stuck(ticket) or self._names_head(ticket, head, ("ready", "verdict")):
            return
        with self._connect() as connection:
            held = connection.execute(
                """
                SELECT 1 FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                  AND (paused_action IS NOT NULL OR update_from_head IS NOT NULL)
                """,
                key,
            ).fetchone()
            jobs = connection.execute(
                """
                SELECT action, head, detail FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action IN ('build', 'review')
                """,
                key,
            ).fetchall()
            active = _harness_job_active(connection, key)
        if held:
            return
        reviews = [
            row
            for row in jobs
            if row["action"] == "review" and _matching_head(str(row["head"] or ""), head)
        ]
        if reviews:
            return
        if active or any(
            row["action"] == "build"
            and _matching_head(str(_detail_field(row["detail"], "start_head") or ""), head)
            for row in jobs
        ):
            return
        if self._open_job_at(ticket, head):
            return
        source = self._latest_trusted_comment(ticket, kinds=("ready", "go"))
        if source is None:
            return
        if self.feeder is not None:
            base = self.feeder.review_base_unless_stacked(ticket, head)
            if base is None:
                return
            ticket = replace(ticket, factory=replace(ticket.factory, base_branch=base))
        review_round = completed + 1
        if review_round > self._effective_cap(ticket):
            detail = (
                f"head {head[:12]} was pushed with no ready and went green past the "
                "review cap; nothing was queued"
            )
            with self._connect() as connection:
                self._flag_attention_once(
                    connection, ticket.repo, ticket.ticket_id, UNANNOUNCED_AFTER_CAP, detail
                )
            self.wake_grok_once(
                ticket,
                reason="capped",
                source_comment_id=source.id,
                head=head,
                once_key=f"unannounced:{head}",
                detail=self._project_wake_detail(ticket, detail),
            )
            outcome = f"review round {review_round} is past the cap; flagged"
        else:
            self._record_ci_result(ticket, head, "success")
            self._queue_review(
                ticket,
                source,
                head=head,
                generation=self._generation(ticket.repo, ticket.ticket_id),
                source=_UNANNOUNCED_GREEN_SOURCE,
            )
            self._set_workflow_state(ticket, "review")
            outcome = f"review round {review_round} queued"
        if self.log is not None:
            self.log(
                f"unannounced green: {ticket.repo}#{ticket.ticket_id} at {head[:12]}: {outcome}"
            )

    def _latest_trusted_comment(
        self, ticket: TrackedTicket, kinds: tuple[str, ...] | None = None
    ) -> Comment | None:
        for comment in sorted(ticket.comments, key=_comment_order, reverse=True):
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if kinds is not None and fields["kind"] not in kinds:
                continue
            outcome = self._handled_outcome(ticket, comment.id)
            if self._trusted_session(ticket, comment, fields) and not (
                outcome or ""
            ).startswith("rejected-"):
                return comment
        return None

    def _review_after_green_rerun(self, ticket: TrackedTicket) -> None:
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        head = ticket.current_head
        result = ticket.ci.get(head) if head is not None else None
        if result is None or result.status != "success" or self._is_stuck(ticket):
            return
        state = self._review_context(ticket)[0]
        if state not in TERMINAL_STATES:
            with self._connect() as connection:
                failed = connection.execute(
                    """
                    SELECT 1 FROM ci_events
                    WHERE repo = ? AND ticket_id = ? AND head = ? AND status = 'failure'
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, head),
                ).fetchone()
            if failed is not None:
                self._record_ci_result(ticket, head, "success")
        if state not in _RERUN_STATES:
            return
        latest = self._latest_ready_at(ticket, head)
        if latest is None or self._open_job_at(ticket, head):
            return
        comment, fields = latest
        handled, outcome = self._t2(ticket, comment, fields)
        if not handled:
            return
        self._rehandle(ticket, comment.id, outcome)
        if self.log is not None:
            self.log(
                f"green rerun at {head[:12]}: ready {comment.id} {outcome} "
                f"({ticket.repo}#{ticket.ticket_id})"
            )

    def _latest_ready_at(
        self, ticket: TrackedTicket, head: str
    ) -> tuple[Comment, dict[str, str]] | None:
        for comment in sorted(ticket.comments, key=_comment_order, reverse=True):
            if comment.edited_at is not None:
                continue
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] != "ready" or _matching_head(fields["head"], head) is None:
                continue
            outcome = self._handled_outcome(ticket, comment.id)
            if outcome is None or outcome.startswith("rejected-"):
                continue
            return (comment, fields) if outcome in _CI_RED_READY else None
        return None

    def _open_job_at(self, ticket: TrackedTicket, head: str) -> bool:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT head FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action IN ('review', 'fix')
                  AND (state IN ('queued', 'running') OR (state = 'failed' AND failure_handled = 0))
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        return any(_matching_head(str(row["head"] or ""), head) is not None for row in rows)

    def _mark_stuck_on_third_red(
        self, ticket: TrackedTicket, comment: Comment, head: str, count: int
    ) -> bool:
        if count < 3:
            return False
        self._mark_stuck_and_wake(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            reason="ci-red-3",
            detail=self._project_wake_detail(
                ticket,
                f"CI failed three times in a row; latest head {head}",
            ),
        )
        return True

    def _queue_ci_fix(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
        result: CIResult,
        review_round: int,
        generation: int,
        *,
        dedupe_key: str,
    ) -> None:
        self._enqueue_or_flag(
            ticket,
            comment,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="fix",
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
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
            dedupe_key=dedupe_key,
            ci=self._ci_fix_evidence(ticket.repo, result, dedupe_key),
        )

    def _ci_fix_evidence(
        self, repo: str, result: CIResult, dedupe_key: str
    ) -> CIEvidence | None:
        """Runs outside the enqueue's transaction, so no network read holds the DB lock."""
        from lib.evidence.ci import ci_evidence

        with self._connect() as connection:
            held = connection.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
        if held is not None:
            return None
        return ci_evidence(failed=failed_checks(result), logs=self._failed_job_logs(repo, result))

    def _failed_job_logs(self, repo: str, result: CIResult) -> dict[str, str | None]:
        return self._job_logs(repo, failed_checks(result))

    def _job_logs(self, repo: str, failed: Sequence[FailedCheck]) -> dict[str, str | None]:
        logs: dict[str, str | None] = {}
        if self.ci_logs is None:
            return logs
        for run_id in dict.fromkeys(check.run_id for check in failed if check.run_id is not None):
            checks = [check for check in failed if check.run_id == run_id]
            key = (repo.lower(), run_id, tuple(check.job_id for check in checks))
            run_logs = self._ci_log_reads.get(key, {})
            if key not in self._ci_log_reads and key not in self._ci_log_failures:
                try:
                    run_logs = self._ci_log_reads[key] = self.ci_logs(repo, run_id)
                except Exception as exc:
                    self._ci_log_failures.add(key)
                    if self.log is not None:
                        self.log(f"ci log: {repo} run {run_id} unavailable: {type(exc).__name__}")
                if len(self._ci_log_reads) > _CI_LOG_READS:
                    del self._ci_log_reads[next(iter(self._ci_log_reads))]
            for check in checks:
                if check.job_id is not None:
                    logs[check.job_id] = run_logs.get(check.name)
        return logs

    def _base_checks_for(self, repo: str, base: str) -> Mapping[str, BaseCheck] | None:
        key = (repo.lower(), base)
        if key not in self._base_checks:
            self._base_checks[key] = None
            if self.ci_base_checks is not None:
                try:
                    self._base_checks[key] = self.ci_base_checks(repo, base)
                except Exception as exc:
                    if self.log is not None:
                        self.log(f"ci base: {repo} {base} unavailable: {type(exc).__name__}")
        return self._base_checks[key]

    def _unlogged_job_steps(
        self, repo: str, result: CIResult, logs: Mapping[str, str | None]
    ) -> dict[str, int | None]:
        steps: dict[str, int | None] = {}
        if self.ci_runs is None:
            return steps
        unlogged: dict[str, list[str]] = {}
        for check in failed_checks(result):
            if (
                check.cause != "infra"
                and check.run_id is not None
                and check.job_id is not None
                and logs.get(check.job_id) is None
            ):
                unlogged.setdefault(check.run_id, []).append(check.job_id)
        for run_id, job_ids in unlogged.items():
            key = (repo.lower(), run_id, tuple(job_ids))
            if key not in self._ci_step_reads and key not in self._ci_step_failures:
                try:
                    self._ci_step_reads[key] = {
                        job.job_id: job.steps for job in self.ci_runs(repo, run_id)
                    }
                except Exception as exc:
                    self._ci_step_failures.add(key)
                    if self.log is not None:
                        self.log(f"ci jobs: {repo} run {run_id} unavailable: {type(exc).__name__}")
                if len(self._ci_step_reads) > _CI_LOG_READS:
                    del self._ci_step_reads[next(iter(self._ci_step_reads))]
            steps.update(self._ci_step_reads.get(key, {}))
        return steps

    def _reenter_after_handoff(
        self, ticket: TrackedTicket, approved_round: int, *, after: str = "handoff"
    ) -> bool:
        """#108: a trusted ready after handoff re-enters review. The approved
        round counts as completed, so the next review is approved_round + 1.
        A merge-queue ticket (#130) leaves the queue the same way, and a
        rebased head (#1408) from fixing. False when the ticket left those
        states meanwhile."""
        states = _REENTERS_FROM[after]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                f"""
                UPDATE ticket_state
                SET review_round = ?, approved_head = NULL, approved_at = NULL,
                    approve_comment_id = NULL, update_failures = 0,
                    update_from_head = NULL
                WHERE repo = ? AND ticket_id = ?
                  AND workflow_state IN ({", ".join("?" for _ in states)})
                """,
                (approved_round, ticket.repo.lower(), ticket.ticket_id, *states),
            ).rowcount
            connection.commit()
        if changed == 1 and self.log is not None:
            self.log(
                f"re-entry: {ticket.repo}#{ticket.ticket_id} after {after}, "
                f"next review round {approved_round + 1}"
            )
        return changed == 1

    def _reviewed_rebase(self, ticket: TrackedTicket) -> RebaseStep | None:
        with self._connect() as connection:
            rebase = newest_rebase(connection, (ticket.repo.lower(), ticket.ticket_id))
        if rebase is None:
            return None
        reviewed = self._latest_review_head(ticket)
        if reviewed is None or _matching_head(rebase.from_head, reviewed) is None:
            return None
        return rebase

    def _rebase_noop(self, ticket: TrackedTicket, rebase: RebaseStep) -> tuple[bool, str]:
        self._write_state_label(ticket, rebase.from_state)
        with self._connect() as connection:
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                rebase.from_state,
                source="rebase-noop",
                at=self.queue.now(),
                also=rebase.approval(),
            )
            connection.commit()
        return True, "rebase-noop"

    def _restart_ci(self, ticket: TrackedTicket, head: str) -> None:
        """A retarget rebase that pushed no head fires no CI on the new base, so
        close and reopen the PR to fire pull_request: reopened (#1496)."""
        with self._connect() as connection:
            rebase = newest_rebase(connection, (ticket.repo.lower(), ticket.ticket_id))
            record = own_close_at_head(connection, ticket)
            if (
                rebase is None
                or rebase.onto_from is None
                or _matching_head(head, rebase.from_head) is None
                or (record is not None and record[0] == "reopened")
            ):
                return
            record_own_close(connection, ticket, head, "closing", self.queue.now())
        actions = self._review_actions(ticket)
        actions.close_pull(ticket)
        actions.reopen(ticket)
        with self._connect() as connection:
            record_own_close(connection, ticket, head, "reopened", self.queue.now())
        if self.log is not None:
            self.log(
                f"restarted CI: {ticket.repo}#{ticket.ticket_id} closed and reopened at {head[:12]}"
                " after a retarget rebase pushed no head"
            )

    def _verdict_after(
        self,
        ticket: TrackedTicket,
        ready: Comment,
        head: str,
        review_round: int,
        *,
        reentry: bool = False,
    ) -> set[int]:
        """The rounds of trusted reviewer verdicts for head, posted after the
        ready, that are the first for their round (#71).

        A verdict whose round already had one at or before the ready (a late
        duplicate) answered an earlier ready. One with no round, a round
        above the cap, or a round past the one this ready asks for answers
        none: T3 and T4 act on no such verdict. Nor does one for a completed
        round that T3 did not act on: another verdict closed that round.
        A verdict for the next round answers it only if T3 or T4 could act
        on it once T2 moves the ticket to review: not BLOCKED, not while the
        ticket is in a state other than none, questions, review or
        awaiting-ok, not an APPROVE for a head other than the last reviewed
        one, not one they already handled without acting, and not one behind
        a question pause from the ready on.

        With #108, review_round is the last completed round (the approved
        round for a ready that re-enters review after handoff, which T2 moves
        to review), so this ready would start review_round + 1. A verdict for
        that round answers this ready only once that round's review job for
        head is recorded; on a thread read whole with none (an empty DB) it
        is the verdict of the review this ready starts, which _enqueue_step
        records as answered (answered-review, no claimable job) so that
        verdict can then act.
        """
        next_round = review_round + 1
        last_round = min(
            self._effective_cap(ticket),
            next_round
            if self._has_review_job(ticket, head, next_round)
            else review_round,
        )
        actable = self._review_context(ticket)[0] in (
            None, "questions", "review", "awaiting-ok"
        ) or reentry
        review_head = self._latest_review_head(ticket)
        approvable = (
            review_head is not None and _matching_head(head, review_head) is not None
        )
        with self._connect() as connection:
            handled = {
                str(row["comment_id"]): str(row["outcome"])
                for row in connection.execute(
                    """
                    SELECT comment_id, outcome FROM handled_comments
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            }
            round_closers = {
                str(row["comment_id"])
                for row in connection.execute(
                    """
                    SELECT comment_id FROM review_events
                    WHERE repo = ? AND ticket_id = ?
                    AND outcome IN ('queued-review-fix', 'capped-review')
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            }
        before: set[int] = set()
        after: set[int] = set()
        for item in ticket.comments:
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            round_number = _round_number(fields.get("round"))
            if (
                round_number is not None
                and round_number <= last_round
                and (
                    item.id in round_closers
                    if round_number <= review_round
                    else actable
                    and (item.id not in handled or _acted_verdict(handled[item.id]))
                    and (approvable or fields.get("verdict") != "APPROVE")
                    and not self._paused_between(ticket, ready, item)
                )
                and fields.get("kind") == "verdict"
                and fields.get("verdict") in ("REVISE", "APPROVE")
                and self._counted_verdict(ticket, item, fields)
                and _matching_head(fields.get("head") or "", head) is not None
            ):
                if _comment_order(item) <= _comment_order(ready):
                    before.add(round_number)
                else:
                    after.add(round_number)
        return after - before

    def _verdict_behind_question(
        self,
        ticket: TrackedTicket,
        ready: Comment,
        head: str,
        review_round: int,
    ) -> bool:
        """Whether T5 holds a review of head at waiting-max and a trusted
        REVISE or APPROVE for head and a round past review_round follows the
        ready.

        _verdict_after skips that verdict, but the review this ready would
        queue takes it as answered (_answered_by), so T2 would clear the
        pause and T3 or T4 would act on it with no trusted answer. Max's
        answer resumes the paused review instead (Codex PR #112 r9 F1)."""
        with self._connect() as connection:
            paused = connection.execute(
                """
                SELECT paused_action, paused_head FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if (
            paused is None
            or paused["paused_action"] != "review"
            or _matching_head(str(paused["paused_head"] or ""), head) is None
        ):
            return False
        for item in ticket.comments:
            if _comment_order(item) <= _comment_order(ready):
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            round_number = _round_number(fields.get("round"))
            if (
                round_number is not None
                and round_number > review_round
                and fields.get("kind") == "verdict"
                and fields.get("verdict") in ("REVISE", "APPROVE")
                and self._counted_verdict(ticket, item, fields)
                and _matching_head(fields.get("head") or "", head) is not None
            ):
                return True
        return False

    def _paused_between(
        self, ticket: TrackedTicket, ready: Comment, verdict: Comment
    ) -> bool:
        """Whether T5 holds the ticket at waiting-max at any point from ready
        to verdict. With the ready unreviewed, answers resume clarify, not
        review, so T3 and T4 act on no such verdict (#112 r7, r8)."""
        start, end = _comment_order(ready), _comment_order(verdict)
        waiting = held = False
        for item in sorted(ticket.comments, key=_comment_order):
            order = _comment_order(item)
            if order >= end:
                break
            held = held or (waiting and order >= start)
            try:
                handoff = parse_comment(item.body)
            except HandoffError:
                continue
            fields = handoff.as_dict()
            if fields.get("kind") == "questions":
                waiting = waiting or (
                    self._trusted_session(ticket, item, fields)
                    and (
                        fields.get("state") == "waiting-max"
                        or (
                            fields.get("role") != "orchestrator"
                            and _has_questions(handoff.body)
                        )
                    )
                )
            elif fields.get("kind") == "answers" and self._trusted(
                ticket, item, fields, "grok-bot", role="orchestrator"
            ):
                waiting = False
        return held or waiting

    def _queue_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        *,
        head: str,
        generation: int,
        detail: str | None = None,
        update: UpdateScope | None = None,
        source: str | None = None,
        retarget: RebaseStep | None = None,
        delta: RetargetDelta | None = None,
    ) -> tuple[bool, Comment | None]:
        """Queue the next review round for comment, with the later trusted
        verdict that already answered it (_enqueue_step); (False, None), with
        nothing queued, past the cap."""
        _state, completed_rounds = self._review_context(ticket)
        review_round = completed_rounds + 1
        if source != "rebase" and review_round > self._effective_cap(ticket):
            self.flag_attention(
                ticket.repo,
                ticket.ticket_id,
                "ready-after-cap",
                f"review round {review_round} for head {head[:12]} is past the cap "
                f"and no extra round was approved (comment {comment.id})",
            )
            return False, None
        _job, answered = self._enqueue_or_flag(
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
                    update=update,
                    source=source,
                    rebase=(
                        self._rebase_scope(ticket, comment, head, review_round, retarget, delta)
                        if (review_round > 1 or source == "rebase") and update is None
                        else None
                    ),
                    size_flag=self._size_flag(ticket),
                ),
            ),
            dedupe_key=self._review_dedupe_key(ticket, head, review_round),
        )
        self._queue_qa(ticket, comment, head=head, review_round=review_round)
        return True, answered

    def _size_flag(self, ticket: TrackedTicket) -> dict[str, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT plan_estimate FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        estimate = None if row is None else row["plan_estimate"]
        changed = ticket.changed_lines
        if (
            estimate is None
            or changed is None
            or changed <= max(ticket.factory.plan_max_lines, padded_estimate(estimate))
        ):
            return None
        return {"changed_lines": changed, "estimate": estimate}

    def _review_dedupe_key(
        self, ticket: TrackedTicket, head: str, review_round: int
    ) -> str:
        key = f"review:{_scope(ticket.repo, ticket.ticket_id)}:{head}:r{review_round}"
        with self._connect() as connection:
            fix = connection.execute(
                """
                SELECT fix.id FROM jobs AS keyed
                JOIN jobs AS review
                  ON review.id = keyed.id
                  OR review.dedupe_key LIKE 't6-retry:' || keyed.id || ':%'
                JOIN jobs AS fix
                  ON lower(fix.repo) = lower(review.repo)
                 AND fix.ticket_id = review.ticket_id
                 AND fix.action = 'fix' AND fix.head = review.head
                 AND fix.rowid > review.rowid
                WHERE keyed.dedupe_key = ? AND review.state = 'done'
                ORDER BY fix.rowid DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
        return key if fix is None else f"{key}:after-{fix['id']}"

    def _ci_fix_dedupe_key(self, ticket: TrackedTicket, head: str, generation: int) -> str:
        key = f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}:g{generation}"
        repeat = f"{key}:after-"
        with self._connect() as connection:
            latest = connection.execute(
                """
                SELECT keyed.dedupe_key, (
                    SELECT later.id FROM jobs AS later
                    WHERE lower(later.repo) = lower(keyed.repo)
                      AND later.ticket_id = keyed.ticket_id
                      AND later.action IN ('review', 'fix') AND later.state != 'cancelled'
                      AND later.rowid > keyed.rowid
                    ORDER BY later.rowid DESC LIMIT 1
                ) AS later_id
                FROM jobs AS keyed
                WHERE keyed.dedupe_key = ? OR substr(keyed.dedupe_key, 1, ?) = ?
                ORDER BY keyed.rowid DESC LIMIT 1
                """,
                (key, len(repeat), repeat),
            ).fetchone()
        if latest is None:
            return key
        if latest["later_id"] is None:
            return str(latest["dedupe_key"])
        return f"{repeat}{latest['later_id']}"

    def _ci_failure_carried(self, ticket: TrackedTicket, head: str) -> bool | object:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, action, state, source_comment_id, detail, created_at,
                       handoff_comment_id, state IN ('queued', 'running')
                       OR (state = 'failed' AND failure_handled = 0) AS open
                FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND head = ?
                  AND action IN ('review', 'fix') AND state != 'cancelled'
                ORDER BY open DESC, rowid DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchone()
        if row is None:
            return False
        if (row["action"], row["state"]) == ("fix", "failed") and row["open"]:
            failed = next(job for job in self.queue.unhandled_failures() if job.id == row["id"])
            handoff = self._posted_handoff(
                failed, self._polled.get((ticket.repo.lower(), ticket.ticket_id))
            )
            return handoff if handoff is _HANDOFF_UNREAD else handoff is None
        if row["open"]:
            return True
        return (row["action"], row["state"]) == ("review", "done") and not self._verdict_handled(
            ticket,
            head,
            _detail_round_number(row["detail"]),
            str(row["source_comment_id"]),
            float(row["created_at"]),
            row["handoff_comment_id"],
        )

    def _verdict_handled(
        self,
        ticket: TrackedTicket,
        head: str,
        review_round: int | None,
        ready_id: str,
        review_queued_at: float,
        handoff_id: str | None,
    ) -> bool:
        self._remember_verdicts(ticket, ticket.comments)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT verdict.comment_id, verdict.outcome, recorded.head
                FROM handled_comments AS verdict
                LEFT JOIN verdict_heads AS recorded USING (repo, ticket_id, comment_id)
                WHERE verdict.repo = ? AND verdict.ticket_id = ?
                  AND verdict.outcome NOT IN ('rejected-edited', 'rejected-unregistered-verdict')
                  AND NOT COALESCE(recorded.posted_at < ?, 0)
                  AND (
                      recorded.review_round = ?
                      OR recorded.head IS NULL AND verdict.comment_id = ? AND verdict.rowid <= (
                          SELECT MAX(handled_rowid) FROM verdict_heads_floor
                      ) AND NOT EXISTS (
                          SELECT 1 FROM handled_times AS handled
                          WHERE handled.repo = verdict.repo
                            AND handled.ticket_id = verdict.ticket_id
                            AND handled.comment_id = verdict.comment_id
                            AND handled.handled_at < ?
                      )
                  )
                  AND verdict.rowid > COALESCE((
                      SELECT ready.rowid FROM handled_comments AS ready
                      WHERE ready.repo = verdict.repo AND ready.ticket_id = verdict.ticket_id
                        AND ready.comment_id = ?
                  ), 0)
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    review_queued_at,
                    review_round,
                    handoff_id,
                    review_queued_at,
                    ready_id,
                ),
            ).fetchall()
        visible = {comment.id for comment in ticket.comments}
        return any(
            _matching_head(str(row["head"]), head) is not None
            if row["head"] is not None
            else row["comment_id"] not in visible and _answering_outcome(str(row["outcome"]))
            for row in rows
        )

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

    def _has_review_job(self, ticket: TrackedTicket, head: str, round_number: int) -> bool:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT head, detail FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        return any(
            _detail_round_number(row["detail"]) == round_number
            and _matching_head(str(row["head"] or ""), head) is not None
            for row in rows
        )

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

    def _rebase_scope(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
        review_round: int,
        retarget: RebaseStep | None = None,
        delta: RetargetDelta | None = None,
    ) -> RebaseScope | None:
        last = (
            retarget.from_head
            if retarget is not None
            else self._reviewed_head(ticket, comment, review_round)
        )
        if last is None or last == head or self.rebase_classifier is None:
            return None
        try:
            scope = self.rebase_classifier(ticket.repo, ticket.factory.base_branch, last, head)
        except Exception as exc:
            self.record_poll_error(
                ticket.repo, ticket.ticket_id, f"rebase-check:{type(exc).__name__}"
            )
            return None
        if retarget is None or scope is None:
            return scope
        return replace(
            scope,
            reviewed_base=retarget.onto_from,
            overlap=(
                None if delta is None else parent_overlap(delta, ticket.factory.exclusive_paths)
            ),
            retarget=True,
        )

    def _retarget_delta_read(
        self, ticket: TrackedTicket, retarget: RebaseStep, head: str
    ) -> RetargetDelta | None:
        if retarget.parent_head is None or self.retarget_delta is None:
            return None
        try:
            return self.retarget_delta(
                ticket.repo,
                ticket.factory.base_branch,
                retarget.onto_from,
                retarget.from_head,
                retarget.parent_head,
                head,
            )
        except Exception as exc:
            self.record_poll_error(
                ticket.repo, ticket.ticket_id, f"retarget-delta:{type(exc).__name__}"
            )
            return None

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


def _at_base(ticket: TrackedTicket, head: str) -> bool:
    return ticket.pr_commits == 0 and _matching_head(head, ticket.current_head) is not None


def _safe_refusal(exc: Exception) -> tuple[str, str]:
    status = getattr(exc, "http_status", None)
    message = getattr(exc, "api_message", None) or getattr(exc, "safe_detail", None)
    http = f"HTTP {status}" if status is not None else ""
    detail = " ".join(part for part in (type(exc).__name__, http, message or "") if part)
    return detail, (f" ({http})" if http else "") + f": {message or type(exc).__name__}"
