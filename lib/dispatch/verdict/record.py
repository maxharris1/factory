from __future__ import annotations

import sqlite3

from lib.dispatch.constants import _NOT_FINISHED
from lib.dispatch.types import Comment, TrackedTicket


class ApprovalRecordMixin:
    def _record_approval(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        head: str,
        comment: Comment,
    ) -> None:
        """The covering APPROVE the merge queue orders and gates on (#130)."""
        connection.execute(
            f"""
            UPDATE ticket_state
            SET approved_head = ?, approved_at = ?, approve_comment_id = ?,
                update_failures = 0, update_from_head = NULL
            WHERE repo = ? AND ticket_id = ? AND {_NOT_FINISHED}
            """,
            (
                head,
                comment.created_at,
                comment.id,
                ticket.repo.lower(),
                ticket.ticket_id,
            ),
        )
