
from __future__ import annotations

import sqlite3
from typing import Literal

from lib.dispatch.bookkeeping import locked_write
from lib.dispatch.constants import UNANNOUNCED_AFTER_CAP
from lib.dispatch.decisions import record_decisions
from lib.dispatch.helpers import _comment_link, _scope, grant_open, red_checks
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, RoundGrant, TrackedTicket
from lib.dispatch.verdict.repeat import OVERRULE, _held_kind, _held_row
from lib.dispatch.verdict.shipped import _approved_round
from lib.queue import JobRequest


class ResumeMixin:
    def _overrule_stop(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            replayed, state = _overrule_state(connection, key, comment)
        if replayed:
            stopped = int(state["review_round"]) + 1
        else:
            held = self._held_stop(ticket)
            if held is None or held[0] == "unsat" or not self._may_lift_cap(comment.author):
                return None
            stop, verdict = held
            decision = self._stop_decision(ticket, verdict, stop, comment, "Another round")
            if not decision.choice.lower().startswith(OVERRULE.name.lower()):
                return None

            def overrule(connection: sqlite3.Connection) -> int | None:
                replayed, state = _overrule_state(connection, key, comment)
                if replayed:
                    return int(state["review_round"]) + 1
                round_number = int(state["review_round"])
                row = _held_row(connection, ticket)
                if row is None or (row["comment_id"], _held_kind(row)) != (verdict.id, stop):
                    return None
                record_decisions(connection, ticket, verdict.id, [decision])
                connection.execute(
                    """
                    INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                    VALUES (?, ?, ?, 'overruled-finding')
                    """,
                    (*key, comment.id),
                )
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
                        generation=int(state["generation"]),
                        head=ticket.current_head,
                        detail=self._job_detail(
                            ticket,
                            action="review",
                            head=ticket.current_head,
                            round_number=round_number,
                            instruction="\n".join([_comment_link(verdict), _comment_link(comment)]),
                            connection=connection,
                        ),
                    ),
                    dedupe_key=f"t1:{_scope(ticket.repo, ticket.ticket_id)}:review:{comment.id}",
                )
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    "review",
                    source="overrule-review",
                    at=self.queue.now(),
                    also={
                        "review_round": round_number - 1,
                        "paused_action": None,
                        "paused_recipient": None,
                        "paused_head": None,
                        "paused_generation": None,
                        "paused_question_id": None,
                    },
                )
                return round_number

            stopped = locked_write(self._connect, overrule, site="_overrule_stop", ledger=self.lock_ledger)
            if stopped is None:
                return None
        self._write_state_label(ticket, "review")
        self._queue_qa(ticket, comment, head=ticket.current_head, review_round=stopped)
        return True, "overruled-finding"

    def _resume_after_cap(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        *,
        building: bool = False,
    ) -> tuple[bool, str] | None:
        """A trusted go on a capped ticket: Max's OK for one more round (#105).

        One transaction raises cap_extra by 1, moves the ticket to fixing
        and queues the fix, keyed on the go comment so every go Max relays
        queues exactly one fix. A handed-off ticket whose next round T2
        would flag ready-after-cap resumes the same way (#118): the approved
        round counts as completed and the approval is cleared, as a T2
        re-entry does. Inside the cap a building go on a ship: handoff
        ticket reopens it the same way, without cap_extra (#1197). None
        when none of these holds. A go _may_lift_cap refuses wakes Grok
        once with go-not-from-approver and changes nothing (#122, #174).
        """
        lifted = self._apply_round_grant(ticket, RoundGrant.approver(comment, building=building))
        if lifted is None:
            return None
        outcome, queued, reopened = lifted
        if outcome == "go-not-from-approver":
            self._wake_comment_problem(ticket, comment, outcome, outcome)
            return True, outcome
        self._after_lift(ticket, queued, reopened)
        return True, outcome

    def _reopen_on_reviews(self, ticket: TrackedTicket) -> None:
        if ticket.factory.ship != "handoff" or not ticket.reviews:
            return
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workflow_state FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            replayed = {
                str(item["comment_id"])
                for item in connection.execute(
                    "SELECT comment_id FROM review_events"
                    " WHERE repo = ? AND ticket_id = ? AND comment_id LIKE 'review:%'",
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            }
            unlabelled = [
                str(item["comment_id"])
                for item in connection.execute(
                    """
                    SELECT comment_id FROM review_events
                    WHERE repo = ? AND ticket_id = ? AND comment_id LIKE 'review:%'
                      AND outcome = 'queued-fix'
                      AND NOT EXISTS (
                        SELECT 1 FROM handled_comments
                        WHERE handled_comments.repo = review_events.repo
                          AND handled_comments.ticket_id = review_events.ticket_id
                          AND handled_comments.comment_id = review_events.comment_id
                      )
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            ]
        for comment_id in unlabelled:
            self._after_lift(ticket, queued=False, reopened=False)
            self._mark_handled(ticket, comment_id, "queued-fix")
        if row is None or row["workflow_state"] != "handed-off":
            return
        approved_at = self._shipped_approval_at(ticket)
        if approved_at is None:
            return
        reviewers = {login.casefold() for login in ticket.factory.human_reviewers}
        for review in sorted(ticket.reviews, key=lambda item: (item.submitted_at, int(item.id))):
            if (
                review.state != "CHANGES_REQUESTED"
                or review.submitted_at <= approved_at
                or f"review:{review.id}" in replayed
                or not (self.approvals.may_approve(review.author) or review.author.casefold() in reviewers)
            ):
                continue
            comment = Comment(
                id=f"review:{review.id}",
                author=review.author,
                body="",
                created_at=review.submitted_at,
                url=review.url,
            )
            lifted = self._apply_round_grant(ticket, RoundGrant.approver(comment, building=True), trusted_review=True)
            if lifted is not None and lifted[1]:
                self._after_lift(ticket, queued=True, reopened=True)
                self._mark_handled(ticket, comment.id, "queued-fix")
                return

    def _after_lift(self, ticket: TrackedTicket, queued: bool, reopened: bool) -> None:
        if self._review_context(ticket)[0] != "fixing":
            return
        if queued:
            self.clear_attention(ticket.repo, ticket.ticket_id, "ready-after-cap")
            self.clear_attention(ticket.repo, ticket.ticket_id, UNANNOUNCED_AFTER_CAP)
        if reopened:
            self._reopen_linear(ticket)
        self._write_state_label(ticket, "fixing")

    def _apply_round_grant(
        self,
        ticket: TrackedTicket,
        grant: RoundGrant,
        *,
        trusted_review: bool = False,
    ) -> tuple[str, bool, bool] | None:
        scope = _scope(ticket.repo, ticket.ticket_id)
        ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT outcome FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, grant.key),
            ).fetchone()
            if prior is not None:
                connection.commit()
                return str(prior["outcome"]), False, False
            row = connection.execute(
                """
                SELECT workflow_state, review_round, cap_extra, generation
                FROM ticket_state
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if row is None or row["workflow_state"] not in ("capped", "handed-off"):
                connection.commit()
                return None
            handed_off = row["workflow_state"] == "handed-off"
            if trusted_review and not handed_off:
                connection.commit()
                return None
            completed = int(row["review_round"])
            reopen = False
            if handed_off:
                completed = _approved_round(self._shipped_approval(ticket), completed)
                if completed + 1 <= self._effective_cap(ticket, connection):
                    if not grant.building or ticket.factory.ship != "handoff":
                        connection.commit()
                        return None
                    reopen = True
                elif trusted_review:
                    connection.execute(
                        """
                        INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                        VALUES (?, ?, ?, 'changes-requested-after-cap')
                        """,
                        (ticket.repo.lower(), ticket.ticket_id, grant.key),
                    )
                    self._flag_attention_once(
                        connection,
                        ticket.repo,
                        ticket.ticket_id,
                        "changes-requested-after-cap",
                        _comment_link(grant.trigger),
                    )
                    connection.commit()
                    return "changes-requested-after-cap", False, False
            if (
                grant.released_by == "approver"
                and not trusted_review
                and not self._may_lift_cap(grant.trigger.author)
            ):
                connection.commit()
                return "go-not-from-approver", False, False
            cap_extra = int(row["cap_extra"]) + (0 if reopen else 1)
            capping = connection.execute(
                """
                SELECT comment_id FROM review_events
                WHERE repo = ? AND ticket_id = ? AND outcome = 'capped-review'
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
            if grant.released_by == "engine" and (
                handed_off or not grant_open(connection, ticket, grant, capping and capping["comment_id"])
            ):
                connection.commit()
                return None
            verdict = _comment_link(grant.trigger) if handed_off else None
            if capping is not None and not handed_off:
                stop = next((item for item in ticket.comments if item.id == capping["comment_id"]), None)
                verdict = _comment_link(stop) if stop is not None else f"comment:{capping['comment_id']}"
                if stop is not None and grant.released_by == "approver":
                    record_decisions(
                        connection,
                        ticket,
                        stop.id,
                        [
                            self._stop_decision(
                                ticket, stop, self._capped_stop(ticket, stop), grant.trigger, "Another round"
                            )
                        ],
                    )
            self._enqueue_step(
                connection,
                ticket,
                grant.trigger,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="fix",
                    recipient=ticket.factory.builder_harness,
                    source_comment_id=grant.trigger.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(row["generation"]),
                    head=ticket.current_head,
                    detail=self._job_detail(
                        ticket,
                        action="fix",
                        head=ticket.current_head,
                        round_number=completed + 1,
                        cap=self._config_cap(ticket, connection) + cap_extra,
                        instruction=verdict,
                        resume=True,
                        kind="reopen" if reopen else None,
                        source="review" if trusted_review else None,
                        ci_failure=red_checks(ticket, ticket.current_head),
                        connection=connection,
                    ),
                ),
                dedupe_key=f"t1:{scope}:fix:{grant.key}",
            )
            cleared = {"cap_extra": cap_extra} | (
                {
                    "review_round": completed,
                    "approved_head": None,
                    "approved_at": None,
                    "approve_comment_id": None,
                    "update_failures": 0,
                    "update_from_head": None,
                }
                if handed_off
                else {}
            )
            if reopen:
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    "fixing",
                    source="handoff-reopen",
                    at=self.queue.now(),
                    also=cleared | {"reopen_hold": _reopen_hold(connection, ticket)},
                )
            else:
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    "fixing",
                    source="cap-resume",
                    at=self.queue.now(),
                    also=cleared,
                )
            connection.execute(
                """
                INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                VALUES (?, ?, ?, 'queued-fix')
                """,
                (ticket.repo.lower(), ticket.ticket_id, grant.key),
            )
            connection.commit()
        return "queued-fix", True, reopen

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
        outcome = self._queue_blocked_review(ticket, comment)
        if outcome is None:
            return None
        if outcome == "go-not-from-approver":
            self._wake_comment_problem(ticket, comment, outcome, outcome)
            return True, outcome
        self._write_state_label(ticket, "review")
        self._queue_qa(
            ticket,
            comment,
            head=ticket.current_head,
            review_round=self._review_context(ticket)[1] + 1,
        )
        return True, outcome

    def _queue_blocked_review(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> str | None:
        scope = _scope(ticket.repo, ticket.ticket_id)
        ticket = self._fresh_issue(ticket)
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
                return str(prior["outcome"])
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
                return "go-not-from-approver"
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
        return "queued-blocked-review"

    def _fix_unsat_revise(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replayed = connection.execute(
                """
                SELECT 1 FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND outcome = 'queued-fix'
                """,
                (*key, comment.id),
            ).fetchone()
            if replayed is None:
                row = connection.execute(
                    """
                    SELECT ticket_state.review_round, ticket_state.cap_extra,
                           ticket_state.generation, ticket_state.paused_question_id
                    FROM ticket_state
                    JOIN review_events
                      ON review_events.repo = ticket_state.repo
                     AND review_events.ticket_id = ticket_state.ticket_id
                     AND review_events.comment_id = ticket_state.paused_question_id
                    WHERE ticket_state.repo = ? AND ticket_state.ticket_id = ?
                      AND ticket_state.workflow_state = 'waiting-max'
                      AND review_events.outcome = 'waiting-max-unsat'
                    """,
                    key,
                ).fetchone()
                if row is None or not self._may_lift_cap(comment.author):
                    connection.commit()
                    return None
                counted_round = int(row["review_round"]) + 1
                at_cap = counted_round >= self._effective_cap(ticket, connection)
                cap_extra = int(row["cap_extra"]) + (1 if at_cap else 0)
                stop = next((item for item in ticket.comments if item.id == row["paused_question_id"]), None)
                verdict = _comment_link(stop) if stop is not None else f"comment:{row['paused_question_id']}"
                if stop is not None:
                    record_decisions(
                        connection,
                        ticket,
                        stop.id,
                        [self._stop_decision(ticket, stop, "unsat", comment, "Fix round")],
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
                            round_number=counted_round + 1,
                            cap=self._config_cap(ticket, connection) + cap_extra,
                            instruction=verdict,
                            resume=True,
                            ci_failure=red_checks(ticket, ticket.current_head),
                            connection=connection,
                        ),
                    ),
                    dedupe_key=f"t1:{_scope(ticket.repo, ticket.ticket_id)}:fix:{comment.id}",
                )
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    "fixing",
                    source="unsat-fix",
                    at=self.queue.now(),
                    also={
                        "review_round": counted_round,
                        "cap_extra": cap_extra,
                        "paused_action": None,
                        "paused_recipient": None,
                        "paused_head": None,
                        "paused_generation": None,
                        "paused_question_id": None,
                    },
                )
                connection.execute(
                    """
                    INSERT INTO review_events (repo, ticket_id, comment_id, outcome)
                    VALUES (?, ?, ?, 'queued-fix')
                    """,
                    (*key, comment.id),
                )
            connection.commit()
        self._write_state_label(ticket, "fixing")
        return True, "queued-fix"

    def _resume_for_max(
        self,
        ticket: TrackedTicket,
        comment: Comment,
    ) -> tuple[bool, str] | None:
        parked = self._parked_for_max(ticket)
        if parked is not None:
            if not self._may_lift_cap(comment.author):
                self._wake_comment_problem(
                    ticket, comment, "go-not-from-approver", "go-not-from-approver"
                )
                return True, "go-not-from-approver"
            return self._resume_paused(
                ticket, comment, grant=RoundGrant.approver(comment) if parked == "repeat" else None
            )
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

    def _parked_for_max(
        self, ticket: TrackedTicket
    ) -> Literal["unsat", "repeat", "size"] | None:
        with self._connect() as connection:
            if connection.execute(
                """
                SELECT 1 FROM ticket_state
                JOIN plan_events
                  ON plan_events.repo = ticket_state.repo
                 AND plan_events.ticket_id = ticket_state.ticket_id
                 AND plan_events.comment_id = ticket_state.paused_question_id
                WHERE ticket_state.repo = ? AND ticket_state.ticket_id = ?
                  AND ticket_state.workflow_state = 'waiting-max'
                  AND plan_events.outcome = 'waiting-max-size'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone() is not None:
                return "size"
            parked = connection.execute(
                """
                SELECT review_events.outcome FROM ticket_state
                JOIN review_events
                  ON review_events.repo = ticket_state.repo
                 AND review_events.ticket_id = ticket_state.ticket_id
                 AND review_events.comment_id = ticket_state.paused_question_id
                WHERE ticket_state.repo = ? AND ticket_state.ticket_id = ?
                  AND ticket_state.workflow_state = 'waiting-max'
                  AND review_events.outcome IN ('waiting-max-unsat', 'waiting-max-repeat')
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return None if parked is None else "unsat" if parked["outcome"] == "waiting-max-unsat" else "repeat"


def _overrule_state(
    connection: sqlite3.Connection, key: tuple[str, str], comment: Comment
) -> tuple[bool, sqlite3.Row | None]:
    replayed = connection.execute(
        """
        SELECT 1 FROM review_events
        WHERE repo = ? AND ticket_id = ? AND comment_id = ?
          AND outcome = 'overruled-finding'
        """,
        (*key, comment.id),
    ).fetchone()
    state = connection.execute(
        "SELECT review_round, generation FROM ticket_state WHERE repo = ? AND ticket_id = ?",
        key,
    ).fetchone()
    return replayed is not None, state


def _reopen_hold(connection: sqlite3.Connection, ticket: TrackedTicket) -> str:
    row = connection.execute(
        "SELECT state_name FROM linear_states WHERE repo = ? AND ticket_id = ?",
        (ticket.repo.lower(), ticket.ticket_id),
    ).fetchone()
    return "-" if row is None or row["state_name"] is None else str(row["state_name"])
