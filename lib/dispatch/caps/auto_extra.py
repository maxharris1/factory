"""Auto extra rounds: the poller grants extra rounds at the cap itself.
Under factory.md review.stop_on: rounds it grants review.auto_extra_rounds
of them (issue #263). Under convergence it grants one per REVISE that
converge() lets continue and none once it stalls (issue #1110)."""

from __future__ import annotations

import sqlite3

from lib.dispatch.types import Comment, TrackedTicket
from lib.dispatch.verdict.convergence import Continue, Stalled


def auto_extra_marker(comment_id: str) -> str:
    return f"<!-- orch-auto-extra verdict={comment_id} -->"


class AutoExtraMixin:
    def _auto_granted_cap_extra(
        self,
        ticket: TrackedTicket,
        state: sqlite3.Row,
        convergence: Continue | Stalled,
    ) -> int | None:
        if state["stuck"]:
            return None
        already = int(state["cap_extra"])
        if ticket.factory.stop_on == "convergence":
            return already + 1 if isinstance(convergence, Continue) else None
        if already >= ticket.factory.auto_extra_rounds:
            return None
        return already + 1

    def _record_auto_extra(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        granted: int,
        convergence: Continue | Stalled,
    ) -> None:
        if ticket.factory.stop_on != "convergence":
            reason = (
                f"Auto extra round {granted} of {ticket.factory.auto_extra_rounds} "
                "under factory.md `review.auto_extra_rounds` (set by Max)."
            )
        elif isinstance(convergence, Continue) and convergence.reduced:
            reason = (
                f"Extra round {granted}: this round cut the open blocking findings to a new low, "
                "so review goes on under factory.md `review.stop_on: convergence`."
            )
        else:
            reason = (
                f"Extra round {granted}: this round did not cut the open blocking findings. "
                "One flat round is allowed under factory.md `review.stop_on: convergence`."
            )
        body = f"{reason}\n\n{auto_extra_marker(comment.id)}"
        connection.execute(
            """
            INSERT OR IGNORE INTO auto_extra_comments (
                repo, ticket_id, comment_id, body, status, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (ticket.repo.lower(), ticket.ticket_id, comment.id, body, self.queue.now()),
        )

    def _drain_auto_extra(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT comment_id, body FROM auto_extra_comments
                WHERE repo = ? AND ticket_id = ? AND status = 'pending'
                ORDER BY created_at, comment_id
                """,
                key,
            ).fetchall()
        if not rows:
            return
        actions = self._review_actions(ticket)
        for row in rows:
            comment_id = str(row["comment_id"])
            actions.comment_once(ticket, str(row["body"]), auto_extra_marker(comment_id))
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE auto_extra_comments SET status = 'posted'
                    WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                    """,
                    (*key, comment_id),
                )
                connection.commit()
