
from __future__ import annotations

from lib.dispatch.helpers import _comment_link
from lib.dispatch.types import Comment, TrackedTicket


class CappedWakeMixin:
    def _wake_capped(self, ticket: TrackedTicket, comment: Comment) -> None:
        self.wake_grok_once(
            ticket,
            reason="capped",
            source_comment_id=comment.id,
            detail=self._project_wake_detail(
                ticket,
                f"review round cap reached, {_comment_link(comment)}",
            ),
            head=ticket.current_head,
        )
