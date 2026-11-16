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
snapshot prints as unrecorded:

- rounds per PR: a PR is reviewed once it has a REVISE round
  (ticket_state.review_round) or an approval; its rounds are its REVISE
  rounds plus the approving one
- first-round REVISE and first-pass APPROVE rates: each PR's first acted-on
  verdict, in handled_comments order: a REVISE recorded in review_events,
  or an APPROVE recorded as a ship intent or ship hold. They read verdict
  history only, so a re-entry after handoff or an update-from-main
  re-review does not change them. A held APPROVE stays unhandled until its
  hold clears, so it counts as that PR's latest verdict until then
- PRs capped: capped now, or a capped review_events outcome
- PRs dirty after approval: tickets with an update-from-main job
- duplicate handoffs: duplicate-verdict handled comments
- BLOCKED stops: tickets in blocked
- tickets succeeded: merged or done; Aut0 counts at handed-off (Ready for
  Review)
- Skills read and helpers per job: #100's job_telemetry tool counts
  (Skill; Agent and Task) over Claude jobs. D25 sets no helper cap, so the
  max is printed too
- preflight failures: requeued-unstarted job events whose reason is one
  lib.remote.preflight raises (PREFLIGHT_FAILURE). An unreachable device
  counts: preflight is a job's first ssh call
- slot-hours lost: telemetry elapsed time of failed and cancelled jobs
- hand-written gos, ac=unmet with APPROVE, submitted-invalid, repeat-block
  and unsatisfiable stops: nothing records these yet

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
from lib.handoff import STATES, HandoffError, parse_comment
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
    seconds: tuple[tuple[str, int], ...]
    open_states: tuple[tuple[str, str, str], ...]
    wakes: tuple[Wake, ...]
    rounds: tuple[tuple[str, str, int], ...]
    stuck: tuple[tuple[str, str], ...]
    model: tuple[ModelBreak, ...] | None
    mismatches: tuple[Mismatch, ...]
    handoffs: tuple[Handoffs, ...]


@dataclass(frozen=True)
class Metrics:
    values: tuple[tuple[str, int | float | None], ...]


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
    events, messages = _load_log(_top_level(root, "dispatcher.log"))
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
        report=_assemble(runs, findings, configs, _handoffs(manual, scripted)),
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
    with log.open("a+b") as handle:
        if handle.tell() > 0:
            handle.seek(handle.tell() - 1)
            if handle.read(1) != b"\n":
                line = "\n" + line
        handle.write(line.encode("utf-8"))


def render_json(report: Report) -> str:
    return json.dumps(asdict(report), default=_format_time, indent=2) + "\n"


def render(report: Report) -> str:
    """Plain text. Section titles are the F1-14 measures."""
    lines = ["weekly eval report", f"runs: {report.runs}", ""]
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


def _load_log(path: Path) -> tuple[list[_Event], list[_Handoff]]:
    if not path.is_file():
        raise EvalError("dispatcher.log not found")
    events: list[_Event] = []
    messages: list[_Handoff] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = _parse_log_line(raw, number)
        if isinstance(parsed, _Event):
            events.append(parsed)
        elif isinstance(parsed, _Handoff):
            messages.append(parsed)
    return events, messages


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
UNRECORDED_METRICS = (
    "hand-written-gos",
    "ac-unmet-approve",
    "submitted-invalid",
    "repeat-block-stops",
    "unsatisfiable-stops",
)
HELPER_TOOLS = ("Agent", "Task")


def build_metrics(db: Path) -> Metrics:
    """Read an orch.db snapshot read-only; None marks a metric it does not record."""
    if not db.is_file():
        raise EvalError(f"database not found: {db}")
    try:
        connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise EvalError(f"{db}: {exc}") from exc
    try:
        return Metrics(values=_metric_values(connection))
    except sqlite3.DatabaseError as exc:
        raise EvalError(f"{db} is not a readable SQLite database: {exc}") from exc
    finally:
        connection.close()


def render_metrics(current: Metrics, baseline: Metrics) -> str:
    base = dict(baseline.values)
    lines = ["eval metrics", "metric current baseline"]
    for name, value in current.values:
        lines.append(f"{name} {_metric_text(value)} {_metric_text(base.get(name))}")
    return "\n".join(lines) + "\n"


