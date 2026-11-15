from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lib.dispatch.helpers import _comment_link, _matching_head, _round_number, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.queue import JobRequest


@dataclass(frozen=True)
class ReviseCIGate:
    outcome: tuple[bool, str] | None = None
    ci_failure: str = ""


class T3Mixin:
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
        instruction = _comment_link(comment)
        if ticket.factory.start_before_ci:
            gate = self._revise_ci_gate(ticket, comment, reported_round)
            if gate.outcome is not None:
                return gate.outcome
            instruction += gate.ci_failure
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
                SELECT workflow_state, review_round, generation, cap_extra, stuck
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
            granted = (
                self._auto_granted_cap_extra(ticket, fields, state)
                if round_number >= cap
                else None
            )
            if granted is not None:
                cap += 1
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
                        instruction=instruction,
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
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                next_state,
                source="t3",
                at=self.queue.now(),
                also={
                    "review_round": round_number,
                    "cap_extra": (
                        int(state["cap_extra"]) if granted is None else granted
                    ),
                },
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
            if granted is not None:
                self._record_auto_extra(connection, ticket, comment, granted)
            self._record_followups(connection, ticket, comment, round_number)
            connection.commit()
        if next_state == "capped":
            self._wake_capped(ticket, comment)
        self._write_state_label(ticket, next_state)
        return True, outcome

    def _revise_ci_gate(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
    ) -> ReviseCIGate:
        state, completed = self._review_context(ticket)
        if state not in ("review", "awaiting-ok") or reported_round != completed + 1:
            return ReviseCIGate()
        head = ticket.current_head
        assert head is not None
        result = ticket.ci.get(head)
        if result is None or result.status == "pending":
            return ReviseCIGate(outcome=(False, "revise-ci-pending"))
        _status, count = self._record_ci_result(ticket, head, result.status)
        if result.status == "success":
            return ReviseCIGate()
        if self._mark_stuck_on_third_red(ticket, comment, head, count):
            return ReviseCIGate(outcome=(True, "stuck-ci-red-3"))
        return ReviseCIGate(
            ci_failure=f"\n\nCI failed on {head}:\n{result.log or ''}".rstrip()
        )
