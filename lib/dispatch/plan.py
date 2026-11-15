"""Plan-step handling (T_plan)."""

from __future__ import annotations

from collections.abc import Mapping

from lib.dispatch.helpers import _comment_link, _round_number, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.queue import JobRequest


class PlanMixin:
    def _t_plan(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        plan_step = self._latest_plan_step(ticket)
        if plan_step is None:
            return True, "rejected-plan-without-plan-job"
        builder_harness, generation = plan_step
        if not self._trusted(
            ticket,
            comment,
            fields,
            f"{builder_harness}-bot",
            role="builder",
        ):
            return True, "rejected-plan-author"
        ac = fields.get("ac")
        if ac not in ("met", "unmet"):
            return True, "rejected-plan-result"
        reported_round = _round_number(fields.get("round"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM plan_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                outcome = str(prior["outcome"])
                state = "stuck" if outcome == "stuck-plan-gaps" else (
                    "planning" if outcome == "queued-plan-gap" else "building"
                )
                self._set_workflow_state(ticket, "plan-check")
                self._set_workflow_state(ticket, state)
                return True, outcome
            state = connection.execute(
                """
                SELECT workflow_state, plan_gap_count, plan_check_comment_id
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            resuming = (
                state is not None
                and state["workflow_state"] == "plan-check"
                and state["plan_check_comment_id"] == comment.id
            )
            if (
                state is None
                or (state["workflow_state"] != "planning" and not resuming)
                or reported_round != int(state["plan_gap_count"]) + 1
            ):
                connection.commit()
                return True, "stale-plan"
            gap_count = int(state["plan_gap_count"])
            if not resuming:
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    "plan-check",
                    source="plan-check",
                    at=self.queue.now(),
                    also={"plan_check_comment_id": comment.id},
                )
            connection.commit()
        self._write_state_label(ticket, "plan-check")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                """
                SELECT workflow_state, plan_gap_count, plan_check_comment_id,
                    review_round
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if (
                state is None
                or state["workflow_state"] != "plan-check"
                or state["plan_check_comment_id"] != comment.id
                or reported_round != int(state["plan_gap_count"]) + 1
            ):
                connection.commit()
                return True, "stale-plan"
            gap_count = int(state["plan_gap_count"])
            if ac == "met":
                next_state = "building"
                outcome = "queued-plan-build"
                request = JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="build",
                    recipient=builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    detail=self._job_detail(
                        ticket,
                        action="build",
                        round_number=int(state["review_round"]) + 1,
                        cap=self._effective_cap(ticket, connection),
                        instruction=_comment_link(comment),
                    ),
                )
                self._enqueue_step(
                    connection,
                    ticket,
                    comment,
                    request,
                    dedupe_key=(
                        f"plan-build:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{comment.id}"
                    ),
                )
            elif gap_count == 0:
                next_state = "planning"
                outcome = "queued-plan-gap"
                gap_count = 1
                self._enqueue_step(
                    connection,
                    ticket,
                    comment,
                    JobRequest(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        action="plan",
                        recipient=builder_harness,
                        source_comment_id=comment.id,
                        timeout_seconds=ticket.job_timeout_minutes * 60,
                        generation=generation,
                        detail=self._job_detail(
                            ticket,
                            action="plan",
                            instruction=(
                                "round=2/2; previous plan "
                                f"{_comment_link(comment)}"
                            ),
                            plan_round=2,
                        ),
                    ),
                    dedupe_key=(
                        f"plan-gap:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{comment.id}"
                    ),
                )
            else:
                next_state = "stuck"
                outcome = "stuck-plan-gaps"
                gap_count = 2
                connection.execute(
                    """
                    UPDATE ticket_state
                    SET stuck = 1, stuck_reason = 'plan-gaps-2',
                        stuck_source_comment_id = ?, timeout_seconds = ?
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (
                        comment.id,
                        ticket.job_timeout_minutes * 60,
                        ticket.repo.lower(),
                        ticket.ticket_id,
                    ),
                )
                self._ensure_stuck_wake_in_transaction(
                    connection,
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    detail=self._project_wake_detail(
                        ticket,
                        (
                            "plan has gaps a second time, "
                            f"{_comment_link(comment)}"
                        ),
                    ),
                )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                next_state,
                source="plan-gap",
                at=self.queue.now(),
                also={"plan_gap_count": gap_count, "plan_check_comment_id": None},
            )
            connection.execute(
                """
                INSERT INTO plan_events (
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
            connection.commit()
        self._write_state_label(ticket, next_state)
        return True, outcome

    def _t1p_accepts_plan(
        self, ticket: TrackedTicket, comment: Comment, reported_round: int | None
    ) -> bool:
        with self._connect() as connection:
            state = connection.execute(
                """
                SELECT workflow_state, plan_gap_count, plan_check_comment_id
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return (
            state is not None
            and (
                state["workflow_state"] == "planning"
                or (
                    state["workflow_state"] == "plan-check"
                    and state["plan_check_comment_id"] == comment.id
                )
            )
            and reported_round == int(state["plan_gap_count"]) + 1
        )

    def _latest_plan_step(self, ticket: TrackedTicket) -> tuple[str, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recipient, generation FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'plan'
                  AND attempt = 1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["recipient"]), int(row["generation"])
