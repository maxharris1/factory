from __future__ import annotations

from collections.abc import Mapping

from lib.dispatch.helpers import _comment_link, _matching_head, _round_number
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, Registration, TrackedTicket


class BlockedMixin:
    def _t_blocked(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        """A reviewer's BLOCKED stops the ticket until an approver's go (#188).

        Checked like REVISE. A valid one stores state blocked, records the
        verdict as the cause in review_events, and wakes Grok once per
        BLOCKED comment with its link. It consumes no round.
        """
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-blocked-author"
        reported_round = _round_number(fields.get("round"))
        reported_head = fields.get("head") or ""
        if not self._blocked_review_recorded(ticket, comment):
            registration = self._registration(ticket, comment, "verdict")
            if registration is Registration.UNREGISTERED:
                return True, "rejected-unregistered-verdict"
            if registration is Registration.PENDING:
                return False, "verdict-awaiting-registration"
            if self._over_cap(ticket, reported_round):
                return True, "rejected-over-cap"
            if _matching_head(reported_head, ticket.current_head) is None:
                return True, "stale-blocked"
            if not self._record_blocked_review(ticket, comment, reported_round):
                return True, "stale-blocked"
        head = _matching_head(reported_head, ticket.current_head) or reported_head
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="blocked",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=self._project_wake_detail(
                ticket,
                f"verdict=BLOCKED {_comment_link(comment)} from "
                f"{ticket.factory.reviewer_harness}-bot on {head} "
                f"round {reported_round}",
            ),
            head=head,
            once_key=comment.id,
        )
        self._write_state_label(ticket, "blocked")
        return True, "blocked-review"

    def _blocked_review_recorded(self, ticket: TrackedTicket, comment: Comment) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND outcome = 'blocked-review'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
        return row is not None

    def _record_blocked_review(
        self, ticket: TrackedTicket, comment: Comment, reported_round: int | None
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is None:
                state = connection.execute(
                    """
                    SELECT workflow_state, review_round FROM ticket_state
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                ).fetchone()
                if (
                    state is None
                    or state["workflow_state"] != "review"
                    or reported_round != int(state["review_round"]) + 1
                ):
                    connection.commit()
                    return False
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    "blocked",
                    source="t-blocked",
                    at=self.queue.now(),
                )
                connection.execute(
                    """
                    INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                    VALUES (?, ?, ?, 'blocked-review')
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, comment.id),
                )
            connection.commit()
        return True

    def _blocked_by_another(self, ticket: TrackedTicket, comment: Comment) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ticket_state AS state
                WHERE state.repo = ? AND state.ticket_id = ?
                  AND state.workflow_state = 'blocked'
                  AND NOT EXISTS (
                    SELECT 1 FROM review_events AS event
                    WHERE event.repo = state.repo AND event.ticket_id = state.ticket_id
                      AND event.comment_id = ?
                  )
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
        return row is not None
