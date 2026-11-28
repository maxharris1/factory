from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lib import epoch, exposure, job_detail, pricing
from lib.role_router import Sample, parse_sample, routed_slot

Role = Literal["plan", "build", "review"]
Mark = Literal["sampled", "default"]
Ticket = tuple[str, str]

ROLE_ORDER: tuple[Role, ...] = ("plan", "build", "review")
_ROLES: dict[str, Role] = {"plan": "plan", "build": "build", "fix": "build", "review": "review"}
APPROVE, REVISE, BLOCKED = "APPROVE", "REVISE", "BLOCKED"


@dataclass(frozen=True)
class Measure:
    value: float | None
    of: int


@dataclass(frozen=True)
class VerdictMix:
    approve: int
    revise: int
    blocked: int
    of: int


@dataclass(frozen=True)
class Row:
    role: Role
    harness: str
    model: str | None
    risk: str | None
    plan_first: bool
    mark: Mark
    tickets: int
    jobs: int
    share: Measure
    rounds_to_approve: Measure
    verdicts: VerdictMix | None
    fix_rounds: Measure
    first_push_ci: Measure
    changed_lines: Measure
    elapsed_minutes: Measure
    tokens_in: Measure
    tokens_cached: Measure
    tokens_out: Measure
    cost_usd: Measure


@dataclass(frozen=True)
class _Job:
    id: str
    ticket: Ticket
    action: str
    recipient: str
    created_at: float | None
    comment: str | None
    detail: dict


@dataclass(frozen=True)
class _Verdict:
    comment: str
    verdict: str | None
    changed_lines: int | None


@dataclass(frozen=True)
class _Telemetry:
    elapsed_minutes: float | None
    tokens: tuple[int, int, int] | None
    cost_usd: float | None


def rows(db: Path, *, since: float | None = None, repo: str | None = None) -> list[Row]:
    uri = db.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.execute("PRAGMA query_only = ON")
        epoch.register(connection)
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "jobs" not in tables:
            return []
        on_repo, params = ("WHERE lower(repo) = lower(?)", (repo,)) if repo is not None else ("", ())
        ran = f"{on_repo or 'WHERE 1'} AND state IN ('done', 'failed')"
        jobs = [
            _Job(
                job_id,
                (job_repo.lower(), ticket_id),
                action,
                recipient,
                created_at,
                None if comment is None else str(comment),
                _detail(connection, job_id, detail),
            )
            for job_id, job_repo, ticket_id, action, recipient, detail, created_at, comment
            in connection.execute(
                f"SELECT id, repo, ticket_id, action, recipient, detail, {epoch.sql('created_at')}, "
                f"handoff_comment_id FROM jobs {ran} "
                f"ORDER BY {epoch.sql('created_at')} IS NULL, {epoch.sql('created_at')}, rowid",
                params,
            )
        ]
        models: dict[str, str] = {}
        if exposure.TABLE in tables:
            models = dict(
                connection.execute(
                    f"SELECT job_id, model FROM {exposure.TABLE} "
                    f"WHERE job_id IN (SELECT id FROM jobs {ran})",
                    params,
                )
            )
        verdicts: dict[Ticket, list[_Verdict]] = defaultdict(list)
        if "verdict_heads" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(verdict_heads)")}
            verdict = "verdict" if "verdict" in columns else "NULL"
            lines = "changed_lines" if "changed_lines" in columns else "NULL"
            for head_repo, ticket_id, comment, value, changed in connection.execute(
                f"SELECT repo, ticket_id, comment_id, {verdict}, {lines} "
                f"FROM verdict_heads {on_repo} "
                f"ORDER BY {epoch.sql('posted_at')} IS NULL, {epoch.sql('posted_at')}, rowid",
                params,
            ):
                verdicts[(head_repo.lower(), ticket_id)].append(
                    _Verdict(str(comment), value, changed)
                )
        first_ci: dict[Ticket, bool] = {}
        if "ci_events" in tables:
            for ci_repo, ticket_id, status in connection.execute(
                f"SELECT repo, ticket_id, status FROM ci_events {on_repo} ORDER BY rowid", params
            ):
                first_ci.setdefault((ci_repo.lower(), ticket_id), status == "success")
        telemetry: dict[str, _Telemetry] = {}
        if "job_telemetry" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(job_telemetry)")}
            flag = "tokens_unknown" if "tokens_unknown" in columns else "NULL"
            recipients = {job.id: job.recipient for job in jobs}
            for job_id, harness, elapsed, *tokens, cost, unknown in connection.execute(
                "SELECT job_id, harness, elapsed_seconds, tokens_in, tokens_cached, tokens_out, "
                f"cost_usd, {flag} FROM job_telemetry WHERE job_id IN (SELECT id FROM jobs {ran})",
                params,
            ):
                telemetry[job_id] = _telemetry(
                    harness or recipients[job_id], elapsed, tokens, cost, unknown
                )

    jobs = [job for job in jobs if job.detail.get("kind") != "merge-only"]
    planned = {job.ticket for job in jobs if job.action == "plan"}
    fixes = Counter(job.ticket for job in jobs if job.action == "fix")
    samples: dict[Ticket, Sample] = {}
    for job in jobs:
        sample = parse_sample(_routing(job).get("sample"))
        if sample is not None:
            samples[job.ticket] = sample
    rounds = {
        ticket: next(n for n, v in enumerate(heads, 1) if v.verdict == APPROVE)
        for ticket, heads in verdicts.items()
        if any(v.verdict == APPROVE for v in heads)
    }
    sizes = {
        ticket: next(v.changed_lines for v in heads if v.changed_lines is not None)
        for ticket, heads in verdicts.items()
        if any(v.changed_lines is not None for v in heads)
    }

    groups: dict[tuple, list[_Job]] = defaultdict(list)
    role_tickets: dict[Role, set[Ticket]] = defaultdict(set)
    for job in jobs:
        role = _ROLES.get(job.action)
        if role is None or (
            since is not None and (job.created_at is None or job.created_at < since)
        ):
            continue
        key = (
            role,
            job.recipient,
            _model(job, models.get(job.id)),
            _risk(job),
            job.ticket in planned,
            _mark(job, role, samples.get(job.ticket)),
        )
        groups[key].append(job)
        role_tickets[role].add(job.ticket)

    report = []
    for key, members in groups.items():
        role = key[0]
        tickets = {job.ticket for job in members}
        if role == "review":
            own = {(job.ticket, job.comment) for job in members}
            counted = [
                v.verdict for t in tickets for v in verdicts.get(t, ()) if (t, v.comment) in own
            ]
        else:
            counted = [v.verdict for t in tickets for v in verdicts.get(t, ())]
        everyone = len(role_tickets[role])
        runs = [telemetry[job.id] for job in members if job.id in telemetry]
        measured = [run.tokens for run in runs if run.tokens is not None]
        report.append(
            Row(
                *key,
                tickets=len(tickets),
                jobs=len(members),
                share=Measure(len(tickets) / everyone, everyone),
                rounds_to_approve=_mean([rounds[t] for t in tickets if t in rounds]),
                verdicts=_mix(counted),
                fix_rounds=_mean([fixes[t] for t in tickets]),
                first_push_ci=_mean([first_ci[t] for t in tickets if t in first_ci]),
                changed_lines=_mean([sizes[t] for t in tickets if t in sizes]),
                elapsed_minutes=_mean(
                    [run.elapsed_minutes for run in runs if run.elapsed_minutes is not None]
                ),
                tokens_in=_mean([tokens[0] for tokens in measured]),
                tokens_cached=_mean([tokens[1] for tokens in measured]),
                tokens_out=_mean([tokens[2] for tokens in measured]),
                cost_usd=_mean([run.cost_usd for run in runs if run.cost_usd is not None]),
            )
        )
    return sorted(report, key=_order)


