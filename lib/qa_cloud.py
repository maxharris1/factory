"""The QA worker and lane coordinator (#784): a fixed program, never a model session.

`orch qa-worker` calls QaWorker.tick on a timer. A tick claims queued `qa`
jobs under the per-runner lane cap, then advances every run: launch reserved
lanes, grant extra lanes while the cap has room, read results, apply the swarm
rules, and post one `kind=qa` comment when the run is decided. Every fact a
restarted worker needs is in the jobs, qa_runs and qa_lanes rows, so a fresh
worker carries on where the last one stopped. docs/SPEC.md section 4.2.2.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from lib import lane_limit
from lib.brief.qa import AFTER_SCREENSHOT, PASS_OUTPUT, LaneFacts, Scenario, pack_ids, render_qa_brief
from lib.dispatch.constants import ORCHESTRATOR
from lib.dispatch.qa import record_runner_moved
from lib.dispatch.schema import ensure_qa_lanes
from lib.factory_md import FactoryConfig, QaConfig
from lib.orch_yml import QaStorage
from lib.queue import QA_RECIPIENT, Job, SQLiteJobQueue, claim_order, job_from_row
from lib.qa_evidence import EvidenceRefused, collect, lane_dir, record_evidence_dir, recycle, run_dir, stored
from lib.qa_runners import Lane, Runner, RunnerOutage, UsageLimit
from lib.qa_storage import Storage, StorageError, links_for_lane

SliceKind = Literal["head", "regression", "base-check"]
Result = Literal["PASS", "FAIL", "BLOCKED"]

OPEN = ("launching", "running")
GAPS = ("gap", "timed-out")
FALLBACK_METHODS = ("fallback-playwright", "fallback-control-ui")
SCENARIO_RESULTS = ("verified", "failed", "inconclusive", "needs-human")
LANE_STATUSES = ("PASS", "ISSUES", "BLOCKED")
BACKOFF_SECONDS = 300
DEFAULT_MAX_CLOUD_LANES = 10
GIB = 2**30

_WORDS = {
    "verified": "Verified",
    "failed": "Failed",
    "inconclusive": "Inconclusive",
    "needs-human": "Needs human",
}
_ON_BASE = ("regression", "base-check")


@dataclass(frozen=True)
class Slice:
    index: int
    kind: SliceKind
    ids: tuple[str, ...]


@dataclass(frozen=True)
class LaneRow:
    lane_id: str
    run_id: str
    job_id: str
    slice: int
    kind: SliceKind
    lane_no: int
    scenarios: tuple[str, ...]
    commit: str
    base_commit: str
    runner: str
    agent_id: str | None
    agent_url: str | None
    state: str
    attempt: int
    reason: str | None
    result: dict | None
    started_at: float
    launch_called_at: float | None

    @classmethod
    def of(cls, row: sqlite3.Row) -> LaneRow:
        return cls(
            lane_id=row["lane_id"],
            run_id=row["run_id"],
            job_id=row["job_id"],
            slice=row["slice"],
            kind=row["kind"],
            lane_no=row["lane_no"],
            scenarios=tuple(json.loads(row["scenarios"])),
            commit=row["commit_sha"],
            base_commit=row["base_commit"],
            runner=row["runner"],
            agent_id=row["agent_id"],
            agent_url=row["agent_url"],
            state=row["state"],
            attempt=row["attempt"],
            reason=row["reason"],
            result=None if row["result"] is None else json.loads(row["result"]),
            started_at=row["started_at"],
            launch_called_at=row["launch_called_at"],
        )


@dataclass(frozen=True)
class Detail:
    head: str
    scope: str
    runner: str
    arm: str
    run_id: str
    pr: int
    ids: tuple[str, ...]
    live: Mapping[str, Mapping]
    regression: tuple[str, ...]

    @classmethod
    def of(cls, raw: Mapping) -> Detail:
        return cls(
            head=raw["head"],
            scope=raw["scope"],
            runner=raw["runner"],
            arm=raw["arm"],
            run_id=raw["run_id"],
            pr=int(raw["pr_number"]),
            ids=tuple(raw["scenarios"]),
            live={item["id"]: item for item in raw["live"]},
            regression=tuple(raw.get("regression") or ()),
        )


@dataclass(frozen=True)
class RunPlan:
    repo: str
    ticket: str
    detail: Detail
    factory: FactoryConfig
    qa: QaConfig
    evidence_root: Path
    base_commit: str
    slices: tuple[Slice, ...]


@dataclass(frozen=True)
class Decision:
    result: Result
    table: tuple[tuple[str, str, str], ...]
    findings: tuple[str, ...]
    gaps: tuple[str, ...]


def s_id(qa_id: str) -> str:
    return "s-" + qa_id.removeprefix("qa-")


def qa_id(scenario_id: str) -> str:
    return "qa-" + scenario_id.removeprefix("s-")


def save_name(phrase: str) -> str | None:
    name = re.sub(r"[^a-z0-9._-]+", "-", phrase.strip().lower()).strip("-.")[:96]
    if not name:
        return None
    return name if "." in name else f"{name}.png"


def plan_slices(ids: Sequence[str], regression: Sequence[str], lanes: int) -> tuple[Slice, ...]:
    if not ids:
        return ()
    rerun = tuple(item for item in ids if item in set(regression))
    with_regression = bool(rerun) and lanes >= 2
    count = min(lanes - with_regression, len(ids))
    size, extra = divmod(len(ids), count)
    slices: list[Slice] = []
    start = 0
    for index in range(count):
        end = start + size + (index < extra)
        slices.append(Slice(index, "head", tuple(ids[start:end])))
        start = end
    if with_regression:
        slices.append(Slice(count, "regression", rerun))
    return tuple(slices)


def rejection(lane: LaneRow, result: object, verify: str | None) -> str | None:
    if not isinstance(result, dict):
        return "not-an-object"
    expected = {
        "run_id": lane.run_id,
        "lane": lane.lane_no,
        "kind": lane.kind,
        "commit": lane.commit,
        "base_commit": lane.base_commit,
    }
    for key, value in expected.items():
        if type(result.get(key)) is not type(value) or result.get(key) != value:
            return f"wrong-{key.replace('_', '-')}"
    if result.get("method") not in (*((verify,) if verify else ()), *FALLBACK_METHODS):
        return "wrong-method"
    if result.get("status") not in LANE_STATUSES:
        return "wrong-status"
    scenarios = result.get("scenarios")
    if not isinstance(scenarios, list) or not all(isinstance(item, dict) for item in scenarios):
        return "wrong-scenarios"
    if sorted(str(item.get("id")) for item in scenarios) != sorted(s_id(item) for item in lane.scenarios):
        return "wrong-scenarios"
    if any(item.get("result") not in SCENARIO_RESULTS for item in scenarios):
        return "wrong-scenario-result"
    return None


def newest(rows: Sequence[LaneRow]) -> dict[int, LaneRow]:
    latest: dict[int, LaneRow] = {}
    for row in sorted(rows, key=lambda item: (item.slice, item.attempt)):
        latest[row.slice] = row
    return latest


def _results(rows: Sequence[LaneRow]) -> tuple[dict[str, str], dict[str, str]]:
    head: dict[str, str] = {}
    base: dict[str, str] = {}
    for row in rows:
        if row.state != "reported" or row.result is None:
            continue
        target = base if row.kind in _ON_BASE else head
        for item in row.result["scenarios"]:
            target[qa_id(item["id"])] = item["result"]
    return head, base


def owed_base_check(rows: Sequence[LaneRow]) -> tuple[str, ...]:
    latest = newest(rows).values()
    if any(row.kind == "base-check" for row in latest) or any(row.state != "reported" for row in latest):
        return ()
    head, base = _results(list(latest))
    return tuple(item for item, outcome in head.items() if outcome == "failed" and item not in base)


def decide(rows: Sequence[LaneRow], detail: Detail) -> Decision:
    latest = list(newest(rows).values())
    gaps: list[str] = [] if detail.ids else ["no scenarios"]
    for row in latest:
        if row.state in GAPS:
            gaps.append(f"lane {row.lane_no} ({row.kind}): {row.state}, {row.reason}")
        elif row.state == "reported" and row.result is not None and row.result["status"] == "BLOCKED":
            gaps.append(f"lane {row.lane_no} ({row.kind}): reported BLOCKED")
    head, base = _results(latest)
    gaps.extend(f"{item}: inconclusive at the head commit" for item in detail.ids if head.get(item) == "inconclusive")
    gaps.extend(f"{item}: not run at the head commit" for item in detail.ids if item not in head)
    table = tuple(
        (item, str(detail.live.get(item, {}).get("criterion", "")), _WORDS.get(head.get(item, ""), "Not run"))
        for item in detail.ids
    )
    findings = tuple(
        f"- [follow-up] {item} ({criterion}): failed at the head commit and also on the base commit"
        if base.get(item) == "failed"
        else f"- [blocking] {item} ({criterion}): failed at the head commit and not on the base commit"
        for item, criterion, word in table
        if word == "Failed"
    )
    if gaps:
        result: Result = "BLOCKED"
    elif findings:
        result = "FAIL"
    else:
        result = "PASS"
    return Decision(result, table, findings, tuple(gaps))


def render_comment(
    plan: RunPlan | None,
    detail: Detail,
    decision: Decision,
    *,
    runner: str,
    attempt: int,
    lanes: Sequence[tuple[LaneRow, str]],
) -> str:
    fields = [
        "kind=qa", f"from={ORCHESTRATOR}", "role=qa", f"head={detail.head}", f"result={decision.result}",
        f"scope={detail.scope}",
    ]
    launched = {row.slice for row, _ in lanes if row.kind != "base-check"}
    if launched:
        fields.append(f"lanes={len(launched)}")
    if plan is not None:
        fields.append(f"env={plan.qa.profile}")
    fields.extend((f"runner={runner}", f"arm={detail.arm}", f"attempt={attempt}"))
    parts = [f"<!-- orch:v1 {' '.join(fields)} -->", f"## QA result: {decision.result}"]
    if decision.table:
        parts.append(
            "| Scenario | Criterion | Result |\n| --- | --- | --- |\n"
            + "\n".join(f"| {item} | {criterion} | {word} |" for item, criterion, word in decision.table)
        )
    if decision.findings:
        parts.append("Findings:\n" + "\n".join(decision.findings))
    if decision.gaps:
        parts.append("Gaps:\n" + "\n".join(f"- {gap}" for gap in decision.gaps))
    if lanes:
        parts.append(
            "Lanes:\n"
            + "\n".join(f"- lane {row.lane_no} ({row.kind}): {row.agent_url} {info}" for row, info in lanes)
        )
    parts.append(f"<!-- qa-run {detail.run_id} -->")
    return "\n\n".join(parts) + "\n"


Post = Callable[[str, int, str, str], str]


@dataclass
class QaWorker:
    queue: SQLiteJobQueue
    runners: Mapping[str, Runner]
    factory: Callable[[str], FactoryConfig]
    base_commit: Callable[[str, str, str], str]
    post: Post
    storage: Callable[[str], Storage] | None = None
    max_cloud_lanes: int = DEFAULT_MAX_CLOUD_LANES
    shares_usage_with: Mapping[str, str] = field(default_factory=dict)
    log: Callable[[str], None] = lambda line: None
    evidence: Mapping[str, QaStorage] = field(default_factory=dict)
    free_bytes: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free
    _plans: dict[str, RunPlan] = field(default_factory=dict)
    _low_disk: set[str] = field(default_factory=set)
    _skips_logged: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        with self.queue.connect() as connection:
            ensure_qa_lanes(connection)

    def tick(self) -> None:
        self._recycle()
        if not self.runners or self.storage is None:
            return
        waiting: Counter[str] = Counter()
        try:
            waiting = self._claim()
        except Exception as exc:
            self.log(f"qa claim: {type(exc).__name__}: {exc}")
        for job in self._active_jobs():
            try:
                self._advance(job, waiting)
                if self._job_state(job.id) == "running":
                    self.queue.heartbeat(job.id, owner=QA_RECIPIENT)
            except Exception as exc:
                self.log(f"qa run {job.id}: {type(exc).__name__}: {exc}")

    def _recycle(self) -> None:
        for repo, entry in self.evidence.items():
            budget = entry.evidence_budget_gib
            try:
                with self.queue.connect() as connection:
                    report = recycle(
                        connection,
                        root=entry.evidence_root,
                        repo=repo,
                        now=self.queue.now(),
                        budget_bytes=None if budget is None else budget * GIB,
                    )
            except Exception as exc:
                self.log(f"qa recycle {repo}: {type(exc).__name__}: {exc}")
                continue
            for path in report.removed:
                self.log(f"qa recycle {repo}: removed run {PurePosixPath(path).name} for the budget")
            if report.deleted:
                self.log(f"qa recycle {repo}: deleted {len(report.deleted)} files")
            for run_id, reason in report.skipped:
                if (run_id, reason) not in self._skips_logged:
                    self._skips_logged.add((run_id, reason))
                    self.log(f"qa recycle {repo}: skipped run {run_id}: {reason}")

    def _disk_low(self, repo: str) -> bool:
        entry = self.evidence.get(repo)
        if entry is None or entry.min_free_gib is None:
            return False
        root = entry.evidence_root
        try:
            root.mkdir(parents=True, exist_ok=True)
            free = self.free_bytes(root)
        except OSError as exc:
            reason = f"cannot read free space ({exc.strerror or exc})"
        else:
            if free >= entry.min_free_gib * GIB:
                if repo in self._low_disk:
                    self._low_disk.discard(repo)
                    self.log(f"qa claim resumed for {repo}: {root}")
                return False
            reason = f"has {free // GIB} GiB free, below {entry.min_free_gib} GiB"
        if repo not in self._low_disk:
            self._low_disk.add(repo)
            self.log(f"qa claim paused for {repo}: {root} {reason}")
        return True

    def _claim(self) -> Counter[str]:
        now = self.queue.now()
        waiting: Counter[str] = Counter()
        low: dict[str, bool] = {}
        with self.queue.connect() as connection:
            jobs = [job_from_row(row, connection) for row in claim_order(connection, QA_RECIPIENT, now=now)]
        for job in jobs:
            raw = json.loads(job.request.detail or "{}")
            if "live" not in raw:
                self._fail_old(job)
                continue
            runner = raw["runner"]
            if runner not in self.runners:
                continue
            repo = job.request.repo.lower()
            if repo not in low:
                low[repo] = self._disk_low(repo)
            if low[repo]:
                self._restart_wait(job)
                continue
            with self.queue.connect() as connection:
                if connection.execute(
                    "SELECT 1 FROM qa_runner_backoff WHERE runner = ? AND until > ?", (runner, now)
                ).fetchone() is not None:
                    _restart_wait(connection, job.id, now)
                    continue
                reason = self._yields(connection, job.id, runner, now)
                if reason is not None:
                    _restart_wait(connection, job.id, now)
                    if _note_yield(connection, job, reason, now):
                        self.log(f"qa job {job.id}: waits, {reason}")
                    continue
            if self._open_count(runner) >= self.max_cloud_lanes:
                waiting[runner] += 1
                self._restart_wait(job)
                continue
            try:
                plan = self._plan(job)
            except Exception as exc:
                self.log(f"qa job {job.id}: no plan: {type(exc).__name__}: {exc}")
                continue
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if _open_count(connection, runner) >= self.max_cloud_lanes:
                    waiting[runner] += 1
                    _restart_wait(connection, job.id, now)
                elif _take(connection, job.id, now) and plan.slices:
                    _reserve(connection, job, plan, plan.slices[0], runner, attempt=1, now=now)
                connection.commit()
        return waiting

    def _grant(self, job: Job, plan: RunPlan, waiting: Counter[str]) -> None:
        rows = self._lanes(plan.detail.run_id)
        if not any(row.agent_id for row in rows):
            return
        runner = rows[0].runner
        with self.queue.connect() as connection:
            if self._paused(connection, runner, self.queue.now()) is not None:
                return
        taken = {row.slice for row in rows}
        for item in plan.slices:
            if item.index in taken:
                continue
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                room = self.max_cloud_lanes - _open_count(connection, runner) - waiting[runner]
                if room > 0:
                    _reserve(connection, job, plan, item, runner, attempt=1, now=self.queue.now())
                connection.commit()
            if room <= 0:
                break

    def _advance(self, job: Job, waiting: Counter[str]) -> None:
        detail = Detail.of(json.loads(job.request.detail or "{}"))
        if job.state != "running" or self._run_cancelled(detail.run_id):
            self._cancel(job, detail)
            return
        plan = self._plan(job)
        for row in self._lanes(detail.run_id):
            if row.state == "launching" and self._launch(job, plan, row) == "requeued":
                return
        self._grant(job, plan, waiting)
        for row in self._lanes(detail.run_id):
            if row.state == "running":
                self._watch(plan, row)
        rows = self._lanes(detail.run_id)
        if any(row.state in OPEN for row in rows):
            return
        gapped = any(row.state in GAPS for row in newest(rows).values())
        if not gapped and len({row.slice for row in rows}) < len(plan.slices):
            return
        owed = () if gapped else owed_base_check(rows)
        if owed:
            base_check = Slice(max(row.slice for row in rows) + 1, "base-check", owed)
            with self.queue.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _reserve(connection, job, plan, base_check, rows[-1].runner, attempt=1, now=self.queue.now())
                connection.commit()
            return
        self._finish(job, plan, rows)

    def _launch(self, job: Job, plan: RunPlan, row: LaneRow) -> Literal["launched", "gap", "requeued", "deferred"]:
        first = row.slice == 0 and row.attempt == 1 and row.kind != "base-check" and not any(
            other.agent_id for other in self._lanes(row.run_id)
        )
        runner = row.runner
        with self.queue.connect() as connection:
            if self._yields(connection, job.id, runner, self.queue.now()) is not None:
                return "deferred"
            connection.execute(
                "UPDATE qa_lanes SET launch_called_at = ? WHERE lane_id = ?", (self.queue.now(), row.lane_id)
            )
        limited: list[str] = []
        while True:
            try:
                launched = self.runners[runner].launch(self._lane(plan, row, runner))
            except (RunnerOutage, UsageLimit) as exc:
                if not first:
                    self._close(row, "gap", f"launch-failed:{type(exc).__name__}")
                    return "gap"
                with self.queue.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO qa_runner_backoff (runner, until) VALUES (?, ?)
                        ON CONFLICT(runner) DO UPDATE SET until = MAX(until, excluded.until)
                        """,
                        (runner, self.queue.now() + BACKOFF_SECONDS),
                    )
                if isinstance(exc, UsageLimit) and runner in self.shares_usage_with:
                    limited.append(runner)
                following = self._next_runner(job, plan, runner, {self.shares_usage_with[name] for name in limited})
                if following is None or not self._move(row, following):
                    self._requeue(job, plan, runner, exc, limited)
                    return "requeued"
                runner = following
                continue
            except Exception as exc:
                self._close(row, "gap", f"launch-failed:{type(exc).__name__}")
                return "gap"
            break
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE qa_lanes SET agent_id = ?, agent_url = ?, state = 'running', started_at = ?
                WHERE lane_id = ? AND state = 'launching'
                """,
                (launched.agent_id, launched.url, self.queue.now(), row.lane_id),
            )
            connection.commit()
        return "launched"

    def _lane(self, plan: RunPlan, row: LaneRow, runner: str) -> Lane:
        scenarios = tuple(_scenario(plan.detail.live[item]) for item in row.scenarios)
        links = links_for_lane(
            row.run_id, row.lane_no, pack_ids(scenarios), plan.qa.timeout_minutes * 60,
            storage=self.storage(plan.repo), repo=plan.repo,
        )
        facts = LaneFacts(
            repo=plan.repo,
            pr=plan.detail.pr,
            run_id=row.run_id,
            commit=plan.detail.head,
            base_commit=plan.base_commit,
            lane=row.lane_no,
            kind=row.kind,
            scenarios=scenarios,
            lane_config=plan.qa.lane,
            verify=plan.qa.verify,
            evidence=plan.qa.evidence,
            links=links,
        )
        config = next(item for item in plan.qa.runners if item.name == runner)
        return Lane(
            lane_id=row.lane_id,
            brief=render_qa_brief(facts, runner),
            repo=facts.repo,
            base_ref=plan.factory.base_branch,
            environment_name=config.environment,
            model=config.model,
        )

    def _watch(self, plan: RunPlan, row: LaneRow) -> None:
        runner = self.runners[row.runner]
        try:
            result = self._intake(plan, row)
            if result is None and runner.status(row.agent_id) == "ended":
                result = self._intake(plan, row)
                if result is None:
                    self._close(row, "gap", "ended-without-result")
                    return
        except StorageError as exc:
            self.log(f"qa lane {row.lane_no} of {row.run_id}: StorageError: {exc}")
            result = None
        except EvidenceRefused as exc:
            self.log(f"qa lane {row.lane_no} of {row.run_id}: evidence refused: {exc}")
            self._drop(row, "evidence-refused")
            return
        if result is None:
            if self.queue.now() - row.started_at >= plan.qa.timeout_minutes * 60:
                runner.stop(row.agent_id)
                self._close(row, "timed-out", "no-result-in-time")
            return
        reason = rejection(row, result, plan.qa.verify)
        if reason is None:
            with self.queue.connect() as connection:
                _set_state(connection, row.lane_id, "reported", None, self.queue.now(), result=result)
                folder = run_dir(plan.evidence_root, plan.repo, plan.ticket, row.run_id)
                record_evidence_dir(connection, row.run_id, folder)
            return
        self._drop(row, reason)

    def _intake(self, plan: RunPlan, row: LaneRow) -> dict | None:
        folder = lane_dir(plan.evidence_root, plan.repo, plan.ticket, row.run_id, row.lane_no)
        if folder.exists():
            return stored(folder)
        return collect(
            row.run_id, row.lane_no, storage=self.storage(plan.repo), repo=plan.repo, ticket=plan.ticket,
            root=plan.evidence_root,
        )

    def _drop(self, row: LaneRow, reason: str) -> None:
        self.runners[row.runner].stop(row.agent_id)
        if row.attempt == 2:
            self._close(row, "gap", f"dropped-twice:{reason}")
            return
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _set_state(connection, row.lane_id, "dropped", reason, self.queue.now())
            _relaunch(connection, row, self.queue.now())
            connection.commit()

    def _finish(self, job: Job, plan: RunPlan, rows: Sequence[LaneRow]) -> None:
        detail = plan.detail
        decision = decide(rows, detail)
        latest = [row for row in newest(rows).values() if row.agent_id]
        lanes = [(row, self._describe(row)) for row in latest]
        runner = rows[0].runner if rows else detail.runner
        body = render_comment(plan, detail, decision, runner=runner, attempt=job.request.attempt, lanes=lanes)
        comment_id = self.post(job.request.repo, detail.pr, body, f"<!-- qa-run {detail.run_id} -->")
        self.queue.finish(job.id, owner=QA_RECIPIENT, handoff_comment_id=str(comment_id))
        self._plans.pop(detail.run_id, None)

    def _describe(self, row: LaneRow) -> str:
        try:
            info = self.runners[row.runner].describe(row.agent_id)
        except Exception as exc:
            return f"(machine unknown: {type(exc).__name__})"
        return f"(machine {info.machine}, agent {info.version})"

    def _cancel(self, job: Job, detail: Detail) -> None:
        for row in self._lanes(detail.run_id):
            if row.state in OPEN and self._stopped(job, row):
                self._close(row, "cancelled", None)
        if job.state == "running":
            self.queue.finish(job.id, owner=QA_RECIPIENT, handoff_comment_id=None)
        self._plans.pop(detail.run_id, None)

    def _stopped(self, job: Job, row: LaneRow) -> bool:
        if row.agent_id is None and row.launch_called_at is None:
            return True
        if row.runner not in self.runners:
            self.log(f"qa lane {row.lane_id}: runner {row.runner} is not registered; cleanup kept")
            return False
        runner = self.runners[row.runner]
        try:
            agent_id = row.agent_id
            if agent_id is None:
                launched = runner.launch(self._lane(self._plan(job), row, row.runner))
                agent_id = launched.agent_id
                with self.queue.connect() as connection:
                    connection.execute(
                        "UPDATE qa_lanes SET agent_id = ?, agent_url = ? WHERE lane_id = ? AND agent_id IS NULL",
                        (agent_id, launched.url, row.lane_id),
                    )
            runner.stop(agent_id)
        except Exception as exc:
            self.log(f"qa lane {row.lane_id}: stop failed: {type(exc).__name__}; retrying")
            return False
        return True

    def _requeue(self, job: Job, plan: RunPlan, runner: str, exc: Exception, limited: Sequence[str]) -> None:
        now = self.queue.now()
        causes = [f"{name}:{UsageLimit.__name__}" for name in limited if name != runner]
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM qa_lanes WHERE run_id = ? AND agent_id IS NULL", (plan.detail.run_id,))
            record_runner_moved(connection, plan.detail.run_id, plan.detail.runner)
            changed = connection.execute(
                "UPDATE jobs SET state = 'queued', last_activity_at = ? WHERE id = ? AND state = 'running'",
                (now, job.id),
            ).rowcount
            if changed:
                connection.execute(
                    """
                    INSERT INTO job_events (
                        job_id, repo, ticket_id, action, recipient, event, detail, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, 'qa-requeued', ?, ?)
                    """,
                    (
                        job.id, job.request.repo.lower(), job.request.ticket_id, job.request.action,
                        QA_RECIPIENT, ",".join([*causes, f"{runner}:{type(exc).__name__}"]), now,
                    ),
                )
            connection.commit()
        self._plans.pop(plan.detail.run_id, None)

    def _move(self, row: LaneRow, runner: str) -> bool:
        with self.queue.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            moved = _open_count(connection, runner) < self.max_cloud_lanes and connection.execute(
                "UPDATE qa_lanes SET runner = ? WHERE lane_id = ? AND state = 'launching'",
                (runner, row.lane_id),
            ).rowcount == 1
            if moved:
                record_runner_moved(connection, row.run_id, runner)
            connection.commit()
        return moved

    def _next_runner(self, job: Job, plan: RunPlan, runner: str, spent: set[str]) -> str | None:
        if plan.detail.arm != "primary" or plan.qa.shootout:
            return None
        names = [item.name for item in plan.qa.runners]
        following = names[names.index(runner) + 1 :] if runner in names else []
        now = self.queue.now()
        with self.queue.connect() as connection:
            return next(
                (
                    name for name in following
                    if name in self.runners
                    and self.shares_usage_with.get(name) not in spent
                    and self._yields(connection, job.id, name, now) is None
                ),
                None,
            )

    def _paused(self, connection: sqlite3.Connection, runner: str, now: float) -> lane_limit.LaneLimit | None:
        recipient = self.shares_usage_with.get(runner)
        return None if recipient is None else lane_limit.recipient_paused(connection, recipient, now, None)

    def _yields(self, connection: sqlite3.Connection, job_id: str, runner: str, now: float) -> str | None:
        recipient = self.shares_usage_with.get(runner)
        if recipient is None:
            return None
        limit = self._paused(connection, runner, now)
        if limit is not None:
            return f"{recipient}: {lane_limit.label(limit)}"
        if recipient in self._owed(connection, job_id) and claim_order(connection, recipient, now=now):
            return f"behind queued {recipient} jobs"
        return None

    def _owed(self, connection: sqlite3.Connection, job_id: str) -> set[str]:
        rows = connection.execute(
            "SELECT detail FROM job_events WHERE job_id = ? AND event = 'qa-requeued'", (job_id,)
        ).fetchall()
        return {
            self.shares_usage_with[name]
            for row in rows
            for name, _, cause in (entry.partition(":") for entry in row["detail"].split(","))
            if cause == UsageLimit.__name__ and name in self.shares_usage_with
        }

    def _plan(self, job: Job) -> RunPlan:
        detail = Detail.of(json.loads(job.request.detail or "{}"))
        plan = self._plans.get(detail.run_id)
        if plan is not None:
            return plan
        factory = self.factory(job.request.repo)
        if factory.qa is None:
            raise ValueError("factory.md has no qa block")
        entry = self.evidence.get(job.request.repo.lower())
        if entry is None:
            raise ValueError("orch.yml has no projects[].qa_storage for this repo")
        rows = self._lanes(detail.run_id)
        base = rows[0].base_commit if rows else self.base_commit(job.request.repo, factory.base_branch, detail.head)
        plan = RunPlan(
            repo=job.request.repo,
            ticket=job.request.ticket_id,
            detail=detail,
            factory=factory,
            qa=factory.qa,
            evidence_root=entry.evidence_root.resolve(),
            base_commit=base,
            slices=plan_slices(detail.ids, detail.regression, factory.qa.lanes),
        )
        self._plans[detail.run_id] = plan
        return plan

    def _close(self, row: LaneRow, state: str, reason: str | None, *, result: dict | None = None) -> None:
        with self.queue.connect() as connection:
            _set_state(connection, row.lane_id, state, reason, self.queue.now(), result=result)

    def _lanes(self, run_id: str) -> list[LaneRow]:
        with self.queue.connect() as connection:
            return [
                LaneRow.of(row)
                for row in connection.execute(
                    "SELECT * FROM qa_lanes WHERE run_id = ? ORDER BY lane_no", (run_id,)
                )
            ]

    def _open_count(self, runner: str) -> int:
        with self.queue.connect() as connection:
            return _open_count(connection, runner)

    def _restart_wait(self, job: Job) -> None:
        with self.queue.connect() as connection:
            _restart_wait(connection, job.id, self.queue.now())

    def _job_state(self, job_id: str) -> str:
        with self.queue.connect() as connection:
            return connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()["state"]

    def _run_cancelled(self, run_id: str) -> bool:
        with self.queue.connect() as connection:
            row = connection.execute("SELECT state FROM qa_runs WHERE run_id = ?", (run_id,)).fetchone()
        return row is not None and row["state"] == "cancelled"

    def _active_jobs(self) -> list[Job]:
        with self.queue.connect() as connection:
            return [
                job_from_row(row, connection)
                for row in connection.execute(
                    """
                    SELECT * FROM jobs WHERE recipient = ? AND (
                        state = 'running'
                        OR id IN (SELECT job_id FROM qa_lanes WHERE state IN ('launching', 'running'))
                    )
                    ORDER BY created_at, id
                    """,
                    (QA_RECIPIENT,),
                )
            ]

    def _fail_old(self, job: Job) -> None:
        with self.queue.connect() as connection:
            taken = _take(connection, job.id, self.queue.now())
        if taken:
            self.log(f"qa job {job.id}: qa-detail-old")
            self.queue.finish(job.id, owner=QA_RECIPIENT, handoff_comment_id=None)


def _scenario(item: Mapping) -> Scenario:
    named = tuple(name for name in (save_name(phrase) for phrase in item.get("saves", ())) if name)
    return Scenario(
        id=s_id(item["id"]),
        criterion=item["criterion"],
        steps=(item["steps"],),
        pass_test=item["passes"],
        pack=item.get("pack") or "none",
        save=named + tuple(name for name in (AFTER_SCREENSHOT, PASS_OUTPUT) if name not in named),
    )


def _open_count(connection: sqlite3.Connection, runner: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM qa_lanes WHERE runner = ? AND state IN ('launching', 'running')", (runner,)
    ).fetchone()[0]


def _restart_wait(connection: sqlite3.Connection, job_id: str, now: float) -> None:
    connection.execute(
        "UPDATE jobs SET last_activity_at = ? WHERE id = ? AND state = 'queued'", (now, job_id)
    )


def _note_yield(connection: sqlite3.Connection, job: Job, reason: str, now: float) -> bool:
    return connection.execute(
        """
        INSERT INTO job_events (job_id, repo, ticket_id, action, recipient, event, detail, recorded_at)
        SELECT ?, ?, ?, ?, ?, 'qa-yielded', ?, ?
        WHERE NOT EXISTS (SELECT 1 FROM job_events WHERE job_id = ? AND event = 'qa-yielded' AND detail = ?)
        """,
        (job.id, job.request.repo.lower(), job.request.ticket_id, job.request.action, QA_RECIPIENT, reason, now,
         job.id, reason),
    ).rowcount == 1


def _take(connection: sqlite3.Connection, job_id: str, now: float) -> bool:
    return connection.execute(
        "UPDATE jobs SET state = 'running', last_activity_at = ? WHERE id = ? AND state = 'queued'",
        (now, job_id),
    ).rowcount == 1


def _reserve(
    connection: sqlite3.Connection,
    job: Job,
    plan: RunPlan,
    item: Slice,
    runner: str,
    *,
    attempt: int,
    now: float,
) -> None:
    commit = plan.base_commit if item.kind in _ON_BASE else plan.detail.head
    _insert(connection, (plan.detail.run_id, job.id, item.index, item.kind, json.dumps(list(item.ids)),
                         commit, plan.base_commit, runner), attempt=attempt, now=now)


def _relaunch(connection: sqlite3.Connection, row: LaneRow, now: float) -> None:
    _insert(connection, (row.run_id, row.job_id, row.slice, row.kind, json.dumps(list(row.scenarios)),
                         row.commit, row.base_commit, row.runner), attempt=2, now=now)


def _insert(connection: sqlite3.Connection, facts: tuple, *, attempt: int, now: float) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO qa_lanes (
            lane_id, run_id, job_id, slice, kind, scenarios, commit_sha, base_commit,
            runner, state, attempt, started_at, lane_no
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, 'launching', ?, ?, COALESCE(MAX(lane_no), 0) + 1
        FROM qa_lanes WHERE run_id = ?
        """,
        (uuid.uuid4().hex, *facts, attempt, now, facts[0]),
    )


def _set_state(
    connection: sqlite3.Connection,
    lane_id: str,
    state: str,
    reason: str | None,
    now: float,
    *,
    result: dict | None = None,
) -> None:
    connection.execute(
        """
        UPDATE qa_lanes SET state = ?, reason = ?, result = ?, finished_at = ?
        WHERE lane_id = ? AND state IN ('launching', 'running')
        """,
        (state, reason, None if result is None else json.dumps(result, sort_keys=True), now, lane_id),
    )
