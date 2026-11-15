"""Job detail, step enqueue and Grok wake delivery."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace

from lib.dispatch.constants import GROK_WAKE_REASONS
from lib.dispatch.helpers import (
    _ANSWERS,
    _REJECTED_OUTCOMES,
    _comment_link,
    _detail_round_number,
    _grok_logical_key,
    _scope,
)
from lib.dispatch.types import Comment, TrackedTicket
from lib.grok_delivery import GrokDeliveryError
from lib.queue import Job, JobRequest


class DetailMixin:
    def _deliver_grok_jobs(self, deferred: set[str]) -> None:
        while (
            job := self.queue.claim(
                "grok",
                excluded_job_ids=deferred,
            )
        ) is not None:
            try:
                receipt = self.grok_delivery.deliver(
                    job,
                    logical_key=_grok_logical_key(job),
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

    def wake_grok_once(
        self,
        *,
        repo: str,
        ticket_id: str,
        reason: str,
        source_comment_id: str,
        timeout_seconds: int,
        detail: str,
        head: str | None = None,
    ) -> None:
        """T7: one Grok wake per stop, across restarts.

        refused wakes once per ticket. blocked and capped wake once per
        ticket and head. unparsed, ship-failed and go-not-from-approver wake
        once per comment.
        stuck goes through the ticket's stuck flag, which wakes once.
        """
        if reason not in GROK_WAKE_REASONS:
            raise ValueError(f"unknown wake reason {reason!r}")
        if reason in ("unparsed", "ship-failed", "rejected", "stale", "go-not-from-approver"):
            once_key = source_comment_id
        elif reason in ("blocked", "capped", "done", "awaiting-ok"):
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
        self.queue.enqueue(
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
            ),
            dedupe_key=f"t7:{_scope(repo, ticket_id)}:{reason}:{once_key}",
        )

    def _enqueue_or_flag(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
        *,
        dedupe_key: str,
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job, answered = self._enqueue_step(
                connection, ticket, comment, request, dedupe_key=dedupe_key
            )
            connection.commit()
        if answered is not None:
            return job, answered
        if job.request.source_comment_id == request.source_comment_id:
            self.clear_attention(ticket.repo, ticket.ticket_id, "dedupe-dropped")
        elif job.state not in ("queued", "running"):
            self.flag_attention(
                ticket.repo,
                ticket.ticket_id,
                "dedupe-dropped",
                f"{request.action} for comment {request.source_comment_id} matched "
                f"finished job {job.id} ({job.state}); nothing was queued",
            )
        return job, None

    def _enqueue_step(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
        *,
        dedupe_key: str,
    ) -> tuple[Job, Comment | None]:
        """Enqueue the step comment asks for in the caller's open transaction,
        and cancel it in that transaction when a later trusted comment in the
        read already answered it (#108). Every plan, build, fix and review a
        comment queues goes through here, so no claim sees an answered step
        queued. Returns the job and that answer.
        """
        request = self._with_evidence(connection, ticket, comment, request)
        job = self.queue.enqueue_in_transaction(connection, request, dedupe_key=dedupe_key)
        if request.action not in _ANSWERS:
            return job, None
        answered = self._answered_by(
            ticket,
            comment,
            request.action,
            request.recipient,
            head=request.head,
            round_number=_detail_round_number(request.detail),
        )
        if answered is not None:
            self._cancel_answered(connection, job, request, answered)
        return job, answered

    def _with_evidence(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        comment: Comment,
        request: JobRequest,
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
        handled = dict(
            connection.execute(
                """
                SELECT comment_id, outcome FROM handled_comments
                WHERE repo = ? AND ticket_id = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
        )
        findings = findings_evidence(
            comments=[
                item
                for item in ticket.comments
                if item.id == comment.id
                or (item.id in handled and not handled[item.id].startswith(_REJECTED_OUTCOMES))
            ],
            trusted_logins=self.trusted_logins,
            reviewer=f"{ticket.factory.reviewer_harness}-bot",
            current_round=_detail_round_number(request.detail) or 1,
            bases={},
        )
        result = ticket.ci.get(request.head or "")
        ci = None
        if result is not None and result.status == "failure":
            ci = ci_evidence(
                failed_checks=[
                    name
                    for name in (result.log or "").removeprefix("failed: ").split(", ")
                    if name
                ],
                run_url=None,
                fetch_log=str,
            )
        detail["evidence"] = {
            "criteria": asdict(
                criteria_evidence(
                    issue_key=ticket.source_issue or ticket.ticket_id,
                    issue_url=ticket.ticket_url,
                    comments=ticket.comments,
                    trusted_logins=self.trusted_logins,
                    issue_body=None,
                    pr_body=None,
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
            SELECT detail FROM jobs
            WHERE lower(repo) = ? AND ticket_id = ? AND action = ?
              AND source_comment_id != ?
              AND (? IS NULL OR handoff_comment_id = ?)
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (ticket.repo.lower(), ticket.ticket_id, action, comment.id, question_id, question_id),
        ).fetchone()
        return json.loads(row["detail"] or "{}") if row is not None else None

    def _job_detail(
        self,
        ticket: TrackedTicket,
        *,
        action: str,
        head: str | None = None,
        round_number: int | None = None,
        instruction: str | None = None,
        resume: bool = False,
        cap: int | None = None,
        kind: str | None = None,
    ) -> str:
        if round_number is not None and cap is None:
            cap = self._effective_cap(ticket)
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
            "post_to": (
                f"PR #{ticket.pr_number} issue comments (not reviews or inline comments)"
                if ticket.pr_number is not None
                else "the linked PR issue comments (not reviews or inline comments)"
            ),
            "action": action,
            "head": head,
            "project_bot": ticket.grok_bot,
        }
        if instruction is not None:
            context["instruction"] = instruction
        if ticket.factory_md is not None:
            context["routing"] = {
                "builder": ticket.factory.builder_harness,
                "reviewer": ticket.factory.reviewer_harness,
            }
        if kind is not None:
            context["kind"] = kind
        if resume:
            # Claim order (lib/queue.py): a resumed paused step goes before
            # bug and ordinary work, after urgent (issue #85).
            context["resume"] = True
        return json.dumps(context, separators=(",", ":"), sort_keys=True)

    def _wake_comment_problem(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        reason: str,
        outcome: str,
    ) -> None:
        if not self._trusted_bots(comment):
            return
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason=reason,
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=json.dumps(
                {
                    "project_bot": ticket.grok_bot,
                    "outcome": outcome,
                    "comment": _comment_link(comment),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            head=ticket.current_head,
        )

    def _project_wake_detail(
        self,
        ticket: TrackedTicket,
        detail: str,
    ) -> str:
        return json.dumps(
            {
                "project_bot": ticket.grok_bot,
                "detail": detail,
            },
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
            "outcome": outcome,
            "pr_number": ticket.pr_number,
            "post_to": ticket.ticket_url,
        }
        if note is not None:
            detail["note"] = note
        self.wake_grok_once(
            repo=ticket.repo,
            ticket_id=ticket.ticket_id,
            reason="done",
            source_comment_id=comment.id,
            timeout_seconds=ticket.job_timeout_minutes * 60,
            detail=json.dumps(
                detail,
                separators=(",", ":"),
                sort_keys=True,
            ),
            head=ticket.current_head,
        )
