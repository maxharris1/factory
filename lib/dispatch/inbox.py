from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping

from lib.dispatch.errors_text import poll_error_text
from lib.dispatch.types import IssueComments
from lib.orch_yml import DriverInbox, Project
from lib.orchestrator_pull import enqueue_pull_wake
from lib.pr_title import SOURCES_TABLE
from lib.queue import Job
from lib.schema import AUT0_REPO

DRAIN_LIMIT = 10
DETAIL_LIMIT = 1500
_URL = re.compile(r"https://[^\s<>()\[\]`]+")
_LINEAR_SLUG = re.compile(r"^(https://linear\.app/[^/]+/issue/[A-Za-z0-9-]+)(?:/[^#]*)?(#.*)?$")


def inbox_marker(job_id: str) -> str:
    return f"<!-- orch:inbox job={job_id} -->"


class InboxMixin:
    wake_routes: Mapping[str, Project]

    def _wake_route(self, job: Job) -> Project | None:
        return self.wake_routes.get(job.request.repo.lower())

    def _queue_inbox_post(self, job: Job, inbox: DriverInbox) -> None:
        body = self._inbox_body(job, inbox)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO driver_inbox (
                    job_id, inbox_repo, inbox_number, body, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (job.id, inbox.repo, inbox.number, body, self.queue.now()),
            )

    def _queue_pull_wake(
        self, job: Job, logical_key: str, inbox: DriverInbox | None = None
    ) -> None:
        body = self._inbox_body(job, inbox) if inbox is not None else ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            enqueue_pull_wake(
                connection,
                job,
                logical_key,
                now=self.queue.now(),
                inbox=inbox,
                body=body,
            )
            connection.commit()

    def _inbox_body(self, job: Job, inbox: DriverInbox) -> str:
        request = job.request
        try:
            parsed = json.loads(request.detail or "")
        except json.JSONDecodeError:
            parsed = request.detail
        link = parsed.get("link") if isinstance(parsed, dict) else None
        cause = link if isinstance(link, str) and _URL.fullmatch(link) else "unknown"
        if request.repo.lower() == AUT0_REPO:
            cause = _LINEAR_SLUG.sub(r"\1\2", cause)
            detail = "(left out: Aut0 wakes carry IDs and links only)"
        elif request.repo.lower() != inbox.repo.lower():
            cause = _LINEAR_SLUG.sub(r"\1\2", cause)
            detail = "(left out: wake from another repo)"
        else:
            detail = _detail_text(parsed)
        return "\n".join(
            (
                f"**Wake: {request.action}** on {request.repo} {request.ticket_id}",
                f"PR: {self._inbox_pr_link(request.repo, request.ticket_id)}",
                f"Cause: {cause}",
                f"Detail: {detail}",
                inbox_marker(job.id),
            )
        )

    def _inbox_pr_link(self, repo: str, ticket_id: str) -> str:
        number: str | None = ticket_id if ticket_id.isdigit() else None
        if number is None:
            ticket = self._polled.get((repo.lower(), ticket_id))
            number = ticket.pr_number if ticket is not None else None
        if number is None:
            with self._connect() as connection:
                row = connection.execute(
                    f"SELECT pr_number FROM {SOURCES_TABLE} WHERE repo = ? AND ticket_id = ?",
                    (repo.casefold(), ticket_id),
                ).fetchone()
            number = row["pr_number"] if row is not None else None
        if number is None or not str(number).isdigit():
            return "unknown"
        return f"https://github.com/{repo}/pull/{number}"

    def _drain_driver_inbox(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, inbox_repo, inbox_number, body, status FROM driver_inbox
                WHERE status IN ('pending', 'sending')
                ORDER BY created_at, job_id LIMIT ?
                """,
                (DRAIN_LIMIT,),
            ).fetchall()
        for row in rows:
            repo, number = row["inbox_repo"], int(row["inbox_number"])
            try:
                poster = self._inbox_poster(repo)
                if not _posted_before_a_crash(poster, row):
                    self._mark_inbox(row["job_id"], "sending")
                    poster.post_issue_comment(repo, number, row["body"])
                self._mark_inbox(row["job_id"], "posted")
            except Exception as exc:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE driver_inbox SET last_error = ? WHERE job_id = ?",
                        (poll_error_text(exc), row["job_id"]),
                    )
                self.record_poll_exception(repo, str(number), exc, prefix="driver-inbox:")
                return

    def _mark_inbox(self, job_id: str, status: str) -> None:
        with self._connect() as connection:
            if status == "sending":
                connection.execute(
                    """
                    UPDATE driver_inbox SET status = 'sending', attempts = attempts + 1
                    WHERE job_id = ?
                    """,
                    (job_id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE driver_inbox
                    SET status = 'posted', posted_at = ?, last_error = NULL
                    WHERE job_id = ?
                    """,
                    (self.queue.now(), job_id),
                )

    def _inbox_poster(self, repo: str) -> IssueComments:
        if isinstance(self.review_actions, Mapping):
            return self.review_actions[repo.lower()]
        return self.review_actions


def _posted_before_a_crash(poster: IssueComments, row: sqlite3.Row) -> bool:
    return row["status"] == "sending" and poster.issue_has_marker(
        row["inbox_repo"], int(row["inbox_number"]), inbox_marker(row["job_id"])
    )


def _detail_text(parsed: object) -> str:
    from lib.dashboard import redact

    if isinstance(parsed, dict):
        text = parsed.get("detail")
        if not isinstance(text, str):
            rest = {key: value for key, value in parsed.items() if key != "link"}
            text = json.dumps(rest, separators=(",", ":"), sort_keys=True)
    else:
        text = str(parsed or "")
    text = redact(text).strip()
    if len(text) > DETAIL_LIMIT:
        text = text[:DETAIL_LIMIT] + " (cut)"
    if not text:
        return "none"
    fenced = text.replace("```", "'''")
    return f"\n```\n{fenced}\n```"
