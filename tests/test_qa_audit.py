from __future__ import annotations

import ast
from pathlib import Path

import pytest

from lib.qa_audit import AskedScenario, Gap, LaneEvidence, audit

START = "a" * 40
METHOD = "verify-shop"
S1 = AskedScenario(id="s-1", after="s-1/after.png", pass_output="s-1/pass.txt", video="s-1/video.webm")
S2 = AskedScenario(id="s-2", after="s-2/after.png", pass_output="s-2/pass.txt")


def _entry(scenario: AskedScenario, result: str = "verified") -> dict:
    paths = [p for p in (scenario.after, scenario.pass_output, scenario.video) if p]
    return {
        "id": scenario.id,
        "criterion": "ac-1",
        "result": result,
        "artifacts": [{"path": p, "sha256": "0" * 64, "size": 1} for p in paths],
        "observed": "the table shows three lines",
        "findings": [],
    }


def _lane(entries: list[dict], *, lane: int = 1, mismatches: list[dict] | None = None, drop: tuple[str, ...] = (), **result_fields) -> LaneEvidence:
    listed = [a for e in entries if isinstance(e, dict) for a in e["artifacts"] if isinstance(a, dict)]
    files = [a["path"] for a in listed if isinstance(a["path"], str) and a["path"] not in drop]
    result = {"run_id": "run-1", "lane": lane, "commit": START, "method": METHOD, "scenarios": entries, **result_fields}
    manifest = {
        "run_id": "run-1",
        "lane": lane,
        "commit": result.get("commit"),
        "files": [{"path": p, "sha256": "0" * 64, "size": 1} for p in files] + [{"path": "result.json", "sha256": "1" * 64, "size": 9}],
        "mismatches": mismatches or [],
    }
    return LaneEvidence(lane=lane, commit=START, method=METHOD, result=result, manifest=manifest)


def _run(lanes: list[LaneEvidence], asked: tuple[AskedScenario, ...] = (S1, S2), head: str = START):
    return audit(lanes, asked, start_commit=START, current_head=head)


def test_a_run_that_passes_all_five_checks_is_clean() -> None:
    lanes = [_lane([_entry(S1)]), _lane([_entry(S2, "needs-human")], lane=2)]

    assert _run(lanes) == "clean"


def test_a_verified_scenario_without_its_after_screenshot_yields_one_gap_naming_it() -> None:
    lanes = [_lane([_entry(S1), _entry(S2)], drop=(S1.after,))]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario) for g in gaps] == [("artifact-missing", "s-1")]


def test_a_listed_after_screenshot_that_never_arrived_still_yields_one_gap() -> None:
    lanes = [_lane([_entry(S1), _entry(S2)], drop=(S1.after,), mismatches=[{"path": S1.after, "reason": "missing"}])]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario) for g in gaps] == [("artifact-missing", "s-1")]


def test_a_verified_scenario_missing_its_video_or_pass_output_yields_a_gap_each() -> None:
    lanes = [_lane([_entry(S1), _entry(S2)], drop=(S1.video, S2.pass_output))]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario) for g in gaps] == [("artifact-missing", "s-1"), ("artifact-missing", "s-2")]


def test_a_scenario_that_asked_for_no_video_needs_none() -> None:
    assert _run([_lane([_entry(S1), _entry(S2)])]) == "clean"


def test_a_sha256_mismatch_on_an_artifact_a_verified_scenario_relies_on_yields_a_gap() -> None:
    lanes = [_lane([_entry(S1), _entry(S2)], mismatches=[{"path": S1.after, "reason": "sha256"}])]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario, g.lane) for g in gaps] == [("artifact-mismatch", "s-1", 1)]


def test_a_sha256_mismatch_on_an_artifact_of_a_failed_scenario_yields_no_gap() -> None:
    lanes = [_lane([_entry(S1, "failed"), _entry(S2)], mismatches=[{"path": S1.after, "reason": "sha256"}])]

    assert _run(lanes) == "clean"


def test_a_mismatch_on_an_extra_artifact_a_verified_scenario_listed_yields_a_gap() -> None:
    entry = _entry(S2)
    entry["artifacts"].append({"path": "s-2/trace.zip", "sha256": "0" * 64, "size": 1})
    lanes = [_lane([_entry(S1), entry], mismatches=[{"path": "s-2/trace.zip", "reason": "size"}])]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario) for g in gaps] == [("artifact-mismatch", "s-2")]


