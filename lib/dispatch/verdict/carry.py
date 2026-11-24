"""#1522: carry an approval across a stacked child's retarget rebase."""

from __future__ import annotations

from lib.dispatch.helpers import _matching_head
from lib.dispatch.rebase import LEAVES_MERGE_QUEUE, RebaseStep, newest_rebase, parent_overlap
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import TrackedTicket
from lib.update_check import RetargetDelta


def carries(delta: RetargetDelta | None, shared: tuple[str, ...]) -> bool:
    return (
        delta is not None
        and delta.own_patch_before is not None
        and delta.own_patch_before == delta.own_patch_after
        and not parent_overlap(delta, shared)
    )


class ApprovalCarryMixin:
    def _approval_carries(
        self, ticket: TrackedTicket, approved_head: str, head: str
    ) -> bool:
        """_approval_covers, or a recorded retarget carry chain reaches head
        from a head the APPROVE covers."""
        if self._approval_covers(ticket, approved_head, head):
            return True
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT from_head, to_head FROM approval_carries WHERE repo = ? AND ticket_id = ?",
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        frontier = [head]
        seen = {head}
        while frontier:
            reached = frontier.pop()
            for row in rows:
                from_head = str(row["from_head"])
                if from_head in seen or _matching_head(reached, str(row["to_head"])) is None:
                    continue
                if self._approval_covers(ticket, approved_head, from_head):
                    return True
                seen.add(from_head)
                frontier.append(from_head)
        return False

    def _approval_held(self, ticket: TrackedTicket, rebase: RebaseStep) -> bool:
        if rebase.from_state in LEAVES_MERGE_QUEUE:
            return rebase.approved_head is not None
        if rebase.from_state == "handed-off":
            return True
        if rebase.from_state != "review":
            return False
        latest = self._latest_review_verdict(ticket)
        if latest is None:
            return False
        approve, fields = latest
        return (
            fields.get("verdict") == "APPROVE"
            and _matching_head(fields.get("head", ""), rebase.from_head) is not None
            and not self._is_handled(ticket, approve.id)
        )

    def _carry_retarget_approval(
        self, ticket: TrackedTicket, rebase: RebaseStep, head: str
    ) -> tuple[bool, str]:
        self._write_state_label(ticket, rebase.from_state)
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO approval_carries
                    (repo, ticket_id, from_head, to_head, carried_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, rebase.from_head, head, now),
            )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                rebase.from_state,
                source="retarget-carry",
                at=now,
                also=rebase.approval(),
            )
            connection.commit()
        if self.log is not None:
            self.log(
                f"retarget carry: {ticket.repo}#{ticket.ticket_id} keeps its approval "
                f"from {rebase.from_head[:12]} at {head[:12]} as {rebase.from_state}"
            )
        return True, "retarget-carry"

    def _carry_waits(self, ticket: TrackedTicket, approved_head: str) -> bool:
        with self._connect() as connection:
            rebase = newest_rebase(connection, (ticket.repo.lower(), ticket.ticket_id))
        return (
            rebase is not None
            and rebase.onto_from is not None
            and rebase.from_state == "review"
            and _matching_head(approved_head, rebase.from_head) is not None
        )
