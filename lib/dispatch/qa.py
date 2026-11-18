"""QA runs (#763): decide each head's QA scope, queue the qa jobs, keep qa_runs.

QaMixin is the only writer of qa_runs. decide_scope is pure: the caller
gathers the facts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lib.approver_commands import SlashQa
from lib.dispatch.constants import ORCHESTRATOR
from lib.dispatch.helpers import _comment_link, _scope
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import Comment, TrackedTicket
from lib.factory_md import QaConfig
from lib.feeder_gates import path_matches
from lib.handoff import HandoffError, parse_comment
from lib.queue import QA_RECIPIENT, Job, JobRequest, cancel_queued
from lib.update_check import HeadDiff

if TYPE_CHECKING:
    from lib.evidence.scenarios import LiveScenarios

OPEN_STATES = ("queued", "running")
_BLOCKING = re.compile(r"^\s*(?:[-*+]\s+)?(\[blocking\].*?)\s*$")
_EVIDENCE = re.compile(r"^\s*(?:[-*+]\s+)?Evidence:\s*(\S+)", re.MULTILINE)
_SCENARIO_ROW = re.compile(r"^\s*\|\s*(qa-[1-9][0-9]*)\s*\|(.*)\|\s*$")


@dataclass(frozen=True)
class Prior:
    run_id: str
    head: str
    result: str


@dataclass(frozen=True)
class ScopeInput:
    live_ids: tuple[str, ...] | None
    baseline: Prior | None
    same_patch_pass: Prior | None
    touched: bool
    lines: int | None
    full_rerun_lines: int
    unpassed: tuple[str, ...]


@dataclass(frozen=True)
class Run:
    scope: str
    scenario_ids: tuple[str, ...]


@dataclass(frozen=True)
class NoRun:
    reason: str
    result: str | None = None


def decide_scope(facts: ScopeInput) -> Run | NoRun:
    """The first match wins (docs/SPEC.md section 4.2.1)."""
    if facts.same_patch_pass is not None:
        return NoRun(f"carried:{facts.same_patch_pass.run_id}", "PASS")
    if facts.baseline is not None and not facts.touched:
        return NoRun(f"carried:{facts.baseline.run_id}", facts.baseline.result)
    if facts.live_ids is None:
        return Run("smoke", ()) if facts.touched else NoRun("qa-not-required")
    if facts.baseline is None or facts.lines is None or facts.lines > facts.full_rerun_lines:
        return Run("full", facts.live_ids)
    return Run("targeted", facts.unpassed or facts.live_ids)


@dataclass(frozen=True)
class Covered:
    pass


@dataclass(frozen=True)
class Failed:
    report_id: str


@dataclass(frozen=True)
class EnvFailed:
    failures: int


@dataclass(frozen=True)
class Running:
    pass


@dataclass(frozen=True)
class Missing:
    pass


QaStatus = Covered | Failed | EnvFailed | Running | Missing


@dataclass(frozen=True)
class Proceed:
    pass


@dataclass(frozen=True)
class Wait:
    outcome: str


@dataclass(frozen=True)
class FixFromQa:
    report_id: str


@dataclass(frozen=True)
class ReviewFix:
    report_id: str | None


@dataclass(frozen=True)
class EnsureQa:
    pass


@dataclass(frozen=True)
class RetryQa:
    pass


@dataclass(frozen=True)
class ParkQa:
    pass


@dataclass(frozen=True)
class CancelQa:
    pass


GateAction = Proceed | Wait | FixFromQa | ReviewFix | EnsureQa | RetryQa | ParkQa | CancelQa

ENV_RETRIES = 1


@dataclass(frozen=True)
class QaFix:
    link: str
    evidence_link: str | None
    failed: tuple[str, ...]


def decide_gate(verdict: str, status: QaStatus, *, waited_out: bool = False) -> GateAction:
    match verdict, status:
        case "APPROVE", Covered():
            return Proceed()
        case "APPROVE", Failed(report_id):
            return FixFromQa(report_id)
        case "APPROVE", EnvFailed(failures) if failures <= ENV_RETRIES:
            return RetryQa()
        case "APPROVE", EnvFailed():
            return ParkQa()
        case "APPROVE", Running():
            return Wait("approve-qa-running")
        case "APPROVE", Missing():
            return EnsureQa()
        case "REVISE", Failed(report_id):
            return ReviewFix(report_id)
        case "REVISE", Running() if not waited_out:
            return Wait("revise-qa-running")
        case "REVISE", _:
            return ReviewFix(None)
        case "BLOCKED", Running():
            return CancelQa()
        case "BLOCKED", _:
            return Proceed()
    raise ValueError(f"no QA gate row for {verdict!r}")


def _arms(qa: QaConfig) -> tuple[tuple[str, str], ...]:
    primary, *others = qa.runners
    arms = [(primary.name, "primary")]
    if qa.shootout:
        arms.extend((runner.name, "shootout") for runner in others)
    return tuple(arms)


def _under_paths(qa: QaConfig, diff: HeadDiff | None) -> tuple[bool, int | None]:
    if diff is None:
        return True, None
    changed = [lines for path, lines in diff.files if not qa.paths or path_matches(qa.paths, path)]
    return bool(changed), sum(changed)


class QaMixin:
    def _queue_qa(
        self, ticket: TrackedTicket, comment: Comment, *, head: str | None, review_round: int
    ) -> None:
        qa = ticket.factory.qa
        if qa is None or head is None:
            return
        try:
            self._decide_qa(ticket, qa, comment, head, review_round)
        except Exception as exc:
            self.record_poll_exception(ticket.repo, ticket.ticket_id, exc)

    def _decide_qa(
        self,
        ticket: TrackedTicket,
        qa: QaConfig,
        comment: Comment,
        head: str,
        review_round: int,
    ) -> None:
        key = (ticket.repo.lower(), ticket.ticket_id)
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM qa_runs WHERE repo = ? AND ticket_id = ? AND head = ? LIMIT 1",
                (*key, head),
            ).fetchone():
                return
            baseline = _prior(
                connection,
                "repo = ? AND ticket_id = ? AND head != ? AND result IN ('PASS', 'FAIL')",
                (*key, head),
            )
        diff = self._qa_head_diff(ticket, baseline.head if baseline else None, head)
        patch_id = diff.patch_id if diff is not None else None
        same_patch_pass = None
        if patch_id is not None:
            with self._connect() as connection:
                same_patch_pass = _prior(
                    connection,
                    "repo = ? AND ticket_id = ? AND head != ? AND result = 'PASS' AND patch_id = ?",
                    (*key, head, patch_id),
                )
        touched, lines = _under_paths(qa, diff)
        live = self._qa_live(ticket)
        live_ids = None if live is None else tuple(scenario.id for scenario in live.scenarios)
        decision = decide_scope(
            ScopeInput(
                live_ids=live_ids,
                baseline=baseline,
                same_patch_pass=same_patch_pass,
                touched=touched,
                lines=lines,
                full_rerun_lines=qa.full_rerun_lines,
                unpassed=self._qa_unpassed(ticket, live_ids or ()),
            )
        )
        generation = self._generation(ticket.repo, ticket.ticket_id)
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if isinstance(decision, NoRun):
                connection.execute(
                    """
                    INSERT INTO qa_runs (
                        run_id, repo, ticket_id, head, patch_id, round, scope, runner, arm,
                        state, result, reason, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'none', ?, 'primary', 'reported', ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex, *key, head, patch_id, review_round, qa.runners[0].name,
                        decision.result, decision.reason, now, now,
                    ),
                )
            else:
                for runner, arm in _arms(qa):
                    self._queue_qa_run(
                        connection, ticket, qa, comment, head, patch_id, review_round,
                        generation, decision, runner, arm, live,
                    )
            connection.commit()

    def _qa_head_diff(self, ticket: TrackedTicket, since: str | None, head: str) -> HeadDiff | None:
        if self.qa_diff is None:
            return None
        try:
            return self.qa_diff(ticket.repo, ticket.factory.base_branch, since, head)
        except Exception as exc:
            self.record_poll_error(ticket.repo, ticket.ticket_id, f"qa-diff:{type(exc).__name__}")
            return None

    def _queue_qa_run(
        self,
        connection: sqlite3.Connection,
        ticket: TrackedTicket,
        qa: QaConfig,
        comment: Comment,
        head: str,
        patch_id: str | None,
        review_round: int,
        generation: int,
        run: Run,
        runner: str,
        arm: str,
        live: LiveScenarios | None,
        *,
        attempt: int = 1,
    ) -> None:
        run_id = uuid.uuid4().hex
        retry = "" if attempt == 1 else f":attempt-{attempt}"
        chosen = [scenario for scenario in (live.scenarios if live else ()) if scenario.id in run.scenario_ids]
        job = self.queue.enqueue_in_transaction(
            connection,
            JobRequest(
                repo=ticket.repo,
                ticket_id=ticket.ticket_id,
                action="qa",
                recipient=QA_RECIPIENT,
                source_comment_id=comment.id,
                timeout_seconds=qa.timeout_minutes * 60,
                generation=generation,
                attempt=attempt,
                head=head,
                detail=json.dumps(
                    {
                        "action": "qa",
                        "head": head,
                        "scope": run.scope,
                        "runner": runner,
                        "arm": arm,
                        "scenarios": list(run.scenario_ids),
                        "live": [
                            {
                                "id": scenario.id,
                                "criterion": scenario.criterion,
                                "steps": scenario.steps,
                                "passes": scenario.passes,
                                "pack": scenario.pack,
                                "saves": list(scenario.saves),
                            }
                            for scenario in chosen
                        ],
                        "regression": [qa_id for qa_id in (live.regression if live else ()) if qa_id in run.scenario_ids],
                        "run_id": run_id,
                        "pr_number": ticket.pr_number,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
            dedupe_key=f"qa:{_scope(ticket.repo, ticket.ticket_id)}:{run.scope}:{head}:{runner}:{arm}{retry}",
        )
        connection.execute(
            """
            INSERT INTO qa_runs (
                run_id, repo, ticket_id, head, patch_id, round, attempt, scope, runner, arm,
                job_id, state, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                run_id, ticket.repo.lower(), ticket.ticket_id, head, patch_id, review_round,
                attempt, run.scope, runner, arm, job.id, self.queue.now(),
            ),
        )

    def _qa_unpassed(self, ticket: TrackedTicket, live_ids: tuple[str, ...]) -> tuple[str, ...]:
        with self._connect() as connection:
            reported = [
                row["comment_id"]
                for row in connection.execute(
                    """
                    SELECT comment_id FROM qa_runs
                    WHERE repo = ? AND ticket_id = ? AND arm = 'primary'
                      AND state = 'reported' AND comment_id IS NOT NULL
                    ORDER BY finished_at DESC, rowid DESC
                    """,
                    (ticket.repo.lower(), ticket.ticket_id),
                )
            ]
        bodies = {comment.id: comment.body for comment in ticket.comments}
        latest: dict[str, bool] = {}
        for comment_id in reported:
            for qa_id, passed in _scenario_rows(bodies.get(comment_id, "")):
                latest.setdefault(qa_id, passed)
        return tuple(qa_id for qa_id in live_ids if not latest.get(qa_id, False))

    def _qa_live(self, ticket: TrackedTicket) -> LiveScenarios | None:
        from lib.evidence.scenarios import InvalidSection, live_scenarios

        found = live_scenarios(
            [comment for comment in ticket.comments if self._trusted_builder_post(ticket, comment)]
        )
        if isinstance(found, InvalidSection) or not found.scenarios:
            return None
        return found

    def _trusted_builder_post(self, ticket: TrackedTicket, comment: Comment) -> bool:
        try:
            fields = parse_comment(comment.body).as_dict()
        except HandoffError:
            return False
        return fields["kind"] in ("plan", "ready") and self._trusted(
            ticket, comment, fields, f"{ticket.factory.builder_harness}-bot", role="builder"
        )

    def _qa_record(
        self, ticket: TrackedTicket, comment: Comment, fields: dict[str, str]
    ) -> str:
        if not (
            fields.get("from") == ORCHESTRATOR
            and fields.get("role", "qa") == "qa"
            and ORCHESTRATOR in self._trusted_bots(comment)
            and self._app_may_post(comment, fields)
        ):
            return "qa-untrusted"
        head = fields["head"]
        current = head == ticket.current_head
        states = OPEN_STATES if current else (*OPEN_STATES, "cancelled")
        marks = ", ".join("?" for _ in states)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT run_id FROM qa_runs
                WHERE repo = ? AND ticket_id = ? AND head = ? AND runner = ? AND arm = ?
                  AND state IN ({marks})
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id, head, fields["runner"], fields["arm"], *states),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE qa_runs
                    SET state = ?, result = ?, comment_id = ?, finished_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        "reported" if current else "stale",
                        fields["result"],
                        comment.id,
                        self.queue.now(),
                        row["run_id"],
                    ),
                )
            connection.commit()
        if not current:
            return "qa-stale"
        return "qa-reported" if row is not None else "qa-unmatched"

    def _qa_ruling(self, ticket: TrackedTicket, comment: Comment, command: SlashQa) -> str:
        target, args = "arm = 'primary'", ()
        if command.runner is not None:
            target, args = "lower(runner) = ?", (command.runner,)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT run_id FROM qa_runs
                WHERE repo = ? AND ticket_id = ? AND head = ? AND state = 'reported'
                  AND comment_id IS NOT NULL AND {target}
                ORDER BY finished_at DESC, rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id, ticket.current_head, *args),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE qa_runs SET ruling = ?, ruled_by = ?, ruled_at = ?, ruling_reason = ?
                    WHERE run_id = ?
                    """,
                    (command.ruling, comment.author, self.queue.now(), command.reason, row["run_id"]),
                )
            connection.commit()
        return "qa-ruling-no-run" if row is None else "qa-ruled"

    def _qa_follow(self, ticket: TrackedTicket) -> None:
        if ticket.factory.qa is None:
            return
        key = (ticket.repo.lower(), ticket.ticket_id)
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT qa_runs.run_id, qa_runs.head, qa_runs.state, jobs.id AS job_id,
                       jobs.state AS job_state
                FROM qa_runs JOIN jobs ON jobs.id = qa_runs.job_id
                WHERE qa_runs.repo = ? AND qa_runs.ticket_id = ?
                  AND qa_runs.state IN ('queued', 'running')
                """,
                key,
            ).fetchall()
            for row in rows:
                state, reason = _followed(row, ticket.current_head)
                if (state, reason) == (row["state"], None):
                    continue
                if reason == "new-head" and row["job_state"] == "queued":
                    cancel_queued(connection, row["job_id"], "qa-new-head", now)
                connection.execute(
                    """
                    UPDATE qa_runs SET state = ?, reason = ?, finished_at = ?
                    WHERE run_id = ?
                    """,
                    (state, reason, now if state == "cancelled" else None, row["run_id"]),
                )
            failed = connection.execute(
                """
                SELECT head, comment_id FROM qa_runs
                WHERE repo = ? AND ticket_id = ? AND arm = 'primary' AND state = 'reported'
                  AND result = 'FAIL' AND comment_id IS NOT NULL
                """,
                key,
            ).fetchall()
            connection.commit()
        if ticket.factory.qa.gate != "block":
            self._wake_on_qa_fail(ticket, failed)

    def _qa_gate(
        self, ticket: TrackedTicket, verdict: str, head: str, *, since: float | None = None
    ) -> GateAction:
        qa = ticket.factory.qa
        if qa is None or qa.gate != "block":
            return Proceed()
        waited_out = since is not None and self.queue.now() - since >= qa.timeout_minutes * 60
        return decide_gate(verdict, self._qa_status(ticket, head), waited_out=waited_out)

    def _qa_status(self, ticket: TrackedTicket, head: str) -> QaStatus:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state, result, reason, comment_id FROM qa_runs
                WHERE repo = ? AND ticket_id = ? AND head = ? AND arm = 'primary'
                ORDER BY rowid DESC
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchall()
            if not rows:
                return Missing()
            row = rows[0]
            report_id, carried = row["comment_id"], row["reason"] or ""
            while row["result"] == "FAIL" and report_id is None and carried.startswith("carried:"):
                source = connection.execute(
                    "SELECT comment_id, reason FROM qa_runs WHERE run_id = ?",
                    (carried.removeprefix("carried:"),),
                ).fetchone()
                if source is None:
                    break
                report_id, carried = source["comment_id"], source["reason"] or ""
        match (row["state"], row["result"]):
            case ("queued" | "running", _):
                return Running()
            case ("reported", "PASS"):
                return Covered()
            case ("reported", None) if row["reason"] == "qa-not-required":
                return Covered()
            case ("reported", "FAIL"):
                return Failed(report_id or "")
            case _:
                return EnvFailed(sum(_env_failure(item) for item in rows))

    def _retry_qa(self, ticket: TrackedTicket, comment: Comment, head: str) -> None:
        qa = ticket.factory.qa
        assert qa is not None
        key = (ticket.repo.lower(), ticket.ticket_id)
        generation = self._generation(ticket.repo, ticket.ticket_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            last = connection.execute(
                """
                SELECT qa_runs.run_id, qa_runs.patch_id, qa_runs.round, qa_runs.attempt,
                       qa_runs.scope, qa_runs.runner, jobs.detail
                FROM qa_runs JOIN jobs ON jobs.id = qa_runs.job_id
                WHERE qa_runs.repo = ? AND qa_runs.ticket_id = ? AND qa_runs.head = ?
                  AND qa_runs.arm = 'primary'
                ORDER BY qa_runs.rowid DESC LIMIT 1
                """,
                (*key, head),
            ).fetchone()
            scenarios = tuple(json.loads(last["detail"]).get("scenarios") or ())
            connection.execute(
                """
                UPDATE qa_runs SET state = 'cancelled', reason = 'env-retry'
                WHERE run_id = ? AND state = 'reported'
                """,
                (last["run_id"],),
            )
            self._queue_qa_run(
                connection, ticket, qa, comment, head, last["patch_id"], int(last["round"]),
                generation, Run(last["scope"], scenarios), last["runner"], "primary",
                self._qa_live(ticket), attempt=int(last["attempt"]) + 1,
            )
            connection.commit()

    def _park_qa(self, ticket: TrackedTicket, comment: Comment, head: str) -> None:
        generation = self._generation(ticket.repo, ticket.ticket_id)
        with self._connect() as connection:
            write_workflow_state(
                connection,
                ticket.repo,
                ticket.ticket_id,
                "waiting-max",
                source="waiting-max-qa",
                at=self.queue.now(),
                also={
                    "paused_action": "qa",
                    "paused_recipient": QA_RECIPIENT,
                    "paused_head": head,
                    "paused_generation": generation,
                    "paused_question_id": comment.id,
                },
                guard="workflow_state IN ('review', 'awaiting-ok', 'fixing', 'merge-queue', 'waiting-external')",
            )
            connection.commit()
        self.wake_grok_once(
            ticket,
            reason="qa-env-failed",
            source_comment_id=comment.id,
            detail=self._project_wake_detail(
                ticket,
                f"waiting-max-qa: QA at {head} failed on its environment {ENV_RETRIES + 1} times; "
                f"the APPROVE {_comment_link(comment)} waits for Max",
            ),
            head=head,
            once_key=head,
        )
        self._write_state_label(ticket, "waiting-max")

    def _cancel_open_qa(self, ticket: TrackedTicket, reason: str) -> None:
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT qa_runs.run_id, jobs.id AS job_id, jobs.state AS job_state
                FROM qa_runs LEFT JOIN jobs ON jobs.id = qa_runs.job_id
                WHERE qa_runs.repo = ? AND qa_runs.ticket_id = ?
                  AND qa_runs.state IN ('queued', 'running')
                """,
                (ticket.repo.lower(), ticket.ticket_id),
            ).fetchall()
            for row in rows:
                if row["job_state"] == "queued":
                    cancel_queued(connection, row["job_id"], f"qa-{reason}", now)
                connection.execute(
                    "UPDATE qa_runs SET state = 'cancelled', reason = ?, finished_at = ? WHERE run_id = ?",
                    (reason, now, row["run_id"]),
                )
            connection.commit()

    def _qa_fix(self, ticket: TrackedTicket, report_id: str) -> QaFix:
        report = next((comment for comment in ticket.comments if comment.id == report_id), None)
        if report is None:
            return QaFix(f"comment:{report_id}", None, ())
        evidence = _EVIDENCE.search(report.body)
        return QaFix(
            _comment_link(report),
            evidence.group(1) if evidence else None,
            tuple(qa_id for qa_id, passed in _scenario_rows(report.body) if not passed),
        )

    def _wake_on_qa_fail(self, ticket: TrackedTicket, failed: list[sqlite3.Row]) -> None:
        comments = {comment.id: comment for comment in ticket.comments}
        for row in failed:
            report = comments.get(row["comment_id"])
            if report is None:
                continue
            blocking = _first_blocking(report.body)
            if blocking is None or not self._approved_head(ticket, row["head"]):
                continue
            evidence = _EVIDENCE.search(report.body)
            self.wake_grok_once(
                ticket,
                reason="qa-failed",
                source_comment_id=report.id,
                detail=json.dumps(
                    {
                        "project_bot": ticket.grok_bot,
                        "head": row["head"],
                        "finding": blocking,
                        "evidence_link": evidence.group(1) if evidence else None,
                        "link": _comment_link(report),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                head=row["head"],
                once_key=report.id,
            )

    def _approved_head(self, ticket: TrackedTicket, head: str) -> bool:
        for comment in ticket.comments:
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if (
                fields["kind"] == "verdict"
                and fields.get("verdict") == "APPROVE"
                and fields.get("head") == head
                and self._trusted_reviewer(ticket, comment, fields)
            ):
                return True
        return False

    def _record_unhandled_qa(self, ticket: TrackedTicket) -> None:
        for comment in ticket.comments:
            if comment.edited_at is not None or self._is_handled(ticket, comment.id):
                continue
            try:
                fields = parse_comment(comment.body).as_dict()
            except HandoffError:
                continue
            if fields["kind"] == "qa":
                self._mark_handled(ticket, comment.id, self._qa_record(ticket, comment, fields))

    def _held_qa(self, ticket: TrackedTicket, head: str) -> dict[str, object] | None:
        qa = ticket.factory.qa
        assert qa is not None
        self._record_unhandled_qa(ticket)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, scope, state, result, reason, comment_id, started_at
                FROM qa_runs
                WHERE repo = ? AND ticket_id = ? AND head = ? AND arm = 'primary'
                ORDER BY rowid DESC LIMIT 1
                """,
                (ticket.repo.lower(), ticket.ticket_id, head),
            ).fetchone()
        if row is None:
            return {"result": None, "reason": "no-qa-run", "summary": "No QA run: no-qa-run."}
        if row["scope"] == "none":
            return {
                "result": row["result"],
                "reason": row["reason"],
                "summary": f"No QA run: {row['reason']}.",
            }
        if row["state"] in OPEN_STATES and self.queue.now() < (
            row["started_at"] + qa.timeout_minutes * 60
        ):
            return None
        if row["state"] == "reported":
            report = next(
                (comment for comment in ticket.comments if comment.id == row["comment_id"]),
                Comment(id=row["comment_id"], author="", body="", created_at=0.0),
            )
            rows = _scenario_rows(report.body)
            passed = sum(verified for _qa_id, verified in rows)
            evidence = _EVIDENCE.search(report.body)
            link = evidence.group(1) if evidence else _comment_link(report)
            return {
                "result": row["result"],
                "passed": passed,
                "total": len(rows),
                "evidence_link": link,
                "summary": (
                    f"QA {row['result']}: {passed} of {len(rows)} scenarios passed. "
                    f"Evidence: {link}"
                ),
            }
        reason = row["reason"] if row["state"] == "cancelled" and row["reason"] else "timeout"
        return {
            "result": None,
            "reason": reason,
            "summary": f"QA did not finish ({reason}).",
        }

    def _qa_job_failed(self, job: Job) -> None:
        now = self.queue.now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE qa_runs SET state = 'cancelled', reason = 'job-failed', finished_at = ?
                WHERE job_id = ? AND state IN ('queued', 'running')
                """,
                (now, job.id),
            )
            connection.commit()
        self._record_job_event_once(job, "qa-no-retry", "a qa job is never retried")
        self.queue.mark_failure_handled(job.id)


def _followed(row: sqlite3.Row, current_head: str | None) -> tuple[str, str | None]:
    if current_head is not None and row["head"] != current_head:
        return "cancelled", "new-head"
    if row["job_state"] == "cancelled":
        return "cancelled", "job-cancelled"
    if row["job_state"] in ("running", "queued"):
        return row["job_state"], None
    return row["state"], None


def record_runner_moved(connection: sqlite3.Connection, run_id: str, runner: str) -> None:
    connection.execute("UPDATE qa_runs SET runner = ? WHERE run_id = ?", (runner, run_id))


def _prior(connection: sqlite3.Connection, where: str, args: tuple) -> Prior | None:
    row = connection.execute(
        f"""
        SELECT run_id, head, result FROM qa_runs
        WHERE arm = 'primary' AND state = 'reported' AND {where}
        ORDER BY finished_at DESC, rowid DESC LIMIT 1
        """,
        args,
    ).fetchone()
    return None if row is None else Prior(row["run_id"], row["head"], row["result"])


def _scenario_rows(body: str) -> list[tuple[str, bool]]:
    rows = []
    for line in body.splitlines():
        match = _SCENARIO_ROW.match(line)
        if match:
            cells = [cell.strip() for cell in match.group(2).split("|")]
            rows.append((match.group(1), "Verified" in cells))
    return rows


def _first_blocking(body: str) -> str | None:
    for line in body.splitlines():
        match = _BLOCKING.match(line)
        if match:
            return match.group(1)
    return None


def _env_failure(row: sqlite3.Row) -> bool:
    return row["result"] == "BLOCKED" or (
        row["state"] == "cancelled" and row["reason"] in ("job-failed", "job-cancelled")
    )
