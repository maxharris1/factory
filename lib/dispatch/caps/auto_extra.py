"""Auto extra rounds (issue #263): the poller grants factory.md
review.auto_extra_rounds extra rounds at the cap itself."""

from __future__ import annotations

import sqlite3

from lib.dispatch.types import Comment, TrackedTicket


def auto_extra_marker(comment_id: str) -> str:
    return f"<!-- orch-auto-extra verdict={comment_id} -->"


class AutoExtraMixin:
    def _auto_granted_cap_extra(
        self,
        ticket: TrackedTicket,
        state: sqlite3.Row,
    ) -> int | None:
        if state["stuck"]:
            return None
        already = int(state["cap_extra"])
        if already >= ticket.factory.auto_extra_rounds:
            return None
        return already + 1

    def _record_auto_extra(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        granted: int,
    ) -> None:
        body = (
            f"Auto extra round {granted} of {ticket.factory.auto_extra_rounds} "
            "under factory.md `review.auto_extra_rounds` (set by Max).\n\n"
            f"{auto_extra_marker(comment.id)}"
        )
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
