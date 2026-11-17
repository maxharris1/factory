"""QA runs (#763): decide each head's QA scope, queue the qa jobs, keep qa_runs.

QaMixin is the only writer of qa_runs. Every method returns before any read or
write when the ticket's factory.md has no qa block, so a project with QA off
behaves as before. decide_scope is pure: the caller gathers the facts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from lib.dispatch.helpers import _scope
from lib.dispatch.types import Comment, TrackedTicket
from lib.factory_md import QaConfig
from lib.handoff import HandoffError, parse_comment
from lib.queue import QA_RECIPIENT, Job, JobRequest

OPEN_STATES = ("queued", "running")


@dataclass(frozen=True)
class ScopeInput:
    live_ids: tuple[str, ...] | None


@dataclass(frozen=True)
class Run:
    scope: str
    scenario_ids: tuple[str, ...]


@dataclass(frozen=True)
class NoRun:
    reason: str
    result: str | None = None


def decide_scope(facts: ScopeInput) -> Run | NoRun:
    return Run("full", facts.live_ids or ())


def _arms(qa: QaConfig) -> tuple[tuple[str, str], ...]:
    primary, *others = qa.runners
    arms = [(primary.name, "primary")]
    if qa.shootout:
        arms.extend((runner.name, "shootout") for runner in others)
    return tuple(arms)


class QaMixin:
    def _queue_qa(
        self, ticket: TrackedTicket, comment: Comment, *, head: str | None, review_round: int
    ) -> None:
        """Decide QA for a head the dispatcher just queued a review for.

        A head is decided once, so a retry or a resume at the same head
        queues no second run. QA never changes the review path, so an error
        here is recorded and swallowed.
        """
        qa = ticket.factory.qa
        if qa is None or head is None:
            return
        try:
            self._decide_qa(ticket, qa, comment, head, review_round)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)

    def _decide_qa(
        self,
        ticket: TrackedTicket,
        qa: QaConfig,
        comment: Comment,
        head: str,
        review_round: int,
    ) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            decided = connection.execute(
                "SELECT 1 FROM qa_runs WHERE repo = ? AND ticket_id = ? AND head = ? LIMIT 1",
                (*key, head),
            ).fetchone()
        if decided is not None:
            return
        decision = decide_scope(ScopeInput(live_ids=self._qa_live_ids(ticket)))
        generation = self._generation(ticket.repo, ticket.ticket_id)
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if isinstance(decision, NoRun):
                connection.execute(
                    """
                    INSERT INTO qa_runs (
                        run_id, repo, ticket_id, head, round, scope, runner, arm,
                        state, result, reason, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, 'none', ?, 'primary', 'reported', ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex, *key, head, review_round, qa.runners[0].name,
                        decision.result, decision.reason, now, now,
                    ),
                )
            else:
                for runner, arm in _arms(qa):
                    self._queue_qa_run(
                        connection, ticket, qa, comment, head, review_round, generation,
                        decision, runner, arm,
                    )
            connection.commit()

    def _queue_qa_run(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        qa: QaConfig,
        comment: Comment,
        head: str,
        review_round: int,
        generation: int,
        run: Run,
        runner: str,
        arm: str,
    ) -> None:
        run_id = uuid.uuid4().hex
        job = self.queue.enqueue_in_transaction(
            connection,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="qa",
                recipient=QA_RECIPIENT,
                source_comment_id=comment.id,
                timeout_seconds=qa.timeout_minutes * 60,
                generation=generation,
                head=head,
                detail=json.dumps(
                    {
                        "action": "qa",
                        "head": head,
                        "scope": run.scope,
                        "runner": runner,
                        "arm": arm,
                        "scenarios": list(run.scenario_ids),
                        "run_id": run_id,
                        "pr_number": ticket.pr_number,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
            dedupe_key=f"qa:{_scope(ticket.repo, ticket.ticket_id)}:{run.scope}:{head}:{runner}:{arm}",
        )
        connection.execute(
            """
            INSERT INTO qa_runs (
                run_id, repo, ticket_id, head, round, scope, runner, arm,
                job_id, state, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                run_id, ticket.repo.lower(), ticket.ticket_id, head, review_round,
                run.scope, runner, arm, job.id, self.queue.now(),
            ),
        )

    def _qa_live_ids(self, ticket: TrackedTicket) -> tuple[str, ...] | None:
        # Imported here: lib.evidence imports lib.dispatcher at module level.
        from lib.evidence.scenarios import InvalidSection, live_scenarios

        found = live_scenarios(
            [comment for comment in ticket.comments if self._trusted_builder_post(ticket, comment)]
        )
        if isinstance(found, InvalidSection) or not found.scenarios:
            return None
        return tuple(scenario.id for scenario in found.scenarios)

    def _trusted_builder_post(self, ticket: TrackedTicket, comment: Comment) -> bool:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            return False
        return fields["kind"] in ("plan", "ready") and self._trusted(
            ticket, comment, fields, f"{ticket.factory.builder_harness}-bot", role="builder"
        )

    def _qa_job_failed(self, job: Job) -> None:
        """T6 for a qa job: cancel its run, never retry, never mark stuck."""
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE qa_runs SET state = 'cancelled', reason = 'job-failed', finished_at = ?
                WHERE job_id = ? AND state IN ('queued', 'running')
                """,
                (now, job.id),
            )
            connection.commit()
        self._record_job_event_once(job, "qa-no-retry", "a qa job is never retried")
        self.queue.mark_failure_handled(job.id)
