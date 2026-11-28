"""Central pstack benchmark-driven role router (E2-4).

Loads `config/routing.yml` and resolves the highest-ranked eligible
`(harness, model, effort)` for every factory and pstack role across Claude,
Codex, Gemini, and Cursor, with:

- budget ladder (`unlimited`, `large`, `medium`, `small`),
- mechanical cross-family review enforcement (filtering out any candidate
  whose resolved model family matches the builder's family, failing closed),
- automatic filtering of refused or unavailable harnesses,
- per-repo `factory.md` override precedence and legacy `builder:`/`reviewer:`
  fallback when `config/routing.yml` is absent or specifies `inherit`, and
- strict token validation before adding model/effort flags to harness argv.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal, get_args

import yaml

from lib import harnesses
from lib.factory_md import FactoryConfig, resolved_family
from lib.load import Finding, load_yaml
from lib.pstack_pin import ROOT

ROUTING_PATH = Path("config") / "routing.yml"

BUDGETS: tuple[str, ...] = ("unlimited", "large", "medium", "small")
EFFORTS: tuple[str, ...] = ("max", "xhigh", "high", "medium", "low")
_EFFORT_RANK: dict[str, int] = {name: idx for idx, name in enumerate(EFFORTS)}
EFFORT_CEILING_BY_BUDGET: dict[str, str] = {
    "unlimited": "max",
    "large": "xhigh",
    "medium": "high",
    "small": "medium",
}

KNOWN_HARNESSES: tuple[str, ...] = ("claude", "codex", "cursor", "gemini")

ROLE_SLOTS: tuple[str, ...] = (
    "orchestrate",
    "plan",
    "build",
    "build:feature",
    "build:bug",
    "build:refactor",
    "build:perf",
    "review",
    "qa",
    "helper:deep",
    "helper:standard",
    "helper:fast",
    "helper:panel",
)
_ROLE_SET = frozenset(ROLE_SLOTS)

_TOP_KEYS = frozenset({"budget", "roles", "budget_overrides", "shares"})
_CANDIDATE_KEYS = frozenset({"harness", "model", "effort"})
_MODEL_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_GOOGLE_WORD = re.compile(r"^gemini(?=$|[-._:0-9])")
_SHARE_HARNESSES = frozenset({"gemini"})
_SHARE_FIELDS: Mapping[str, Literal["fraction", "count"]] = MappingProxyType({
    "build": "fraction",
    "review": "fraction",
    "min_headroom": "fraction",
    "max_builds_in_flight": "count",
})
_SHARE_RULES: Mapping[str, str] = MappingProxyType({
    "fraction": "a number from 0 to 1",
    "count": "a whole number of at least 0",
})


class RoutingError(ValueError):
    """Raised when a role cannot be routed safely (fail closed)."""


@dataclass(frozen=True)
class Candidate:
    harness: str
    model: str | None = None
    effort: str | None = None


SlotEntry = tuple[Candidate, ...] | Literal["inherit"]


@dataclass(frozen=True)
class HarnessShares:
    build: float = 0.0
    review: float = 0.0
    min_headroom: float = 0.15
    max_builds_in_flight: int = 2


@dataclass(frozen=True)
class RoutingConfig:
    budget: str
    roles: Mapping[str, SlotEntry]
    budget_overrides: Mapping[str, Mapping[str, SlotEntry]] = MappingProxyType({})
    shares: Mapping[str, HarnessShares] = MappingProxyType({})

    def share(self, harness: str, role: Literal["build", "review"]) -> float:
        shares = self.shares.get(harness)
        return 0.0 if shares is None else getattr(shares, role)


@dataclass(frozen=True)
class ResolvedSlot:
    role: str
    harness: str
    model: str | None
    effort: str | None
    family: str
    budget: str
    source: str
    draw: Draw | None = None


def family_for(harness: str, model: str | None) -> tuple[str | None, str | None]:
    """Return `(resolved_family, hint_family)` across Claude, Codex, Gemini, and Cursor."""
    hint_family: str | None = None
    if model is not None:
        lowered = model.lower()
        if _GOOGLE_WORD.match(lowered) is not None:
            hint_family = "google"
        else:
            _, hint_family = resolved_family("cursor", model)
    if harness == "gemini":
        return "google", hint_family
    if harness == "cursor":
        return hint_family, hint_family
    base_family, base_hint = resolved_family(harness, model)
    return base_family, hint_family if hint_family is not None else base_hint


def _validate_candidate(
    raw: object,
    where: str,
    findings: list[Finding],
) -> Candidate | None:
    if not isinstance(raw, dict):
        findings.append(Finding("error", f"{where} must be a mapping"))
        return None
    unknown = sorted(str(k) for k in set(raw) - _CANDIDATE_KEYS)
    for key in unknown:
        findings.append(Finding("error", f"unknown field {where}.{key!r}"))
    harness = raw.get("harness")
    ok = True
    if not isinstance(harness, str) or harness not in KNOWN_HARNESSES:
        findings.append(
            Finding(
                "error",
                f"{where}.harness must be one of {', '.join(KNOWN_HARNESSES)}",
            )
        )
        ok = False
    model = raw.get("model")
    if model is not None:
        if not isinstance(model, str) or _MODEL_TOKEN.fullmatch(model) is None:
            findings.append(Finding("error", f"{where}.model must be a valid model name"))
            ok = False
    effort = raw.get("effort")
    if effort is not None:
        if not isinstance(effort, str) or effort not in EFFORTS:
            findings.append(
                Finding("error", f"{where}.effort must be one of {', '.join(EFFORTS)}")
            )
            ok = False
    if not ok or not isinstance(harness, str):
        return None
    fam, hint_fam = family_for(harness, model if isinstance(model, str) else None)
    if fam is not None and hint_fam is not None and fam != hint_fam:
        findings.append(
            Finding(
                "error",
                f"{where}.model {model!r} contradicts harness {harness!r} ({fam} vs {hint_fam})",
            )
        )
        return None
    if harness == "cursor" and fam is None:
        findings.append(
            Finding(
                "error",
                f"{where}: cursor candidate requires a model naming a known family (claude*/gpt*/o<n>/gemini*)",
            )
        )
        return None
    return Candidate(
        harness=harness,
        model=model if isinstance(model, str) else None,
        effort=effort if isinstance(effort, str) else None,
    )


def _validate_slots_map(
    raw: object,
    prefix: str,
    findings: list[Finding],
) -> dict[str, SlotEntry]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        findings.append(Finding("error", f"{prefix} must be a mapping"))
        return {}
    parsed: dict[str, SlotEntry] = {}
    for role, entry in raw.items():
        if not isinstance(role, str) or role not in _ROLE_SET:
            findings.append(Finding("error", f"unknown routing role {prefix}.{role!r}"))
            continue
        if entry == "inherit":
            parsed[role] = "inherit"
            continue
        if not isinstance(entry, list) or not entry:
            findings.append(
                Finding("error", f"{prefix}.{role} must be 'inherit' or a non-empty candidate list")
            )
            continue
        candidates: list[Candidate] = []
        for idx, item in enumerate(entry):
            cand = _validate_candidate(item, f"{prefix}.{role}[{idx}]", findings)
            if cand is not None:
                candidates.append(cand)
        if candidates:
            parsed[role] = tuple(candidates)
    return parsed


def _share_value_ok(kind: Literal["fraction", "count"], value: object) -> bool:
    if isinstance(value, bool):
        return False
    if kind == "count":
        return isinstance(value, int) and value >= 0
    return isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1


def _validate_shares(raw: object, findings: list[Finding]) -> dict[str, HarnessShares]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        findings.append(Finding("error", "shares must be a mapping"))
        return {}
    parsed: dict[str, HarnessShares] = {}
    for harness, block in raw.items():
        if harness not in _SHARE_HARNESSES:
            findings.append(
                Finding("error", f"shares.{harness!r} must be one of {', '.join(sorted(_SHARE_HARNESSES))}")
            )
            continue
        where = f"shares.{harness}"
        if not isinstance(block, dict):
            findings.append(Finding("error", f"{where} must be a mapping"))
            continue
        for key in sorted(str(k) for k in set(block) - set(_SHARE_FIELDS)):
            findings.append(Finding("error", f"unknown field {where}.{key!r}"))
        values = {key: block[key] for key in _SHARE_FIELDS if key in block}
        bad = [key for key, value in values.items() if not _share_value_ok(_SHARE_FIELDS[key], value)]
        for key in bad:
            findings.append(Finding("error", f"{where}.{key} must be {_SHARE_RULES[_SHARE_FIELDS[key]]}"))
        if not bad:
            parsed[harness] = HarnessShares(**values)
    return parsed


def parse_routing_yml(text: str) -> tuple[RoutingConfig | None, list[Finding]]:
    try:
        data = load_yaml(text)
    except (ValueError, yaml.YAMLError) as exc:
        return None, [Finding("error", f"invalid routing YAML: {exc}")]
    if not isinstance(data, dict):
        return None, [Finding("error", "routing.yml must be a mapping")]
    findings: list[Finding] = []
    for key in sorted(str(k) for k in set(data) - _TOP_KEYS):
        findings.append(Finding("error", f"unknown field {key!r}"))
    budget = data.get("budget", "medium")
    if not isinstance(budget, str) or budget not in BUDGETS:
        findings.append(
            Finding("error", f"budget must be one of {', '.join(BUDGETS)}")
        )
        budget = "medium"
    if "roles" not in data:
        findings.append(Finding("error", "missing required field 'roles'"))
        roles: dict[str, SlotEntry] = {}
    else:
        roles = _validate_slots_map(data.get("roles"), "roles", findings)
    overrides: dict[str, Mapping[str, SlotEntry]] = {}
    raw_overrides = data.get("budget_overrides")
    if raw_overrides is not None:
        if not isinstance(raw_overrides, dict):
            findings.append(Finding("error", "budget_overrides must be a mapping"))
        else:
            for tier, slot_map in raw_overrides.items():
                if not isinstance(tier, str) or tier not in BUDGETS:
                    findings.append(
                        Finding(
                            "error",
                            f"budget_overrides.{tier!r} must be one of {', '.join(BUDGETS)}",
                        )
                    )
                    continue
                overrides[tier] = MappingProxyType(
                    _validate_slots_map(slot_map, f"budget_overrides.{tier}", findings)
                )
    shares = _validate_shares(data.get("shares"), findings)
    if any(f.severity == "error" for f in findings):
        return None, findings
    return (
        RoutingConfig(
            budget=budget,
            roles=MappingProxyType(roles),
            budget_overrides=MappingProxyType(overrides),
            shares=MappingProxyType(shares),
        ),
        findings,
    )


def load_routing_yml(path: Path = ROOT / ROUTING_PATH) -> tuple[RoutingConfig | None, list[Finding]]:
    if not path.is_file():
        return None, [Finding("error", "file not found")]
    return parse_routing_yml(path.read_text(encoding="utf-8"))


def central_routing(path: Path = ROOT / ROUTING_PATH) -> RoutingConfig | None:
    """The shipped routing table, None when absent. Raises RoutingError when invalid."""
    if not path.is_file():
        return None
    config, findings = parse_routing_yml(path.read_text(encoding="utf-8"))
    if config is None:
        errors = "; ".join(f.message for f in findings if f.severity == "error")
        raise RoutingError(f"{path}: {errors}")
    return config


def _clamp_effort(effort: str | None, budget: str) -> str | None:
    if effort is None:
        return None
    ceiling = EFFORT_CEILING_BY_BUDGET[budget]
    if _EFFORT_RANK[effort] < _EFFORT_RANK[ceiling]:
        return ceiling
    return effort


def _slot_lookup(
    slots: Mapping[str, SlotEntry],
    role: str,
) -> SlotEntry | None:
    if role in slots:
        return slots[role]
    if role.startswith("build:") and "build" in slots:
        return slots["build"]
    return None


def _central_entry(
    routing: RoutingConfig,
    role: str,
    budget: str,
) -> tuple[SlotEntry | None, Literal["budget_overrides", "roles"]]:
    tier_entry = _slot_lookup(routing.budget_overrides.get(budget, {}), role)
    if tier_entry is not None:
        return tier_entry, "budget_overrides"
    return _slot_lookup(routing.roles, role), "roles"


def role_matrix(path: Path = ROOT / ROUTING_PATH) -> dict:
    if not path.is_file():
        return {"state": "missing", "roles": []}
    config, findings = parse_routing_yml(path.read_text(encoding="utf-8"))
    if config is None:
        errors = [f.message for f in findings if f.severity == "error"]
        return {"state": "invalid", "errors": errors, "roles": []}
    budget = config.budget
    rows = []
    for role in ROLE_SLOTS:
        entry, source = _central_entry(config, role, budget)
        if entry is None or entry == "inherit":
            rows.append({"role": role, "source": "unset" if entry is None else "inherit", "candidates": []})
            continue
        rows.append({"role": role, "source": source, "candidates": [
            {
                "rank": rank,
                "harness": cand.harness,
                "model": cand.model,
                "family": family_for(cand.harness, cand.model)[0],
                "effort": _clamp_effort(cand.effort, budget) if source == "roles" else cand.effort,
                "configured": cand.effort,
            }
            for rank, cand in enumerate(entry, 1)
        ]})
    return {"state": "ok", "budget": budget, "ceiling": EFFORT_CEILING_BY_BUDGET[budget], "roles": rows}


def _factory_override_entry(factory: FactoryConfig | None, role: str) -> SlotEntry | None:
    if factory is None or factory.routing is None:
        return None
    raw = factory.routing.slots
    key = role if role in raw else ("build" if role.startswith("build:") and "build" in raw else None)
    if key is None:
        return None
    entry = raw[key]
    if entry == "inherit":
        return "inherit"
    return tuple(Candidate(harness=h, model=m, effort=e) for h, m, e in entry)


def _legacy_fallback(factory: FactoryConfig | None, role: str) -> tuple[Candidate, ...]:
    if factory is None:
        return ()
    if role == "review":
        return (Candidate(harness=factory.reviewer_harness, model=factory.reviewer_model),)
    return (Candidate(harness=factory.builder_harness, model=factory.builder_model),)


def resolve_slot(
    role: str,
    *,
    routing: RoutingConfig | None = None,
    factory: FactoryConfig | None = None,
    budget: str | None = None,
    built_by: tuple[str, str | None] | None = None,
    refused_harnesses: Collection[str] = (),
    unavailable_harnesses: Collection[str] = (),
    drawn: Draw | None = None,
) -> ResolvedSlot:
    """Resolve the best eligible `(harness, model, effort)` for `role`.

    Precedence:
    1. Per-repo `factory.md` `routing.slots` override (unless `"inherit"`).
    2. Central `config/routing.yml` (`budget_overrides[budget]` then `roles`, unless `"inherit"`).
    3. Legacy `factory.md` `builder:` / `reviewer:` fallback.

    For `role == "review"` with `built_by` provided, mechanically filters out
    every candidate sharing `built_by`'s model family and fails closed if none
    remains eligible.

    A picked `drawn` moves its harness to the front of a `routing.yml` pool.
    A review draw whose harness shares the builder's family is recorded as
    lost with reason `same-family`, unless a guard already skipped it.
    """
    if role not in _ROLE_SET:
        raise RoutingError(f"unknown routing role {role!r}")
    effective_budget = (
        budget
        or (factory.routing.budget if factory is not None and factory.routing is not None else None)
        or (routing.budget if routing is not None else None)
        or "medium"
    )
    if effective_budget not in BUDGETS:
        raise RoutingError(f"unknown budget tier {effective_budget!r}")

    excluded_family: str | None = None
    if role == "review" and built_by is not None:
        builder_harness, builder_model = built_by
        excluded_family, _ = family_for(builder_harness, builder_model)
        if excluded_family is None:
            raise RoutingError(
                f"cannot enforce cross-family review: builder family unresolved for {builder_harness!r} ({builder_model!r})"
            )

    blocked = frozenset(refused_harnesses) | frozenset(unavailable_harnesses)

    pool: tuple[Candidate, ...] = ()
    source = "factory-fallback"
    clamp_budget = True

    override = _factory_override_entry(factory, role)
    if override is not None and override != "inherit":
        pool = override
        source = "factory-override"
        clamp_budget = False
    else:
        central, central_source = (
            _central_entry(routing, role, effective_budget) if routing is not None else (None, "roles")
        )
        clamp_budget = central_source == "roles"
        if central is not None and central != "inherit":
            pool = central
            source = "routing.yml"
        else:
            pool = _legacy_fallback(factory, role)
            source = "factory-fallback"
            clamp_budget = False

    applied = drawn
    if drawn is not None and excluded_family is not None and family_for(drawn.harness, None)[0] == excluded_family:
        applied = replace(drawn, picked=False, skipped_reason=drawn.skipped_reason or "same-family")
    if source == "routing.yml" and applied is not None and applied.picked:
        pool = tuple(sorted(pool, key=lambda c: c.harness != applied.harness))

    for candidate in pool:
        if candidate.harness in blocked:
            continue
        fam, hint_fam = family_for(candidate.harness, candidate.model)
        if fam is None:
            continue
        if hint_fam is not None and fam != hint_fam:
            continue
        if excluded_family is not None and fam == excluded_family:
            continue
        effort = _clamp_effort(candidate.effort, effective_budget) if clamp_budget else candidate.effort
        return ResolvedSlot(
            role=role,
            harness=candidate.harness,
            model=candidate.model,
            effort=effort,
            family=fam,
            budget=effective_budget,
            source=source,
            draw=applied,
        )

    reasons: list[str] = []
    if excluded_family is not None:
        reasons.append(f"must differ from builder family {excluded_family!r}")
    if blocked:
        reasons.append(f"blocked harnesses: {', '.join(sorted(blocked))}")
    detail = f" ({'; '.join(reasons)})" if reasons else ""
    raise RoutingError(f"no eligible candidate for role {role!r}{detail}")


_BUILD_ROLE_BY_TICKET_TYPE: dict[str, str] = {
    "feature": "build:feature",
    "bug": "build:bug",
    "bug-fix": "build:bug",
    "refactor": "build:refactor",
    "refactoring": "build:refactor",
    "perf": "build:perf",
    "perf-issue": "build:perf",
}


def build_role_for_ticket_type(ticket_type: str | None) -> str:
    return _BUILD_ROLE_BY_TICKET_TYPE.get(ticket_type or "", "build")


REFERENCE_MEAN_WEIGHT = 1.25


@dataclass(frozen=True)
class TicketFacts:
    repo: str
    ticket: int
    risk: str | None
    plan_first: bool
    touches: tuple[str, ...] = ()
    ticket_type: str | None = None

    @property
    def weight(self) -> float:
        score = (
            2 * (self.risk == "high")
            + 2 * self.plan_first
            + (len(set(self.touches)) >= 4)
            + (build_role_for_ticket_type(self.ticket_type) != "build:bug")
        )
        return 0.5 + 0.25 * score


@dataclass(frozen=True)
class HeadroomReading:
    window: str
    headroom: float
    observed_at: float


@dataclass(frozen=True)
class GuardFacts:
    refused: bool = False
    signed_out: bool = False
    usage_held: bool = False
    readings: tuple[HeadroomReading, ...] = ()
    builds_in_flight: int = 0


SkipReason = Literal["refused", "signed-out", "usage-held", "low-headroom", "builds-in-flight", "same-family"]
DrawRole = Literal["build", "review"]


@dataclass(frozen=True)
class Hardness:
    risk: str | None
    plan_first: bool
    touches: int
    ticket_type: str | None
    weight: float


@dataclass(frozen=True)
class Draw:
    role: DrawRole
    harness: str
    seed: str
    draw: float
    share: float
    chance: float
    picked: bool
    skipped_reason: SkipReason | None
    hardness: Hardness | None = None


def _low_headroom(guards: GuardFacts, shares: HarnessShares) -> bool:
    latest = {r.window: r for r in sorted(guards.readings, key=lambda r: (r.observed_at, -r.headroom))}
    return any(r.headroom < shares.min_headroom for r in latest.values())


_GUARDS: tuple[tuple[SkipReason, Callable[[DrawRole, GuardFacts, HarnessShares], bool]], ...] = (
    ("refused", lambda role, guards, shares: guards.refused),
    ("signed-out", lambda role, guards, shares: guards.signed_out),
    ("usage-held", lambda role, guards, shares: guards.usage_held),
    ("low-headroom", lambda role, guards, shares: _low_headroom(guards, shares)),
    (
        "builds-in-flight",
        lambda role, guards, shares: role == "build" and guards.builds_in_flight >= shares.max_builds_in_flight,
    ),
)


def _chance(role: DrawRole, ticket: TicketFacts, shares: HarnessShares, build: Draw | None) -> float:
    if role == "build":
        return min(1.0, shares.build * ticket.weight / REFERENCE_MEAN_WEIGHT)
    taken = build.chance if build is not None and build.skipped_reason is None else 0.0
    if taken >= 1.0:
        return 1.0 if shares.review > 0 else 0.0
    return min(1.0, shares.review / (1 - taken))


def draw(
    role: DrawRole,
    ticket: TicketFacts,
    shares: HarnessShares,
    guards: GuardFacts = GuardFacts(),
    *,
    build: Draw | None = None,
) -> Draw:
    seed = f"{ticket.repo.lower()}#{ticket.ticket}:{role}"
    value = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") / 2**64
    chance = _chance(role, ticket, shares, build)
    skipped = next((reason for reason, held in _GUARDS if held(role, guards, shares)), None)
    return Draw(
        role=role,
        harness="gemini",
        seed=seed,
        draw=value,
        share=shares.build if role == "build" else shares.review,
        chance=chance,
        picked=skipped is None and value < chance,
        skipped_reason=skipped,
        hardness=(
            Hardness(ticket.risk, ticket.plan_first, len(set(ticket.touches)), ticket.ticket_type, ticket.weight)
            if role == "build"
            else None
        ),
    )


@dataclass(frozen=True)
class Sample:
    build: Draw | None = None
    review: Draw | None = None


def _draw_json(d: Draw) -> dict:
    entry = {
        "role": d.role,
        "harness": d.harness,
        "seed": d.seed,
        "draw": d.draw,
        "share": d.share,
        "chance": d.chance,
        "picked": d.picked,
        "skipped_reason": d.skipped_reason,
    }
    if d.hardness is not None:
        h = d.hardness
        entry["hardness"] = {
            "risk": h.risk,
            "plan_first": h.plan_first,
            "touches": h.touches,
            "type": h.ticket_type,
            "weight": h.weight,
        }
    return entry


def sample_json(sample: Sample) -> dict:
    pairs = (("build", sample.build), ("review", sample.review))
    return {role: _draw_json(d) for role, d in pairs if d is not None}


def _number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


_SKIP_REASONS = frozenset(get_args(SkipReason))
_DRAW_FIELDS: Mapping[str, Callable[[object], bool]] = MappingProxyType({
    "harness": lambda v: isinstance(v, str),
    "seed": lambda v: isinstance(v, str),
    "draw": _number,
    "share": _number,
    "chance": _number,
    "picked": lambda v: isinstance(v, bool),
    "skipped_reason": lambda v: v is None or (isinstance(v, str) and v in _SKIP_REASONS),
})
_HARDNESS_FIELDS: Mapping[str, Callable[[object], bool]] = MappingProxyType({
    "risk": _optional_str,
    "plan_first": lambda v: isinstance(v, bool),
    "touches": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "type": _optional_str,
    "weight": _number,
})


def _fields_ok(raw: object, fields: Mapping[str, Callable[[object], bool]]) -> bool:
    return isinstance(raw, dict) and all(key in raw and ok(raw[key]) for key, ok in fields.items())


def _parse_draw(role: DrawRole, raw: object) -> Draw | None:
    if not _fields_ok(raw, _DRAW_FIELDS) or raw.get("role") != role:
        return None
    hardness = None
    if role == "build":
        h = raw.get("hardness")
        if not _fields_ok(h, _HARDNESS_FIELDS):
            return None
        hardness = Hardness(h["risk"], h["plan_first"], h["touches"], h["type"], float(h["weight"]))
    return Draw(
        role=role,
        harness=raw["harness"],
        seed=raw["seed"],
        draw=float(raw["draw"]),
        share=float(raw["share"]),
        chance=float(raw["chance"]),
        picked=raw["picked"],
        skipped_reason=raw["skipped_reason"],
        hardness=hardness,
    )


def parse_sample(raw: object) -> Sample | None:
    """The stored sample record, or None when it is malformed."""
    if not isinstance(raw, dict) or not raw or set(raw) - {"build", "review"}:
        return None
    draws = {role: _parse_draw(role, entry) for role, entry in raw.items()}
    if any(d is None for d in draws.values()):
        return None
    return Sample(**draws)


def resolve_pair(
    *,
    routing: RoutingConfig | None = None,
    factory: FactoryConfig | None = None,
    ticket_type: str | None = None,
    budget: str | None = None,
    built_by: tuple[str, str | None] | None = None,
    refused_harnesses: Collection[str] = (),
    unavailable_harnesses: Collection[str] = (),
) -> tuple[ResolvedSlot, ResolvedSlot]:
    """Resolve both builder and cross-family reviewer slots together."""
    if built_by is not None:
        b_harness, b_model = built_by
        b_fam, _ = family_for(b_harness, b_model)
        if b_fam is None:
            raise RoutingError(f"built_by family not established for {b_harness!r}")
        builder = ResolvedSlot(
            role=build_role_for_ticket_type(ticket_type),
            harness=b_harness,
            model=b_model,
            effort=None,
            family=b_fam,
            budget=budget or (routing.budget if routing is not None else "medium"),
            source="built-by",
        )
    else:
        builder = resolve_slot(
            build_role_for_ticket_type(ticket_type),
            routing=routing,
            factory=factory,
            budget=budget,
            refused_harnesses=refused_harnesses,
            unavailable_harnesses=unavailable_harnesses,
        )
    reviewer = resolve_slot(
        "review",
        routing=routing,
        factory=factory,
        budget=budget,
        built_by=(builder.harness, builder.model),
        refused_harnesses=refused_harnesses,
        unavailable_harnesses=unavailable_harnesses,
    )
    return builder, reviewer


_CODEX_EFFORT_KEY = "model_reasoning_effort"


def _without_option(
    argv: Sequence[str], names: Collection[str], value_prefix: str = ""
) -> list[str]:
    end = argv.index("--") if "--" in argv else len(argv)
    kept = list(argv[:1])
    idx = 1
    while idx < end:
        arg = argv[idx]
        if arg in names and (idx + 1 == end or argv[idx + 1].startswith(value_prefix)):
            idx = min(idx + 2, end)
            continue
        if not any(arg.startswith(f"{name}={value_prefix}") for name in names):
            kept.append(arg)
        idx += 1
    return [*kept, *argv[end:]]


# Options whose value is the prompt, so `<option> -` stays one pair (agy's
# -p/--print/--prompt each take the prompt as their value).
_PROMPT_OPTIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"gemini": frozenset({"-p", "--print", "--prompt"})}
)
# Harnesses whose CLI takes `--effort <level>` (agy's levels are EFFORTS).
_EFFORT_OPTION_HARNESSES = frozenset({"claude", "gemini"})


def _insert_index(argv: Sequence[str], harness: str) -> int:
    if "--" in argv:
        return argv.index("--")
    if len(argv) >= 2 and argv[-1] == "-":
        if len(argv) >= 3 and argv[-2] in _PROMPT_OPTIONS.get(harness, frozenset()):
            return len(argv) - 2
        return len(argv) - 1
    return len(argv)


def apply_argv_flags(
    command: Sequence[str],
    *,
    harness: str,
    model: str | None,
    effort: str | None,
) -> list[str]:
    """Return `command` with validated `--model` and effort flags.

    A routed flag replaces the same flag pinned before `--`. Never mutates
    `command`. Inserts before `--` or a trailing `-` stdin marker. Rejects
    any model or effort value outside the strict token allowlist so
    untrusted text can never reach argv.
    """
    argv = list(command)
    if not argv:
        return argv
    extra: list[str] = []
    if model is not None:
        if not isinstance(model, str) or _MODEL_TOKEN.fullmatch(model) is None:
            raise RoutingError(f"invalid model token {model!r}")
        argv = _without_option(argv, ("--model", "-m"))
        extra.extend(["--model", model])
    if effort is not None:
        if not isinstance(effort, str) or effort not in EFFORTS:
            raise RoutingError(f"invalid effort level {effort!r}")
        if harness in _EFFORT_OPTION_HARNESSES:
            argv = _without_option(argv, ("--effort",))
            extra.extend(["--effort", effort])
        elif harness == "codex":
            argv = _without_option(argv, ("-c", "--config"), f"{_CODEX_EFFORT_KEY}=")
            extra.extend(["-c", f"{_CODEX_EFFORT_KEY}={effort}"])
    if not extra:
        return argv
    idx = _insert_index(argv, harness)
    return [*argv[:idx], *extra, *argv[idx:]]


def routed_slot(routing: dict, *, action: object, harness: str) -> tuple[object, object]:
    """The (model, effort) a job's routing names for the harness that runs it."""
    side = "reviewer" if action == "review" else "builder"
    side_harness = routing.get(side)
    if side_harness is not None and side_harness != harness:
        return None, None
    pick = "plan" if action == "plan" and isinstance(routing.get("planner"), str) else side
    return routing.get(f"{pick}_model"), routing.get(f"{pick}_effort")


def routing_argv_for_detail(
    command: Sequence[str],
    *,
    harness: str,
    detail: str | None,
) -> list[str]:
    """Apply validated routing model/effort from a queued job's detail JSON, if present."""
    if not detail or not command:
        return list(command)
    driver = harnesses.find(harness)
    if driver is None:
        return list(command)
    name = Path(command[0]).name
    if not (driver.matches_binary(name) or name in driver.wrapper_binaries):
        return list(command)
    try:
        parsed = json.loads(detail)
    except ValueError:
        return list(command)
    if not isinstance(parsed, dict):
        return list(command)
    routing = parsed.get("routing")
    if not isinstance(routing, dict):
        return list(command)
    model, effort = routed_slot(routing, action=parsed.get("action"), harness=harness)
    if model is None and effort is None:
        return list(command)
    return apply_argv_flags(
        command,
        harness=harness,
        model=model if isinstance(model, str) else None,
        effort=effort if isinstance(effort, str) else None,
    )
