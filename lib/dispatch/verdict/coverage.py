from __future__ import annotations

from lib.dispatch.helpers import _is_full_sha, _is_patch_id, _matching_head
from lib.dispatch.types import TrackedTicket


class ApprovalCoverageMixin:
    def _approval_covers(
        self,
        ticket: TrackedTicket,
        approved_head: str,
        head: str,
    ) -> bool:
        """Whether an APPROVE naming approved_head covers head, by T4's rule:
        the same commit (a 7+ character prefix either way) or the same
        stable patch id. A patch id that cannot be read does not cover."""
        if _matching_head(approved_head, head) is not None:
            return True
        try:
            actions = self._review_actions(ticket)
            approved_patch = actions.stable_patch_id(
                ticket, self._registered_review_head(ticket, approved_head)
            )
            current_patch = actions.stable_patch_id(ticket, head)
        except Exception:
            return False
        return (
            _is_patch_id(approved_patch)
            and _is_patch_id(current_patch)
            and approved_patch.lower() == current_patch.lower()
        )

    def _registered_review_head(self, ticket: TrackedTicket, approved_head: str) -> str:
        if _is_full_sha(approved_head):
            return approved_head
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT head FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                  AND head IS NOT NULL
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        named = {
            str(row["head"])
            for row in rows
            if _is_full_sha(str(row["head"])) and str(row["head"]).startswith(approved_head)
        }
        if len(named) != 1:
            raise ValueError(f"APPROVE head {approved_head} names {len(named)} review heads")
        return named.pop()
