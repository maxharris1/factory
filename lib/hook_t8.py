"""T8: external ok / decline for ship: after:<approver> (docs/SPEC.md section 4.2).

The previewed SHA is the head of the reviewer's latest verdict before the
approver's comment, when that verdict is APPROVE. That preview is
waiting until one ok or one decline consumes it. Any other verdict, or
none, means nothing is waiting.

- ok head=<sha> naming the previewed SHA calls Shipper.ship with that
  SHA. Any other sha is refused and the preview keeps waiting. An ok is
  also refused when the PR head is no longer the previewed SHA.
- decline (optional head=, which must name the preview) queues one fix
  for the builder. The third decline on the ticket marks it stuck.

The approver posts with from=<approver> role=approver from a login that
trusted_logins maps to <approver>. T8 queues no post-merge job (T9 is
not built in Phase 1).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from lib.dispatcher import (
    Comment,
    Dispatcher,
    TrackedTicket,
    _matching_head,
    _scope,
)
from lib.handoff import HandoffError, parse_comment
from lib.queue import JobRequest

DECLINES_TO_STUCK = 3


@dataclass(frozen=True)
class ShipRequest:
    repo: str
    ticket_id: str
    head: str
    approver: str
    source_comment_id: str


class Shipper(Protocol):
    """Ship seam for F1-05.

    ship must ship exactly request.head per the project's factory.md, or
    raise. A raise leaves the ok unhandled, so the next poll calls ship
    again with the same request; ship must be idempotent for it.
    """

    def ship(self, request: ShipRequest) -> None: ...


class T8Hook:
    def __init__(self, shipper: Shipper | None = None):
        self.shipper = shipper

    def initialize(self, dispatcher: Dispatcher) -> None:
        with dispatcher.queue.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS external_previews (
                    repo TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    approve_comment_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    PRIMARY KEY (repo, ticket_id, approve_comment_id)
                )
                """
            )

    def handle(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str] | None,
    ) -> tuple[bool, str] | None:
        if fields is None or fields["kind"] not in ("ok", "decline"):
            return None
        kind = fields["kind"]
        if not ticket.factory.ship.startswith("after:"):
            return True, f"rejected-{kind}-not-after-ship"
        approver = ticket.factory.ship[len("after:") :]
        if not dispatcher._trusted(comment, fields, approver, role="approver"):
            return True, f"rejected-{kind}-author"
        if dispatcher.is_stuck(ticket):
            dispatcher._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
            return True, "ticket-stuck"
        preview = _waiting_preview(dispatcher, ticket, comment)
        if preview is None:
            return True, f"rejected-{kind}-not-waiting"
        approve_id, previewed = preview
        consumed = self._consumed_by(dispatcher, ticket, approve_id)
        if consumed is not None and consumed != comment.id:
            return True, f"rejected-{kind}-not-waiting"
        reported = fields.get("head")
        if reported is not None and _matching_head(reported, previewed) is None:
            return True, f"refused-{kind}-wrong-sha"
        if kind == "ok":
            return self._ok(dispatcher, ticket, comment, approver, approve_id, previewed)
        return self._decline(dispatcher, ticket, comment, approver, approve_id, previewed)

    def _ok(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        approver: str,
        approve_id: str,
        previewed: str,
    ) -> tuple[bool, str]:
        head = _matching_head(previewed, ticket.current_head)
        if head is None:
            return True, "refused-ok-head-moved"
        if self.shipper is None:
            return False, "ship-unwired"
        self.shipper.ship(
            ShipRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                head=head,
                approver=approver,
                source_comment_id=comment.id,
            )
        )
        with dispatcher.queue.connect() as connection:
            _record(connection, ticket, approve_id, previewed, "shipped", comment.id)
        return True, "shipped"

    def _decline(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        approver: str,
        approve_id: str,
        previewed: str,
    ) -> tuple[bool, str]:
        timeout_seconds = ticket.job_timeout_minutes * 60
        generation = dispatcher._generation(ticket.repo, ticket.ticket_id)
        with dispatcher.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _record(connection, ticket, approve_id, previewed, "declined", comment.id)
            declines = int(
                connection.execute(
                    """
                    SELECT count(*) FROM external_previews
                    WHERE repo = ? AND ticket_id = ? AND outcome = 'declined'
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                ).fetchone()[0]
            )
            if declines < DECLINES_TO_STUCK:
                dispatcher.queue.enqueue_in_transaction(
                    connection,
                    JobRequest(
                        repo=ticket.repo,
                        ticket_id=ticket.ticket_id,
                        action="fix",
                        recipient=ticket.factory.builder_harness,
                        source_comment_id=comment.id,
                        timeout_seconds=timeout_seconds,
                        generation=generation,
                        head=previewed,
                        detail=(
                            f"{approver} declined {previewed} "
                            f"({declines} of {DECLINES_TO_STUCK}), "
                            f"comment {comment.id}"
                        ),
                    ),
                    dedupe_key=(
                        f"t8-fix:{_scope(ticket.repo, ticket.ticket_id)}:"
                        f"{comment.id}"
                    ),
                )
            connection.commit()
        if declines < DECLINES_TO_STUCK:
            return True, "queued-decline-fix"
        dispatcher._mark_stuck_and_wake(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            source_comment_id=comment.id,
            timeout_seconds=timeout_seconds,
            reason="declined-3",
            detail=f"{approver} declined three times; latest {previewed}",
        )
        return True, "stuck-declined-3"

    def _consumed_by(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        approve_id: str,
    ) -> str | None:
        with dispatcher.queue.connect() as connection:
            row = connection.execute(
                """
                SELECT comment_id FROM external_previews
                WHERE repo = ? AND ticket_id = ? AND approve_comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, approve_id),
            ).fetchone()
        return str(row["comment_id"]) if row is not None else None


def _waiting_preview(
    dispatcher: Dispatcher,
    ticket: TrackedTicket,
    comment: Comment,
) -> tuple[str, str] | None:
    reviewer = f"{ticket.factory.reviewer_harness}-bot"
    cutoff = (comment.created_at, comment.id)
    latest: tuple[str, Mapping[str, str]] | None = None
    for item in sorted(ticket.comments, key=lambda value: (value.created_at, value.id)):
        if (item.created_at, item.id) >= cutoff:
            break
        try:
            fields = parse_comment(item.body).as_dict()
        except HandoffError:
            continue
        if fields["kind"] != "verdict":
            continue
        if not dispatcher._trusted(item, fields, reviewer, role="reviewer"):
            continue
        latest = (item.id, fields)
    if latest is None or latest[1].get("verdict") != "APPROVE":
        return None
    head = latest[1]["head"]
    return latest[0], _matching_head(head, ticket.current_head) or head


def _record(
    connection: sqlite3.Connection,
    ticket: TrackedTicket,
    approve_id: str,
    head: str,
    outcome: str,
    comment_id: str,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO external_previews (
            repo, ticket_id, approve_comment_id, head, outcome, comment_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ticket.repo.lower(), ticket.ticket_id, approve_id, head, outcome, comment_id),
    )
