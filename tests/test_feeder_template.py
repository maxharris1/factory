"""Feeder intake template (issue #204): criteria, Risk, Plan first, Depends on.

parse_intake is pure. The feeder tests use the fakes from tests/test_feeder.py;
nothing reaches a real tracker.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.feeder import parse_intake
from lib.handoff import parse_comment
from lib.journeys import Journey, QaCriterion
from tests.test_feeder import (
    BASE_SHA,
    FACTORY_MD,
    LINEAR_FACTORY_MD,
    LINEAR_ORCH,
    FakeLinear,
    headers,
    issue,
    linear_issue,
    make_feeder,
    rows,
)

HOME = "owner/app"


def body(*, criteria=("- [ ] It works.",), risk="Risk: low", plan="Plan first: no",
         depends="Depends on: none", extra: str = "") -> str:
    parts = [extra, risk, plan, depends]
    if criteria is not None:
        parts += ["", "## Acceptance criteria", "", *criteria]
    return "\n".join(part for part in parts if part is not None) + "\n"


def codes(text: str, labels=()) -> set[str]:
    return {defect.code for defect in parse_intake(text, labels).defects}


def wakes(tmp_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM jobs WHERE recipient = 'grok' AND action = 'refused'"
        ).fetchall()


def flags(tmp_path: Path) -> set[tuple[str, str, str]]:
    with sqlite3.connect(tmp_path / "orch.db") as connection:
        return set(connection.execute("SELECT repo, ticket_id, reason FROM needs_attention"))


def nothing_written(github) -> bool:
    return not github.branches and not github.pulls and not github.posted and not github.labels


# --- the parser ---------------------------------------------------------------


def test_a_full_template_parses_with_no_defects() -> None:
    parsed = parse_intake(body(criteria=("- [ ] One.", "- [x] Two.", "* Three", "1. Four")), ())
    assert parsed.defects == ()
    assert parsed.criteria == ("One.", "Two.", "Three", "Four")
    assert parsed.risk == "low"
    assert parsed.plan_first is False
    assert parsed.depends_on == "none"


def test_no_criteria_section_or_no_items_is_no_criteria() -> None:
    assert codes(body(criteria=None)) == {"no-criteria"}
    assert codes(body(criteria=())) == {"no-criteria"}
    assert codes(body(criteria=("- [ ]", "- [ ]   "))) == {"no-criteria"}


def test_criteria_stop_at_the_next_heading_of_the_same_level() -> None:
    text = body(criteria=("- [ ] Mine.",)) + "\n## Notes\n\n- not a criterion\n"
    assert parse_intake(text, ()).criteria == ("Mine.",)
    nested = body(criteria=("- [ ] Mine.", "", "### Detail", "", "- also mine"))
    assert parse_intake(nested, ()).criteria == ("Mine.", "also mine")


def test_a_fenced_template_does_not_count() -> None:
    text = "```\n" + body() + "```\n"
    assert codes(text) == {"no-criteria", "no-risk", "no-plan-first", "no-depends-on"}


@pytest.mark.parametrize(
    "criterion",
    [
        "Merges without conflict against every other open PR.",
        "Merges cleanly with the other PRs in the lane.",
        "No conflicts with any open pull request.",
        "PR #157 is merged first.",
        "Pull request #157 is merged first.",
        "Waits for pull requests #3 and #4.",
        "Lands after PR 12.",
        "Lands after https://github.com/owner/app/pull/12 does.",
    ],
)
def test_an_other_prs_criterion_is_refused(criterion: str) -> None:
    parsed = parse_intake(body(criteria=(f"- [ ] {criterion}",)), ())
    assert {defect.code for defect in parsed.defects} == {"other-prs-criterion"}
    assert criterion in parsed.defects[0].reason
    assert "Blocked by" in parsed.defects[0].reason


@pytest.mark.parametrize(
    ("lines", "joined"),
    [
        (("- [ ] No conflicts with", "  any open pull request."), "No conflicts with any open pull request."),
        (("- [ ] No conflicts with", "any open pull request."), "No conflicts with any open pull request."),
        (("1. Lands after", "", "   pull request #12."), "Lands after pull request #12."),
    ],
    ids=["indented", "lazy", "after-blank"],
)
def test_a_wrapped_other_prs_criterion_is_refused(lines: tuple[str, ...], joined: str) -> None:
    parsed = parse_intake(body(criteria=lines), ())
    assert {defect.code for defect in parsed.defects} == {"other-prs-criterion"}
    assert joined in parsed.defects[0].reason


def test_wrapped_items_stay_separate_items() -> None:
    parsed = parse_intake(
        body(criteria=("- [ ] The feeder", "  refuses it.", "", "Notes after the list.",
                       "- [ ] ops: Max restarts", "  the poller.")),
        (),
    )
    assert parsed.criteria == ("The feeder refuses it.",)
    assert parsed.ops_criteria == ("Max restarts the poller.",)


@pytest.mark.parametrize(
    "criterion",
    [
        "Users can manually trigger the retry from the dashboard.",
        "Operators can edit the list by hand.",
        "The dispatcher can restart the stuck job.",
    ],
)
def test_functionality_that_mentions_manual_work_is_a_builder_criterion(criterion: str) -> None:
    parsed = parse_intake(body(criteria=(f"- [ ] {criterion}",)), ())
    assert parsed.defects == ()
    assert parsed.criteria == (criterion,)


@pytest.mark.parametrize(
    "criterion",
    ["Manually rotate the key.", "By hand, clear the stale lock.", "Restart the poller.",
     "Max sets ORCH_LIMIT to 3.", "The operator installs the poller."],
)
def test_an_operator_step_is_an_ops_criterion(criterion: str) -> None:
    parsed = parse_intake(body(criteria=("- [ ] It works.", f"- [ ] {criterion}")), ())
    assert parsed.ops_criteria == (criterion,)


@pytest.mark.parametrize(
    ("criterion", "expected"),
    [
        ("No conflicts with\n  any open pull request.", (0, 1, 0)),
        ("Pull request #157 is merged first.", (0, 1, 0)),
        ("Users can manually trigger the retry from the dashboard.", (1, 0, 1)),
        ("**Pull request** #157 is merged first.", (0, 1, 0)),
        ("PR **#157** is merged first.", (0, 1, 0)),
        ("`PR` #157 is merged first.", (0, 1, 0)),
        ("Lands after <https://github.com/owner/app/pull/157> does.", (0, 1, 0)),
        ("Evaluates `pr*2` correctly when pr is the pressure ratio.", (1, 0, 1)),
        ("Accepts `pr~1` as the parent revision of branch pr.", (1, 0, 1)),
        ("[PR](https://example.com) #157 is merged first.", (0, 1, 0)),
        ("PR [#157](https://example.com) is merged first.", (0, 1, 0)),
    ],
    ids=["wrapped", "spelled-out", "user-manual", "bold-words", "bold-number", "code",
         "autolink", "code-star", "code-tilde", "link-word", "link-number"],
)
def test_the_feeder_classifies_review_repros(tmp_path: Path, criterion: str, expected) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(criteria=("- [ ] " + criterion,)), template=False)]
    report = feeder.feed_once()
    assert (len(report.fed), report.refused, len(github.pulls)) == expected


@pytest.mark.parametrize(
    ("criterion", "definitions"),
    [
        ("[PR][docs] #157 is merged first.", "[docs]: https://example.com"),
        ("PR [#157][docs] is merged first.", "[docs]: https://example.com"),
        ("[PR][] #157 is merged first.", "[PR]: https://example.com"),
        ("[PR] #157 is merged first.", "[pr]: https://example.com"),
        ("Lands after [the fix][fix] does.", "[fix]: https://github.com/owner/app/pull/157"),
        ("Lands after ![diagram][d] does.", "[d]: https://github.com/owner/app/pull/157"),
        ("Lands after <a href=\"https://github.com/owner/app/pull/157\">the fix</a> does.", ""),
    ],
    ids=["full-word", "full-number", "collapsed", "shortcut", "ref-target", "ref-image", "html-anchor"],
)
def test_the_feeder_refuses_other_prs_criteria_that_resolve_through_the_document(
    tmp_path: Path, criterion: str, definitions: str
) -> None:
    feeder, _, github, _ = make_feeder(tmp_path)
    text = body(criteria=("- [ ] " + criterion,)) + f"\n{definitions}\n"
    github.issues[HOME] = [issue(7, body=text, template=False)]
    report = feeder.feed_once()
    assert (len(report.fed), report.refused, len(github.pulls)) == (0, 1, 0)


def test_a_bracketed_word_without_a_definition_is_not_an_other_prs_criterion() -> None:
    assert codes(body(criteria=("- [ ] Shows [draft] next to #120.",)) + "\n[draft]: https://example.com\n") == set()


def test_a_plain_issue_reference_is_not_an_other_prs_criterion() -> None:
    assert codes(body(criteria=("- [ ] Fixes the crash in #120.",))) == set()


@pytest.mark.parametrize(
    "criterion",
    [
        "<b>PR</b> #157 is merged first.",
        "PR \\#157 is merged first.",
        "~~PR~~ #157 is merged first.",
        "__Pull request__ #157 is merged first.",
        "*PR* #157 is merged first.",
        "[**PR** #157](https://example.com) is merged first.",
        "PR&nbsp;#157 is merged first.",
        "Lands after [the fix](https://github.com/owner/app/pull/9) does.",
        "![PR #157](diagram.png) is merged first.",
        "PR <span>#157</span> is merged first.",
    ],
)
def test_a_formatted_other_prs_criterion_is_refused(criterion: str) -> None:
    parsed = parse_intake(body(criteria=(f"- [ ] {criterion}",)), ())
    assert {defect.code for defect in parsed.defects} == {"other-prs-criterion"}
    assert repr(criterion) in parsed.defects[0].reason
    assert parsed.criteria == (criterion,)


@pytest.mark.parametrize(
    "criterion",
    ["Keeps `pr_157` as one name.", "Fixes **#120**.", "Edits `lib/feeder.py` **only**.",
     "Parses `pr*2` and `pr~1` literally.", "Renders `<b>pr</b>2` as code.",
     "Escapes `pr\\_2` as written."],
)
def test_formatting_alone_does_not_make_an_other_prs_criterion(criterion: str) -> None:
    assert codes(body(criteria=(f"- [ ] {criterion}",))) == set()


def test_the_brief_keeps_a_formatted_criterion_as_written(tmp_path: Path) -> None:
    criterion = "Edits `lib/feeder.py` **only**."
    feeder, _, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(criteria=(f"- [ ] {criterion}",)), template=False)]
    feeder.feed_once()
    _, brief = go_and_brief(github)
    assert f"- {criterion}" in brief.split("### Acceptance criteria")[1].split("###")[0]


def test_ops_criteria_are_flagged_apart_and_alone_are_no_criteria() -> None:
    parsed = parse_intake(
        body(criteria=("- [ ] The feeder refuses it.", "- [ ] ops: Max restarts the poller.",
                       "- [ ] Install the poller on the box by hand.")),
        (),
    )
    assert parsed.defects == ()
    assert parsed.criteria == ("The feeder refuses it.",)
    assert parsed.ops_criteria == ("Max restarts the poller.", "Install the poller on the box by hand.")
    assert codes(body(criteria=("- [ ] [ops] Rotate the key manually.",))) == {"no-criteria"}


@pytest.mark.parametrize(
    ("risk", "labels", "expected", "defect"),
    [
        ("Risk: low", (), "low", False),
        ("Risk: High", (), "high", False),
        ("risk: low", ("risk:high",), "high", False),
        ("Risk: high", (), "high", False),
        (None, (), None, True),
        (None, ("risk:high",), "high", True),
        ("Risk: medium", (), None, True),
    ],
)
def test_risk_comes_from_the_template_and_is_never_defaulted_low(
    risk, labels, expected, defect
) -> None:
    parsed = parse_intake(body(risk=risk), labels)
    assert parsed.risk == expected
    assert ("no-risk" in {d.code for d in parsed.defects}) is defect


@pytest.mark.parametrize(
    ("line", "expected"),
    [("Plan first: yes", True), ("Plan first (yes/no): no", False), ("**Plan first:** Yes", True),
     ("Plan first: maybe", None), (None, None)],
)
def test_plan_first_is_required(line, expected) -> None:
    parsed = parse_intake(body(plan=line), ())
    assert parsed.plan_first is expected
    assert ("no-plan-first" in {d.code for d in parsed.defects}) is (expected is None)


def test_depends_on_is_required_and_none_is_a_value() -> None:
    assert codes(body(depends=None)) == {"no-depends-on"}
    assert codes(body(depends="Depends on:")) == {"no-depends-on"}
    assert parse_intake(body(depends="Depends on: #116 landed"), ()).depends_on == "#116 landed"


# --- the feeder: no criteria means not fed, with one wake ---------------------


@pytest.mark.parametrize(
    "criteria",
    [None, (), ("- [ ] ops: Max installs the poller.",)],
    ids=["no-section", "no-items", "only-ops"],
)
def test_no_criteria_is_not_fed_and_wakes_once(tmp_path: Path, criteria) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(criteria=criteria), template=False)]
    for _ in range(3):
        report = feeder.feed_once()
        assert report.fed == ()
        assert report.refused == 1
    assert nothing_written(github)
    row = rows(tmp_path / "orch.db")["7"]
    assert row["state"] == "refused-intake"
    assert "acceptance criteria" in row["waiting_reason"].lower()
    jobs = wakes(tmp_path)
    assert len(jobs) == 1
    detail = json.loads(jobs[0]["detail"])
    assert jobs[0]["ticket_id"] == "7"
    assert detail["project_bot"] == "grok-bot"
    assert detail["defects"] == ["no-criteria"]
    assert detail["link"] == github.issues[HOME][0].url
    assert flags(tmp_path) == {(HOME, "7", "intake-refused")}


def test_a_fixed_ticket_feeds_and_clears_its_flag(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(criteria=None), template=False)]
    feeder.feed_once()
    github.issues[HOME] = [issue(7, body=body(), template=False)]
    report = feeder.feed_once()
    assert len(report.fed) == 1
    assert rows(tmp_path / "orch.db")["7"]["state"] == "fed"
    assert flags(tmp_path) == set()
    assert len(wakes(tmp_path)) == 1


def test_a_refused_ticket_that_loses_its_label_is_forgotten(tmp_path: Path) -> None:
    now = [10_000.0]
    feeder, queue, github, _ = make_feeder(tmp_path, now=now)
    github.issues[HOME] = [issue(7, body=body(criteria=None), template=False)]
    feeder.feed_once()
    github.issues[HOME] = []
    now[0] += 60
    feeder.feed_once()
    assert rows(tmp_path / "orch.db") == {}
    assert flags(tmp_path) == set()


def test_a_new_defect_set_wakes_once_more(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(criteria=None), template=False)]
    feeder.feed_once()
    github.issues[HOME] = [issue(7, body=body(criteria=None, risk=None), template=False)]
    feeder.feed_once()
    feeder.feed_once()
    assert len(wakes(tmp_path)) == 2


def test_a_refused_ticket_takes_no_room(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path, box=1)
    github.issues[HOME] = [
        issue(1, body=body(criteria=None), template=False, created_at=1.0),
        issue(2, body=body(), template=False, created_at=2.0),
    ]
    report = feeder.feed_once()
    assert [ref.ticket_id for ref in report.fed] == ["501"]
    assert rows(tmp_path / "orch.db")["2"]["state"] == "fed"


def test_a_feeding_ticket_is_not_rechecked(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.fail["comment"] = 1
    github.issues[HOME] = [issue(7, body=body(), template=False)]
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["7"]["state"] == "error"
    github.issues[HOME] = [issue(7, body=body(criteria=None), template=False)]
    report = feeder.feed_once()
    assert len(report.fed) == 1
    assert wakes(tmp_path) == []


def test_linear_no_criteria_wake_does_not_mark_a_later_feed_done(tmp_path: Path) -> None:
    linear = FakeLinear()
    feeder, queue, github, _ = make_feeder(
        tmp_path, orch=LINEAR_ORCH, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    linear.issues = [replace(linear_issue("AUT-7"), body=body(criteria=None))]
    feeder.feed_once()
    assert len(wakes(tmp_path)) == 1
    linear.issues = [replace(linear_issue("AUT-7"), body=body())]
    report = feeder.feed_once()
    assert len(report.fed) == 1
    feeder.feed_once()
    assert rows(tmp_path / "orch.db")["AUT-7"]["state"] == "fed"


# --- the feeder: an other-PRs criterion is refused -----------------------------


def test_the_n02_criterion_is_refused_with_no_status_read(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    text = body(
        criteria=("- [ ] Merges without conflict against every other open PR.",),
        extra="Blocked by #3",
    )
    github.issues[HOME] = [issue(9, body=text, template=False)]
    report = feeder.feed_once()
    assert report.fed == () and report.refused == 1
    assert github.status_reads == []
    assert nothing_written(github)
    row = rows(tmp_path / "orch.db")["9"]
    assert row["state"] == "refused-intake"
    assert "every other open PR" in row["waiting_reason"]
    assert json.loads(wakes(tmp_path)[0]["detail"])["defects"] == ["other-prs-criterion"]


# --- the feeder: risk comes from the template ----------------------------------


def go_and_brief(github, number: int = 501) -> tuple[dict[str, str], str]:
    comments = github.posted[(HOME, number)]
    return headers(comments)[1], parse_comment(comments[0].body).body


def test_no_risk_line_is_refused_and_never_goes_low(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(risk=None), template=False)]
    feeder.feed_once()
    assert nothing_written(github)
    assert json.loads(wakes(tmp_path)[0]["detail"])["defects"] == ["no-risk"]


def test_risk_high_line_goes_high(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(risk="Risk: high"), template=False)]
    feeder.feed_once()
    go, brief = go_and_brief(github)
    assert go["risk"] == "high"
    assert "- Risk: high (from the ticket's Risk line)" in brief
    assert rows(tmp_path / "orch.db")["7"]["risk"] == "high"


def test_a_risk_high_label_raises_a_low_line(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [
        issue(7, body=body(risk="Risk: low"), labels=("factory:ready", "risk:high"), template=False)
    ]
    feeder.feed_once()
    go, brief = go_and_brief(github)
    assert go["risk"] == "high"
    assert "- Risk: high (from the ticket's risk:high label)" in brief


def test_risk_low_line_goes_low_and_says_where_from(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(), template=False)]
    feeder.feed_once()
    go, brief = go_and_brief(github)
    assert go["risk"] == "low"
    assert "- Risk: low (from the ticket's Risk line)" in brief


def test_linear_risk_comes_from_the_template_too(tmp_path: Path) -> None:
    linear = FakeLinear()
    feeder, queue, github, _ = make_feeder(
        tmp_path, orch=LINEAR_ORCH, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    linear.issues = [replace(linear_issue("AUT-7"), body=body(risk=None))]
    feeder.feed_once()
    assert linear.posted == {}
    linear.issues = [replace(linear_issue("AUT-7"), body=body(risk="Risk: high"))]
    feeder.feed_once()
    go = headers(linear.posted["uuid-AUT-7"])[1]
    assert go["risk"] == "high"


# --- Plan first, Depends on and the ops step -------------------------------------


def test_plan_first_yes_starts_planning_with_one_file(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [
        issue(7, body=body(plan="Plan first: yes", extra="Change lib/x.py."), template=False)
    ]
    feeder.feed_once()
    go, brief = go_and_brief(github)
    assert go["state"] == "planning"
    assert "Plan first: yes" in brief.split("### Ticket")[0]


def test_missing_plan_first_or_depends_on_is_refused(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [
        issue(7, body=body(plan=None), template=False),
        issue(8, body=body(depends=None), template=False),
    ]
    report = feeder.feed_once()
    assert report.refused == 2 and nothing_written(github)
    defects = {job["ticket_id"]: json.loads(job["detail"])["defects"] for job in wakes(tmp_path)}
    assert defects == {"7": ["no-plan-first"], "8": ["no-depends-on"]}


def test_ops_criteria_get_their_own_brief_section(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [
        issue(7, body=body(criteria=("- [ ] It works.", "- [ ] ops: Max restarts the poller.")),
              template=False)
    ]
    feeder.feed_once()
    _, brief = go_and_brief(github)
    head = brief.split("### Ticket")[0]
    builder = head.split("### Acceptance criteria")[1].split("### Ops step")[0]
    ops = head.split("### Ops step (operator action, not the builder's)")[1]
    assert "It works." in builder and "restarts" not in builder
    assert "Max restarts the poller." in ops



@pytest.mark.parametrize("value", ["feature", "bug-fix", "refactoring", "perf-issue"])
def test_a_known_type_line_parses(value: str) -> None:
    parsed = parse_intake(body(extra=f"Type: {value}"), ())
    assert parsed.defects == ()
    assert parsed.ticket_type == value


def test_no_type_line_is_accepted_with_no_type() -> None:
    parsed = parse_intake(body(), ())
    assert parsed.defects == ()
    assert parsed.ticket_type is None


@pytest.mark.parametrize("value", ["bugfix", "Bug", "fix", ""])
def test_an_unknown_type_is_refused_naming_the_valid_values(value: str) -> None:
    parsed = parse_intake(body(extra=f"Type: {value}"), ())
    (defect,) = parsed.defects
    assert defect.code == "bad-type"
    assert "`feature`, `bug-fix`, `refactoring`, `perf-issue`" in defect.reason


def test_the_brief_carries_the_type_line(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(extra="Type: bug-fix"), template=False)]
    feeder.feed_once()
    _, brief = go_and_brief(github)
    assert "\n- Type: bug-fix\n" in brief


def test_a_brief_with_no_type_line_has_none(tmp_path: Path) -> None:
    feeder, queue, github, _ = make_feeder(tmp_path)
    github.issues[HOME] = [issue(7, body=body(), template=False)]
    feeder.feed_once()
    _, brief = go_and_brief(github)
    assert "- Type:" not in brief


@pytest.mark.parametrize("example", [
    "Schema example:\n\n    type: object\n    properties:\n      name:\n        type: string\n",
    "- A list item.\n\n      Type: object\n",
    "Config example:\n\n    Risk: high\n    Plan first: yes\n    Depends on: #9\n",
])
def test_an_indented_code_example_is_not_ticket_metadata(example: str) -> None:
    parsed = parse_intake(body(extra=example), ())
    assert parsed.defects == ()
    assert (parsed.ticket_type, parsed.risk, parsed.plan_first, parsed.depends_on) == (None, "low", False, "none")


# --- QA journeys from the feature map (#1029) --------------------------------

QA_FACTORY_MD = FACTORY_MD.replace(
    "feeder:\n",
    "qa:\n  enabled: true\n  runners: [claude-cloud]\n  profile: none\n  verify: verify-app\n"
    "  claude_cloud:\n    environment: qa-env\n  lane:\n    boot: bash boot.sh\n"
    "    base_url: http://127.0.0.1:8080\nfeeder:\n",
)
SKILL = ".claude/skills/verify-app"
MAP = (HOME, f"{SKILL}/features")


def journey_ticket(features: str | None, *criteria: str) -> str:
    return body(
        criteria=criteria or ("- [ ] It works.",),
        extra=f"Features: {features}" if features is not None else "",
    )


def map_feeder(tmp_path: Path, factory_text: str = QA_FACTORY_MD, skill: str = SKILL):
    feeder, queue, github, _ = make_feeder(tmp_path, factory_text=factory_text)
    github.trees[HOME] = (("README.md", f"{skill}/SKILL.md", f"{skill}/features/search.md"), False)
    github.dirs[(HOME, f"{skill}/features")] = ("README.md", "create-note.md", "search.md")
    return feeder, github


def test_the_template_carries_journeys_and_qa_criteria() -> None:
    parsed = parse_intake(journey_ticket(
        "create-note, new:bulk-export",
        "- [ ] qa: A note saves offline.",
        "- [ ] qa(bulk-export): Export writes a zip.",
        "- [ ] The API returns 201.",
    ), ())
    assert parsed.defects == ()
    assert parsed.journeys == (Journey("create-note"), Journey("bulk-export", new=True))
    assert parsed.qa_criteria == (
        QaCriterion("A note saves offline."),
        QaCriterion("Export writes a zip.", ("bulk-export",)),
    )
    assert parsed.criteria == (
        "qa: A note saves offline.", "qa(bulk-export): Export writes a zip.", "The API returns 201.",
    )


def test_a_ticket_without_features_has_no_journeys() -> None:
    parsed = parse_intake(body(), ())
    assert (parsed.journeys, parsed.qa_criteria) == ((), ())


@pytest.mark.parametrize(
    ("features", "criterion", "code"),
    [
        (None, "- [ ] qa: A note saves.", "qa-no-features"),
        ("search", "- [ ] qa(create-note): A note saves.", "features-unbound"),
        ("Search_Page", "- [ ] It works.", "features-unknown"),
    ],
)
def test_journey_defects_the_body_shows(features, criterion, code) -> None:
    assert codes(journey_ticket(features, criterion)) == {code}


@pytest.mark.parametrize("tag", ["qa(search):", "(qa)", "[qa]"])
def test_a_qa_criterion_naming_an_operator_action_stays_a_builder_criterion(tag: str) -> None:
    item = f"{tag} Help displays the command the operator runs to reset the index."
    parsed = parse_intake(journey_ticket("search", f"- [ ] {item}"), ())
    assert parsed.defects == ()
    assert parsed.criteria == (item,) and parsed.ops_criteria == ()
    assert [c.text for c in parsed.qa_criteria] == ["Help displays the command the operator runs to reset the index."]


def test_a_qa_binding_on_an_operator_action_criterion_is_checked() -> None:
    assert codes(journey_ticket(
        "search",
        "- [ ] qa(typo): Help displays the command the operator runs to reset the index.",
        "- [ ] It works.",
    )) == {"features-unbound"}


@pytest.mark.parametrize(
    "skill",
    [".claude/skills/verify-app", ".cursor/skills/verify-app", ".agents/skills/verify-app",
     "apps/web/.cursor/skills/verify-app"],
)
def test_the_map_is_read_where_the_verify_skill_lives_at_the_base(tmp_path: Path, skill: str) -> None:
    feeder, github = map_feeder(tmp_path, skill=skill)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("search"))]
    report = feeder.feed_once()
    assert len(report.fed) == 1 and report.refused == 0
    assert github.tree_reads == [(HOME, BASE_SHA, "")]
    assert github.dir_reads == [(HOME, f"{skill}/features", BASE_SHA)]


NESTED = "apps/web/.cursor/skills/verify-app"


def truncated_monorepo(tmp_path: Path):
    feeder, github = map_feeder(tmp_path, skill=NESTED)
    github.trees[HOME] = (("README.md",), True)
    github.dirs[(HOME, "")] = ("README.md", "apps/", "docs/")
    github.trees[(HOME, "apps")] = (("web/package.json",), True)
    github.dirs[(HOME, "apps")] = ("web/",)
    github.trees[(HOME, "apps/web")] = (
        ("package.json", ".cursor/skills/verify-app/SKILL.md", ".cursor/skills/verify-app/features/search.md"),
        False,
    )
    return feeder, github


def test_a_truncated_tree_is_walked_to_a_nested_verify_skill(tmp_path: Path) -> None:
    feeder, github = truncated_monorepo(tmp_path)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("search"))]
    report = feeder.feed_once()
    assert len(report.fed) == 1 and report.refused == 0
    assert github.dir_reads[-1] == (HOME, f"{NESTED}/features", BASE_SHA)


def test_a_walked_map_refuses_a_new_id_it_already_has(tmp_path: Path) -> None:
    feeder, github = truncated_monorepo(tmp_path)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("new:search"))]
    report = feeder.feed_once()
    assert report.fed == () and report.refused == 1
    assert json.loads(wakes(tmp_path)[0]["detail"])["defects"] == ["features-exists"]


def test_a_truncated_tree_finds_a_skill_folder_too_big_to_read_whole(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path, skill=".cursor/skills/verify-app")
    github.trees[HOME] = (("README.md",), True)
    github.dirs[(HOME, "")] = ("README.md", ".cursor/")
    github.trees[(HOME, ".cursor")] = ((), True)
    github.dirs[(HOME, ".cursor")] = ("skills/",)
    github.trees[(HOME, ".cursor/skills")] = ((), True)
    github.dirs[(HOME, ".cursor/skills")] = ("verify-app/",)
    github.trees[(HOME, ".cursor/skills/verify-app")] = ((), True)
    github.dirs[(HOME, ".cursor/skills/verify-app")] = ("SKILL.md", "features/")
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("search"))]
    assert len(feeder.feed_once().fed) == 1


def test_a_skill_the_walk_cannot_reach_leaves_the_ticket_eligible(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path, skill=NESTED)
    github.trees[HOME] = (("README.md",), True)
    github.dirs[(HOME, "")] = tuple(f"pkg{n}/" for n in range(500))
    for n in range(500):
        github.trees[(HOME, f"pkg{n}")] = ((), True)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("search"))]
    report = feeder.feed_once()
    assert report.fed == () and report.refused == 0 and report.errors == 1
    assert nothing_written(github)
    assert rows(tmp_path / "orch.db")["7"]["state"] == "eligible"
    assert wakes(tmp_path) == [] and flags(tmp_path) == set()


def test_a_verify_skill_missing_at_the_base_has_an_empty_map(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path)
    github.trees[HOME] = (("README.md",), False)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("search"))]
    report = feeder.feed_once()
    assert report.fed == () and report.refused == 1
    assert github.dir_reads == []
    assert json.loads(wakes(tmp_path)[0]["detail"])["defects"] == ["features-unknown"]


def test_a_ticket_with_map_ids_is_fed_after_one_map_read_at_the_base(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket(
        "create-note, search", "- [ ] qa(create-note): A note saves offline."))]
    report = feeder.feed_once()
    assert len(report.fed) == 1
    assert github.dir_reads == [(*MAP, BASE_SHA)]
    assert wakes(tmp_path) == []


def test_a_ticket_that_adds_a_journey_is_fed(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket(
        "create-note, new:bulk-export", "- [ ] qa(bulk-export): Export writes a zip."))]
    assert len(feeder.feed_once().fed) == 1


@pytest.mark.parametrize(
    ("features", "code", "named"),
    [("create-note, typo", "features-unknown", "typo"), ("new:search", "features-exists", "search")],
)
def test_a_ticket_the_map_contradicts_is_refused(tmp_path: Path, features, code, named) -> None:
    feeder, github = map_feeder(tmp_path)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket(features))]
    report = feeder.feed_once()
    assert report.fed == () and report.refused == 1
    assert nothing_written(github)
    row = rows(tmp_path / "orch.db")["7"]
    assert row["state"] == "refused-intake"
    assert named in row["waiting_reason"]
    assert json.loads(wakes(tmp_path)[0]["detail"])["defects"] == [code]


def test_features_with_no_verify_skill_is_refused_with_no_read(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path, factory_text=FACTORY_MD)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("search"))]
    feeder.feed_once()
    assert github.dir_reads == []
    assert json.loads(wakes(tmp_path)[0]["detail"])["defects"] == ["features-no-verify"]


@pytest.mark.parametrize("read", ["tree_paths", "dir_names"])
def test_a_failed_map_read_leaves_the_ticket_eligible_until_a_read_works(tmp_path: Path, read: str) -> None:
    feeder, github = map_feeder(tmp_path)
    github.issues[HOME] = [issue(7, template=False, body=journey_ticket("search"))]
    github.fail[read] = 1
    report = feeder.feed_once()
    assert report.fed == () and report.refused == 0 and report.errors == 1
    assert nothing_written(github)
    assert rows(tmp_path / "orch.db")["7"]["state"] == "eligible"
    assert wakes(tmp_path) == [] and flags(tmp_path) == set()
    assert len(feeder.feed_once().fed) == 1


def test_a_ticket_without_features_reads_no_map(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path)
    github.issues[HOME] = [issue(7, template=False, body=body())]
    assert len(feeder.feed_once().fed) == 1
    assert github.dir_reads == []


def test_the_map_is_read_once_per_base_in_a_poll(tmp_path: Path) -> None:
    feeder, github = map_feeder(tmp_path)
    github.issues[HOME] = [
        issue(number, template=False, body=journey_ticket("search")) for number in (7, 8)
    ]
    feeder.feed_once()
    assert len(github.dir_reads) == 1
