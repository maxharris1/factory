"""Ship intents, handoff redrive, external ships, and ReviewShipper."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from lib.dispatch.constants import (
    HARNESS_WORK,
    MERGED_BEFORE_HANDOFF,
    SETTLE_CLEARS,
    SETTLE_FAILURES_TO_WAKE,
    WATCHED_STATES,
    _NOT_FINISHED,
)
from lib.dispatch.errors_text import safe_ship_error
from lib.dispatch.helpers import (
    _comment_order,
    _is_full_sha,
    _round_number,
    _ship_state,
    ship_mode,
)
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import (
    AlreadyMergedShip,
    BaseMismatch,
    Comment,
    PollSource,
    RetryableStateError,
    RouteError,
    ShipError,
    ShipRequest,
    TicketRef,
    TrackedTicket,
)

if TYPE_CHECKING:
    from lib.dispatcher import Dispatcher


class ShipMixin:
    def _ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> tuple[str, str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head, policy, status FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        if row is None:
            return None
        return str(row["head"]), str(row["policy"]), str(row["status"])

    def _replay_ship_intent(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        current_head: str,
        policy: str,
        intent: tuple[str, str, str],
    ) -> tuple[bool, str]:
        intent_head, intent_policy, status = intent
        if intent_head != current_head or intent_policy != policy:
            raise RuntimeError("ship intent does not match approval")
        if status == "completed":
            current = self._review_context(ticket)[0]
            if current in ("merged", *WATCHED_STATES):
                state = current
            else:
                state = _ship_state(policy)
                if state == "waiting-external" and self._external_ship_completed(
                    ticket, intent_head
                ):
                    state = "merged"
            self._write_state_label(ticket, state)
            return True, f"ship-already-completed-{policy}"
        if status == "pending":
            if policy == "merge" and self._review_actions(ticket).is_merged(
                ticket,
                current_head,
            ):
                self._complete_ship_intent(ticket, comment.id, "merged")
                self._write_state_label(ticket, "merged")
                return True, "merge-confirmed"
            self._hold_ship_intent(ticket, comment.id)
            self._wake_ship_failed(ticket, comment, f"{policy} ship is unconfirmed")
            return True, "ship-unconfirmed"
        if status == "unconfirmed":
            self._wake_ship_failed(ticket, comment, f"{policy} ship is unconfirmed")
            return True, "ship-unconfirmed"
        if status == "failed":
            self._wake_ship_failed(ticket, comment, f"{policy} ship failed")
            return True, "ship-failed"
        raise RuntimeError(f"unknown ship intent status {status!r}")

    def _external_ship_completed(self, ticket: TrackedTicket, head: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM external_ship_intents
                WHERE repo = ? AND ticket_id = ? AND head = ?
                  AND status = 'completed'
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchone()
        return row is not None

    def _prepare_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        head: str,
        policy: str,
    ) -> tuple[str, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT head, policy, status FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
            if existing is not None:
                if existing["head"] != head or existing["policy"] != policy:
                    raise RuntimeError("ship intent does not match approval")
                connection.commit()
                return str(existing["status"]), True
            connection.execute(
                """
                INSERT INTO ship_intents (
                    repo, ticket_id, comment_id, head, policy, status
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment_id,
                    head,
                    policy,
                ),
            )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "ship",
                source="ship-intent",
                at=self.queue.now(),
            )
            connection.commit()
        return "pending", False

    def _hold_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents SET status = 'unconfirmed'
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "ship",
                source="ship-hold",
                at=self.queue.now(),
                guard=_NOT_FINISHED,
            )
            connection.commit()

    def _complete_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        state: str,
        *,
        approval: tuple[str, Comment] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents
                SET status = 'completed', error = NULL
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            )
            self._enter_ship_state(connection, ticket.repo, ticket.ticket_id, state)
            if approval is not None:
                self._record_approval(connection, ticket, *approval)
            self.clear_attention(
                ticket.repo, ticket.ticket_id, "ship-failed", connection=connection
            )
            connection.commit()

    def _mark_ship_intent_failed(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        error: str,
        *,
        redrive: bool = False,
        ship_failed_detail: str | None = None,
    ) -> bool:
        """pending -> failed. A handoff that failed before any confirmed
        write, or a merge that failed with a retryable error (issue #83), is
        marked redrive='eligible' for one re-drive."""
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE ship_intents
                SET status = 'failed', error = ?,
                    redrive = CASE
                        WHEN ? AND policy IN ('handoff', 'merge') THEN 'eligible'
                        ELSE NULL
                    END
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (
                    error,
                    1 if redrive else 0,
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment_id,
                ),
            ).rowcount
            if changed == 1 and ship_failed_detail is not None:
                self.flag_attention(
                    ticket.repo,
                    ticket.ticket_id,
                    "ship-failed",
                    ship_failed_detail,
                    connection=connection,
                )
        return changed == 1

    def _redrive_handoffs(self, ticket: TrackedTicket) -> None:
        """Re-drive, once, a ship that failed in a way that may be retried.

        A handoff that failed before any confirmed write, or (issue #83) a
        merge that failed with a retryable error. For a merge the re-drive
        first checks whether the PR is already merged at the head, and then
        ships through the same path as T4 (which marks a draft PR ready
        before the squash merge). A re-drive that fails again flags the
        ticket needs-attention: no further retry is made.

        The intent's comment must still be the APPROVE in force: the latest
        trusted reviewer verdict on the ticket, picked by the same rule the
        live path uses (_latest_review_verdict). A newer trusted verdict, a
        REVISE or another APPROVE, spends the re-drive with no write, so of
        several eligible intents on one ticket only the latest APPROVE can
        hand off. That APPROVE's head field must also still cover the
        intent's head, by T4's rule (_approval_covers), so an APPROVE
        edited in place to name another head spends the re-drive. A ticket
        already merged, handed off or done, or one with a completed handoff
        intent at the intent's head, is never handed off again: its
        re-drives are spent with no write.

        Then it checks what else can have changed since T4: the ship policy
        is still handoff, the PR head is still the intent's head and CI on
        it is still green. A moved head or red CI spends the re-drive with
        no write. Pending CI or an unread head waits for a later poll.

        The re-drive is claimed (eligible -> spent) before the adapter
        runs, so a crash mid-handoff never repeats it. A retryable error
        hands the claim back. Any other error keeps the intent failed and
        records its safe detail; the ship-failed wake is once per comment,
        so Grok keeps the wake from the first failure. Success completes
        the intent, wakes done and lets T4 replay the state label.
        """
        policy = ticket.factory.ship
        if policy not in ("handoff", "merge"):
            return
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT comment_id, head FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND policy = ?
                  AND status = 'failed' AND redrive = 'eligible'
                ORDER BY comment_id
                """,
                (ticket.repo.lower(), ticket.ticket_id, policy),
            ).fetchall()
        if not rows:
            return
        by_id = {comment.id: comment for comment in ticket.comments}
        in_force = self._latest_review_verdict(ticket)
        approve_in_force = (
            in_force
            if in_force is not None and in_force[1].get("verdict") == "APPROVE"
            else None
        )
        for row in rows:
            comment_id = str(row["comment_id"])
            comment = by_id.get(comment_id)
            if comment is None:
                continue
            if (
                approve_in_force is None
                or comment_id != approve_in_force[0].id
                or self._review_context(ticket)[0]
                in ("merged", "handed-off", "done", *WATCHED_STATES)
                or self._has_completed_ship_at(ticket, str(row["head"]), policy)
                or not self._approval_covers(
                    ticket, approve_in_force[1].get("head", ""), str(row["head"])
                )
            ):
                self._set_redrive(ticket, comment_id, "spent", "eligible")
                continue
            head = ticket.current_head
            if head is None:
                continue
            if head != row["head"]:
                self._set_redrive(ticket, comment_id, "spent", "eligible")
                continue
            ci = ticket.ci.get(head)
            if ci is None or ci.status == "pending":
                continue
            if not ci.clears:
                self._set_redrive(ticket, comment_id, "spent", "eligible")
                continue
            if self._held(ticket, head):
                # Issue #86 on the #83 re-drive: a hold that applies at this
                # head (a label put back after the OK, or an unread hold
                # input) keeps the ship waiting; the re-drive is kept.
                continue
            if not self._set_redrive(ticket, comment_id, "spent", "eligible"):
                continue
            if policy == "merge" and ticket.factory.finish_line == "merge":
                # #130: a watched ticket merges only through the queue.
                self._complete_redrive(
                    ticket, comment_id, "merge-queue", approval=(head, comment)
                )
                continue
            try:
                if policy == "handoff":
                    try:
                        note = self._review_actions(ticket).handoff(ticket, head)
                    except AlreadyMergedShip:
                        self._complete_redrive(ticket, comment_id, "merged")
                        self._wake_done(
                            ticket, comment, "merged", note=MERGED_BEFORE_HANDOFF
                        )
                        continue
                else:
                    note = None
                    if not self._review_actions(ticket).is_merged(ticket, head):
                        self._ship_head(ticket, head)
            except RetryableStateError:
                self._set_redrive(ticket, comment_id, "eligible", "spent")
                raise
            except Exception as exc:
                error = safe_ship_error(exc)
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE ship_intents SET error = ?
                        WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                          AND status = 'failed'
                        """,
                        (error, ticket.repo.lower(), ticket.ticket_id, comment_id),
                    )
                    self.flag_attention(
                        ticket.repo,
                        ticket.ticket_id,
                        "ship-failed",
                        f"{policy} ship failed again on its one retry: {error}",
                        connection=connection,
                    )
                self._wake_ship_failed(
                    ticket,
                    comment,
                    f"{policy} re-drive raised {error}",
                )
                continue
            state = _ship_state(policy)
            if state == "handed-off" and ticket.factory.finish_line == "merge":
                self._complete_redrive(
                    ticket, comment_id, "merge-queue", approval=(head, comment)
                )
                continue
            self._complete_redrive(ticket, comment_id, state)
            self._wake_done(ticket, comment, state, note=note)

    def _complete_redrive(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        state: str = "handed-off",
        *,
        approval: tuple[str, Comment] | None = None,
    ) -> None:
        """failed -> completed, and hand the APPROVE back to T4.

        T4 then finds the completed intent and replays its label write, as
        it does after any completed ship, retrying it on a later poll if the
        write fails. One transaction, so a crash leaves neither half.
        """
        scope = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ship_intents SET status = 'completed', error = NULL
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'failed'
                """,
                (*scope, comment_id),
            )
            self._enter_ship_state(connection, ticket.repo, ticket.ticket_id, state)
            if approval is not None:
                self._record_approval(connection, ticket, *approval)
            connection.execute(
                """
                DELETE FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*scope, comment_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO verdict_acceptances
                SELECT repo, ticket_id, comment_id, handled_at FROM handled_times
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*scope, comment_id),
            )
            connection.execute(
                "DELETE FROM handled_times WHERE repo = ? AND ticket_id = ? AND comment_id = ?",
                (*scope, comment_id),
            )
            self.clear_attention(
                ticket.repo, ticket.ticket_id, "ship-failed", connection=connection
            )
            connection.commit()

    def _enter_ship_state(
        self,
        connection: sqlite3.Connection,
        repo: str,
        ticket_id: str,
        state: str,
    ) -> None:
        """A completed ship's state: merged goes through _enter_merged; any
        other ship state never overwrites merged or done."""
        if state == "merged":
            self._enter_merged(connection, repo, ticket_id, "shipped")
            return
        write_workflow_state(
            connection,
            repo,
            ticket_id,
            state,
            source="ship-state",
            at=self.queue.now(),
            also=SETTLE_CLEARS,
            guard=_NOT_FINISHED,
        )

    def _reconcile_handoff(
        self,
        ticket: TrackedTicket,
        *,
        reading_comment_id: str | None = None,
    ) -> bool:
        if ticket.factory.ship != "handoff" or ticket.factory.finish_line == "merge":
            return False
        if ticket.draft is not False:
            return False
        if self._review_context(ticket)[0] != "building" or self._is_stuck(ticket):
            return False
        if self._has_open_factory_work(ticket, reading_comment_id):
            return False
        approve = self._standing_approval(ticket)
        if approve is None and ticket.linear_state_phase != "ready-for-review":
            return False
        head = ticket.current_head
        if approve is not None:
            if head is None:
                return False
            intent = self._ship_intent(ticket, approve.id)
            if intent is not None and intent[2] != "completed":
                return False
        self._write_state_label(ticket, "handed-off")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if approve is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO ship_intents (
                        repo, ticket_id, comment_id, head, policy, status
                    ) VALUES (?, ?, ?, ?, 'handoff', 'completed')
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, approve.id, head),
                )
            written = write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "handed-off",
                source="handoff-reconcile",
                at=self.queue.now(),
                also=SETTLE_CLEARS,
                guard="workflow_state = 'building'",
            )
            if written:
                connection.commit()
            else:
                connection.rollback()
        return written

    def _standing_approval(self, ticket: TrackedTicket) -> Comment | None:
        latest = self._latest_review_verdict(ticket)
        if latest is None:
            return None
        comment, fields = latest
        if fields.get("verdict") != "APPROVE" or comment.edited_at is not None:
            return None
        outcome = self._handled_outcome(ticket, comment.id)
        if outcome is not None and outcome.startswith(("rejected-", "superseded-")):
            return None
        if self._approve_refusal(comment, fields) is not None:
            return None
        if self._over_cap(ticket, _round_number(fields.get("round"))):
            return None
        return comment

    def _has_open_factory_work(
        self, ticket: TrackedTicket, reading_comment_id: str | None
    ) -> bool:
        scope = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT 1 FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND {HARNESS_WORK}
                  AND (
                    state IN ('queued', 'running')
                    OR (state = 'failed' AND failure_handled = 0)
                    OR (
                      handoff_comment_id IS NOT NULL AND handoff_comment_id != ?
                      AND handoff_comment_id NOT IN (
                        SELECT comment_id FROM handled_comments
                        WHERE repo = ? AND ticket_id = ?
                      )
                    )
                  )
                LIMIT 1
                """,
                (*scope, reading_comment_id or "", *scope),
            ).fetchone()
        return row is not None

    def _has_completed_ship_at(
        self,
        ticket: TrackedTicket,
        head: str,
        policy: str = "handoff",
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND policy = ?
                  AND head = ? AND status = 'completed'
                """,
                (ticket.repo.lower(), ticket.ticket_id, policy, head),
            ).fetchone()
        return row is not None

    def _set_redrive(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        value: str,
        expected: str,
    ) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE ship_intents SET redrive = ?
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'failed' AND redrive = ?
                """,
                (value, ticket.repo.lower(), ticket.ticket_id, comment_id, expected),
            ).rowcount
        return changed == 1

    def _discard_pending_ship_intent(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        state: str = "review",
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                DELETE FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND status = 'pending'
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).rowcount
            if changed == 1:
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    state,
                    source="ship-discard",
                    at=self.queue.now(),
                )
            connection.commit()

    def _wake_ship_failed(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        detail: str,
    ) -> None:
        self.wake_grok_once(
            ticket,
            reason="ship-failed",
            source_comment_id=comment.id,
            detail=self._project_wake_detail(ticket, detail),
        )

    def _has_completed_ship_intent(
        self, repo: str, ticket_id: str, comment_id: str, policy: str
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ship_intents
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND policy = ? AND status = 'completed'
                """,
                (repo.lower(), ticket_id, comment_id, policy),
            ).fetchone()
        return row is not None

    def _prepare_external_ship(self, request: ShipRequest) -> tuple[str, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT head, status FROM external_ship_intents
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                """,
                (request.repo.lower(), request.ticket_id, request.source_comment_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                if existing["head"] != request.head:
                    raise ShipError("ship intent does not match the request head")
                return str(existing["status"]), False
            connection.execute(
                """
                INSERT INTO external_ship_intents (
                    repo, ticket_id, source_comment_id, head, status
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    request.repo.lower(),
                    request.ticket_id,
                    request.source_comment_id,
                    request.head,
                ),
            )
            connection.commit()
        return "pending", True

    def _finish_external_ship(
        self,
        request: ShipRequest,
        status: str,
        error: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE external_ship_intents SET status = ?, error = ?
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                  AND status = 'pending'
                """,
                (
                    status,
                    error,
                    request.repo.lower(),
                    request.ticket_id,
                    request.source_comment_id,
                ),
            )
            connection.commit()

    def _settle_external_ship(
        self,
        ticket: TrackedTicket,
        source_comment_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._enter_merged(connection, ticket.repo, ticket.ticket_id, "shipped")
            connection.commit()
        self._write_state_label(ticket, "merged")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE external_ship_intents SET settled = 1
                WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, source_comment_id),
            )

    def _settle_external_ships(self, ticket: TrackedTicket) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_comment_id FROM external_ship_intents
                WHERE repo = ? AND ticket_id = ?
                  AND status = 'completed' AND settled = 0
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        for row in rows:
            self._try_settle_external_ship(ticket, str(row["source_comment_id"]))

    def _try_settle_external_ship(
        self,
        ticket: TrackedTicket,
        source_comment_id: str,
    ) -> None:
        try:
            self._settle_external_ship(ticket, source_comment_id)
        except Exception as exc:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE external_ship_intents
                    SET settle_failures = settle_failures + 1
                    WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, source_comment_id),
                )
                failures = int(
                    connection.execute(
                        """
                        SELECT settle_failures FROM external_ship_intents
                        WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                        """,
                        (ticket.repo.lower(), ticket.ticket_id, source_comment_id),
                    ).fetchone()[0]
                )
            if failures >= SETTLE_FAILURES_TO_WAKE:
                self.wake_grok_once(
                    ticket,
                    reason="ship-failed",
                    source_comment_id=source_comment_id,
                    detail=self._project_wake_detail(
                        ticket,
                        (
                            f"merged, but the merged state or label write failed "
                            f"{failures} times: {type(exc).__name__}: {exc}"
                        ),
                    ),
                )


class ReviewShipper:
    """F1-05's ship for T8's after:<approver> ok (the hook_t8 Shipper).

    ship raises unless F1-05 completed an after:<approver> ship intent for
    request.approve_comment_id whose previewed head (_previewed_head: on a
    finish_line: merge ticket, the approval the merge queue holds now) is
    request.head, CI on request.head is green, and request.head is a full
    SHA that is still the PR head. A finish_line: merge ticket that is not
    yet its merge queue's turn (_merge_turn) raises RetryableStateError
    before CI is checked, so T8 reads the ok again next poll: an ok posted
    while the PR conflicts waits for the update whatever CI on the old
    head says. The
    ticket is re-read from source, and in those fresh comments the latest
    trusted reviewer verdict must still be that APPROVE, before the ok.
    A verdict posted between that read and the merge is not seen; that
    gap is accepted. Then ship lands exactly that head in the repo's
    ship_mode. An after: repo fast-forwards base_branch to it, raising if
    that is not a fast-forward, and counts as shipped only when
    base_branch's tip is exactly that head; a squash repo squash-merges it
    or confirms it is already merged. Its own intent is keyed on the ok
    comment, so
    one request never merges twice. Once the completed write for that
    intent has committed, ship no longer raises: a failed merged state or
    label write is retried by Dispatcher.process, and wakes Grok after
    SETTLE_FAILURES_TO_WAKE failed attempts, not polls: the ship's own
    settle and each later process call are attempts, so the wake can come
    on the second poll. Before that commit, any failure raises.
    """

    def __init__(self, dispatcher: Dispatcher, source: PollSource):
        self.dispatcher = dispatcher
        self.source = source

    def ship(self, request: ShipRequest) -> None:
        dispatcher = self.dispatcher
        head = request.head
        policy = f"after:{request.approver}"
        if not _is_full_sha(head):
            raise ShipError(f"ship head {head!r} is not a 40-character SHA")
        if not dispatcher._has_completed_ship_intent(
            request.repo, request.ticket_id, request.approve_comment_id, policy
        ):
            raise ShipError(f"no completed {policy} ship intent for {head}")
        ticket = self._ticket(request)
        latest = dispatcher._latest_review_verdict(ticket)
        if (
            latest is None
            or latest[0].id != request.approve_comment_id
            or latest[1].get("verdict") != "APPROVE"
        ):
            raise ShipError(
                f"latest review verdict is not APPROVE {request.approve_comment_id}"
            )
        ok = next(
            (item for item in ticket.comments if item.id == request.source_comment_id),
            None,
        )
        if ok is None or _comment_order(latest[0]) >= _comment_order(ok):
            raise ShipError(
                f"APPROVE {request.approve_comment_id} does not precede "
                f"ok {request.source_comment_id}"
            )
        if ticket.factory.ship != policy:
            raise ShipError(f"ticket ship policy is {ticket.factory.ship}")
        if dispatcher._previewed_head(ticket, request.approve_comment_id) != head:
            raise ShipError(f"no completed {policy} ship intent for {head}")
        if ticket.current_head != head:
            raise ShipError(f"PR head is {ticket.current_head}, not {head}")
        if ticket.closed:
            raise RetryableStateError(f"PR closed before {head} shipped")
        if not dispatcher._merge_turn(ticket):
            raise RetryableStateError(f"{head} waits its turn in the merge queue")
        result = ticket.ci.get(head)
        if result is None or not result.clears:
            raise ShipError(f"CI on {head} is not green")
        status, fresh = dispatcher._prepare_external_ship(request)
        if status == "pending":
            if ship_mode(ticket.factory.ship) == "fast-forward":
                self._fast_forward(request, ticket, fresh)
            elif dispatcher.review_actions.is_merged(ticket, head):
                pass
            elif not fresh:
                raise ShipError(f"ship of {head} has no recorded result")
            else:
                try:
                    dispatcher._ship_head(ticket, head)
                except Exception as exc:
                    dispatcher._finish_external_ship(
                        request,
                        "failed",
                        safe_ship_error(exc),
                    )
                    raise
            dispatcher._finish_external_ship(request, "completed", None)
        elif status != "completed":
            raise ShipError(f"ship of {head} is {status}; not shipping again")
        try:
            dispatcher._try_settle_external_ship(ticket, request.source_comment_id)
        except Exception:
            # The completed write has committed; Dispatcher.process retries.
            pass

    def _fast_forward(
        self,
        request: ShipRequest,
        ticket: TrackedTicket,
        fresh: bool,
    ) -> None:
        """Confirm only when base_branch's tip is exactly request.head.

        is_merged(head) is not enough: a squash or a merge commit made
        elsewhere also merges the PR at that head, and a merge commit even
        makes head reachable, but the branch is then not the approved SHA.
        """
        actions = self.dispatcher._review_actions(ticket)
        head = request.head
        branch = ticket.factory.base_branch
        try:
            tip = actions.branch_tip(ticket, branch)
            if tip == head:
                return
            if not fresh:
                raise ShipError(f"ship of {head} has no recorded result")
            if actions.is_merged(ticket, head):
                raise ShipError(
                    f"{branch} is {tip}, not {head}; the PR was merged another way"
                )
            self.dispatcher._ship_head(ticket, head)
            try:
                tip = actions.branch_tip(ticket, branch)
            except Exception as exc:
                # Only the inner safe detail or the class name: str(exc) of
                # an adapter error can hold gh's stdout or stderr.
                inner = getattr(exc, "safe_detail", None)
                tip = (
                    f"unreadable ({type(exc).__name__}: {inner})"
                    if isinstance(inner, str) and inner
                    else f"unreadable ({type(exc).__name__})"
                )
            if tip != head:
                raise ShipError(f"{branch} is {tip}, not {head}, after fast-forward")
        except Exception as exc:
            if fresh:
                self.dispatcher._finish_external_ship(
                    request,
                    "failed",
                    safe_ship_error(exc),
                )
            raise

    def _ticket(self, request: ShipRequest) -> TrackedTicket:
        tracker = self.dispatcher._tracker(request.repo, request.ticket_id)
        if tracker is None:
            raise ShipError(f"{request.repo}#{request.ticket_id} is not tracked")
        # #108 (#42 N3): a merged PR is read, not dropped; the is_merged
        # check in ship confirms it.
        ref = TicketRef(request.repo, request.ticket_id, tracker, allow_merged=True)
        fresh_reader = getattr(self.source, "fresh_ticket", None)
        if fresh_reader is not None:
            fresh = fresh_reader(ref)
            found = () if fresh is None else (fresh,)
        else:
            found = self.source.tracked_tickets((ref,))
        found = [
            item
            for item in found
            if item.repo.lower() == request.repo.lower()
            and item.ticket_id == request.ticket_id
        ]
        if len(found) != 1:
            raise ShipError(f"{request.repo}#{request.ticket_id} was not found")
        try:
            observed = self.dispatcher._observed(found[0])
            return self.dispatcher._routed(observed or found[0])
        except (BaseMismatch, RouteError) as error:
            raise ShipError(str(error)) from error