def test_a_mismatch_on_a_file_no_verified_scenario_relies_on_yields_no_gap() -> None:
    lanes = [_lane([_entry(S1), _entry(S2)], mismatches=[{"path": "network.json", "reason": "invalid network.json"}])]

    assert _run(lanes) == "clean"


def test_a_listed_scenario_in_no_lane_result_yields_a_gap_naming_it() -> None:
    gaps = _run([_lane([_entry(S1)])])

    assert [(g.kind, g.scenario) for g in gaps] == [("scenario-missing", "s-2")]


def test_a_scenario_with_an_unknown_result_counts_as_missing_and_malformed() -> None:
    gaps = _run([_lane([_entry(S1), _entry(S2, "pass")])])

    assert [(g.kind, g.scenario) for g in gaps] == [("evidence-malformed", None), ("scenario-missing", "s-2")]


def test_each_of_the_four_results_counts_as_reported() -> None:
    for result in ("verified", "failed", "inconclusive", "needs-human"):
        assert _run([_lane([_entry(S1), _entry(S2, result)])]) == "clean"


def test_a_lane_result_with_no_commit_yields_a_gap() -> None:
    lane = _lane([_entry(S1), _entry(S2)])
    del lane.result["commit"]

    gaps = _run([lane])

    assert [(g.kind, g.lane, g.scenario) for g in gaps] == [("lane-commit", 1, None)]


def test_a_lane_result_naming_another_commit_or_method_yields_a_gap_each() -> None:
    lanes = [_lane([_entry(S1), _entry(S2)], commit="b" * 40, method="fallback-playwright")]

    gaps = _run(lanes)

    assert [(g.kind, g.lane) for g in gaps] == [("lane-commit", 1), ("lane-method", 1)]


def test_a_moved_head_yields_exactly_one_head_moved_gap() -> None:
    lanes = [_lane([_entry(S1)]), _lane([_entry(S2)], lane=2)]

    gaps = _run(lanes, head="c" * 40)

    assert gaps == (Gap(kind="head-moved", scenario=None, lane=None, detail=gaps[0].detail),)


def test_a_malformed_result_fails_closed_instead_of_raising() -> None:
    lane = LaneEvidence(lane=1, commit=START, method=METHOD, result={"scenarios": "nope"}, manifest={"files": None})

    gaps = _run([lane])

    assert [(g.kind, g.scenario) for g in gaps] == [
        ("lane-commit", None),
        ("lane-method", None),
        ("evidence-malformed", None),
        ("scenario-missing", "s-1"),
        ("scenario-missing", "s-2"),
    ]


def test_a_malformed_scenario_entry_is_skipped_and_its_artifacts_still_checked() -> None:
    entry = _entry(S1)
    entry["artifacts"] = [None, {"path": 3}, *entry["artifacts"]]
    lanes = [_lane([None, "s-2", entry, _entry(S2)], mismatches=[None, {"path": S1.after, "reason": "sha256"}])]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario) for g in gaps] == [("evidence-malformed", None), ("evidence-malformed", "s-1"), ("artifact-mismatch", "s-1")]


@pytest.mark.parametrize(
    "spoil",
    [
        lambda e: e.update(result=None),
        lambda e: e.update(result=3),
        lambda e: e.update(result=["verified"]),
        lambda e: e.update(result={"verified": True}),
        lambda e: e.update(result="Verified"),
        lambda e: e.update(id=None),
        lambda e: e.update(id=["s-1"]),
    ],
)
def test_a_malformed_scenario_entry_another_lane_covers_cannot_hide_its_mismatch(spoil) -> None:
    entry = _entry(S1)
    spoil(entry)
    lanes = [
        _lane([entry], mismatches=[{"path": S1.after, "reason": "sha256"}]),
        _lane([_entry(S1, "failed"), _entry(S2, "failed")], lane=2),
    ]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario, g.lane) for g in gaps] == [("evidence-malformed", None, 1)]


@pytest.mark.parametrize("scenarios", [None, "s-1", {"s-1": "verified"}, [None], ["s-1"]])
def test_a_malformed_scenario_list_another_lane_covers_yields_a_gap_instead_of_clean(scenarios: object) -> None:
    lanes = [
        _lane([], scenarios=scenarios, mismatches=[{"path": S1.after, "reason": "sha256"}]),
        _lane([_entry(S1, "failed"), _entry(S2, "failed")], lane=2),
    ]

    gaps = _run(lanes)

    assert [(g.kind, g.scenario, g.lane) for g in gaps] == [("evidence-malformed", None, 1)]


