from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

GapKind = Literal["lane-commit", "lane-method", "scenario-missing", "artifact-missing", "artifact-mismatch", "evidence-malformed", "head-moved"]

REPORTED = frozenset({"verified", "failed", "inconclusive", "needs-human"})


@dataclass(frozen=True)
class AskedScenario:
    id: str
    after: str
    pass_output: str
    video: str | None = None


@dataclass(frozen=True)
class LaneEvidence:
    lane: int
    commit: str
    method: str
    result: Mapping[str, Any]
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class Gap:
    kind: GapKind
    scenario: str | None
    lane: int | None
    detail: str


@dataclass(frozen=True)
class _Entry:
    id: str
    result: str
    paths: tuple[str, ...]
    artifacts_well_formed: bool


@dataclass(frozen=True)
class _View:
    evidence: LaneEvidence
    commit: object
    method: object
    entries: tuple[_Entry, ...]
    files: frozenset[str]
    mismatched: frozenset[str]
    mismatches_well_formed: bool
    scenarios_well_formed: bool


def audit(
    lanes: Sequence[LaneEvidence], asked: Sequence[AskedScenario], *, start_commit: str, current_head: str
) -> Literal["clean"] | tuple[Gap, ...]:
    views = [_view(lane) for lane in lanes]
    by_id: dict[str, AskedScenario] = {}
    for scenario in asked:
        by_id.setdefault(scenario.id, scenario)
    gaps = (
        [gap for view in views for gap in _lane_claims(view) + _malformed(view)]
        + _unreported(views, by_id)
        + [gap for view in views for gap in _receipts(view, by_id)]
        + _head_moved(start_commit, current_head)
    )
    return tuple(gaps) or "clean"


def _view(lane: LaneEvidence) -> _View:
    result = lane.result if isinstance(lane.result, Mapping) else {}
    manifest = lane.manifest if isinstance(lane.manifest, Mapping) else {}
    entries = tuple(
        _Entry(item["id"], item["result"], tuple(_paths(item.get("artifacts"))), _well_formed(item.get("artifacts")))
        for item in _items(result.get("scenarios"))
        if isinstance(item.get("id"), str) and isinstance(item.get("result"), str)
    )
    return _View(
        evidence=lane,
        commit=result.get("commit"),
        method=result.get("method"),
        entries=entries,
        files=frozenset(_paths(manifest.get("files"))),
        mismatched=frozenset(_paths(manifest.get("mismatches"))),
        mismatches_well_formed=_well_formed(manifest.get("mismatches")),
        scenarios_well_formed=_scenarios_well_formed(result.get("scenarios")),
    )


def _scenarios_well_formed(raw: object) -> bool:
    return isinstance(raw, list) and all(
        isinstance(item, Mapping) and isinstance(item.get("id"), str) and isinstance(item.get("result"), str) and item["result"] in REPORTED
        for item in raw
    )


def _items(raw: object) -> Iterator[Mapping[str, Any]]:
    return (item for item in raw if isinstance(item, Mapping)) if isinstance(raw, list) else iter(())


def canonical_path(path: str) -> str:
    parts = [part for part in path.split("/") if part not in ("", ".")]
    return ("/" if path.startswith("/") else "") + "/".join(parts)


def _paths(raw: object) -> Iterator[str]:
    return (canonical_path(item["path"]) for item in _items(raw) if isinstance(item.get("path"), str))


def _well_formed(raw: object) -> bool:
    return isinstance(raw, list) and all(isinstance(item, Mapping) and isinstance(item.get("path"), str) for item in raw)


def _lane_claims(view: _View) -> list[Gap]:
    lane = view.evidence
    gaps = []
    if view.commit != lane.commit:
        gaps.append(Gap("lane-commit", None, lane.lane, f"Lane {lane.lane}'s result does not name the commit its brief named."))
    if view.method != lane.method:
        gaps.append(Gap("lane-method", None, lane.lane, f"Lane {lane.lane}'s result does not name the method its brief named."))
    return gaps


def _malformed(view: _View) -> list[Gap]:
    lane = view.evidence.lane
    gaps = []
    unsound = [
        what
        for what, sound in (
            ("manifest mismatches is not a list of entries that each name a path", view.mismatches_well_formed),
            ("result scenarios is not a list of entries that each name an id and one of the four results", view.scenarios_well_formed),
        )
        if not sound
    ]
    if unsound:
        gaps.append(Gap("evidence-malformed", None, lane, f"Lane {lane}'s {' and its '.join(unsound)}."))
    unreadable = dict.fromkeys(entry.id for entry in view.entries if entry.result == "verified" and not entry.artifacts_well_formed)
    gaps += [
        Gap("evidence-malformed", sid, lane, f"Lane {lane} marks {sid} verified but its artifacts are not a list of entries that each name a path.")
        for sid in unreadable
    ]
    return gaps


def _unreported(views: list[_View], by_id: dict[str, AskedScenario]) -> list[Gap]:
    reported = {entry.id for view in views for entry in view.entries if entry.result in REPORTED}
    return [
        Gap("scenario-missing", sid, None, f"No lane reports {sid} as verified, failed, inconclusive or needs-human.")
        for sid in by_id
        if sid not in reported
    ]


def _required(scenario: AskedScenario | None) -> list[tuple[str, str]]:
    if scenario is None:
        return []
    named = [("after screenshot", scenario.after), ("pass test output", scenario.pass_output), ("video", scenario.video)]
    return [(what, canonical_path(path)) for what, path in named if path is not None]


def _receipts(view: _View, by_id: dict[str, AskedScenario]) -> list[Gap]:
    lane = view.evidence.lane
    listed: dict[str, dict[str, None]] = {}
    for entry in view.entries:
        if entry.result == "verified":
            listed.setdefault(entry.id, {}).update(dict.fromkeys(entry.paths))
    gaps = []
    for sid, paths in listed.items():
        required = _required(by_id.get(sid))
        missing = {path for _, path in required if path not in view.files}
        gaps += [
            Gap("artifact-missing", sid, lane, f"Lane {lane} marks {sid} verified but its {what} {path} is not in the manifest.")
            for what, path in required
            if path in missing
        ]
        relied = dict.fromkeys([path for _, path in required] + list(paths))
        gaps += [
            Gap("artifact-mismatch", sid, lane, f"Lane {lane} marks {sid} verified but {path} does not match its manifest.")
            for path in relied
            if path in view.mismatched and path not in missing
        ]
    return gaps


def _head_moved(start_commit: str, current_head: str) -> list[Gap]:
    if start_commit == current_head:
        return []
    return [Gap("head-moved", None, None, f"The head moved from {start_commit[:12]} to {current_head[:12]} after the run started.")]
