"""T5 questions and answers, plus cancel and requeue of answered jobs."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from lib.dispatch.constants import FINISHED_STATES
from lib.dispatch.helpers import (
    _ANSWERS,
    _REJECTED_OUTCOMES,
    _comment_link,
    _comment_order,
    _detail_round_number,
    _has_questions,
    _matching_head,
    _round_number,
    _scope,
    _state_for_action,
)
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.queue import Job, JobRequest, _job
from lib.state_backup import RESTORED


class QuestionsMixin:
    def _t5_questions(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted_session(ticket, comment, fields):
            return True, "rejected-questions-author"
        if fields.get("state") != "waiting-max" and (
            fields.get("role") == "orchestrator"
            or not _has_questions(parse_comment(comment.body).body)
        ):
            return True, "no-questions"
        step = self._latest_step(ticket)
        if step is None:
            action = "clarify"
            recipient = "grok"
            head = ticket.current_head
            generation = self._generation(ticket.repo, ticket.ticket_id)
        else:
            action, recipient, head, generation = step
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            waiting = connection.execute(
                """
                SELECT workflow_state, paused_question_id FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if waiting is not None and waiting["workflow_state"] == "waiting-max":
                connection.commit()
                if waiting["paused_question_id"] == comment.id:
                    self._write_state_label(ticket, "waiting-max")
                    return True, "waiting-max"
                return True, "already-waiting-max"
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, gate_count, stuck, generation,
                    workflow_state, paused_action, paused_recipient,
                    paused_head, paused_generation, paused_question_id
                ) VALUES (?, ?, 0, 0, ?, 'waiting-max', ?, ?, ?, ?, ?)
                ON CONFLICT(repo, ticket_id)
                DO UPDATE SET
                    workflow_state = 'waiting-max',
                    paused_action = excluded.paused_action,
                    paused_recipient = excluded.paused_recipient,
                    paused_head = excluded.paused_head,
                    paused_generation = excluded.paused_generation,
                    paused_question_id = excluded.paused_question_id
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    generation,
                    action,
                    recipient,
                    head,
                    generation,
                    comment.id,
                ),
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
                        head=head,
                        detail=self._project_wake_detail(
                            ticket,
                            _comment_link(comment),
                        ),
                    )
                ),
                dedupe_key=(
                    f"t5-wait:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                ),
            )
            connection.commit()
        self._write_state_label(ticket, "waiting-max")
        return True, "waiting-max"

    def _t5_answers(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not self._trusted(
            ticket, comment, fields, "grok-bot", role="orchestrator"
        ):
            return True, "rejected-answers-author"
        with self._connect() as connection:
            paused = connection.execute(
                """
                SELECT workflow_state, paused_action, paused_recipient,
                       paused_head, paused_generation, paused_question_id
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            resumed = connection.execute(
                """
                SELECT action FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND source_comment_id = ? AND dedupe_key LIKE 't5:%'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
        if (
            paused is not None
            and paused["workflow_state"] != "waiting-max"
            and resumed is not None
        ):
            resumed_state = str(paused["workflow_state"])
            self._write_state_label(ticket, resumed_state)
            return True, f"resumed-{resumed['action']}"
        if (
            paused is None
            or paused["workflow_state"] != "waiting-max"
            or paused["paused_action"] is None
        ):
            return True, "rejected-answers-without-question"
        action = str(paused["paused_action"])
        recipient = str(paused["paused_recipient"])
        generation = int(paused["paused_generation"])
        head = str(paused["paused_head"]) if paused["paused_head"] is not None else None
        request = JobRequest(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            action=action,
            recipient=recipient,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            generation=generation,
            head=head,
            detail=self._job_detail(
                ticket,
                action=action,
                head=head,
                round_number=(
                    None if action == "plan" else self._review_context(ticket)[1] + 1
                ),
                instruction=_comment_link(comment),
                resume=True,
            ),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enqueue_step(
                connection,
                ticket,
                comment,
                request,
                dedupe_key=(
                    f"t5:{_scope(ticket.repo, ticket.ticket_id)}:"
                    f"{paused['paused_question_id']}:{comment.id}"
                ),
            )
            connection.commit()
        resumed_state = _state_for_action(action)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ticket_state
                SET workflow_state = ?, paused_action = NULL,
                    paused_recipient = NULL, paused_head = NULL,
                    paused_generation = NULL, paused_question_id = NULL
                WHERE repo = ? AND ticket_id = ?
                """,
                (resumed_state, ticket.repo.lower(), ticket.ticket_id),
            )
        self._write_state_label(ticket, resumed_state)
        return True, f"resumed-{action}"

    def _answered_by(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        action: str,
        recipient: str,
        *,
        head: str | None = None,
        round_number: int | None = None,
    ) -> Comment | None:
        """The first later trusted orch:v1 comment that already answered the
        action job comment queues for recipient, or None (#108, #66).

        Only the answer that job's step accepts counts: a plan for a plan, a
        ready for a build or fix (at a new head when the job has one, as
        _posted_handoff reads it), and for a review a REVISE or APPROVE
        naming head at round_number. It must come from a login trusted for recipient's bot
        and role, and not have been handled as rejected. With an intact
        database no such answer exists yet when a comment is handled; it is
        found on a replay (a lost or restored database) or when both
        comments landed while the poller was down.
        """
        kind, role = _ANSWERS[action]
        after = _comment_order(comment)
        for item in sorted(ticket.comments, key=_comment_order):
            if _comment_order(item) <= after:
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] != kind:
                continue
            if not self._trusted(ticket, item, fields, f"{recipient}-bot", role=role):
                continue
            reported = fields.get("head", "")
            if action == "review":
                if (
                    _matching_head(reported, head) is None
                    or _round_number(fields.get("round")) != round_number
                    or fields.get("verdict") not in ("REVISE", "APPROVE")
                ):
                    # #71: T3 and T4 act on no other verdict (BLOCKED), so
                    # it answers no review (Codex PR #112 r5 F1).
                    continue
            elif head is not None and _matching_head(reported, head) is not None:
                continue
            handled = self._handled_outcome(ticket, item.id)
            if handled is not None and handled.startswith(_REJECTED_OUTCOMES):
                continue
            return item
        return None

    def _cancel_answered_jobs(self, ticket: TrackedTicket) -> None:
        """Cancel this ticket's queued steps that a later trusted comment
        already answered (#108).

        Catches a job queued before its answer was read: one restored from
        a backup taken before the builder or reviewer posted, whose
        triggering comment is already handled and so is never replayed.
        """
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        sources = {comment.id: comment for comment in ticket.comments}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
                  AND recipient != 'grok'
                  AND action IN ({", ".join("?" for _ in _ANSWERS)})
                ORDER BY created_at, id
                """,
                (ticket.repo.lower(), ticket.ticket_id, *_ANSWERS),
            ).fetchall()
        answered: list[tuple[Job, Comment]] = []
        for row in rows:
            job = _job(row)
            source = sources.get(job.request.source_comment_id)
            if source is None:
                continue
            answer = self._answered_by(
                ticket,
                source,
                job.request.action,
                job.request.recipient,
                head=job.request.head,
                round_number=_detail_round_number(job.request.detail),
            )
            if answer is not None:
                answered.append((job, answer))
        if not answered:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for job, answer in answered:
                current = connection.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
                if current is not None and current["state"] == "queued":
                    self._cancel_answered(connection, job, job.request, answer)
            connection.commit()

    def _requeue_unanswered_jobs(self, ticket: TrackedTicket) -> None:
        """Queue again a step cancelled as answered when that answer was then
        handled as rejected, or held by a restore (state_backup.RESTORED),
        when no trusted answer stands (#108).

        The cancel relied on that answer being accepted. Without this, a
        replay that met a plan of another round, or a ready at a head the PR
        does not have, left the ticket with no runnable work. A held step
        whose source comment the read lacks is queued again: nothing shows
        it was answered.
        """
        ticket = self._polled.get((ticket.repo.lower(), ticket.ticket_id), ticket)
        if self._is_stuck(ticket) or self._review_context(ticket)[0] in FINISHED_STATES:
            return
        sources = {comment.id: comment for comment in ticket.comments}
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT jobs.*, (
                    SELECT detail FROM job_events
                    WHERE job_id = jobs.id AND event = 'cancelled'
                    ORDER BY id DESC LIMIT 1
                ) AS cancel_reason
                FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'cancelled'
                  AND recipient != 'grok'
                  AND action IN ({", ".join("?" for _ in _ANSWERS)})
                ORDER BY created_at, id
                """,
                (ticket.repo.lower(), ticket.ticket_id, *_ANSWERS),
            ).fetchall()
        unanswered: list[tuple[Job, str]] = []
        for row in rows:
            reason = row["cancel_reason"] or ""
            source = sources.get(row["source_comment_id"])
            if reason == RESTORED:
                requeue_reason = "restored-unanswered"
            elif reason.startswith("answered-by:") and source is not None:
                answer_id = reason.removeprefix("answered-by:")
                outcome = self._handled_outcome(ticket, answer_id)
                if outcome is None or not outcome.startswith(_REJECTED_OUTCOMES):
                    continue
                requeue_reason = f"answer-rejected:{answer_id}"
            else:
                continue
            job = _job(row)
            if source is None or self._answered_by(
                ticket,
                source,
                job.request.action,
                job.request.recipient,
                head=job.request.head,
                round_number=_detail_round_number(job.request.detail),
            ) is None:
                unanswered.append((job, requeue_reason))
        if not unanswered:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            requeued = [
                (job, reason)
                for job, reason in unanswered
                if self.queue.requeue(job.id, reason, connection=connection)
            ]
            connection.commit()
        if self.log is not None:
            for job, reason in requeued:
                self.log(
                    f"requeued {job.request.action} ({reason}) "
                    f"({ticket.repo}#{ticket.ticket_id})"
                )

    def _cancel_answered(
        self,
        connection: sqlite3.Connection,
        job: Job,
        request: JobRequest,
        answered: Comment,
    ) -> None:
        """Cancel the job this comment just queued: answered already."""
        if job.state != "queued" or job.request.source_comment_id != request.source_comment_id:
            return
        self.queue.cancel(job.id, f"answered-by:{answered.id}", connection=connection)
        if self.log is not None:
            self.log(
                f"skipped {request.action}: answered by {answered.id} "
                f"({request.repo}#{request.ticket_id})"
            )
