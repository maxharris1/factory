"""Comment processing: process, per-comment dispatch, edited comments and handled bookkeeping."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from lib.approver_commands import parse_slash_go
from lib.dispatch.constants import FINISHED_STATES, WATCHED_STATES
from lib.dispatch.errors_text import safe_ship_error
from lib.dispatch.helpers import (
    _acted_verdict,
    _comment_order,
    _holds,
    _matching_head,
    _redrive_eligible,
    _round_number,
)
from lib.dispatch.types import (
    Comment,
    RetryableShipError,
    RetryableStateError,
    RouteError,
    TrackedTicket,
)
from lib.handoff import HandoffError, parse_comment

_RECOVERS_EDITED = {
    "ready": frozenset({"queued-review", "answered-review", "queued-ci-fix"}),
    "verdict": frozenset({"answered-fix", "approve-ci-red-fix", "held-base-update"}),
}


class CommentsMixin:
    def process(self, ticket: TrackedTicket) -> None:
        self._remember_links(ticket, ticket.comments)
        try:
            ticket = self._routed(ticket)
        except RouteError as error:
            if ticket.closed and self._watched(ticket):
                self._abandon_closed(ticket)
            else:
                self._reject_route(ticket, error)
            return
        self._polled[(ticket.repo.lower(), ticket.ticket_id)] = ticket
        try:
            self._title_pull(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        if ticket.closed and self._watched(ticket):
            self._abandon_closed(ticket)
            return
        comments = sorted(ticket.comments, key=_comment_order)
        try:
            self._cancel_answered_jobs(ticket)
            self._redrive_handoffs(ticket)
            self._process_comments(ticket, comments)
            self._requeue_unanswered_jobs(ticket)
            self._requeue_refused_reviews(ticket)
        except BaseException as error:
            try:
                self._settle_external_ships(ticket)
            except Exception as settle_error:
                error.add_note(
                    f"settle also raised {type(settle_error).__name__}: {settle_error}"
                )
            raise
        self._settle_external_ships(ticket)
        try:
            self._drain_followups(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._drain_auto_extra(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)

    def _process_comments(
        self,
        ticket: TrackedTicket,
        comments: list[Comment],
    ) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        self._remember_verdicts(ticket, comments)
        for comment in comments:
            # A ready may re-read the ticket uncached (T2); later comments
            # then see the fresh head.
            ticket = self._polled.get(key, ticket)
            if self._is_handled(ticket, comment.id):
                continue
            if self._is_abandoned(ticket.repo, ticket.ticket_id):
                self._mark_handled(ticket, comment.id, "ticket-abandoned")
                continue
            fields: dict[str, str] | None
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                fields = None
            if self._after_finish(ticket, comment):
                continue
            if (
                fields is not None
                and fields["kind"] != "brief"
                and comment.edited_at is not None
            ):
                self._reject_edited(ticket, comment)
                continue
            if (
                fields is not None
                and fields["kind"] == "answers"
                and fields.get("state") == "abandoned"
            ):
                outcome = self._t5_abandoned(ticket, comment, fields)
                if outcome.startswith("rejected-"):
                    self._wake_comment_problem(ticket, comment, "rejected", outcome)
                self._mark_handled(ticket, comment.id, outcome)
                continue
            hooked = self._run_hooks(ticket, comment, fields)
            if hooked is not None:
                handled, outcome = hooked
                if handled:
                    if outcome.startswith("rejected-"):
                        self._wake_comment_problem(
                            ticket, comment, "rejected", outcome
                        )
                    self._mark_handled(ticket, comment.id, outcome)
                continue
            if fields is None:
                if parse_slash_go(comment.body) is not None:
                    self._mark_handled(ticket, comment.id, self._slash_go(ticket, comment))
                continue
            kind = fields["kind"]
            if kind == "ok" and _holds(ticket):
                outcome = self._hold_ok_outcome(ticket, comment)
                if outcome.startswith("rejected-"):
                    self._wake_comment_problem(ticket, comment, "rejected", outcome)
                self._mark_handled(ticket, comment.id, outcome)
                continue
            if kind not in (
                "go",
                "plan",
                "ready",
                "verdict",
                "questions",
                "answers",
                "retry",
            ):
                continue
            if kind != "retry" and self._is_stuck(ticket):
                self._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
                self._mark_handled(ticket, comment.id, "ticket-stuck")
                continue
            try:
                if kind == "retry":
                    handled, outcome = self._t_retry(ticket, comment, fields)
                elif kind == "go":
                    handled, outcome = self._t1(ticket, comment, fields)
                elif kind == "plan":
                    handled, outcome = self._t_plan(ticket, comment, fields)
                elif kind == "ready":
                    handled, outcome = self._t2(ticket, comment, fields)
                elif kind == "verdict":
                    verdict = fields["verdict"]
                    if verdict in ("REVISE", "APPROVE") and self._duplicate_verdict(
                        ticket, comment, fields, comments
                    ):
                        # One verdict per review job (#80): a repeat of a
                        # handled verdict starts no job, ship or wake.
                        self._mark_handled(ticket, comment.id, "duplicate-verdict")
                        continue
                    if (
                        self._review_context(ticket)[0]
                        in ("handed-off", *WATCHED_STATES)
                        and self._ship_intent(ticket, comment.id) is None
                    ):
                        # #108: a verdict with no re-entry behind it wakes nobody.
                        self._mark_handled(ticket, comment.id, "after-handoff")
                        continue
                    if verdict == "REVISE":
                        handled, outcome = self._t3(ticket, comment, fields)
                    elif verdict == "APPROVE":
                        handled, outcome = self._t4(ticket, comment, fields)
                    else:
                        handled, outcome = self._t_blocked(ticket, comment, fields)
                elif kind == "questions":
                    handled, outcome = self._t5_questions(ticket, comment, fields)
                else:
                    handled, outcome = self._t5_answers(ticket, comment, fields)
            except Exception as exc:
                if isinstance(exc, RetryableShipError):
                    self._discard_pending_ship_intent(
                        ticket,
                        comment.id,
                    )
                    raise
                if isinstance(exc, RetryableStateError):
                    raise
                if kind not in ("verdict", "questions", "answers"):
                    raise
                error = safe_ship_error(exc)
                eligible = _redrive_eligible(exc, ticket.factory.ship)
                if self._mark_ship_intent_failed(
                    ticket,
                    comment.id,
                    error,
                    redrive=eligible,
                ):
                    self._wake_ship_failed(
                        ticket,
                        comment,
                        f"ship raised {error}",
                    )
                    if not eligible:
                        # Issue #83: no retry is coming, so a person must look.
                        self.flag_attention(
                            ticket.repo,
                            ticket.ticket_id,
                            "ship-failed",
                            f"{ticket.factory.ship} ship failed: {error}",
                        )
                self._mark_handled(
                    ticket,
                    comment.id,
                    f"failed-{kind}-{type(exc).__name__}",
                )
                continue
            if handled:
                if outcome.startswith("rejected-"):
                    self._wake_comment_problem(ticket, comment, "rejected", outcome)
                elif kind == "verdict" and outcome.startswith("stale-"):
                    self._wake_comment_problem(ticket, comment, "stale", outcome)
                elif kind == "verdict" and outcome.startswith("superseded-"):
                    self._wake_comment_problem(ticket, comment, "rejected", outcome)
                if outcome in _RECOVERS_EDITED.get(kind, ()) or (
                    kind == "verdict" and _acted_verdict(outcome)
                ):
                    self.clear_attention(ticket.repo, ticket.ticket_id, "edited-comment")
                self._mark_handled(ticket, comment.id, outcome)

    def _slash_go(self, ticket: TrackedTicket, comment: Comment) -> str:
        if comment.edited_at is not None:
            self._reject_edited(ticket, comment)
            return "rejected-edited"
        if not self.approvals.may_approve(comment.author):
            if not self._trusted_bots(comment):
                return "ignored-slash-go-author"
            self._wake_comment_problem(
                ticket, comment, "go-not-from-approver", "go-not-from-approver"
            )
            return "go-not-from-approver"
        if self._is_stuck(ticket):
            self._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
            return "ticket-stuck"
        resumed = (
            self._resume_after_blocked(ticket, comment)
            or self._resume_after_cap(ticket, comment)
            or self._resume_paused(ticket, comment)
        )
        return "slash-go-no-target" if resumed is None else resumed[1]

    def _after_finish(self, ticket: TrackedTicket, comment: Comment) -> bool:
        """merged and done are absorbing (#108): on a finished ticket only a
        ship intent's replay acts; any other comment, a hook's included, is
        handled with no job, ship, state write or wake."""
        if (
            self._review_context(ticket)[0] not in FINISHED_STATES
            or self._ship_intent(ticket, comment.id) is not None
        ):
            return False
        self._mark_handled(ticket, comment.id, "after-merge")
        return True

    def _reject_edited(self, ticket: TrackedTicket, comment: Comment) -> None:
        """#18: an orch:v1 comment edited before it was handled is refused."""
        if self._trusted_bots(comment):
            self.flag_attention(
                ticket.repo,
                ticket.ticket_id,
                "edited-comment",
                f"orch:v1 comment {comment.id} was edited before it was handled; "
                "post a new comment instead",
            )
            self._wake_comment_problem(ticket, comment, "rejected", "rejected-edited")
        self._mark_handled(ticket, comment.id, "rejected-edited")

    def _duplicate_verdict(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        fields: Mapping[str, str],
        comments: Sequence[Comment],
    ) -> bool:
        """An earlier verdict that was accepted and acted on says the same thing.

        Same author, `from`, `role`, head (as a prefix match, as the rows
        compare heads), round and verdict. The first one already took its
        action (a fix job, a ship, a wake); a repeat must take none.

        Codex PR #82 r2 F2.3: only an earlier verdict whose recorded outcome
        is in ACTED_VERDICT_OUTCOMES counts. A rejected (wrong role or
        author), stale, refused or failed one took no action, so it never
        suppresses a later verdict, such as its corrected repost.
        """
        head = fields.get("head") or ""
        for earlier in comments:
            if earlier.id == comment.id:
                return False
            if earlier.author.lower() != comment.author.lower():
                continue
            try:
                other = parse_comment(earlier.body).as_dict()
            except HandoffError:
                continue
            if (
                other.get("kind") == "verdict"
                and other.get("from") == fields.get("from")
                and other.get("role") == fields.get("role")
                and other.get("verdict") == fields.get("verdict")
                and other.get("round") == fields.get("round")
                and _matching_head(other.get("head") or "", head) is not None
                and _acted_verdict(self._handled_outcome(ticket, earlier.id))
            ):
                return True
        return False

    def _handled_outcome(self, ticket: TrackedTicket, comment_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT outcome FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        return None if row is None else str(row["outcome"])

    def _predates(self, ticket: TrackedTicket, comment: Comment, queued_at: float) -> bool:
        """True when comment came before work first queued at queued_at:
        GitHub dates it to an earlier second, or the dispatcher handled it
        first. GitHub stamps comments in whole seconds."""
        if comment.created_at < math.floor(queued_at):
            return True
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM handled_times
                WHERE repo = ? AND ticket_id = ? AND comment_id = ? AND handled_at < ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id, queued_at),
            ).fetchone()
        return row is not None

    def _is_handled(self, ticket: TrackedTicket, comment_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM handled_comments
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            ).fetchone()
        return row is not None

    def _mark_handled(
        self,
        ticket: TrackedTicket,
        comment_id: str,
        outcome: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO handled_comments (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id, outcome),
            )
            connection.execute(
                "INSERT OR IGNORE INTO handled_times VALUES (?, ?, ?, ?)",
                (ticket.repo.lower(), ticket.ticket_id, comment_id, self.queue.now()),
            )

    def _remember_verdicts(self, ticket: TrackedTicket, comments: Sequence[Comment]) -> None:
        reviewer = f"{ticket.factory.reviewer_harness}-bot"
        rows = []
        for comment in comments:
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if fields.get("kind") == "verdict" and self._trusted(
                ticket, comment, fields, reviewer, role="reviewer"
            ):
                rows.append(
                    (
                        ticket.repo.lower(),
                        ticket.ticket_id,
                        comment.id,
                        fields.get("head") or "",
                        _round_number(fields.get("round")),
                        comment.created_at,
                    )
                )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO verdict_heads (
                    repo, ticket_id, comment_id, head, review_round, posted_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo, ticket_id, comment_id) DO UPDATE
                SET posted_at = COALESCE(verdict_heads.posted_at, excluded.posted_at)
                """,
                rows,
            )

    def _remember_links(self, ticket: TrackedTicket, comments: Sequence[Comment]) -> None:
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO comment_links (
                    repo, ticket_id, comment_id, link
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        ticket.repo.lower(),
                        ticket.ticket_id,
                        comment.id,
                        comment.url or ticket.ticket_url,
                    )
                    for comment in comments
                ],
            )
