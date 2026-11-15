from __future__ import annotations

from lib.dispatch.helpers import _comment_order
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment


class LatestVerdictMixin:
    def _latest_review_verdict(
        self,
        ticket: TrackedTicket,
    ) -> tuple[Comment, dict[str, str]] | None:
        reviewer = f"{ticket.factory.reviewer_harness}-bot"
        latest: tuple[Comment, dict[str, str]] | None = None
        for item in sorted(ticket.comments, key=_comment_order):
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] != "verdict":
                continue
            if not self._trusted(
                ticket, item, fields, reviewer, role="reviewer"
            ):
                continue
            latest = (item, fields)
        return latest
