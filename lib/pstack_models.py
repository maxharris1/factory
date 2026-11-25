"""One helper model map for every harness (#160).

config/pstack-models.yml gives each pstack helper role one value per
harness, in that harness adapter's own format. The roles are read from
step 5 of the pinned setup-pstack SKILL.md, so a pin bump that changes the
role list fails here until the map catches up. brief_lines and rules_text
render from the same load_map result. Nothing here writes a file.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from lib.load import load_yaml
from lib.pstack_pin import ROOT
from lib.schema import HARNESSES

MAP_PATH = Path("config") / "pstack-models.yml"
SETUP_SKILL = Path("skills") / "pstack" / "skills" / "setup-pstack" / "SKILL.md"
STEP_HEADING = "### 5. Write the rule"

_EFFORT = r"(max|xhigh|high|medium|low)"
_ALIASES = frozenset({"inherit-parent", "auto"})
_VALUE_FORMS = {
    "claude": re.compile(rf"^(opus|sonnet|haiku|fable) {_EFFORT}$"),
    "codex": re.compile(rf"^gpt-[a-z0-9][a-z0-9.\-]* {_EFFORT}$"),
    "cursor": re.compile(r"^[a-z0-9][a-z0-9.\-]*$"),
    "gemini": re.compile(rf"^gemini-[a-z0-9][a-z0-9.\-]*(?: {_EFFORT})?$"),
}
_FOREIGN = {
    "claude": ("grok-",),
    "codex": ("grok-", "claude-"),
    "cursor": (),
    "gemini": ("grok-", "claude-", "gpt-"),
}
_HARNESS_KEYS = frozenset({"budget", "default", "panel"})

RULES_HEADER = (
    "# pstack model configuration. One line per role. Generated from "
    "config/pstack-models.yml by lib/pstack_models.py; do not edit by hand.\n"
    "# `inherit-parent` or `auto` as a value: the role runs on the parent chat model "
    "(omit Task `model`). Alias entries in a panel list still count toward its fan-out.\n"
    "# A factory job's brief carries its own helper model lines. "
    "Where the brief and this file differ, the brief wins.\n"
)


class ModelMapError(ValueError):
    pass


@dataclass(frozen=True)
class RoleLine:
    role: str
    value: str | tuple[str, ...]

    def render(self) -> str:
        value = ", ".join(self.value) if isinstance(self.value, tuple) else self.value
        return f"{self.role}: {value}"


def pinned_roles(root: Path = ROOT) -> tuple[tuple[str, bool], ...]:
    """(role, is_panel) in step-5 order, read from the pinned setup-pstack SKILL.md."""
    text = (root / SETUP_SKILL).read_text(encoding="utf-8")
    _, found, rest = text.partition(STEP_HEADING)
    if not found:
        raise ModelMapError(f"{SETUP_SKILL}: no {STEP_HEADING!r} step")
    parts = rest.split("```", 2)
    if len(parts) < 3:
        raise ModelMapError(f"{SETUP_SKILL}: step 5 has no fenced block")
    lines = parts[1].splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("# budget:")]
    if not starts:
        raise ModelMapError(f"{SETUP_SKILL}: step 5 block has no # budget line")
    roles: list[tuple[str, bool]] = []
    for line in lines[starts[0] + 1 :]:
        if not line.strip():
            continue
        role, sep, default = line.partition(": ")
        if not sep or not role or not default:
            raise ModelMapError(f"{SETUP_SKILL}: step 5 line is not '<role>: <default>': {line!r}")
        roles.append((role, "," in default))
    if not roles:
        raise ModelMapError(f"{SETUP_SKILL}: step 5 block lists no roles")
    return tuple(roles)


def load_map(root: Path = ROOT) -> dict[str, tuple[RoleLine, ...]]:
    """harness -> one line per pinned role, in pin order."""
    return {harness: lines for harness, (_, lines) in _load(root).items()}


def brief_lines(harness: str, root: Path = ROOT) -> str:
    """The brief's helper model block for one harness: one `<role>: <value>` line per role."""
    if harness not in HARNESSES:
        raise ModelMapError(f"unknown harness {harness!r}")
    return _render(load_map(root)[harness])


def rules_text(root: Path = ROOT) -> str:
    """Full text of ~/.claude/rules/pstack-models.md for the claude harness."""
    budget, lines = _load(root)["claude"]
    return f"{RULES_HEADER}# budget: {budget}\n{_render(lines)}"


