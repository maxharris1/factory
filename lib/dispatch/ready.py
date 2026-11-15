"""T2 ready handling: queue review, CI results, verdict ordering and re-entry after handoff."""

from __future__ import annotations

from collections.abc import Mapping

from lib.dispatch.constants import WATCHED_STATES
from lib.dispatch.helpers import (
    _acted_verdict,
    _comment_link,
    _comment_order,
    _detail_round_number,
    _has_questions,
    _matching_head,
    _round_number,
    _scope,
)
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.queue import JobRequest


class ReadyMixin:
    def _t2(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        builder_harness = (
            ticket.factory.builder_harness
            if ticket.factory_md is not None
            else self._job_builder_harness(ticket)
        )
        if builder_harness is None and ticket.ready_starts_review:
            # The project builds its PRs outside the queue (orch.yml
            # projects[].ready_starts_review): factory.md's builder, trusted
            # as usual below, may start review with no build job.
            builder_harness = ticket.factory.builder_harness
        if builder_harness is None:
            return True, "rejected-ready-without-builder-job"
        expected = f"{builder_harness}-bot"
        if not self._trusted(ticket, comment, fields, expected, role="builder"):
            return True, "rejected-ready-author"
        state, review_round = self._review_context(ticket)
        if state in ("planning", "plan-check"):
            return True, "rejected-ready-in-planning"
        if state == "capped":
            self._flag_ready_after_cap(ticket, comment, fields["head"])
            return True, "capped-ready"
        reported_head = fields["head"]
        reentry: int | None = None
        rereview = False
        if state == "updating":
            settled = self._settle_update(ticket, comment, reported_head)
            if settled is not None:
                return settled
            ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
            rereview = True
        elif state in ("handed-off", "merge-queue"):
            shipped = self._shipped_approval(ticket)
            approved = self._queue_row(ticket)["approved_head"]
            if (shipped is not None and _matching_head(reported_head, shipped[0])) or (
                approved is not None and _matching_head(reported_head, approved)
            ):
                return True, "ready-already-shipped"
            reentry = (
                shipped[1]
                if shipped is not None and shipped[1] is not None
                else review_round + 1
            )
            if reentry + 1 > self._effective_cap(ticket):
                self._flag_ready_after_cap(ticket, comment, reported_head)
                return True, "ready-after-cap"
        head = _matching_head(reported_head, ticket.current_head)
        if head is None:
            try:
                fresh = self._fresh_ticket(ticket)
            except Exception:
                return False, "pr-head-unavailable"
            if fresh is not None and fresh.merged:
                # #108: the next poll's own read records the merge.
                return True, "ready-on-merged-pr"
            if fresh is not None and self._review_context(fresh)[0] == "abandoned":
                return True, "ready-on-closed-pr"
            if fresh is not None:
                head = _matching_head(reported_head, fresh.current_head)
            if head is None:
                return True, "stale-ready-head"
            assert fresh is not None
            ticket = fresh
            self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
        answered = self._verdict_after(
            ticket,
            comment,
            head,
            reentry if reentry is not None else review_round,
            reentry=reentry is not None,
        )
        if answered:
            # #71: that review already ran. A ready posted after the verdict
            # (a fix that changed nothing) is still reviewed, as round N+1.
            if max(answered) > review_round and state in (None, "questions"):
                self._set_workflow_state(ticket, "review")
            return True, "stale-ready-verdict"
        if state == "waiting-max" and self._verdict_behind_question(
            ticket, comment, head, review_round
        ):
            return True, "stale-ready-verdict"
        result = ticket.ci.get(head) or ticket.ci.get(reported_head)
        if result is None or result.status == "pending":
            if (
                (result is None or not result.reported)
                and ticket.mergeable_state == "dirty"
                and state not in WATCHED_STATES
            ):
                return False, self._queue_base_update(ticket, comment, head, review_round)
            return False, "ci-pending"
        if result.status == "success" and self.feeder is not None:
            if self.feeder.hold_stacked_review(ticket):
                return False, "stacked-held"
        if reentry is not None:
            if not self._reenter_after_handoff(ticket, reentry):
                return True, "stale-ready"
            review_round = reentry
        timeout_seconds = ticket.job_timeout_minutes * 60
        recorded_status, count = self._record_ci_result(ticket, head, result.status)
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if recorded_status == "success":
            queued, answered = self._queue_review(
                ticket, comment, head=head, generation=generation
            )
            if not queued:
                return True, "capped-ready"
            if rereview:
                self._leave_merge_queue(ticket, "update-rereview")
            self._set_workflow_state(ticket, "review")
            return True, "answered-review" if answered else "queued-review"
        if count >= 3:
            self._mark_stuck_and_wake(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                source_comment_id=comment.id,
                timeout_seconds=timeout_seconds,
                reason="ci-red-3",
                detail=self._project_wake_detail(
                    ticket,
                    f"CI failed three times in a row; latest head {head}",
                ),
            )
            return True, "stuck-ci-red-3"
        self._enqueue_or_flag(
            ticket,
            comment,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="fix",
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=timeout_seconds,
                generation=generation,
                head=head,
                detail=self._job_detail(
                    ticket,
                    action="fix",
                    head=head,
                    round_number=review_round + 1,
                    instruction=result.log or f"CI failed on {head}",
                ),
            ),
            dedupe_key=(
                f"t2:{_scope(ticket.repo, ticket.ticket_id)}:{head}:"
                f"g{generation}"
            ),
        )
        if rereview:
            self._leave_merge_queue(ticket, "update-ci-red")
        if reentry is not None or rereview:
            self._set_workflow_state(ticket, "fixing")
        return True, "queued-ci-fix"

    def _reenter_after_handoff(self, ticket: TrackedTicket, approved_round: int) -> bool:
        """#108: a trusted ready after handoff re-enters review. The approved
        round counts as completed, so the next review is approved_round + 1.
        A merge-queue ticket (#130) leaves the queue the same way. False when
        the ticket left handed-off or merge-queue meanwhile."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE ticket_state
                SET review_round = ?, approved_head = NULL, approved_at = NULL,
                    approve_comment_id = NULL, update_failures = 0,
                    update_from_head = NULL
                WHERE repo = ? AND ticket_id = ?
                  AND workflow_state IN ('handed-off', 'merge-queue')
                """,
                (approved_round, ticket.repo.lower(), ticket.ticket_id),
            ).rowcount
            connection.commit()
        if changed == 1 and self.log is not None:
            self.log(
                f"re-entry: {ticket.repo}#{ticket.ticket_id} after handoff, "
                f"next review round {approved_round + 1}"
            )
        return changed == 1

    def _verdict_after(
        self,
        ticket: TrackedTicket,
        ready: Comment,
        head: str,
        review_round: int,
        *,
        reentry: bool = False,
    ) -> set[int]:
        """The rounds of trusted reviewer verdicts for head, posted after the
        ready, that are the first for their round (#71).

        A verdict whose round already had one at or before the ready (a late
        duplicate) answered an earlier ready. One with no round, a round
        above the cap, or a round past the one this ready asks for answers
        none: T3 and T4 act on no such verdict. Nor does one for a completed
        round that T3 did not act on: another verdict closed that round.
        A verdict for the next round answers it only if T3 or T4 could act
        on it once T2 moves the ticket to review: not BLOCKED, not while the
        ticket is in a state other than none, questions, review or
        awaiting-ok, not an APPROVE for a head other than the last reviewed
        one, not one they already handled without acting, and not one behind
        a question pause from the ready on.

        With #108, review_round is the last completed round (the approved
        round for a ready that re-enters review after handoff, which T2 moves
        to review), so this ready would start review_round + 1. A verdict for
        that round answers this ready only once that round's review job for
        head is recorded; on a thread read whole with none (an empty DB) it
        is the verdict of the review this ready starts, which _enqueue_step
        records as answered (answered-review, no claimable job) so that
        verdict can then act.
        """
        reviewer = f"{ticket.factory.reviewer_harness}-bot"
        next_round = review_round + 1
        last_round = min(
            self._effective_cap(ticket),
            next_round
            if self._has_review_job(ticket, head, next_round)
            else review_round,
        )
        actable = self._review_context(ticket)[0] in (
            None, "questions", "review", "awaiting-ok"
        ) or reentry
        review_head = self._latest_review_head(ticket)
        approvable = (
            review_head is not None and _matching_head(head, review_head) is not None
        )
        with self._connect() as connection:
            handled = {
                str(row["comment_id"]): str(row["outcome"])
                for row in connection.execute(
                    """
                    SELECT comment_id, outcome FROM handled_comments
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            }
            round_closers = {
                str(row["comment_id"])
                for row in connection.execute(
                    """
                    SELECT comment_id FROM review_events
                    WHERE repo = ? AND ticket_id = ?
                    AND outcome IN ('queued-review-fix', 'capped-review')
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            }
        before: set[int] = set()
        after: set[int] = set()
        for item in ticket.comments:
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            round_number = _round_number(fields.get("round"))
            if (
                round_number is not None
                and round_number <= last_round
                and (
                    item.id in round_closers
                    if round_number <= review_round
                    else actable
                    and (item.id not in handled or _acted_verdict(handled[item.id]))
                    and (approvable or fields.get("verdict") != "APPROVE")
                    and not self._paused_between(ticket, ready, item)
                )
                and fields.get("kind") == "verdict"
                and fields.get("verdict") in ("REVISE", "APPROVE")
                and self._trusted(ticket, item, fields, reviewer, role="reviewer")
                and _matching_head(fields.get("head") or "", head) is not None
            ):
                if _comment_order(item) <= _comment_order(ready):
                    before.add(round_number)
                else:
                    after.add(round_number)
        return after - before

    def _verdict_behind_question(
        self,
        ticket: TrackedTicket,
        ready: Comment,
        head: str,
        review_round: int,
    ) -> bool:
        """Whether T5 holds a review of head at waiting-max and a trusted
        REVISE or APPROVE for head and a round past review_round follows the
        ready.

        _verdict_after skips that verdict, but the review this ready would
        queue takes it as answered (_answered_by), so T2 would clear the
        pause and T3 or T4 would act on it with no trusted answer. Max's
        answer resumes the paused review instead (Codex PR #112 r9 F1)."""
        with self._connect() as connection:
            paused = connection.execute(
                """
                SELECT paused_action, paused_head FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if (
            paused is None
            or paused["paused_action"] != "review"
            or _matching_head(str(paused["paused_head"] or ""), head) is None
        ):
            return False
        reviewer = f"{ticket.factory.reviewer_harness}-bot"
        for item in ticket.comments:
            if _comment_order(item) <= _comment_order(ready):
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            round_number = _round_number(fields.get("round"))
            if (
                round_number is not None
                and round_number > review_round
                and fields.get("kind") == "verdict"
                and fields.get("verdict") in ("REVISE", "APPROVE")
                and self._trusted(ticket, item, fields, reviewer, role="reviewer")
                and _matching_head(fields.get("head") or "", head) is not None
            ):
                return True
        return False

    def _paused_between(
        self, ticket: TrackedTicket, ready: Comment, verdict: Comment
    ) -> bool:
        """Whether T5 holds the ticket at waiting-max at any point from ready
        to verdict. With the ready unreviewed, answers resume clarify, not
        review, so T3 and T4 act on no such verdict (#112 r7, r8)."""
        start, end = _comment_order(ready), _comment_order(verdict)
        waiting = held = False
        for item in sorted(ticket.comments, key=_comment_order):
            order = _comment_order(item)
            if order >= end:
                break
            held = held or (waiting and order >= start)
            try:
                handoff = parse_comment(item.body)
            except HandoffError:
                continue
            fields = handoff.as_dict()
            if fields.get("kind") == "questions":
                waiting = waiting or (
                    self._trusted_session(ticket, item, fields)
                    and (
                        fields.get("state") == "waiting-max"
                        or (
                            fields.get("role") != "orchestrator"
                            and _has_questions(handoff.body)
                        )
                    )
                )
            elif fields.get("kind") == "answers" and self._trusted(
                ticket, item, fields, "grok-bot", role="orchestrator"
            ):
                waiting = False
        return held or waiting

    def _queue_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        *,
        head: str,
        generation: int,
        detail: str | None = None,
    ) -> tuple[bool, Comment | None]:
        """Queue the next review round for comment, with the later trusted
        verdict that already answered it (_enqueue_step); (False, None), with
        nothing queued, past the cap."""
        _state, completed_rounds = self._review_context(ticket)
        review_round = completed_rounds + 1
        if review_round > self._effective_cap(ticket):
            self.flag_attention(
                ticket.repo,
                ticket.ticket_id,
                "ready-after-cap",
                f"review round {review_round} for head {head[:12]} is past the cap "
                f"and no extra round was approved (comment {comment.id})",
            )
            return False, None
        _job, answered = self._enqueue_or_flag(
            ticket,
            comment,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="review",
                recipient=ticket.factory.reviewer_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                head=head,
                detail=self._job_detail(
                    ticket,
                    action="review",
                    head=head,
                    round_number=review_round,
                    instruction=detail,
                ),
            ),
            dedupe_key=(
                f"review:{_scope(ticket.repo, ticket.ticket_id)}:"
                f"{head}:r{review_round}"
            ),
        )
        return True, answered

    def _flag_ready_after_cap(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
    ) -> None:
        self.flag_attention(
            ticket.repo,
            ticket.ticket_id,
            "ready-after-cap",
            f"ready {_comment_link(comment)} for head {head[:12]} came after the "
            "review cap with no extra round approved; nothing was queued",
        )

    def _queue_patch_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        current_head: str,
    ) -> tuple[bool, str]:
        queued, _answered = self._queue_review(
            ticket,
            comment,
            head=current_head,
            generation=self._generation(ticket.repo, ticket.ticket_id),
            detail=f"Approval does not cover current head {current_head}",
        )
        if not queued:
            return True, "capped-patch-review"
        self._set_workflow_state(ticket, "review")
        return True, "queued-patch-review"

    def _review_context(self, ticket: TrackedTicket) -> tuple[str | None, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT workflow_state, review_round FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None, 0
        state = (
            str(row["workflow_state"])
            if row["workflow_state"] is not None
            else None
        )
        return state, int(row["review_round"])

    def _has_review_job(self, ticket: TrackedTicket, head: str, round_number: int) -> bool:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT head, detail FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        return any(
            _detail_round_number(row["detail"]) == round_number
            and _matching_head(str(row["head"] or ""), head) is not None
            for row in rows
        )

    def _latest_review_head(self, ticket: TrackedTicket) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None or row["head"] is None:
            return None
        return str(row["head"])

    def _record_ci_result(
        self,
        ticket: TrackedTicket,
        head: str,
        status: str,
    ) -> tuple[str, int]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT status, gate_count_after FROM ci_events
                WHERE repo = ? AND ticket_id = ? AND head = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "failure" and status == "success":
                    # Issue #84: CI was rerun at this head and its latest
                    # attempts all passed. The recorded failure no longer
                    # holds; green resets the gate count as a new head would.
                    connection.execute(
                        """
                        UPDATE ci_events SET status = 'success', gate_count_after = 0
                        WHERE repo = ? AND ticket_id = ? AND head = ?
                        """,
                        (ticket.repo.lower(), ticket.ticket_id, head),
                    )
                    connection.execute(
                        """
                        UPDATE ticket_state SET gate_count = 0
                        WHERE repo = ? AND ticket_id = ?
                        """,
                        (ticket.repo.lower(), ticket.ticket_id),
                    )
                    connection.commit()
                    return "success", 0
                connection.commit()
                return str(existing["status"]), int(existing["gate_count_after"])
            row = connection.execute(
                """
                SELECT gate_count FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            previous = int(row["gate_count"]) if row is not None else 0
            count = previous + 1 if status == "failure" else 0
            connection.execute(
                """
                INSERT INTO ci_events (
                    repo, ticket_id, head, status, gate_count_after
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, head, status, count),
            )
            connection.execute(
                """
                INSERT INTO ticket_state (repo, ticket_id, gate_count, stuck)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET gate_count = excluded.gate_count
                """,
                (ticket.repo.lower(), ticket.ticket_id, count),
            )
            connection.commit()
        return status, count
