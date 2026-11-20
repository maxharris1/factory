"""Plan-step handling (T_plan)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass

from lib.dispatch.helpers import _comment_link, _round_number, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, RetryableStateError, TrackedTicket
from lib.factory_md import padded_estimate
from lib.followups import (
    SplitPart,
    proposed_split,
    split_comment,
    split_dependencies,
    split_issue,
    split_link_marker,
    split_marker,
    split_parts,
)
from lib.handoff import HandoffError, parse_comment, plan_estimate
from lib.queue import JobRequest


@dataclass(frozen=True)
class _PendingSplit:
    comment_id: str
    plan_text: str
    since: float


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
        settled = self._open_plan_check(ticket, comment, reported_round)
        if settled in ("stale-plan", "auto-split"):
            return True, settled
        if settled == "waiting-max-size":
            self._write_state_label(ticket, "waiting-max")
            return True, settled
        if settled is not None:
            self._set_workflow_state(ticket, "plan-check")
            self._set_workflow_state(
                ticket,
                "stuck" if settled == "stuck-plan-gaps" else (
                    "planning" if settled == "queued-plan-gap" else "building"
                ),
            )
            return True, settled
        plan_text = parse_comment(comment.body).body.strip()
        estimate = plan_estimate(plan_text)
        mapped = ac == "met" and estimate is not None
        over_size = self._plan_over_size(ticket, estimate) if mapped else None
        parts = (
            self._auto_split_parts(ticket, plan_text)
            if mapped and over_size is not None
            else ()
        )
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
            if parts:
                connection.execute(
                    """
                    UPDATE ticket_state SET paused_plan_text = ?, plan_estimate = ?
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (plan_text, estimate, ticket.repo.lower(), ticket.ticket_id),
                )
                connection.execute(
                    """
                    INSERT INTO plan_events (
                        repo, ticket_id, comment_id, outcome
                    ) VALUES (?, ?, ?, 'auto-split')
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, comment.id),
                )
                connection.commit()
                return True, "auto-split"
            gap_count = int(state["plan_gap_count"])
            paused: dict[str, object] = {}
            if mapped and over_size is not None:
                next_state = "waiting-max"
                outcome = "waiting-max-size"
                paused = {
                    "paused_action": "build",
                    "paused_recipient": builder_harness,
                    "paused_head": None,
                    "paused_generation": generation,
                    "paused_question_id": comment.id,
                    "paused_plan_text": plan_text,
                }
                self.queue.enqueue_in_transaction(
                    connection,
                    self._grok_request(
                        JobRequest(
                            repo=ticket.repo,
                            ticket_id=ticket.ticket_id,
                            action="clarify",
                            recipient="grok",
                            source_comment_id=comment.id,
                            timeout_seconds=ticket.job_timeout_minutes * 60,
                            generation=generation,
                            detail=self._project_wake_detail(
                                ticket,
                                f"plan-over-size: {over_size}, {_comment_link(comment)}\n"
                                f"{proposed_split(plan_text)}",
                            ),
                        )
                    ),
                    dedupe_key=(
                        f"plan-size:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{comment.id}"
                    ),
                )
            elif mapped:
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
                        plan_text=plan_text,
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
                            plan_text=plan_text,
                            unmapped=_unmapped(fields, plan_text),
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
                also={
                    "plan_gap_count": gap_count,
                    "plan_check_comment_id": None,
                    **paused,
                    **({"plan_estimate": estimate} if mapped else {}),
                },
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

    def _auto_split_parts(
        self, ticket: TrackedTicket, plan_text: str
    ) -> tuple[SplitPart, ...]:
        if ticket.factory.tickets != "github" or not ticket.intake_label:
            return ()
        return split_parts(plan_text)

    def _pending_split(self, ticket: TrackedTicket) -> _PendingSplit | None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state.plan_check_comment_id, state.paused_plan_text
                FROM ticket_state AS state
                JOIN plan_events AS event
                  ON event.repo = state.repo AND event.ticket_id = state.ticket_id
                 AND event.comment_id = state.plan_check_comment_id
                WHERE state.repo = ? AND state.ticket_id = ?
                  AND state.workflow_state = 'plan-check'
                  AND state.paused_plan_text IS NOT NULL
                  AND event.outcome = 'auto-split'
                """,
                key,
            ).fetchone()
            if row is None:
                return None
            since = connection.execute(
                """
                SELECT max(at) FROM ticket_transitions
                WHERE repo = ? AND ticket_id = ? AND to_state = 'plan-check'
                """,
                key,
            ).fetchone()[0]
        return _PendingSplit(
            str(row["plan_check_comment_id"]),
            str(row["paused_plan_text"]),
            float(since or 0.0),
        )

    def _finish_auto_split(self, ticket: TrackedTicket) -> None:
        pending = self._pending_split(ticket)
        if pending is None:
            return
        comment_id = pending.comment_id
        parts = split_parts(pending.plan_text)
        issue = (ticket.source_issue or ticket.ticket_id).removeprefix("#")
        plan_url = next(
            (
                _comment_link(comment)
                for comment in ticket.comments
                if comment.id == comment_id
            ),
            f"comment:{comment_id}",
        )
        actions = self._review_actions(ticket)
        numbers: list[int] = []
        for number, (part, depends) in enumerate(zip(parts, split_dependencies(parts)), 1):
            title, body = split_issue(
                issue,
                plan_url,
                comment_id,
                number,
                part,
                ticket.risk,
                tuple(numbers[ref - 1] for ref in depends),
            )
            numbers.append(
                actions.file_issue(
                    ticket,
                    title,
                    body,
                    (ticket.intake_label,),
                    split_marker(comment_id, number),
                    pending.since,
                )
            )
        actions.comment_once(
            ticket,
            split_comment(comment_id, tuple(numbers)),
            split_link_marker(comment_id),
        )
        self._abandon_split(ticket, comment_id)

    def _abandon_split(self, ticket: TrackedTicket, comment_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            written = write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "abandoned",
                source="plan-auto-split",
                at=self.queue.now(),
                guard="workflow_state = 'plan-check' AND plan_check_comment_id = ?",
                guard_args=(comment_id,),
                also={"plan_check_comment_id": None, "paused_plan_text": None},
            )
            if not written:
                connection.rollback()
                return
            closed = self._fail_abandoned_queued(connection)
            connection.commit()
        self._log_abandoned_jobs(closed)
        if self.log is not None:
            self.log(f"abandoned: {ticket.repo}#{ticket.ticket_id} split into intake issues")
        try:
            self._write_state_label(ticket, "abandoned")
        except RetryableStateError:
            self.record_poll_error(ticket.repo, ticket.ticket_id, "abandoned-label")

    def _plan_over_size(self, ticket: TrackedTicket, estimate: int) -> str | None:
        """Why the plan is over the line, or None when it is not."""
        from lib.evidence.criteria import criteria_evidence

        criteria = len(
            criteria_evidence(
                issue_key=ticket.source_issue or ticket.ticket_id,
                issue_url=ticket.ticket_url,
                brief=ticket.brief_comment,
                issue_body=ticket.issue_body,
                pr_body=ticket.pr_body,
                ready_starts_review=ticket.ready_starts_review,
                pr_url=ticket.ticket_url,
                limit=sys.maxsize,
            ).criteria
        )
        max_lines = ticket.factory.plan_max_lines
        max_criteria = ticket.factory.plan_max_criteria
        padded = padded_estimate(estimate)
        if padded <= max_lines and criteria <= max_criteria:
            return None
        return (
            f"estimate={estimate} x1.35={padded} (line {max_lines}), "
            f"criteria={criteria} (line {max_criteria})"
        )

    def _open_plan_check(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
    ) -> str | None:
        """Move the ticket to plan-check for this plan comment, in one
        transaction. Returns the outcome when the comment is already settled
        (a replay, or stale-plan), None when the plan check is open."""
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
                return str(prior["outcome"])
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
                return "stale-plan"
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
        return None

    def _t1p_accepted_or_accepts_plan(
        self, ticket: TrackedTicket, comment: Comment, reported_round: int | None
    ) -> bool:
        with self._connect() as connection:
            if connection.execute(
                """
                SELECT 1 FROM plan_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone() is not None:
                return True
            state = connection.execute(
                """
                SELECT workflow_state, plan_gap_count, plan_check_comment_id
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return (
            self._handled_outcome(ticket, comment.id) is None
            and state is not None
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


def gap_of(comment: Comment) -> tuple[str, str] | None:
    """A plan comment's text and the item it left unmapped, or None when the
    comment is not a plan."""
    try:
        handoff = parse_comment(comment.body)
    except HandoffError:
        return None
    fields = handoff.as_dict()
    if fields["kind"] != "plan":
        return None
    return handoff.body.strip(), _unmapped(fields, handoff.body)


def _unmapped(fields: Mapping[str, str], body: str) -> str:
    if "unsat" in fields:
        return fields["unsat"]
    return "expected size" if plan_estimate(body) is None else "not named"
