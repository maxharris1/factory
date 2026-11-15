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
from lib.dispatch.helpers import _comment_order, _matching_head, _queue_state, _scope
from lib.dispatch.types import Comment, RetryableStateError, ShipError, TrackedTicket
from lib.handoff import HandoffError, parse_comment
from lib.queue import JobRequest
from lib.update_check import UpdateClass


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
        request = JobRequest(
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
                instruction=(
                    f"The PR conflicts with {ticket.factory.base_branch}, so GitHub "
                    f"ran no CI on {head}: merge {ticket.factory.base_branch} in."
                ),
                kind="merge-only",
            ),
        )
        self._enqueue_or_flag(
            ticket,
            comment,
            request,
            dedupe_key=f"base-update:{_scope(ticket.repo, ticket.ticket_id)}:{head}",
        )
        return "ci-pending-base-update"

    def _settle_update(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reported_head: str,
    ) -> tuple[bool, str] | None:
        """The builder's ready after an update-from-main job (#130).

        A trivial update keeps the approval: the ticket goes back to the
        queue at the new head and the ready is recorded as
        approval-carried:<old>-><new>. A PR that still conflicts counts as
        a failed update. None sends the ready on through T2 to review (a
        non-trivial update) or to a CI fix (red CI), with the fresh ticket
        in _polled.
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
        if row["update_from_head"] is not None and _matching_head(
            reported_head, str(row["update_from_head"])
        ):
            return True, self._update_failed(**failed, why="the ready names the old head")
        try:
            fresh = self._fresh_ticket(ticket)
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
            return True, self._update_failed(**failed, why="the PR still conflicts")
        if fresh.mergeable is None:
            return False, "update-mergeable-pending"
        ci = fresh.ci.get(head)
        if ci is None or ci.status == "pending":
            return False, "ci-pending"
        if approved is None or ci.status != "success":
            return None
        verdict = self._classify_update(fresh, str(approved), head)
        if not verdict.trivial:
            if self.log is not None:
                self.log(
                    f"update re-review: {ticket.repo}#{ticket.ticket_id} at "
                    f"{head[:12]}: {verdict.reason}"
                )
            return None
        self._write_state_label(fresh, back_to)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ticket_state
                SET approved_head = ?, workflow_state = ?, update_failures = 0,
                    update_from_head = NULL
                WHERE repo = ? AND ticket_id = ? AND workflow_state = 'updating'
                """,
                (head, back_to, *key),
            )
            connection.commit()
        if self.log is not None:
            self.log(
                f"approval carried: {ticket.repo}#{ticket.ticket_id} "
                f"{str(approved)[:12]} -> {head[:12]} ({verdict.reason})"
            )
        if fresh.factory.ship.startswith("after:"):
            try:
                self._review_actions(fresh).await_approver(
                    fresh, head, fresh.factory.ship.removeprefix("after:")
                )
            except Exception as exc:
                self.record_poll_error(
                    ticket.repo, ticket.ticket_id, f"carried-preview:{type(exc).__name__}"
                )
        return True, f"approval-carried:{str(approved)[:12]}->{head[:12]}"

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
        """#130: act on the head of each repo's merge queue, once a poll.

        Only the oldest approval per repo moves: it merges, or gets an
        update-from-main job when it conflicts. Every other member waits,
        so two approved PRs never keep breaking each other.
        """
        heads: dict[str, sqlite3.Row] = {}
        for row in self._merge_queue_members():
            if str(row["repo"]) not in self._merged_repos:
                heads.setdefault(str(row["repo"]), row)
        for row in heads.values():
            try:
                self._advance_merge_queue(row)
            except Exception as exc:
                self.record_poll_error(
                    str(row["repo"]),
                    str(row["ticket_id"]),
                    f"merge-queue:{type(exc).__name__}",
                )

    def _advance_merge_queue(self, row: sqlite3.Row) -> None:
        polled = self._polled.get((str(row["repo"]), str(row["ticket_id"])))
        if polled is None:
            return
        ticket = self._fresh_ticket(polled)
        if ticket is None:
            return
        if ticket.merged:
            self.record_terminal(ticket.repo, ticket.ticket_id, "merged", ticket)
            return
        if ticket.closed:
            return
        if row["workflow_state"] == "updating":
            # An update job is out: its ready (T2) or failure (T6) moves on,
            # and so does a done job whose ready never came.
            unanswered = self._update_unanswered(ticket)
            if unanswered is not None:
                job_id, why = unanswered
                self._update_failed(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    source_comment_id=self._queue_comment(ticket, row).id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    project_bot=ticket.grok_bot,
                    back_to=_queue_state(ticket.factory.ship),
                    job_id=job_id,
                    why=why,
                )
            return
        if ticket.factory.finish_line != "merge":
            return
        if ticket.mergeable_state in ("dirty", "behind") and not self._base_update_out(ticket):
            self._queue_update(ticket, row)
            return
        head = ticket.current_head
        wait = self._queue_head_wait_reason(ticket, row, head)
        if wait is not None:
            self._log_queue_wait(ticket, head, wait)
            return
        self._merge_queue_head(ticket, row, head)

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
        if ci is None or ci.status != "success":
            return f"CI {'missing' if ci is None else ci.status} at {head[:12]}"
        approved = str(row["approved_head"])
        if not self._approval_covers(ticket, approved, head):
            return f"approval at {approved[:12]} does not cover {head[:12]}"
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
        """The update-from-main attempt a ready came from: the job that
        handed it off, else the latest job that has run and was queued no
        later than the ready's whole second. A job still queued (a retry not
        yet claimed) or cancelled unclaimed never ran, so it posted nothing."""
        with self._connect() as connection:
            job = connection.execute(
                """
                SELECT id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'update-from-main'
                  AND (
                    handoff_comment_id = ?
                    OR (state IN ('running', 'done', 'failed') AND created_at < ? + 1)
                  )
                ORDER BY handoff_comment_id = ? DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment.id,
                    comment.created_at,
                    comment.id,
                ),
            ).fetchone()
        return None if job is None else str(job["id"])

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
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                UPDATE ticket_state SET workflow_state = 'abandoned'
                WHERE repo = ? AND ticket_id = ? AND {_NOT_FINISHED}
                """,
                key,
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
        """A #255 base update for the current head is queued or running, so
        the merge queue must not queue a second update job for that head."""
        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM jobs
                    WHERE dedupe_key = ? AND state IN ('queued', 'running')
                    """,
                    (
                        f"base-update:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{ticket.current_head}",
                    ),
                ).fetchone()
                is not None
            )

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
            connection.execute(
                """
                UPDATE ticket_state
                SET workflow_state = 'updating', update_from_head = ?
                WHERE repo = ? AND ticket_id = ? AND workflow_state = ?
                """,
                (head, ticket.repo.lower(), ticket.ticket_id, row["workflow_state"]),
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
        job_id: str | None,
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
            if job_id is not None and row["update_failed_job"] == job_id:
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
            if job_id is not None and str(newest["id"]) != job_id:
                connection.commit()
                return "update-superseded"
            failures = int(row["update_failures"]) + 1
            if failures < UPDATE_FAILURES_TO_STOP:
                connection.execute(
                    """
                    UPDATE ticket_state
                    SET update_failures = ?, update_failed_job = ?, workflow_state = ?
                    WHERE repo = ? AND ticket_id = ?
                    """,
                    (failures, job_id, back_to, *key),
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
        query = """
            SELECT repo, ticket_id, workflow_state, approved_head, approved_at,
                   approve_comment_id, update_failures, update_from_head,
                   generation, review_round
            FROM ticket_state
            WHERE workflow_state IN ('merge-queue', 'updating', 'waiting-external')
              AND approved_head IS NOT NULL
              AND stuck = 0
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
                and self._trusted_reviewer(ticket, item, fields)
            ):
                return True
        return False