def _render(lines: tuple[RoleLine, ...]) -> str:
    return "".join(f"{line.render()}\n" for line in lines)


def _load(root: Path) -> dict[str, tuple[str, tuple[RoleLine, ...]]]:
    roles = pinned_roles(root)
    data = load_yaml((root / MAP_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"roles", "harnesses", "overrides"}:
        raise ModelMapError(f"{MAP_PATH}: top level takes only roles, harnesses and overrides")
    pinned = [role for role, _ in roles]
    if data.get("roles") != pinned:
        listed = data.get("roles") if isinstance(data.get("roles"), list) else []
        added = [role for role in pinned if role not in listed]
        dropped = [str(role) for role in listed if role not in pinned]
        raise ModelMapError(
            f"{MAP_PATH}: roles differ from the pinned {SETUP_SKILL} step 5 "
            f"(missing: {', '.join(added) or 'none'}; not pinned: {', '.join(dropped) or 'none'}; "
            "the order must match too)"
        )
    harnesses = data.get("harnesses")
    overrides = data.get("overrides") or {}
    if not isinstance(harnesses, dict) or not isinstance(overrides, dict):
        raise ModelMapError(f"{MAP_PATH}: harnesses and overrides must be mappings")
    _same_harnesses("harnesses", set(harnesses), exact=True)
    _same_harnesses("overrides", set(overrides), exact=False)
    known = {role for role, _ in roles}
    mapping: dict[str, tuple[str, tuple[RoleLine, ...]]] = {}
    for harness in HARNESSES:
        spec = _harness_spec(harness, harnesses[harness])
        extra = overrides.get(harness) or {}
        if not isinstance(extra, dict):
            raise ModelMapError(f"{MAP_PATH}: overrides.{harness} must be a mapping")
        unknown = sorted(str(role) for role in set(extra) - known)
        if unknown:
            raise ModelMapError(f"{MAP_PATH}: overrides.{harness} names unpinned roles: {', '.join(unknown)}")
        lines = []
        for role, panel in roles:
            raw = extra.get(role, spec["panel"] if panel else spec["default"])
            lines.append(RoleLine(role, _role_value(harness, role, panel, raw)))
        mapping[harness] = (spec["budget"], tuple(lines))
    return mapping


def _same_harnesses(field: str, keys: set, exact: bool) -> None:
    unknown = sorted(str(key) for key in keys - set(HARNESSES))
    if unknown:
        raise ModelMapError(f"{MAP_PATH}: {field} has unknown harnesses: {', '.join(unknown)}")
    missing = [harness for harness in HARNESSES if harness not in keys]
    if exact and missing:
        raise ModelMapError(f"{MAP_PATH}: {field} is missing {', '.join(missing)}")


def _harness_spec(harness: str, spec: object) -> dict:
    if not isinstance(spec, dict) or set(spec) != _HARNESS_KEYS:
        raise ModelMapError(f"{MAP_PATH}: harnesses.{harness} takes exactly budget, default and panel")
    budget = spec["budget"]
    if not isinstance(budget, str) or not budget.strip() or "\n" in budget:
        raise ModelMapError(f"{MAP_PATH}: harnesses.{harness}.budget is not one line")
    return spec


def _role_value(harness: str, role: str, panel: bool, raw: object) -> str | tuple[str, ...]:
    where = f"{harness} {role!r}"
    if panel:
        if not isinstance(raw, list) or not raw:
            raise ModelMapError(f"{MAP_PATH}: {where} is a panel role and needs a non-empty list")
        return tuple(_entry(harness, where, entry) for entry in raw)
    return _entry(harness, where, raw)


def _entry(harness: str, where: str, entry: object) -> str:
    if not isinstance(entry, str):
        raise ModelMapError(f"{MAP_PATH}: {where} value is not a string: {entry!r}")
    for prefix in _FOREIGN[harness]:
        if prefix in entry:
            raise ModelMapError(f"{MAP_PATH}: {where} names a {prefix}* model: {entry!r}")
    if entry not in _ALIASES and _VALUE_FORMS[harness].fullmatch(entry) is None:
        raise ModelMapError(f"{MAP_PATH}: {where} is not in the {harness} adapter format: {entry!r}")
    return entry


def main(argv: list[str]) -> int:
    if argv == ["rules"]:
        sys.stdout.write(rules_text())
        return 0
    if len(argv) == 2 and argv[0] == "brief":
        sys.stdout.write(brief_lines(argv[1]))
        return 0
    sys.stderr.write("usage: python -m lib.pstack_models rules | brief <harness>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
