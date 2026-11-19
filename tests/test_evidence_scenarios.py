from __future__ import annotations

import pytest

from lib.dispatcher import Comment
from lib.evidence.scenarios import (
    InvalidSection,
    LiveScenarios,
    NotLive,
    Scenario,
    live_scenarios,
    parse_live,
)

PLAN_HEADER = "<!-- orch:v1 kind=plan from=claude-bot role=builder ac=met round=1/2 -->"
READY_HEADER = "<!-- orch:v1 kind=ready from=claude-bot role=builder head=" + "a" * 40 + " -->"

EXAMPLE = """\
## Verify, live
- qa-1 (ac-1): As a practice user, upload the file from pack case-happy-01 on a draft case.
  Pass: the table shows the three lines the pack expects. Saves: after screenshot, video.
- qa-2 (ac-2): As an admin, open the case and accept the proposed values.
  Pass: the case's status reads "pending review". Saves: after screenshot.
- Regression lane: qa-1 on base.
- Not live: ac-3 (unit only). ac-4 (needs a human: sends a real email).
"""

PLAN_BODY = f"""\
{PLAN_HEADER}
## Plan
Estimate: 120
Map ac-1 and ac-2 to the upload form.

{EXAMPLE}
## Risks
- The upload size limit.
"""

ONE_SCENARIO = """\
## Verify, live
- qa-{n} (ac-1): Open the case. Pass: it opens.
"""


def comment(id: str, body: str, created_at: float) -> Comment:
    return Comment(
        id=id,
        author="claude-bot",
        body=body,
        created_at=created_at,
        url=f"https://github.com/o/r/issues/1#issuecomment-{id}",
    )


def opens(n: int) -> Scenario:
    return Scenario(f"qa-{n}", "ac-1", "Open the case.", "it opens.", None, ())


def test_reads_the_example() -> None:
    assert parse_live(PLAN_BODY) == LiveScenarios(
        scenarios=(
            Scenario(
                id="qa-1",
                criterion="ac-1",
                steps="As a practice user, upload the file from pack case-happy-01 on a draft case.",
                passes="the table shows the three lines the pack expects.",
                pack="case-happy-01",
                saves=("after screenshot", "video"),
            ),
            Scenario(
                id="qa-2",
                criterion="ac-2",
                steps="As an admin, open the case and accept the proposed values.",
                passes='the case\'s status reads "pending review".',
                pack=None,
                saves=("after screenshot",),
            ),
        ),
        regression=("qa-1",),
        not_live=(NotLive("ac-3", "unit only"), NotLive("ac-4", "needs a human: sends a real email")),
    )


def test_no_section_reads_as_empty() -> None:
    assert parse_live("## Plan\n- qa-1 (ac-1): Open it. Pass: it opens.\n") == LiveScenarios()
    fenced = "## Plan\n```\n" + EXAMPLE + "```\n"
    assert parse_live(fenced) == LiveScenarios()
    tilde = "~~~~markdown\n~~~\n" + EXAMPLE + "~~~~\n"
    assert parse_live(tilde) == LiveScenarios()


def test_a_heading_with_trailing_spaces_starts_the_section() -> None:
    assert parse_live("## Verify, live   \n- qa-1 (ac-1): Open the case. Pass: it opens.\n") == LiveScenarios(
        scenarios=(opens(1),)
    )


def test_pass_and_saves_may_sit_on_the_first_line_or_later_ones() -> None:
    body = (
        "## Verify, live\n"
        "- qa-1 (ac-1): Open the case. Pass: it opens.\n"
        "- qa-2 (ac-1): Open the case\n"
        "\n"
        "\tfrom pack p1.\n"
        "  Pass: it opens.\n"
        "  Saves: video, , trace\n"
    )
    assert parse_live(body) == LiveScenarios(
        scenarios=(
            opens(1),
            Scenario("qa-2", "ac-1", "Open the case from pack p1.", "it opens.", "p1", ("video", "trace")),
        )
    )


def test_a_repeated_id_names_its_second_line() -> None:
    body = (
        "## Verify, live\n"
        "- qa-1 (ac-1): Open the case. Pass: it opens.\n"
        "- qa-2 (ac-2): Close the case. Pass: it closes.\n"
        "- qa-1 (ac-2): Close it again.\n"
        "  Pass: it stays closed.\n"
    )
    assert parse_live(body) == InvalidSection(
        line=4, text="- qa-1 (ac-2): Close it again.", reason="duplicate id qa-1"
    )


def test_a_scenario_without_pass_names_its_first_line() -> None:
    body = (
        "## Verify, live\n"
        "- qa-1 (ac-1): Open the case. Pass: it opens.\n"
        "- qa-2 (ac-2): Close the case.\n"
        "  Saves: video.\n"
    )
    assert parse_live(body) == InvalidSection(
        line=3, text="- qa-2 (ac-2): Close the case.", reason="qa-2 has no Pass: line"
    )


