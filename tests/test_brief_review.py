"""The review brief: requirements first, rounds converge, repeat= and unsat= in the header (#186)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import tests.regenerate_brief_snapshots as snapshots
from lib.brief import render_brief
from lib.brief.review import task_lines as review_task_lines
from lib.dispatcher import Comment
from lib.evidence.findings import blocking_ids, findings_evidence
from lib.factory_md import parse_factory_context, parse_factory_md
from lib.followups import followup_findings
from lib.handoff import parse_header_line


def review_brief(round_text: str) -> str:
    action, recipient, attempt, head, extra = snapshots.CASES["review"]
    job = snapshots._job(action, recipient, attempt, head, {**extra, "round": round_text})
    context, findings = parse_factory_context(snapshots.FACTORY_MD)
    assert context is not None, findings
    return render_brief(
        job, bot="codex-bot", context=context, context_ref="dev", factory_root=snapshots.BOX_FACTORY_ROOT
    )


def steps(brief: str) -> list[str]:
    task = brief.split("## What to do\n", 1)[1].split("\n## ", 1)[0]
    return [line for line in task.splitlines() if re.match(r"\d+\. ", line)]


def header(brief: str) -> str:
    return next(line for line in brief.splitlines() if line.startswith("<!-- orch:v1 kind=verdict"))


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_criteria_come_first_with_four_states(round_text: str) -> None:
    first = steps(review_brief(round_text))[0]
    assert first.startswith("1. Requirements first. Before the diff, tag each `ac-<n>` in **Acceptance criteria** above")
    for state in ("`met`", "`unmet`", "`unverified`", "`unsatisfiable`"):
        assert state in first
    assert "evidence" in first


def test_round_one_has_no_earlier_rounds_and_round_three_closes_them_by_id() -> None:
    round_one = "\n".join(steps(review_brief("1/3")))
    round_three = "\n".join(steps(review_brief("3/3")))
    for later_only in ("Close the earlier rounds first", "`git range-diff`", "set `repeat=<id>` in the header"):
        assert later_only not in round_one
        assert later_only in round_three
    assert "is `[minor]` unless it fails a criterion" in round_three


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_every_finding_carries_an_id_and_a_rule(round_text: str) -> None:
    brief = review_brief(round_text)
    assert any("Give each finding an id `f-<n>` and name the rule it breaks" in step for step in steps(brief))
    assert "`1. [blocking] f-<n>. Rule: <the rule it breaks>. <file:line>. <repro>.`" in brief


def item_forms(brief: str) -> dict[str, str]:
    return {form.split()[1]: form for form in re.findall(r"`(1\. \[[a-z-]+\] f-<n>\.[^`]*)`", brief)}


def posted_verdict(round_text: str, items: list[str], cid: str) -> Comment:
    template = header(review_brief(round_text))
    filled = re.sub(r" (repeat|unsat)=<id>", "", template)
    filled = filled.replace("ac=<met|unmet>", "ac=unmet").replace("verdict=<APPROVE|REVISE|BLOCKED>", "verdict=REVISE")
    body = "\n".join([filled, f"**Review round {round_text}: REVISE**", "", *items])
    return Comment(id=cid, author="codex-app[bot]", body=body, created_at=float(cid), url=f"u{cid}")


def test_findings_written_in_the_briefs_form_reach_the_next_rounds_evidence() -> None:
    first = item_forms(review_brief("1/3"))
    assert set(first) == {"[blocking]"}
    blocking = first["[blocking]"]
    raised = [
        blocking.replace("f-<n>", "f-1")
        .replace("<the rule it breaks>", "later rounds keep earlier finding ids")
        .replace("<file:line>", "`lib/evidence/findings.py:37`")
        .replace("<repro>", "Run the dispatcher lifecycle"),
        blocking.replace("[blocking]", "[minor]").replace("f-<n>", "f-2"),
    ]
    trusted = {"github": {"codex-app[bot]": frozenset({"codex-bot"})}}
    round_two = findings_evidence(
        comments=[posted_verdict("1/3", raised, "1")],
        trusted_logins=trusted,
        reviewer="codex-bot",
        current_round=2,
        bases={},
    )
    assert [(f.id, f.rule) for f in round_two.findings] == [("f-1", "later rounds keep earlier finding ids")]

    later = item_forms(review_brief("2/3"))
    assert set(later) == {"[blocking]", "[closed]"}
    closed = later["[closed]"].replace("f-<n>", "f-1").replace("<what shows it closed>", "The test passes")
    round_three = findings_evidence(
        comments=[posted_verdict("1/3", raised, "1"), posted_verdict("2/3", [closed], "2")],
        trusted_logins=trusted,
        reviewer="codex-bot",
        current_round=3,
        bases={},
    )
    assert round_three.findings == ()


def test_follow_up_findings_in_the_briefs_form_are_filed() -> None:
    form = item_forms(review_brief("1/3"))["[blocking]"].replace("[blocking]", "[follow-up]")
    assert followup_findings(form.replace("f-<n>", "f-3")) == (form.replace("f-<n>", "f-3"),)


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_header_template_names_repeat_and_unsat(round_text: str) -> None:
    brief = review_brief(round_text)
    assert header(brief).endswith(" repeat=<id> unsat=<id> -->")
    assert any("sets `unsat=<id>` in the header" in step for step in steps(brief))


def test_the_filled_template_parses() -> None:
    template = header(review_brief("3/3"))
    filled = (
        template.replace("ac=<met|unmet>", "ac=unmet")
        .replace("verdict=<APPROVE|REVISE|BLOCKED>", "verdict=REVISE")
        .replace("repeat=<id>", "repeat=f-2")
        .replace("unsat=<id>", "unsat=ac-3")
    )
    fields = parse_header_line(filled)
    assert (fields["repeat"], fields["unsat"]) == ("f-2", "ac-3")
    dropped = re.sub(r" (repeat|unsat)=<id>", "", template.replace("ac=<met|unmet>", "ac=met")).replace(
        "verdict=<APPROVE|REVISE|BLOCKED>", "verdict=APPROVE"
    )
    assert "repeat" not in parse_header_line(dropped)


def test_merge_conflict_is_not_a_finding() -> None:
    assert "A merge conflict with the base is `mergeable: no` in the verdict, not a finding." in review_brief("1/3")


NO_APP = (
    "That reviewer checks code and logic and never boots or drives the app: `review.verify` is `none`, "
    "whatever verify skill factory.md or Job data names."
)
QA_OWNS = "QA owns live checks, so judge a `qa` criterion from its code, never `unverified` for lack of a live run."
VERIFY_NONE_MD = snapshots.FACTORY_MD.replace("  verify: verify-cleffy\n", "  verify: none\n")
USER_IMPACT = "review:\n  block_on: user-impact\n"


def brief_from(factory_md: str, round_text: str) -> str:
    action, recipient, attempt, head, extra = snapshots.CASES["review"]
    job = snapshots._job(action, recipient, attempt, head, {**extra, "round": round_text})
    context, findings = parse_factory_context(factory_md)
    assert context is not None, findings
    return render_brief(
        job, bot="codex-bot", context=context, context_ref="dev", factory_root=snapshots.BOX_FACTORY_ROOT
    )


def task(brief: str) -> str:
    return brief.split("## What to do\n", 1)[1].split("\n## ", 1)[0]


def merge_update_task(verify: str) -> str:
    facts = {
        "base": "dev",
        "head": "d" * 40,
        "verify": verify,
        "verify_unavailable": False,
        "round_number": 2,
        "followup_from": None,
        "block_on": "user-impact",
        "update": {"last_reviewed_head": "a" * 40, "merge": "b" * 40, "commits": []},
        "rebase": None,
        "start_before_ci": False,
        "no_ci_base": None,
        "local_checks": (),
        "qa_gate": None,
    }
    return "\n".join(review_task_lines(facts))


REVIEW_TASKS = {
    f"{name}-{verify}": make
    for verify, md in (("verify-cleffy", snapshots.FACTORY_MD), ("none", VERIFY_NONE_MD))
    for name, make in {
        "round-one": lambda md=md: task(brief_from(md, "1/3")),
        "later-round": lambda md=md: task(brief_from(md, "3/3")),
        "user-impact-fix-round": lambda md=md: task(brief_from(md.replace("review:\n", USER_IMPACT), "2/3")),
        "user-impact-merge-update": lambda verify=verify: merge_update_task(verify),
    }.items()
}


@pytest.mark.parametrize("case", list(REVIEW_TASKS))
def test_the_reviewer_never_boots_the_app_and_runs_no_verify_skill(case: str) -> None:
    text = REVIEW_TASKS[case]()
    assert QA_OWNS in text
    for gone in ("Run the verify skill", "verify result", "UI and database checks", "`verify-cleffy`"):
        assert gone not in text


def test_the_review_rules_say_the_reviewer_never_boots_the_app() -> None:
    review_rules = review_brief("1/3").split("## Review rules", 1)[1].split("\n## ", 1)[0]
    assert NO_APP in review_rules
    assert "runs the project's verify skill" not in review_rules


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_each_criterion_has_exactly_one_owner_and_only_review_criteria_get_an_independent_check(
    round_text: str,
) -> None:
    listed = steps(review_brief(round_text))
    assert (
        "tag each `ac-<n>` in **Acceptance criteria** above with one owner, `review` (code, tests or logic show "
        "it) or `qa` (only the running app can)" in listed[0]
    )
    independent = [step for step in listed if "write your own check" in step]
    assert len(independent) == 1
    assert (
        "For each `review` criterion, write your own check (test, script or trace) before reading the builder's "
        "proof, then compare. A `qa` criterion gets none."
    ) in independent[0]
    assert QA_OWNS in independent[0]


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_each_helper_gets_a_distinct_question(round_text: str) -> None:
    helpers = [step for step in steps(review_brief(round_text)) if "helper" in step]
    assert len(helpers) == 1
    assert helpers[0].endswith("Give each helper a distinct question: one criterion group or one risk.")


SIZE_LINE = (
    "`Could delete: <code this PR could have deleted or left out, or nothing>. "
    "Size: <lines changed> vs the plan's estimate of <estimate>.`"
)


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_the_verdict_ends_with_one_non_blocking_size_line(round_text: str) -> None:
    brief = review_brief(round_text)
    assert brief.count(SIZE_LINE) == 1
    assert "Leave out ` vs the plan's estimate of <estimate>` when the plan gives no estimate." in brief
    assert "This line never blocks." in brief
    assert "each criterion by `ac-<n>` with its state, owner and evidence" in brief
    assert "`- ac-<n>: <state>. Owner: <review|qa>. <evidence>.`" in brief


@pytest.mark.parametrize(("state", "blocks"), [("unmet", {"ac-2"}), ("met", set())])
def test_a_criterion_line_with_its_owner_still_reads_its_state(state: str, blocks: set[str]) -> None:
    form = re.search(r"`(- ac-<n>: [^`]*)`", review_brief("1/3"))
    assert form is not None
    line = (
        form.group(1)
        .replace("<n>", "2")
        .replace("<state>", state)
        .replace("<review|qa>", "review")
        .replace("<evidence>", "tests/test_cli.py fails")
    )
    assert blocking_ids(line + "\n") == blocks


def test_the_size_line_is_not_read_as_a_finding() -> None:
    blocking = item_forms(review_brief("1/3"))["[blocking]"]
    raised = (
        blocking.replace("f-<n>", "f-1")
        .replace("<the rule it breaks>", "keep one owner per criterion")
        .replace("<file:line>", "`lib/brief/review.py:1`")
        .replace("<repro>", "Render the brief")
    )
    size = "Could delete: nothing. Size: 120 lines changed vs the plan's estimate of 100."
    evidence = findings_evidence(
        comments=[posted_verdict("1/3", [raised, "", size], "1")],
        trusted_logins={"github": {"codex-app[bot]": frozenset({"codex-bot"})}},
        reviewer="codex-bot",
        current_round=2,
        bases={},
    )
    assert [(f.id, f.rule) for f in evidence.findings] == [("f-1", "keep one owner per criterion")]


SKILL = Path(__file__).resolve().parents[1] / "skills/factory/factory-reviewer/SKILL.md"


def test_the_reviewer_skill_says_no_app_one_owner_and_distinct_helpers() -> None:
    text = " ".join(SKILL.read_text().split())
    assert "Do not boot or drive the app. `review.verify` is `none` for the reviewer" in text
    assert "Tag each criterion `review` or `qa`" in text
    assert "before you read the builder's proof" in text
    assert "Give each helper a distinct question: one criterion group or one risk." in text
    assert "Run the verify skill" not in text


def with_verify_unavailable(value: str) -> str:
    return snapshots.FACTORY_MD.replace(
        "  verify: verify-cleffy\n", f"  verify: verify-cleffy\n  verify_unavailable: {value}\n"
    )


@pytest.mark.parametrize("value", [True, False])
def test_verify_unavailable_parses_a_boolean(value: bool) -> None:
    config, findings = parse_factory_md(with_verify_unavailable(str(value).lower()))
    assert findings == []
    assert config is not None and config.verify_unavailable is value


def test_verify_unavailable_is_off_when_absent() -> None:
    config, findings = parse_factory_md(snapshots.FACTORY_MD)
    assert findings == []
    assert config is not None and config.verify_unavailable is False


@pytest.mark.parametrize("value", ["maybe", "1", '"true"'])
def test_verify_unavailable_refuses_anything_but_a_boolean(value: str) -> None:
    message = "review.verify_unavailable must be true or false"
    config, findings = parse_factory_md(with_verify_unavailable(value))
    assert config is None
    assert [(f.severity, f.message) for f in findings] == [("error", message)]
    lenient, lenient_findings = parse_factory_md(with_verify_unavailable(value), strict=False)
    assert lenient is not None and lenient.verify_unavailable is False
    assert [(f.severity, f.message) for f in lenient_findings] == [("warning", message)]

LIVE_PROVIDER = (
    "A criterion needing a live provider without credentials here is `unverified`, not `verdict=BLOCKED`: "
    "name what is missing and the builder's live-run evidence or a human check."
)


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_a_live_provider_criterion_without_credentials_is_unverified_not_blocked(round_text: str) -> None:
    first = steps(review_brief(round_text))[0]
    assert LIVE_PROVIDER in first
    assert "Never read, request or look for credentials." in first
    assert "Count `unverified` and `unsatisfiable` as unmet for `ac`." in first


def test_the_new_routing_names_no_project() -> None:
    texts = [steps(review_brief("1/3"))[0], *(make() for make in REVIEW_TASKS.values())]
    for text in texts:
        assert "aut0" not in text.lower()


@pytest.mark.parametrize("kind", ["review", "build"])
def test_a_review_and_a_build_brief_require_labelled_containers(kind: str) -> None:
    for brief in snapshots.render_snapshot(kind).split("=====")[2::2]:
        assert "`factory.job=$ORCH_JOB_ID`" in brief and "EXIT trap" in brief


def test_the_review_rules_point_the_throwaway_copy_at_the_container_rule() -> None:
    brief = review_brief("1/3")
    review_rules = brief.split("## Review rules", 1)[1]
    throwaway = [line for line in review_rules.splitlines() if "throwaway copy" in line]
    assert len(throwaway) == 1 and "its containers labelled as the Rules say" in throwaway[0]


def test_the_review_task_still_forbids_merging_and_pushing() -> None:
    assert "Do not merge. Do not push to the PR." in review_brief("1/3")
