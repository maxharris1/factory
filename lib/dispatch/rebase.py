"""#1434: a trusted kind=rebase queues one rebase-only fix that spends no review round."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace

from lib.dispatch.constants import STACK_PARENT_CLOSED
from lib.dispatch.decisions import Decision, record_decisions
from lib.dispatch.helpers import (
    _base_mismatch,
    _comment_link,
    _comment_order,
    _detail_field,
    _detail_round_number,
    _matching_head,
    _scope,
)
from lib.dispatch.retry import _harness_job_active, _newest_harness_job
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import (
    Comment,
    ParentClosed,
    ParentFacts,
    ParentMerged,
    RetargetProof,
    TrackedTicket,
)
from lib.handoff import parse_comment
from lib.job_detail import as_object
from lib.queue import JobRequest
from lib.update_check import StackUnbounded

STACK_RETARGET_FAILED = "stack-retarget-failed"
LEAVES_MERGE_QUEUE = frozenset({"merge-queue", "waiting-external"})
REBASE_FROM = (
    frozenset({"review", "fixing", "awaiting-ok", "capped", "handed-off"}) | LEAVES_MERGE_QUEUE
)
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_ALONE = re.compile(r"`?([^`\s]+)`?")
_BACKTICKED = re.compile(r"`([^`\s]+)`")
_PATH_LIKE = re.compile(r"/|\w\.\w")
_NOT_A_FILE = re.compile(r"-.*|[0-9a-f]{7,40}|\w*HEAD[~^\d]*|.*[.,;:!?]")


def answers_rebase_review(connection: sqlite3.Connection, ticket: TrackedTicket, round_number: int) -> bool:
    row = connection.execute(
        """
        SELECT head, detail FROM jobs
        WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
        ORDER BY rowid DESC LIMIT 1
        """,
        (ticket.repo.lower(), ticket.ticket_id),
    ).fetchone()
    return (
        row is not None
        and _detail_round_number(row["detail"]) == round_number
        and _detail_field(row["detail"], "source") == "rebase"
        and _matching_head(str(row["head"] or ""), ticket.current_head) is not None
    )


def conflict_files(body: str, base: str = "main") -> tuple[str, ...]:
    refs = {base, f"origin/{base}"}

    def named(span: str) -> bool:
        return span not in refs and _NOT_A_FILE.fullmatch(span) is None

    files = []
    fenced = False
    in_run = False
    for line in body.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        listed = not fenced and _LIST_MARKER.match(line) is not None
        text = line.strip() if fenced else _LIST_MARKER.sub("", line, count=1).strip()
        alone = _ALONE.fullmatch(text)
        if alone and (
            fenced or listed or in_run or text.startswith("`") or _PATH_LIKE.search(text)
        ):
            in_run = named(alone[1])
            if in_run:
                files.append(alone[1])
        elif not fenced:
            files.extend(span for span in _BACKTICKED.findall(text) if named(span))
            in_run = text.endswith(":")
    return tuple(dict.fromkeys(files))


APPROVAL = ("approved_head", "approved_at", "approve_comment_id")


@dataclass(frozen=True)
class RebaseStep:
    from_state: str
    from_head: str
    round: int
    approved_head: str | None = None
    approved_at: float | None = None
    approve_comment_id: str | None = None
    onto_from: str | None = None
    parent_head: str | None = None

    @property
    def review_round(self) -> int:
        return self.round if self.from_state == "capped" else self.round + 1

    def block(self) -> dict[str, object]:
        return {name: value for name, value in asdict(self).items() if value is not None}

    def approval(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in APPROVAL if getattr(self, name) is not None}


def newest_rebase(connection: sqlite3.Connection, key: tuple[str, str]) -> RebaseStep | None:
    job = _newest_harness_job(connection, key)
    if job is None or job.request.action != "fix":
        return None
    detail = as_object(job.request.detail) or {}
    if detail.get("kind") != "rebase":
        return None
    return RebaseStep(**detail["rebase"])


def _on_base(ticket: TrackedTicket, base: str) -> TrackedTicket:
    return replace(
        ticket,
        factory=replace(ticket.factory, base_branch=base),
        factory_md=ticket.factory_md and replace(ticket.factory_md, base_branch=base),
        stack=None,
    )


class RebaseMixin:
    def _advance_stack(self, ticket: TrackedTicket) -> tuple[TrackedTicket, bool]:
        if self.feeder is None:
            return ticket, False
        try:
            facts = self.feeder.stacked_parent_facts(ticket)
            if isinstance(facts, ParentClosed) and not (ticket.merged or ticket.closed):
                self._hold_for_closed_parent(ticket, facts)
                return ticket, True
            if self._stack_parent_closed(ticket.repo, ticket.ticket_id):
                self.clear_attention(ticket.repo, ticket.ticket_id, STACK_PARENT_CLOSED)
            ticket, queued = self._land_stack_parent(ticket, facts)
            queued = queued or self._queue_pending_stack_rebase(ticket)
        except StackUnbounded as exc:
            with self._connect() as connection:
                self._flag_attention_once(
                    connection, ticket.repo, ticket.ticket_id, STACK_RETARGET_FAILED,
                    f"the child's own commits cannot be bounded: {exc}",
                )
            return ticket, True
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
            return ticket, True
        if queued:
            self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
            self._write_state_label(ticket, "fixing")
        return ticket, False

    def _stack_parent_closed(self, repo: str, ticket_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM needs_attention WHERE repo = ? AND ticket_id = ? AND reason = ?",
                (repo.lower(), ticket_id, STACK_PARENT_CLOSED),
            ).fetchone() is not None

    def _hold_for_closed_parent(self, ticket: TrackedTicket, parent: ParentClosed) -> None:
        detail = f"stack parent PR #{parent.pr} closed without merging; the child waits for a person"
        with self._connect() as connection:
            self._flag_attention_once(
                connection, ticket.repo, ticket.ticket_id, STACK_PARENT_CLOSED, detail
            )
        source = self._latest_trusted_comment(ticket)
        self.wake_grok_once(
            ticket,
            reason=STACK_PARENT_CLOSED,
            source_comment_id=source.id if source is not None else "",
            detail=self._project_wake_detail(ticket, detail),
            head=ticket.current_head,
            once_key=str(parent.pr),
        )

    def _land_stack_parent(
        self, ticket: TrackedTicket, facts: ParentFacts | None
    ) -> tuple[TrackedTicket, bool]:
        head = ticket.current_head
        if (
            not isinstance(facts, ParentMerged)
            or ticket.merged
            or ticket.closed
            or self.stack_bounds is None
            or head is None
            or ticket.pr_base not in (None, ticket.factory.base_branch, facts.into)
        ):
            return ticket, False
        proof = RetargetProof(
            self.stack_bounds(ticket.repo, head, facts.heads), head, facts.heads, facts.head
        )
        self.clear_attention(ticket.repo, ticket.ticket_id, STACK_RETARGET_FAILED)
        if ticket.pr_base != facts.into:
            self._review_actions(ticket).set_pull_base(ticket, facts.into)
        moved = _on_base(replace(ticket, pr_base=facts.into), facts.into)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            queued = self.feeder.move_stack_base(
                connection, ticket, facts.into, proof
            ) and self._stack_rebase_in_transaction(connection, moved, proof)
            connection.commit()
        return moved, queued

    def _queue_pending_stack_rebase(self, ticket: TrackedTicket) -> bool:
        head = ticket.current_head
        with self._connect() as connection:
            proof = self.feeder.pending_retarget(connection, ticket)
            if (
                proof is None
                or head is None
                or _base_mismatch(ticket) is not None
                or self._stack_rebase_row(connection, ticket) is None
            ):
                return False
        if proof.head != head:
            proof = replace(
                proof, onto_from=self.stack_bounds(ticket.repo, head, proof.parent_heads), head=head
            )
            self.clear_attention(ticket.repo, ticket.ticket_id, STACK_RETARGET_FAILED)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            queued = self._stack_rebase_in_transaction(connection, ticket, proof)
            connection.commit()
        return queued

    def _stack_rebase_row(
        self, connection: sqlite3.Connection, ticket: TrackedTicket
    ) -> sqlite3.Row | None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        row = connection.execute(
            """
            SELECT workflow_state, review_round, generation, approved_head, approved_at,
                approve_comment_id
            FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            key,
        ).fetchone()
        if (
            row is None
            or row["workflow_state"] not in REBASE_FROM
            or _harness_job_active(connection, key)
        ):
            return None
        return row

    def _stack_rebase_in_transaction(
        self, connection: sqlite3.Connection, ticket: TrackedTicket, proof: RetargetProof
    ) -> bool:
        if self.feeder.pending_retarget(connection, ticket) is None:
            return False
        row = self._stack_rebase_row(connection, ticket)
        if row is None:
            return False
        self.feeder.set_pending_retarget(connection, ticket, None)
        onto_from = proof.onto_from
        dedupe_key = f"t-retarget:{_scope(ticket.repo, ticket.ticket_id)}:{onto_from}"
        if connection.execute(
            "SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone() is not None:
            return False
        cleared = {}
        if row["workflow_state"] in LEAVES_MERGE_QUEUE:
            cleared = {name: row[name] for name in APPROVAL}
            self._clear_approval(connection, ticket, "retarget")
        self._queue_rebase_fix(
            connection,
            ticket,
            int(row["generation"]),
            RebaseStep(
                row["workflow_state"],
                proof.head,
                int(row["review_round"]),
                **cleared,
                onto_from=onto_from,
                parent_head=proof.parent_head,
            ),
            f"retarget:{onto_from}",
            lambda request: self.queue.enqueue_in_transaction(
                connection, request, dedupe_key=dedupe_key
            ),
        )
        if self.log is not None:
            self.log(
                f"retarget: {ticket.repo}#{ticket.ticket_id} rebase --onto "
                f"origin/{ticket.factory.base_branch} {onto_from[:12]} queued"
            )
        return True

    def _queue_rebase_fix(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        generation: int,
        step: RebaseStep,
        source_comment_id: str,
        enqueue: Callable[[JobRequest], object],
    ) -> None:
        enqueue(
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="fix",
                recipient=ticket.factory.builder_harness,
                source_comment_id=source_comment_id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                generation=generation,
                head=step.from_head,
                detail=self._job_detail(
                    ticket,
                    action="fix",
                    head=step.from_head,
                    round_number=step.round + 1,
                    cap=self._effective_cap(ticket, connection),
                    rebase_step=step,
                    connection=connection,
                ),
            )
        )
        write_workflow_state(
            connection,
            ticket.repo,
            ticket.ticket_id,
            "fixing",
            source="rebase-queued",
            at=self.queue.now(),
        )

    def _rebase_conflicted(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> None:
        if newest_rebase(connection, (ticket.repo.lower(), ticket.ticket_id)) is None:
            return
        body = parse_comment(comment.body).body
        files = ", ".join(conflict_files(body, ticket.factory.base_branch))
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
            retargeted = connection.execute(
                """
                SELECT max(created_at) FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND dedupe_key LIKE 't-retarget:%'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()[0]
        if retargeted is not None and comment.created_at < retargeted:
            return True
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
            SELECT workflow_state, review_round, generation, approved_head, approved_at,
                approve_comment_id
            FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            key,
        ).fetchone()
        if row is None or row["workflow_state"] not in REBASE_FROM:
            return "rejected-rebase-state", False
        cleared = {}
        if row["workflow_state"] in LEAVES_MERGE_QUEUE:
            cleared = {name: row[name] for name in APPROVAL}
            self._clear_approval(connection, ticket, "rebase")
        if _harness_job_active(connection, key):
            return "rejected-rebase-job-active", False
        self._queue_rebase_fix(
            connection,
            ticket,
            int(row["generation"]),
            RebaseStep(row["workflow_state"], head, int(row["review_round"]), **cleared),
            comment.id,
            lambda request: self._enqueue_step(
                connection, ticket, comment, request, dedupe_key=dedupe_key
            ),
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
