from __future__ import annotations

import json
import re

import pytest

from tests.regenerate_brief_snapshots import CASES, QA_CASES, _job, render_qa_snapshot, render_snapshot

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
    "build": 12028,
    "build-github": 12072,
    "build-pre-ready": 12343,
    "build-qa": 13738,
    "ci-fix": 13444,
    "ci-fix-log-unavailable": 13984,
    "fix": 13600,
    "fix-pre-ready": 12869,
    "merge-only": 10058,
    "plan": 13590,
    "plan-github": 13590,
    "plan-qa": 14380,
    "plan-round-2": 13886,
    "qa:base-check": 7084,
    "qa:base-check-fallback": 6508,
    "qa:head": 7040,
    "qa:head-fallback": 6464,
    "qa:regression": 7075,
    "qa:regression-fallback": 6499,
    "rebase": 11898,
    "resume": 12492,
    "retry": 12830,
    "review": 17450,
    "review-before-ci": 19342,
    "review-evidence": 17454,
    "review-followups": 19692,
    "review-qa-block": 17916,
    "review-qa-report": 17697,
    "review-rebased": 19738,
    "review-round-2-evidence": 19400,
    "review-round-3": 19200,
    "review-update": 18388,
    "review-update-commits": 18912,
    "review-user-impact": 18386,
    "review-user-impact-round-2": 20858,
}

ROUTERLESS_BUILDER_KINDS = frozenset({"merge-only", "rebase"})
BUILDER_ROUTER = "skills/factory/factory-builder/SKILL.md"
REVIEWER_ROUTER = "skills/factory/factory-reviewer/SKILL.md"

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
                 "judgment and prose: opus high", "factory-builder/SKILL.md", "## Additional context"):
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


@pytest.mark.parametrize("kind", sorted(CASES))
def test_brief_names_head_criteria_evidence_and_router(kind: str) -> None:
    head, detail = job_detail(kind)
    brief = without_job_data(render(kind))

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
    if kind in ROUTERLESS_BUILDER_KINDS:
        assert "skills/factory/" not in brief
    else:
        assert brief.count(router) == 2, router


@pytest.mark.parametrize("case", sorted(QA_CASES))
def test_qa_brief_names_commit_criteria_evidence_and_no_router(case: str) -> None:
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
    assert "no skill needs to be installed" in brief
    assert "skills/factory/" not in brief
