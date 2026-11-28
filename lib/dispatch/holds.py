"""Hold gate and approver checks before ship."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from lib.dispatch.constants import (
    _NOT_FINISHED,
    OK_COVERED_STATES,
    OK_UNREAD,
    OK_WITHOUT_APPROVE,
    TERMINAL_STATES,
    OkReading,
)
from lib.dispatch.helpers import (
    _base_update_key,
    _comment_link,
    _is_full_sha,
    _matching_head,
    _round_number,
    _scope,
    ship_mode,
)
from lib.dispatch.ok_note import HeldStop, LatestVerdict, no_hold_body, ok_marker, other_head_body
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, ShipError, TrackedTicket
from lib.factory_md import DEFAULT_HOLD_APPROVER, DEFAULT_HOLD_LABEL, HoldConfig
from lib.handoff import parse_comment
from lib.hold import VISUAL_QA_REASON, HoldDecision, decide, ok_carries
from lib.queue import JobRequest, cancel_queued
from lib.rule_engine import (
    Escalate,
    HeadProbe,
    Land,
    MovedHeadFacts,
    Outcome,
    Wait,
    git_path,
    sha,
)
from lib.rule_engine import decide as engine_decide

_HELD_BASE_UPDATE = "held-base-update"
ALWAYS_HELD = ("supabase/migrations/**", ".github/**", "factory.md")


def _ok_head(comment: Comment) -> str | None:
    head = parse_comment(comment.body).as_dict().get("head")
    return head if head is not None and _is_full_sha(head) else None


class HoldsMixin:
    def _effective_hold_and_approvers(
        self, ticket: TrackedTicket
    ) -> tuple[HoldConfig, tuple[str, ...]]:
        if ticket.factory.configured_hold_applies:
            hold = ticket.factory.hold
            assert hold is not None
            return hold, (hold.approver,)
        assert ticket.factory.visual_hold_applies
        if self.approvals.app_trust:
            candidates = tuple(sorted(self.approvals.logins))
        elif self.approvals.logins:
            candidates = tuple(
                sorted(login for login in self.approvals.logins if self._trusted_login(login))
            )
        else:
            candidates = tuple(sorted(self.trusted_logins.get("github", ())))
        primary = candidates[0] if candidates else DEFAULT_HOLD_APPROVER
        return HoldConfig(label=DEFAULT_HOLD_LABEL, approver=primary, paths=()), candidates

    def _hold_gate(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
    ) -> tuple[bool, str] | None:
        """Issue #86: None lets T4 merge; otherwise the APPROVE stays
        unhandled and T4 re-checks it on every poll, at whatever the head
        then is. A held PR moves to awaiting-ok once per head and wakes
        Grok Bot once per head with the PR and head."""
        hold, _approvers = self._effective_hold_and_approvers(ticket)
        decision = self._hold_decision(ticket, head)
        if decision.unread is not None:
            return False, "hold-unread"
        if not decision.blocks:
            return None
        if ticket.mergeable_state in ("dirty", "behind"):
            return self._hold_base_update(ticket, comment, head)
        scope = (ticket.repo.lower(), ticket.ticket_id)
        reasons = ",".join(decision.reasons)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT head, approver, reasons FROM ship_holds
                WHERE repo = ? AND ticket_id = ?
                """,
                scope,
            ).fetchone()
        state, _round = self._review_context(ticket)
        stored = None if row is None else (row["head"], row["approver"], row["reasons"])
        if state != "awaiting-ok" or stored != (head, hold.approver, reasons):
            if state != "awaiting-ok":
                self._write_state_label(ticket, "awaiting-ok")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO ship_holds (
                        repo, ticket_id, head, approver, reasons,
                        approve_comment_id, held_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo, ticket_id) DO UPDATE SET
                        head = excluded.head,
                        approver = excluded.approver,
                        reasons = excluded.reasons,
                        approve_comment_id = excluded.approve_comment_id,
                        held_at = CASE WHEN ship_holds.head = excluded.head
                            THEN ship_holds.held_at ELSE excluded.held_at END
                    """,
                    (
                        *scope,
                        head,
                        hold.approver,
                        reasons,
                        comment.id,
                        self.queue.now(),
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO verdict_acceptances VALUES (?, ?, ?, ?)",
                    (*scope, comment.id, self.queue.now()),
                )
                write_workflow_state(
                    connection,
                    ticket.repo,
                    ticket.ticket_id,
                    "awaiting-ok",
                    source="hold-gate",
                    at=self.queue.now(),
                )
                connection.commit()
        detail = {
            "project_bot": ticket.grok_bot,
            "outcome": "awaiting-ok",
            "pr": ticket.ticket_url or ticket.pr_number,
            "pr_number": ticket.pr_number,
            "post_to": ticket.ticket_url,
            "head": head,
            "approver": hold.approver,
            "reasons": list(decision.reasons),
            "held_paths": list(decision.paths),
            "label": hold.label,
            "clears_with": (
                f"<!-- orch:v1 kind=ok head={head} --> from "
                f"{hold.approver}"
                + (
                    f", or {hold.approver} removing {hold.label}"
                    if decision.reasons == ("label",)
                    else ""
                )
            ),
        }
        if ticket.factory.qa is not None:
            qa = self._held_qa(ticket, head, VISUAL_QA_REASON in decision.reasons)
            if qa is None:
                return False, "awaiting-ok"
            detail["qa"] = qa
        self.wake_grok_once(
            ticket,
            reason="awaiting-ok",
            source_comment_id=comment.id,
            head=head,
            detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
        )
        return False, "awaiting-ok"

    def _hold_base_update(
        self,
        ticket: TrackedTicket,
        comment: Comment,
        head: str,
    ) -> tuple[bool, str]:
        _state, review_round = self._review_context(ticket)
        base = ticket.factory.base_branch
        source = self._latest_fix_answer(ticket, comment, head)
        request = self._base_update_request(
            ticket,
            source,
            head,
            review_round,
            f"The approved PR is {ticket.mergeable_state} against {base} while "
            f"it waits for an ok: merge {base} in.",
        )
        out = self._base_update_out(ticket)
        job = None
        ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not out:
                job, _answered = self._enqueue_step(
                    connection,
                    ticket,
                    source,
                    request,
                    dedupe_key=_base_update_key(ticket, head),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO review_events (
                    repo, ticket_id, comment_id, outcome
                ) VALUES (?, ?, ?, ?)
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id, _HELD_BASE_UPDATE),
            )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "fixing",
                source="held-base-update",
                at=self.queue.now(),
                guard=_NOT_FINISHED,
            )
            if job is not None:
                self._flag_dropped(connection, ticket, request, job)
            connection.commit()
        self._write_state_label(ticket, "fixing")
        return True, _HELD_BASE_UPDATE

    def _replay_held_base_update(
        self, ticket: TrackedTicket, comment: Comment
    ) -> tuple[bool, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM review_events
                WHERE repo = ? AND ticket_id = ? AND comment_id = ?
                  AND outcome = ?
                """,
                (ticket.repo.lower(), ticket.ticket_id, comment.id, _HELD_BASE_UPDATE),
            ).fetchone()
        if row is None:
            return None
        self._write_state_label(ticket, self._review_context(ticket)[0])
        return True, _HELD_BASE_UPDATE

    def _resume_after_hold(
        self, ticket: TrackedTicket, comment: Comment
    ) -> tuple[bool, str] | None:
        """An approver's `kind=go state=building` on a held PR (`awaiting-ok`)
        clears the hold, supersedes the held APPROVE, and sends the PR back to
        the builder with the approver's feedback (E4-1 Request Changes)."""
        if not self._may_approve_go(comment.author):
            return None
        scope = (ticket.repo.lower(), ticket.ticket_id)
        dedupe_key = f"hold-changes:{_scope(ticket.repo, ticket.ticket_id)}:{comment.id}"
        ticket = self._fresh_issue(ticket)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            row = connection.execute(
                """
                SELECT workflow_state, review_round, generation
                FROM ticket_state WHERE repo = ? AND ticket_id = ?
                """,
                scope,
            ).fetchone()
            if existing is not None:
                state = str(row["workflow_state"]) if row is not None and row["workflow_state"] else "building"
                connection.commit()
                self._write_state_label(ticket, state)
                return True, "queued-hold-changes"
            if row is None or row["workflow_state"] != "awaiting-ok":
                connection.commit()
                return None
            hold = connection.execute(
                "SELECT approve_comment_id FROM ship_holds WHERE repo = ? AND ticket_id = ?",
                scope,
            ).fetchone()
            now = self.queue.now()
            if hold is not None and hold["approve_comment_id"]:
                approve_id = str(hold["approve_comment_id"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO handled_comments (
                        repo, ticket_id, comment_id, outcome
                    ) VALUES (?, ?, ?, 'superseded-hold-changes')
                    """,
                    (*scope, approve_id),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO handled_times VALUES (?, ?, ?, ?)",
                    (*scope, approve_id, now),
                )
            connection.execute(
                "DELETE FROM ship_holds WHERE repo = ? AND ticket_id = ?",
                scope,
            )
            open_qa = connection.execute(
                """
                SELECT qa_runs.run_id, jobs.id AS job_id, jobs.state AS job_state
                FROM qa_runs LEFT JOIN jobs ON jobs.id = qa_runs.job_id
                WHERE qa_runs.repo = ? AND qa_runs.ticket_id = ?
                  AND qa_runs.state IN ('queued', 'running')
                """,
                scope,
            ).fetchall()
            for qa_row in open_qa:
                if qa_row["job_state"] == "queued" and qa_row["job_id"] is not None:
                    cancel_queued(connection, str(qa_row["job_id"]), "qa-hold-changes", now)
                connection.execute(
                    """
                    UPDATE qa_runs SET state = 'cancelled', reason = 'hold-changes', finished_at = ?
                    WHERE run_id = ?
                    """,
                    (now, qa_row["run_id"]),
                )
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "building",
                source="hold-changes",
                at=now,
                also={
                    "approved_head": None,
                    "approved_at": None,
                    "approve_comment_id": None,
                },
            )
            self._enqueue_step(
                connection,
                ticket,
                comment,
                JobRequest(
                    repo=ticket.repo,
                    ticket_id=ticket.ticket_id,
                    action="build",
                    recipient=ticket.factory.builder_harness,
                    source_comment_id=comment.id,
                    timeout_seconds=ticket.job_timeout_minutes * 60,
                    generation=int(row["generation"]),
                    head=ticket.current_head,
                    detail=self._job_detail(
                        ticket,
                        action="build",
                        head=ticket.current_head,
                        round_number=int(row["review_round"]) + 1,
                        instruction=_comment_link(comment),
                        resume=True,
                        cap=self._effective_cap(ticket, connection),
                        connection=connection,
                    ),
                ),
                dedupe_key=dedupe_key,
            )
            connection.commit()
        self._write_state_label(ticket, "building")
        return True, "queued-hold-changes"

    def _rule_on_moved_head(self, ticket: TrackedTicket, ok: str, head: str) -> Outcome:
        try:
            ok_head, new_head = sha(ok), sha(head)
        except ValueError:
            return Escalate("bad-sha")
        hold = ticket.factory.effective_hold
        assert hold is not None
        result = ticket.ci.get(head)
        ci = "unknown" if result is None else result.status
        moved = self._moved_head(ticket, ok, head)
        try:
            held = tuple(git_path(pattern) for pattern in (*hold.paths, *ALWAYS_HELD))
            probe = None if moved is None else HeadProbe(
                parents=tuple(sha(parent) for parent in moved.parents),
                second_parent_on_base=moved.second_parent_on_base,
                remerge_empty=moved.remerge_empty,
                changed_paths=tuple(git_path(path) for path in sorted(moved.paths)),
                scan=moved.scan.status,
            )
        except ValueError:
            held, probe = (), None
        ruling = engine_decide(MovedHeadFacts(ok_head, new_head, probe, held, ci))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.review.log_ruling(connection, ticket, "moved-head", f"{ok}..{head}", ruling, ())
            connection.commit()
        return ruling.outcome

    def _hold_decision(self, ticket: TrackedTicket, head: str) -> HoldDecision:
        return self._read_hold(ticket, head)[0]

    def _read_hold(
        self, ticket: TrackedTicket, head: str, ok_head: str | None = None
    ) -> tuple[HoldDecision, bool, bool]:
        hold, approvers = self._effective_hold_and_approvers(ticket)
        allowed_approvers = tuple(login for login in approvers if self._may_approve(login))
        visual = self._visual_qa_hold(ticket)
        carry_paths = (*hold.paths, "**") if visual else hold.paths
        waited = False
        coverage_unread = False

        def covers(ok_head: str) -> bool:
            nonlocal waited, coverage_unread
            if ticket.factory.rule_engine != "on":
                if self._approval_covers(ticket, ok_head, head):
                    return True
                carries = self._ok_carries(ticket, ok_head, head, carry_paths)
                coverage_unread = coverage_unread or carries is None
                return carries is True
            if _matching_head(ok_head, head) is not None:
                return True
            outcome = self._rule_on_moved_head(ticket, ok_head, head)
            waited = waited or isinstance(outcome, Wait)
            coverage_unread = coverage_unread or (
                outcome == Escalate("probe-unknown")
                and self.moved_head is not None
                and self._moved_head(ticket, ok_head, head) is None
            )
            return isinstance(outcome, Land)

        decision = decide(
            hold,
            labels=ticket.labels,
            changed_files=ticket.changed_files,
            label_events=ticket.label_events,
            factory_md_trust_changed=ticket.factory_md_trust_changed,
            comments=ticket.comments,
            approver_allowed=bool(allowed_approvers),
            covers=covers,
            check_configured_hold=ticket.factory.configured_hold_applies,
            visual_qa_hold=visual,
            approvers=allowed_approvers,
        )
        if waited and decision.blocks and decision.unread is None:
            decision = replace(decision, unread="moved-head ci")
        if ok_head is None or not decision.reasons:
            return decision, coverage_unread, False
        waited = False
        covered = covers(ok_head)
        return decision, coverage_unread or waited, covered

    def _ok_carries(
        self, ticket: TrackedTicket, ok_head: str, head: str, paths: tuple[str, ...]
    ) -> bool | None:
        if self.hold_carry is None:
            return False
        base = ticket.factory.base_branch
        key = (ticket.repo.lower(), base, ok_head, head, paths)
        if key in self._hold_carries:
            return self._hold_carries[key]
        try:
            update = self.hold_carry(ticket.repo, base, ok_head, head)
            carries = ok_carries(update, paths)
        except Exception as exc:
            if self.log is not None:
                self.log(
                    f"hold ok carry unread: {ticket.repo}#{ticket.ticket_id}: "
                    f"{type(exc).__name__}"
                )
            return None
        self._hold_carries[key] = carries
        return carries

    def _held(self, ticket: TrackedTicket, head: str) -> HoldDecision | None:
        if not ticket.factory.hold_applies:
            return None
        decision = self._hold_decision(ticket, head)
        return decision if decision.blocks else None

    def _hold_in_play(self, ticket: TrackedTicket, head: str) -> bool:
        if not ticket.factory.hold_applies:
            return False
        decision = self._hold_decision(ticket, head)
        return decision.unread is not None or bool(decision.reasons)

    def _read_ok(
        self, ticket: TrackedTicket, comment: Comment
    ) -> OkReading | Literal["unread"]:
        _hold, approvers = self._effective_hold_and_approvers(ticket)
        if not (
            comment.author.casefold() in {login.casefold() for login in approvers}
            and self._may_approve(comment.author)
        ):
            return "rejected-hold-ok-author"
        if ticket.current_head is None:
            return OK_UNREAD
        decision, coverage_unread, covered = self._read_hold(
            ticket, ticket.current_head, _ok_head(comment)
        )
        if decision.unread is not None or coverage_unread:
            return OK_UNREAD
        if not decision.reasons:
            return "ok-no-hold"
        return "hold-ok" if covered else "hold-ok-other-head"

    def _note_ok(self, ticket: TrackedTicket, comment: Comment, outcome: OkReading) -> None:
        head = ticket.current_head
        if head is None:
            return
        actions = self._review_actions(ticket)
        if outcome == "hold-ok-other-head":
            body = other_head_body(comment, ok_head=_ok_head(comment), pr_head=head)
            actions.comment_once(ticket, body, ok_marker(comment.id))
            return
        state = self._review_context(ticket)[0]
        if outcome != "ok-no-hold" or state in OK_COVERED_STATES or state in TERMINAL_STATES:
            return
        latest = None
        if (found := self._latest_review_verdict(ticket)) is not None:
            verdict, fields = found
            number = _round_number(fields.get("round"))
            latest = LatestVerdict(
                _comment_link(verdict),
                fields.get("verdict", "?"),
                fields.get("round", "?")
                if number is None
                else f"{number}/{self._effective_cap(ticket)}",
            )
        stop = None
        if (held := self._held_stop(ticket)) is not None:
            stop = HeldStop(held[0], self._stop_disputed(ticket, held[1], held[0]))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stuck, stuck_reason FROM ticket_state WHERE repo = ? AND ticket_id = ?",
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchone()
        stuck = None
        if row is not None and row["stuck"]:
            stuck = row["stuck_reason"] or "unrecorded reason"
        body = no_hold_body(
            comment, pr_head=head, state=state, latest=latest, stop=stop, stuck=stuck
        )
        actions.comment_once(ticket, body, ok_marker(comment.id))
        with self._connect() as connection:
            self._flag_attention_once(
                connection,
                ticket.repo,
                ticket.ticket_id,
                OK_WITHOUT_APPROVE,
                f"ok {_comment_link(comment)} moved nothing; no APPROVE covers head {head}",
            )

    def _may_approve(self, login: str) -> bool:
        """A Max-only approval (#122): a trusted_logins key while the
        GitHub App switch is off, else a login in approver_logins."""
        if self.approvals.app_trust:
            return self.approvals.may_approve(login)
        return self._trusted_login(login)

    def _approver_go(self, comment: Comment, fields: Mapping[str, str]) -> bool:
        """With the switch on, an approver's go may lift a cap even when the
        approver is not trusted for grok-bot; it starts nothing else (#122)."""
        identity = (fields.get("from"), fields.get("role"))
        return (
            self.approvals.app_trust
            and identity in (("grok-bot", "orchestrator"), ("approver", "approver"))
            and self.approvals.may_approve(comment.author)
        )

    def _may_approve_go(self, author: str) -> bool:
        """A Linear author keeps today's rule: the app changes GitHub
        identity only."""
        if author.startswith("linear:") or not self.approvals.app_trust:
            return True
        return self.approvals.may_approve(author)

    def _may_lift_cap(self, author: str) -> bool:
        if author.startswith("linear:"):
            return self.approvals.may_approve_linear(author)
        return self._may_approve_go(author)

    def _trusted_login(self, login: str) -> bool:
        wanted = login.casefold()
        return any(
            key.casefold() == wanted for key in self.trusted_logins.get("github", {})
        )

    def _ship_head(self, ticket: TrackedTicket, head: str) -> None:
        """Ship exactly head in the repo's ship_mode, or raise. No fallback."""
        if not _is_full_sha(head):
            raise ShipError(f"ship head {head!r} is not a 40-character SHA")
        mode = ship_mode(ticket.factory.ship)
        if mode == "fast-forward":
            self._review_actions(ticket).fast_forward(
                ticket,
                branch=ticket.factory.base_branch,
                head=head,
            )
        elif mode == "squash":
            self._review_actions(ticket).merge(
                ticket,
                squash=True,
                match_head_commit=head,
            )
        else:
            raise ShipError(f"unknown ship mode {mode!r}")
        self._merged_repos.add(ticket.repo.lower())
