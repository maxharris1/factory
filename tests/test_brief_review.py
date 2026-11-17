"""The review brief: requirements first, rounds converge, repeat= and unsat= in the header (#186)."""

from __future__ import annotations

import re

import pytest

import tests.regenerate_brief_snapshots as snapshots
from lib.brief import render_brief
from lib.brief.review import task_lines as review_task_lines
from lib.dispatcher import Comment
from lib.evidence.findings import findings_evidence
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
    assert first.startswith("1. Requirements first. Before the diff, mark each `ac-<n>` in **Acceptance criteria** above")
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
    assert any("set `unsat=<id>` in the header, for example `unsat=ac-3`" in step for step in steps(brief))


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


def test_merge_conflict_and_unknown_verify() -> None:
    brief = review_brief("1/3")
    assert "A merge conflict with the base is `mergeable: no` in the verdict, not a finding." in brief
    assert "A verify result you cannot read (unknown) is `verdict=BLOCKED`." in brief


UNREADABLE_BLOCKS = "A verify result you cannot read (unknown) is `verdict=BLOCKED`."
NOT_YET_REVIEW = (
    "The reviewer runs `verify-cleffy` once that skill is on `dev`. Until then, list it in the PR as not run."
)
PROSE_ONLY_MD = snapshots.FACTORY_MD.replace("The reviewer runs `verify-cleffy`.", NOT_YET_REVIEW)
UNAVAILABLE = "  verify: verify-cleffy\n  verify_unavailable: true\n"
NOT_YET_MD = PROSE_ONLY_MD.replace("  verify: verify-cleffy\n", UNAVAILABLE)
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


def merge_update_task(verify_unavailable: bool) -> str:
    facts = {
        "base": "dev",
        "head": "d" * 40,
        "verify": "verify-cleffy",
        "verify_unavailable": verify_unavailable,
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


NOT_YET_TASKS = {
    "round-one": lambda: task(brief_from(NOT_YET_MD, "1/3")),
    "later-round": lambda: task(brief_from(NOT_YET_MD, "3/3")),
    "user-impact-fix-round": lambda: task(brief_from(NOT_YET_MD.replace("review:\n", USER_IMPACT), "2/3")),
    "user-impact-merge-update": lambda: merge_update_task(True),
}


@pytest.mark.parametrize("case", list(NOT_YET_TASKS))
def test_verify_unavailable_lists_the_verify_skill_as_not_run(case: str) -> None:
    text = NOT_YET_TASKS[case]()
    assert UNREADABLE_BLOCKS not in text
    assert "factory.md sets `review.verify_unavailable`, so `verify-cleffy` may not be available yet." in text
    assert "If `verify-cleffy` is not on the reviewed base, list it in the verdict as not run." in text
    assert "A missing or unreadable result from it is then not `verdict=BLOCKED`." in text
    assert "If it is on the reviewed base, run it. A result you cannot read there is `verdict=BLOCKED`." in text


def test_without_verify_unavailable_an_unreadable_verify_result_is_blocked() -> None:
    assert UNREADABLE_BLOCKS in task(brief_from(snapshots.FACTORY_MD, "1/3"))
    assert UNREADABLE_BLOCKS in task(brief_from(snapshots.FACTORY_MD.replace("review:\n", USER_IMPACT), "2/3"))
    assert UNREADABLE_BLOCKS in merge_update_task(False)


PROSE_ONLY = {
    "original policy": NOT_YET_REVIEW,
    "skill named twice": NOT_YET_REVIEW.replace("list it in", "list `verify-cleffy` in"),
    "one sentence": "The reviewer must run `verify-cleffy`, and list optional browser checks as not run when no "
    "browser is installed.",
    "separate paragraph": "The reviewer must run `verify-cleffy`.\n\nList browser checks as not run.",
}


@pytest.mark.parametrize("shape", list(PROSE_ONLY))
def test_review_prose_that_says_not_run_sets_nothing(shape: str) -> None:
    md = PROSE_ONLY_MD.replace(NOT_YET_REVIEW, PROSE_ONLY[shape])
    text = task(brief_from(md, "1/3"))
    assert UNREADABLE_BLOCKS in text
    assert "review.verify_unavailable" not in text



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
    config, findings = parse_factory_md(PROSE_ONLY_MD)
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
    "A criterion that needs a live provider (a hosted model or cloud service) this sandbox has no credentials "
    "for is `unverified`, not `verdict=BLOCKED`."
)


@pytest.mark.parametrize("round_text", ["1/3", "3/3"])
def test_a_live_provider_criterion_without_credentials_is_unverified_not_blocked(round_text: str) -> None:
    first = steps(review_brief(round_text))[0]
    assert LIVE_PROVIDER in first
    assert "Name the missing environment." in first
    assert "live-run evidence the builder posted on the PR" in first
    assert "name the human step that would check it" in first
    assert "Never read, request or look for credentials." in first
    assert "An `unverified` criterion is not met. Count it as unmet for `ac`, so it never leads to `APPROVE`." in first


def test_the_new_routing_names_no_project() -> None:
    texts = [steps(review_brief("1/3"))[0], *(make() for make in NOT_YET_TASKS.values())]
    for text in texts:
        assert "aut0" not in text.lower()


def test_verify_unavailable_waives_only_the_skill_factory_md_names_now() -> None:
    moved_on = NOT_YET_MD.replace("  verify: verify-cleffy\n", "  verify: verify-next\n")
    text = task(brief_from(moved_on, "1/3"))
    assert "`verify-cleffy`" in text
    assert UNREADABLE_BLOCKS in text
    assert "review.verify_unavailable" not in text
