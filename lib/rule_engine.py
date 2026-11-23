"""The pure core of the rule engine (issue #1319)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NewType, Protocol

from lib import handoff
from lib.hold import glob_matches

Site = Literal["cap", "repeat", "moved-head"]
StopKind = Literal["capped", "stalled", "repeat"]
ScanResult = Literal["clean", "found", "too-large"]
CIState = Literal["success", "pending", "failure", "not-run", "unknown"]

FindingId = NewType("FindingId", str)
CommentId = NewType("CommentId", str)
Sha = NewType("Sha", str)
GitPath = NewType("GitPath", str)

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_COMMENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def finding_id(text: str) -> FindingId:
    if handoff.FINDING_ID.fullmatch(text) is None:
        raise ValueError(f"bad finding id {text!r}")
    return FindingId(text)


def comment_id(text: str) -> CommentId:
    if _COMMENT_ID.fullmatch(text) is None:
        raise ValueError(f"bad comment id {text!r}")
    return CommentId(text)


def sha(text: str) -> Sha:
    if handoff.HEAD_SHA.fullmatch(text) is None:
        raise ValueError(f"bad sha {text!r}")
    return Sha(text)


def git_path(text: str) -> GitPath:
    if text == "" or text.startswith("/") or ".." in text.split("/") or _CONTROL.search(text):
        raise ValueError(f"bad git path {text!r}")
    return GitPath(text)


@dataclass(frozen=True)
class StopFacts:
    site: Literal["cap", "repeat"]
    stop: StopKind
    verdict_id: CommentId
    finding_ids: tuple[FindingId, ...]
    repeat_stops: int
    prior_grants: frozenset[FindingId] | None
    round: int
    cap: int
    stuck: bool


@dataclass(frozen=True)
class HeadProbe:
    parents: tuple[Sha, ...]
    second_parent_on_base: bool
    remerge_empty: bool
    changed_paths: tuple[GitPath, ...]
    scan: ScanResult


@dataclass(frozen=True)
class MovedHeadFacts:
    ok_head: Sha
    head: Sha
    probe: HeadProbe | None
    held_patterns: tuple[GitPath, ...]
    ci: CIState


Facts = StopFacts | MovedHeadFacts


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool | None
    value: str | int | None


@dataclass(frozen=True)
class Grant:
    header: str


@dataclass(frozen=True)
class Land:
    pass


@dataclass(frozen=True)
class Wait:
    pass


@dataclass(frozen=True)
class Escalate:
    reason: str


Outcome = Grant | Land | Wait | Escalate


@dataclass(frozen=True)
class Ruling:
    rule: str
    outcome: Outcome
    checks: tuple[Check, ...]


class Rule(Protocol):
    name: str

    def applies(self, facts: Facts) -> bool: ...

    def rule(self, facts: Facts) -> Ruling: ...


class FirstCapGrant:
    name = "first-cap-grant"

    def applies(self, facts: Facts) -> bool:
        return isinstance(facts, StopFacts)

    def rule(self, facts: Facts) -> Ruling:
        assert isinstance(facts, StopFacts)
        repeated = ["unknown"] if facts.prior_grants is None else sorted(set(facts.finding_ids) & facts.prior_grants)
        checks = (
            Check("stop", facts.stop != "stalled", facts.stop),
            Check("finding-ids", bool(facts.finding_ids), len(facts.finding_ids)),
            *((Check("repeat-stops", facts.repeat_stops == 1, facts.repeat_stops),) if facts.site == "repeat" else ()),
            Check("stuck", not facts.stuck, int(facts.stuck)),
            Check("earlier-round", not repeated, repeated[0] if repeated else None),
        )
        failed = next((check for check in checks if not check.passed), None)
        if failed is not None:
            return Ruling(self.name, Escalate(failed.name), checks)
        header = go_header(facts.round + 1, facts.cap + int(facts.round >= facts.cap))
        if isinstance(header, Escalate):
            return Ruling(self.name, header, (*checks, Check("header", False, None)))
        return Ruling(self.name, Grant(header), (*checks, Check("header", True, header)))


class MovedHeadLands:
    name = "moved-head-lands"

    def applies(self, facts: Facts) -> bool:
        return isinstance(facts, MovedHeadFacts)

    def rule(self, facts: Facts) -> Ruling:
        assert isinstance(facts, MovedHeadFacts)
        ci = Check("ci", None if facts.ci == "pending" else facts.ci == "success", facts.ci)
        probe = facts.probe
        if probe is None:
            unknown = (Check(name, None, None) for name in ("pure-merge", "remerge-diff", "held-paths", "secrets-scan"))
            return Ruling(self.name, Escalate("probe-unknown"), (*unknown, ci))
        held = [
            path for path in probe.changed_paths if any(glob_matches(pattern, path) for pattern in facts.held_patterns)
        ]
        pure = len(probe.parents) == 2 and probe.parents[0] == facts.ok_head and probe.second_parent_on_base
        checks = (
            Check("pure-merge", pure, len(probe.parents)),
            Check("remerge-diff", probe.remerge_empty, None),
            Check("held-paths", not held, len(held)),
            Check("secrets-scan", probe.scan == "clean", probe.scan),
            ci,
        )
        failed = next((check for check in checks if check.passed is False), None)
        if failed is not None:
            return Ruling(self.name, Escalate(failed.name), checks)
        return Ruling(self.name, Wait() if ci.passed is None else Land(), checks)


RULES: tuple[Rule, ...] = (FirstCapGrant(), MovedHeadLands())


def decide(facts: Facts, rules: Sequence[Rule] = RULES) -> Ruling:
    for rule in rules:
        if rule.applies(facts):
            return rule.rule(facts)
    return Ruling("no-rule", Escalate("no-rule"), ())


def go_header(round: int, cap: int) -> str | Escalate:
    fields = {"kind": "go", "from": "orch", "state": "fixing", "round": f"{round}/{cap}"}
    try:
        line = handoff.format_header(fields)
        parsed = handoff.parse_comment(line).as_dict()
    except handoff.HandoffError:
        return Escalate("header-invalid")
    return line if parsed == fields else Escalate("header-invalid")
