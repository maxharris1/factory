"""Follow-up filings (issue #259): record at T3/T4, then file and link once.

T3 and T4 record a pending row for a trusted verdict they accept whose
`[follow-up]` items fall from factory.md review.followup_from_round on, or
from round 1 under review.block_on: user-impact. The
drain runs after the comment pass: pending -> filed (the issue exists) ->
commented (the PR links it). Each tracker write first looks for its marker,
so a crash between a write and its status update never writes twice.
"""

from __future__ import annotations

import json
import sqlite3

from lib.dispatch.helpers import _comment_link
from lib.dispatch.types import Comment, TrackedTicket
from lib.followups import (
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
        title, body = followup_issue(
            ticket.repo,
            int(ticket.pr_number or ticket.ticket_id),
            round_number,
            _comment_link(comment),
            comment.id,
            findings,
            parse_comment(comment.body).as_dict().get("risk", ticket.risk),
        )
        labels = [*([ticket.intake_label] if ticket.intake_label else []), "urgent"]
        connection.execute(
            """
            INSERT OR IGNORE INTO followup_filings (
                repo, ticket_id, comment_id, round, title, body, labels,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                ticket.repo.lower(),
                ticket.ticket_id,
                comment.id,
                round_number,
                title,
                body,
                json.dumps(labels),
                self.queue.now(),
            ),
        )

    def _drain_followups(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT comment_id, round, title, body, labels, status,
                       issue_number, created_at
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
                issue_number = actions.file_issue(
                    ticket,
                    str(row["title"]),
                    str(row["body"]),
                    tuple(json.loads(row["labels"])),
                    followup_marker(comment_id),
                    float(row["created_at"]),
                )
                self._set_followup_status(key, comment_id, "filed", issue_number)
            actions.comment_once(
                ticket,
                link_comment(comment_id, int(issue_number), int(row["round"])),
                link_marker(comment_id),
            )
            self._set_followup_status(key, comment_id, "commented", issue_number)

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