def test_a_malformed_mismatch_list_and_scenario_list_share_one_lane_gap() -> None:
    lanes = [_lane([_entry(S1), _entry(S2)], scenarios=[_entry(S1), _entry(S2), None]), _lane([], lane=2)]
    lanes[0].manifest["mismatches"] = {"path": S1.after}

    gaps = _run(lanes)

    assert [(g.kind, g.scenario, g.lane) for g in gaps] == [("evidence-malformed", None, 1)]
    assert "mismatches" in gaps[0].detail and "scenarios" in gaps[0].detail


def test_a_mismatch_list_turned_into_a_dict_yields_a_gap_instead_of_clean() -> None:
    lane = _lane([_entry(S1), _entry(S2)], mismatches=[{"path": S1.after, "reason": "sha256"}])
    assert [(g.kind, g.scenario, g.lane) for g in _run([lane])] == [("artifact-mismatch", "s-1", 1)]

    lane.manifest["mismatches"] = {"path": S1.after, "reason": "sha256"}
    gaps = _run([lane])

    assert gaps != "clean"
    assert ("evidence-malformed", None, 1) in [(g.kind, g.scenario, g.lane) for g in gaps]


ABSENT = object()


@pytest.mark.parametrize(
    ("mismatches", "readable"),
    [
        ({"path": S1.after, "reason": "sha256"}, []),
        (None, []),
        (ABSENT, []),
        (S1.after, []),
        ([None], []),
        ([{"path": 3}], []),
        ([{"path": 3}, {"path": S1.after, "reason": "sha256"}], [("artifact-mismatch", "s-1", 1)]),
    ],
    ids=["dict", "none", "absent", "string", "list-with-none", "list-with-int-path", "partly-readable"],
)
def test_a_malformed_mismatch_list_yields_one_evidence_malformed_gap_for_the_lane(mismatches: object, readable: list) -> None:
    lane = _lane([_entry(S1), _entry(S2)])
    if mismatches is ABSENT:
        del lane.manifest["mismatches"]
    else:
        lane.manifest["mismatches"] = mismatches

    gaps = _run([lane])

    assert [(g.kind, g.scenario, g.lane) for g in gaps] == [("evidence-malformed", None, 1), *readable]


def test_a_manifest_that_is_not_a_mapping_yields_an_evidence_malformed_gap() -> None:
    lane = _lane([_entry(S1), _entry(S2)])
    lane = LaneEvidence(lane=1, commit=START, method=METHOD, result=lane.result, manifest=[lane.manifest])

    gaps = _run([lane])

    assert gaps != "clean"
    assert [(g.kind, g.scenario, g.lane) for g in gaps if g.kind == "evidence-malformed"] == [("evidence-malformed", None, 1)]


@pytest.mark.parametrize("artifacts", [{"path": S1.after}, None, [S1.after]], ids=["dict", "none", "list-with-string"])
def test_a_verified_scenario_with_malformed_artifacts_yields_an_evidence_malformed_gap_naming_it(artifacts: object) -> None:
    lane = _lane([_entry(S1), _entry(S2)])
    lane.result["scenarios"][0]["artifacts"] = artifacts

    gaps = _run([lane])

    assert [(g.kind, g.scenario, g.lane) for g in gaps] == [("evidence-malformed", "s-1", 1)]


@pytest.mark.parametrize("artifacts", [{"path": S1.after}, None, [S1.after]], ids=["dict", "none", "list-with-string"])
def test_a_failed_scenario_with_malformed_artifacts_yields_no_gap(artifacts: object) -> None:
    lane = _lane([_entry(S1, "failed"), _entry(S2)])
    lane.result["scenarios"][0]["artifacts"] = artifacts

    assert _run([lane]) == "clean"


def test_a_scenario_marked_verified_twice_with_malformed_artifacts_yields_one_gap() -> None:
    lane = _lane([_entry(S1), _entry(S1), _entry(S2)])
    lane.result["scenarios"][0]["artifacts"] = None
    lane.result["scenarios"][1]["artifacts"] = None

    gaps = _run([lane])

    assert [(g.kind, g.scenario, g.lane) for g in gaps] == [("evidence-malformed", "s-1", 1)]


def test_the_audit_never_changes_a_result_or_manifest() -> None:
    lane = _lane([_entry(S1), _entry(S2)], mismatches=[{"path": S1.after, "reason": "sha256"}])
    before = (repr(lane.result), repr(lane.manifest))

    _run([lane], head="c" * 40)

    assert (repr(lane.result), repr(lane.manifest)) == before


def test_the_module_imports_nothing_that_does_io() -> None:
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("lib", "qa_audit.py").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")])
    }

    assert imported <= {"__future__", "collections", "dataclasses", "typing"}