def _metric_text(value: int | float | None) -> str:
    if value is None:
        return "unrecorded"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _metric_values(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, int | float | None], ...]:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    values: list[tuple[str, int | float | None]] = list(_review_metrics(connection, tables))
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
    values.extend(_tool_metrics(connection, tables))
    values.append(("preflight-failures", _preflight_failures(connection, tables)))
    lost = _scalar(
        connection,
        tables,
        ("jobs", "job_telemetry"),
        "SELECT COALESCE(SUM(t.elapsed_seconds), 0) FROM job_telemetry t "
        "JOIN jobs j ON j.id = t.job_id WHERE j.state IN ('failed', 'cancelled')",
    )
    values.append(("slot-hours-lost", None if lost is None else lost / 3600))
    values.extend((name, None) for name in UNRECORDED_METRICS)
    return tuple(values)


def _scalar(
    connection: sqlite3.Connection,
    tables: set[str],
    needs: tuple[str, ...],
    query: str,
    params: tuple[object, ...] = (),
) -> int | float | None:
    if not set(needs) <= tables:
        return None
    return connection.execute(query, params).fetchone()[0]


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _review_metrics(
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[tuple[str, int | float | None]]:
    rates = _first_verdict_rates(connection, tables)
    if "ticket_state" not in tables:
        return [("prs-reviewed", None), ("rounds-per-pr", None), *rates]
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
    if count == 0:
        return [("prs-reviewed", 0), ("rounds-per-pr", None), *rates]
    total = sum(rounds + int(approved) for rounds, approved in reviewed)
    return [
        ("prs-reviewed", count),
        ("rounds-per-pr", total / count),
        *rates,
    ]


def _first_verdict_rates(
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[tuple[str, float | None]]:
    names = ("first-round-revise-rate", "first-pass-approve-rate")
    if not {"handled_comments", "review_events", "ship_intents"} <= tables:
        return [(name, None) for name in names]
    approvals = ["SELECT repo, ticket_id, comment_id, 'APPROVE', 0 FROM ship_intents"]
    if "ship_holds" in tables:
        approvals.append(
            "SELECT repo, ticket_id, approve_comment_id, 'APPROVE', 1 FROM ship_holds"
        )
    marks = ", ".join("?" for _ in REVISE_OUTCOMES)
    first: dict[tuple[str, str], str] = {}
    for repo, ticket_id, verdict in connection.execute(
        f"""
        SELECT v.repo, v.ticket_id, v.verdict FROM (
            SELECT repo, ticket_id, comment_id, 'REVISE' AS verdict, 0 AS held
            FROM review_events WHERE outcome IN ({marks})
            UNION ALL {" UNION ALL ".join(approvals)}
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
    if not first:
        return [(name, None) for name in names]
    verdicts = list(first.values())
    return [
        ("first-round-revise-rate", verdicts.count("REVISE") / len(verdicts)),
        ("first-pass-approve-rate", verdicts.count("APPROVE") / len(verdicts)),
    ]


def _preflight_failures(connection: sqlite3.Connection, tables: set[str]) -> int | None:
    if "job_events" not in tables:
        return None
    return sum(
        1
        for (detail,) in connection.execute(
            "SELECT detail FROM job_events WHERE event = 'requeued-unstarted'"
        )
        if PREFLIGHT_FAILURE.match(str(detail or ""))
    )


def _capped(connection: sqlite3.Connection, tables: set[str]) -> int | None:
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
        return None
    return connection.execute(f"SELECT COUNT(*) FROM ({' UNION '.join(parts)})").fetchone()[0]


def _tool_metrics(
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[tuple[str, int | float | None]]:
    names = ("skills-read-per-job", "helpers-per-job", "helpers-per-job-max")
    if "job_telemetry" not in tables:
        return [(name, None) for name in names]
    skills: list[int] = []
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
        skills.append(_count(counts.get("Skill")))
        helpers.append(sum(_count(counts.get(tool)) for tool in HELPER_TOOLS))
    if not skills:
        return [(name, None) for name in names]
    return [
        ("skills-read-per-job", sum(skills) / len(skills)),
        ("helpers-per-job", sum(helpers) / len(helpers)),
        ("helpers-per-job-max", max(helpers)),
    ]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
