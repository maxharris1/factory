from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.regenerate_brief_snapshots import (
    CASES,
    QA_CASES,
    _job,
    render_devices,
    render_qa_snapshot,
    render_snapshot,
)

DATA_SECTIONS = frozenset({
    "## Job data",
    "## Additional context from factory.md",
    "## Ticket",
    "## Plan",
    "## First plan",
})
SKILL_PATH_LINE = re.compile(r"^- `/\S+`$")
FENCE = re.compile(r"^(`{3,})")
CRITERION_ID = re.compile(r"\bac-\d+\b")

CEILINGS: dict[str, int] = {
    "build": 11317,
    "build-github": 11361,
    "build-pre-ready": 11632,
    "build-qa": 12287,
    "ci-fix": 12823,
    "ci-fix-log-unavailable": 13363,
    "fix": 12237,
    "fix-pre-ready": 12246,
    "merge-only": 10824,
    "plan": 13417,
    "plan-github": 13417,
    "plan-qa": 14207,
    "plan-round-2": 13713,
    "qa:base-check": 7203,
    "qa:base-check-fallback": 6627,
    "qa:head": 7159,
    "qa:head-fallback": 6583,
    "qa:regression": 7194,
    "qa:regression-fallback": 6618,
    "rebase": 12664,
    "resume": 11781,
    "retry": 12119,
    "review": 16074,
    "review-before-ci": 17966,
    "review-evidence": 16078,
    "review-followups": 18315,
    "review-qa-block": 16540,
    "review-qa-report": 16321,
    "review-rebased": 18362,
    "review-round-2-evidence": 18024,
    "review-round-3": 17824,
    "review-update": 17012,
    "review-update-commits": 17536,
    "review-user-impact": 17010,
    "review-user-impact-round-2": 19415,
}

DEVICES = ("box", "remote (mac)")
BUILDER_ROUTER = "skills/factory/factory-builder/SKILL.md"
REVIEWER_ROUTER = "skills/factory/factory-reviewer/SKILL.md"
QA_ROUTER = "skills/factory/factory-qa/SKILL.md"

HANDOFF_PHRASES = (
    "message no other bot",
    "post only this job's handoff",
    "post exactly one handoff comment",
)
HELPER_PHRASES = (
    "helper models, one `<role>: <value>` line per pstack role",
    "you may start helper agents as pstack recommends.",
)
DROPPED_SKILLS_PHRASES = (
    "They win over any other model config",
    "They do research only; you write the verdict.",
    "start a general helper",
)
MOVED_TO_ROUTER = (
    ("write your own check", REVIEWER_ROUTER, "write your own independent check before you read the builder's proof"),
    ("QA owns live checks", REVIEWER_ROUTER, "never `unverified` for lack of a live run"),
    ("`review` (code, tests or logic show it)", REVIEWER_ROUTER, "A `review` criterion is one that code, tests or logic"),
    ("Give each helper a distinct question", REVIEWER_ROUTER, "Give each helper a distinct question"),
    ("never boots or drives the app", REVIEWER_ROUTER, "Do not boot or drive the app."),
    ("Do not boot or drive the app.", REVIEWER_ROUTER, "Do not boot or drive the app."),
    ("A `full` review runs interrogate and blast-radius.", REVIEWER_ROUTER, "In a `full` review, run"),
    ("reruns the gates and does not boot the app", REVIEWER_ROUTER, "reruns the gates and never boots the app"),
)
KEPT_IN_REVIEW_RULES = (
    ("You are the one cross-family reviewer", "One reviewer, from a different model family"),
    ("Do not merge.", "A reviewer never merges."),
)
REPO = Path(__file__).resolve().parents[1]
REVIEW_KINDS = tuple(sorted(kind for kind in CASES if kind.startswith("review")))
NO_ROUTER = frozenset({"merge-only", "rebase"})
ROOT = Path(__file__).resolve().parent.parent
BUILDER_MOVED_TO_ROUTER = (
    ("Write tests first, as the `tdd` skill says", BUILDER_ROUTER, "writes each test first with"),
    ("Never poll, watch or wait on GitHub Actions", BUILDER_ROUTER, "Never babysit, merge or wait on CI"),
    ("Start with an architect scaffold", BUILDER_ROUTER, "A plan job also runs"),
    (
        "Before `ready`: `git fetch origin",
        BUILDER_ROUTER,
        "Rebase on the current base and push the ticket branch with `--force-with-lease`",
    ),
    (
        "A `review` entry gives the command you ran",
        BUILDER_ROUTER,
        "A `review` entry gives the command you ran",
    ),
    (
        "Run the verify skill's doctor before any qa entry",
        BUILDER_ROUTER,
        "Run the verify skill's doctor before any `qa` entry",
    ),
)
OLD_CHANNEL_BULLET = "- Message no other bot. Everything you hand on goes in the one handoff comment.\n"

REGROWTH = (
    "Strip slop with deslop over the diff, run the comments pass with no-comments, "
    "rebase on the current base, run the repo's checks for the touched paths on the "
    "committed head, and prove the change with prove-it-works before you post ready.\n"
)


