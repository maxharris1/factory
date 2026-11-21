"""factory.md schema (docs/SPEC.md section 6).

check-config enforces the YAML front matter. Review, Ship and Context
prose is not required to pass. github_issues and github_labels are
optional booleans; either one set true with tickets: linear is refused.
Model hints have no allow-list: an unrecognized name warns. The
family rule, also in the README, is fail closed:

- A claude harness is Anthropic. A codex harness is OpenAI. The
  harness decides the family. A hint that names the other family is
  refused.
- A cursor harness takes its family only from the model hint.
  claude*, sonnet*, opus*, and haiku* are Anthropic. gpt*, codex*,
  and o<digit> (o3, o4-mini) are OpenAI. The family word must end the
  name or be followed by one of -._: or a digit, so haikus and
  magnum-opus resolve to nothing. opus-mt*, gpt-j* and gpt-neo* are
  other model lines and resolve to nothing.
- If either side's family cannot be resolved, the check refuses. A
  cursor side needs one of those hints.
- If both families resolve and they are the same, the check refuses.

review.rounds.low is at most 1 and review.rounds.high is at most 3
(SPEC section 8).

review.followup_from_round is optional (issue #259): an integer of at
least 2. From that review round on, a variant of a rule an earlier round
already raised is a [follow-up] finding, not a blocker, and the poller
files a trusted verdict's [follow-up] findings as one new ticket. Absent,
the policy is off. Refused with tickets: linear, which has no filer.

review.auto_extra_rounds is optional (issue #263): a non-negative integer,
default 0. A trusted REVISE at the cap gets an extra round from the poller
itself while ticket_state.cap_extra is below it; after that the ticket
waits for a person as before.

plan.max_lines and plan.max_criteria are optional (issue #502): positive
integers, default 400 and 5. A plan's Estimate is multiplied by 1.35 and
rounded up before the comparison (issue #1021). A plan
whose padded estimate is over max_lines, or a ticket with more builder
criteria, waits for Max instead of queuing the build.

review.start_before_ci is optional (issue #262): a boolean, default false.
True queues the review as soon as a trusted ready lands while CI is still
running on that head. Ship still needs green CI on the approved head.

review.block_on is optional (issue #490): any-failure (the default) or
user-impact. user-impact gives the review brief pstack's blocking bar from
round 1, files [follow-up] findings from round 1, and limits a later round
to a check of the fix. Refused with tickets: linear, which has no filer.

review.stop_on is optional (issue #1110): rounds (the default) or
convergence. convergence lets a REVISE that cuts the open blocking findings
to a new low, or a first flat round, go on past the cap;
review.auto_extra_rounds is not read under it.

review.verify_unavailable is optional (issue #596): a boolean, default
false. True says the review.verify skill may not be on the reviewed base
yet: the review brief tells the reviewer to list it as not run there
instead of returning BLOCKED for its missing result. The brief reads only
this key, never the Review prose.

hold is optional (issue #86): a mapping with label (default
factory:hold), approver (default max, a GitHub login) and paths (a list
of repo-relative globs, default none). It acts on ship: merge and ship:
handoff (issue #183); on ship: after:<approver> it is accepted with a
warning and never applied.

finish_line is optional (issue #130): ready-for-review stops at the
handoff; merge keeps watching the approved PR until it merges or closes.
Omitted, it follows ship: ship: merge gives merge, any other ship gives
ready-for-review. ship: merge with finish_line: ready-for-review is refused.

ci is optional (issue #615): a mapping with branches (a non-empty list of
branch names CI runs on for a pull request into that base) and
local_checks (a list of short check labels, default none). A head with no
check run and no commit status on a PR whose base is not in branches is
CI not-run: the CI gates do not wait, and the build, fix and review
briefs ask for the local_checks results instead. Absent, every base is
treated as a CI base. required_checks (issue #1278) is a list of job-name
globs, default none. It is parsed and validated only. The PR CI gate that
reads it lands in the second part of #1275, so no project should set it
before then. With required_checks given, branches may be omitted, and every
base is a CI base. It is independent of feeder.required_checks.

shared_paths is optional (issue #261): a list of repo-relative paths or
globs, a trailing / naming a directory. A Touches: path that matches never
causes a feeder collision wait, and in a matching file a conflict hunk kept
as both sides still counts as a trivial update. Absent, it is empty.

feeder is optional (issue #269): a mapping with max_holders_per_path (an
integer of at least 1, default 2) and exclusive_paths (a list like
shared_paths, default none). Up to max_holders_per_path tickets in flight
may hold one Touches: path; a path matching exclusive_paths keeps one
holder. A bad max_holders_per_path is an error under check-config and a
warning elsewhere that falls back to 1. required_checks (a list of job-name
globs such as "test (*)", default none) stops the feeder starting new
tickets while a matching check is red on the base branch (issue #1025).

qa is optional (issue #759): what the project's QA lane runs. The block
needs enabled, a boolean. Enabled, it also needs runners (one or two of
claude-cloud and cursor-cloud, the first primary, each with a claude_cloud
or cursor_cloud block holding an environment and an optional model), a
profile, lane.boot and lane.base_url. The other keys are optional.
enabled: false still checks the values it holds. check-config refuses a bad
block; with strict=False its problems are warnings and qa is off. Off is
always FactoryConfig.qa None.

Unknown fields are errors by default, as check-config reads them. The
poller, feeder and waiter read factory.md with strict=False (issue #288):
an unknown field is a warning there, so a just-merged factory.md naming a
field this code does not know yet still polls.

parse_factory_context keeps the prose after the front block (Review, Ship,
Context) for the waiter's standard brief (#80): the brief appends it as an
additional-context section. Project-specific behavior lives there, not in
shared code. The front matter must still validate, and the prose is capped
at CONTEXT_LIMIT characters.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import yaml

from lib.load import Finding, is_strict_int, load_yaml, split_front_matter
from lib.schema import HARNESSES, TICKETS

EXAMPLE_PATHS = (
    "examples/aut0/factory.md",
    "examples/cleffy/factory.md",
    "examples/tracy/factory.md",
    "docs/factory.example.md",
)

# Prefixes drawn from names the spec uses. This is not an allow-list.
ADVISORY_MODEL_PREFIXES = ("claude", "codex", "cursor", "gpt", "grok")

_ANTHROPIC_WORD = re.compile(r"^(?:claude|sonnet|opus|haiku)(?=$|[-._:0-9])")
_OPENAI_WORD = re.compile(r"^(?:gpt|codex)(?=$|[-._:0-9])")
_OPENAI_O_SERIES = re.compile(r"^o[0-9]+(?=$|[-._:])")
_OTHER_MODEL_LINES = ("opus-mt", "gpt-j", "gpt-neo")
_SKILL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_APPROVER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_LABEL = re.compile(r"^(?=.{1,50}$)[^\s,](?:[^,\x00-\x1f\x7f]*[^\s,])?$")
CHECK_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/+-]{0,39}$")
_GLOB = re.compile(r"^[A-Za-z0-9._*?/@+=~-]+$")

DEFAULT_HOLD_LABEL = "factory:hold"
DEFAULT_HOLD_APPROVER = "max"
FINISH_LINES = ("ready-for-review", "merge")
BLOCK_ON = ("any-failure", "user-impact")
STOP_ON = ("rounds", "convergence")

# The most factory.md prose a brief carries (about four screens).
CONTEXT_LIMIT = 16_000

_TOP_KEYS = {
    "base_branch",
    "tickets",
    "builder",
    "reviewer",
    "review",
    "ship",
    "skills",
    "ci_runner",
    "github_issues",
    "github_labels",
    "hold",
    "finish_line",
    "shared_paths",
    "feeder",
    "plan",
    "ci",
    "qa",
}
_CI_KEYS = {"branches", "local_checks", "required_checks"}
_PLAN_KEYS = {"max_lines", "max_criteria"}
DEFAULT_PLAN_MAX_LINES = 400
PLAN_ESTIMATE_FACTOR = Fraction(27, 20)
DEFAULT_PLAN_MAX_CRITERIA = 5
_HOLD_KEYS = {"label", "approver", "paths"}
TRUST_KEYS = ("hold", "ship", "finish_line", "builder", "reviewer", "ci", "qa")
_FEEDER_KEYS = {"max_holders_per_path", "exclusive_paths", "required_checks"}
DEFAULT_MAX_HOLDERS_PER_PATH = 2
QA_RUNNERS = ("claude-cloud", "cursor-cloud")
QA_GATES = ("report", "block")
QA_PROFILES = ("full-stack", "supabase", "none")
QA_VIDEO = ("multi-step", "always", "never")
QA_TRACE = ("on-failure", "always", "never")
_QA_KEYS = {
    "enabled",
    "gate",
    "runners",
    "shootout",
    "profile",
    "lanes",
    "paths",
    "full_rerun_lines",
    "setup",
    "lane",
    "fixtures",
    "outside",
    "secrets",
    "verify",
    "timeout_minutes",
    "evidence",
    *(name.replace("-", "_") for name in QA_RUNNERS),
}
_URL = re.compile(r"^https?://\S+$")
_NAME_RULE = "a name, not a value or URL"


@dataclass(frozen=True)
class HoldConfig:
    label: str = DEFAULT_HOLD_LABEL
    approver: str = DEFAULT_HOLD_APPROVER
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreReady:
    command: str
    timeout_seconds: int


PRE_READY_MAX_TIMEOUT = 900


@dataclass(frozen=True)
class QaRunner:
    name: str
    environment: str
    model: str | None = None


@dataclass(frozen=True)
class QaLane:
    boot: str
    base_url: str
    doctor: tuple[str, ...] = ()
    recover: str | None = None


@dataclass(frozen=True)
class QaEvidence:
    video: str = "multi-step"
    trace: str = "on-failure"


@dataclass(frozen=True)
class QaConfig:
    runners: tuple[QaRunner, ...]
    lane: QaLane
    profile: str
    gate: str = "report"
    shootout: bool = False
    lanes: int = 1
    paths: tuple[str, ...] = ()
    full_rerun_lines: int = 400
    setup: str | None = None
    fixtures_load: str | None = None
    outside: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    verify: str | None = None
    timeout_minutes: int = 60
    evidence: QaEvidence = QaEvidence()


@dataclass(frozen=True)
class FactoryConfig:
    base_branch: str
    tickets: str
    builder_harness: str
    reviewer_harness: str
    rounds_low: int
    rounds_high: int
    verify: str
    ship: str
    skills: tuple[str, ...]
    builder_model: str | None = None
    reviewer_model: str | None = None
    ci_runner: str | None = None
    github_issues: bool = False
    github_labels: bool = False
    hold: HoldConfig | None = None
    # None resolves from ship (default_finish_line), so a config built in
    # code gets the same default as one parsed from factory.md.
    finish_line: str | None = None
    shared_paths: tuple[str, ...] = ()
    followup_from_round: int | None = None
    auto_extra_rounds: int = 0
    start_before_ci: bool = False
    block_on: str = "any-failure"
    stop_on: str = "rounds"
    verify_unavailable: bool = False
    max_holders_per_path: int = DEFAULT_MAX_HOLDERS_PER_PATH
    exclusive_paths: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    pre_ready: PreReady | None = None
    plan_max_lines: int = DEFAULT_PLAN_MAX_LINES
    plan_max_criteria: int = DEFAULT_PLAN_MAX_CRITERIA
    ci_branches: tuple[str, ...] | None = None
    ci_local_checks: tuple[str, ...] = ()
    ci_required_checks: tuple[str, ...] = ()
    qa: QaConfig | None = None

    def __post_init__(self) -> None:
        if self.finish_line is None:
            object.__setattr__(self, "finish_line", default_finish_line(self.ship))

    @property
    def hold_applies(self) -> bool:
        return self.hold is not None and self.ship in ("merge", "handoff")


def trust_front_matter_changed(base_text: str | None, head_text: str | None) -> bool:
    base = _trust_sections(base_text)
    head = _trust_sections(head_text)
    return base is None or head is None or base != head


def _trust_sections(text: str | None) -> dict[str, object] | None:
    if text is None:
        return None
    try:
        raw, _body = split_front_matter(text)
        data = load_yaml(raw)
    except (ValueError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return {key: _typed(data.get(key)) for key in TRUST_KEYS}


def _typed(value: object) -> object:
    if isinstance(value, dict):
        return {_typed(key): _typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_typed(item) for item in value]
    return (type(value), value)


def padded_estimate(estimate: int) -> int:
    return math.ceil(estimate * PLAN_ESTIMATE_FACTOR)


def default_finish_line(ship: str) -> str:
    return "merge" if ship == "merge" else "ready-for-review"


@dataclass(frozen=True)
class FactoryContext:
    """A valid factory.md: its config and the prose after the front block."""

    config: FactoryConfig
    body: str
    truncated: bool = False


def parse_factory_context(
    text: str, *, strict: bool = True
) -> tuple[FactoryContext | None, list[Finding]]:
    config, findings = parse_factory_md(text, strict=strict)
    if config is None:
        return None, findings
    _raw, body = split_front_matter(text)
    body = body.strip()
    truncated = len(body) > CONTEXT_LIMIT
    if truncated:
        body = body[:CONTEXT_LIMIT].rstrip() + (
            f"\n\n[factory.md prose cut to its first {CONTEXT_LIMIT} characters]"
        )
    return FactoryContext(config=config, body=body, truncated=truncated), findings


def load_factory_md(path: Path) -> tuple[FactoryConfig | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("error", "file not found")]
    return parse_factory_md(path.read_text(encoding="utf-8"))


def parse_factory_md(
    text: str, *, strict: bool = True
) -> tuple[FactoryConfig | None, list[Finding]]:
    try:
        raw, _body = split_front_matter(text)
        data = load_yaml(raw)
    except (ValueError, yaml.YAMLError) as exc:
        return None, [Finding("error", _yaml_message(exc))]
    if not isinstance(data, dict):
        return None, [Finding("error", "front matter must be a mapping")]
    return _validate(data, strict)


def _validate(data: dict, strict: bool) -> tuple[FactoryConfig | None, list[Finding]]:
    findings: list[Finding] = []
    unknown = "error" if strict else "warning"
    findings.extend(_unknown(data, _TOP_KEYS, "", unknown))

    base_branch = _branch(data, findings)
    tickets = _choice(data, "tickets", TICKETS, findings)
    builder_harness, builder_model = _actor(data, "builder", findings, unknown)
    reviewer_harness, reviewer_model = _actor(data, "reviewer", findings, unknown)
    (
        rounds_low,
        rounds_high,
        verify,
        followup_from_round,
        auto_extra_rounds,
        start_before_ci,
        pre_ready,
        block_on,
        stop_on,
        verify_unavailable,
    ) = _review(
        data, findings, unknown
    )
    ship = _ship(data, findings)
    skills = _skills(data, findings)
    ci_runner = _optional_token(data, "ci_runner", findings)
    github_issues = _optional_bool(data, "github_issues", findings)
    github_labels = _optional_bool(data, "github_labels", findings)
    hold = _hold(data, findings, unknown)
    finish_line = _finish_line(data, ship, findings)
    shared_paths = _path_list(data.get("shared_paths"), "shared_paths", findings)
    max_holders_per_path, exclusive_paths, required_checks = _feeder(data, findings, unknown)
    plan_max_lines, plan_max_criteria = _plan(data, findings, unknown)
    ci_branches, ci_local_checks, ci_required_checks = _ci(data, findings, unknown)
    qa, qa_findings = _qa(data, strict)
    findings.extend(qa_findings)

    if builder_harness is not None and reviewer_harness is not None:
        findings.extend(
            family_findings(
                builder_harness,
                builder_model,
                reviewer_harness,
                reviewer_model,
            )
        )
    if tickets == "linear" and github_issues is True:
        findings.append(
            Finding("error", "tickets is linear and github_issues is true")
        )
    if tickets == "linear" and github_labels is True:
        findings.append(
            Finding("error", "tickets is linear and github_labels is true")
        )
    if tickets == "linear" and followup_from_round is not None:
        findings.append(
            Finding(
                "error", "tickets is linear and review.followup_from_round is set"
            )
        )
    if tickets == "linear" and block_on == "user-impact":
        findings.append(
            Finding("error", "tickets is linear and review.block_on is user-impact")
        )

    if hold is not None and ship is not None and ship.startswith("after:"):
        findings.append(
            Finding("warning", "hold has no effect on ship: after:<approver>")
        )

    if any(item.severity == "error" for item in findings):
        return None, findings
    assert base_branch is not None
    assert tickets is not None
    assert builder_harness is not None
    assert reviewer_harness is not None
    assert rounds_low is not None
    assert rounds_high is not None
    assert verify is not None
    assert ship is not None
    assert skills is not None
    assert github_issues is not None
    assert github_labels is not None
    config = FactoryConfig(
        base_branch=base_branch,
        tickets=tickets,
        builder_harness=builder_harness,
        reviewer_harness=reviewer_harness,
        rounds_low=rounds_low,
        rounds_high=rounds_high,
        verify=verify,
        ship=ship,
        skills=skills,
        builder_model=builder_model,
        reviewer_model=reviewer_model,
        ci_runner=ci_runner,
        github_issues=github_issues,
        github_labels=github_labels,
        hold=hold,
        finish_line=finish_line,
        shared_paths=shared_paths,
        followup_from_round=followup_from_round,
        auto_extra_rounds=auto_extra_rounds,
        start_before_ci=start_before_ci,
        pre_ready=pre_ready,
        block_on=block_on,
        stop_on=stop_on,
        verify_unavailable=verify_unavailable,
        max_holders_per_path=max_holders_per_path,
        exclusive_paths=exclusive_paths,
        required_checks=required_checks,
        plan_max_lines=plan_max_lines,
        plan_max_criteria=plan_max_criteria,
        ci_branches=ci_branches,
        ci_local_checks=ci_local_checks,
        ci_required_checks=ci_required_checks,
        qa=qa,
    )
    return config, findings


def _unknown(data: dict, allowed: set[str], prefix: str, severity: str) -> list[Finding]:
    findings: list[Finding] = []
    for key in sorted(data, key=lambda item: str(item)):
        if key not in allowed:
            dotted = f"{prefix}.{key}" if prefix else str(key)
            findings.append(Finding(severity, f"unknown field {dotted!r}"))
    return findings


def _branch(data: dict, findings: list[Finding]) -> str | None:
    if "base_branch" not in data:
        findings.append(Finding("error", "missing required field 'base_branch'"))
        return None
    value = data["base_branch"]
    if not isinstance(value, str) or _BRANCH.fullmatch(value) is None:
        findings.append(Finding("error", "base_branch must be a branch name"))
        return None
    return value


def _choice(
    data: dict,
    key: str,
    allowed: tuple[str, ...],
    findings: list[Finding],
) -> str | None:
    if key not in data:
        findings.append(Finding("error", f"missing required field '{key}'"))
        return None
    value = data[key]
    if value not in allowed:
        joined = " or ".join(allowed)
        findings.append(Finding("error", f"{key} must be {joined}"))
        return None
    return str(value)


def _actor(
    data: dict,
    key: str,
    findings: list[Finding],
    unknown: str,
) -> tuple[str | None, str | None]:
    if key not in data:
        findings.append(Finding("error", f"missing required field '{key}.harness'"))
        return None, None
    value = data[key]
    if not isinstance(value, dict):
        findings.append(Finding("error", f"{key} must be a mapping"))
        return None, None
    findings.extend(_unknown(value, {"harness", "model"}, key, unknown))
    harness: str | None = None
    if "harness" not in value:
        findings.append(Finding("error", f"missing required field '{key}.harness'"))
    elif value["harness"] not in HARNESSES:
        findings.append(
            Finding("error", f"{key}.harness must be claude, codex or cursor")
        )
    else:
        harness = str(value["harness"])
    model: str | None = None
    if "model" in value:
        model = _model(value["model"], f"{key}.model", findings)
    return harness, model


def _review(
    data: dict,
    findings: list[Finding],
    unknown: str,
) -> tuple[int | None, int | None, str | None, int | None, int, bool, PreReady | None, str, str, bool]:
    if "review" not in data:
        findings.append(Finding("error", "missing required field 'review.rounds.low'"))
        findings.append(Finding("error", "missing required field 'review.rounds.high'"))
        findings.append(Finding("error", "missing required field 'review.verify'"))
        return None, None, None, None, 0, False, None, "any-failure", "rounds", False
    review = data["review"]
    if not isinstance(review, dict):
        findings.append(Finding("error", "review must be a mapping"))
        return None, None, None, None, 0, False, None, "any-failure", "rounds", False
    findings.extend(
        _unknown(
            review,
            {
                "rounds",
                "verify",
                "followup_from_round",
                "auto_extra_rounds",
                "start_before_ci",
                "pre_ready",
                "block_on",
                "stop_on",
                "verify_unavailable",
            },
            "review",
            unknown,
        )
    )
    low: int | None = None
    high: int | None = None
    if "rounds" not in review:
        findings.append(Finding("error", "missing required field 'review.rounds.low'"))
        findings.append(Finding("error", "missing required field 'review.rounds.high'"))
    elif not isinstance(review["rounds"], dict):
        findings.append(Finding("error", "review.rounds must be a mapping"))
    else:
        rounds = review["rounds"]
        findings.extend(_unknown(rounds, {"low", "high"}, "review.rounds", unknown))
        low = _round(rounds, "low", findings)
        high = _round(rounds, "high", findings)
    verify: str | None = None
    if "verify" not in review:
        findings.append(Finding("error", "missing required field 'review.verify'"))
    else:
        raw = review["verify"]
        if raw == "none" or (isinstance(raw, str) and _SKILL.fullmatch(raw)):
            verify = str(raw)
        else:
            findings.append(
                Finding("error", "review.verify must be a skill name or none")
            )
    return (
        low,
        high,
        verify,
        _followup_from_round(review, findings, unknown),
        _auto_extra_rounds(review, findings, unknown),
        _start_before_ci(review, findings, unknown),
        _pre_ready(review, findings, unknown),
        _block_on(review, findings, unknown),
        _stop_on(review, findings, unknown),
        _verify_unavailable(review, findings, unknown),
    )


def _pre_ready(review: dict, findings: list[Finding], unknown: str) -> PreReady | None:
    if "pre_ready" not in review:
        return None
    value = review["pre_ready"]
    if not isinstance(value, dict):
        findings.append(Finding("error", "review.pre_ready must be a mapping"))
        return None
    findings.extend(_unknown(value, {"command", "timeout_seconds"}, "review.pre_ready", unknown))
    command = value.get("command")
    timeout = value.get("timeout_seconds")
    ok = True
    if not isinstance(command, str) or not command.strip():
        findings.append(Finding("error", "review.pre_ready.command must be a non-empty string"))
        ok = False
    if not is_strict_int(timeout) or not 1 <= timeout <= PRE_READY_MAX_TIMEOUT:
        findings.append(
            Finding(
                "error",
                "review.pre_ready.timeout_seconds must be an integer from 1 to "
                f"{PRE_READY_MAX_TIMEOUT}",
            )
        )
        ok = False
    return PreReady(command, int(timeout)) if ok else None


def _block_on(review: dict, findings: list[Finding], unknown: str) -> str:
    value = review.get("block_on", "any-failure")
    if not isinstance(value, str) or value not in BLOCK_ON:
        findings.append(Finding(unknown, "review.block_on must be any-failure or user-impact"))
        return "any-failure"
    return value


def _stop_on(review: dict, findings: list[Finding], unknown: str) -> str:
    value = review.get("stop_on", "rounds")
    if not isinstance(value, str) or value not in STOP_ON:
        findings.append(Finding(unknown, "review.stop_on must be rounds or convergence"))
        return "rounds"
    return value


def _verify_unavailable(review: dict, findings: list[Finding], unknown: str) -> bool:
    value = review.get("verify_unavailable", False)
    if not isinstance(value, bool):
        findings.append(Finding(unknown, "review.verify_unavailable must be true or false"))
        return False
    return value


def _start_before_ci(review: dict, findings: list[Finding], unknown: str) -> bool:
    value = review.get("start_before_ci", False)
    if not isinstance(value, bool):
        findings.append(Finding(unknown, "review.start_before_ci must be true or false"))
        return False
    return value


def _followup_from_round(
    review: dict, findings: list[Finding], unknown: str
) -> int | None:
    if "followup_from_round" not in review:
        return None
    value = review["followup_from_round"]
    if not is_strict_int(value) or value < 2:
        findings.append(
            Finding(unknown, "review.followup_from_round must be an integer of at least 2")
        )
        return None
    return int(value)


def _auto_extra_rounds(review: dict, findings: list[Finding], unknown: str) -> int:
    value = review.get("auto_extra_rounds", 0)
    if not is_strict_int(value) or value < 0:
        findings.append(
            Finding(unknown, "review.auto_extra_rounds must be a non-negative integer")
        )
        return 0
    return int(value)


def _round(rounds: dict, name: str, findings: list[Finding]) -> int | None:
    field = f"review.rounds.{name}"
    if name not in rounds:
        findings.append(Finding("error", f"missing required field '{field}'"))
        return None
    value = rounds[name]
    if not is_strict_int(value) or value < 1:
        findings.append(Finding("error", f"{field} must be a positive integer"))
        return None
    ceiling = 1 if name == "low" else 3
    if value > ceiling:
        findings.append(Finding("error", f"{field} must be at most {ceiling}"))
        return None
    return int(value)


def family_findings(
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
) -> list[Finding]:
    findings: list[Finding] = []
    builder_family, builder_hint_family = resolved_family(builder_harness, builder_model)
    reviewer_family, reviewer_hint_family = resolved_family(
        reviewer_harness, reviewer_model
    )
    findings.extend(
        _contradiction("builder", builder_harness, builder_model, builder_hint_family, builder_family)
    )
    findings.extend(
        _contradiction(
            "reviewer",
            reviewer_harness,
            reviewer_model,
            reviewer_hint_family,
            reviewer_family,
        )
    )
    if builder_family is None or reviewer_family is None:
        if builder_family is None:
            findings.append(Finding("error", _unresolved("builder")))
        if reviewer_family is None:
            findings.append(Finding("error", _unresolved("reviewer")))
        return findings
    if builder_family == reviewer_family:
        findings.append(
            Finding(
                "error",
                "same-family reviewer: "
                f"builder family {builder_family!r} and "
                f"reviewer family {reviewer_family!r}",
            )
        )
    return findings


def _unresolved(side: str) -> str:
    return (
        f"same-family reviewer: {side} family not established: "
        "give a claude*/sonnet*/opus*/haiku*/gpt*/codex*/o<n> model hint"
    )


def _contradiction(
    side: str,
    harness: str,
    model: str | None,
    hint_family: str | None,
    harness_family: str | None,
) -> list[Finding]:
    if (
        harness_family is None
        or hint_family is None
        or model is None
        or hint_family == harness_family
    ):
        return []
    return [
        Finding(
            "error",
            f"{side} model hint {model!r} contradicts {harness} harness",
        )
    ]


def resolved_family(harness: str, model: str | None) -> tuple[str | None, str | None]:
    """Return (family, hint family).

    For claude and codex the harness family is authoritative. The hint
    family is returned so a contradicting hint can be refused. Cursor
    has no harness family; its family is the hint family.
    """
    hint_family = _family_name(model) if model is not None else None
    if harness == "claude":
        return "anthropic", hint_family
    if harness == "codex":
        return "openai", hint_family
    if harness == "cursor":
        return hint_family, hint_family
    return None, hint_family


def _family_name(name: str) -> str | None:
    lowered = name.lower()
    if lowered.startswith(_OTHER_MODEL_LINES):
        return None
    if _ANTHROPIC_WORD.match(lowered) is not None:
        return "anthropic"
    if (
        _OPENAI_WORD.match(lowered) is not None
        or _OPENAI_O_SERIES.match(lowered) is not None
    ):
        return "openai"
    return None


def _ship(data: dict, findings: list[Finding]) -> str | None:
    if "ship" not in data:
        findings.append(Finding("error", "missing required field 'ship'"))
        return None
    value = data["ship"]
    if value in ("merge", "handoff"):
        return str(value)
    if isinstance(value, str) and value.startswith("after:"):
        approver = value[len("after:") :]
        if _APPROVER.fullmatch(approver):
            return value
    findings.append(
        Finding("error", "ship must be merge, handoff, or after:<approver>")
    )
    return None


def _finish_line(
    data: dict, ship: str | None, findings: list[Finding]
) -> str | None:
    if "finish_line" not in data:
        return None if ship is None else default_finish_line(ship)
    value = data["finish_line"]
    if value not in FINISH_LINES:
        findings.append(
            Finding("error", "finish_line must be ready-for-review or merge")
        )
        return None
    if ship == "merge" and value == "ready-for-review":
        findings.append(
            Finding(
                "error",
                "ship: merge merges on APPROVE; finish_line: ready-for-review "
                "contradicts it",
            )
        )
        return None
    return str(value)


def _hold(data: dict, findings: list[Finding], unknown: str) -> HoldConfig | None:
    if "hold" not in data:
        return None
    value = data["hold"]
    if value is None:
        value = {}
    if not isinstance(value, dict):
        findings.append(Finding("error", "hold must be a mapping"))
        return None
    start = len(findings)
    findings.extend(_unknown(value, _HOLD_KEYS, "hold", unknown))
    label = value.get("label", DEFAULT_HOLD_LABEL)
    if not isinstance(label, str) or _LABEL.fullmatch(label) is None:
        findings.append(Finding("error", "hold.label must be a GitHub label name"))
    approver = value.get("approver", DEFAULT_HOLD_APPROVER)
    if not isinstance(approver, str) or _LOGIN.fullmatch(approver) is None:
        findings.append(Finding("error", "hold.approver must be a GitHub login"))
    raw_paths = value.get("paths", [])
    paths: list[str] = []
    if not isinstance(raw_paths, list):
        findings.append(Finding("error", "hold.paths must be a list"))
    else:
        for index, item in enumerate(raw_paths):
            if not _is_glob(item):
                findings.append(
                    Finding(
                        "error",
                        f"hold.paths[{index}] must be a repo-relative glob",
                    )
                )
            else:
                paths.append(item)
    if any(item.severity == "error" for item in findings[start:]):
        return None
    return HoldConfig(label=label, approver=approver, paths=tuple(paths))


def _feeder(
    data: dict, findings: list[Finding], unknown: str
) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    value = data.get("feeder")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        findings.append(Finding("error", "feeder must be a mapping"))
        return 1, (), ()
    findings.extend(_unknown(value, _FEEDER_KEYS, "feeder", unknown))
    limit = value.get("max_holders_per_path", DEFAULT_MAX_HOLDERS_PER_PATH)
    if not is_strict_int(limit) or limit < 1:
        findings.append(
            Finding(unknown, "feeder.max_holders_per_path must be an integer of at least 1")
        )
        limit = 1
    exclusive = _path_list(value.get("exclusive_paths"), "feeder.exclusive_paths", findings)
    required = _glob_names(value.get("required_checks", []), "feeder.required_checks", findings)
    return int(limit), exclusive, required


def _glob_names(value: object, field: str, findings: list[Finding]) -> tuple[str, ...]:
    if not isinstance(value, list):
        findings.append(Finding("error", f"{field} must be a list"))
        return ()
    names: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            names.append(item)
        else:
            findings.append(Finding("error", f"{field}[{index}] must be a non-empty job name glob"))
    return tuple(names)


def _plan(data: dict, findings: list[Finding], unknown: str) -> tuple[int, int]:
    value = data.get("plan", {})
    if not isinstance(value, dict):
        findings.append(Finding("error", "plan must be a mapping"))
        return DEFAULT_PLAN_MAX_LINES, DEFAULT_PLAN_MAX_CRITERIA
    findings.extend(_unknown(value, _PLAN_KEYS, "plan", unknown))
    return (
        _positive(value, "max_lines", DEFAULT_PLAN_MAX_LINES, findings, unknown),
        _positive(value, "max_criteria", DEFAULT_PLAN_MAX_CRITERIA, findings, unknown),
    )


def _ci(
    data: dict, findings: list[Finding], unknown: str
) -> tuple[tuple[str, ...] | None, tuple[str, ...], tuple[str, ...]]:
    if "ci" not in data:
        return None, (), ()
    value = data["ci"]
    if not isinstance(value, dict):
        findings.append(Finding("error", "ci must be a mapping"))
        return None, (), ()
    findings.extend(_unknown(value, _CI_KEYS, "ci", "error"))
    branches = _ci_branches(value, findings)
    checks = value.get("local_checks", [])
    if not isinstance(checks, list):
        findings.append(Finding("error", "ci.local_checks must be a list"))
        checks = []
    for index, item in enumerate(checks):
        if not isinstance(item, str) or CHECK_LABEL.fullmatch(item) is None:
            findings.append(Finding("error", f"ci.local_checks[{index}] must be a short label"))
    required = _glob_names(value.get("required_checks", []), "ci.required_checks", findings)
    return branches, tuple(str(item) for item in checks), required


def _ci_branches(value: dict, findings: list[Finding]) -> tuple[str, ...] | None:
    if "branches" not in value and "required_checks" in value:
        return None
    branches = value.get("branches")
    if not isinstance(branches, list) or not branches:
        findings.append(Finding("error", "ci.branches must be a non-empty list"))
        return ()
    for index, item in enumerate(branches):
        if not isinstance(item, str) or _BRANCH.fullmatch(item) is None:
            findings.append(Finding("error", f"ci.branches[{index}] must be a branch name"))
    return tuple(str(item) for item in branches)


def _qa(data: dict, strict: bool) -> tuple[QaConfig | None, list[Finding]]:
    if "qa" not in data:
        return None, []
    problems: list[Finding] = []
    qa = _qa_config(data["qa"], problems)
    if strict or not any(item.severity == "error" for item in problems):
        return qa, problems
    warnings = [Finding("warning", item.message) for item in problems]
    return None, [*warnings, Finding("warning", "qa is off: its block is invalid")]


def _qa_config(value: object, problems: list[Finding]) -> QaConfig | None:
    if not isinstance(value, dict):
        problems.append(Finding("error", "qa must be a mapping"))
        return None
    problems.extend(_unknown(value, _QA_KEYS, "qa", "error"))
    enabled = value.get("enabled")
    if "enabled" not in value:
        problems.append(Finding("error", "missing required field 'qa.enabled'"))
    elif not isinstance(enabled, bool):
        problems.append(Finding("error", "qa.enabled must be true or false"))
    required = enabled is True
    gate = _qa_choice(value.get("gate"), "qa.gate", QA_GATES, problems) or "report"
    runners = _qa_runners(value, required, problems)
    shootout = value.get("shootout", False)
    if not isinstance(shootout, bool):
        problems.append(Finding("error", "qa.shootout must be true or false"))
    elif shootout and len(runners) != 2:
        problems.append(Finding("error", "qa.shootout needs two runners"))
    elif shootout and gate != "report":
        problems.append(Finding("error", "qa.shootout needs gate: report"))
    if required and value.get("profile") is None:
        problems.append(Finding("error", "missing required field 'qa.profile'"))
    profile = _qa_choice(value.get("profile"), "qa.profile", QA_PROFILES, problems)
    lane = _qa_lane(value.get("lane"), required, problems)
    fixtures = _qa_mapping(value.get("fixtures"), "qa.fixtures", {"load"}, problems)
    evidence = _qa_mapping(value.get("evidence"), "qa.evidence", {"video", "trace"}, problems)
    verify = value.get("verify")
    if verify is not None and (not isinstance(verify, str) or _SKILL.fullmatch(verify) is None):
        problems.append(Finding("error", "qa.verify must be a skill name"))
    lanes = _qa_int(value.get("lanes"), "qa.lanes", 1, 3, 1, problems)
    paths = _path_list(value.get("paths"), "qa.paths", problems)
    full_rerun_lines = _qa_int(
        value.get("full_rerun_lines"), "qa.full_rerun_lines", 1, None, 400, problems
    )
    timeout_minutes = _qa_int(
        value.get("timeout_minutes"), "qa.timeout_minutes", 5, 120, 60, problems
    )
    setup = _qa_text(value.get("setup"), "qa.setup", problems)
    fixtures_load = _qa_text(fixtures.get("load"), "qa.fixtures.load", problems)
    outside = _qa_list(value.get("outside"), "qa.outside", _is_env_name, _NAME_RULE, problems)
    secrets = _qa_list(value.get("secrets"), "qa.secrets", _is_env_name, _NAME_RULE, problems)
    video = _qa_choice(evidence.get("video"), "qa.evidence.video", QA_VIDEO, problems)
    trace = _qa_choice(evidence.get("trace"), "qa.evidence.trace", QA_TRACE, problems)
    if not required or any(item.severity == "error" for item in problems):
        return None
    assert lane is not None
    assert profile is not None
    return QaConfig(
        runners=runners,
        lane=lane,
        profile=profile,
        gate=gate,
        shootout=shootout,
        lanes=lanes,
        paths=paths,
        full_rerun_lines=full_rerun_lines,
        setup=setup,
        fixtures_load=fixtures_load,
        outside=outside,
        secrets=secrets,
        verify=verify,
        timeout_minutes=timeout_minutes,
        evidence=QaEvidence(video=video or "multi-step", trace=trace or "on-failure"),
    )


def _qa_runners(qa: dict, required: bool, problems: list[Finding]) -> tuple[QaRunner, ...]:
    raw = qa.get("runners")
    names: list[str] = []
    if raw is None:
        if required:
            problems.append(Finding("error", "missing required field 'qa.runners'"))
    elif not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        problems.append(Finding("error", "qa.runners must be a list of one or two runners"))
    else:
        for index, name in enumerate(raw):
            if name not in QA_RUNNERS:
                problems.append(
                    Finding("error", f"qa.runners[{index}] must be {_either(QA_RUNNERS)}")
                )
            elif name in names:
                problems.append(Finding("error", f"qa.runners names {name} twice"))
            else:
                names.append(name)
    runners: dict[str, QaRunner] = {}
    for name in QA_RUNNERS:
        key = name.replace("-", "_")
        field = f"qa.{key}"
        if key in qa and name not in names:
            problems.append(Finding("error", f"{field} is set but qa.runners does not name {name}"))
            continue
        if name not in names:
            continue
        block = _qa_mapping(qa.get(key), field, {"environment", "model"}, problems)
        environment = block.get("environment")
        model = _model(block["model"], f"{field}.model", problems) if "model" in block else None
        if environment is None:
            problems.append(Finding("error", f"missing required field '{field}.environment'"))
        elif not isinstance(environment, str) or _MODEL.fullmatch(environment) is None:
            problems.append(Finding("error", f"{field}.environment must be a token"))
        else:
            runners[name] = QaRunner(name, environment, model)
    return tuple(runners[name] for name in names if name in runners)


def _qa_lane(value: object, required: bool, problems: list[Finding]) -> QaLane | None:
    lane = _qa_mapping(value, "qa.lane", {"boot", "doctor", "recover", "base_url"}, problems)
    boot = _qa_text(lane.get("boot"), "qa.lane.boot", problems)
    recover = _qa_text(lane.get("recover"), "qa.lane.recover", problems)
    doctor = _qa_list(lane.get("doctor"), "qa.lane.doctor", _is_filled, "a non-empty string", problems)
    base_url = lane.get("base_url")
    if base_url is not None and (not isinstance(base_url, str) or _URL.fullmatch(base_url) is None):
        problems.append(Finding("error", "qa.lane.base_url must be an http:// or https:// URL"))
    for key in ("boot", "base_url"):
        if required and lane.get(key) is None:
            problems.append(Finding("error", f"missing required field 'qa.lane.{key}'"))
    if boot is None or not isinstance(base_url, str):
        return None
    return QaLane(boot=boot, base_url=base_url, doctor=doctor, recover=recover)


def _qa_mapping(value: object, field: str, keys: set[str], problems: list[Finding]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(Finding("error", f"{field} must be a mapping"))
        return {}
    problems.extend(_unknown(value, keys, field, "error"))
    return value


def _qa_choice(
    value: object, field: str, allowed: tuple[str, ...], problems: list[Finding]
) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        problems.append(Finding("error", f"{field} must be {_either(allowed)}"))
        return None
    return str(value)


def _either(allowed: tuple[str, ...]) -> str:
    return ", ".join(allowed[:-1]) + " or " + allowed[-1]


def _qa_int(
    value: object,
    field: str,
    low: int,
    high: int | None,
    default: int,
    problems: list[Finding],
) -> int:
    if value is None:
        return default
    if not is_strict_int(value) or value < low or (high is not None and value > high):
        rule = "a positive integer" if high is None else f"an integer from {low} to {high}"
        problems.append(Finding("error", f"{field} must be {rule}"))
        return default
    return int(value)


def _qa_text(value: object, field: str, problems: list[Finding]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        problems.append(Finding("error", f"{field} must be a non-empty string"))
        return None
    return value


def _qa_list(
    value: object, field: str, valid: Callable[[object], bool], rule: str, problems: list[Finding]
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append(Finding("error", f"{field} must be a list"))
        return ()
    for index, item in enumerate(value):
        if not valid(item):
            problems.append(Finding("error", f"{field}[{index}] must be {rule}"))
    return tuple(value)


def _is_filled(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_env_name(value: object) -> bool:
    return (
        _is_filled(value)
        and "=" not in value
        and "://" not in value
        and not any(char.isspace() for char in value)
    )


def _positive(
    plan: dict, key: str, default: int, findings: list[Finding], severity: str
) -> int:
    value = plan.get(key, default)
    if not is_strict_int(value) or value < 1:
        findings.append(Finding(severity, f"plan.{key} must be a positive integer"))
        return default
    return int(value)


def _path_list(value: object, name: str, findings: list[Finding]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        findings.append(Finding("error", f"{name} must be a list"))
        return ()
    paths: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and _is_glob(item.removesuffix("/")):
            paths.append(item)
        else:
            findings.append(
                Finding(
                    "error",
                    f"{name}[{index}] must be a repo-relative path or glob",
                )
            )
    return tuple(paths)


def _is_glob(value: object) -> bool:
    return (
        isinstance(value, str)
        and _GLOB.fullmatch(value) is not None
        and not value.startswith("/")
        and ".." not in value.split("/")
        and "" not in value.split("/")
    )


def _skills(data: dict, findings: list[Finding]) -> tuple[str, ...] | None:
    if "skills" not in data:
        findings.append(Finding("error", "missing required field 'skills'"))
        return None
    value = data["skills"]
    if not isinstance(value, list):
        findings.append(Finding("error", "skills must be a list"))
        return None
    names: list[str] = []
    ok = True
    for index, item in enumerate(value):
        if not isinstance(item, str) or _SKILL.fullmatch(item) is None:
            findings.append(Finding("error", f"skills[{index}] must be a skill name"))
            ok = False
        else:
            names.append(item)
    if not ok:
        return None
    return tuple(names)


def _optional_token(data: dict, key: str, findings: list[Finding]) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str) or _MODEL.fullmatch(value) is None:
        findings.append(Finding("error", f"{key} must be a token"))
        return None
    return value


def _optional_bool(data: dict, key: str, findings: list[Finding]) -> bool | None:
    if key not in data:
        return False
    value = data[key]
    if not isinstance(value, bool):
        findings.append(Finding("error", f"{key} must be a boolean"))
        return None
    return value


def _model(value: object, field: str, findings: list[Finding]) -> str | None:
    if not isinstance(value, str) or _MODEL.fullmatch(value) is None:
        findings.append(Finding("error", f"{field} must be a model name"))
        return None
    known_family = _family_name(value) is not None
    known_prefix = any(value.lower().startswith(prefix) for prefix in ADVISORY_MODEL_PREFIXES)
    if not known_family and not known_prefix:
        findings.append(Finding("warning", f"unknown model name {value!r} ({field})"))
    return value


def _yaml_message(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    text = " ".join(line.strip() for line in str(exc).splitlines() if line.strip())
    return f"invalid YAML: {text or exc.__class__.__name__}"
