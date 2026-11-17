"""Weekly eval report (docs/SPEC.md section 11 and F1-14).

The report reads one directory. It does not contact GitHub or Linear.
Fixtures use that directory until F1-10 has logs. A weekly run points
--input at the collected directory and may set --since and --until
(inclusive, UTC, YYYY-MM-DDTHH:MM:SSZ).

Inputs, all local, and none read from outside the directory:

- orch.yml, validated by lib.orch_yml
- projects/<repo>/factory.md, validated by lib.factory_md
- dispatcher.log, one event per line
- comment files named by the log, parsed by lib.handoff
- model-findings.txt, optional

dispatcher.log lines:

    TIMESTAMP repo=<owner/name> ticket=<id> [pr=<n>] enter state=<state>
    TIMESTAMP repo=<owner/name> ticket=<id> [pr=<n>] comment path=<relative>
    TIMESTAMP repo=<owner/name> ticket=<id> [pr=<n>] wake role=<token> reason=<token> [wasted=yes] [handled=<token>]
    TIMESTAMP repo=<owner/name> ticket=<id> [pr=<n>] end
    TIMESTAMP repo=<owner/name> [pr=<n>] message from=<bot> to=<bot> [kind=<token>]

A run is one (repo, ticket). The ticket id is a GitHub number or a
Linear id such as ENG-42, so Intake, Questions and Planning rows log
before any PR exists.

The whole log is measured, then clipped to the window. State dwell
time is the gap between an enter and the next enter or end for that
ticket, clipped to [since, until]. With --until, a state still open at
the end of the log is measured to until. Without it, the state is listed
under open states. Wakes, verdicts, stuck rows and transitions count
when their event is in the window. A run counts when it has an event in
the window or a dwell that overlaps it. Chart comparison uses the
section 4.1 edges. post-merge is on the chart and is still reported,
because Phase 1 configures no post_merge.

A line that does not parse is skipped. The report counts the skipped
lines, and each one is a warning naming its line number and fault.

A message line is one manual bot-to-bot handoff and belongs to no run.
Message lines in the window are counted against comment lines in the
window, per repo and ISO week.

A wake is wasted when wasted=yes, when reason=nobody, or when handled
repeats for that ticket, including a first handling before the window.

Review rounds are the max verdict round in the window. A verdict with no
round takes its 1-based position among the run's verdicts. Ready
comments are not rounds. Each verdict is checked against the cap for its
risk. Risk comes from the run's latest go that carries risk=, else from
the verdict: neither is cap-unverified, and a disagreement is
risk-mismatch. The rounds allowed at a verdict are the factory.md cap
plus the capped to fixing transitions before it, since only the trusted
go makes that transition (issue #105). A round inside that allowance and
over the cap is round-extra-authorized; above it is round-over-cap. The
denominator in the verdict header is the reviewer's claim and is not
read.

model-findings.txt lines:

    TIMESTAMP <owner/name> <ticket> <rule> <detail>

The model checks the spec, not runs, so nothing writes this file yet. A
missing file is reported as none supplied.

Metrics (issue #209, SF-10) come from an orch.db snapshot, not from this
directory: build_metrics opens a backup (lib.state_backup) read-only, so
it writes nothing to it or beside it, and render_metrics prints each
metric beside O4's baseline snapshot. A metric whose source is not in a
snapshot prints as unrecorded(<reason>). A ratio with nothing to divide
prints as -. --since and --until (inclusive, UTC) filter the rows that
carry a time, for the metrics named in WINDOWED_METRICS and the follow-up
list. The rest are lifetime counts:

- rounds per PR: a PR is reviewed once it has a REVISE round
  (ticket_state.review_round) or an approval; its rounds are its REVISE
  rounds plus the approving one
- first-round REVISE and first-pass APPROVE rates: each PR's first acted-on
  verdict, in handled_comments order: a REVISE recorded in review_events,
  or an APPROVE recorded as a ship intent, a ship hold, or a held APPROVE
  a base update displaced (held-base-update review_events). They read verdict
  history only, so a re-entry after handoff or an update-from-main
  re-review does not change them. A held APPROVE stays unhandled until its
  hold clears, so it counts as that PR's latest verdict until then
- PRs capped: capped now, or a capped review_events outcome
- PRs dirty after approval: tickets with an update-from-main job
- duplicate handoffs: duplicate-verdict handled comments
- refused approvals: rejected-approve-* handled comments (#508). A snapshot
  from before #508 has no approve_rule_floor table and prints unrecorded
- BLOCKED stops: tickets in blocked
- tickets succeeded: merged or done; Aut0 counts at handed-off (Ready for
  Review)
- skill reads per job kind (plan, build, fix, review; #505), over done
  and failed jobs, merge-only fixes left out: the share of jobs that read
  the factory-builder or factory-reviewer router and the mean count of
  distinct skill files read, from job_telemetry.skill_reads (paths under
  the deployed skills tree, from file reads and Skill tool calls). The
  Skill tool count is not used. A job with no telemetry row or no
  skill_reads value is counted in skill-reads-unrecorded-<kind> and left
  out of both means; a kind with no recorded job, or a snapshot from
  before the column, prints unrecorded
- helpers per job: #100's job_telemetry tool counts (Agent and Task) over
  Claude jobs. D25 sets no helper cap, so the max is printed too
- preflight failures: requeued-unstarted job events whose reason is one
  lib.remote.preflight raises (PREFLIGHT_FAILURE). An unreachable device
  counts: preflight is a job's first ssh call
- slot-hours lost: telemetry elapsed time of failed and cancelled jobs
- hand-written gos: gos the dispatcher acted on that the feeder did not
  post (feeder_items.go_comment_id), timed by handled_times: a queued,
  answered or deduped plan or build, a go that lifts a cap (queued-fix) or
  resumes a BLOCKED review (queued-blocked-review), and a resumed-<action>
  whose job's evidence names a go as its cause
- submitted-invalid: submitted-invalid job events
- repeat-block and unsatisfiable stops: waiting-max-repeat and
  waiting-max-unsat review_events, timed by handled_times
- review-loop measures read verdict_heads. A PR's cohort is the window
  holding its first verdict. A verdict's round is its review_round, else
  its place in the PR's verdicts. An APPROVE counts once it is a ship
  intent, ship hold or held-base-update. Risk is the review job's
  (jobs.handoff_comment_id), else the PR's latest job's. Line bands read the first verdict's
  changed_lines, stored from the PR's additions plus deletions when the
  verdict names the PR's current head; older rows fall in unknown. A
  snapshot whose verdict_heads has no posted_at prints them unrecorded
- verdicts per merged PR: verdicts of PRs merged in the window
- CI fixes without a log: CI-fix jobs (instruction "CI failed on <head>"
  or the failed check names, "failed: <checks>") whose evidence carries no
  CI log tail
- verdicts on an accepted head: verdicts posted after another verdict on
  the same head, matched as the dispatcher matches a sha prefix, was
  accepted (review_events, ship intent or ship hold), timed by
  handled_times or the hold's held_at
- merged PRs with follow-ups: followup_filings filed or commented on a PR
  merged in the window, with the issue link

This module does not correct, reflect, run blinded evals, or maintain.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, assert_never

from lib.factory_md import FactoryConfig, load_factory_md
from lib.handoff import STATES, HandoffError, matching_head, parse_comment
from lib.load import Finding
from lib.orch_yml import load_orch_yml
from lib.schema import AUT0_REPO

EventKind = Literal["enter", "comment", "wake", "end"]
RiskName = Literal["low", "high"]

# Section 4.1 edges, in the kebab-case names lib.handoff.STATES uses.
# [*] is not a state. The two PostMerge --> Done arrows are one edge.
CHART_TRANSITIONS = frozenset(
    {
        ("intake", "refused"),
        ("intake", "questions"),
        ("questions", "waiting-max"),
        ("waiting-max", "questions"),
        ("questions", "planning"),
        ("questions", "building"),
        ("planning", "plan-check"),
        ("planning", "stuck"),
        ("plan-check", "planning"),
        ("plan-check", "building"),
        ("plan-check", "stuck"),
        ("plan-check", "waiting-max"),
        ("building", "review"),
        ("building", "building"),
        ("building", "stuck"),
        ("building", "waiting-max"),
        ("review", "fixing"),
        ("fixing", "review"),
        ("fixing", "ship"),
        ("fixing", "fixing"),
        ("fixing", "stuck"),
        ("review", "capped"),
        ("review", "blocked"),
        ("blocked", "review"),
        ("review", "ship"),
        ("review", "stuck"),
        ("ship", "review"),
        ("ship", "merged"),
        ("ship", "handed-off"),
        ("ship", "waiting-external"),
        ("ship", "awaiting-ok"),
        ("awaiting-ok", "merged"),
        ("awaiting-ok", "merge-queue"),
        ("awaiting-ok", "handed-off"),
        ("awaiting-ok", "review"),
        ("awaiting-ok", "fixing"),
        ("awaiting-ok", "capped"),
        ("waiting-external", "merged"),
        ("waiting-external", "fixing"),
        ("waiting-external", "stuck"),
        ("merged", "post-merge"),
        ("merged", "done"),
        ("post-merge", "done"),
        ("fixing", "waiting-max"),
        ("waiting-max", "building"),
        ("waiting-max", "fixing"),
        ("waiting-max", "abandoned"),
        ("review", "waiting-max"),
        ("awaiting-ok", "waiting-max"),
        ("waiting-max", "review"),
        ("capped", "fixing"),
        ("handed-off", "review"),
        ("handed-off", "fixing"),
        ("handed-off", "merged"),
        ("ship", "merge-queue"),
        ("merge-queue", "merged"),
        ("merge-queue", "updating"),
        ("merge-queue", "planning"),
        ("merge-queue", "building"),
        ("merge-queue", "review"),
        ("merge-queue", "abandoned"),
        ("updating", "merge-queue"),
        ("updating", "review"),
        ("updating", "stuck"),
    }
)

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_PR = re.compile(r"^[1-9][0-9]*$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TICKET_PATTERN = r"(?:[1-9][0-9]*|[A-Za-z][A-Za-z0-9]*-[1-9][0-9]*)"
_TICKET = re.compile(rf"^{_TICKET_PATTERN}$")
_STATE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOKEN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FINDING = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) "
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    rf"(?P<ticket>{_TICKET_PATTERN}) "
    r"(?P<rule>[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?: (?P<detail>.*))?$"
)

_ROW_KEYS = frozenset({"repo", "ticket", "pr"})
_EVENT_KEYS = {
    "enter": frozenset({"state"}),
    "comment": frozenset({"path"}),
    "wake": frozenset({"role", "reason", "wasted", "handled"}),
    "end": frozenset(),
}
_MESSAGE_KEYS = frozenset({"repo", "pr", "from", "to", "kind"})
_REQUIRED_KEYS = {
    "enter": frozenset({"state"}),
    "comment": frozenset({"path"}),
    "wake": frozenset({"role", "reason"}),
    "end": frozenset(),
}


class EvalError(ValueError):
    """The input directory is not a readable eval bundle."""


@dataclass(frozen=True)
class Wake:
    at: datetime
    repo: str
    ticket: str
    role: str
    reason: str


@dataclass(frozen=True)
class ModelBreak:
    at: datetime
    repo: str
    ticket: str
    rule: str
    detail: str


@dataclass(frozen=True)
class Mismatch:
    repo: str
    ticket: str
    code: str
    detail: str


@dataclass(frozen=True)
class Handoffs:
    repo: str
    week: str
    manual: int
    scripted: int


@dataclass(frozen=True)
class Report:
    runs: int
    skipped_lines: int
    seconds: tuple[tuple[str, int], ...]
    open_states: tuple[tuple[str, str, str], ...]
    wakes: tuple[Wake, ...]
    rounds: tuple[tuple[str, str, int], ...]
    stuck: tuple[tuple[str, str], ...]
    model: tuple[ModelBreak, ...] | None
    mismatches: tuple[Mismatch, ...]
    handoffs: tuple[Handoffs, ...]


@dataclass(frozen=True)
class Unrecorded:
    reason: str


@dataclass(frozen=True)
class NoRatio:
    pass


NO_RATIO = NoRatio()
MetricValue = int | float | Unrecorded | NoRatio
FollowUp = tuple[str, str, str]


@dataclass(frozen=True)
class MetricWindow:
    since: float | None = None
    until: float | None = None

    def sql(self, column: str) -> tuple[str, tuple[float, ...]]:
        clauses: list[str] = []
        params: list[float] = []
        if self.since is not None:
            clauses.append(f"{column} >= ?")
            params.append(self.since)
        if self.until is not None:
            clauses.append(f"{column} <= ?")
            params.append(self.until)
        return " AND ".join(clauses) or "1", tuple(params)

    def contains(self, at: float | None) -> bool:
        if at is None:
            return self.since is None and self.until is None
        if self.since is not None and at < self.since:
            return False
        return self.until is None or at <= self.until

    def text(self) -> str:
        start, end = (
            "-" if at is None else _format_time(datetime.fromtimestamp(at, timezone.utc))
            for at in (self.since, self.until)
        )
        return f"window {start} to {end}"


@dataclass(frozen=True)
class Metrics:
    values: tuple[tuple[str, MetricValue], ...]
    followups: tuple[FollowUp, ...] | Unrecorded = ()
    window: MetricWindow = MetricWindow()


@dataclass(frozen=True)
class Built:
    report: Report
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Window:
    start: datetime | None
    end: datetime | None

    def contains(self, at: datetime) -> bool:
        if self.start is not None and at < self.start:
            return False
        if self.end is not None and at > self.end:
            return False
        return True

    def clip(self, begin: datetime, finish: datetime) -> int:
        if self.start is not None:
            begin = max(begin, self.start)
        if self.end is not None:
            finish = min(finish, self.end)
        return max(0, int((finish - begin).total_seconds()))


@dataclass
class _Event:
    at: datetime
    repo: str
    ticket: str
    kind: EventKind
    fields: dict[str, str]


@dataclass(frozen=True)
class _Handoff:
    at: datetime
    repo: str


@dataclass(frozen=True)
class _Verdict:
    round: int
    risk: str | None
    go_risk: str | None
    extras: int
    in_window: bool


@dataclass
class _Run:
    repo: str
    ticket: str
    active: bool = False
    seconds: dict[str, int] = field(default_factory=dict)
    open_state: str | None = None
    open_at: datetime | None = None
    last_at: datetime | None = None
    entered: list[str] = field(default_factory=list)
    transitions: list[tuple[str | None, str]] = field(default_factory=list)
    go_risk: str | None = None
    extras: int = 0
    verdicts: list[_Verdict] = field(default_factory=list)
    stuck_comment: bool = False
    seen_handled: set[str] = field(default_factory=set)
    wakes: list[Wake] = field(default_factory=list)


def build_report(
    root: Path,
    *,
    since: str | None = None,
    until: str | None = None,
) -> Built:
    """Read root, measure every run, and report what falls in the window."""
    if not root.is_dir():
        raise EvalError(f"input directory not found: {root}")
    window = _Window(_bound(since, "since"), _bound(until, "until"))
    if window.start is not None and window.end is not None and window.start > window.end:
        raise EvalError("since is after until")

    warnings: list[str] = []
    repos = _load_orch(_top_level(root, "orch.yml"), warnings)
    events, messages, skipped = _load_log(_top_level(root, "dispatcher.log"))
    warnings.extend(f"{fault}; line skipped" for fault in skipped)
    for item in [*events, *messages]:
        if item.repo not in repos:
            raise EvalError(f"{item.repo} is not a project in orch.yml")
    configs = _load_factories(root, {event.repo for event in events}, warnings)
    findings = _load_findings(_top_level(root, "model-findings.txt"), window)
    runs = [run for run in _measure(root, events, window) if run.active]

    scripted = [
        _Handoff(at=event.at, repo=event.repo)
        for event in events
        if event.kind == "comment" and window.contains(event.at)
    ]
    manual = [item for item in messages if window.contains(item.at)]

    return Built(
        report=_assemble(runs, len(skipped), findings, configs, _handoffs(manual, scripted)),
        warnings=tuple(warnings),
    )


def append_message(
    log: Path,
    *,
    repo: str,
    pr: str | None,
    sender: str,
    recipient: str,
    kind: str,
    at: datetime,
) -> None:
    """Append one manual bot-to-bot message line, or raise and write nothing."""
    if _REPO.fullmatch(repo) is None:
        raise EvalError(f"bad repo {repo!r}: expected owner/name")
    if pr is not None and _PR.fullmatch(pr) is None:
        raise EvalError(f"bad pr {pr!r}")
    for name, value in (("from", sender), ("to", recipient), ("kind", kind)):
        if _TOKEN.fullmatch(value) is None:
            raise EvalError(f"bad {name} {value!r}")
    pr_field = "" if pr is None else f" pr={pr}"
    line = f"{_format_time(at)} repo={repo}{pr_field} message from={sender} to={recipient} kind={kind}\n"
    with log.open("ab") as handle:
        handle.write(f"\n{line}".encode("utf-8"))


def render_json(report: Report) -> str:
    return json.dumps(asdict(report), default=_format_time, indent=2) + "\n"


def render(report: Report) -> str:
    """Plain text. Section titles are the F1-14 measures."""
    lines = [
        "weekly eval report",
        f"runs: {report.runs}",
        f"skipped dispatcher.log lines: {report.skipped_lines}",
        "",
    ]
    lines.append("time in each state")
    if not report.seconds:
        lines.append("none")
    else:
        for state, seconds in report.seconds:
            lines.append(f"{state} {seconds}")
    lines.append("")
    lines.append("open states")
    if not report.open_states:
        lines.append("none")
    else:
        for repo, ticket, state in report.open_states:
            lines.append(f"{repo} {ticket} {state}")
    lines.append("")
    lines.append("wasted agent wakes")
    lines.append(f"count: {len(report.wakes)}")
    for wake in report.wakes:
        stamp = _format_time(wake.at)
        lines.append(f"{stamp} {wake.repo} {wake.ticket} {wake.role} {wake.reason}")
    lines.append("")
    lines.append("review rounds per PR")
    if not report.rounds:
        lines.append("none")
    else:
        for repo, ticket, rounds in report.rounds:
            lines.append(f"{repo} {ticket} {rounds}")
    lines.append("")
    lines.append("stuck jobs")
    if not report.stuck:
        lines.append("none")
    else:
        for repo, ticket in report.stuck:
            lines.append(f"{repo} {ticket}")
    lines.append("")
    lines.append("rule breaks the model finds")
    if report.model is None:
        lines.append("none supplied")
    elif not report.model:
        lines.append("none")
    else:
        for item in report.model:
            stamp = _format_time(item.at)
            detail = f" {item.detail}" if item.detail else ""
            lines.append(f"{stamp} {item.repo} {item.ticket} {item.rule}{detail}")
    lines.append("")
    lines.append("chart mismatches")
    if not report.mismatches:
        lines.append("none")
    else:
        for item in report.mismatches:
            if item.detail:
                lines.append(f"{item.repo} {item.ticket} {item.code} {item.detail}")
            else:
                lines.append(f"{item.repo} {item.ticket} {item.code}")
    lines.append("")
    lines.append("bot-to-bot messages vs scripted handoffs")
    if not report.handoffs:
        lines.append("none")
    for item in report.handoffs:
        lines.append(f"{item.repo} {item.week} manual={item.manual} scripted={item.scripted}")
    lines.append("token savings: not measured (no per-message token count in the input)")
    return "\n".join(lines) + "\n"


def _ticket_key(ticket: str) -> tuple[int, int, str]:
    if ticket.isdigit():
        return (0, int(ticket), "")
    return (1, 0, ticket.casefold())


def _run_key(repo: str, ticket: str) -> tuple[str, tuple[int, int, str]]:
    return (repo, _ticket_key(ticket))


def _handoffs(manual: list[_Handoff], scripted: list[_Handoff]) -> tuple[Handoffs, ...]:
    counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for column, items in ((0, manual), (1, scripted)):
        for item in items:
            counts[(item.repo, item.at.strftime("%G-W%V"))][column] += 1
    return tuple(
        Handoffs(repo=repo, week=week, manual=pair[0], scripted=pair[1])
        for (repo, week), pair in sorted(counts.items())
    )


def _assemble(
    runs: list[_Run],
    skipped_lines: int,
    findings: list[ModelBreak] | None,
    configs: dict[str, FactoryConfig],
    handoffs: tuple[Handoffs, ...],
) -> Report:
    totals: dict[str, int] = defaultdict(int)
    for run in runs:
        for state, seconds in run.seconds.items():
            totals[state] += seconds
    order = {name: index for index, name in enumerate(STATES)}
    seconds = tuple(
        sorted(
            totals.items(),
            key=lambda item: (item[0] not in order, order.get(item[0], 0), item[0]),
        )
    )
    open_states = tuple(
        sorted(
            (
                (run.repo, run.ticket, run.open_state)
                for run in runs
                if run.open_state is not None
            ),
            key=lambda item: _run_key(item[0], item[1]),
        )
    )
    wakes = tuple(
        sorted(
            (wake for run in runs for wake in run.wakes),
            key=lambda item: (
                item.at,
                *_run_key(item.repo, item.ticket),
                item.role,
                item.reason,
            ),
        )
    )
    rounds = tuple(
        sorted(
            ((run.repo, run.ticket, _rounds(run)) for run in runs),
            key=lambda item: _run_key(item[0], item[1]),
        )
    )
    stuck = tuple(
        sorted(
            ((run.repo, run.ticket) for run in runs if _is_stuck(run)),
            key=lambda item: _run_key(item[0], item[1]),
        )
    )
    mismatches: list[Mismatch] = []
    for run in runs:
        mismatches.extend(_mismatches(run, configs[run.repo]))
    mismatches.sort(
        key=lambda item: (*_run_key(item.repo, item.ticket), item.code, item.detail)
    )
    model = None
    if findings is not None:
        model = tuple(
            sorted(
                findings,
                key=lambda item: (item.at, *_run_key(item.repo, item.ticket), item.rule),
            )
        )
    return Report(
        runs=len(runs),
        skipped_lines=skipped_lines,
        seconds=seconds,
        open_states=open_states,
        wakes=wakes,
        rounds=rounds,
        stuck=stuck,
        model=model,
        mismatches=tuple(mismatches),
        handoffs=handoffs,
    )


def _measure(root: Path, events: list[_Event], window: _Window) -> list[_Run]:
    grouped: dict[tuple[str, str], _Run] = {}
    for event in events:
        key = (event.repo, event.ticket)
        run = grouped.get(key)
        if run is None:
            run = _Run(repo=event.repo, ticket=event.ticket)
            grouped[key] = run
        _apply(root, run, event, window)
    for run in grouped.values():
        if run.open_state is None or run.open_at is None:
            continue
        if window.end is None:
            run.active = True
        elif run.open_at <= window.end:
            _close(run, window.end, window)
        else:
            run.open_state = None
    return list(grouped.values())


def _apply(root: Path, run: _Run, event: _Event, window: _Window) -> None:
    if run.last_at is not None and event.at < run.last_at:
        raise EvalError(f"{run.repo} {run.ticket} goes backwards in dispatcher.log")
    run.last_at = event.at
    in_window = window.contains(event.at)
    if in_window:
        run.active = True
    kind = event.kind
    if kind == "enter":
        _enter(run, event, window, in_window)
        return
    if kind == "comment":
        _comment(root, run, event, in_window)
        return
    if kind == "wake":
        _wake(run, event, in_window)
        return
    if kind == "end":
        _close(run, event.at, window)
        return
    assert_never(kind)


def _enter(run: _Run, event: _Event, window: _Window, in_window: bool) -> None:
    state = event.fields["state"]
    if _STATE.fullmatch(state) is None:
        raise EvalError(f"bad state {state!r}")
    _close(run, event.at, window)
    previous = run.entered[-1] if run.entered else None
    if previous == "capped" and state == "fixing":
        run.extras += 1
    run.entered.append(state)
    run.open_state = state
    run.open_at = event.at
    if in_window:
        run.transitions.append((previous, state))
        run.seconds.setdefault(state, 0)


def _close(run: _Run, at: datetime, window: _Window) -> None:
    if run.open_state is None or run.open_at is None:
        return
    if at < run.open_at:
        raise EvalError(f"bad dwell time for {run.repo} {run.ticket}")
    clipped = window.clip(run.open_at, at)
    if clipped > 0:
        run.active = True
        run.seconds[run.open_state] = run.seconds.get(run.open_state, 0) + clipped
    run.open_state = None
    run.open_at = None


def _comment(root: Path, run: _Run, event: _Event, in_window: bool) -> None:
    path = _safe_path(root, event.fields["path"])
    if not path.is_file():
        raise EvalError(f"comment not found: {event.fields['path']}")
    try:
        handoff = parse_comment(path.read_text(encoding="utf-8"))
    except HandoffError as exc:
        raise EvalError(f"{event.fields['path']}: {exc}") from exc
    fields = handoff.as_dict()
    if fields["kind"] == "go" and "risk" in fields:
        run.go_risk = _risk(fields["risk"])
    if fields["kind"] == "verdict":
        number = len(run.verdicts) + 1
        if "round" in fields:
            number = int(fields["round"].split("/", 1)[0])
        run.verdicts.append(
            _Verdict(
                round=number,
                risk=_risk(fields["risk"]) if "risk" in fields else None,
                go_risk=run.go_risk,
                extras=run.extras,
                in_window=in_window,
            )
        )
    if fields["kind"] == "stuck" and in_window:
        run.stuck_comment = True


def _wake(run: _Run, event: _Event, in_window: bool) -> None:
    role = event.fields["role"]
    reason = event.fields["reason"]
    if _TOKEN.fullmatch(role) is None or _TOKEN.fullmatch(reason) is None:
        raise EvalError(f"bad wake for {run.repo} {run.ticket}")
    handled = event.fields.get("handled")
    if handled is not None and _TOKEN.fullmatch(handled) is None:
        raise EvalError(f"bad handled id for {run.repo} {run.ticket}")
    wasted_flag = event.fields.get("wasted")
    if wasted_flag is not None and wasted_flag != "yes":
        raise EvalError(f"bad wasted flag for {run.repo} {run.ticket}")
    repeated = handled is not None and handled in run.seen_handled
    if handled is not None:
        run.seen_handled.add(handled)
    if not in_window:
        return
    if wasted_flag == "yes" or reason == "nobody" or repeated:
        run.wakes.append(
            Wake(at=event.at, repo=run.repo, ticket=run.ticket, role=role, reason=reason)
        )


def _rounds(run: _Run) -> int:
    return max((item.round for item in run.verdicts if item.in_window), default=0)


def _is_stuck(run: _Run) -> bool:
    return run.stuck_comment or "stuck" in run.seconds or run.open_state == "stuck"


def _mismatches(run: _Run, config: FactoryConfig) -> list[Mismatch]:
    found: list[Mismatch] = []
    for previous, state in run.transitions:
        if state not in STATES:
            found.append(Mismatch(run.repo, run.ticket, "unknown-state", state))
        # A merge seen in any state ends the ticket (#108).
        if (
            previous is not None
            and (previous, state) not in CHART_TRANSITIONS
            and state != "merged"
        ):
            found.append(
                Mismatch(run.repo, run.ticket, "illegal-transition", f"{previous} {state}")
            )
    if any(state == "post-merge" for _previous, state in run.transitions):
        found.append(Mismatch(run.repo, run.ticket, "post-merge-in-phase-1", ""))
    if run.stuck_comment and "stuck" not in run.entered:
        found.append(Mismatch(run.repo, run.ticket, "stuck-comment-without-state", ""))
    found.extend(_cap_mismatches(run, config))
    return found


def _cap_mismatches(run: _Run, config: FactoryConfig) -> list[Mismatch]:
    found: list[Mismatch] = []
    for item in run.verdicts:
        if not item.in_window:
            continue
        risk = item.go_risk or item.risk
        if risk is None:
            found.append(Mismatch(run.repo, run.ticket, "cap-unverified", str(item.round)))
            continue
        if item.go_risk is not None and item.risk is not None and item.go_risk != item.risk:
            found.append(
                Mismatch(run.repo, run.ticket, "risk-mismatch", f"{item.go_risk} {item.risk}")
            )
        cap = _round_cap(config, _risk(risk))
        if item.round > cap + item.extras:
            found.append(
                Mismatch(
                    run.repo,
                    run.ticket,
                    "round-over-cap",
                    f"{risk} {item.round} {cap + item.extras}",
                )
            )
        elif item.round > cap:
            found.append(
                Mismatch(
                    run.repo,
                    run.ticket,
                    "round-extra-authorized",
                    f"{risk} {item.round} {cap}",
                )
            )
    return found


def _risk(value: str) -> RiskName:
    if value == "low":
        return "low"
    if value == "high":
        return "high"
    raise EvalError(f"bad risk {value!r}")


def _round_cap(config: FactoryConfig, risk: RiskName) -> int:
    if risk == "low":
        return config.rounds_low
    if risk == "high":
        return config.rounds_high
    assert_never(risk)


def _top_level(root: Path, name: str) -> Path:
    path = root / name
    if not _inside(root, path):
        raise EvalError(f"{name} leaves the input directory")
    return path


def _load_orch(path: Path, warnings: list[str]) -> set[str]:
    config, findings = load_orch_yml(path)
    _raise_errors(path, findings)
    warnings.extend(_warning_lines(path, findings))
    if config is None:
        raise EvalError(f"{path}: invalid orch.yml")
    return {project.repo for project in config.projects}


def _load_factories(
    root: Path,
    repos: set[str],
    warnings: list[str],
) -> dict[str, FactoryConfig]:
    configs: dict[str, FactoryConfig] = {}
    for repo in sorted(repos):
        path = root / "projects" / Path(repo) / "factory.md"
        if not _inside(root, path):
            raise EvalError(f"factory.md leaves the input directory: {repo}")
        config, findings = load_factory_md(path)
        _raise_errors(path, findings)
        warnings.extend(_warning_lines(path, findings))
        if config is None:
            raise EvalError(f"{path}: invalid factory.md")
        configs[repo] = config
    return configs


def _raise_errors(path: Path, findings: list[Finding]) -> None:
    for finding in findings:
        if finding.severity == "error":
            raise EvalError(finding.format(str(path)))


def _warning_lines(path: Path, findings: list[Finding]) -> list[str]:
    return [
        finding.format(str(path))
        for finding in findings
        if finding.severity == "warning"
    ]


def _load_log(path: Path) -> tuple[list[_Event], list[_Handoff], list[str]]:
    if not path.is_file():
        raise EvalError("dispatcher.log not found")
    events: list[_Event] = []
    messages: list[_Handoff] = []
    skipped: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            parsed = _parse_log_line(raw, number)
        except EvalError as fault:
            skipped.append(str(fault))
            continue
        if isinstance(parsed, _Event):
            events.append(parsed)
        elif isinstance(parsed, _Handoff):
            messages.append(parsed)
    return events, messages, skipped


def _parse_message(at: datetime, fields: dict[str, str], where: str) -> _Handoff:
    for key in fields:
        if key not in _MESSAGE_KEYS:
            raise EvalError(f"{where}: unknown key {key!r}")
    for key in ("repo", "from", "to"):
        if key not in fields:
            raise EvalError(f"{where}: missing {key}")
    if _REPO.fullmatch(fields["repo"]) is None:
        raise EvalError(f"{where}: bad repo")
    if "pr" in fields and _PR.fullmatch(fields["pr"]) is None:
        raise EvalError(f"{where}: bad pr")
    for key in ("from", "to", "kind"):
        if key in fields and _TOKEN.fullmatch(fields[key]) is None:
            raise EvalError(f"{where}: bad {key}")
    return _Handoff(at=at, repo=fields["repo"])


def _parse_log_line(raw: str, number: int) -> _Event | _Handoff | None:
    if raw.strip() == "":
        return None
    parts = raw.split()
    where = f"dispatcher.log:{number}"
    if _TIME.fullmatch(parts[0]) is None:
        raise EvalError(f"{where}: bad timestamp")
    fields: dict[str, str] = {}
    bare: list[str] = []
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            if _KEY.fullmatch(key) is None or _VALUE.fullmatch(value) is None:
                raise EvalError(f"{where}: bad attribute {token!r}")
            if key in fields:
                raise EvalError(f"{where}: duplicate key {key!r}")
            fields[key] = value
        else:
            bare.append(token)
    if len(bare) != 1:
        raise EvalError(f"{where}: expected one event")
    if bare[0] == "message":
        return _parse_message(_parse_time(parts[0], where), fields, where)
    kind = _event_kind(bare[0], number)
    allowed = _EVENT_KEYS[kind] | _ROW_KEYS
    for key in fields:
        if key not in allowed:
            raise EvalError(f"{where}: unknown key {key!r}")
    if "repo" not in fields or "ticket" not in fields:
        raise EvalError(f"{where}: missing repo or ticket")
    if _TICKET.fullmatch(fields["ticket"]) is None:
        raise EvalError(f"{where}: bad ticket")
    if "pr" in fields and _PR.fullmatch(fields["pr"]) is None:
        raise EvalError(f"{where}: bad pr")
    missing = _REQUIRED_KEYS[kind] - set(fields)
    if missing:
        name = sorted(missing)[0]
        raise EvalError(f"{where}: missing {name}")
    return _Event(
        at=_parse_time(parts[0], where),
        repo=fields["repo"],
        ticket=fields["ticket"],
        kind=kind,
        fields=fields,
    )


def _event_kind(value: str, number: int) -> EventKind:
    if value == "enter":
        return "enter"
    if value == "comment":
        return "comment"
    if value == "wake":
        return "wake"
    if value == "end":
        return "end"
    raise EvalError(f"dispatcher.log:{number}: unknown event {value!r}")


def _load_findings(path: Path, window: _Window) -> list[ModelBreak] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise EvalError("model-findings.txt is not a file")
    found: list[ModelBreak] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.strip() == "":
            continue
        where = f"model-findings.txt:{number}"
        match = _FINDING.fullmatch(raw.strip())
        if match is None:
            raise EvalError(f"{where}: bad finding")
        at = _parse_time(match.group("ts"), where)
        if not window.contains(at):
            continue
        found.append(
            ModelBreak(
                at=at,
                repo=match.group("repo"),
                ticket=match.group("ticket"),
                rule=match.group("rule"),
                detail=match.group("detail") or "",
            )
        )
    return found


def _inside(root: Path, path: Path) -> bool:
    resolved = path.resolve()
    root_resolved = root.resolve()
    return resolved == root_resolved or root_resolved in resolved.parents


def _safe_path(root: Path, raw: str) -> Path:
    if raw.startswith("/") or "\\" in raw:
        raise EvalError(f"comment path leaves the input directory: {raw}")
    parts = Path(raw).parts
    if any(part in ("..", "") for part in parts):
        raise EvalError(f"comment path leaves the input directory: {raw}")
    path = root.joinpath(*parts)
    if not _inside(root, path):
        raise EvalError(f"comment path leaves the input directory: {raw}")
    return path


def _bound(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    if _TIME.fullmatch(value) is None:
        raise EvalError(f"bad {name}")
    return _parse_time(value, name)


def _parse_time(value: str, where: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise EvalError(f"{where}: bad timestamp {value!r}") from None
    return parsed.replace(tzinfo=timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


APPROVED_STATES = (
    "awaiting-ok",
    "ship",
    "merge-queue",
    "updating",
    "handed-off",
    "merged",
    "done",
)
PREFLIGHT_FAILURE = re.compile(
    r"\S+ is unreachable over ssh \(|missing under bash -lc on "
    r"|bash not found on |preflight on \S+ exited "
)
REVISE_OUTCOMES = ("queued-review-fix", "answered-fix", "capped-review")
HELPER_TOOLS = ("Agent", "Task")
SKILL_KINDS = ("plan", "build", "fix", "review")
SKILL_METRICS = ("router-read-share", "skill-files-per-job", "skill-reads-unrecorded")
ROUTER_SKILLS = frozenset({"factory/factory-builder/SKILL.md", "factory/factory-reviewer/SKILL.md"})
GO_OUTCOMES = (
    *(
        f"{verb}-{action}"
        for verb in ("queued", "answered", "deduped")
        for action in ("plan", "build")
    ),
    "queued-fix",
    "queued-blocked-review",
)
GO_CAUSES = ("go", "extra-round")
HELD_APPROVE_OUTCOME = "held-base-update"
LINE_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("under-150", 0, 150),
    ("150-399", 150, 400),
    ("400-799", 400, 800),
    ("800-plus", 800, None),
)
_STOP_NAMES = (
    "hand-written-gos",
    "submitted-invalid",
    "repeat-block-stops",
    "unsatisfiable-stops",
)
WINDOWED_METRICS = (
    *_STOP_NAMES,
    "approved-by-round-2-share",
    "verdicts-per-merged-pr",
    "first-round-approve-rate-high-risk",
    *(f"first-round-approve-rate-lines-{band}" for band, _low, _high in LINE_BANDS),
    "first-round-approve-rate-lines-unknown",
    "ci-fix-without-log",
    "verdicts-on-accepted-head",
)


def build_metrics(db: Path, *, since: str | None = None, until: str | None = None) -> Metrics:
    window = MetricWindow(
        *(
            None if bound is None else bound.timestamp()
            for bound in (_bound(since, "--since"), _bound(until, "--until"))
        )
    )
    if not db.is_file():
        raise EvalError(f"database not found: {db}")
    try:
        connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise EvalError(f"{db}: {exc}") from exc
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        return Metrics(
            values=_metric_values(connection, tables, window),
            followups=_followups(connection, tables, window),
            window=window,
        )
    except sqlite3.DatabaseError as exc:
        raise EvalError(f"{db} is not a readable SQLite database: {exc}") from exc
    finally:
        connection.close()


def render_metrics(current: Metrics, baseline: Metrics) -> str:
    base = dict(baseline.values)
    lines = [
        "eval metrics",
        current.window.text(),
        f"windowed {' '.join(WINDOWED_METRICS)}",
        "metric current baseline",
    ]
    for name, value in current.values:
        lines.append(f"{name} {_metric_text(value)} {_metric_text(base[name])}")
    lines.append("merged PRs with follow-ups")
    if isinstance(current.followups, Unrecorded):
        lines.append(_metric_text(current.followups))
    else:
        lines.extend(" ".join(row) for row in current.followups)
        if not current.followups:
            lines.append("none")
    return "\n".join(lines) + "\n"


def _metric_text(value: MetricValue) -> str:
    if isinstance(value, Unrecorded):
        return f"unrecorded({value.reason})"
    if isinstance(value, NoRatio):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _missing(table: str) -> Unrecorded:
    return Unrecorded(f"no-{table}-table")


def _metric_values(
    connection: sqlite3.Connection,
    tables: set[str],
    window: MetricWindow,
) -> tuple[tuple[str, MetricValue], ...]:
    values: list[tuple[str, MetricValue]] = list(_review_metrics(connection, tables))
    values.append(("prs-capped", _capped(connection, tables)))
    values.append(
        (
            "prs-dirty-after-approval",
            _scalar(
                connection,
                tables,
                ("jobs",),
                "SELECT COUNT(*) FROM (SELECT DISTINCT lower(repo), ticket_id FROM jobs "
                "WHERE action = 'update-from-main')",
            ),
        )
    )
    values.append(
        (
            "duplicate-handoffs",
            _scalar(
                connection,
                tables,
                ("handled_comments",),
                "SELECT COUNT(*) FROM handled_comments WHERE outcome = 'duplicate-verdict'",
            ),
        )
    )
    values.append(
        (
            "refused-approvals",
            _scalar(
                connection,
                tables,
                ("handled_comments", "approve_rule_floor"),
                "SELECT COUNT(*) FROM handled_comments WHERE outcome LIKE 'rejected-approve-%'",
            ),
        )
    )
    values.append(
        (
            "blocked-stops",
            _scalar(
                connection,
                tables,
                ("ticket_state",),
                "SELECT COUNT(*) FROM ticket_state WHERE workflow_state = 'blocked'",
            ),
        )
    )
    values.append(
        (
            "tickets-succeeded",
            _scalar(
                connection,
                tables,
                ("ticket_state",),
                "SELECT COUNT(*) FROM ticket_state WHERE workflow_state IN ('merged', 'done') "
                "OR (lower(repo) = ? AND workflow_state = 'handed-off')",
                (AUT0_REPO,),
            ),
        )
    )
    values.extend(_skill_read_metrics(connection, tables))
    values.extend(_tool_metrics(connection, tables))
    values.append(("preflight-failures", _preflight_failures(connection, tables)))
    lost = _scalar(
        connection,
        tables,
        ("jobs", "job_telemetry"),
        "SELECT COALESCE(SUM(t.elapsed_seconds), 0) FROM job_telemetry t "
        "JOIN jobs j ON j.id = t.job_id WHERE j.state IN ('failed', 'cancelled')",
    )
    values.append(("slot-hours-lost", lost if isinstance(lost, Unrecorded) else lost / 3600))
    values.extend(_stop_metrics(connection, tables, window))
    values.extend(_loop_metrics(connection, tables, window))
    return tuple(values)


def _scalar(
    connection: sqlite3.Connection,
    tables: set[str],
    needs: tuple[str, ...],
    query: str,
    params: tuple[object, ...] = (),
) -> MetricValue:
    for table in needs:
        if table not in tables:
            return _missing(table)
    return connection.execute(query, params).fetchone()[0]


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _review_metrics(
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[tuple[str, MetricValue]]:
    rates = _first_verdict_rates(connection, tables)
    if "ticket_state" not in tables:
        missing = _missing("ticket_state")
        return [("prs-reviewed", missing), ("rounds-per-pr", missing), *rates]
    has_approval = "approve_comment_id" in _columns(connection, "ticket_state")
    approval = "approve_comment_id IS NOT NULL OR " if has_approval else ""
    marks = ", ".join("?" for _ in APPROVED_STATES)
    rows = connection.execute(
        f"SELECT review_round, ({approval}workflow_state IN ({marks})) FROM ticket_state",
        APPROVED_STATES,
    ).fetchall()
    reviewed = [(int(rounds or 0), bool(approved)) for rounds, approved in rows]
    reviewed = [(rounds, approved) for rounds, approved in reviewed if rounds or approved]
    count = len(reviewed)
    total = sum(rounds + int(approved) for rounds, approved in reviewed)
    return [
        ("prs-reviewed", count),
        ("rounds-per-pr", _ratio(total, count)),
        *rates,
    ]


def _ratio(part: float, whole: int) -> float | NoRatio:
    return part / whole if whole else NO_RATIO


def _first_verdict_rates(
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[tuple[str, MetricValue]]:
    names = ("first-round-revise-rate", "first-pass-approve-rate")
    for table in ("handled_comments", "review_events", "ship_intents"):
        if table not in tables:
            return [(name, _missing(table)) for name in names]
    approvals = _approvals_sql(tables)
    marks = ", ".join("?" for _ in REVISE_OUTCOMES)
    first: dict[tuple[str, str], str] = {}
    for repo, ticket_id, verdict in connection.execute(
        f"""
        SELECT v.repo, v.ticket_id, v.verdict FROM (
            SELECT repo, ticket_id, comment_id, 'REVISE' AS verdict, 0 AS held
            FROM review_events WHERE outcome IN ({marks})
            UNION ALL SELECT repo, ticket_id, comment_id, 'APPROVE', held FROM ({approvals})
        ) v
        LEFT JOIN handled_comments h
          ON lower(h.repo) = lower(v.repo) AND h.ticket_id = v.ticket_id
         AND h.comment_id = v.comment_id
        WHERE h.rowid IS NOT NULL OR v.held
        ORDER BY h.rowid IS NULL, h.rowid
        """,
        REVISE_OUTCOMES,
    ):
        first.setdefault((str(repo).lower(), str(ticket_id)), str(verdict))
    verdicts = list(first.values())
    return [
        ("first-round-revise-rate", _ratio(verdicts.count("REVISE"), len(verdicts))),
        ("first-pass-approve-rate", _ratio(verdicts.count("APPROVE"), len(verdicts))),
    ]


def _approvals_sql(tables: set[str]) -> str:
    sources = [
        "SELECT repo, ticket_id, comment_id, 0 AS held FROM ship_intents",
        "SELECT repo, ticket_id, comment_id, 1 FROM review_events "
        f"WHERE outcome = '{HELD_APPROVE_OUTCOME}'",
    ]
    if "ship_holds" in tables:
        sources.append("SELECT repo, ticket_id, approve_comment_id, 1 FROM ship_holds")
    if "verdict_acceptances" in tables:
        sources.append("SELECT repo, ticket_id, comment_id, 1 FROM verdict_acceptances")
    return " UNION ALL ".join(sources)


def _handled_count(
    connection: sqlite3.Connection,
    tables: set[str],
    window: MetricWindow,
    source: str,
    where: str,
    params: tuple[object, ...],
    needs: tuple[str, ...] = (),
) -> MetricValue:
    clause, bounds = window.sql("t.handled_at")
    return _scalar(
        connection,
        tables,
        (source, "handled_times", *needs),
        f"""
        SELECT COUNT(*) FROM {source} s
        LEFT JOIN handled_times t
          ON lower(t.repo) = lower(s.repo) AND t.ticket_id = s.ticket_id
         AND t.comment_id = s.comment_id
        WHERE {where} AND {clause}
        """,
        (*params, *bounds),
    )


def _stop_metrics(
    connection: sqlite3.Connection,
    tables: set[str],
    window: MetricWindow,
) -> list[tuple[str, MetricValue]]:
    marks = ", ".join("?" for _ in GO_OUTCOMES)
    gos = (
        _handled_count(
            connection,
            tables,
            window,
            "handled_comments",
            f"""
            (s.outcome IN ({marks}) OR (s.outcome LIKE 'resumed-%' AND EXISTS (
                SELECT 1 FROM jobs j
                WHERE lower(j.repo) = lower(s.repo) AND j.ticket_id = s.ticket_id
                  AND j.source_comment_id = s.comment_id AND json_valid(j.detail)
                  AND json_extract(j.detail, '$.evidence.instructions.cause') IN (?, ?)
            )))
            AND s.comment_id NOT IN
                (SELECT go_comment_id FROM feeder_items WHERE go_comment_id IS NOT NULL)
            """,
            (*GO_OUTCOMES, *GO_CAUSES),
            ("jobs",),
        )
        if "feeder_items" in tables
        else _missing("feeder_items")
    )
    clause, bounds = window.sql("recorded_at")
    return [
        ("hand-written-gos", gos),
        (
            "submitted-invalid",
            _scalar(
                connection,
                tables,
                ("job_events",),
                f"SELECT COUNT(*) FROM job_events WHERE event = 'submitted-invalid' AND {clause}",
                bounds,
            ),
        ),
        *(
            (
                name,
                _handled_count(
                    connection, tables, window, "review_events", "s.outcome = ?", (outcome,)
                ),
            )
            for name, outcome in (
                ("repeat-block-stops", "waiting-max-repeat"),
                ("unsatisfiable-stops", "waiting-max-unsat"),
            )
        ),
    ]


@dataclass(frozen=True)
class _HeadVerdict:
    comment_id: str
    head: str
    round: int
    posted_at: float | None
    changed_lines: int | None


def _loop_metrics(
    connection: sqlite3.Connection,
    tables: set[str],
    window: MetricWindow,
) -> list[tuple[str, MetricValue]]:
    measures = _verdict_measures(connection, tables, window)
    measures["ci-fix-without-log"] = (
        _ci_fixes_without_log(connection, window) if "jobs" in tables else _missing("jobs")
    )
    return [(name, measures[name]) for name in WINDOWED_METRICS if name not in _STOP_NAMES]


def _verdict_measures(
    connection: sqlite3.Connection,
    tables: set[str],
    window: MetricWindow,
) -> dict[str, MetricValue]:
    names = [
        name
        for name in WINDOWED_METRICS
        if name not in _STOP_NAMES and name != "ci-fix-without-log"
    ]
    for table in ("verdict_heads", "review_events", "ship_intents", "jobs", "ticket_transitions"):
        if table not in tables:
            return {name: _missing(table) for name in names}
    if "posted_at" not in _columns(connection, "verdict_heads"):
        return {name: Unrecorded("no-verdict_heads-posted_at-column") for name in names}
    approvals = {
        str(row[0])
        for row in connection.execute(f"SELECT comment_id FROM ({_approvals_sql(tables)})")
    }
    verdicts = _head_verdicts(connection)
    cohort = {key: rows for key, rows in verdicts.items() if window.contains(rows[0].posted_at)}
    risks = _risks(connection, {key: rows[0].comment_id for key, rows in cohort.items()})
    by_round_2 = sum(
        any(row.comment_id in approvals and row.round <= 2 for row in rows)
        for rows in cohort.values()
    )
    first_approved = {
        key: any(row.comment_id in approvals and row.round <= 1 for row in rows)
        for key, rows in cohort.items()
    }
    high = [key for key in cohort if risks[key] == "high"]
    clause, bounds = window.sql("at")
    merged = {
        (str(repo), str(ticket_id))
        for repo, ticket_id in connection.execute(
            "SELECT DISTINCT lower(repo), ticket_id FROM ticket_transitions "
            f"WHERE to_state = 'merged' AND {clause}",
            bounds,
        )
    }
    measures: dict[str, MetricValue] = {
        "approved-by-round-2-share": _ratio(by_round_2, len(cohort)),
        "verdicts-per-merged-pr": _ratio(
            sum(len(verdicts.get(key, ())) for key in merged), len(merged)
        ),
        "first-round-approve-rate-high-risk": _ratio(
            sum(first_approved[key] for key in high), len(high)
        ),
    }
    for band, low, high_edge in (*LINE_BANDS, ("unknown", None, None)):
        members = [
            key for key, rows in cohort.items() if _in_band(rows[0].changed_lines, low, high_edge)
        ]
        measures[f"first-round-approve-rate-lines-{band}"] = _ratio(
            sum(first_approved[key] for key in members), len(members)
        )
    measures["verdicts-on-accepted-head"] = (
        _verdicts_on_accepted_head(connection, tables, window, verdicts)
        if "handled_times" in tables
        else _missing("handled_times")
    )
    return measures


def _verdicts_on_accepted_head(
    connection: sqlite3.Connection,
    tables: set[str],
    window: MetricWindow,
    verdicts: dict[tuple[str, str], list[_HeadVerdict]],
) -> int:
    accepted_sources = "".join(
        f" UNION ALL SELECT {columns} FROM {table}"
        for table, columns in (
            ("ship_holds", "approve_comment_id, held_at"),
            ("verdict_acceptances", "comment_id, accepted_at"),
        )
        if table in tables
    )
    accepted_at: dict[str, float] = {}
    for comment_id, at in connection.execute(
        f"""
        SELECT a.comment_id, MIN(a.at) FROM (
            SELECT comment_id, handled_at AS at FROM handled_times{accepted_sources}
        ) a
        JOIN (
            SELECT comment_id FROM review_events
            UNION SELECT comment_id FROM ({_approvals_sql(tables)})
        ) v ON v.comment_id = a.comment_id
        GROUP BY a.comment_id
        """
    ):
        accepted_at[str(comment_id)] = float(at)
    return sum(
        window.contains(later.posted_at)
        and any(
            earlier.comment_id != later.comment_id
            and earlier.comment_id in accepted_at
            and accepted_at[earlier.comment_id] < later.posted_at
            and matching_head(earlier.head, later.head) is not None
            for earlier in rows
        )
        for rows in verdicts.values()
        for later in rows
        if later.posted_at is not None
    )


def _in_band(lines: int | None, low: int | None, high: int | None) -> bool:
    if low is None:
        return lines is None
    return lines is not None and lines >= low and (high is None or lines < high)


def _head_verdicts(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], list[_HeadVerdict]]:
    sized = "changed_lines" in _columns(connection, "verdict_heads")
    lines = "changed_lines" if sized else "NULL"
    verdicts: dict[tuple[str, str], list[_HeadVerdict]] = defaultdict(list)
    for repo, ticket_id, comment_id, head, review_round, posted_at, changed in connection.execute(
        f"""
        SELECT lower(repo), ticket_id, comment_id, head, review_round, posted_at, {lines}
        FROM verdict_heads ORDER BY posted_at IS NULL, posted_at, rowid
        """
    ):
        rows = verdicts[(str(repo), str(ticket_id))]
        rows.append(
            _HeadVerdict(
                comment_id=str(comment_id),
                head=str(head or ""),
                round=int(review_round) if review_round is not None else len(rows) + 1,
                posted_at=posted_at,
                changed_lines=changed,
            )
        )
    return verdicts


def _risks(
    connection: sqlite3.Connection,
    first_comment: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    by_comment: dict[str, str] = {}
    latest: dict[tuple[str, str], str] = {}
    for repo, ticket_id, comment_id, risk in connection.execute(
        """
        SELECT lower(repo), ticket_id, handoff_comment_id, risk FROM (
            SELECT repo, ticket_id, handoff_comment_id, created_at, rowid AS id,
                   CASE WHEN json_valid(detail) THEN json_extract(detail, '$.risk') END AS risk
            FROM jobs
        ) WHERE risk IS NOT NULL ORDER BY created_at, id
        """
    ):
        latest[(str(repo), str(ticket_id))] = str(risk)
        if comment_id is not None:
            by_comment[str(comment_id)] = str(risk)
    return {
        key: by_comment.get(comment_id) or latest.get(key, "unknown")
        for key, comment_id in first_comment.items()
    }


def _ci_fixes_without_log(connection: sqlite3.Connection, window: MetricWindow) -> int:
    clause, bounds = window.sql("created_at")
    return connection.execute(
        f"""
        SELECT COUNT(*) FROM jobs WHERE action = 'fix' AND {clause}
          AND CASE WHEN json_valid(detail) THEN
              (json_extract(detail, '$.instruction') = 'CI failed on ' || head
               OR json_extract(detail, '$.instruction') LIKE 'failed: %')
              AND COALESCE(json_extract(detail, '$.evidence.ci.log_tail'), '') = ''
          END
        """,
        bounds,
    ).fetchone()[0]


def _followups(
    connection: sqlite3.Connection,
    tables: set[str],
    window: MetricWindow,
) -> tuple[FollowUp, ...] | Unrecorded:
    for table in ("followup_filings", "ticket_transitions"):
        if table not in tables:
            return _missing(table)
    clause, bounds = window.sql("t.at")
    return tuple(
        (str(repo), str(ticket_id), f"https://github.com/{repo}/issues/{issue}")
        for repo, ticket_id, issue in connection.execute(
            f"""
            SELECT DISTINCT f.repo, f.ticket_id, f.issue_number FROM followup_filings f
            JOIN ticket_transitions t
              ON lower(t.repo) = lower(f.repo) AND t.ticket_id = f.ticket_id
             AND t.to_state = 'merged'
            WHERE f.status IN ('filed', 'commented') AND f.issue_number IS NOT NULL
              AND {clause}
            ORDER BY f.repo, f.ticket_id, f.issue_number
            """,
            bounds,
        )
    )


def _preflight_failures(connection: sqlite3.Connection, tables: set[str]) -> MetricValue:
    if "job_events" not in tables:
        return _missing("job_events")
    return sum(
        1
        for (detail,) in connection.execute(
            "SELECT detail FROM job_events WHERE event = 'requeued-unstarted'"
        )
        if PREFLIGHT_FAILURE.match(str(detail or ""))
    )


def _capped(connection: sqlite3.Connection, tables: set[str]) -> MetricValue:
    parts: list[str] = []
    if "ticket_state" in tables:
        parts.append(
            "SELECT lower(repo), ticket_id FROM ticket_state WHERE workflow_state = 'capped'"
        )
    if "review_events" in tables:
        parts.append(
            "SELECT lower(repo), ticket_id FROM review_events "
            "WHERE outcome IN ('capped-review', 'capped-patch-review')"
        )
    if not parts:
        return _missing("ticket_state")
    return connection.execute(f"SELECT COUNT(*) FROM ({' UNION '.join(parts)})").fetchone()[0]


def _skill_read_metrics(
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[tuple[str, MetricValue]]:
    names = [f"{metric}-{kind}" for kind in SKILL_KINDS for metric in SKILL_METRICS]
    for table in ("job_telemetry", "jobs"):
        if table not in tables:
            return [(name, _missing(table)) for name in names]
    if "skill_reads" not in _columns(connection, "job_telemetry"):
        return [(name, Unrecorded("no-skill_reads-column")) for name in names]
    recorded: dict[str, list[frozenset[str]]] = {kind: [] for kind in SKILL_KINDS}
    unrecorded = dict.fromkeys(SKILL_KINDS, 0)
    for action, detail, raw in connection.execute(
        "SELECT j.action, j.detail, t.skill_reads FROM jobs j "
        "LEFT JOIN job_telemetry t ON t.job_id = j.id "
        "WHERE j.state IN ('done', 'failed') "
        f"AND j.action IN ({', '.join('?' * len(SKILL_KINDS))})",
        SKILL_KINDS,
    ):
        if _merge_only(detail):
            continue
        files = _skill_files(raw)
        if files is None:
            unrecorded[action] += 1
        else:
            recorded[action].append(files)
    values: list[tuple[str, MetricValue]] = []
    for kind in SKILL_KINDS:
        jobs = recorded[kind]
        none = Unrecorded("no-skill-reads")
        values += [
            (
                f"router-read-share-{kind}",
                sum(bool(files & ROUTER_SKILLS) for files in jobs) / len(jobs) if jobs else none,
            ),
            (f"skill-files-per-job-{kind}", sum(map(len, jobs)) / len(jobs) if jobs else none),
            (f"skill-reads-unrecorded-{kind}", unrecorded[kind]),
        ]
    return values


def _merge_only(detail: object) -> bool:
    try:
        value = json.loads(detail)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("kind") == "merge-only"


def _skill_files(raw: object) -> frozenset[str] | None:
    try:
        files = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
        return None
    return frozenset(files)


def _tool_metrics(
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[tuple[str, MetricValue]]:
    names = ("helpers-per-job", "helpers-per-job-max")
    if "job_telemetry" not in tables:
        return [(name, _missing("job_telemetry")) for name in names]
    helpers: list[int] = []
    for (raw,) in connection.execute(
        "SELECT tool_calls FROM job_telemetry WHERE harness = 'claude'"
    ):
        try:
            counts = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(counts, dict):
            continue
        helpers.append(sum(_count(counts.get(tool)) for tool in HELPER_TOOLS))
    if not helpers:
        return [(name, NO_RATIO) for name in names]
    return [
        ("helpers-per-job", sum(helpers) / len(helpers)),
        ("helpers-per-job-max", max(helpers)),
    ]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