def _detail(connection: sqlite3.Connection, job_id: str, raw: str | None) -> dict:
    try:
        loaded = job_detail.load_row(connection, job_id, raw)
        detail = json.loads(loaded) if loaded is not None else None
    except ValueError:
        return {}
    return detail if isinstance(detail, dict) else {}


def _routing(job: _Job) -> dict:
    routing = job.detail.get("routing")
    return routing if isinstance(routing, dict) else {}


def _model(job: _Job, reported: str | None) -> str | None:
    if reported is not None and reported != exposure.UNKNOWN:
        return reported
    routed, _ = routed_slot(_routing(job), action=job.action, harness=job.recipient)
    return routed if isinstance(routed, str) else None


def _risk(job: _Job) -> str | None:
    risk = job.detail.get("risk")
    return risk if isinstance(risk, str) else None


def _mark(job: _Job, role: Role, sample: Sample | None) -> Mark:
    draw = None if sample is None else sample.review if role == "review" else sample.build
    picked = draw is not None and draw.picked and draw.harness == "gemini"
    return "sampled" if picked and job.recipient == "gemini" else "default"


def _telemetry(
    harness: str,
    elapsed_seconds: float | None,
    tokens: list[int | None],
    cost_usd: float | None,
    unknown: int | None,
) -> _Telemetry:
    tokens_in, tokens_cached, tokens_out = (count or 0 for count in tokens)
    known = any((tokens_in, tokens_cached, tokens_out)) if unknown is None else unknown == 0
    if not known:
        tokens_in = tokens_cached = tokens_out = 0
    return _Telemetry(
        None if elapsed_seconds is None else elapsed_seconds / 60,
        (tokens_in, tokens_cached, tokens_out) if known else None,
        pricing.effective_cost_usd(
            harness, cost_usd, tokens_in=tokens_in, tokens_cached=tokens_cached,
            tokens_out=tokens_out,
        ),
    )


def _mean(values: list[float]) -> Measure:
    return Measure(sum(values) / len(values) if values else None, len(values))


def _mix(verdicts: list[str | None]) -> VerdictMix | None:
    counts = Counter(verdicts)
    known = counts[APPROVE] + counts[REVISE] + counts[BLOCKED]
    if known == 0:
        return None
    return VerdictMix(counts[APPROVE], counts[REVISE], counts[BLOCKED], known)


def _order(row: Row) -> tuple:
    def none_first(value: str | None) -> tuple[bool, str]:
        return (value is not None, value or "")

    return (
        ROLE_ORDER.index(row.role),
        row.harness,
        none_first(row.model),
        none_first(row.risk),
        row.plan_first,
        row.mark,
    )
