
from __future__ import annotations

from collections.abc import Mapping

from lib.dispatch.constants import TERMINAL_STATES, WATCHED_STATES
from lib.dispatch.helpers import _route_go, _scope
from lib.dispatch.retry import _harness_job_active
from lib.dispatch.types import Comment, TrackedTicket
from lib.queue import JobRequest


class T1Mixin:
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
        state = fields.get("state")
        action = {"planning": "plan", "building": "build"}.get(state)
        if action is not None:
            routed = _route_go(ticket, fields)
            if routed is None:
                return True, "rejected-go-same-family"
            ticket = routed
            resumed = (
                (self._fix_unsat_revise(ticket, comment) if state == "building" else None)
                or self._resume_for_max(ticket, comment)
                or self._resume_after_blocked(ticket, comment)
                or self._resume_after_cap(ticket, comment, building=state == "building")
            )
            if resumed is not None:
                self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
                return resumed
        if current == "handed-off":
            # #108, #118, #1197: inside the cap only a building go on a
            # ship: handoff ticket reopens it (_resume_after_cap); any other
            # go changes nothing, and the builder's ready still re-enters.
            return True, "go-after-handoff"
        if not trusted:
            return True, "rejected-go-author"
        if action == "plan":
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                replanned = self._unlanded_plan(connection, ticket) is not None
                if replanned:
                    self._replan_in_transaction(connection, ticket, comment)
                connection.commit()
            if replanned:
                self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
                self._write_state_label(ticket, "planning")
                return True, "queued-plan"
        generation = self._generation(ticket.repo, ticket.ticket_id)
        if action is None:
            self.queue.enqueue(
                self._grok_request(
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
                    )
                ),
                dedupe_key=(
                    f"t1-clarify:{_scope(ticket.repo, ticket.ticket_id)}:"
                    f"g{generation}"
                ),
            )
            return True, "queued-go-clarification"
        once = self._go_once(ticket, comment, action, generation, current)
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
        self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
        self._set_workflow_state(ticket, state)
        return True, f"answered-{action}" if answered else f"queued-{action}"

    def _go_once(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        action: str,
        generation: int,
        current: str | None,
    ) -> str:
        prefix = f"t1:{_scope(ticket.repo, ticket.ticket_id)}:{action}:"
        with self._connect() as connection:
            held = {
                row["dedupe_key"]: row
                for row in connection.execute(
                    "SELECT dedupe_key, state, source_comment_id FROM jobs"
                    " WHERE dedupe_key IN (?, ?)",
                    (prefix + comment.id, f"{prefix}g{generation}"),
                )
            }
            if prefix + comment.id in held:
                return comment.id
            spent = held.get(f"{prefix}g{generation}")
            if (
                current not in TERMINAL_STATES
                and spent is not None
                and spent["state"] not in ("queued", "running")
                and spent["source_comment_id"] != comment.id
                and not _harness_job_active(connection, (ticket.repo.lower(), ticket.ticket_id))
            ):
                return comment.id
        return f"g{generation}"
