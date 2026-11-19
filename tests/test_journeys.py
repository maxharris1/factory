"""QA journeys named at intake (issue #1029, F-21a): the grammar and the map check."""

from __future__ import annotations

import pytest

from lib.journeys import (
    NO_VERIFY,
    Journey,
    QaCriterion,
    map_ids,
    map_problems,
    parse_features,
    problems,
    qa_tag,
    skill_dir,
)


def test_features_parse_plain_and_new_ids() -> None:
    assert parse_features("create-note, search, new:bulk-export") == (
        (Journey("create-note"), Journey("search"), Journey("bulk-export", new=True)),
        (),
    )


@pytest.mark.parametrize("value", ["`create-note` `search`", "create-note,search", "create-note  search,"])
def test_features_split_on_commas_or_spaces_and_drop_backticks(value: str) -> None:
    assert parse_features(value)[0] == (Journey("create-note"), Journey("search"))


@pytest.mark.parametrize("value", [None, "", "none", "None"])
def test_no_features_or_none_names_no_journey(value: str | None) -> None:
    assert parse_features(value) == ((), ())


def test_a_repeated_id_is_one_journey() -> None:
    assert parse_features("search, search")[0] == (Journey("search"),)


@pytest.mark.parametrize("token", ["Create-Note", "-search", "new:", "new:Bad_id", "a" * 65, "x/y"])
def test_an_id_outside_the_grammar_is_bad(token: str) -> None:
    assert parse_features(f"search, {token}") == ((Journey("search"),), (token,))


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("qa: Notes save offline.", QaCriterion("Notes save offline.")),
        ("QA : Notes save offline.", QaCriterion("Notes save offline.")),
        ("(qa) Notes save offline.", QaCriterion("Notes save offline.")),
        ("[qa] Notes save offline.", QaCriterion("Notes save offline.")),
        ("qa(create-note): Notes save.", QaCriterion("Notes save.", ("create-note",))),
        ("qa(create-note, `search`): Both.", QaCriterion("Both.", ("create-note", "search"))),
        ("qa(): Notes save.", QaCriterion("Notes save.")),
    ],
)
def test_a_qa_tag_marks_a_qa_criterion(item: str, expected: QaCriterion) -> None:
    assert qa_tag(item) == expected


@pytest.mark.parametrize("item", ["QA passes on the new screen.", "qualify: x", "The qa: tag is parsed."])
def test_text_without_a_leading_tag_is_not_a_qa_criterion(item: str) -> None:
    assert qa_tag(item) is None


def codes(found) -> list[str]:
    return [code for code, _ in found]


def test_well_formed_journeys_and_criteria_have_no_problems() -> None:
    journeys, bad = parse_features("create-note, new:bulk-export")
    qa = (QaCriterion("x"), QaCriterion("y", ("bulk-export",)))
    assert problems(journeys, bad, qa) == []


def test_a_bad_id_is_features_unknown_naming_the_grammar() -> None:
    [(code, reason)] = problems(*parse_features("Create-Note"), ())
    assert code == "features-unknown"
    assert "Create-Note" in reason and "[a-z0-9]" in reason


def test_a_qa_criterion_bound_to_an_unlisted_id_is_features_unbound() -> None:
    found = problems(*parse_features("search"), (QaCriterion("x", ("search", "export")),))
    assert codes(found) == ["features-unbound"]
    assert "export" in found[0][1] and "Features:" in found[0][1]


def test_qa_criteria_without_a_features_line_is_qa_no_features() -> None:
    found = problems((), (), (QaCriterion("x"), QaCriterion("y")))
    assert codes(found) == ["qa-no-features"]
    assert "Features:" in found[0][1]


def test_features_with_no_qa_criteria_is_fine() -> None:
    assert problems(*parse_features("search"), ()) == []


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        ((".cursor/skills/verify-app/SKILL.md", ".cursor/skills/verify-app/features/search.md"),
         ".cursor/skills/verify-app"),
        (("apps/web/.agents/skills/verify-app/SKILL.md",), "apps/web/.agents/skills/verify-app"),
        ((".cursor/skills/verify-app/SKILL.md", ".claude/skills/verify-app/SKILL.md"),
         ".claude/skills/verify-app"),
        (("a/verify-app/SKILL.md", "a/verify-app-x/verify-app/SKILL.md"), "a/verify-app-x/verify-app"),
        ((".claude/skills/verify-app-old/SKILL.md", ".claude/skills/xverify-app/SKILL.md", "verify-app/SKILL.md"),
         None),
    ],
    ids=["cursor", "nested", "first-sorted", "ls-files-order", "no-match"],
)
def test_the_verify_skill_is_found_where_the_qa_lane_finds_it(paths, expected) -> None:
    assert skill_dir(paths, "verify-app") == expected


def test_map_ids_are_the_markdown_files_named_by_id() -> None:
    names = ["README.md", "create-note.md", "search.md", "notes.txt", "Bad_Name.md", "drafts"]
    assert map_ids(names) == frozenset({"create-note", "search"})


def test_map_ids_match_known_ids_and_refuse_unknown_or_existing_new_ones() -> None:
    journeys, _ = parse_features("create-note, typo, new:bulk-export, new:search")
    found = map_problems(journeys, frozenset({"create-note", "search"}))
    assert codes(found) == ["features-unknown", "features-exists"]
    assert "typo" in found[0][1] and ".claude/skills" not in found[0][1]
    assert "search" in found[1][1]


def test_a_known_id_and_a_new_one_pass_the_map() -> None:
    journeys, _ = parse_features("create-note, new:bulk-export")
    assert map_problems(journeys, frozenset({"create-note"})) == []


def test_no_verify_skill_is_features_no_verify() -> None:
    assert NO_VERIFY[0] == "features-no-verify"
    assert "qa.verify" in NO_VERIFY[1]
