"""Weekly eval report (docs/SPEC.md section 11 and F1-14).

The report reads one directory. It does not contact GitHub or Linear.
Fixtures use that directory until F1-10 has logs. A weekly run points
--input at the collected directory and may set --since and --until
(inclusive, UTC, YYYY-MM-DDTHH:MM:SSZ).

Inputs, all local:

- orch.yml, validated by lib.orch_yml
- projects/<repo>/factory.md, validated by lib.factory_md
- dispatcher.log, one event per line
- comment files named by the log, parsed by lib.handoff
- model-findings.txt, optional lines the F1-13 model wrote

dispatcher.log lines:

    TIMESTAMP repo=<owner/name> pr=<n> enter state=<state>
    TIMESTAMP repo=<owner/name> pr=<n> comment path=<relative>
    TIMESTAMP repo=<owner/name> pr=<n> wake role=<token> reason=<token> [wasted=yes] [handled=<token>]
    TIMESTAMP repo=<owner/name> pr=<n> end

State dwell time is the gap between an enter and the next enter or end
for that PR. An enter with no later end stays open and adds no seconds.
Chart comparison uses the section 4.1 edges. post-merge is on the chart
and is still reported, because Phase 1 configures no post_merge.

A wake is wasted when wasted=yes, when reason=nobody, or when handled
repeats for that PR. Review rounds are the max verdict round numerator,
or the verdict count when no verdict carries round. Ready comments are
not rounds. A verdict with no risk is not checked against the round cap.

model-findings.txt lines:

    TIMESTAMP <owner/name> <pr> <rule> <detail>

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
        ("handed-off", "review"),
        ("handed-off", "merged"),
    }
)

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_PR = re.compile(r"^[1-9][0-9]*$")
_STATE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOKEN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FINDING = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) "
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    r"(?P<pr>[1-9][0-9]*) "
    r"(?P<rule>[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?: (?P<detail>.*))?$"
)

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
    pr: int
    role: str
    reason: str


@dataclass(frozen=True)
class ModelBreak:
    at: datetime
    repo: str
    pr: int
    rule: str
    detail: str


@dataclass(frozen=True)
class Mismatch:
    repo: str
    pr: int
    code: str
    detail: str


@dataclass(frozen=True)
class Report:
    runs: int
    seconds: tuple[tuple[str, int], ...]
    wakes: tuple[Wake, ...]
    rounds: tuple[tuple[str, int, int], ...]
    stuck: tuple[tuple[str, int], ...]
    model: tuple[ModelBreak, ...]
    mismatches: tuple[Mismatch, ...]


@dataclass(frozen=True)
class Built:
    report: Report
    warnings: tuple[str, ...]


@dataclass
class _Event:
    at: datetime
    repo: str
    pr: int
    kind: EventKind
    fields: dict[str, str]


@dataclass
class _Run:
    repo: str
    pr: int
    seconds: dict[str, int] = field(default_factory=dict)
    open_state: str | None = None
    open_at: datetime | None = None
    last_at: datetime | None = None
    entered: list[str] = field(default_factory=list)
    verdicts: list[dict[str, str]] = field(default_factory=list)
    stuck_comment: bool = False
    seen_handled: set[str] = field(default_factory=set)
    wakes: list[Wake] = field(default_factory=list)


def build_report(
    root: Path,
    *,
    since: str | None = None,
    until: str | None = None,
) -> Built:
    """Read root and measure the runs whose events fall in the window."""
    if not root.is_dir():
        raise EvalError(f"input directory not found: {root}")
    start = _bound(since, "since")
    end = _bound(until, "until")
    if start is not None and end is not None and start > end:
        raise EvalError("since is after until")

    warnings: list[str] = []
    repos = _load_orch(root / "orch.yml", warnings)
    events = [
        event
        for event in _load_log(root / "dispatcher.log")
        if _in_window(event.at, start, end)
    ]
    for event in events:
        if event.repo not in repos:
            raise EvalError(f"{event.repo} is not a project in orch.yml")
    configs = _load_factories(root, {event.repo for event in events}, warnings)
    findings = _load_findings(root / "model-findings.txt", start, end)
    runs = _measure(root, events)

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
    lines.append("wasted agent wakes")
    lines.append(f"count: {len(report.wakes)}")
    for wake in report.wakes:
        stamp = _format_time(wake.at)
        lines.append(f"{stamp} {wake.repo} {wake.pr} {wake.role} {wake.reason}")
    lines.append("")
    lines.append("review rounds per PR")
    if not report.rounds:
        lines.append("none")
    else:
        for repo, pr, rounds in report.rounds:
            lines.append(f"{repo} {pr} {rounds}")
    lines.append("")
    lines.append("stuck jobs")
    if not report.stuck:
        lines.append("none")
    else:
        for repo, pr in report.stuck:
            lines.append(f"{repo} {pr}")
    lines.append("")
    lines.append("rule breaks the model finds")
    if not report.model:
        lines.append("none")
    else:
        for item in report.model:
            stamp = _format_time(item.at)
            detail = f" {item.detail}" if item.detail else ""
            lines.append(f"{stamp} {item.repo} {item.pr} {item.rule}{detail}")
    lines.append("")
    lines.append("chart mismatches")
    if not report.mismatches:
        lines.append("none")
    else:
        for item in report.mismatches:
            if item.detail:
                lines.append(f"{item.repo} {item.pr} {item.code} {item.detail}")
            else:
                lines.append(f"{item.repo} {item.pr} {item.code}")
    return "\n".join(lines) + "\n"


def _assemble(
    runs: list[_Run],
    findings: list[ModelBreak],
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
    wakes = tuple(
        sorted(
            (wake for run in runs for wake in run.wakes),
            key=lambda item: (item.at, item.repo, item.pr, item.role, item.reason),
        )
    )
    rounds = tuple(
        sorted(
            ((run.repo, run.pr, _rounds(run)) for run in runs),
            key=lambda item: (item[0], item[1]),
        )
    )
    stuck = tuple(
        sorted(
            ((run.repo, run.pr) for run in runs if _is_stuck(run)),
            key=lambda item: (item[0], item[1]),
        )
    )
    mismatches: list[Mismatch] = []
    for run in runs:
        mismatches.extend(_mismatches(run, configs[run.repo]))
    mismatches.sort(key=lambda item: (item.repo, item.pr, item.code, item.detail))
    model = tuple(sorted(findings, key=lambda item: (item.at, item.repo, item.pr, item.rule)))
    return Report(
        runs=len(runs),
        seconds=seconds,
        wakes=wakes,
        rounds=rounds,
        stuck=stuck,
        model=model,
        mismatches=tuple(mismatches),
    )


def _measure(root: Path, events: list[_Event]) -> list[_Run]:
    grouped: dict[tuple[str, int], _Run] = {}
    order: list[tuple[str, int]] = []
    for event in events:
        key = (event.repo, event.pr)
        run = grouped.get(key)
        if run is None:
            run = _Run(repo=event.repo, pr=event.pr)
            grouped[key] = run
            order.append(key)
        _apply(root, run, event)
    return [grouped[key] for key in order]


def _apply(root: Path, run: _Run, event: _Event) -> None:
    if run.last_at is not None and event.at < run.last_at:
        raise EvalError(f"{run.repo} {run.pr} goes backwards in dispatcher.log")
    run.last_at = event.at
    kind = event.kind
    if kind == "enter":
        _enter(run, event)
        return
    if kind == "comment":
        _comment(root, run, event)
        return
    if kind == "wake":
        _wake(run, event)
        return
    if kind == "end":
        _close(run, event.at)
        return
    assert_never(kind)


def _enter(run: _Run, event: _Event) -> None:
    state = event.fields["state"]
    if _STATE.fullmatch(state) is None:
        raise EvalError(f"bad state {state!r}")
    _close(run, event.at)
    run.open_state = state
    run.open_at = event.at
    run.entered.append(state)
    run.seconds.setdefault(state, 0)


def _close(run: _Run, at: datetime) -> None:
    if run.open_state is None or run.open_at is None:
        return
    delta = (at - run.open_at).total_seconds()
    if delta < 0 or delta != int(delta):
        raise EvalError(f"bad dwell time for {run.repo} {run.pr}")
    run.seconds[run.open_state] += int(delta)
    run.open_state = None
    run.open_at = None


def _comment(root: Path, run: _Run, event: _Event) -> None:
    path = _safe_path(root, event.fields["path"])
    if not path.is_file():
        raise EvalError(f"comment not found: {event.fields['path']}")
    try:
        handoff = parse_comment(path.read_text(encoding="utf-8"))
    except HandoffError as exc:
        raise EvalError(f"{event.fields['path']}: {exc}") from exc
    fields = handoff.as_dict()
    if fields["kind"] == "verdict":
        run.verdicts.append(fields)
    if fields["kind"] == "stuck":
        run.stuck_comment = True


def _wake(run: _Run, event: _Event) -> None:
    role = event.fields["role"]
    reason = event.fields["reason"]
    if _TOKEN.fullmatch(role) is None or _TOKEN.fullmatch(reason) is None:
        raise EvalError(f"bad wake for {run.repo} {run.pr}")
    handled = event.fields.get("handled")
    if handled is not None and _TOKEN.fullmatch(handled) is None:
        raise EvalError(f"bad handled id for {run.repo} {run.pr}")
    wasted_flag = event.fields.get("wasted")
    if wasted_flag is not None and wasted_flag != "yes":
        raise EvalError(f"bad wasted flag for {run.repo} {run.pr}")
    repeated = handled is not None and handled in run.seen_handled
    if handled is not None:
        run.seen_handled.add(handled)
    if wasted_flag == "yes" or reason == "nobody" or repeated:
        run.wakes.append(
            Wake(at=event.at, repo=run.repo, pr=run.pr, role=role, reason=reason)
        )


def _rounds(run: _Run) -> int:
    numbers: list[int] = []
    for fields in run.verdicts:
        if "round" not in fields:
            continue
        numbers.append(int(fields["round"].split("/", 1)[0]))
    if numbers:
        return max(numbers)
    return len(run.verdicts)


def _is_stuck(run: _Run) -> bool:
    return run.stuck_comment or "stuck" in run.entered


def _mismatches(run: _Run, config: FactoryConfig) -> list[Mismatch]:
    found: list[Mismatch] = []
    for state in run.entered:
        if state not in STATES:
            found.append(Mismatch(run.repo, run.pr, "unknown-state", state))
    for src, dst in zip(run.entered, run.entered[1:]):
        # A merge seen in any state ends the ticket (#108).
        if (src, dst) not in CHART_TRANSITIONS and dst != "merged":
            found.append(Mismatch(run.repo, run.pr, "illegal-transition", f"{src} {dst}"))
    if "post-merge" in run.entered:
        found.append(Mismatch(run.repo, run.pr, "post-merge-in-phase-1", ""))
    if run.stuck_comment and "stuck" not in run.entered:
        found.append(Mismatch(run.repo, run.pr, "stuck-comment-without-state", ""))
    found.extend(_cap_mismatches(run, config))
    return found


def _cap_mismatches(run: _Run, config: FactoryConfig) -> list[Mismatch]:
    found: list[Mismatch] = []
    for fields in run.verdicts:
        if "round" not in fields or "risk" not in fields:
            continue
        risk = _risk(fields["risk"])
        number = int(fields["round"].split("/", 1)[0])
        cap = _round_cap(config, risk)
        if number > cap:
            found.append(
                Mismatch(run.repo, run.pr, "round-over-cap", f"{risk} {number} {cap}")
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
    if _TIME.fullmatch(parts[0]) is None:
        raise EvalError(f"dispatcher.log:{number}: bad timestamp")
    fields: dict[str, str] = {}
    bare: list[str] = []
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            if _KEY.fullmatch(key) is None or _VALUE.fullmatch(value) is None:
                raise EvalError(f"dispatcher.log:{number}: bad attribute {token!r}")
            if key in fields:
                raise EvalError(f"dispatcher.log:{number}: duplicate key {key!r}")
            fields[key] = value
        else:
            bare.append(token)
    if len(bare) != 1:
        raise EvalError(f"dispatcher.log:{number}: expected one event")
    kind = _event_kind(bare[0], number)
    allowed = _EVENT_KEYS[kind]
    for key in fields:
        if key in ("repo", "pr"):
            continue
        if key not in allowed:
            raise EvalError(f"dispatcher.log:{number}: unknown key {key!r}")
    if "repo" not in fields or "pr" not in fields:
        raise EvalError(f"dispatcher.log:{number}: missing repo or pr")
    if _PR.fullmatch(fields["pr"]) is None:
        raise EvalError(f"dispatcher.log:{number}: bad pr")
    missing = _REQUIRED_KEYS[kind] - set(fields)
    if missing:
        name = sorted(missing)[0]
        raise EvalError(f"dispatcher.log:{number}: missing {name}")
    return _Event(
        at=_parse_time(parts[0]),
        repo=fields["repo"],
        pr=int(fields["pr"]),
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


def _load_findings(
    path: Path,
    start: datetime | None,
    end: datetime | None,
) -> list[ModelBreak]:
    if not path.exists():
        return []
    if not path.is_file():
        raise EvalError("model-findings.txt is not a file")
    found: list[ModelBreak] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.strip() == "":
            continue
        match = _FINDING.fullmatch(raw.strip())
        if match is None:
            raise EvalError(f"model-findings.txt:{number}: bad finding")
        at = _parse_time(match.group("ts"))
        if not _in_window(at, start, end):
            continue
        detail = match.group("detail") or ""
        found.append(
            ModelBreak(
                at=at,
                repo=match.group("repo"),
                pr=int(match.group("pr")),
                rule=match.group("rule"),
                detail=detail,
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
    return _parse_time(value)


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_window(at: datetime, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and at < start:
        return False
    if end is not None and at > end:
        return False
    return True
