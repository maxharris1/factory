from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from lib import job_detail
from lib.dispatch.constants import APPROVE_ROUND_STATES, REVISE_ROUND_STATES
from lib.dispatch.helpers import _comment_link, _matching_head, _round_number, _scope, red_checks
from lib.dispatch.qa import QaFix, ReviewFix, Wait
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, Registration, TrackedTicket
from lib.queue import JobRequest


@dataclass(frozen=True)
class ReviseCIGate:
    outcome: tuple[bool, str] | None = None
    ci_failure: tuple[str, ...] = ()


class T3Mixin:
    def _t3(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_reviewer(ticket, comment, fields):
            return True, "rejected-revise-author"
        registration = self._registration(ticket, comment, "verdict")
        if registration is Registration.UNREGISTERED:
            return True, "rejected-unregistered-verdict"
        if registration is Registration.PENDING:
            return False, "verdict-awaiting-registration"
        reported_round = _round_number(fields.get("round"))
        if self._over_cap(ticket, reported_round):
            return True, "rejected-over-cap"
        verdict_head = fields.get("head")
        if _matching_head(verdict_head or "", ticket.current_head) is None:
            return True, "stale-revise"
        if "unsat" in fields:
            return self._t3_unsat(ticket, comment, fields["unsat"], reported_round)
        qa = None
        match self._qa_gate(ticket, "REVISE", ticket.current_head, since=comment.created_at):
            case Wait(outcome):
                return False, outcome
            case ReviewFix(report_id, drift_report_id):
                if drift_report_id is not None:
                    self._wake_skill_drift(ticket, drift_report_id, ticket.current_head)
                if report_id is not None:
                    qa = self._qa_fix(ticket, report_id)
        qa_ids = frozenset(qa.failed) if qa is not None else frozenset()
        repeat = self._repeat_trigger(ticket, comment, reported_round, qa_ids)
        if repeat is not None:
            return self._t3_repeat(ticket, comment, repeat, reported_round)
        ci_failure: tuple[str, ...] = ()
        if ticket.factory.start_before_ci:
            gate = self._revise_ci_gate(ticket, comment, reported_round)
            if gate.outcome is not None:
                return gate.outcome
            ci_failure = gate.ci_failure
        return self._spend_review_round(
            ticket, comment, reported_round, ci_failure=ci_failure, qa=qa
        )

    def _spend_review_round(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
        *,
        ci_failure: tuple[str, ...] = (),
        qa: QaFix | None = None,
        from_approve: bool = False,
    ) -> tuple[bool, str]:
        outcome, state = self._record_revise(
            ticket, comment, reported_round, ci_failure, qa, from_approve
        )
        if state is None:
            self._wake_comment_problem(ticket, comment, "stale", outcome)
            return True, outcome
        if state == "capped":
            self._wake_capped(ticket, comment)
        self._write_state_label(ticket, state)
        return True, outcome

    def _record_revise(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
        ci_failure: tuple[str, ...],
        qa: QaFix | None = None,
        from_approve: bool = False,
    ) -> tuple[str, str | None]:
        ticket = self._fresh_issue(ticket)
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
                return outcome, (
                    "capped" if outcome == "capped-review" else "fixing"
                )
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
                SELECT workflow_state, review_round, generation, cap_extra, stuck
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            assert state is not None
            round_number = int(state["review_round"]) + 1
            cap = self._effective_cap(ticket, connection)
            allowed = APPROVE_ROUND_STATES if from_approve else REVISE_ROUND_STATES
            if (
                state["workflow_state"] not in allowed
                or reported_round != round_number
            ):
                connection.commit()
                return "stale-revise", None
            granted = (
                self._auto_granted_cap_extra(ticket, state)
                if round_number >= cap
                else None
            )
            if granted is not None:
                cap += 1
            next_state = "capped" if round_number >= cap else "fixing"
            answered = None
            if next_state == "fixing":
                detail = self._job_detail(
                    ticket,
                    action="fix",
                    head=ticket.current_head,
                    round_number=round_number + 1,
                    cap=cap,
                    instruction=qa.link if from_approve and qa is not None else _comment_link(comment),
                    ci_failure=ci_failure,
                    qa_findings=qa,
                )
                request = JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="fix",
                    recipient=ticket.factory.builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(state["generation"]),
                    head=ticket.current_head,
                    detail=detail,
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
                also={
                    "review_round": round_number,
                    "cap_extra": (
                        int(state["cap_extra"]) if granted is None else granted
                    ),
                },
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
            if granted is not None:
                self._record_auto_extra(connection, ticket, comment, granted)
            self._record_followups(connection, ticket, comment, round_number)
            connection.commit()
        return outcome, next_state

    def _t3_unsat(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        criterion_id: str,
        reported_round: int | None,
    ) -> tuple[bool, str]:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*key, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                self._write_state_label(ticket, "waiting-max")
                return True, str(prior["outcome"])
            state = connection.execute(
                """
                SELECT workflow_state, review_round, generation FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                key,
            ).fetchone()
            if (
                state is None
                or state["workflow_state"] not in ("review", "awaiting-ok")
                or reported_round != int(state["review_round"]) + 1
            ):
                connection.commit()
                self._wake_comment_problem(ticket, comment, "stale", "stale-revise")
                return True, "stale-revise"
            generation = int(state["generation"])
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "waiting-max",
                source="t3-unsat",
                at=self.queue.now(),
                also={
                    "paused_action": "review",
                    "paused_recipient": ticket.factory.reviewer_harness,
                    "paused_head": ticket.current_head,
                    "paused_generation": generation,
                    "paused_question_id": comment.id,
                },
            )
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, 'waiting-max-unsat')
                """,
                (*key, comment.id),
            )
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
                        head=ticket.current_head,
                        detail=self._project_wake_detail(
                            ticket,
                            "criterion-unsatisfiable: "
                            f"{self._criterion_text(connection, ticket, comment, criterion_id)}, "
                            f"{_comment_link(comment)}",
                        ),
                    )
                ),
                dedupe_key=f"t3-unsat:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}",
            )
            connection.commit()
        self._write_state_label(ticket, "waiting-max")
        return True, "waiting-max-unsat"

    @staticmethod
    def _criterion_text(
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        criterion_id: str,
    ) -> str:
        rows = connection.execute(
            """
            SELECT id, head, detail FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
            ORDER BY coalesce(handoff_comment_id = ?, 0) DESC, created_at DESC, rowid DESC
            """,
            (ticket.repo.lower(), ticket.ticket_id, comment.id),
        ).fetchall()
        review = next(
            (row for row in rows if _matching_head(str(row["head"] or ""), ticket.current_head)),
            None,
        )
        detail = (
            json.loads(job_detail.load_row(connection, review["id"], review["detail"]) or "{}")
            if review is not None
            else {}
        )
        criteria = detail.get("evidence", {}).get("criteria", {}).get("criteria", [])
        text = next((item["text"] for item in criteria if item["id"] == criterion_id), None)
        if text is None:
            return f"{criterion_id} (not in the criteria)"
        return f"{criterion_id}: {text}"

    def _revise_ci_gate(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_round: int | None,
    ) -> ReviseCIGate:
        state, completed = self._review_context(ticket)
        if state not in ("review", "awaiting-ok") or reported_round != completed + 1:
            return ReviseCIGate()
        head = ticket.current_head
        assert head is not None
        result = ticket.ci.get(head)
        if result is None or result.status == "pending":
            return ReviseCIGate(outcome=(False, "revise-ci-pending"))
        if result.status == "not-run":
            return ReviseCIGate()
        settled = self._settled_red(ticket, comment, head, result)
        if isinstance(settled, str):
            return ReviseCIGate(outcome=(False, f"revise-{settled}"))
        _status, count = self._record_ci_result(ticket, head, settled.status)
        if settled.status == "success":
            return ReviseCIGate()
        if self._mark_stuck_on_third_red(ticket, comment, head, count):
            return ReviseCIGate(outcome=(True, "stuck-ci-red-3"))
        return ReviseCIGate(ci_failure=red_checks(ticket, head))
