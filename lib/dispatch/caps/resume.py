
from __future__ import annotations

from lib.dispatch.helpers import _comment_link, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.queue import JobRequest


class ResumeMixin:
    def _resume_after_cap(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        """A trusted go on a capped ticket: Max's OK for one more round (#105).

        One transaction raises cap_extra by 1, moves the ticket to fixing
        and queues the fix, keyed on the go comment so every go Max relays
        queues exactly one fix. None when the ticket is not capped. A go
        _may_lift_cap refuses wakes Grok once with go-not-from-approver and
        changes nothing (#122, #174).
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
            if not self._may_lift_cap(comment.author):
                connection.commit()
                self._wake_comment_problem(
                    ticket, comment, "go-not-from-approver", "go-not-from-approver"
                )
                return True, "go-not-from-approver"
            cap_extra = int(row["cap_extra"]) + 1
            capping = connection.execute(
                """
                SELECT comment_id FROM review_events
                WHERE repo = ? AND ticket_id = ? AND outcome = 'capped-review'
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            verdict = None
            if capping is not None:
                verdict = next(
                    (_comment_link(item) for item in ticket.comments if item.id == capping["comment_id"]),
                    f"comment:{capping['comment_id']}",
                )
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
                        cap=self._config_cap(ticket, connection) + cap_extra,
                        instruction=verdict,
                        resume=True,
                    ),
                ),
                dedupe_key=f"t1:{scope}:fix:{comment.id}",
            )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "fixing",
                source="cap-resume",
                at=self.queue.now(),
                also={"cap_extra": cap_extra},
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

    def _resume_after_blocked(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        """An approver's go on a blocked ticket queues a fresh review (#188).

        The review runs at the same head and round the BLOCKED named, keyed
        on the go. None when the ticket is not blocked. A go _may_lift_cap
        refuses wakes Grok once with go-not-from-approver and changes nothing.
        """
        scope = _scope(ticket.repo, ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND outcome = 'queued-blocked-review'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
            if prior is not None:
                connection.commit()
                self._write_state_label(ticket, "review")
                return True, str(prior["outcome"])
            row = connection.execute(
                """
                SELECT workflow_state, review_round, generation
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if row is None or row["workflow_state"] != "blocked":
                connection.commit()
                return None
            if not self._may_lift_cap(comment.author):
                connection.commit()
                self._wake_comment_problem(
                    ticket, comment, "go-not-from-approver", "go-not-from-approver"
                )
                return True, "go-not-from-approver"
            blocking = connection.execute(
                """
                SELECT comment_id FROM review_events
                WHERE repo = ? AND ticket_id = ? AND outcome = 'blocked-review'
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            links = [
                _comment_link(item)
                for item in ticket.comments
                if blocking is not None and item.id == blocking["comment_id"]
            ]
            self._enqueue_step(
                connection,
                ticket,
                comment,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="review",
                    recipient=ticket.factory.reviewer_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(row["generation"]),
                    head=ticket.current_head,
                    detail=self._job_detail(
                        ticket,
                        action="review",
                        head=ticket.current_head,
                        round_number=int(row["review_round"]) + 1,
                        instruction="\n".join([*links, _comment_link(comment)]),
                    ),
                ),
                dedupe_key=f"t1:{scope}:review:{comment.id}",
            )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "review",
                source="blocked-resume",
                at=self.queue.now(),
            )
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, 'queued-blocked-review')
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            )
            connection.commit()
        self._write_state_label(ticket, "review")
        return True, "queued-blocked-review"

    def _resume_unsatisfiable(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        if self._unsat_parked(ticket):
            if not self._may_lift_cap(comment.author):
                self._wake_comment_problem(
                    ticket, comment, "go-not-from-approver", "go-not-from-approver"
                )
                return True, "go-not-from-approver"
            return self._resume_paused(ticket, comment)
        with self._connect() as connection:
            resumed = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ?
                  AND source_comment_id = ? AND dedupe_key LIKE 't5:%'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id),
            ).fetchone()
        return None if resumed is None else self._resume_paused(ticket, comment)

    def _unsat_parked(self, ticket: TrackedTicket) -> bool:
        with self._connect() as connection:
            parked = connection.execute(
                """
                SELECT 1 FROM ticket_state
                JOIN review_events
                  ON review_events.repo = ticket_state.repo
                 AND review_events.ticket_id = ticket_state.ticket_id
                 AND review_events.comment_id = ticket_state.paused_question_id
                WHERE ticket_state.repo = ? AND ticket_state.ticket_id = ?
                  AND ticket_state.workflow_state = 'waiting-max'
                  AND review_events.outcome = 'waiting-max-unsat'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return parked is not None
