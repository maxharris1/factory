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
  and o<digit> (o3, o4-mini) are OpenAI.
- If either side's family cannot be resolved, the check refuses. A
  cursor side needs one of those hints.
- If both families resolve and they are the same, the check refuses.

review.rounds.low is at most 1 and review.rounds.high is at most 3
(SPEC section 8).
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

_OPENAI_O_SERIES = re.compile(r"^o[0-9]")
_SKILL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_APPROVER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")

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
}


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


def load_factory_md(path: Path) -> tuple[FactoryConfig | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("error", "file not found")]
    text = path.read_text(encoding="utf-8")
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

    if builder_harness is not None and reviewer_harness is not None:
        findings.extend(
            _family_findings(
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


def _family_findings(
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
) -> list[Finding]:
    findings: list[Finding] = []
    builder_family, builder_hint_family = _resolved_family(builder_harness, builder_model)
    reviewer_family, reviewer_hint_family = _resolved_family(
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


def _resolved_family(harness: str, model: str | None) -> tuple[str | None, str | None]:
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
    if (
        lowered.startswith("claude")
        or lowered.startswith("sonnet")
        or lowered.startswith("opus")
        or lowered.startswith("haiku")
    ):
        return "anthropic"
    if (
        lowered.startswith("gpt")
        or lowered.startswith("codex")
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
