"""Merge queue and base updates: queue entry/exit, update jobs and their outcomes."""

from __future__ import annotations

import json
import sqlite3

from lib.dispatch.constants import (
    BASE_UPDATE_GRACE_SECONDS,
    UPDATE_FAILURES_TO_STOP,
    UPDATE_READY_GRACE_SECONDS,
    _NOT_FINISHED,
)
from lib.dispatch.errors_text import safe_ship_error
from lib.dispatch.helpers import (
    _base_update_key,
    _comment_order,
    _matching_head,
    _merge_only,
    _queue_state,
    _scope,
)
from lib.dispatch.retry import _harness_job_active
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import (
    BaseMismatch,
    Comment,
    RetryableStateError,
    ShipError,
    TrackedTicket,
)
from lib.handoff import HandoffError, parse_comment
from lib.job_state import (
    FAILED_UNSTARTED,
    JOB_EVENTS_TABLE,
    LINK_DROP,
    REQUEUED_LINK_DROP,
    Placement,
    has_job_events,
    lagging_end,
    never_started,
    read_placements,
)
from lib.queue import PRE_READY_FAILED, PRE_READY_PASSED, JobRequest
from lib.update_check import UpdateClass, UpdateScope


MERGES_BEFORE_OVERLAP_WAITS = 1

_MEMBER = """
    workflow_state IN ('merge-queue', 'updating', 'waiting-external')
    AND approved_head IS NOT NULL
    AND stuck = 0
"""


