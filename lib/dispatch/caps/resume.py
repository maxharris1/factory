
from __future__ import annotations

from lib.dispatch.helpers import _comment_link, _scope
from lib.dispatch.types import Comment, TrackedTicket
from lib.queue import JobRequest


class ResumeAfterCapMixin:
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
                        cap=self._config_cap(ticket) + cap_extra,
                        instruction=verdict,
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
