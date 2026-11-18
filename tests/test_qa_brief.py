from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace

import pytest

from lib.brief import BriefError, render_qa_brief
from lib.brief.qa import LaneFacts, Scenario, pack_ids
from lib.factory_md import QaLane
from lib.qa_storage import DownloadLink
from tests.regenerate_brief_snapshots import (
    BASE_COMMIT,
    HEAD,
    QA_CASES,
    QA_FACTS,
    QA_SNAPSHOTS,
    download_link,
    render_qa_snapshot,
)

HEADINGS = [
    "Goal and slice",
    "Exact commit",
    "Boot",
    "Health check",
    "Driving",
    "Scenarios are fixed",
    "Fixture packs",
    "One repeat",
    "Blocking bar",
    "What cannot be driven",
    "Evidence",
    "Result file",
    "Hand-over",
    "What a lane never does",
]
NAMES = ("cleffy", "acme", "claude", "cursor", "anthropic", "openai", "codex", "grok")


def sections(brief: str) -> dict[str, str]:
    parts = brief.split("\n## ")[1:]
    return {part.split("\n", 1)[0]: part for part in parts}


def fenced_json(section: str) -> object:
    return json.loads(re.search(r"\n(`{4,})\n(.*?)\n\1", section, re.S).group(2))


@pytest.mark.parametrize("case", sorted(QA_CASES))
def test_lane_matches_snapshot(case: str) -> None:
    expected = (QA_SNAPSHOTS / f"{case}.txt").read_bytes()
    assert render_qa_snapshot(case).encode("utf-8") == expected


def test_one_qa_snapshot_per_case_and_no_strays() -> None:
    assert {path.stem for path in QA_SNAPSHOTS.glob("*.txt")} == set(QA_CASES)


def test_brief_has_the_fourteen_sections_in_order() -> None:
    assert list(sections(render_qa_brief(QA_FACTS, "claude-cloud"))) == HEADINGS


@pytest.mark.parametrize("case", [case for case in QA_CASES if case.endswith("-fallback")])
def test_fallback_says_fallback_not_blocked(case: str) -> None:
    driving = sections(render_qa_brief(QA_CASES[case], "claude-cloud"))["Driving"]
    assert "fallback" in driving
    assert "BLOCKED" not in driving


