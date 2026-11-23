"""#1434: a trusted kind=rebase queues one rebase-only fix that spends no review round."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from lib.dispatch.decisions import Decision, record_decisions
from lib.dispatch.helpers import _comment_link, _comment_order, _scope
from lib.dispatch.retry import _harness_job_active, _newest_harness_job
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.handoff import parse_comment
from lib.job_detail import as_object
from lib.queue import JobRequest

LEAVES_MERGE_QUEUE = frozenset({"merge-queue", "waiting-external"})
REBASE_FROM = (
    frozenset({"review", "fixing", "awaiting-ok", "capped", "handed-off"}) | LEAVES_MERGE_QUEUE
)
_LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+?)\s*$")
_BACKTICKED = re.compile(r"`([^`]+)`")


def conflict_files(body: str) -> tuple[str, ...]:
    files = []
    for line in body.splitlines():
        item = _LIST_ITEM.match(line)
        if item is None:
            continue
        span = _BACKTICKED.search(item[1])
        path = span[1] if span else item[1]
        if not any(character.isspace() for character in path):
            files.append(path)
    return tuple(dict.fromkeys(files))


def rebase_fix_is_newest(connection: sqlite3.Connection, key: tuple[str, str]) -> bool:
    job = _newest_harness_job(connection, key)
    return (
        job is not None
        and job.request.action == "fix"
        and (as_object(job.request.detail) or {}).get("kind") == "rebase"
    )


@dataclass(frozen=True)
class RebaseStep:
    from_state: str
    from_head: str
    round: int


class RebaseMixin:
    def _rebase_conflicted(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> None:
        if not rebase_fix_is_newest(connection, (ticket.repo.lower(), ticket.ticket_id)):
            return
        files = ", ".join(conflict_files(parse_comment(comment.body).body))
        record_decisions(
            connection,
            ticket,
            comment.id,
            [
                Decision(
                    question=f"rebase-conflict:{comment.id}",
                    options=(),
                    choice=f"Conflict: waiting on Max; files: {files}",
                    answerer=comment.author,
                    answered_at=comment.created_at,
                    url=_comment_link(comment),
                    answer_id=comment.id,
                    position=0,
                )
            ],
        )

    def _superseded_by_rebase(self, ticket: TrackedTicket, comment: Comment) -> bool:
        with self._connect() as connection:
            sources = {
                row["source_comment_id"]
                for row in connection.execute(
                    """
                    SELECT source_comment_id FROM jobs
                    WHERE lower(repo) = ? AND ticket_id = ? AND dedupe_key LIKE 't-rebase:%'
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            }
        return any(
            _comment_order(comment) < _comment_order(rebase)
            for rebase in ticket.comments
            if rebase.id in sources
        )

    def _t_rebase(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
    ) -> tuple[bool, str]:
        if not (
            self._trusted(ticket, comment, fields, "grok-bot", role="orchestrator")
            or self._approver_go(comment, fields)
        ):
            return True, "rejected-rebase-author"
        ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            outcome, queued = self._rebase_in_transaction(connection, ticket, comment)
            if queued:
                connection.commit()
            else:
                connection.rollback()
        if queued:
            self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
            self._write_state_label(ticket, "fixing")
        return True, outcome

    def _rebase_in_transaction(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[str, bool]:
        dedupe_key = f"t-rebase:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
        if connection.execute(
            "SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone() is not None:
            return "queued-rebase", False
        head = ticket.current_head
        if head is None:
            return "rejected-rebase-no-pr", False
        key = (ticket.repo.lower(), ticket.ticket_id)
        row = connection.execute(
            """
            SELECT workflow_state, review_round, generation FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            key,
        ).fetchone()
        if row is None or row["workflow_state"] not in REBASE_FROM:
            return "rejected-rebase-state", False
        if row["workflow_state"] in LEAVES_MERGE_QUEUE:
            self._clear_approval(connection, ticket, "rebase")
        if _harness_job_active(connection, key):
            return "rejected-rebase-job-active", False
        review_round = int(row["review_round"])
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
                head=head,
                detail=self._job_detail(
                    ticket,
                    action="fix",
                    head=head,
                    round_number=review_round + 1,
                    cap=self._effective_cap(ticket, connection),
                    rebase_step=RebaseStep(row["workflow_state"], head, review_round),
                    connection=connection,
                ),
            ),
            dedupe_key=dedupe_key,
        )
        write_workflow_state(
            connection,
            ticket.repo,
            ticket.ticket_id,
            "fixing",
            source="rebase-queued",
            at=self.queue.now(),
        )
        record_decisions(
            connection,
            ticket,
            comment.id,
            [
                Decision(
                    question=f"rebase:{comment.id}",
                    options=(),
                    choice=f"Rebase onto origin/{ticket.factory.base_branch} queued",
                    answerer=comment.author,
                    answered_at=comment.created_at,
                    url=_comment_link(comment),
                    answer_id=comment.id,
                    position=0,
                )
            ],
        )
        return "queued-rebase", True
