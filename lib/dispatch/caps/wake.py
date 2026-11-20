
from __future__ import annotations

from lib.dispatch.helpers import _comment_link
from lib.dispatch.types import Comment, TrackedTicket

_CAPPED_DETAIL = {"capped": "review round cap reached", "stalled": "review not converging"}


class CappedWakeMixin:
    def _wake_capped(self, ticket: TrackedTicket, comment: Comment) -> None:
        stop = self._capped_stop(ticket, comment)
        self.wake_grok_once(
            ticket,
            reason="capped",
            source_comment_id=comment.id,
            detail=self._project_wake_detail(
                ticket,
                f"{_CAPPED_DETAIL[stop]}, {_comment_link(comment)}",
                question=self._stop_question(ticket, comment, stop, self._stop_disputed(ticket, comment, stop)),
            ),
            head=ticket.current_head,
        )
