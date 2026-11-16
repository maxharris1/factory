"""Follow-up filings (issues #259, #492): record at T3/T4, then park and link once.

T3 and T4 record a pending row for a trusted verdict they accept whose
`[follow-up]` items fall from factory.md review.followup_from_round on, or
from round 1 under review.block_on: user-impact. The drain runs after the
comment pass and before a done wake. A PR's first row files an unlabelled
issue, so the findings wait outside the queue until the orchestrator promotes
them. A later row adds its findings as a comment on the PR's newest parked
issue (open, without the intake label).
Rows go pending -> filed (the issue holds the findings) -> commented (the PR
links it). Each tracker write first looks for its marker, so a crash between
a write and its status update never writes twice.
"""

from __future__ import annotations

import json
import sqlite3

from lib.dispatch.helpers import _comment_link
from lib.dispatch.types import Comment, ReviewActions, TrackedTicket
from lib.followups import (
    followup_addition,
    followup_findings,
    followup_issue,
    followup_marker,
    followup_start,
    followups_active,
    link_comment,
    link_marker,
)
from lib.handoff import parse_comment


class FollowupsMixin:
    def _record_followups(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        round_number: int | None,
    ) -> None:
        if ticket.factory.tickets != "github" or round_number is None:
            return
        start = followup_start(ticket.factory.followup_from_round, ticket.factory.block_on)
        if not followups_active(start, round_number):
            return
        findings = followup_findings(comment.body)
        if not findings:
            return
        pr = int(ticket.pr_number or ticket.ticket_id)
        verdict_url = _comment_link(comment)
        title, body = followup_issue(
            ticket.repo,
            pr,
            round_number,
            verdict_url,
            comment.id,
            findings,
            parse_comment(comment.body).as_dict().get("risk", ticket.risk),
        )
        addition = followup_addition(
            ticket.repo, pr, round_number, verdict_url, comment.id, findings
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO followup_filings (
                repo, ticket_id, comment_id, round, title, body, labels,
                status, created_at, addition
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', 'pending', ?, ?)
            """,
            (
                ticket.repo.lower(),
                ticket.ticket_id,
                comment.id,
                round_number,
                title,
                body,
                self.queue.now(),
                addition,
            ),
        )

    def _park_followups(self, ticket: TrackedTicket) -> None:
        try:
            self._drain_followups(ticket)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)
        self._send_done_wakes_once_parked(ticket)

    def _hold_done_wake(
        self, ticket: TrackedTicket, comment: Comment, detail: dict[str, object]
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO followup_wakes (
                    repo, ticket_id, source_comment_id, head, timeout_seconds, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket.repo.lower(),
                    ticket.ticket_id,
                    comment.id,
                    ticket.current_head,
                    ticket.job_timeout_minutes * 60,
                    json.dumps(detail),
                ),
            )
            connection.commit()

    def _send_done_wakes_once_parked(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            if connection.execute(
                """
                SELECT 1 FROM followup_filings
                WHERE repo = ? AND ticket_id = ? AND status = 'pending'
                """,
                key,
            ).fetchone() is not None:
                return
            wakes = connection.execute(
                """
                SELECT source_comment_id, head, timeout_seconds, detail
                FROM followup_wakes WHERE repo = ? AND ticket_id = ?
                """,
                key,
            ).fetchall()
        parked = self._latest_followup_issue(ticket)
        for wake in wakes:
            detail = json.loads(wake["detail"])
            if parked is not None:
                detail["followup_issue"] = parked
            self.wake_grok_once(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                reason="done",
                source_comment_id=str(wake["source_comment_id"]),
                timeout_seconds=int(wake["timeout_seconds"]),
                detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
                head=wake["head"],
            )
            with self._connect() as connection:
                connection.execute(
                    """
                    DELETE FROM followup_wakes
                    WHERE repo = ? AND ticket_id = ? AND source_comment_id = ?
                    """,
                    (*key, wake["source_comment_id"]),
                )
                connection.commit()

    def _drain_followups(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT comment_id, round, title, body, status,
                       issue_number, created_at, addition
                FROM followup_filings
                WHERE repo = ? AND ticket_id = ? AND status IN ('pending', 'filed')
                ORDER BY created_at, comment_id
                """,
                key,
            ).fetchall()
        if not rows:
            return
        actions = self._review_actions(ticket)
        for row in rows:
            comment_id = str(row["comment_id"])
            issue_number = row["issue_number"]
            if row["status"] == "pending":
                if issue_number is None and row["addition"] is not None:
                    issue_number = self._parked_issue(ticket, actions)
                    if issue_number is None:
                        self._drop_addition(key, comment_id)
                    else:
                        self._set_followup_status(key, comment_id, "pending", issue_number)
                if issue_number is None:
                    issue_number = actions.file_issue(
                        ticket,
                        str(row["title"]),
                        str(row["body"]),
                        (),
                        followup_marker(comment_id),
                        float(row["created_at"]),
                    )
                else:
                    actions.comment_on_issue_once(
                        ticket,
                        int(issue_number),
                        str(row["addition"]),
                        followup_marker(comment_id),
                    )
                self._set_followup_status(key, comment_id, "filed", issue_number)
            actions.comment_once(
                ticket,
                link_comment(comment_id, int(issue_number), int(row["round"])),
                link_marker(comment_id),
            )
            self._set_followup_status(key, comment_id, "commented", issue_number)

    def _parked_issue(self, ticket: TrackedTicket, actions: ReviewActions) -> int | None:
        number = self._latest_followup_issue(ticket)
        if number is None:
            return None
        labels = actions.open_issue_labels(ticket, number)
        if labels is None or ticket.intake_label in labels:
            return None
        return number

    def _latest_followup_issue(self, ticket: TrackedTicket) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT issue_number FROM followup_filings
                WHERE repo = ? AND ticket_id = ? AND status != 'pending'
                ORDER BY created_at DESC, comment_id DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        return None if row is None else int(row["issue_number"])

    def _drop_addition(self, key: tuple[str, str], comment_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE followup_filings SET addition = NULL
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (*key, comment_id),
            )
            connection.commit()

    def _set_followup_status(
        self,
        key: tuple[str, str],
        comment_id: str,
        status: str,
        issue_number: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE followup_filings SET status = ?, issue_number = ?
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (status, issue_number, *key, comment_id),
            )
            connection.commit()
