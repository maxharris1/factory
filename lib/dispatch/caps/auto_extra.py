"""Auto extra rounds: the poller grants extra rounds at the cap itself
(issues #263, #1110). How many, and the comment each grant posts, come
from the StopPolicy in lib/dispatch/verdict/convergence.py: cap_grant
and grant_reason."""

from __future__ import annotations

import sqlite3

from lib.dispatch.types import Comment, TrackedTicket
from lib.dispatch.verdict.convergence import Continue, Stalled, cap_grant, grant_reason, stop_policy


def auto_extra_marker(comment_id: str) -> str:
    return f"<!-- orch-auto-extra verdict={comment_id} -->"


class AutoExtraMixin:
    def _auto_granted_cap_extra(
        self,
        ticket: TrackedTicket,
        state: sqlite3.Row,
        convergence: Continue | Stalled,
    ) -> int | None:
        return cap_grant(stop_policy(ticket.factory), convergence, int(state["cap_extra"]), bool(state["stuck"]))

    def _record_auto_extra(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        granted: int,
        convergence: Continue | Stalled,
    ) -> None:
        reason = grant_reason(stop_policy(ticket.factory), granted, convergence)
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
