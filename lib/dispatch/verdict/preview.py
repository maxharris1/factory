from __future__ import annotations

from lib.dispatch.types import TrackedTicket


class PreviewedHeadMixin:
    def _previewed_head(self, ticket: TrackedTicket, approve_id: str) -> str | None:
        """The head an ok of an after:<approver> APPROVE may ship.

        Its completed ship intent's head; on a finish_line: merge ticket, the
        approval the merge queue holds for that APPROVE now (#130): moved to
        the new head by a carried update, cleared by a go or a re-review.
        """
        intent = self._ship_intent(ticket, approve_id)
        if intent is None:
            return None
        head, policy, status = intent
        if policy != ticket.factory.ship or status != "completed":
            return None
        if ticket.factory.finish_line != "merge":
            return head
        row = self._queue_row(ticket)
        if row["approve_comment_id"] != approve_id or row["approved_head"] is None:
            return None
        return str(row["approved_head"])
