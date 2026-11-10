"""T8: external ok / decline for ship: after:<approver> (docs/SPEC.md section 4.2).

A preview is waiting when the reviewer's latest verdict on the whole
ticket is an APPROVE that came before the approver's comment, and F1-05
(T4) completed an after:<approver> ship intent for it. The previewed SHA
is that intent's head: the full current head T4 passed to
await_approver, after a short-prefix or patch-id match. The APPROVE's
own head= is never the preview. A later verdict of any kind, even in the
same poll, means the earlier ok or decline is refused. An ok or decline
that arrives before T4 has completed that intent (CI still pending, say)
is refused as not-waiting by design: the approver has not been shown a
preview yet, and posts again after await_approver.

- ok head=<40-char sha> equal to the previewed SHA, while the PR head is
  still that SHA, ships it. Any other sha is refused and the preview
  keeps waiting.
- decline (optional head=, which must equal the preview) queues one fix
  for the builder. The third decline on the ticket marks it stuck.

One ok or one decline claims a preview, through an insert into
external_previews that only one dispatcher can win. A claimed ok calls
Shipper.ship at most once. If ship raises, or a later poll finds the
ok's claim still marked shipping, the preview is marked ship-failed and
Grok Bot is woken once. A result is written only over shipping, so a
recorded shipped is never overwritten; shipped also replaces a
ship-failed that a racing dispatcher wrote while ship() was running. The dispatcher never ships the
same request again. Three stored declines mark the ticket stuck before
any ok or decline is read, even after a crash.

The approver posts with from=<approver> role=approver from a login that
trusted_logins maps to <approver>. T8 queues no post-merge job (T9 is
not built in Phase 1).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Protocol

from lib.dispatcher import Comment, Dispatcher, ShipRequest, TrackedTicket, _scope
from lib.handoff import HandoffError, parse_comment
from lib.queue import JobRequest

DECLINES_TO_STUCK = 3
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class Shipper(Protocol):
    """Ship seam for F1-05 (lib.dispatcher.ReviewShipper).

    ship must ship exactly request.head (a full SHA) per the project's
    factory.md, or raise. T8 calls it at most once per request.
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
        orphan = _shipping_claim(dispatcher, ticket, comment.id)
        if orphan is not None:
            return _ship_failed(
                dispatcher,
                ticket,
                comment,
                orphan,
                "ship has no recorded result; not shipping again",
            )
        if (
            not dispatcher.is_stuck(ticket)
            and _declines(dispatcher, ticket) >= DECLINES_TO_STUCK
        ):
            _mark_declined_stuck(dispatcher, ticket, comment.id, approver)
        if dispatcher.is_stuck(ticket):
            dispatcher._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
            return True, "ticket-stuck"
        preview = _waiting_preview(dispatcher, ticket, comment)
        if preview is None:
            return True, f"rejected-{kind}-not-waiting"
        approve_id, previewed = preview
        claimed = _claimed_by(dispatcher, ticket, approve_id)
        if claimed is not None and claimed[0] != comment.id:
            return True, f"rejected-{kind}-not-waiting"
        reported = fields.get("head")
        if kind == "ok" or reported is not None:
            if reported != previewed:
                return True, f"refused-{kind}-wrong-sha"
        if kind == "ok":
            return self._ok(dispatcher, ticket, comment, approver, approve_id, previewed)
        return _decline(dispatcher, ticket, comment, approver, approve_id, previewed)

    def _ok(
        self,
        dispatcher: Dispatcher,
        ticket: TrackedTicket,
        comment: Comment,
        approver: str,
        approve_id: str,
        previewed: str,
    ) -> tuple[bool, str]:
        if ticket.current_head != previewed:
            return True, "refused-ok-head-moved"
        if self.shipper is None:
            return False, "ship-unwired"
        with dispatcher.queue.connect() as connection:
            won = _claim(connection, ticket, approve_id, previewed, "shipping", comment.id)
        if not won:
            claimed = _claimed_by(dispatcher, ticket, approve_id)
            if claimed is None or claimed[0] != comment.id:
                return True, "rejected-ok-not-waiting"
            if claimed[1] == "shipped":
                return True, "shipped"
            return _ship_failed(
                dispatcher,
                ticket,
                comment,
                approve_id,
                f"ship of {previewed} has no recorded result; not shipping again",
            )
        try:
            self.shipper.ship(
                ShipRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    head=previewed,
                    approver=approver,
                    source_comment_id=comment.id,
                )
            )
        except Exception as exc:
            return _ship_failed(
                dispatcher,
                ticket,
                comment,
                approve_id,
                f"ship of {previewed} raised {type(exc).__name__}: {exc}",
            )
        _set_outcome(dispatcher, ticket, approve_id, "shipped")
        return True, "shipped"


def _decline(
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
        won = _claim(connection, ticket, approve_id, previewed, "declined", comment.id)
        if not won:
            owner = connection.execute(
                """
                SELECT comment_id FROM external_previews
                WHERE repo = ? AND ticket_id = ? AND approve_comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, approve_id),
            ).fetchone()
            if str(owner["comment_id"]) != comment.id:
                connection.commit()
                return True, "rejected-decline-not-waiting"
        declines = int(
            connection.execute(
                """
                SELECT count(*) FROM external_previews
                WHERE repo = ? AND ticket_id = ? AND outcome = 'declined'
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()[0]
        )
        if won and declines < DECLINES_TO_STUCK:
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
                    f"t8-fix:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
                ),
            )
        connection.commit()
    if declines < DECLINES_TO_STUCK:
        return True, "queued-decline-fix"
    _mark_declined_stuck(dispatcher, ticket, comment.id, approver)
    return True, "stuck-declined-3"


