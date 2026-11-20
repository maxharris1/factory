from __future__ import annotations

from lib.dispatch.helpers import _round_number
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment


def _approved_round(shipped: tuple[str, int | None] | None, review_round: int) -> int:
    """The round a handed-off ticket has completed (#108): its shipped
    APPROVE's round, never below review_round, since rounds a reopen ran
    after that APPROVE count too (#1197); one past review_round when that
    APPROVE names none."""
    if shipped is not None and shipped[1] is not None:
        return max(shipped[1], review_round)
    return review_round + 1


class ShippedApprovalMixin:
    def _shipped_approval(self, ticket: TrackedTicket) -> tuple[str, int | None] | None:
        """(head, round) of the APPROVE whose ship intent completed last."""
        shipped = self._shipped_approve(ticket)
        if shipped is None:
            return None
        head, approve = shipped
        approved_round = None
        if approve is not None:
            try:
                fields = parse_comment(approve.body).as_dict()
            except HandoffError:
                fields = {}
            approved_round = _round_number(fields.get("round"))
        return head, approved_round

    def _shipped_approval_at(self, ticket: TrackedTicket) -> float | None:
        shipped = self._shipped_approve(ticket)
        return None if shipped is None or shipped[1] is None else shipped[1].created_at

    def _shipped_approve(self, ticket: TrackedTicket) -> tuple[str, Comment | None] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT comment_id, head FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND status = 'completed'
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        approve = next(
            (item for item in ticket.comments if item.id == row["comment_id"]), None
        )
        return str(row["head"]), approve
