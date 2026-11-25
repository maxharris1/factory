from __future__ import annotations

import re
from pathlib import Path

import pytest

import tests.regenerate_brief_snapshots as snapshots
from lib.brief import render_brief
from lib.factory_md import parse_factory_context
from lib.handoff import parse_header_line

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots" / "brief"
OLD_PLACEHOLDER_RULE = "with each `<...>` replaced by one of its choices:"
OLD_NOBODY = "Nobody will message you about this job"
PRINT_MODE = "Print mode stops background tasks"
NOT_RUN = "say which checks did not run"
USER_IMPACT = "review:\n  block_on: user-impact\n"
SAMPLES = {"head": "3f9c2ab", "repeat": "f-1", "unsat": "ac-1", "round": "1"}


def snapshot_texts() -> list[str]:
    return [path.read_text(encoding="utf-8") for path in sorted(SNAPSHOTS.glob("*.txt"))]


def render(case: str, extra: dict[str, object], *, factory_md: str = snapshots.FACTORY_MD, recipient: str | None = None) -> str:
    action, default_recipient, attempt, head, base_extra = snapshots.CASES[case]
    who = recipient or default_recipient
    job = snapshots._job(action, who, attempt, head, {**base_extra, **extra})
    context, findings = parse_factory_context(factory_md)
    assert context is not None, findings
    return render_brief(
        job, bot=f"{who}-bot", context=context, context_ref="dev", factory_root=snapshots.BOX_FACTORY_ROOT
    )


def task(brief: str) -> str:
    return brief.split("## Method\n", 1)[1].split("\n## ", 1)[0]


def header_templates(brief: str) -> list[str]:
    return [line for line in brief.splitlines() if line.startswith("<!-- orch:v1 ") and "<" in line[4:]]


def filled(template: str) -> str:
    def value(match: re.Match[str]) -> str:
        key, placeholder = match.group(1), match.group(2)
        if "|" in placeholder:
            return f"{key}={placeholder.split('|')[0]}"
        return f"{key}={SAMPLES[key]}"

    return re.sub(r"(\w+)=<([^>]*)>", value, template)


@pytest.mark.parametrize("verify", ["verify-cleffy", "none", None])
@pytest.mark.parametrize(
    ("case", "factory_md", "round_text"),
    [
        ("review", snapshots.FACTORY_MD, "1/3"),
        ("review", snapshots.FACTORY_MD, "2/3"),
        ("review", snapshots.FACTORY_MD.replace("review:\n", USER_IMPACT), "2/3"),
        ("review-update", snapshots.FACTORY_MD.replace("review:\n", USER_IMPACT), "2/3"),
    ],
    ids=["round-1", "round-2", "fix-round", "update-round"],
)
def test_every_verify_setting_says_which_checks_did_not_run(
    case: str, factory_md: str, round_text: str, verify: str | None
) -> None:
    brief = render(case, {"round": round_text, "verify_skill": verify}, factory_md=factory_md)
    text = task(brief)
    assert NOT_RUN in text
    assert "verify skill is unknown" not in brief


def test_no_snapshot_keeps_the_old_sentences() -> None:
    for text in snapshot_texts():
        assert OLD_PLACEHOLDER_RULE not in text
        assert OLD_NOBODY not in text
        assert "Nobody will message you while this job runs" in text


def test_the_placeholder_rule_covers_free_values() -> None:
    for text in snapshot_texts():
        if "## Handoff comment" in text and header_templates(text):
            assert "each other `<...>` by its value" in text


def test_a_review_with_no_round_leaves_round_out_of_the_header() -> None:
    brief = render("review", {"round": None})
    [template] = header_templates(brief)
    assert "round=" not in template
    assert "risk=low" in template
    assert "round" not in parse_header_line(filled(template))


def test_every_rendered_header_parses_once_filled() -> None:
    briefs = [*snapshot_texts(), render("review", {"round": None}), render("review", {"round": None, "risk": None})]
    for brief in briefs:
        for template in header_templates(brief):
            parse_header_line(filled(template))


@pytest.mark.parametrize("case", ["build", "fix", "ci-fix", "retry"])
def test_print_mode_is_a_claude_only_sentence(case: str) -> None:
    assert PRINT_MODE in render(case, {})
    for harness in ("codex", "cursor"):
        assert PRINT_MODE not in render(case, {}, recipient=harness)


def test_a_build_bound_to_a_pr_uses_that_prs_branch() -> None:
    bound = task(render("build", {"gitBranchName": None}))
    assert "a new branch" not in bound
    assert "PR #42's branch" in bound
    loose = task(render("build", {"gitBranchName": None, "pr_number": None}))
    assert "a new branch off `dev`" in loose
