from __future__ import annotations

from lib.dispatch.helpers import _matching_head, _round_number
from lib.dispatch.types import Comment, TrackedTicket


class PendingApprovalMixin:
    def _red_ci_approval_covering(
        self, ticket: TrackedTicket, head: str
    ) -> tuple[Comment, str, int] | None:
        state, review_round = self._review_context(ticket)
        if state != "fixing":
            return None
        latest = self._latest_review_verdict(ticket)
        if latest is None:
            return None
        approve, fields = latest
        if fields.get("verdict") != "APPROVE":
            return None
        if self._handled_outcome(ticket, approve.id) != "approve-ci-red-fix":
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