def test_an_unrecognized_bullet_is_refused_with_its_line() -> None:
    body = "## Verify, live\n- qa-1 (ac-1): Open the case. Pass: it opens.\n- Check the logs too.\n"
    found = parse_live(body)
    assert isinstance(found, InvalidSection)
    assert (found.line, found.text) == (3, "- Check the logs too.")
    assert "not a scenario, regression lane or not-live entry" in found.reason


def test_a_line_that_is_not_an_entry_is_refused_with_its_line() -> None:
    entry = "- qa-1 (ac-1): Open the case. Pass: it opens."
    for stray in ("Some prose.", "### Details", "```"):
        found = parse_live(f"## Verify, live\n{entry}\n{stray}\n")
        assert isinstance(found, InvalidSection), stray
        assert (found.line, found.text) == (3, stray)
    found = parse_live("## Verify, live\n  indented before any entry\n")
    assert isinstance(found, InvalidSection)
    assert (found.line, found.text) == (2, "  indented before any entry")


def test_a_regression_lane_of_none_is_empty() -> None:
    body = "## Verify, live\n- qa-1 (ac-1): Open the case. Pass: it opens.\n- Regression lane: none.\n"
    assert parse_live(body) == LiveScenarios(scenarios=(opens(1),), regression=())


def test_plan_wins_over_a_later_ready() -> None:
    plan = comment("10", PLAN_BODY, 1.0)
    ready = comment("11", READY_HEADER + "\n" + ONE_SCENARIO.format(n=9), 2.0)
    found = live_scenarios([ready, plan])
    assert isinstance(found, LiveScenarios)
    assert [scenario.id for scenario in found.scenarios] == ["qa-1", "qa-2"]
    assert found.source_url == plan.url


def test_the_latest_plan_counts() -> None:
    older = comment("9", PLAN_HEADER + "\n" + ONE_SCENARIO.format(n=1), 1.0)
    newer = comment("10", PLAN_HEADER + "\n" + ONE_SCENARIO.format(n=2), 1.0)
    assert live_scenarios([newer, older]) == LiveScenarios(scenarios=(opens(2),), source_url=newer.url)


def test_ready_counts_when_no_plan_has_a_section() -> None:
    ready = comment("11", READY_HEADER + "\n" + ONE_SCENARIO.format(n=3), 2.0)
    expected = LiveScenarios(scenarios=(opens(3),), source_url=ready.url)
    assert live_scenarios([ready]) == expected
    bare_plan = comment("10", PLAN_HEADER + "\n## Plan\nMap ac-1.\n", 1.0)
    assert live_scenarios([bare_plan, ready]) == expected


def test_an_invalid_plan_section_is_returned_not_skipped() -> None:
    plan = comment("10", PLAN_HEADER + "\n## Verify, live\n- qa-1 (ac-1): Open the case.\n", 1.0)
    ready = comment("11", READY_HEADER + "\n" + ONE_SCENARIO.format(n=3), 2.0)
    assert live_scenarios([plan, ready]) == InvalidSection(
        line=3, text="- qa-1 (ac-1): Open the case.", reason="qa-1 has no Pass: line", source_url=plan.url
    )


def test_comments_without_a_header_or_section_give_nothing() -> None:
    assert live_scenarios([comment("1", EXAMPLE, 1.0)]) == LiveScenarios()
    assert live_scenarios([comment("2", PLAN_HEADER + "\n## Plan\n", 1.0)]) == LiveScenarios()
    assert live_scenarios([]) == LiveScenarios()


def test_a_not_live_reason_keeps_its_nested_parentheses() -> None:
    body = "## Verify, live\n- Not live: ac-4 (needs a human (approval) sends a real email).\n"
    assert parse_live(body) == LiveScenarios(not_live=(NotLive("ac-4", "needs a human (approval) sends a real email"),))


def test_a_not_live_entry_may_continue_on_an_indented_line() -> None:
    body = "## Verify, live\n- Not live: ac-3 (unit only),\n  ac-4 (needs a human; sends a real email);\n"
    assert parse_live(body) == LiveScenarios(
        not_live=(NotLive("ac-3", "unit only"), NotLive("ac-4", "needs a human; sends a real email"))
    )


def test_text_after_the_not_live_pairs_names_the_entry() -> None:
    first = "- Not live: ac-2 (unit only). ac-3 needs a human: sends a real email."
    found = parse_live(f"## Verify, live\n- Regression lane: none.\n{first}\n")
    assert isinstance(found, InvalidSection)
    assert (found.line, found.text) == (3, first)
    assert "ac-3 needs a human: sends a real email." in found.reason


@pytest.mark.parametrize(
    "entry",
    [
        "- Not live: see below. ac-2 (unit only).",
        "- Not live: ac-2 (unit only) and ac-3 (no account).",
        "- Not live: ac-2 (unit only)ac-3 (no account).",
        "- Not live: ac-2 (unit only). ac-4 (needs a human (approval).",
        "- Not live:",
        "- Not live: ac-0 (unit only).",
    ],
)
def test_a_malformed_not_live_entry_is_refused_with_its_line(entry: str) -> None:
    found = parse_live(f"## Verify, live\n{entry}\n")
    assert isinstance(found, InvalidSection)
    assert (found.line, found.text) == (2, entry)
