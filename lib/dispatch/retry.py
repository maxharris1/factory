"""TR: Max's kind=retry restarts a stuck ticket at the step that got stuck."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from lib.dispatch.helpers import _comment_link, _scope
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import parse_comment
from lib.queue import Job, JobRequest, _job

RetryStep = Literal["replay", "ci-fix", "plan", "build"]

# A retry buys exactly one more attempt and never resets the counter that
# made the ticket stuck. "replay" clones the newest harness job at attempt 2,
# so its next failure is stuck again.
_STEP_BY_REASON: Mapping[str, RetryStep] = {
    "ci-red-3": "ci-fix",
    "plan-gaps-2": "plan",
}
_STEP_BY_STATE: Mapping[str, RetryStep] = {
    "planning": "plan",
    "building": "build",
    "fixing": "replay",
    "review": "replay",
}
_HARNESS_ACTIONS = ("plan", "build", "review", "fix", "update-from-main")


def _retry_step(
    stuck_reason: str | None,
    workflow_state: str | None,
    newest_job_state: str | None,
) -> RetryStep | None:
    """The step a retry queues, or None when it only clears stuck."""
    step = _STEP_BY_REASON.get(stuck_reason or "")
    if step is None:
        step = (
            "replay"
            if newest_job_state == "failed"
            else _STEP_BY_STATE.get(workflow_state or "")
        )
    if step == "replay" and newest_job_state is None:
        return None
    return step


def _with_fields(detail: str | None, **fields: object) -> str | None:
    try:
        context = json.loads(detail) if detail else {}
    except ValueError:
        return detail
    if not isinstance(context, dict):
        return detail
    context.update(fields)
    return json.dumps(context, separators=(",", ":"), sort_keys=True)


class RetryMixin:
    def _t_retry(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not (
            self._trusted(ticket, comment, fields, "grok-bot", role="orchestrator")
            and self._may_approve_go(comment.author)
        ):
            return True, "rejected-retry-author"
        if not parse_comment(comment.body).body.strip():
            return True, "rejected-retry-no-reason"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            outcome, state = self._retry_in_transaction(connection, ticket, comment)
            connection.commit()
        if state is not None:
            self._write_state_label(ticket, state)
        return True, outcome

    def _retry_in_transaction(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[str, str | None]:
        """The retry's outcome and the state label to write after commit."""
        key = (ticket.repo.lower(), ticket.ticket_id)
        row = connection.execute(
            """
            SELECT stuck, stuck_reason, stuck_source_comment_id, workflow_state,
                   review_round, plan_gap_count, generation
            FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            key,
        ).fetchone()
        prior = connection.execute(
            """
            SELECT outcome FROM ticket_retries
            WHERE repo = ? AND ticket_id = ? AND comment_id = ?
            """,
            (*key, comment.id),
        ).fetchone()
        if prior is not None:
            outcome = str(prior["outcome"])
            if outcome.startswith("rejected-") or row is None:
                return outcome, None
            return outcome, row["workflow_state"]
        reason = None if row is None else row["stuck_reason"]
        if row is None or not row["stuck"]:
            return self._record_retry(
                connection, ticket, comment, reason, None, "rejected-retry-not-stuck"
            ), None
        active = connection.execute(
            """
            SELECT 1 FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND recipient != 'grok'
              AND state IN ('queued', 'running')
            LIMIT 1
            """,
            key,
        ).fetchone()
        if active is not None:
            return self._record_retry(
                connection, ticket, comment, reason, None, "rejected-retry-job-active"
            ), None
        newest_row = connection.execute(
            f"""
            SELECT * FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND recipient != 'grok'
              AND action IN ({", ".join("?" for _ in _HARNESS_ACTIONS)})
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (*key, *_HARNESS_ACTIONS),
        ).fetchone()
        newest = None if newest_row is None else _job(newest_row)
        step = _retry_step(
            reason, row["workflow_state"], None if newest is None else newest.state
        )
        generation = int(row["generation"]) + 1
        state = "planning" if step == "plan" else row["workflow_state"]
        gaps = int(row["plan_gap_count"])
        if step == "plan":
            # One plan round left: the resumed plan is checked as round 2,
            # and gaps again go stuck again.
            gaps = min(gaps, 1)
        connection.execute(
            """
            UPDATE ticket_state
            SET stuck = 0, stuck_reason = NULL, stuck_source_comment_id = NULL,
                stuck_wake_job_id = NULL, generation = ?, workflow_state = ?,
                plan_gap_count = ?
            WHERE repo = ? AND ticket_id = ?
            """,
            (generation, state, gaps, *key),
        )
        for wake in connection.execute(
            """
            SELECT id FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND action = 'stuck'
              AND state = 'queued'
            """,
            key,
        ).fetchall():
            self.queue.cancel(str(wake["id"]), "retried", connection=connection)
        request = (
            None
            if step is None
            else self._retry_request(
                connection, ticket, comment, row, step, newest, generation, gaps
            )
        )
        if request is None:
            return self._record_retry(
                connection, ticket, comment, reason, None, "retried-no-job"
            ), state
        job, _ = self._enqueue_step(
            connection,
            ticket,
            comment,
            request,
            dedupe_key=f"retry:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}",
        )
        return self._record_retry(
            connection,
            ticket,
            comment,
            reason,
            job,
            f"queued-retry-{request.action}",
        ), state

    def _retry_request(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        row: sqlite3.Row,
        step: RetryStep,
        newest: Job | None,
        generation: int,
        gaps: int,
    ) -> JobRequest | None:
        if step == "replay":
            assert newest is not None
            return replace(
                newest.request,
                generation=generation,
                attempt=2,
                detail=_with_fields(
                    newest.request.detail,
                    retry_of=newest.id,
                    retry_comment=comment.id,
                    resume=True,
                ),
            )
        head = None
        round_number = int(row["review_round"]) + 1
        if step == "ci-fix":
            head = ticket.current_head
            if head is None:
                return None
            result = ticket.ci.get(head)
            action = "fix"
            detail = self._job_detail(
                ticket,
                action=action,
                head=head,
                round_number=round_number,
                cap=self._effective_cap(ticket, connection),
                instruction=(result.log if result else None) or f"CI failed on {head}",
                resume=True,
            )
        elif step == "plan":
            action = "plan"
            detail = self._job_detail(
                ticket,
                action=action,
                instruction=(
                    f"round=2/2; previous plan {self._previous_plan_link(ticket, row)}"
                    if gaps
                    else None
                ),
                resume=True,
            )
        else:
            action = "build"
            detail = self._job_detail(
                ticket,
                action=action,
                round_number=round_number,
                cap=self._effective_cap(ticket, connection),
                resume=True,
            )
        return JobRequest(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            action=action,
            recipient=ticket.factory.builder_harness,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            generation=generation,
            head=head,
            detail=_with_fields(detail, retry_comment=comment.id),
        )

    def _previous_plan_link(self, ticket: TrackedTicket, row: sqlite3.Row) -> str:
        source = row["stuck_source_comment_id"]
        found = next((item for item in ticket.comments if item.id == source), None)
        if found is not None:
            return _comment_link(found)
        return ticket.ticket_url or f"comment:{source}"

    def _record_retry(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        stuck_reason: str | None,
        job: Job | None,
        outcome: str,
    ) -> str:
        connection.execute(
            """
            INSERT INTO ticket_retries (
                repo, ticket_id, comment_id, stuck_reason, resumed_action,
                job_id, outcome, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket.repo.lower(),
                ticket.ticket_id,
                comment.id,
                stuck_reason,
                None if job is None else job.request.action,
                None if job is None else job.id,
                outcome,
                self.queue.now(),
            ),
        )
        return outcome
