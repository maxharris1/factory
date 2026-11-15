"""Hold gate and approver checks before ship."""

from __future__ import annotations

import json
from collections.abc import Mapping

from lib.dispatch.helpers import _is_full_sha, ship_mode
from lib.dispatch.types import Comment, ShipError, TrackedTicket
from lib.hold import HoldDecision, decide


class HoldsMixin:
    def _hold_gate(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
    ) -> tuple[bool, str] | None:
        """Issue #86: None lets T4 merge; otherwise the APPROVE stays
        unhandled and T4 re-checks it on every poll, at whatever the head
        then is. A held PR moves to awaiting-ok once per head and wakes
        Grok Bot once per head with the PR and head."""
        hold = ticket.factory.hold
        assert hold is not None
        decision = self._hold_decision(ticket, head)
        if decision.unread is not None:
            return False, "hold-unread"
        if not decision.blocks:
            return None
        scope = (ticket.repo.lower(), ticket.ticket_id)
        reasons = ",".join(decision.reasons)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head, approver, reasons FROM ship_holds
                WHERE repo = ? AND ticket_id = ?
                """,
                scope,
            ).fetchone()
        state, _round = self._review_context(ticket)
        stored = None if row is None else (row["head"], row["approver"], row["reasons"])
        if state != "awaiting-ok" or stored != (head, hold.approver, reasons):
            if state != "awaiting-ok":
                self._write_state_label(ticket, "awaiting-ok")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO ship_holds (
                        repo, ticket_id, head, approver, reasons,
                        approve_comment_id, held_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo, ticket_id) DO UPDATE SET
                        head = excluded.head,
                        approver = excluded.approver,
                        reasons = excluded.reasons,
                        approve_comment_id = excluded.approve_comment_id,
                        held_at = CASE WHEN ship_holds.head = excluded.head
                            THEN ship_holds.held_at ELSE excluded.held_at END
                    """,
                    (
                        *scope,
                        head,
                        hold.approver,
                        reasons,
                        comment.id,
                        self.queue.now(),
                    ),
                )
                connection.execute(
                    """
                    UPDATE ticket_state SET workflow_state = 'awaiting-ok'
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    scope,
                )
                connection.commit()
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="awaiting-ok",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            head=head,
            detail=json.dumps(
                {
                    "project_bot": ticket.grok_bot,
                    "outcome": "awaiting-ok",
                    "pr": ticket.ticket_url or ticket.pr_number,
                    "pr_number": ticket.pr_number,
                    "post_to": ticket.ticket_url,
                    "head": head,
                    "approver": hold.approver,
                    "reasons": list(decision.reasons),
                    "held_paths": list(decision.paths),
                    "label": hold.label,
                    "clears_with": (
                        f"<!-- orch:v1 kind=ok head={head} --> from "
                        f"{hold.approver}"
                        + (
                            f", or {hold.approver} removing {hold.label}"
                            if decision.reasons == ("label",)
                            else ""
                        )
                    ),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return False, "awaiting-ok"

    def _hold_decision(self, ticket: TrackedTicket, head: str) -> HoldDecision:
        hold = ticket.factory.hold
        assert hold is not None
        return decide(
            hold,
            labels=ticket.labels,
            changed_files=ticket.changed_files,
            label_events=ticket.label_events,
            comments=ticket.comments,
            approver_allowed=self._may_approve(hold.approver),
            covers=lambda ok_head: self._approval_covers(ticket, ok_head, head),
        )

    def _held(self, ticket: TrackedTicket, head: str) -> HoldDecision | None:
        if ticket.factory.ship != "merge" or ticket.factory.hold is None:
            return None
        decision = self._hold_decision(ticket, head)
        return decision if decision.blocks else None

    def _hold_in_play(self, ticket: TrackedTicket, head: str) -> bool:
        if ticket.factory.ship != "merge" or ticket.factory.hold is None:
            return False
        decision = self._hold_decision(ticket, head)
        return decision.unread is not None or bool(decision.reasons)

    def _hold_ok_outcome(self, ticket: TrackedTicket, comment: Comment) -> str:
        hold = ticket.factory.hold
        assert hold is not None
        if (
            comment.author.casefold() == hold.approver.casefold()
            and self._may_approve(hold.approver)
        ):
            return "hold-ok"
        return "rejected-hold-ok-author"

    def _may_approve(self, login: str) -> bool:
        """A Max-only approval (#122): a trusted_logins key while the
        GitHub App switch is off, else a login in approver_logins."""
        if self.approvals.app_trust:
            return self.approvals.may_approve(login)
        return self._trusted_login(login)

    def _approver_go(self, comment: Comment, fields: Mapping[str, str]) -> bool:
        """With the switch on, an approver's go may lift a cap even when the
        approver is not trusted for grok-bot; it starts nothing else (#122)."""
        return (
            self.approvals.app_trust
            and fields.get("from") == "grok-bot"
            and fields.get("role") == "orchestrator"
            and self.approvals.may_approve(comment.author)
        )

    def _may_approve_go(self, author: str) -> bool:
        """A Linear author keeps today's rule: the app changes GitHub
        identity only."""
        if author.startswith("linear:") or not self.approvals.app_trust:
            return True
        return self.approvals.may_approve(author)

    def _trusted_login(self, login: str) -> bool:
        wanted = login.casefold()
        return any(
            key.casefold() == wanted for key in self.trusted_logins.get("github", {})
        )

    def _ship_head(self, ticket: TrackedTicket, head: str) -> None:
        """Ship exactly head in the repo's ship_mode, or raise. No fallback."""
        if not _is_full_sha(head):
            raise ShipError(f"ship head {head!r} is not a 40-character SHA")
        mode = ship_mode(ticket.factory.ship)
        if mode == "fast-forward":
            self._review_actions(ticket).fast_forward(
                ticket,
                branch=ticket.factory.base_branch,
                head=head,
            )
        elif mode == "squash":
            self._review_actions(ticket).merge(
                ticket,
                squash=True,
                match_head_commit=head,
            )
        else:
            raise ShipError(f"unknown ship mode {mode!r}")
        self._merged_repos.add(ticket.repo.lower())
