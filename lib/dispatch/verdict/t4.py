from __future__ import annotations

from collections.abc import Mapping

from lib.approve_rule import ApproveRefusal, approve_refusal
from lib.dispatch.constants import MERGED_BEFORE_HANDOFF
from lib.dispatch.helpers import (
    _comment_link,
    _holds,
    _is_full_sha,
    _matching_head,
    _round_number,
    _scope,
)
from lib.dispatch.types import AlreadyMergedShip, CIResult, Comment, Registration, TrackedTicket
from lib.handoff import parse_comment


class T4Mixin:
    def _t4(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-approve-author"
        registration = self._registration(ticket, comment, "verdict")
        if registration is Registration.UNREGISTERED:
            return True, "rejected-unregistered-verdict"
        if registration is Registration.PENDING:
            return False, "verdict-awaiting-registration"
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
        replayed = self._replay_held_base_update(ticket, comment)
        if replayed is not None:
            return replayed
        refusal = self._approve_refusal(comment, fields)
        if refusal is not None:
            return True, refusal.outcome
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
        held_fix = state == "fixing" and self._held_ci_fix_out(ticket, comment.id)
        if (
            state not in ("review", "awaiting-ok") and not held_fix
        ) or reported_round != review_round + 1:
            self._wake_comment_problem(
                ticket, comment, "stale", "stale-approve"
            )
            return True, "stale-approve"
        if review_head is None or _matching_head(approved_head, review_head) is None:
            return True, "stale-approve"
        if state == "awaiting-ok" and ticket.mergeable_state in ("dirty", "behind"):
            return self._hold_base_update(ticket, comment, current_head)
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
            if state in ("awaiting-ok", "fixing") or self._hold_in_play(
                ticket, current_head
            ):
                return self._held_ci_red(
                    ticket, comment, current_head, result, state, review_round
                )
            if ticket.factory.start_before_ci:
                return self._approve_ci_red_fix(
                    ticket, comment, current_head, result, review_round
                )
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
        if ticket.factory.start_before_ci:
            self._record_ci_result(ticket, current_head, "success")
        covered = self._approval_covers(ticket, approved_head, current_head)
        if not covered:
            return self._queue_patch_review(ticket, comment, current_head)
        if _holds(ticket):
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

    def _approve_refusal(
        self, comment: Comment, fields: Mapping[str, str]
    ) -> ApproveRefusal | None:
        with self._connect() as connection:
            since = connection.execute("SELECT since FROM approve_rule_floor").fetchone()[0]
        if comment.created_at < since:
            return None
        return approve_refusal(fields, parse_comment(comment.body).body)

    def _held_ci_red(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
        result: CIResult,
        state: str,
        review_round: int,
    ) -> tuple[bool, str]:
        if not ticket.factory.start_before_ci:
            return False, "approve-ci-red"
        key = _held_ci_fix_key(ticket, comment.id, head)
        with self._connect() as connection:
            queued = connection.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ?", (key,)
            ).fetchone()
        if queued is None:
            _status, count = self._record_ci_result(ticket, head, "failure")
            if self._mark_stuck_on_third_red(ticket, comment, head, count):
                return True, "stuck-ci-red-3"
            self._queue_ci_fix(
                ticket,
                comment,
                head,
                result,
                review_round,
                self._generation(ticket.repo, ticket.ticket_id),
                dedupe_key=key,
            )
        if state == "review":
            self._set_workflow_state(ticket, "fixing")
        return False, "approve-ci-red-fix-held"

    def _held_ci_fix_out(self, ticket: TrackedTicket, approve_id: str) -> bool:
        prefix = _held_ci_fix_key(ticket, approve_id, "")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE substr(dedupe_key, 1, ?) = ?",
                (len(prefix), prefix),
            ).fetchone()
        return row is not None

    def _held_approve_covers(self, ticket: TrackedTicket, head: str) -> bool:
        for comment in ticket.comments:
            if self._is_handled(ticket, comment.id):
                continue
            if not self._held_ci_fix_out(ticket, comment.id):
                continue
            approved = parse_comment(comment.body).as_dict().get("head")
            if approved and self._approval_covers(ticket, approved, head):
                return True
        return False

    def _approve_ci_red_fix(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
        result: CIResult,
        review_round: int,
    ) -> tuple[bool, str]:
        _status, count = self._record_ci_result(ticket, head, "failure")
        if self._mark_stuck_on_third_red(ticket, comment, head, count):
            return True, "stuck-ci-red-3"
        self._queue_ci_fix(
            ticket,
            comment,
            head,
            result,
            review_round,
            self._generation(ticket.repo, ticket.ticket_id),
            dedupe_key=f"t4:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}",
        )
        self._set_workflow_state(ticket, "fixing")
        return True, "approve-ci-red-fix"


def _held_ci_fix_key(ticket: TrackedTicket, approve_id: str, head: str) -> str:
    return f"t4-held:{_scope(ticket.repo, ticket.ticket_id)}:{approve_id}:{head}"
