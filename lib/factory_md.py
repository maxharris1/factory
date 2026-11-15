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

hold is optional (issue #86): a mapping with label (default
factory:hold), approver (default max, a GitHub login) and paths (a list
of repo-relative globs, default none). It only acts on ship: merge; on any
other ship it is accepted with a warning and never applied.

finish_line is optional (issue #130): ready-for-review stops at the
handoff; merge keeps watching the approved PR until it merges or closes.
Omitted, it follows ship: ship: merge gives merge, any other ship gives
ready-for-review. ship: merge with finish_line: ready-for-review is refused.

shared_paths is optional (issue #261): a list of repo-relative paths or
globs, a trailing / naming a directory. A Touches: path that matches never
causes a feeder collision wait, and in a matching file a conflict hunk kept
as both sides still counts as a trivial update. Absent, it is empty.

parse_factory_context keeps the prose after the front block (Review, Ship,
Context) for the waiter's standard brief (#80): the brief appends it as an
additional-context section. Project-specific behavior lives there, not in
shared code. The front matter must still validate, and the prose is capped
at CONTEXT_LIMIT characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
_GLOB = re.compile(r"^[A-Za-z0-9._*?/@+=~-]+$")

DEFAULT_HOLD_LABEL = "factory:hold"
DEFAULT_HOLD_APPROVER = "max"
FINISH_LINES = ("ready-for-review", "merge")

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
}
_HOLD_KEYS = {"label", "approver", "paths"}


@dataclass(frozen=True)
class HoldConfig:
    label: str = DEFAULT_HOLD_LABEL
    approver: str = DEFAULT_HOLD_APPROVER
    paths: tuple[str, ...] = ()


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

    def __post_init__(self) -> None:
        if self.finish_line is None:
            object.__setattr__(self, "finish_line", default_finish_line(self.ship))


def default_finish_line(ship: str) -> str:
    return "merge" if ship == "merge" else "ready-for-review"


@dataclass(frozen=True)
class FactoryContext:
    """A valid factory.md: its config and the prose after the front block."""

    config: FactoryConfig
    body: str
    truncated: bool = False


def parse_factory_context(text: str) -> tuple[FactoryContext | None, list[Finding]]:
    config, findings = parse_factory_md(text)
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


def parse_factory_md(text: str) -> tuple[FactoryConfig | None, list[Finding]]:
    try:
        raw, _body = split_front_matter(text)
        data = load_yaml(raw)
    except (ValueError, yaml.YAMLError) as exc:
        return None, [Finding("error", _yaml_message(exc))]
    if not isinstance(data, dict):
        return None, [Finding("error", "front matter must be a mapping")]
    return _validate(data)


def _validate(data: dict) -> tuple[FactoryConfig | None, list[Finding]]:
    findings: list[Finding] = []
    findings.extend(_unknown(data, _TOP_KEYS, ""))

    base_branch = _branch(data, findings)
    tickets = _choice(data, "tickets", TICKETS, findings)
    builder_harness, builder_model = _actor(data, "builder", findings)
    reviewer_harness, reviewer_model = _actor(data, "reviewer", findings)
    rounds_low, rounds_high, verify = _review(data, findings)
    ship = _ship(data, findings)
    skills = _skills(data, findings)
    ci_runner = _optional_token(data, "ci_runner", findings)
    github_issues = _optional_bool(data, "github_issues", findings)
    github_labels = _optional_bool(data, "github_labels", findings)
    hold = _hold(data, findings)
    finish_line = _finish_line(data, ship, findings)
    shared_paths = _shared_paths(data, findings)

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

    if hold is not None and ship is not None and ship != "merge":
        findings.append(
            Finding("warning", "hold has no effect unless ship is merge")
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
    )
    return config, findings


def _unknown(data: dict, allowed: set[str], prefix: str) -> list[Finding]:
    findings: list[Finding] = []
    for key in sorted(data, key=lambda item: str(item)):
        if key not in allowed:
            dotted = f"{prefix}.{key}" if prefix else str(key)
            findings.append(Finding("error", f"unknown field {dotted!r}"))
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
) -> tuple[str | None, str | None]:
    if key not in data:
        findings.append(Finding("error", f"missing required field '{key}.harness'"))
        return None, None
    value = data[key]
    if not isinstance(value, dict):
        findings.append(Finding("error", f"{key} must be a mapping"))
        return None, None
    findings.extend(_unknown(value, {"harness", "model"}, key))
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
) -> tuple[int | None, int | None, str | None]:
    if "review" not in data:
        findings.append(Finding("error", "missing required field 'review.rounds.low'"))
        findings.append(Finding("error", "missing required field 'review.rounds.high'"))
        findings.append(Finding("error", "missing required field 'review.verify'"))
        return None, None, None
    review = data["review"]
    if not isinstance(review, dict):
        findings.append(Finding("error", "review must be a mapping"))
        return None, None, None
    findings.extend(_unknown(review, {"rounds", "verify"}, "review"))
    low: int | None = None
    high: int | None = None
    if "rounds" not in review:
        findings.append(Finding("error", "missing required field 'review.rounds.low'"))
        findings.append(Finding("error", "missing required field 'review.rounds.high'"))
    elif not isinstance(review["rounds"], dict):
        findings.append(Finding("error", "review.rounds must be a mapping"))
    else:
        rounds = review["rounds"]
        findings.extend(_unknown(rounds, {"low", "high"}, "review.rounds"))
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
    return low, high, verify


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


def _hold(data: dict, findings: list[Finding]) -> HoldConfig | None:
    if "hold" not in data:
        return None
    value = data["hold"]
    if value is None:
        value = {}
    if not isinstance(value, dict):
        findings.append(Finding("error", "hold must be a mapping"))
        return None
    errors = len(findings)
    findings.extend(_unknown(value, _HOLD_KEYS, "hold"))
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
    if len(findings) != errors:
        return None
    return HoldConfig(label=label, approver=approver, paths=tuple(paths))


def _shared_paths(data: dict, findings: list[Finding]) -> tuple[str, ...]:
    value = data.get("shared_paths")
    if value is None:
        return ()
    if not isinstance(value, list):
        findings.append(Finding("error", "shared_paths must be a list"))
        return ()
    paths: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and _is_glob(item.removesuffix("/")):
            paths.append(item)
        else:
            findings.append(
                Finding(
                    "error",
                    f"shared_paths[{index}] must be a repo-relative path or glob",
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
