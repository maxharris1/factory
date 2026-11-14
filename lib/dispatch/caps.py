"""T1 and round caps: over-cap checks, effective caps, cap wakes and resume."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from lib.dispatch.constants import WATCHED_STATES
from lib.dispatch.helpers import _comment_link, _scope
from lib.dispatch.types import Comment, TrackedTicket
from lib.queue import JobRequest


class CapsMixin:
    def _t1(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        trusted = self._trusted(
            ticket, comment, fields, "grok-bot", role="orchestrator"
        )
        if not trusted and not self._approver_go(comment, fields):
            return True, "rejected-go-author"
        current = self._review_context(ticket)[0]
        if current == "handed-off":
            # #108: a handed-off ticket re-enters only through the builder's
            # ready, inside the cap; a go changes nothing.
            return True, "go-after-handoff"
        state = fields.get("state")
        action = {"planning": "plan", "building": "build"}.get(state)
        if action is not None:
            resumed = self._resume_after_cap(ticket, comment)
            if resumed is not None:
                return resumed
        if not trusted:
            return True, "rejected-go-author"
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if action is None:
            self.queue.enqueue(
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="clarify",
                    recipient="grok",
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=generation,
                    detail=self._job_detail(
                        ticket,
                        action="clarify",
                        instruction=(
                            "kind=go must set state=planning or state=building"
                        ),
                    ),
                ),
                dedupe_key=(
                    f"t1-clarify:{_scope(ticket.repo, ticket.ticket_id)}:"
                    f"g{generation}"
                ),
            )
            return True, "queued-go-clarification"
        once = f"g{generation}"
        if current in WATCHED_STATES or (
            current == "waiting-external"
            and self._queue_row(ticket)["approved_head"] is not None
        ):
            # #130: a go on a watched ticket is honored; the ticket leaves
            # the merge queue and ship waits for a new APPROVE. Keyed on the
            # go, since the build that got it approved holds this generation.
            self._leave_merge_queue(ticket, "go")
            once = comment.id
        job, answered = self._enqueue_or_flag(
            ticket,
            comment,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action=action,
                recipient=ticket.factory.builder_harness,
                source_comment_id=comment.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                detail=self._job_detail(
                    ticket,
                    action=action,
                    round_number=(
                        None
                        if action == "plan"
                        else self._review_context(ticket)[1] + 1
                    ),
                ),
            ),
            dedupe_key=f"t1:{_scope(ticket.repo, ticket.ticket_id)}:{action}:{once}",
        )
        if job.request.source_comment_id != comment.id:
            return True, f"deduped-{action}"
        self._set_workflow_state(ticket, state)
        return True, f"answered-{action}" if answered else f"queued-{action}"

    def _resume_after_cap(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        """A trusted go on a capped ticket: Max's OK for one more round (#105).

        One transaction raises cap_extra by 1, moves the ticket to fixing
        and queues the fix, keyed on the go comment so every go Max relays
        queues exactly one fix. None when the ticket is not capped. With the
        GitHub App switch on, only an approver's go counts; any other wakes
        Grok once with go-not-from-approver and changes nothing (#122).
        """
        scope = _scope(ticket.repo, ticket.ticket_id)
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
                self._write_state_label(ticket, "fixing")
                return True, str(prior["outcome"])
            row = connection.execute(
                """
                SELECT workflow_state, review_round, cap_extra, generation
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if row is None or row["workflow_state"] != "capped":
                connection.commit()
                return None
            if not self._may_approve_go(comment.author):
                connection.commit()
                self._wake_comment_problem(
                    ticket, comment, "go-not-from-approver", "go-not-from-approver"
                )
                return True, "go-not-from-approver"
            cap_extra = int(row["cap_extra"]) + 1
            self._enqueue_step(
                connection,
                ticket,
                comment,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="fix",
                    recipient=ticket.factory.builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(row["generation"]),
                    head=ticket.current_head,
                    detail=self._job_detail(
                        ticket,
                        action="fix",
                        head=ticket.current_head,
                        round_number=int(row["review_round"]) + 1,
                        cap=self._config_cap(ticket) + cap_extra,
                        resume=True,
                    ),
                ),
                dedupe_key=f"t1:{scope}:fix:{comment.id}",
            )
            connection.execute(
                """
                UPDATE ticket_state
                SET cap_extra = ?, workflow_state = 'fixing'
                WHERE repo = ? AND ticket_id = ?
                """,
                (cap_extra, ticket.repo.lower(), ticket.ticket_id),
            )
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, 'queued-fix')
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            )
            connection.commit()
        self.clear_attention(ticket.repo, ticket.ticket_id, "ready-after-cap")
        self._write_state_label(ticket, "fixing")
        return True, "queued-fix"

    def _over_cap(self, ticket: TrackedTicket, reported_round: int | None) -> bool:
        """A verdict round above the effective cap: no extra round covers it."""
        return reported_round is not None and reported_round > self._effective_cap(ticket)

    def _config_cap(self, ticket: TrackedTicket) -> int:
        return (
            ticket.factory.rounds_low
            if ticket.risk == "low"
            else ticket.factory.rounds_high
        )

    def _effective_cap(
        self,
        ticket: TrackedTicket,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """The ticket's review round cap: factory.md's for its risk, plus
        one per extra round Max approved (ticket_state.cap_extra)."""
        query = """
            SELECT cap_extra FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
        """
        key = (ticket.repo.lower(), ticket.ticket_id)
        if connection is None:
            with self._connect() as own:
                row = own.execute(query, key).fetchone()
        else:
            row = connection.execute(query, key).fetchone()
        return self._config_cap(ticket) + (int(row["cap_extra"]) if row else 0)

    def _wake_capped(self, ticket: TrackedTicket, comment: Comment) -> None:
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="capped",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=self._project_wake_detail(
                ticket,
                f"review round cap reached, {_comment_link(comment)}",
            ),
            head=ticket.current_head,
        )
