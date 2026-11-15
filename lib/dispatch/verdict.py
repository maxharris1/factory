"""T3/T4 verdicts: approvals and whether an approval covers a head."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from lib.dispatch.constants import MERGED_BEFORE_HANDOFF, _NOT_FINISHED
from lib.dispatch.helpers import (
    _comment_link,
    _comment_order,
    _holds,
    _is_full_sha,
    _is_patch_id,
    _matching_head,
    _round_number,
    _scope,
)
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import AlreadyMergedShip, Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.queue import JobRequest


class VerdictMixin:
    def _shipped_approval(self, ticket: TrackedTicket) -> tuple[str, int | None] | None:
        """(head, round) of the APPROVE whose ship intent completed last."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT comment_id, head FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND status = 'completed'
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        approve = next(
            (item for item in ticket.comments if item.id == row["comment_id"]), None
        )
        approved_round = None
        if approve is not None:
            try:
                fields = parse_comment(approve.body).as_dict()
            except HandoffError:
                fields = {}
            approved_round = _round_number(fields.get("round"))
        return str(row["head"]), approved_round

    def _t3(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-revise-author"
        reported_round = _round_number(fields.get("round"))
        if self._over_cap(ticket, reported_round):
            return True, "rejected-over-cap"
        verdict_head = fields.get("head")
        if _matching_head(verdict_head or "", ticket.current_head) is None:
            return True, "stale-revise"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                outcome = str(prior["outcome"])
                replay_state = (
                    "capped" if outcome == "capped-review" else "fixing"
                )
                if replay_state == "capped":
                    self._wake_capped(ticket, comment)
                self._write_state_label(ticket, replay_state)
                return True, outcome
            connection.execute(
                """
                INSERT OR IGNORE INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation
                ) VALUES (?, ?, 0, 0, 1)
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
            state = connection.execute(
                """
                SELECT workflow_state, review_round, generation
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            assert state is not None
            round_number = int(state["review_round"]) + 1
            cap = self._effective_cap(ticket, connection)
            if (
                state["workflow_state"] not in ("review", "awaiting-ok")
                or reported_round != round_number
            ):
                connection.commit()
                self._wake_comment_problem(
                    ticket, comment, "stale", "stale-revise"
                )
                return True, "stale-revise"
            next_state = "capped" if round_number >= cap else "fixing"
            answered = None
            if next_state == "fixing":
                request = JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="fix",
                    recipient=ticket.factory.builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(state["generation"]),
                    head=ticket.current_head,
                    detail=self._job_detail(
                        ticket,
                        action="fix",
                        head=ticket.current_head,
                        round_number=round_number + 1,
                        cap=cap,
                        instruction=_comment_link(comment),
                    ),
                )
                _, answered = self._enqueue_step(
                    connection,
                    ticket,
                    comment,
                    request,
                    dedupe_key=(
                        f"t3:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                    ),
                )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                next_state,
                source="t3",
                at=self.queue.now(),
                also={"review_round": round_number},
            )
            outcome = (
                "capped-review"
                if next_state == "capped"
                else "answered-fix"
                if answered is not None
                else "queued-review-fix"
            )
            connection.execute(
                """
                INSERT INTO review_events (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment.id,
                    outcome,
                ),
            )
            self._record_followups(connection, ticket, comment, round_number)
            connection.commit()
        if next_state == "capped":
            self._wake_capped(ticket, comment)
        self._write_state_label(ticket, next_state)
        return True, outcome

    def _t4(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-approve-author"
        current_head = ticket.current_head
        if current_head is None:
            return False, "pr-head-unavailable"
        ship = ticket.factory.ship
        intent = self._ship_intent(ticket, comment.id)
        if intent is not None:
            return self._replay_ship_intent(
                ticket,
                comment,
                current_head,
                ship,
                intent,
            )
        approved_head = fields["head"]
        state, review_round = self._review_context(ticket)
        reported_round = _round_number(fields.get("round"))
        if self._over_cap(ticket, reported_round):
            return True, "rejected-over-cap"
        if _holds(ticket) and self._superseded_by_revise(
            ticket, comment, reported_round
        ):
            return True, "superseded-approve"
        review_head = self._latest_review_head(ticket)
        if state not in ("review", "awaiting-ok") or reported_round != review_round + 1:
            self._wake_comment_problem(
                ticket, comment, "stale", "stale-approve"
            )
            return True, "stale-approve"
        if review_head is None or _matching_head(approved_head, review_head) is None:
            return True, "stale-approve"
        if not _is_full_sha(current_head):
            self._wake_ship_failed(
                ticket,
                comment,
                f"PR head {current_head!r} is not a lowercase 40-character SHA",
            )
            return True, "refused-ship-head"
        result = ticket.ci.get(current_head)
        if result is None or result.status == "pending":
            return False, "approve-ci-not-green"
        if result.status == "failure":
            if state == "awaiting-ok" or self._hold_in_play(ticket, current_head):
                return False, "approve-ci-red"
            self.wake_grok_once(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                reason="blocked",
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                detail=self._project_wake_detail(
                    ticket,
                    f"APPROVE {_comment_link(comment)} landed on red CI at "
                    f"{current_head}",
                ),
                head=current_head,
            )
            return True, "approve-ci-red"
        covered = _matching_head(approved_head, current_head) is not None
        if not covered:
            try:
                actions = self._review_actions(ticket)
                approved_patch = actions.stable_patch_id(
                    ticket,
                    approved_head,
                )
                current_patch = actions.stable_patch_id(
                    ticket,
                    current_head,
                )
            except Exception:
                return self._queue_patch_review(ticket, comment, current_head)
            covered = (
                _is_patch_id(approved_patch)
                and _is_patch_id(current_patch)
                and approved_patch.lower() == current_patch.lower()
            )
        if not covered:
            return self._queue_patch_review(ticket, comment, current_head)
        if ship == "merge" and ticket.factory.hold is not None:
            held = self._hold_gate(ticket, comment, current_head)
            if held is not None:
                return held
        with self._connect() as connection:
            self._record_followups(connection, ticket, comment, reported_round)
            connection.commit()
        intent_status, replay = self._prepare_ship_intent(
            ticket,
            comment.id,
            current_head,
            ship,
        )
        if replay:
            intent = self._ship_intent(ticket, comment.id)
            assert intent is not None
            return self._replay_ship_intent(
                ticket,
                comment,
                current_head,
                ship,
                intent,
            )
        assert intent_status == "pending"
        watch = ticket.factory.finish_line == "merge"
        if ship == "merge":
            actions = self._review_actions(ticket)
            if actions.is_merged(ticket, current_head):
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                self._wake_done(ticket, comment, "merged")
                return True, "merge-confirmed"
            if watch:
                return self._enter_merge_queue(ticket, comment, current_head)
            self._ship_head(ticket, current_head)
            self._complete_ship_intent(ticket, comment.id, "merged")
            self._write_state_label(ticket, "merged")
            self._wake_done(ticket, comment, "merged")
            return True, "merged"
        if ship == "handoff":
            try:
                note = self._review_actions(ticket).handoff(ticket, current_head)
            except AlreadyMergedShip:
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                self._wake_done(ticket, comment, "merged", note=MERGED_BEFORE_HANDOFF)
                return True, "merge-confirmed"
            if watch:
                return self._enter_merge_queue(ticket, comment, current_head)
            self._complete_ship_intent(ticket, comment.id, "handed-off")
            self._write_state_label(ticket, "handed-off")
            self._wake_done(ticket, comment, "handed-off", note=note)
            return True, "handed-off"
        approver = ship.removeprefix("after:")
        self._review_actions(ticket).await_approver(ticket, current_head, approver)
        self._complete_ship_intent(
            ticket,
            comment.id,
            "waiting-external",
            approval=(current_head, comment) if watch else None,
        )
        self._write_state_label(ticket, "waiting-external")
        return True, "waiting-external"

    def _record_approval(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        head: str,
        comment: Comment,
    ) -> None:
        """The covering APPROVE the merge queue orders and gates on (#130)."""
        connection.execute(
            f"""
            UPDATE ticket_state
            SET approved_head = ?, approved_at = ?, approve_comment_id = ?,
                update_failures = 0, update_from_head = NULL
            WHERE repo = ? AND ticket_id = ? AND {_NOT_FINISHED}
            """,
            (
                head,
                comment.created_at,
                comment.id,
                ticket.repo.lower(),
                ticket.ticket_id,
            ),
        )

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