def _declines(dispatcher: Dispatcher, ticket: TrackedTicket) -> int:
    with dispatcher.queue.connect() as connection:
        row = connection.execute(
            """
            SELECT count(*) FROM external_previews
            WHERE repo = ? AND ticket_id = ? AND outcome = 'declined'
            """,
            (ticket.repo.lower(), ticket.ticket_id),
        ).fetchone()
    return int(row[0])


def _mark_declined_stuck(
    dispatcher: Dispatcher,
    ticket: TrackedTicket,
    source_comment_id: str,
    approver: str,
) -> None:
    dispatcher._mark_stuck_and_wake(
        repo=ticket.repo,
        ticket_id=ticket.ticket_id,
        source_comment_id=source_comment_id,
        timeout_seconds=ticket.job_timeout_minutes * 60,
        reason="declined-3",
        detail=f"{approver} declined three times",
    )


def _shipping_claim(
    dispatcher: Dispatcher,
    ticket: TrackedTicket,
    comment_id: str,
) -> str | None:
    with dispatcher.queue.connect() as connection:
        row = connection.execute(
            """
            SELECT approve_comment_id FROM external_previews
            WHERE repo = ? AND ticket_id = ? AND comment_id = ?
              AND outcome = 'shipping'
            """,
            (ticket.repo.lower(), ticket.ticket_id, comment_id),
        ).fetchone()
    return str(row["approve_comment_id"]) if row is not None else None


def _ship_failed(
    dispatcher: Dispatcher,
    ticket: TrackedTicket,
    comment: Comment,
    approve_id: str,
    detail: str,
) -> tuple[bool, str]:
    if not _set_outcome(dispatcher, ticket, approve_id, "ship-failed"):
        claimed = _claimed_by(dispatcher, ticket, approve_id)
        if claimed is not None and claimed[1] == "shipped":
            return True, "shipped"
    dispatcher.wake_grok_once(
        repo=ticket.repo,
        ticket_id=ticket.ticket_id,
        reason="ship-failed",
        source_comment_id=comment.id,
        timeout_seconds=ticket.job_timeout_minutes * 60,
        detail=detail,
    )
    return True, "ship-failed"


def _waiting_preview(
    dispatcher: Dispatcher,
    ticket: TrackedTicket,
    comment: Comment,
) -> tuple[str, str] | None:
    reviewer = f"{ticket.factory.reviewer_harness}-bot"
    latest: tuple[Comment, Mapping[str, str]] | None = None
    for item in sorted(ticket.comments, key=lambda value: (value.created_at, value.id)):
        try:
            fields = parse_comment(item.body).as_dict()
        except HandoffError:
            continue
        if fields["kind"] != "verdict":
            continue
        if not dispatcher._trusted(item, fields, reviewer, role="reviewer"):
            continue
        latest = (item, fields)
    if latest is None:
        return None
    verdict, fields = latest
    if (verdict.created_at, verdict.id) >= (comment.created_at, comment.id):
        return None
    if fields.get("verdict") != "APPROVE":
        return None
    intent = dispatcher._ship_intent(ticket, verdict.id)
    if intent is None:
        return None
    head, policy, status = intent
    if (
        policy != ticket.factory.ship
        or status != "completed"
        or _FULL_SHA.fullmatch(head) is None
    ):
        return None
    return verdict.id, head


def _claim(
    connection: sqlite3.Connection,
    ticket: TrackedTicket,
    approve_id: str,
    head: str,
    outcome: str,
    comment_id: str,
) -> bool:
    inserted = connection.execute(
        """
        INSERT OR IGNORE INTO external_previews (
            repo, ticket_id, approve_comment_id, head, outcome, comment_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ticket.repo.lower(), ticket.ticket_id, approve_id, head, outcome, comment_id),
    ).rowcount
    return inserted == 1


def _claimed_by(
    dispatcher: Dispatcher,
    ticket: TrackedTicket,
    approve_id: str,
) -> tuple[str, str] | None:
    with dispatcher.queue.connect() as connection:
        row = connection.execute(
            """
            SELECT comment_id, outcome FROM external_previews
            WHERE repo = ? AND ticket_id = ? AND approve_comment_id = ?
            """,
            (ticket.repo.lower(), ticket.ticket_id, approve_id),
        ).fetchone()
    if row is None:
        return None
    return str(row["comment_id"]), str(row["outcome"])


def _set_outcome(
    dispatcher: Dispatcher,
    ticket: TrackedTicket,
    approve_id: str,
    outcome: str,
) -> bool:
    """Record a ship result over a claim still marked shipping.

    shipped also replaces ship-failed: another dispatcher may have marked
    the claim ship-failed while this one was still inside ship().
    """
    with dispatcher.queue.connect() as connection:
        changed = connection.execute(
            """
            UPDATE external_previews SET outcome = ?
            WHERE repo = ? AND ticket_id = ? AND approve_comment_id = ?
              AND (
                outcome = 'shipping'
                OR (? = 'shipped' AND outcome = 'ship-failed')
              )
            """,
            (outcome, ticket.repo.lower(), ticket.ticket_id, approve_id, outcome),
        ).rowcount
    return changed == 1
