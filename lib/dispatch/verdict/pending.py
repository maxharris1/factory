from __future__ import annotations

from lib.dispatch.helpers import _matching_head, _round_number
from lib.dispatch.types import Comment, TrackedTicket


class PendingApprovalMixin:
    def _red_ci_approval_covering(
        self, ticket: TrackedTicket, head: str
    ) -> tuple[Comment, str, int] | None:
        state, review_round = self._review_context(ticket)
        if state not in ("fixing", "awaiting-ok", "ship"):
            return None
        latest = self._latest_review_verdict(ticket)
        if latest is None:
            return None
        approve, fields = latest
        if fields.get("verdict") != "APPROVE":
            return None
        if self._handled_outcome(ticket, approve.id) != "approve-ci-red-fix":
            return None
        if state != "fixing" and not self._shipping_approval(ticket, approve.id):
            return None
        approved_round = _round_number(fields.get("round"))
        if approved_round != review_round + 1 or self._over_cap(ticket, approved_round):
            return None
        approved_head = fields["head"]
        review_head = self._latest_review_head(ticket)
        if review_head is None or _matching_head(approved_head, review_head) is None:
            return None
        if not self._approval_covers(ticket, approved_head, head):
            return None
        return approve, approved_head, approved_round

    def _shipping_approval(self, ticket: TrackedTicket, approve_id: str) -> bool:
        """A covered ready already took this APPROVE to the ship gate: a hold
        parked it at awaiting-ok, or its ship intent is open."""
        if self._ship_intent(ticket, approve_id) is not None:
            return True
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ship_holds
                WHERE repo = ? AND ticket_id = ? AND approve_comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, approve_id),
            ).fetchone()
        return row is not None