def boilerplate(brief: str) -> str:
    kept: list[str] = []
    fence: str | None = None
    in_data = False
    for line in brief.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if fence is not None:
            closer = bare.strip()
            if closer.startswith(fence) and set(closer) == {"`"}:
                fence = None
            continue
        opener = FENCE.match(bare.lstrip())
        if opener:
            fence = opener.group(1)
            continue
        if bare.startswith(("# ", "## ", "===== ")):
            in_data = bare in DATA_SECTIONS
        if in_data or SKILL_PATH_LINE.match(bare):
            continue
        kept.append(line)
    assert fence is None, f"unclosed {fence} fence"
    return "".join(kept)


def without_job_data(brief: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in brief.splitlines(keepends=True):
        if line.startswith("## "):
            skipping = line.rstrip("\n") == "## Job data"
        if not skipping:
            kept.append(line)
    return "".join(kept)


def render(kind: str) -> str:
    if kind.startswith("qa:"):
        return render_qa_snapshot(kind.removeprefix("qa:"))
    return render_snapshot(kind)


def job_detail(kind: str) -> tuple[str | None, dict]:
    job = _job(*CASES[kind])
    return job.request.head, json.loads(job.request.detail)


def test_every_rendered_brief_has_a_ceiling() -> None:
    assert set(CEILINGS) == set(CASES) | {f"qa:{case}" for case in QA_CASES}


@pytest.mark.parametrize("kind", sorted(CEILINGS))
def test_static_boilerplate_stays_under_its_ceiling(kind: str) -> None:
    size = len(boilerplate(render(kind)))
    assert size <= CEILINGS[kind], f"{kind} boilerplate grew to {size} chars (ceiling {CEILINGS[kind]})"


def test_boilerplate_drops_data_sections_fences_and_skill_paths() -> None:
    brief = render("build-github")
    static = boilerplate(brief)
    assert "## Method" in static and "## Rules" in static
    for gone in ("## Job data", "## Ticket", "## Plan", "Visitors leave the pricing page",
                 "judgment and prose: opus medium", "factory-builder/SKILL.md", "## Additional context"):
        assert gone in brief and gone not in static, gone


def test_regrown_paragraph_fails_the_ceiling() -> None:
    brief = render("build")
    regrown = brief.replace("## Proof\n", REGROWTH + "\n## Proof\n")
    assert regrown != brief
    assert len(boilerplate(regrown)) > CEILINGS["build"]


def test_unclosed_fence_fails_instead_of_hiding_the_rest() -> None:
    with pytest.raises(AssertionError, match="unclosed"):
        boilerplate("## Method\n```bash\n" + REGROWTH)


def test_paragraph_in_a_data_section_does_not_count() -> None:
    brief = render("build")
    in_data = brief.replace("## Job data\n", "## Job data\n" + REGROWTH)
    assert in_data != brief
    assert len(boilerplate(in_data)) == len(boilerplate(brief))


def check_contract(kind: str, brief: str) -> None:
    head, detail = job_detail(kind)
    brief = without_job_data(brief)

    if head:
        assert head in brief
    else:
        assert "- head: none given" in brief

    criteria = set(CRITERION_ID.findall(json.dumps(detail)))
    if detail.get("brief_text") or detail.get("proof") or (detail.get("evidence") or {}).get("criteria"):
        assert criteria, kind
    for criterion in criteria:
        assert criterion in brief, criterion

    for entry in detail.get("proof", []):
        assert f"- {entry['id']} ({entry['kind']})" in brief, entry
    evidence = detail.get("evidence") or {}
    if ci := evidence.get("ci"):
        for job in ci["jobs"]:
            assert f"`{job['check']}`" in brief, job
            assert job["run_url"] in brief, job
            if job["log_tail"]:
                assert job["log_tail"].splitlines()[-1] in brief, job
    for finding in (evidence.get("findings") or {}).get("findings") or []:
        assert finding["id"] in brief, finding
    if instructions := evidence.get("instructions"):
        assert instructions["prose"] in brief
    if detail.get("plan_text"):
        assert detail["plan_text"].splitlines()[0] in brief
    else:
        for url in re.findall(r"https://\S+", detail.get("instruction", "")):
            assert url in brief, url

    router = REVIEWER_ROUTER if detail["action"] == "review" else BUILDER_ROUTER
    assert router in brief, router


def test_every_case_renders_on_each_device() -> None:
    for kind in CASES:
        assert tuple(render_devices(kind)) == DEVICES, kind


@pytest.mark.parametrize("device", DEVICES)
@pytest.mark.parametrize("kind", sorted(CASES))
def test_brief_names_head_criteria_evidence_and_router(kind: str, device: str) -> None:
    check_contract(kind, render_devices(kind)[device])


@pytest.mark.parametrize(
    ("kind", "dropped"),
    [
        ("resume", "3f9c2ab0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"),
        ("fix", "ac-2"),
        ("ci-fix", "https://github.com/owner/repo/actions/runs/123456"),
        ("resume", "Use the v2 table."),
        ("merge-only", BUILDER_ROUTER),
        ("review", REVIEWER_ROUTER),
    ],
)
def test_one_device_missing_a_fact_fails_even_when_the_other_has_it(kind: str, dropped: str) -> None:
    devices = render_devices(kind)
    box = devices["box"].replace(dropped, "")
    assert box != devices["box"]
    check_contract(kind, devices["remote (mac)"])
    with pytest.raises(AssertionError):
        check_contract(kind, box)


@pytest.mark.parametrize("case", sorted(QA_CASES))
def test_qa_brief_names_commit_criteria_evidence_and_router(case: str) -> None:
    facts = QA_CASES[case]
    brief = render_qa_snapshot(case)
    tested = facts.commit if facts.kind == "head" else facts.base_commit
    assert f"Test commit `{tested}`" in brief
    for scenario in facts.scenarios:
        assert f"- {scenario.id}, for {scenario.criterion}" in brief
        for name in scenario.save:
            assert f"{scenario.id}/{name}" in brief, name
    assert facts.links.result.fields["key"] in brief
    assert facts.links.bundle.fields["key"] in brief
    assert f"router `{QA_ROUTER}`" in brief


def check_stated_once(kind: str, brief: str) -> None:
    lowered = brief.lower()
    for phrase in HANDOFF_PHRASES + HELPER_PHRASES:
        if kind.startswith("qa:") or (kind in NO_ROUTER and phrase in HELPER_PHRASES):
            expected = 0
        else:
            expected = 1
        assert lowered.count(phrase) == expected, (kind, phrase, lowered.count(phrase))
    for phrase in DROPPED_SKILLS_PHRASES:
        assert phrase not in brief, (kind, phrase)


@pytest.mark.parametrize("kind", sorted(CEILINGS))
def test_each_shared_fact_is_stated_once(kind: str) -> None:
    briefs = [render(kind)] if kind.startswith("qa:") else list(render_devices(kind).values())
    for brief in briefs:
        check_stated_once(kind, brief)


def test_the_old_channel_bullet_fails_the_once_check() -> None:
    brief = render_devices("build")["box"]
    post_only = "- Post only this job's handoff"
    restored = brief.replace(post_only, OLD_CHANNEL_BULLET + post_only, 1)
    assert restored != brief
    with pytest.raises(AssertionError):
        check_stated_once("build", restored)


def router_of(kind: str) -> str | None:
    if kind.startswith("qa:"):
        return QA_ROUTER
    if kind.startswith("review"):
        return REVIEWER_ROUTER
    return None if kind in NO_ROUTER else BUILDER_ROUTER


def check_moved_prose(kind: str, brief: str) -> None:
    for phrase, router, _anchor in BUILDER_MOVED_TO_ROUTER:
        if router_of(kind) == router:
            assert phrase not in brief, (kind, phrase)


@pytest.mark.parametrize("phrase,router,anchor", BUILDER_MOVED_TO_ROUTER, ids=[row[0] for row in BUILDER_MOVED_TO_ROUTER])
def test_each_moved_rule_still_lives_in_its_router(phrase: str, router: str, anchor: str) -> None:
    assert anchor in (ROOT / router).read_text(encoding="utf-8"), (router, anchor)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_builder_moved_prose_lives_in_the_router_not_the_brief(kind: str) -> None:
    for brief in render_devices(kind).values():
        check_moved_prose(kind, brief)


def test_a_moved_phrase_put_back_fails_the_moved_prose_check() -> None:
    brief = render_devices("build")["box"]
    regrown = brief.replace("## Proof\n", f"{BUILDER_MOVED_TO_ROUTER[0][0]}.\n\n## Proof\n", 1)
    assert regrown != brief
    with pytest.raises(AssertionError):
        check_moved_prose("build", regrown)


def check_moved_out(brief: str) -> None:
    static = boilerplate(brief)
    for phrase, _, _ in MOVED_TO_ROUTER:
        assert phrase not in static, phrase
    rules = static.split("## Review rules", 1)[1].split("\n## ", 1)[0]
    for phrase, kept in KEPT_IN_REVIEW_RULES:
        assert phrase not in static, phrase
        assert static.count(kept) == 1 and kept in rules, kept


@pytest.mark.parametrize("kind", REVIEW_KINDS)
def test_moved_prose_lives_in_the_router_not_the_brief(kind: str) -> None:
    for brief in render_devices(kind).values():
        check_moved_out(brief)


@pytest.mark.parametrize(("phrase", "router", "rule"), MOVED_TO_ROUTER)
def test_each_moved_rule_is_in_its_router(phrase: str, router: str, rule: str) -> None:
    assert rule in " ".join((REPO / router).read_text(encoding="utf-8").split()), (phrase, rule)


@pytest.mark.parametrize("phrase", [row[0] for row in MOVED_TO_ROUTER + KEPT_IN_REVIEW_RULES])
def test_a_restored_phrase_fails_the_moved_check(phrase: str) -> None:
    brief = render_devices("review")["box"]
    restored = brief.replace("## Review rules", f"{phrase}\n\n## Review rules", 1)
    with pytest.raises(AssertionError):
        check_moved_out(restored)
