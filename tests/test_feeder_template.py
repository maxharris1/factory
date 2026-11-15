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
from tests.test_feeder import (
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
        "Lands after https://github.com/owner/app/pull/12 does.",
    ],
)
def test_an_other_prs_criterion_is_refused(criterion: str) -> None:
    parsed = parse_intake(body(criteria=(f"- [ ] {criterion}",)), ())
    assert {defect.code for defect in parsed.defects} == {"other-prs-criterion"}
    assert criterion in parsed.defects[0].reason
    assert "Blocked by" in parsed.defects[0].reason


def test_a_plain_issue_reference_is_not_an_other_prs_criterion() -> None:
    assert codes(body(criteria=("- [ ] Fixes the crash in #120.",))) == set()


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
