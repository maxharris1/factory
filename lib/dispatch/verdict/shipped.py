from __future__ import annotations

from lib.dispatch.helpers import _round_number
from lib.dispatch.types import TrackedTicket
from lib.handoff import HandoffError, parse_comment


def _approved_round(shipped: tuple[str, int | None] | None, review_round: int) -> int:
    """The round a handed-off ticket's shipped APPROVE completed (#108); one
    past review_round when that APPROVE names none."""
    if shipped is not None and shipped[1] is not None:
        return shipped[1]
    return review_round + 1


class ShippedApprovalMixin:
    def _shipped_approval(self, ticket: TrackedTicket) -> tuple[str, int | None] | None:
        """(head, round) of the APPROVE whose ship intent completed last."""
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
        approved_round = None
        if approve is not None:
            try:
                fields = parse_comment(approve.body).as_dict()
            except HandoffError:
                fields = {}
            approved_round = _round_number(fields.get("round"))
        return str(row["head"]), approved_round
