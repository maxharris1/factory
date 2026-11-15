from __future__ import annotations

from lib.dispatch.helpers import _is_patch_id, _matching_head
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
            approved_patch = actions.stable_patch_id(ticket, approved_head)
            current_patch = actions.stable_patch_id(ticket, head)
        except Exception:
            return False
        return (
            _is_patch_id(approved_patch)
            and _is_patch_id(current_patch)
            and approved_patch.lower() == current_patch.lower()
        )
