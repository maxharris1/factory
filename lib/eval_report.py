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

This module does not correct, reflect, run blinded evals, or maintain.

TODO(#50): count bot-to-bot messages against scripted handoffs for the
token-savings metric. Nothing logs a manual bot-to-bot handoff yet, so
there is no input line to count.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, assert_never

from lib.factory_md import FactoryConfig, load_factory_md
from lib.handoff import STATES, HandoffError, parse_comment
from lib.load import Finding
from lib.orch_yml import load_orch_yml

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
        ("fixing", "stuck"),
        ("review", "capped"),
        ("review", "blocked"),
        ("review", "ship"),
        ("review", "stuck"),
        ("ship", "review"),
        ("ship", "merged"),
        ("ship", "handed-off"),
        ("ship", "waiting-external"),
        ("ship", "awaiting-ok"),
        ("awaiting-ok", "merged"),
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
        ("capped", "fixing"),
    }
)

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_PR = re.compile(r"^[1-9][0-9]*$")
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
class Report:
    runs: int
    seconds: tuple[tuple[str, int], ...]
    open_states: tuple[tuple[str, str, str], ...]
    wakes: tuple[Wake, ...]
    rounds: tuple[tuple[str, str, int], ...]
    stuck: tuple[tuple[str, str], ...]
    model: tuple[ModelBreak, ...] | None
    mismatches: tuple[Mismatch, ...]


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
    events = _load_log(_top_level(root, "dispatcher.log"))
    for event in events:
        if event.repo not in repos:
            raise EvalError(f"{event.repo} is not a project in orch.yml")
    configs = _load_factories(root, {event.repo for event in events}, warnings)
    findings = _load_findings(_top_level(root, "model-findings.txt"), window)
    runs = [run for run in _measure(root, events, window) if run.active]

    return Built(
        report=_assemble(runs, findings, configs),
        warnings=tuple(warnings),
    )


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
    return "\n".join(lines) + "\n"


def _ticket_key(ticket: str) -> tuple[int, int, str]:
    if ticket.isdigit():
        return (0, int(ticket), "")
    return (1, 0, ticket.casefold())


def _run_key(repo: str, ticket: str) -> tuple[str, tuple[int, int, str]]:
    return (repo, _ticket_key(ticket))


def _assemble(
    runs: list[_Run],
    findings: list[ModelBreak] | None,
    configs: dict[str, FactoryConfig],
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
        if previous is not None and (previous, state) not in CHART_TRANSITIONS:
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


def _load_log(path: Path) -> list[_Event]:
    if not path.is_file():
        raise EvalError("dispatcher.log not found")
    events: list[_Event] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        event = _parse_log_line(raw, number)
        if event is not None:
            events.append(event)
    return events


def _parse_log_line(raw: str, number: int) -> _Event | None:
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