class MergeQueueMixin:
    def _queue_base_update(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
        review_round: int,
    ) -> str:
        """#255: GitHub runs no pull_request CI on a conflicting PR, so the
        ready at head would wait on CI forever. Once the grace has passed
        with nothing reported on head, queue one merge-only job there; the
        ready it posts gets CI and review as usual. It charges no round."""
        if self.queue.now() - comment.created_at < BASE_UPDATE_GRACE_SECONDS:
            return "ci-pending"
        self._enqueue_base_update(
            ticket,
            comment,
            head,
            review_round,
            f"The PR conflicts with {ticket.factory.base_branch}, so GitHub "
            f"ran no CI on {head}: merge {ticket.factory.base_branch} in.",
        )
        return "ci-pending-base-update"

    def _enqueue_base_update(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
        review_round: int,
        instruction: str,
    ) -> None:
        self._enqueue_or_flag(
            ticket,
            comment,
            self._base_update_request(ticket, comment, head, review_round, instruction),
            dedupe_key=_base_update_key(ticket, head),
        )

    def _base_update_request(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
        review_round: int,
        instruction: str,
    ) -> JobRequest:
        return JobRequest(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            action="fix",
            recipient=ticket.factory.builder_harness,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            generation=self._generation(ticket.repo, ticket.ticket_id),
            head=head,
            detail=self._job_detail(
                ticket,
                action="fix",
                head=head,
                round_number=review_round + 1,
                instruction=instruction,
                kind="merge-only",
            ),
        )

    def _settle_update(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_head: str,
    ) -> tuple[bool, str] | UpdateScope | None:
        """The builder's ready after an update-from-main job (#130).

        A trivial update keeps the approval: the ticket goes back to the
        queue at the new head and the ready is recorded as
        approval-carried:<old>-><new>. A PR that still conflicts counts as
        a failed update. None sends the ready on through T2 to review (a
        non-trivial update) or to a CI fix (red CI), with the fresh ticket
        in _polled. A non-trivial update shaped as a merge of base returns
        its UpdateScope instead, so the review covers only that (#260).
        """
        key = (ticket.repo.lower(), ticket.ticket_id)
        row = self._queue_row(ticket)
        approved = row["approved_head"]
        back_to = _queue_state(ticket.factory.ship)
        failed = {
            "repo": ticket.repo,
            "ticket_id": ticket.ticket_id,
            "source_comment_id": comment.id,
            "timeout_seconds": ticket.job_timeout_minutes * 60,
            "project_bot": ticket.grok_bot,
            "back_to": back_to,
            "job_id": self._update_job_before(ticket, comment),
        }

        def update_failed(why: str) -> str:
            if failed["job_id"] is None:
                if self.log is not None:
                    self.log(
                        f"update ready no started job produced: "
                        f"{ticket.repo}#{ticket.ticket_id}: {why}"
                    )
                return "update-unattributed"
            return self._update_failed(**failed, why=why)

        if row["update_from_head"] is not None and _matching_head(
            reported_head, str(row["update_from_head"])
        ):
            return True, update_failed("the ready names the old head")
        try:
            fresh = self._fresh_ticket(ticket)
        except BaseMismatch:
            return False, "base-mismatch"
        except Exception:
            return False, "pr-head-unavailable"
        fresh = fresh or ticket
        if fresh.merged:
            return True, "ready-on-merged-pr"
        if fresh.closed:
            return True, "ready-on-closed-pr"
        head = _matching_head(reported_head, fresh.current_head)
        if head is None:
            return True, "stale-ready-head"
        self._polled[key] = fresh
        if fresh.mergeable_state == "dirty":
            if failed["job_id"] is not None and self._merged_since_job(
                ticket, str(failed["job_id"])
            ):
                return True, self._conflicts_anew(ticket, back_to)
            return True, update_failed("the PR still conflicts")
        if fresh.mergeable is None:
            return False, "update-mergeable-pending"
        ci = fresh.ci.get(head)
        if ci is None or ci.status == "pending":
            return False, "ci-pending"
        if approved is None or not ci.clears:
            return None
        verdict = self._classify_update(fresh, str(approved), head)
        if not verdict.trivial:
            if self.log is not None:
                self.log(
                    f"update re-review: {ticket.repo}#{ticket.ticket_id} at "
                    f"{head[:12]}: {verdict.reason}"
                )
            return verdict.scope
        self._carry_approval(fresh, str(approved), head, verdict.reason, from_state="updating")
        return True, f"approval-carried:{str(approved)[:12]}->{head[:12]}"

    def _carry_approval(
        self, ticket: TrackedTicket, approved: str, head: str, reason: str, *, from_state: str
    ) -> None:
        back_to = _queue_state(ticket.factory.ship)
        self._write_state_label(ticket, back_to)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                back_to,
                source="update-settled",
                at=self.queue.now(),
                also={
                    "approved_head": head,
                    "update_failures": 0,
                    "update_from_head": None,
                },
                guard="workflow_state = ?",
                guard_args=(from_state,),
            )
            connection.commit()
        if self.log is not None:
            self.log(
                f"approval carried: {ticket.repo}#{ticket.ticket_id} "
                f"{approved[:12]} -> {head[:12]} ({reason})"
            )
        if ticket.factory.ship.startswith("after:"):
            try:
                self._review_actions(ticket).await_approver(
                    ticket, head, ticket.factory.ship.removeprefix("after:")
                )
            except Exception as exc:
                self.record_poll_error(
                    ticket.repo, ticket.ticket_id, f"carried-preview:{type(exc).__name__}"
                )

    def _merged_since_job(self, ticket: TrackedTicket, job_id: str) -> bool:
        with self._connect() as connection:
            job = connection.execute(
                "SELECT created_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        since = float(job["created_at"])
        return self._other_merges_since(ticket.repo, ticket.ticket_id, since) > 0

    def _other_merges_since(self, repo: str, ticket_id: str, since: float) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT ticket_id) FROM ticket_transitions
                    WHERE repo = ? AND ticket_id != ? AND to_state = 'merged'
                      AND at >= ?
                    """,
                    (repo.lower(), ticket_id, since),
                ).fetchone()[0]
            )

    def _conflicts_anew(self, ticket: TrackedTicket, back_to: str) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                back_to,
                source="update-conflicts-anew",
                at=self.queue.now(),
                also={"update_from_head": None},
                guard="workflow_state = 'updating'",
            )
            connection.commit()
        if self.log is not None:
            self.log(
                f"update conflicts anew: {ticket.repo}#{ticket.ticket_id}: "
                f"{ticket.factory.base_branch} moved after the update; one more update"
            )
        return "update-conflicts-anew"

    def _classify_update(
        self, ticket: TrackedTicket, approved_head: str, head: str
    ) -> UpdateClass:
        if self.update_classifier is None:
            return UpdateClass(False, "no update check is configured")
        try:
            return self.update_classifier(
                ticket.repo,
                ticket.factory.base_branch,
                approved_head,
                head,
                ticket.factory.shared_paths,
            )
        except Exception as exc:
            return UpdateClass(False, f"check failed: {type(exc).__name__}")

    def _queue_row(self, ticket: TrackedTicket) -> dict[str, object]:
        """The ticket's merge-queue columns (#130), all None when unset."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT approved_head, approved_at, approve_comment_id,
                       update_failures, update_from_head
                FROM ticket_state WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if row is None:
            return dict.fromkeys(
                (
                    "approved_head",
                    "approved_at",
                    "approve_comment_id",
                    "update_failures",
                    "update_from_head",
                )
            )
        return dict(row)

    def _leave_merge_queue(self, ticket: TrackedTicket, reason: str) -> None:
        """Clear the ticket's approval so ship waits for a new APPROVE, and
        cancel a queued update-from-main job (#130)."""
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ticket_state
                SET approved_head = NULL, approved_at = NULL,
                    approve_comment_id = NULL, update_failures = 0,
                    update_from_head = NULL
                WHERE repo = ? AND ticket_id = ?
                """,
                key,
            )
            for job in connection.execute(
                """
                SELECT id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
                  AND action = 'update-from-main'
                """,
                key,
            ).fetchall():
                self.queue.cancel(str(job["id"]), reason, connection=connection)
            connection.commit()

    def _enter_merge_queue(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
    ) -> tuple[bool, str]:
        """#130: a covered APPROVE on a finish_line: merge project. The
        ticket waits in merge-queue, watched every poll, until the queue
        step merges it or its PR closes. The done wake comes with the merge."""
        self._complete_ship_intent(
            ticket, comment.id, "merge-queue", approval=(head, comment)
        )
        self._write_state_label(ticket, "merge-queue")
        if self.log is not None:
            self.log(
                f"merge-queue: {ticket.repo}#{ticket.ticket_id} approved at "
                f"{head[:12]}; the poller merges it"
            )
        return True, "merge-queue"

    def _advance_merge_queues(self) -> None:
        for row in self._merge_queue_members():
            polled = self._polled.get((str(row["repo"]), str(row["ticket_id"])))
            if polled is not None and _failed_update_may_settle(row, polled):
                self._settle_failed_update_head(polled)
        queues: dict[str, list[sqlite3.Row]] = {}
        for row in self._merge_queue_members():
            if str(row["repo"]) not in self._merged_repos:
                queues.setdefault(str(row["repo"]), []).append(row)
        for head, *behind in queues.values():
            try:
                head_waits = self._advance_merge_queue(head)
            except Exception as exc:
                self._record_queue_error(head, exc)
                continue
            if head_waits:
                self._merge_first_ready(head, behind)

    def _record_queue_error(self, row: sqlite3.Row, exc: Exception) -> None:
        self.record_poll_error(
            str(row["repo"]), str(row["ticket_id"]), f"merge-queue:{type(exc).__name__}"
        )

    def _advance_merge_queue(self, row: sqlite3.Row) -> bool:
        polled = self._polled.get((str(row["repo"]), str(row["ticket_id"])))
        if polled is None:
            return False
        try:
            ticket = self._fresh_ticket(polled)
        except BaseMismatch:
            return False
        if ticket is None:
            return False
        self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
        if ticket.merged:
            self.record_terminal(ticket.repo, ticket.ticket_id, "merged", ticket)
            return False
        if ticket.closed:
            return False
        if row["workflow_state"] == "updating":
            # An update job is out: its ready (T2) or failure (T6) moves on,
            # and so does a done job whose ready never came.
            unanswered = self._update_unanswered(ticket)
            if unanswered is not None:
                job_id, why = unanswered
                counted = self._update_failed(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    source_comment_id=self._queue_comment(ticket, row).id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    project_bot=ticket.grok_bot,
                    back_to=_queue_state(ticket.factory.ship),
                    job_id=job_id,
                    why=why,
                )
                if counted == "update-failed-1":
                    self._settle_failed_update_head(ticket)
            return True
        if ticket.factory.finish_line != "merge":
            return False
        if ticket.mergeable_state in ("dirty", "behind") and not self._base_update_out(ticket):
            self._queue_update(ticket, row)
            return True
        head = ticket.current_head
        wait = self._queue_head_wait_reason(ticket, row, head)
        if wait is not None:
            self._log_queue_wait(ticket, head, wait)
            return _waits_on_update_or_ci(ticket)
        self._merge_queue_head(ticket, row, head)
        return False

    def _update_orphaned(self, ticket: TrackedTicket, row: sqlite3.Row, head: str) -> bool:
        """#660: no ready will settle a head its gone update job pushed.
        Pending CI waits, as TU's ready would."""
        if ticket.factory.finish_line != "merge":
            return False
        from_head = row["update_from_head"]
        if from_head is None or not int(row["update_failures"]):
            return False
        if _matching_head(head, str(from_head)) is not None or ticket.mergeable is not True:
            return False
        ci = ticket.ci.get(head)
        if ci is None or ci.status == "pending":
            return False
        with self._connect() as connection:
            if _harness_job_active(connection, (ticket.repo.lower(), ticket.ticket_id)):
                return False
        return not self._approval_covers(ticket, str(row["approved_head"]), head)

    def _settle_failed_update_head(self, polled: TrackedTicket) -> None:
        row = next(
            (
                row
                for row in self._merge_queue_members(polled.repo)
                if row["ticket_id"] == polled.ticket_id
            ),
            None,
        )
        if row is None:
            return
        try:
            ticket = self._fresh_ticket(polled)
            if ticket is None or ticket.merged or ticket.closed:
                return
            head = ticket.current_head
            if head is not None and self._update_orphaned(ticket, row, head):
                self._settle_orphaned_update(ticket, row, head)
        except BaseMismatch:
            return
        except Exception as exc:
            self._record_queue_error(row, exc)

    def _unmet_gate_instruction(self, ticket: TrackedTicket, row: sqlite3.Row, head: str) -> str | None:
        job_id = row["update_failed_job"]
        events: list[sqlite3.Row] = []
        with self._connect() as connection:
            if job_id is not None and has_job_events(connection):
                events = connection.execute(
                    f"""
                    SELECT event, detail FROM {JOB_EVENTS_TABLE}
                    WHERE job_id = ? AND event IN (?, ?) ORDER BY id
                    """,
                    (job_id, PRE_READY_FAILED, PRE_READY_PASSED),
                ).fetchall()
        failed = [str(detail) for event, detail in events if event == PRE_READY_FAILED]
        if failed:
            return f"The update job pushed {head} but its pre_ready check failed: {failed[-1]}"
        if ticket.factory.pre_ready is None or any(
            event == PRE_READY_PASSED and str(detail).endswith(f": at {head}")
            for event, detail in events
        ):
            return None
        return f"The update job pushed {head} but its pre_ready check never passed at that head"

    def _settle_orphaned_update(self, ticket: TrackedTicket, row: sqlite3.Row, head: str) -> bool:
        """True once the ticket left the queue, so another may merge ahead of it (#660)."""
        approved = str(row["approved_head"])
        source = self._queue_comment(ticket, row)
        generation = int(row["generation"])
        ci = ticket.ci[head]
        if ci.clears:
            unmet = self._unmet_gate_instruction(ticket, row, head)
            if unmet is not None:
                scope = _scope(ticket.repo, ticket.ticket_id)
                self._enqueue_or_flag(
                    ticket,
                    source,
                    JobRequest(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        action="fix",
                        recipient=ticket.factory.builder_harness,
                        source_comment_id=source.id,
                        timeout_seconds=ticket.job_timeout_minutes * 60,
                        generation=generation,
                        head=head,
                        detail=self._job_detail(
                            ticket,
                            action="fix",
                            head=head,
                            round_number=int(row["review_round"]) + 1,
                            instruction=unmet,
                        ),
                    ),
                    dedupe_key=f"t-update-gate:{scope}:{head}:g{generation}",
                )
                self._leave_merge_queue(ticket, "update-gate-unmet")
                self._set_workflow_state(ticket, "fixing")
                if self.log is not None:
                    self.log(
                        f"update pre_ready unmet: {ticket.repo}#{ticket.ticket_id} at "
                        f"{head[:12]}, its update job gone: fix queued"
                    )
                return True
            verdict = self._classify_update(ticket, approved, head)
            if verdict.trivial:
                self._carry_approval(
                    ticket, approved, head, verdict.reason, from_state=str(row["workflow_state"])
                )
                return False
            queued, _answered = self._queue_review(
                ticket, source, head=head, generation=generation, update=verdict.scope
            )
            if not queued:
                return False
            self._leave_merge_queue(ticket, "update-rereview")
            self._set_workflow_state(ticket, "review")
            if self.log is not None:
                self.log(
                    f"update re-review: {ticket.repo}#{ticket.ticket_id} at "
                    f"{head[:12]}, its update job gone: {verdict.reason}"
                )
            return True
        if self._waiting_on_ci_rerun(ticket, head, ci):
            return False
        _status, count = self._record_ci_result(ticket, head, ci.status)
        if self._mark_stuck_on_third_red(ticket, source, head, count):
            return True
        self._queue_ci_fix(
            ticket,
            source,
            head,
            ci,
            int(row["review_round"]),
            generation,
            dedupe_key=self._ci_fix_dedupe_key(ticket, head, generation),
        )
        self._leave_merge_queue(ticket, "update-ci-red")
        self._set_workflow_state(ticket, "fixing")
        if self.log is not None:
            self.log(
                f"update CI red: {ticket.repo}#{ticket.ticket_id} at {head[:12]}, "
                "its update job gone: CI fix queued"
            )
        return True

    def _merge_first_ready(self, head: sqlite3.Row, rows: list[sqlite3.Row]) -> None:
        waiting = self._polled.get((str(head["repo"]), str(head["ticket_id"])))
        merged_since_approval = self._other_merges_since(
            str(head["repo"]), str(head["ticket_id"]), float(head["approved_at"])
        )
        for row in rows:
            if row["workflow_state"] != "merge-queue":
                continue
            polled = self._polled.get((str(row["repo"]), str(row["ticket_id"])))
            if polled is None or polled.current_head is None:
                continue
            ci = polled.ci.get(polled.current_head)
            if ci is None or not ci.clears:
                continue
            overlap_waits = merged_since_approval >= MERGES_BEFORE_OVERLAP_WAITS
            if overlap_waits and self._waits_behind(head, waiting, polled):
                continue
            try:
                ticket = self._fresh_ticket(polled)
                if ticket is None or ticket.merged or ticket.closed:
                    continue
                if overlap_waits and self._waits_behind(head, waiting, ticket):
                    continue
                if ticket.factory.finish_line != "merge":
                    return
                current = ticket.current_head
                if current is None or self._queue_head_wait_reason(ticket, row, current):
                    continue
                if self.log is not None:
                    self.log(
                        f"merge-queue: {ticket.repo}#{ticket.ticket_id} merges ahead of "
                        f"#{head['ticket_id']}, which waits on its update or CI"
                    )
                self._merge_queue_head(ticket, row, current)
            except BaseMismatch:
                continue
            except Exception as exc:
                self._record_queue_error(row, exc)
            return

    def _waits_behind(
        self, head: sqlite3.Row, waiting: TrackedTicket | None, entry: TrackedTicket
    ) -> bool:
        if not _may_share_files(waiting, entry):
            return False
        self._log_queue_wait(
            entry,
            entry.current_head,
            f"behind #{head['ticket_id']}, which may share its files and was passed already",
        )
        return True

    def _queue_head_wait_reason(
        self, ticket: TrackedTicket, row: sqlite3.Row, head: str | None
    ) -> str | None:
        if ticket.mergeable_state in ("dirty", "behind"):
            return "base update out"
        if head is None:
            return "head unread"
        if ticket.mergeable is not True:
            return f"not mergeable ({ticket.mergeable_state or 'pending'})"
        ci = ticket.ci.get(head)
        if ci is None or not ci.clears:
            return f"CI {'missing' if ci is None else ci.status} at {head[:12]}"
        approved = str(row["approved_head"])
        if not self._approval_covers(ticket, approved, head):
            return f"approval at {approved[:12]} does not cover {head[:12]}"
        qa_wait = self._queue_qa_wait(ticket, row, head)
        if qa_wait is not None:
            return qa_wait
        ship = ticket.factory.ship
        if ship.startswith("after:"):
            # The approver's ok still ships it (ReviewShipper).
            return f"ship {ship} waits for the approver's ok"
        held = self._held(ticket, head)
        if held is not None:
            parts = [",".join(held.reasons)] if held.reasons else []
            if held.paths:
                parts.append("paths: " + ", ".join(held.paths[:3]))
            if held.unread is not None:
                parts.append(f"unread: {held.unread}")
            return f"held ({', '.join(parts)})"
        return None

    def _queue_qa_wait(self, ticket: TrackedTicket, row: sqlite3.Row, head: str) -> str | None:
        gated = self._approve_qa(
            ticket, self._queue_comment(ticket, row), head, int(row["review_round"] or 0) + 1
        )
        if gated is None:
            return None
        spent, outcome = gated
        if spent:
            self._leave_merge_queue(ticket, outcome)
        return f"QA {outcome} at {head[:12]}"

    def _log_queue_wait(self, ticket: TrackedTicket, head: str | None, reason: str) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        logged_head, reasons = self._logged_queue_waits.get(key, (None, set()))
        if logged_head != head:
            reasons = set()
        if reason in reasons:
            return
        reasons.add(reason)
        self._logged_queue_waits[key] = (head, reasons)
        if self.log is not None:
            self.log(
                f"merge-queue: {ticket.repo}#{ticket.ticket_id} waits at "
                f"{(head or 'no head')[:12]}: {reason}"
            )

    def _update_job_before(self, ticket: TrackedTicket, comment: Comment) -> str | None:
        """The update-from-main job that recorded this ready as its handoff,
        else the newest one that started (docs/SPEC.md, TU). Never by time:
        GitHub stamps comments in whole seconds."""
        with self._connect() as connection:
            jobs = connection.execute(
                """
                SELECT id, state, handoff_comment_id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'update-from-main'
                ORDER BY created_at DESC, id DESC
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
            for job in jobs:
                if job["handoff_comment_id"] == comment.id:
                    return str(job["id"])
            placements = read_placements(connection)
            ids = [str(job["id"]) for job in jobs]
            ran = _with_event(connection, ids, LINK_DROP, REQUEUED_LINK_DROP)
            unstarted = _with_event(connection, ids, FAILED_UNSTARTED) | {
                job_id for job_id in ids if lagging_end(connection, job_id) is not None
            }
        return next(
            (str(job["id"]) for job in jobs if _started(job, placements, ran, unstarted)),
            None,
        )

    def _update_unanswered(self, ticket: TrackedTicket) -> tuple[str, str] | None:
        """Why the latest update-from-main job counts as failed though it
        finished done (#130 Unit 6): its handoff is not a ready, its ready
        was handled and left the ticket updating, or no handoff showed on
        the thread within UPDATE_READY_GRACE_SECONDS. None while the job is
        out, failed (T6 counts it) or its ready is still to be read."""
        with self._connect() as connection:
            job = connection.execute(
                """
                SELECT id, state, handoff_comment_id, finished_at FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'update-from-main'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        if job is None or job["state"] != "done":
            return None
        handoff = next(
            (item for item in ticket.comments if item.id == job["handoff_comment_id"]),
            None,
        )
        if handoff is not None:
            try:
                kind = parse_comment(handoff.body).as_dict()["kind"]
            except HandoffError:
                kind = None
            if kind == "ready" and not self._is_handled(ticket, handoff.id):
                return None
        elif self.queue.now() - float(job["finished_at"] or 0) < UPDATE_READY_GRACE_SECONDS:
            return None
        return str(job["id"]), f"job {job['id']} finished with no ready that settles the update"

    def _merge_queue_head(
        self,
        ticket: TrackedTicket,
        row: sqlite3.Row,
        head: str,
    ) -> None:
        approve = self._queue_comment(ticket, row)
        try:
            self._ship_head(ticket, head)
        except ShipError as exc:
            if exc.retryable or type(exc).__name__ == "HeadMismatchError":
                # The head moved under the merge, or GitHub is not ready: the
                # next poll reads the PR again.
                self.record_poll_error(
                    ticket.repo, ticket.ticket_id, f"merge-queue-merge:{type(exc).__name__}"
                )
                return
            self._mark_stuck_and_wake(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                source_comment_id=approve.id,
                timeout_seconds=ticket.job_timeout_minutes * 60,
                reason="merge-failed",
                detail=self._project_wake_detail(
                    ticket,
                    f"the merge queue could not merge {head[:12]}: {safe_ship_error(exc)}",
                ),
            )
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enter_merged(connection, ticket.repo, ticket.ticket_id, "merge-queue")
            connection.commit()
        self._logged_queue_waits.pop((ticket.repo.lower(), ticket.ticket_id), None)
        if self.log is not None:
            self.log(f"merged: {ticket.repo}#{ticket.ticket_id} at {head[:12]} (merge queue)")
        try:
            self._write_state_label(ticket, "merged")
        except RetryableStateError:
            self.record_poll_error(ticket.repo, ticket.ticket_id, "merged-label")
        self._wake_done(ticket, approve, "merged")

    def _queue_comment(self, ticket: TrackedTicket, row: sqlite3.Row) -> Comment:
        """The APPROVE the queue entry came from, as wake and job source."""
        comment_id = str(row["approve_comment_id"])
        return next(
            (item for item in ticket.comments if item.id == comment_id),
            Comment(id=comment_id, author="", body="", created_at=0),
        )

    def _abandon_closed(self, ticket: TrackedTicket) -> None:
        """A watched PR closed without merging: the watch ends (#130)."""
        if self._emptied_by_running_job(ticket):
            return
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "abandoned",
                source="closed-unmerged",
                at=self.queue.now(),
                guard=_NOT_FINISHED,
            )
            for job in connection.execute(
                """
                SELECT id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
                  AND recipient != 'grok'
                """,
                key,
            ).fetchall():
                self.queue.cancel(str(job["id"]), "closed", connection=connection)
            connection.commit()
        if self.log is not None:
            self.log(f"abandoned: {ticket.repo}#{ticket.ticket_id} closed unmerged")
        try:
            self._write_state_label(ticket, "abandoned")
        except RetryableStateError:
            self.record_poll_error(ticket.repo, ticket.ticket_id, "abandoned-label")

    def _base_update_out(self, ticket: TrackedTicket) -> bool:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT detail FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'fix'
                  AND head = ? AND state IN ('queued', 'running')
                """,
                (ticket.repo.lower(), ticket.ticket_id, ticket.current_head),
            ).fetchall()
        return any(_merge_only(row["detail"]) for row in rows)

    def _queue_update(self, ticket: TrackedTicket, row: sqlite3.Row) -> None:
        """An update-from-main job for the conflicting queue head (#130)."""
        head = ticket.current_head
        if head is None:
            return
        scope = _scope(ticket.repo, ticket.ticket_id)
        generation = int(row["generation"])
        failures = int(row["update_failures"])
        source = self._queue_comment(ticket, row)
        request = JobRequest(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            action="update-from-main",
            recipient=ticket.factory.builder_harness,
            source_comment_id=source.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            generation=generation,
            head=head,
            detail=self._job_detail(
                ticket,
                action="update-from-main",
                head=head,
                round_number=int(row["review_round"]) + 1,
                instruction=(
                    f"The approved PR is {ticket.mergeable_state} against "
                    f"{ticket.factory.base_branch}: merge it in."
                ),
            ),
        )
        self._write_state_label(ticket, "updating")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.queue.enqueue_in_transaction(
                connection,
                request,
                dedupe_key=f"t-update:{scope}:{head}:u{failures}:g{generation}",
            )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "updating",
                source="update-queued",
                at=self.queue.now(),
                also={"update_from_head": head},
                guard="workflow_state = ?",
                guard_args=(row["workflow_state"],),
            )
            connection.commit()
        if self.log is not None:
            self.log(
                f"updating: {ticket.repo}#{ticket.ticket_id} conflicts with "
                f"{ticket.factory.base_branch}; update-from-main queued"
            )

    def _update_failed(
        self,
        *,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        timeout_seconds: int,
        project_bot: str | None,
        back_to: str,
        job_id: str,
        why: str,
    ) -> str:
        """Count one failed update job, once however many signals report it
        (its old-head ready, its failed exit). The second stops the ticket,
        cancels any queued update and asks Max (update-failed-2); the first
        puts it back in the queue, whose next pass queues one more update
        job."""
        key = (repo.lower(), ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT update_failures, update_from_head, update_failed_job
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ? AND workflow_state = 'updating'
                """,
                key,
            ).fetchone()
            if row is None:
                connection.commit()
                return "update-not-pending"
            if row["update_failed_job"] == job_id:
                connection.commit()
                return "update-failure-counted"
            newest = connection.execute(
                """
                SELECT id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'update-from-main'
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                key,
            ).fetchone()
            if str(newest["id"]) != job_id:
                connection.commit()
                return "update-superseded"
            failures = int(row["update_failures"]) + 1
            if failures < UPDATE_FAILURES_TO_STOP:
                write_workflow_state(
                    connection,
                    repo,
                    ticket_id,
                    back_to,
                    source="update-failed",
                    at=self.queue.now(),
                    also={"update_failures": failures, "update_failed_job": job_id},
                )
                connection.commit()
                if self.log is not None:
                    self.log(f"update failed once: {repo}#{ticket_id}: {why}")
                return "update-failed-1"
            connection.execute(
                """
                UPDATE ticket_state SET update_failures = ?, update_failed_job = ?
                WHERE repo = ? AND ticket_id = ?
                """,
                (failures, job_id, *key),
            )
            for job in connection.execute(
                """
                SELECT id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND state = 'queued'
                  AND action = 'update-from-main'
                """,
                key,
            ).fetchall():
                self.queue.cancel(str(job["id"]), "update-failed-2", connection=connection)
            self._set_stuck_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                reason="update-failed-2",
            )
            self._ensure_stuck_wake_in_transaction(
                connection,
                repo=repo,
                ticket_id=ticket_id,
                detail=json.dumps(
                    {
                        "project_bot": project_bot,
                        "job_id": job_id,
                        "detail": (
                            "update-from-main failed twice on "
                            f"{str(row['update_from_head'] or '')[:12]} ({why}): ask Max"
                        ),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            connection.commit()
        return "stuck-update-failed-2"

    def _merge_turn(self, ticket: TrackedTicket) -> bool:
        """Whether a finish_line: merge ticket may merge now (#130): it heads
        its repo's merge queue, GitHub says it merges cleanly, and nothing in
        that repo merged this poll. Every merge of a watched ticket passes
        this one gate; a PR that conflicts, is behind or is still being
        checked waits for the queue's update instead of failing its ship.
        During the per-ticket pass it defers, so the queue holds every
        approval this poll records before one is chosen."""
        if ticket.factory.finish_line != "merge":
            return True
        if self._ticket_pass:
            self._deferred_ships[(ticket.repo.lower(), ticket.ticket_id)] = ticket
            return False
        if ticket.repo.lower() in self._merged_repos:
            return False
        if ticket.mergeable is not True or ticket.mergeable_state in ("dirty", "behind"):
            return False
        members = self._merge_queue_members(ticket.repo)
        return not members or members[0]["ticket_id"] == ticket.ticket_id

    def _queue_owns_base_updates(self, repo: str, ticket_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                f"SELECT 1 FROM ticket_state WHERE repo = ? AND ticket_id = ? AND {_MEMBER}",
                (repo.lower(), ticket_id),
            ).fetchone() is not None

    def _watched(self, ticket: TrackedTicket) -> bool:
        """An approved ticket the merge queue still watches (#130), an
        update paused on a builder's question included."""
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT 1 FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                  AND workflow_state IN (
                    'merge-queue', 'updating', 'waiting-external', 'waiting-max'
                  )
                  AND approved_head IS NOT NULL
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone() is not None

    def _merge_queue_members(self, repo: str | None = None) -> list[sqlite3.Row]:
        """Approved tickets still watched, oldest approval first (#130).

        merge-queue and updating tickets, plus waiting-external ones whose
        APPROVE recorded an approval (ship: after: with finish_line: merge).
        A stuck ticket leaves the queue, so the next one moves up.
        """
        query = f"""
            SELECT repo, ticket_id, workflow_state, approved_head, approved_at,
                   approve_comment_id, update_failures, update_from_head,
                   update_failed_job, generation, review_round
            FROM ticket_state
            WHERE {_MEMBER}
        """
        args: tuple[str, ...] = ()
        if repo is not None:
            query += " AND repo = ?"
            args = (repo.lower(),)
        query += " ORDER BY repo, approved_at, ticket_id"
        with self._connect() as connection:
            return connection.execute(query, args).fetchall()

    def _superseded_by_revise(
        self,
        ticket: TrackedTicket,
        approve: Comment,
        reported_round: int | None,
    ) -> bool:
        """Issue #86: a held APPROVE can wait long enough for the reviewer
        to post a REVISE for the same round at the current head. That
        REVISE replaces the APPROVE; T3 acts on it in the same pass."""
        for item in ticket.comments:
            if _comment_order(item) <= _comment_order(approve):
                continue
            try:
                fields = parse_comment(item.body).as_dict()
            except HandoffError:
                continue
            round_field = fields.get("round")
            if (
                fields["kind"] == "verdict"
                and fields.get("verdict") == "REVISE"
                and round_field is not None
                and int(round_field.split("/", 1)[0]) == reported_round
                and _matching_head(fields.get("head") or "", ticket.current_head)
                is not None
                and self._counted_verdict(ticket, item, fields)
            ):
                return True
        return False


def _may_share_files(head: TrackedTicket | None, entry: TrackedTicket) -> bool:
    if head is None or head.changed_files is None or entry.changed_files is None:
        return True
    return not set(head.changed_files).isdisjoint(entry.changed_files)


def _failed_update_may_settle(row: sqlite3.Row, polled: TrackedTicket) -> bool:
    from_head = row["update_from_head"]
    if from_head is None or not int(row["update_failures"]) or row["workflow_state"] == "updating":
        return False
    head = polled.current_head
    if head is None or _matching_head(head, str(from_head)) is not None:
        return False
    ci = polled.ci.get(head)
    return ci is not None and ci.status != "pending"


def _waits_on_update_or_ci(ticket: TrackedTicket) -> bool:
    if ticket.mergeable_state in ("dirty", "behind"):
        return True
    head = ticket.current_head
    if head is None:
        return False
    ci = ticket.ci.get(head)
    return ci is None or ci.status == "pending"


def _started(
    job: sqlite3.Row,
    placements: dict[str, Placement],
    ran: set[str],
    unstarted: set[str],
) -> bool:
    job_id = str(job["id"])
    if job_id in ran:
        return True
    placement = placements.get(job_id)
    if placement is not None:
        return not never_started(placement)
    return job["state"] in ("running", "done", "failed") and job_id not in unstarted


def _with_event(connection: sqlite3.Connection, job_ids: list[str], *events: str) -> set[str]:
    if not job_ids or not has_job_events(connection):
        return set()
    jobs = ", ".join("?" for _ in job_ids)
    kinds = ", ".join("?" for _ in events)
    return {
        str(row[0])
        for row in connection.execute(
            f"SELECT job_id FROM {JOB_EVENTS_TABLE} "
            f"WHERE event IN ({kinds}) AND job_id IN ({jobs})",
            (*events, *job_ids),
        )
    }