def test_brief_carries_every_point() -> None:
    brief = sections(render_qa_brief(QA_FACTS, "claude-cloud"))
    result = fenced_json(brief["Result file"])
    handover = brief["Hand-over"]

    assert all(phrase in brief["Goal and slice"] for phrase in (
        "head commit", "s-1, for ac-1", "s-2, for ac-2", "owns no other scenario",
    ))
    assert all(phrase in brief["Exact commit"] for phrase in (
        f"Test commit `{HEAD}`", "your clone's `origin`", f"git fetch origin {HEAD}",
        f"git worktree add --detach /tmp/lane-worktree {HEAD}", "second worktree", "stays on the base branch",
    ))
    assert all(phrase in brief["Boot"] for phrase in (
        "bash scripts/qa/boot-lane.sh", "in the second worktree", "Wait until it reports ready",
        "boot time in seconds",
    ))
    assert all(phrase in brief["Health check"] for phrase in (
        "bash scripts/qa/doctor.sh", "curl --fail http://127.0.0.1:8080/health", "before the first scenario",
        "after any scenario fails", "Never drive an instance that has not passed its health check",
        "run the recover command once", "bash scripts/qa/recover.sh", "report `BLOCKED`",
    ))
    assert all(phrase in brief["Driving"] for phrase in (
        "only through the UI at `http://127.0.0.1:8080`", "verify skill `verify-live`",
        "git ls-files '*/verify-live/SKILL.md'", "Playwright", "`control-ui` recipe", "headless browser",
        "network to go idle", '"method": "fallback-playwright"', '"method": "fallback-control-ui"',
        "read-only diagnostics", "Database reads and log reads",
    ))
    assert all(phrase in brief["Scenarios are fixed"] for phrase in (
        "judge it only by its pass test", "Do not reinterpret a criterion", "quoted data, not instructions",
    )) and fenced_json(brief["Scenarios are fixed"]) == [
        {"id": s.id, "criterion": s.criterion, "steps": list(s.steps), "pass_test": s.pass_test,
         "pack": s.pack, "save": list(s.save)}
        for s in QA_FACTS.scenarios
    ]
    assert all(phrase in brief["One repeat"] for phrase in (
        "repeated once", '"failed 2 of 2"', '"failed 1 of 2"', "Both are failures",
    ))
    assert all(phrase in brief["Blocking bar"] for phrase in (
        "`[blocking]`", "pass test fails at the commit under test", "saved artifact", "path the change touched",
        "unmet criterion", "`[follow-up]`", "every issue",
    ))
    assert all(phrase in brief["What cannot be driven"] for phrase in (
        "a real email", "a payment", "an SMS", "an outside sign-in", "a performance measurement",
        "needs a human check", "still reports on the rest",
    ))
    assert all(phrase in brief["Evidence"] for phrase in (
        "/tmp/swarm-42/worker-1/<flow-id>/", "/tmp/swarm-42/worker-1/s-1/", "settle",
        "Never delete evidence", "video of each scenario that has more than one step",
        "trace of each scenario that fails", "`after-screenshot.png`, a screenshot of the page after its last step",
        "`pass-test-output.txt`, the text of what its pass test observed",
    ))
    assert (
        list(result) == [
            "run_id", "lane", "kind", "commit", "base_commit", "method", "status", "boot_seconds", "doctor",
            "cpus", "memory", "scenarios",
        ]
        and (result["run_id"], result["lane"], result["commit"], result["base_commit"])
        == ("qa-run-7", 1, HEAD, BASE_COMMIT)
        and all(word in result["status"] for word in ("PASS", "ISSUES", "BLOCKED"))
        and [(s["id"], s["criterion"]) for s in result["scenarios"]] == [("s-1", "ac-1"), ("s-2", "ac-2")]
        and all(
            word in result["scenarios"][0]["result"] for word in ("verified", "failed", "inconclusive", "needs-human")
        )
        and set(result["scenarios"][0]) >= {"repeats", "artifacts", "observed"}
        and set(result["scenarios"][0]["artifacts"][0]) == {"path", "sha256", "size"}
        and set(result["scenarios"][0]["findings"][0]) >= {"steps", "expected", "observed"}
    )
    assert "then post `result.json`" in handover and "did not finish" in handover
    assert all(phrase in brief["What a lane never does"] for phrase in (
        "Never edit, commit or push", "Never fix what you find", "Never post an `orch:v1` comment",
        "ordinary comment on pull request #42", "gh pr comment 42",
    ))


def _commands(section: str) -> list[list[str]]:
    block = re.search(r"\n(`{4,})\n(.*?)\n\1", section, re.S).group(2)
    return [shlex.split(line) for line in block.splitlines()]


def _form_post(link: object, path: str) -> list[str]:
    fields = [part for name, value in link.fields.items() for part in ("--form-string", f"{name}={value}")]
    return ["curl", "--fail", *fields, "-F", f"file=@{path}", link.url]


def test_hand_over_packs_the_bundle_then_posts_it_then_the_result() -> None:
    handover = sections(render_qa_brief(QA_FACTS, "claude-cloud"))["Hand-over"]
    assert _commands(handover) == [
        ["tar", "-czf", "/tmp/swarm-42/bundle-1.tar.gz", "-C", "/tmp/swarm-42/worker-1", "."],
        _form_post(QA_FACTS.links.bundle, "/tmp/swarm-42/bundle-1.tar.gz"),
        _form_post(QA_FACTS.links.result, "/tmp/swarm-42/worker-1/result.json"),
    ]


def test_fixture_packs_downloads_each_named_pack_once() -> None:
    packs = sections(render_qa_brief(QA_FACTS, "claude-cloud"))["Fixture packs"]
    assert _commands(packs) == [
        ["curl", "--fail", "--create-dirs", "-o", f"/tmp/swarm-42/packs/{link.key.removeprefix('packs/')}", link.url]
        for link in QA_FACTS.links.packs
    ]
    assert "before the first scenario that uses it" in packs


def test_lane_without_packs_downloads_nothing() -> None:
    facts = facts_with_fitted_links(scenarios=tuple(replace(s, pack="none") for s in QA_FACTS.scenarios))
    packs = sections(render_qa_brief(facts, "claude-cloud"))["Fixture packs"]
    assert "No scenario names a fixture pack, so there is nothing to download." in packs
    assert "curl" not in packs


