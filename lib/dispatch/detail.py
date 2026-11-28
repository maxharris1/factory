"""Job detail, step enqueue and Grok wake delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Literal

from lib import job_detail
from lib.attention import DEDUPE_DROPPED, DRIVER_WAKE_UNANSWERED
from lib.dispatch.constants import (
    APPROVE_REJECTION_NOTES,
    GROK_WAKE_REASONS,
    GROK_WAKE_SKIP,
    REBASE_REJECTION_NOTES,
    RETRY_REJECTION_NOTES,
)
from lib.dispatch.helpers import (
    _ANSWERS,
    _comment_link,
    _detail_round_number,
    _grok_logical_key,
    _post_to,
    _scope,
    failed_checks,
)
from lib.dispatch.retry import _harness_job_active
from lib.dispatch.types import CIResult, Comment, TrackedTicket
from lib.grok_delivery import GrokDeliveryError, PullDriverDelivery, wake_body
from lib.handoff import HandoffError, parse_comment
from lib.max_actions import wait_facts, wait_of
from lib.queue import LOCAL_GATE_PASSED, Job, JobRequest, job_from_row, passed_head
from lib.queue import plan_round as _plan_round
from lib.role_router import sample_json
from lib.ticket_type import ticket_type_of
from lib.update_check import RebaseScope, UpdateScope

if TYPE_CHECKING:
    from lib.dispatch.qa import QaFix
    from lib.dispatch.rebase import RebaseStep
    from lib.evidence.ci import CIEvidence

DriverWakeStep = Literal["wait", "rewake", "flag"]


def driver_wake_step(
    deliveries: Sequence[float | None], rewoken: bool, now: float, timeout: float
) -> DriverWakeStep:
    if not deliveries or None in deliveries:
        return "wait"
    if now - max(deliveries) < timeout:
        return "wait"
    return "flag" if rewoken else "rewake"


CRITICAL_WAKE_ACTIONS = frozenset(
    {"stuck", "capped", "blocked", "awaiting-ok", "ship-failed"}
)


class DetailMixin:
    def _deliver_grok_jobs(self, deferred: set[str]) -> None:
        while (
            job := self.queue.claim(
                "grok",
                excluded_job_ids=deferred,
            )
        ) is not None:
            route = self._wake_route(job)
            inbox = route.driver_inbox if route is not None else None
            pull_driver = isinstance(self.grok_delivery, PullDriverDelivery)
            uses_webhook = (not pull_driver) and (inbox is None or route.grok_webhook)
            if uses_webhook and self.queue.now() < getattr(self, "_grok_backoff_until", 0.0):
                self.queue.release(job.id, owner="grok")
                deferred.add(job.id)
                continue
            try:
                if pull_driver:
                    logical_key = _grok_logical_key(job)
                    self._queue_pull_wake(job, logical_key, inbox=inbox)
                    receipt = self.grok_delivery.deliver(job, logical_key=logical_key)
                elif inbox is not None and not route.grok_webhook:
                    self._queue_inbox_post(job, inbox)
                    receipt = f"inbox:{_grok_logical_key(job)}"
                else:
                    if inbox is not None:
                        try:
                            self._queue_inbox_post(job, inbox)
                        except Exception as exc:
                            self._record_job_error(job, exc, prefix="driver-inbox:")
                    logical_key = _grok_logical_key(job)
                    receipt = self.grok_delivery.deliver(job, logical_key=logical_key)
                    if self.log is not None:
                        request = job.request
                        digest = hashlib.sha256(wake_body(job, logical_key)).hexdigest()
                        self.log(
                            f"wake delivered: job={job.id} repo={request.repo} "
                            f"ticket={request.ticket_id} action={request.action} "
                            f"attempt={request.attempt} body_sha256={digest}"
                        )
                if not isinstance(receipt, str) or not receipt:
                    raise ValueError("Grok delivery receipt must be a non-empty string")
                self.queue.finish(
                    job.id,
                    owner="grok",
                    handoff_comment_id=receipt,
                )
            except Exception as exc:
                if isinstance(exc, GrokDeliveryError):
                    self._record_job_error(job, f"{type(exc).__name__}:{exc.detail}",
                                           prefix="grok-delivery:")
                    if exc.retry_after is not None:
                        self._grok_backoff_until = self.queue.now() + exc.retry_after
                else:
                    self._record_job_error(job, exc, prefix="grok-delivery:")
                try:
                    self.queue.release(
                        job.id,
                        owner="grok",
                    )
                    deferred.add(job.id)
                except Exception as release_exc:
                    self._record_job_error(
                        job, release_exc, prefix="grok-delivery-release:"
                    )

    def _backstop_driver_wakes(self) -> None:
        with self._connect() as connection:
            facts = wait_facts(connection)
            states = {
                (str(row["repo"]).lower(), str(row["ticket_id"])): row
                for row in connection.execute(
                    "SELECT repo, ticket_id, workflow_state, stuck FROM ticket_state"
                )
            }
            flagged = {
                (str(row["repo"]), str(row["ticket_id"]))
                for row in connection.execute(
                    "SELECT repo, ticket_id FROM needs_attention WHERE reason = ?",
                    (DRIVER_WAKE_UNANSWERED,),
                )
            }
            rewakes = connection.execute(
                """
                SELECT jobs.id, jobs.repo, jobs.ticket_id, jobs.source_comment_id, jobs.state
                FROM jobs LEFT JOIN driver_inbox AS inbox ON inbox.job_id = jobs.id
                WHERE jobs.dedupe_key LIKE 'driver-rewake:%'
                  AND (
                      jobs.state = 'queued'
                      OR (jobs.state = 'done' AND (inbox.status != 'posted' OR inbox.pull_state = 'queued'))
                  )
                """
            ).fetchall()

        def asking(key: tuple[str, str]) -> str | None:
            state, fact = states.get(key), facts.get(key, {})
            if state is None:
                return None
            wait = wait_of(
                str(state["workflow_state"]),
                bool(state["stuck"]),
                question_id=fact.get("question_id"),
                parked_outcome=fact.get("parked"),
            )
            return fact.get("question_id") if wait is not None and wait.why == "question" else None

        for row in rewakes:
            if asking((str(row["repo"]).lower(), str(row["ticket_id"]))) == row["source_comment_id"]:
                continue
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if row["state"] == "queued":
                        self.queue.cancel(row["id"], "question-settled", connection=connection)
                    else:
                        connection.execute(
                            "DELETE FROM driver_inbox WHERE job_id = ? AND (status != 'posted' OR pull_state = 'queued')",
                            (row["id"],),
                        )
                    connection.commit()
            except Exception as exc:
                self.record_poll_exception(
                    row["repo"], row["ticket_id"], exc, prefix="driver-wake:"
                )
        for key, ticket in list(self._polled.items()):
            question_id = asking(key)
            if question_id is None and key not in flagged:
                continue
            try:
                self._backstop_driver_wake(ticket, question_id)
            except Exception as exc:
                self.record_poll_exception(
                    ticket.repo, ticket.ticket_id, exc, prefix="driver-wake:"
                )
        self._backstop_undelivered_critical_wakes()

    def _backstop_undelivered_critical_wakes(self) -> None:
        """Raise needs_attention if any critical driver wake stays undelivered
        (queued past driver_wake_seconds) due to webhook/429/network outages."""
        cutoff = self.queue.now() - self.job_states.driver_wake_seconds
        actions = sorted(CRITICAL_WAKE_ACTIONS)
        marks = ", ".join("?" for _ in actions)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    f"""
                    SELECT id, repo, ticket_id, action, created_at
                    FROM jobs
                    WHERE recipient = 'grok' AND state = 'queued'
                      AND action IN ({marks}) AND created_at <= ?
                    ORDER BY created_at, id
                    """,
                    (*actions, cutoff),
                ).fetchall()
                now = self.queue.now()
                for row in rows:
                    minutes = int((now - float(row["created_at"])) // 60)
                    self._flag_attention_once(
                        connection,
                        str(row["repo"]),
                        str(row["ticket_id"]),
                        DRIVER_WAKE_UNANSWERED,
                        f"{row['action']} wake {row['id']} undelivered for {minutes} min",
                    )
                connection.commit()
        except Exception as exc:
            self.record_poll_exception("driver-wake", "-", exc, prefix="driver-wake:")

    def _backstop_driver_wake(self, ticket: TrackedTicket, question_id: str | None) -> None:
        repo, ticket_id = ticket.repo, ticket.ticket_id
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if question_id is None:
                self.clear_attention(
                    repo, ticket_id, DRIVER_WAKE_UNANSWERED, connection=connection
                )
                connection.commit()
                return
            head = ticket.current_head or "-"
            rewake_prefix = f"driver-rewake:{_scope(repo, ticket_id)}:{question_id}:"
            wakes = connection.execute(
                """
                SELECT jobs.*, CASE
                    WHEN jobs.state != 'done' THEN NULL
                    WHEN jobs.handoff_comment_id LIKE 'inbox:%' THEN inbox.posted_at
                    WHEN jobs.handoff_comment_id LIKE 'pull:%' THEN COALESCE(inbox.acked_at, inbox.finished_at, inbox.created_at)
                    ELSE jobs.finished_at
                END AS delivered_at
                FROM jobs LEFT JOIN driver_inbox AS inbox ON inbox.job_id = jobs.id
                WHERE lower(jobs.repo) = ? AND jobs.ticket_id = ? AND jobs.recipient = 'grok'
                  AND jobs.action = 'clarify' AND jobs.source_comment_id = ?
                  AND jobs.state IN ('queued', 'running', 'done')
                """,
                (repo.lower(), ticket_id, question_id),
            ).fetchall()
            deliveries = [row["delivered_at"] for row in wakes]
            rewoken = connection.execute(
                "SELECT 1 FROM jobs WHERE substr(dedupe_key, 1, ?) = ?",
                (len(rewake_prefix), rewake_prefix),
            ).fetchone() is not None
            now = self.queue.now()
            step = driver_wake_step(
                deliveries, rewoken, now, self.job_states.driver_wake_seconds
            )
            if step == "wait":
                connection.commit()
                return
            latest = job_from_row(max(wakes, key=lambda row: row["delivered_at"]), connection)
            if step == "rewake":
                self.queue.enqueue_in_transaction(
                    connection,
                    replace(latest.request, attempt=2),
                    dedupe_key=f"{rewake_prefix}{head}",
                )
            else:
                minutes = int((now - min(deliveries)) // 60)
                self._flag_attention_once(
                    connection,
                    repo,
                    ticket_id,
                    DRIVER_WAKE_UNANSWERED,
                    f"clarify wake {latest.id} at head {head} unanswered for {minutes} min",
                )
            connection.commit()

    grok_wake_skip: frozenset[str] = GROK_WAKE_SKIP

    def wake_grok_once(
        self,
        ticket: TrackedTicket,
        *,
        reason: str,
        source_comment_id: str,
        detail: str,
        head: str | None = None,
        once_key: str | None = None,
    ) -> None:
        """T7: one Grok wake per stop, across restarts.

        refused wakes once per ticket. blocked and capped wake once per
        ticket and head. unparsed, ship-failed and go-not-from-approver wake
        once per comment. A caller's once_key replaces the reason's key, as
        a BLOCKED verdict wakes once per verdict comment.
        stuck goes through the ticket's stuck flag, which wakes once.
        """
        if reason not in GROK_WAKE_REASONS:
            raise ValueError(f"unknown wake reason {reason!r}")
        repo, ticket_id = ticket.repo, ticket.ticket_id
        timeout_seconds = ticket.job_timeout_minutes * 60
        if once_key is None:
            if reason in ("unparsed", "ship-failed", "rejected", "stale", "go-not-from-approver"):
                once_key = source_comment_id
            elif reason in ("blocked", "capped", "done", "awaiting-ok", "ci-rerun-refused"):
                once_key = head or "-"
            else:
                once_key = "-"
        if reason == "stuck":
            self._mark_stuck_and_wake(
                repo=repo,
                ticket_id=ticket_id,
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                reason="t7-stuck",
                detail=detail,
            )
            return
        dedupe_key = f"t7:{_scope(repo, ticket_id)}:{reason}:{once_key}"
        request = self._grok_request(
            JobRequest(
                repo=repo,
                ticket_id=ticket_id,
                action=reason,
                recipient="grok",
                source_comment_id=source_comment_id,
                timeout_seconds=timeout_seconds,
                generation=self._generation(repo, ticket_id),
                head=head,
                detail=detail,
            )
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM skipped_wakes WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone():
                return
            if self._skips_wake(connection, ticket, reason):
                if not connection.execute(
                    "SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone():
                    connection.execute(
                        """
                        INSERT INTO skipped_wakes (
                            dedupe_key, repo, ticket_id, reason, once_key, body,
                            status, skipped_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            dedupe_key,
                            repo.lower(),
                            ticket_id,
                            reason,
                            once_key,
                            _skip_notice(reason, once_key, request.detail or ""),
                            self.queue.now(),
                        ),
                    )
                    connection.commit()
                return
            self.queue.enqueue_in_transaction(connection, request, dedupe_key=dedupe_key)
            connection.commit()

    def _skips_wake(
        self, connection: sqlite3.Connection, ticket: TrackedTicket, reason: str
    ) -> bool:
        if reason not in self.grok_wake_skip:
            return False
        if ticket.pr_number is None and ticket.factory.tickets != "github":
            return False
        return reason != "done" or not connection.execute(
            "SELECT 1 FROM followup_filings WHERE repo = ? AND ticket_id = ?",
            (ticket.repo.lower(), ticket.ticket_id),
        ).fetchone()

    def _drain_skipped_wakes(self, ticket: TrackedTicket) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT dedupe_key, reason, once_key, body FROM skipped_wakes
                WHERE repo = ? AND ticket_id = ? AND status = 'pending'
                ORDER BY skipped_at, dedupe_key
                """,
                key,
            ).fetchall()
        if not rows:
            return
        actions = self._review_actions(ticket)
        for row in rows:
            dedupe_key = str(row["dedupe_key"])
            actions.comment_once(
                ticket, str(row["body"]), _skip_marker(str(row["reason"]), str(row["once_key"]))
            )
            with self._connect() as connection:
                connection.execute(
                    "UPDATE skipped_wakes SET status = 'posted' WHERE dedupe_key = ?",
                    (dedupe_key,),
                )
                connection.commit()

    def _grok_request(self, request: JobRequest) -> JobRequest:
        return replace(
            request,
            detail=self._linked_wake_detail(
                request.repo,
                request.ticket_id,
                request.source_comment_id,
                request.detail or "",
            ),
        )

    def _linked_wake_detail(
        self,
        repo: str,
        ticket_id: str,
        source_comment_id: str,
        detail: str,
    ) -> str:
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            parsed = {"detail": detail}
        parsed.setdefault("link", self._wake_link(repo, ticket_id, source_comment_id))
        return json.dumps(parsed, separators=(",", ":"), sort_keys=True)

    def _wake_link(self, repo: str, ticket_id: str, source_comment_id: str) -> str:
        ticket = self._polled.get((repo.lower(), ticket_id))
        if ticket is not None:
            for comment in ticket.comments:
                if comment.id == source_comment_id and comment.url:
                    return comment.url
        with self._connect() as connection:
            remembered = connection.execute(
                """
                SELECT link FROM comment_links
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                """,
                (repo.lower(), ticket_id, source_comment_id),
            ).fetchone()
        if remembered is not None and remembered["link"]:
            return str(remembered["link"])
        if ticket is not None and ticket.ticket_url:
            return ticket.ticket_url
        return f"comment:{source_comment_id}"

    def _enqueue_or_flag(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
        *,
        dedupe_key: str,
        ci: CIEvidence | None = None,
    ) -> tuple[Job, Comment | None]:
        """Enqueue comment's step; when dedupe drops the job, flag the ticket
        (issue #84). Returns the job holding the key and the later trusted
        comment that already answered the step, which _enqueue_step cancels
        in the same transaction.

        A drop is benign while the job already holding the key is still
        queued or running. When that job is finished, the comment queues
        nothing and the ticket would stall silently, so it is flagged
        needs-attention instead. A job that is queued clears the flag.
        """
        if request.action in ("review", "fix") or json.loads(request.detail or "{}").get("resume"):
            ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job, answered = self._enqueue_step(
                connection, ticket, comment, request, dedupe_key=dedupe_key, ci=ci
            )
            if answered is None:
                self._flag_dropped(connection, ticket, request, job)
            connection.commit()
        return job, answered

    def _flag_dropped(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        request: JobRequest,
        job: Job,
    ) -> None:
        if job.request.source_comment_id == request.source_comment_id:
            self.clear_attention(
                ticket.repo, ticket.ticket_id, DEDUPE_DROPPED, connection=connection
            )
        elif job.state not in ("queued", "running") and not _harness_job_active(
            connection, (ticket.repo.lower(), ticket.ticket_id)
        ):
            self.flag_attention(
                ticket.repo,
                ticket.ticket_id,
                DEDUPE_DROPPED,
                f"{request.action} for comment {request.source_comment_id} matched "
                f"finished job {job.id} ({job.state}); nothing was queued",
                connection=connection,
            )

    def _enqueue_step(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
        *,
        dedupe_key: str,
        ci: CIEvidence | None = None,
    ) -> tuple[Job, Comment | None]:
        """Enqueue the step comment asks for in the caller's open transaction,
        and cancel it in that transaction when a later trusted comment in the
        read already answered it (#108). Every plan, build, fix and review a
        comment queues goes through here, so no claim sees an answered step
        queued. Returns the job and that answer.
        """
        if request.recipient == "grok":
            request = self._grok_request(request)
        if (
            request.action == "review"
            and request.recipient in self.refused_harnesses
            and ticket.factory.reviewer_harness not in self.refused_harnesses
        ):
            request = _review_for_reviewer_of_record(ticket, request)
        request = self._with_evidence(connection, ticket, comment, request, ci=ci)
        job = self.queue.enqueue_in_transaction(connection, request, dedupe_key=dedupe_key)
        self._record_handled_time(connection, ticket, comment.id, self.queue.now())
        if request.action not in _ANSWERS:
            return job, None
        answered = self._answered_by(
            ticket,
            comment,
            request.action,
            request.recipient,
            queued_at=self._first_queued_at(job, connection=connection),
            head=request.head,
            round_number=_detail_round_number(request.detail),
            plan_round=_plan_round(request.detail),
            unhandled_plan_by_round=True,
        )
        if answered is not None:
            self._cancel_answered(connection, job, request, answered)
        return job, answered

    def _reviewed_head(
        self, ticket: TrackedTicket, comment: Comment, through_round: int
    ) -> str | None:
        from lib.evidence.findings import findings_evidence

        with self._connect() as connection:
            comments = _verdict_sources(connection, ticket, comment)
            reviewers = _accepted_reviewers(connection, ticket)
        return findings_evidence(
            comments=comments,
            trusted_logins=self.trusted_logins,
            reviewers=reviewers,
            current_round=through_round + 1,
            bases={},
        ).reviewed_head

    def _fresh_issue(self, ticket: TrackedTicket) -> TrackedTicket:
        """Call before BEGIN IMMEDIATE: the GitHub read must not hold the queue's write lock."""
        if ticket.read_issue_body is None:
            return ticket
        body = ticket.read_issue_body()
        return replace(
            ticket,
            issue_body=ticket.issue_body if body is None else body,
            read_issue_body=None,
        )

    def _with_evidence(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
        *,
        ci: CIEvidence | None = None,
    ) -> JobRequest:
        detail = json.loads(request.detail or "{}")
        resume = detail.get("resume") is True
        if request.action not in ("review", "fix") and not resume:
            return request
        from lib.evidence.ci import ci_evidence
        from lib.evidence.criteria import criteria_evidence
        from lib.evidence.findings import findings_evidence
        from lib.evidence.instructions import instructions_evidence

        state = connection.execute(
            """
            SELECT plan_gap_count, paused_question_id FROM ticket_state
            WHERE repo = ? AND ticket_id = ?
            """,
            (ticket.repo.lower(), ticket.ticket_id),
        ).fetchone()
        scope = (ticket.repo.lower(), ticket.ticket_id)
        findings = findings_evidence(
            comments=_verdict_sources(connection, ticket, comment),
            trusted_logins=self.trusted_logins,
            reviewers=_accepted_reviewers(connection, ticket),
            current_round=(_detail_round_number(request.detail) or 1)
            + (request.action == "review"),
            bases=_review_bases(
                [
                    str(row["detail"] or "{}")
                    for row in connection.execute(
                        """
                        SELECT detail FROM jobs
                        WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                        ORDER BY rowid
                        """,
                        scope,
                    )
                ]
                + ([request.detail or "{}"] if request.action == "review" else [])
            ),
        )
        rebase = detail.get("rebase") if request.action == "review" else None
        if rebase is not None and rebase["last_reviewed_head"] == findings.reviewed_head:
            findings = replace(findings, reviewed_base=rebase["reviewed_base"])
        result = ticket.ci.get(request.head or "")
        if ci is None and result is not None and result.status == "failure":
            ci = ci_evidence(failed=failed_checks(result), logs={})
        detail["evidence"] = {
            "criteria": asdict(
                criteria_evidence(
                    issue_key=ticket.source_issue or ticket.ticket_id,
                    issue_url=ticket.ticket_url,
                    brief=ticket.brief_comment,
                    issue_body=ticket.issue_body,
                    pr_body=ticket.pr_body,
                    ready_starts_review=ticket.ready_starts_review,
                    pr_url=ticket.ticket_url,
                )
            ),
            "findings": asdict(findings),
            "instructions": asdict(
                instructions_evidence(
                    comments=ticket.comments,
                    trusted_logins=self.trusted_logins,
                    source_comment_id=comment.id,
                    action=request.action,
                    detail=detail,
                    prior_detail=(
                        self._resumed_step_detail(
                            connection,
                            ticket,
                            comment,
                            request.action,
                            state["paused_question_id"] if state is not None else None,
                        )
                        if resume
                        else None
                    ),
                    plan_gaps=int(state["plan_gap_count"]) if state is not None else 0,
                    findings=findings.findings,
                    approvers=self.approvals.logins,
                )
            ),
            "ci": asdict(ci) if ci is not None else None,
        }
        return replace(
            request, detail=json.dumps(detail, separators=(",", ":"), sort_keys=True)
        )

    def _resumed_step_detail(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        action: str,
        question_id: str | None,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT id, detail FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND action = ?
              AND source_comment_id != ?
              AND (? IS NULL OR handoff_comment_id = ? OR handoff_comment_id IS NULL)
            ORDER BY coalesce(handoff_comment_id = ?, 0) DESC, created_at DESC, rowid DESC
            LIMIT 1
            """,
            (
                ticket.repo.lower(),
                ticket.ticket_id,
                action,
                comment.id,
                question_id,
                question_id,
                question_id,
            ),
        ).fetchone()
        if row is None:
            return None
        return json.loads(job_detail.load_row(connection, row["id"], row["detail"]) or "{}")

    def _job_detail(
        self,
        ticket: TrackedTicket,
        *,
        action: str,
        head: str | None = None,
        round_number: int | None = None,
        instruction: str | None = None,
        plan_text: str | None = None,
        resume: bool = False,
        cap: int | None = None,
        kind: str | None = None,
        update: UpdateScope | None = None,
        rebase: RebaseScope | None = None,
        rebase_step: RebaseStep | None = None,
        plan_round: int | None = None,
        ci_failure: tuple[str, ...] = (),
        unmapped: str | None = None,
        qa_findings: QaFix | None = None,
        source: str | None = None,
        size_flag: dict[str, int] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        if round_number is not None and cap is None:
            cap = self._effective_cap(ticket, connection)
        context: dict[str, object] = {
            "linear_url": ticket.ticket_url,
            "gitBranchName": ticket.git_branch_name,
            "base_branch": ticket.factory.base_branch,
            "pr_number": ticket.pr_number,
            "source_issue": ticket.source_issue,
            "brief": ticket.brief or ticket.ticket_url,
            "risk": ticket.risk,
            "round": f"{round_number}/{cap}" if round_number is not None else None,
            "reviewer_harness": ticket.factory.reviewer_harness,
            "verify_skill": ticket.factory.verify,
            "repo_skill_paths": [
                f".claude/skills/{skill}/SKILL.md"
                for skill in ticket.factory.skills
            ],
            "post_to": _post_to(ticket.pr_number),
            "action": action,
            "head": head,
            "project_bot": ticket.grok_bot,
        }
        if instruction is not None:
            context["instruction"] = instruction
        if source is not None:
            context["source"] = source
        if action == "build" and ticket.current_head is not None:
            context["start_head"] = ticket.current_head
        if action in ("plan", "build") and ticket.brief_text:
            context.update(brief_text=ticket.brief_text, brief_url=ticket.brief_url)
        if action in ("build", "fix") and ticket.brief_text:
            from lib.proof import required, to_detail

            if proof := required(ticket.brief_text, ticket.issue_body):
                context["proof"] = to_detail(proof)
        if action in ("plan", "build", "fix") and ticket.brief_comment is not None:
            if (ticket_type := ticket_type_of(ticket.brief_comment.body)) is not None:
                context["ticket_type"] = ticket_type
        if plan_text is not None:
            context["plan_text"] = plan_text
        if action in ("plan", "build") and (answers := self._answers(ticket)):
            context["answers"] = answers
        if action in ("plan", "build", "fix", "review") and (decisions := self._decisions(ticket, connection)):
            context["decisions"] = decisions
        if ticket.factory_md is not None:
            context["routing"] = _routing_payload(ticket)
        if kind is not None:
            context["kind"] = kind
        if plan_round is not None:
            context["plan_round"] = plan_round
        if unmapped is not None:
            context["unmapped"] = unmapped
        if update is not None:
            context.update(
                kind="update",
                last_reviewed_head=update.last_reviewed_head,
                update_merge=update.merge,
                update_commits=list(update.commits),
            )
        if rebase is not None:
            context["rebase"] = {
                name: value
                for name, value in asdict(rebase).items()
                if value is not None and value is not False
            }
        if rebase_step is not None:
            context.update(kind="rebase", rebase=rebase_step.block())
        if size_flag is not None:
            context["size_flag"] = size_flag
        if ci_failure:
            context["ci_failure"] = {"failed_checks": list(ci_failure)}
        if qa_findings is not None:
            context["qa_findings"] = asdict(qa_findings)
        if ticket.stack is not None:
            context["stack"] = asdict(ticket.stack)
        if (no_ci_base := ticket.no_ci_base) is not None:
            context.update(no_ci_base=no_ci_base, local_checks=list(ticket.factory.ci_local_checks))
            if (gate := ticket.factory.ci_local_gate) is not None:
                context["local_gate"] = asdict(gate)
                if (gate_head := self._local_gate_head(ticket, connection)) is not None:
                    context["local_gate_head"] = gate_head
        if resume:
            # Claim order (lib/queue.py): a resumed paused step goes before
            # bug and ordinary work, after urgent (issue #85).
            context["resume"] = True
        return json.dumps(context, separators=(",", ":"), sort_keys=True)

    def _local_gate_head(
        self, ticket: TrackedTicket, connection: sqlite3.Connection | None = None
    ) -> str | None:
        if connection is None:
            with self._connect() as connection:
                return self._local_gate_head(ticket, connection)
        rows = connection.execute(
            "SELECT detail FROM job_events WHERE repo = ? AND ticket_id = ? AND event = ? "
            "ORDER BY recorded_at DESC, id DESC",
            (ticket.repo.lower(), ticket.ticket_id, LOCAL_GATE_PASSED),
        )
        return next((head for row in rows if (head := passed_head(row["detail"])) is not None), None)

    def _answers(self, ticket: TrackedTicket) -> list[dict[str, str | None]]:
        answers: list[dict[str, str | None]] = []
        for comment in ticket.comments:
            try:
                handoff = parse_comment(comment.body)
            except HandoffError:
                continue
            fields = handoff.as_dict()
            if (
                fields["kind"] != "answers"
                or fields.get("state") == "abandoned"
                or not self._trusted(ticket, comment, fields, "grok-bot", role="orchestrator")
            ):
                continue
            prose = None if comment.edited_at is not None else handoff.body.strip()
            answers.append({"id": comment.id, "url": comment.url, "prose": prose})
        return answers

    def _wake_comment_problem(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reason: str,
        outcome: str,
    ) -> None:
        if not self._trusted_bots(comment):
            return
        detail = {
            "project_bot": ticket.grok_bot,
            "outcome": outcome,
            "comment": _comment_link(comment),
        }
        note = (
            RETRY_REJECTION_NOTES.get(outcome)
            or REBASE_REJECTION_NOTES.get(outcome)
            or APPROVE_REJECTION_NOTES.get(outcome)
        )
        if outcome in _VERDICT_AUTHOR_REJECTIONS:
            try:
                sender = parse_comment(comment.body).as_dict().get("from")
            except HandoffError:
                sender = None
            note = (
                f"expected a verdict from {ticket.factory.reviewer_harness}-bot (reviewer of"
                f" record); this one is from={sender} by login {comment.author}"
            )
        if note is not None:
            detail["note"] = note
        self.wake_grok_once(
            ticket,
            reason=reason,
            source_comment_id=comment.id,
            detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
            head=ticket.current_head,
        )

    def _project_wake_detail(
        self,
        ticket: TrackedTicket,
        detail: str,
        *,
        question: str | None = None,
    ) -> str:
        return json.dumps(
            {
                "project_bot": ticket.grok_bot,
                "detail": detail,
            }
            | ({} if question is None else {"question": question}),
            separators=(",", ":"),
            sort_keys=True,
        )

    def _wake_done(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        outcome: str,
        *,
        note: str | None = None,
    ) -> None:
        detail: dict[str, object] = {
            "project_bot": ticket.grok_bot,
            "follow_up_owner": ticket.grok_bot,
            "outcome": outcome,
            "pr_number": ticket.pr_number,
            "post_to": ticket.ticket_url,
        }
        if outcome == "handed-off" and (stacked := ticket.stacked_on) is not None:
            parent = f"Stacked on #{stacked.pr}. Merge #{stacked.pr} first."
            note = f"{parent} {note}" if note else parent
        if note is not None:
            detail["note"] = note
        self._hold_done_wake(ticket, comment, detail)
        self._park_followups(ticket)


_VERDICT_AUTHOR_REJECTIONS = frozenset(
    {"rejected-approve-author", "rejected-revise-author", "rejected-blocked-author"}
)

def _skip_marker(reason: str, once_key: str) -> str:
    return f"<!-- orch-wake-skip {reason}:{once_key} -->"


def _skip_notice(reason: str, once_key: str, detail: str) -> str:
    parsed = json.loads(detail)
    facts = "".join(
        f"- {name}: {' '.join(str(parsed[name]).split())}\n"
        for name in ("outcome", "note", "detail", "link")
        if parsed.get(name)
    )
    return (
        f"Factory notice: `{reason}` needs no orchestrator decision, "
        "so the poller did not wake the orchestrator.\n\n"
        f"{facts}\n{_skip_marker(reason, once_key)}"
    )


def _verdict_sources(
    connection: sqlite3.Connection, ticket: TrackedTicket, comment: Comment
) -> list[Comment]:
    accepted = {
        row[0]
        for row in connection.execute(
            """
            SELECT comment_id FROM review_events WHERE lower(repo) = ? AND ticket_id = ?
            UNION SELECT comment_id FROM ship_intents WHERE lower(repo) = ? AND ticket_id = ?
            UNION SELECT approve_comment_id FROM ship_holds
                WHERE lower(repo) = ? AND ticket_id = ?
            UNION SELECT source_comment_id FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
            """,
            (ticket.repo.lower(), ticket.ticket_id) * 4,
        )
    }
    return [item for item in ticket.comments if item.id == comment.id or item.id in accepted]


def _routing_payload(ticket: TrackedTicket) -> dict[str, object]:
    payload: dict[str, object] = {
        "builder": ticket.factory.builder_harness,
        "reviewer": ticket.factory.reviewer_harness,
    }
    if (picks := ticket.router_picks) is not None:
        sides = [("builder", picks.builder), ("reviewer", picks.reviewer)]
        if picks.planner is not None:
            payload["planner"] = picks.planner.harness
            sides.append(("plan", picks.planner))
        for side, pick in sides:
            if pick.model is not None:
                payload[f"{side}_model"] = pick.model
            if pick.effort is not None:
                payload[f"{side}_effort"] = pick.effort
        if picks.sample is not None:
            payload["sample"] = sample_json(picks.sample)
    return payload


def _review_for_reviewer_of_record(ticket: TrackedTicket, request: JobRequest) -> JobRequest:
    detail = json.loads(request.detail or "{}")
    detail["reviewer_harness"] = ticket.factory.reviewer_harness
    if ticket.factory_md is not None:
        detail["routing"] = _routing_payload(ticket)
    return replace(
        request,
        recipient=ticket.factory.reviewer_harness,
        detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
    )


def _accepted_reviewers(connection: sqlite3.Connection, ticket: TrackedTicket) -> frozenset[str]:
    return frozenset(
        {f"{ticket.factory.reviewer_harness}-bot"}
        | {
            f"{row[0]}-bot"
            for row in connection.execute(
                """
                SELECT DISTINCT recipient FROM jobs
                WHERE lower(repo) = ? AND ticket_id = ? AND action = 'review'
                  AND handoff_comment_id IS NOT NULL
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            )
        }
    )


def _review_bases(details: list[str]) -> dict[int, str]:
    bases: dict[int, str] = {}
    for text in details:
        detail = json.loads(text)
        rebase = detail.get("rebase")
        number = _detail_round_number(text)
        if not isinstance(rebase, dict) or number is None:
            continue
        bases[number - 1] = str(rebase["reviewed_base"])
        bases[number] = str(rebase["base"])
    return bases
