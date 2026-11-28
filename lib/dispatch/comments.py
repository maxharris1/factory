"""Comment processing: process, per-comment dispatch, edited comments and handled bookkeeping."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence

from lib.approver_commands import SlashQa, parse_slash_go, parse_slash_qa
from lib.dispatch.constants import FINISHED_STATES, OK_UNREAD, WATCHED_STATES
from lib.dispatch.errors_text import safe_ship_error
from lib.dispatch.helpers import (
    _acted_verdict,
    _base_mismatch,
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
from lib.wait import is_locked

_RECOVERS_EDITED = {
    "ready": frozenset({"queued-review", "answered-review", "queued-ci-fix"}),
    "verdict": frozenset({"answered-fix", "approve-ci-red-fix", "held-base-update"}),
}


class CommentsMixin:
    def process(self, ticket: TrackedTicket) -> None:
        try:
            self._process(ticket)
        finally:
            try:
                self._drain_skipped_wakes(ticket)
            except Exception as exc:
                self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)

    def _process(self, ticket: TrackedTicket) -> None:
        self._remember_links(ticket, ticket.comments)
        self._remember_no_ci_base(ticket)
        if self._reopen_emptied_pull(ticket):
            return
        if self._reopen_own_close(ticket):
            return
        try:
            ticket = self._routed(ticket)
        except RouteError as error:
            if self._closed_unfinished(ticket):
                self._abandon_closed(ticket)
            else:
                self._reject_route(ticket, error)
            return
        ticket, held = self._advance_stack(ticket)
        key = (ticket.repo.lower(), ticket.ticket_id)
        self._polled[key] = ticket
        if held:
            self._sync_base_mismatch(ticket)
            return
        try:
            self._title_pull(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._follow_current_pull(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        if self._closed_unfinished(ticket):
            self._abandon_closed(ticket)
            return
        if self._sync_base_mismatch(ticket) is not None:
            return
        try:
            self.review.apply_engine_grants(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        comments = sorted(ticket.comments, key=_comment_order)
        try:
            self._cancel_answered_jobs(ticket)
            self._redrive_handoffs(ticket)
            self._resume_unannounced_review(ticket)
            self._process_comments(ticket, comments)
            self._reopen_on_reviews(self._polled.get(key, ticket))
            if _base_mismatch(self._polled.get(key, ticket)) is not None:
                return
            self._requeue_unanswered_jobs(ticket)
            self._requeue_refused_reviews(ticket)
            self._review_after_green_rerun(ticket)
            self._reconcile_handoff(self._polled.get(key, ticket))
        except BaseException as error:
            try:
                self._settle_external_ships(ticket)
            except Exception as settle_error:
                error.add_note(
                    f"settle also raised {type(settle_error).__name__}: {settle_error}"
                )
            raise
        self._settle_external_ships(ticket)
        self._park_followups(ticket)
        try:
            self._finish_auto_split(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._watch_ci_silence(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._charge_unannounced_red(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._request_fresh_ci(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._clear_recovered_base_red(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._watch_ci_stale(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._review_unannounced_green(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._drain_auto_extra(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        try:
            self._qa_follow(self._polled.get(key, ticket))
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)

    def _remember_no_ci_base(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        base = ticket.no_ci_base
        with self._connect() as connection:
            if base is None:
                connection.execute("DELETE FROM no_ci_bases WHERE repo = ? AND ticket_id = ?", key)
            else:
                connection.execute(
                    "INSERT INTO no_ci_bases (repo, ticket_id, base) VALUES (?, ?, ?) "
                    "ON CONFLICT (repo, ticket_id) DO UPDATE SET base = excluded.base",
                    (*key, base),
                )

    def _process_comments(
        self,
        ticket: TrackedTicket,
        comments: list[Comment],
    ) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        self._remember_verdicts(ticket, comments)
        self._drop_ci_wait_without_comment(ticket, comments)
        for comment in comments:
            # A ready may re-read the ticket uncached (T2); later comments
            # then see the fresh head.
            ticket = self._polled.get(key, ticket)
            if _base_mismatch(ticket) is not None:
                return
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
            if self._after_finish(ticket, comment, fields):
                continue
            if (
                fields is not None
                and fields["kind"] != "brief"
                and comment.edited_at is not None
            ):
                self._reject_edited(ticket, comment)
                continue
            if fields is not None and fields["kind"] == "answers":
                settle = {
                    "abandoned": self._t5_abandoned,
                    "handed-off": self._t5_handed_off,
                }.get(fields.get("state", ""))
                if settle is not None:
                    outcome = settle(ticket, comment, fields)
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
                elif (ruling := parse_slash_qa(comment.body)) is not None:
                    self._mark_handled(ticket, comment.id, self._slash_qa(ticket, comment, ruling))
                continue
            kind = fields["kind"]
            if kind == "ok" and _holds(ticket):
                outcome = self._read_ok(ticket, comment)
                if outcome == OK_UNREAD:
                    continue
                if outcome.startswith("rejected-"):
                    self._wake_comment_problem(ticket, comment, "rejected", outcome)
                self._note_ok(ticket, comment, outcome)
                self._mark_handled(ticket, comment.id, outcome)
                continue
            if kind == "qa":
                if ticket.factory.qa is not None:
                    self._mark_handled(ticket, comment.id, self._qa_record(ticket, comment, fields))
                continue
            if kind not in (
                "go",
                "plan",
                "ready",
                "verdict",
                "questions",
                "answers",
                "retry",
                "rebase",
            ):
                continue
            if kind != "retry" and self._is_stuck(ticket):
                self._ensure_stuck_wake(ticket.repo, ticket.ticket_id)
                self._mark_handled(ticket, comment.id, "ticket-stuck")
                continue
            try:
                if kind == "retry":
                    handled, outcome = self._t_retry(ticket, comment, fields)
                elif kind == "rebase":
                    handled, outcome = self._t_rebase(ticket, comment, fields)
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
                    if self._blocked_by_another(ticket, comment):
                        handled, outcome = True, "verdict-while-blocked"
                    elif verdict == "REVISE":
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
                if is_locked(exc):
                    raise RetryableStateError(kind) from exc
                if kind not in ("verdict", "questions", "answers"):
                    raise
                if isinstance(exc, (KeyError, AttributeError, TypeError, AssertionError)):
                    raise
                self._ship_raised(ticket, comment, exc)
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

    def _ship_raised(self, ticket: TrackedTicket, comment: Comment, exc: Exception) -> None:
        """comment's pending ship intent failed with exc: mark it failed,
        eligible for one re-drive where the policy allows it."""
        error = safe_ship_error(exc)
        eligible = _redrive_eligible(exc, ticket.factory.ship)
        attention = None if eligible else f"{ticket.factory.ship} ship failed: {error}"
        if not self._mark_ship_intent_failed(
            ticket, comment.id, error, redrive=eligible, ship_failed_detail=attention
        ):
            return
        self._wake_ship_failed(ticket, comment, f"ship raised {error}")

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
            self._overrule_stop(ticket, comment)
            or self._resume_for_max(ticket, comment)
            or self._resume_after_blocked(ticket, comment)
            or self._resume_after_cap(ticket, comment)
            or self._resume_paused(ticket, comment)
        )
        return "slash-go-no-target" if resumed is None else resumed[1]

    def _slash_qa(self, ticket: TrackedTicket, comment: Comment, command: SlashQa) -> str:
        if comment.edited_at is not None:
            return "rejected-edited"
        if not self.approvals.may_approve(comment.author):
            return "qa-ruling-not-from-approver"
        return self._qa_ruling(ticket, comment, command)

    def _after_finish(
        self, ticket: TrackedTicket, comment: Comment, fields: Mapping[str, str] | None
    ) -> bool:
        if (
            self._review_context(ticket)[0] not in FINISHED_STATES
            or self._ship_intent(ticket, comment.id) is not None
            or (fields is not None and fields["kind"] == "qa" and ticket.factory.qa is not None)
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
                and not self._released_by_overrule(ticket, earlier.id)
            ):
                return True
        return False

    def _released_by_overrule(self, ticket: TrackedTicket, verdict_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1 FROM decisions
                    JOIN review_events
                      ON review_events.repo = decisions.repo
                     AND review_events.ticket_id = decisions.ticket_id
                     AND review_events.comment_id = decisions.answer_id
                    WHERE decisions.repo = ? AND decisions.ticket_id = ?
                      AND decisions.question_id = ?
                      AND review_events.outcome = 'overruled-finding'
                    """,
                    (ticket.repo.lower(), ticket.ticket_id, verdict_id),
                ).fetchone()
                is not None
            )

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

    def _handled_no_later_than(
        self, ticket: TrackedTicket, comment_id: str, source_id: str | None
    ) -> bool:
        if source_id is None:
            return False
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM handled_times AS item
                JOIN handled_times AS source
                  ON source.repo = item.repo AND source.ticket_id = item.ticket_id
                WHERE item.repo = ? AND item.ticket_id = ?
                  AND item.comment_id = ? AND source.comment_id = ?
                  AND item.handled_at <= source.handled_at
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment_id, source_id),
            ).fetchone()
        return row is not None

    def _drop_ci_wait_without_comment(
        self, ticket: TrackedTicket, comments: list[Comment]
    ) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT comment_id FROM ci_waits WHERE repo = ? AND ticket_id = ?", key
            ).fetchone()
            if row is not None and row["comment_id"] not in {comment.id for comment in comments}:
                connection.execute("DELETE FROM ci_waits WHERE repo = ? AND ticket_id = ?", key)

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
                "DELETE FROM ci_waits WHERE repo = ? AND ticket_id = ? AND comment_id = ?",
                (ticket.repo.lower(), ticket.ticket_id, comment_id),
            )
            self._record_handled_time(connection, ticket, comment_id, self.queue.now())
            sent = self._write_attention_outbox(connection)
        self._settle_attention(sent)

    def _record_handled_time(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment_id: str,
        handled_at: float,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO handled_times VALUES (?, ?, ?, ?)",
            (ticket.repo.lower(), ticket.ticket_id, comment_id, handled_at),
        )

    def _rehandle(self, ticket: TrackedTicket, comment_id: str, outcome: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE handled_comments SET outcome = ?
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (outcome, ticket.repo.lower(), ticket.ticket_id, comment_id),
            )

    def _remember_verdicts(self, ticket: TrackedTicket, comments: Sequence[Comment]) -> None:
        rows = []
        for comment in comments:
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if fields.get("kind") == "verdict" and self._trusted_reviewer(
                ticket, comment, fields
            ):
                head = fields.get("head") or ""
                rows.append(
                    (
                        ticket.repo.lower(),
                        ticket.ticket_id,
                        comment.id,
                        head,
                        _round_number(fields.get("round")),
                        comment.created_at,
                        ticket.changed_lines
                        if _matching_head(head, ticket.current_head) is not None
                        else None,
                        fields.get("verdict"),
                    )
                )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO verdict_heads (
                    repo, ticket_id, comment_id, head, review_round, posted_at, changed_lines,
                    verdict
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo, ticket_id, comment_id) DO UPDATE
                SET posted_at = COALESCE(verdict_heads.posted_at, excluded.posted_at),
                    changed_lines = COALESCE(verdict_heads.changed_lines, excluded.changed_lines),
                    verdict = COALESCE(verdict_heads.verdict, excluded.verdict)
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