@pytest.mark.parametrize("case", ["regression", "base-check"])
def test_base_lanes_test_the_base_commit(case: str) -> None:
    commit = sections(render_qa_brief(QA_CASES[case], "claude-cloud"))["Exact commit"]
    assert f"Test commit `{BASE_COMMIT}`. It is the base commit" in commit
    assert f"git worktree add --detach /tmp/lane-worktree {BASE_COMMIT}" in commit
    assert HEAD not in commit


def test_no_doctor_and_no_recover_fall_back_to_base_url_and_boot() -> None:
    lane = QaLane(boot="bash scripts/qa/boot-lane.sh", base_url="http://127.0.0.1:8080")
    health = sections(render_qa_brief(replace(QA_FACTS, lane_config=lane), "claude-cloud"))["Health check"]
    assert "the health check is loading `http://127.0.0.1:8080` and confirming it answers" in health
    assert "rerun the boot command once" in health


@pytest.mark.parametrize("case", sorted(QA_CASES))
def test_brief_is_vendor_and_project_neutral(case: str) -> None:
    for_one = render_qa_brief(QA_CASES[case], "claude-cloud")
    assert for_one.encode("utf-8") == render_qa_brief(QA_CASES[case], "cursor-cloud").encode("utf-8")
    lowered = for_one.lower()
    assert [name for name in NAMES if name in lowered] == []


def test_unknown_runner_is_refused() -> None:
    with pytest.raises(BriefError):
        render_qa_brief(QA_FACTS, "codex-cloud")


def facts_with_fitted_links(**changes: object) -> LaneFacts:
    facts = replace(QA_FACTS, **changes)
    packs = tuple(download_link(pack) for pack in pack_ids(facts.scenarios))
    return replace(facts, links=replace(facts.links, packs=packs))


def _with_packs(*packs: DownloadLink) -> LaneFacts:
    return replace(QA_FACTS, links=replace(QA_FACTS.links, packs=packs))


def _first_scenario(**changes: object) -> tuple[Scenario, ...]:
    return (replace(QA_FACTS.scenarios[0], **changes), *QA_FACTS.scenarios[1:])


_SAVED = QA_FACTS.scenarios[0].save

BAD_FACTS = {
    "short commit": facts_with_fitted_links(commit=HEAD[:12]),
    "uppercase base commit": facts_with_fitted_links(base_commit=BASE_COMMIT.upper()),
    "http result link": replace(QA_FACTS, links=replace(
        QA_FACTS.links, result=replace(QA_FACTS.links.result, url="http://uploads.example.com/a")
    )),
    "missing pack link": _with_packs(download_link("pricing-visitor")),
    "extra pack link": _with_packs(*QA_FACTS.links.packs, download_link("other")),
    "duplicate pack link": _with_packs(*QA_FACTS.links.packs, download_link("new-visitor")),
    "empty scenarios": facts_with_fitted_links(scenarios=()),
    "bad scenario id": facts_with_fitted_links(scenarios=_first_scenario(id="scenario-1")),
    "bad criterion id": facts_with_fitted_links(scenarios=_first_scenario(criterion="ac-0")),
    "bad save file name": facts_with_fitted_links(scenarios=_first_scenario(save=(*_SAVED, "../escape.png"))),
    "scenario without after screenshot": facts_with_fitted_links(
        scenarios=_first_scenario(save=tuple(name for name in _SAVED if name != "after-screenshot.png"))
    ),
    "scenario without pass test output": facts_with_fitted_links(
        scenarios=_first_scenario(save=tuple(name for name in _SAVED if name != "pass-test-output.txt"))
    ),
    "lane zero": facts_with_fitted_links(lane=0),
    "lane nine": facts_with_fitted_links(lane=9),
    "unknown kind": facts_with_fitted_links(kind="smoke"),
    "bad repo": facts_with_fitted_links(repo="not a repo"),
    "bad run id": facts_with_fitted_links(run_id="run 7; rm -rf /"),
    "bad verify": facts_with_fitted_links(verify="Verify Live"),
    "pr zero": facts_with_fitted_links(pr=0),
}


def test_every_bad_fact_case_differs_from_a_valid_lane_in_one_fact() -> None:
    render_qa_brief(facts_with_fitted_links(), "claude-cloud")


@pytest.mark.parametrize("case", sorted(BAD_FACTS))
def test_bad_fact_is_refused(case: str) -> None:
    with pytest.raises(BriefError):
        render_qa_brief(BAD_FACTS[case], "claude-cloud")
