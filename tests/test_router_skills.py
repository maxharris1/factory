import re
from pathlib import Path

import pytest

from lib.handoff import parse_header_line
from lib.pstack_pin import ROOT, load_pin

FACTORY = Path("skills") / "factory"
BUILDER = FACTORY / "factory-builder" / "SKILL.md"
REVIEWER = FACTORY / "factory-reviewer" / "SKILL.md"
CODEX = FACTORY / "harness" / "codex.md"
CLAUDE_ADAPTER = Path("skills") / "pstack" / "docs" / "harness" / "claude-code.md"

REQUIRED_PATHS = {
    BUILDER: {
        "skills/poteto-mode/SKILL.md",
        "skills/figure-it-out/SKILL.md",
        "skills/poteto-mode/playbooks/opening-a-pr.md",
        "skills/poteto-mode/playbooks/feature.md",
        "skills/poteto-mode/playbooks/bug-fix.md",
        "skills/poteto-mode/playbooks/refactoring.md",
        "skills/poteto-mode/playbooks/autopilot-full.md",
        "skills/principle-prove-it-works/SKILL.md",
        "skills/no-comments/SKILL.md",
        "agents/comment-sicko.md",
        "docs/harness/claude-code.md",
    },
    REVIEWER: {
        "skills/poteto-mode/SKILL.md",
        "skills/interrogate/SKILL.md",
        "skills/blast-radius/SKILL.md",
        "skills/poteto-mode/playbooks/autopilot-full.md",
        "skills/poteto-mode/playbooks/shipping.md",
        "docs/harness/claude-code.md",
    },
    CODEX: {
        "docs/harness/claude-code.md",
        "agents/poteto-agent.md",
        "agents/comment-sicko.md",
    },
}

PSTACK_REF = re.compile(r"<pstack>/([^\s`|]+)")
KIT_REF = re.compile(r"<kit>/([^\s`|]+)")
HEADER = re.compile(r"<!--\s*orch:v1\b.*?-->")
TABLE_HEADER = "| pstack says | factory job does | why |"
QUOTE = re.compile(r'"([^"]+)"')


def read(rel: Path) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def collapse(text: str) -> str:
    return " ".join(text.split())


def pstack_refs(text: str) -> set[str]:
    refs = (ref.rstrip(".,;:)") for ref in PSTACK_REF.findall(text))
    return {ref for ref in refs if "<" not in ref}


def mapping_rows(rel: Path) -> list[list[str]]:
    lines = read(rel).rstrip("\n").splitlines()
    start = max(i for i, line in enumerate(lines) if line.strip() == TABLE_HEADER)
    table = lines[start:]
    assert all(line.startswith("|") for line in table), f"{rel}: text follows the mapping table"
    assert re.fullmatch(r"\|( *-+ *\|){3}", table[1].replace(" ", "")), f"{rel}: bad separator"
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in table[2:]]
    assert rows, f"{rel}: mapping table has no rows"
    return rows


@pytest.mark.parametrize("rel", sorted(REQUIRED_PATHS), ids=str)
def test_every_named_pstack_file_is_in_the_pin(rel: Path) -> None:
    pinned = load_pin(ROOT).hashes()
    refs = pstack_refs(read(rel))
    missing = REQUIRED_PATHS[rel] - refs
    assert not missing, f"{rel} does not name {sorted(missing)}"
    for ref in sorted(refs):
        assert ref in pinned, f"{rel} names <pstack>/{ref}, which is not in skills/skills.lock"
        assert (ROOT / "skills" / "pstack" / ref).is_file(), f"<pstack>/{ref} is not on disk"


@pytest.mark.parametrize("rel", [BUILDER, REVIEWER], ids=str)
def test_skill_frontmatter_and_size(rel: Path) -> None:
    text = read(rel)
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, f"{rel} has no frontmatter"
    assert f"name: {rel.parent.name}" in match.group(1).splitlines()
    assert any(line.startswith("description: ") for line in match.group(1).splitlines())
    assert 40 <= len(text.splitlines()) <= 80, f"{rel} has {len(text.splitlines())} lines"


@pytest.mark.parametrize("rel", [BUILDER, REVIEWER], ids=str)
def test_mapping_table_quotes_the_pinned_line(rel: Path) -> None:
    pinned = load_pin(ROOT).hashes()
    for row in mapping_rows(rel):
        assert len(row) == 3 and all(row), f"{rel}: row needs three cells: {row}"
        refs = pstack_refs(row[0])
        quotes = QUOTE.findall(row[0])
        assert len(refs) == 1, f"{rel}: row must name one <pstack>/ file: {row[0]}"
        assert quotes, f"{rel}: row must quote the pstack line: {row[0]}"
        (ref,) = refs
        assert ref in pinned, f"{rel}: <pstack>/{ref} is not in skills/skills.lock"
        source = collapse(read(Path("skills") / "pstack" / ref))
        for quote in quotes:
            assert collapse(quote) in source, f"{rel}: {quote!r} is not in <pstack>/{ref}"


@pytest.mark.parametrize("rel", sorted(REQUIRED_PATHS), ids=str)
def test_orch_headers_parse(rel: Path) -> None:
    for header in HEADER.findall(read(rel)):
        parse_header_line(header)


def test_skills_show_parseable_handoff_headers() -> None:
    assert HEADER.search(read(BUILDER)), "builder skill shows no orch:v1 header"
    assert HEADER.search(read(REVIEWER)), "reviewer skill shows no orch:v1 header"


def test_codex_adapter_follows_the_claude_adapter_shape() -> None:
    def headings(rel: Path) -> list[str]:
        return [line for line in read(rel).splitlines() if line.startswith("## ")]

    assert headings(CODEX) == headings(CLAUDE_ADAPTER)
    assert read(CODEX).startswith("# pstack in Codex\n")
    assert "Skipped in Codex: <step>. <reason>." in read(CODEX)


ROUTERS_AND_ADAPTER = (BUILDER, REVIEWER, CODEX)

STALE = {
    "plan ticket id": r"\bT\d{1,3}\b",
    "figure-it-out for every job": r"[Aa]lways[^.\n|]*figure-it-out|figure-it-out[^.\n|]*\balways\b|Careful playbook always",
    "no force-push": r"no force-push",
    "model map ticket": r"model map is",
    "helpers on the reviewer's own model": r"\bown model\b",
    "unclear intent sent to BLOCKED": r"intent[^.\n|]*BLOCKED|BLOCKED[^.\n|]*intent",
    "a kit path outside the pinned tree": r"(?<!<kit>/)skills/cursor-team-kit/",
    "a rule a later ticket will write": r"will spell this",
    "a second title shape": r"Title is `#<issue>: type\(scope\): subject`, for example",
}


@pytest.mark.parametrize("rel", ROUTERS_AND_ADAPTER, ids=str)
@pytest.mark.parametrize("name", sorted(STALE))
def test_no_stale_phrase(rel: Path, name: str) -> None:
    found = re.search(STALE[name], read(rel))
    assert not found, f"{rel}: {name}: {found.group(0)!r}"


TITLE = "#<issue>: type(scope): subject"


def other_title_shapes(text: str) -> set[str]:
    return {shape.strip() for shape in re.findall(r"#<issue>:[^`\n|]*", text)} - {TITLE}


def test_builder_gives_the_title_shape() -> None:
    assert f"`{TITLE}`" in read(BUILDER)


@pytest.mark.parametrize("rel", ROUTERS_AND_ADAPTER, ids=str)
def test_one_title_shape(rel: Path) -> None:
    shapes = other_title_shapes(read(rel))
    assert not shapes, f"{rel}: a second title shape: {sorted(shapes)}"


@pytest.mark.parametrize("rel", ROUTERS_AND_ADAPTER, ids=str)
@pytest.mark.parametrize("line", ["The PR title is `#<issue>: subject`.", "Title: #<issue>: fix the bug"])
def test_title_guard_catches_a_second_shape(rel: Path, line: str) -> None:
    assert other_title_shapes(f"{read(rel)}\n{line}\n"), f"{rel}: guard missed {line!r}"


def test_builder_states_the_owner_duties() -> None:
    text = collapse(read(BUILDER))
    for phrase in (
        "Feature step 4",
        "Bug fix step 3",
        "Autopilot-full step 2",
        "`--force-with-lease`",
        "<kit>/deslop/SKILL.md",
        "<pstack>/skills/no-comments/SKILL.md",
        "<pstack>/skills/principle-prove-it-works/SKILL.md",
        "checks for the touched paths",
    ):
        assert phrase in text, f"builder does not say {phrase!r}"


def test_builder_lets_poteto_mode_pick_the_playbook() -> None:
    text = read(BUILDER)
    for playbook in ("feature", "bug-fix", "refactoring"):
        assert f"<pstack>/skills/poteto-mode/playbooks/{playbook}.md" in text
    assert "<pstack>/skills/figure-it-out/SKILL.md" in text


def test_reviewer_is_the_verifier_and_the_brief_owns_the_rules() -> None:
    text = collapse(read(REVIEWER))
    for phrase in ("Autopilot-full step 4", "Shipping step 1", "The brief decides what blocks"):
        assert phrase in text, f"reviewer does not say {phrase!r}"
    for restated in ("## Round 2", "## Same problem twice", "## BLOCKED", "repeat=", "unsat="):
        assert restated not in text, f"reviewer restates the brief's rule: {restated!r}"


@pytest.mark.parametrize("rel", [BUILDER, REVIEWER], ids=str)
def test_routers_defer_models_and_unclear_intent_to_the_brief(rel: Path) -> None:
    text = collapse(read(rel))
    assert "models from the brief's model lines" in text, f"{rel}: helper models"
    assert re.search(r"intent[^.]*`questions` handoff", text), f"{rel}: unclear intent"


@pytest.mark.parametrize("rel", ROUTERS_AND_ADAPTER, ids=str)
def test_every_named_kit_file_is_in_the_pin(rel: Path) -> None:
    pinned = load_pin(ROOT).kit_hashes()
    for ref in sorted({ref.rstrip(".,;:)") for ref in KIT_REF.findall(read(rel))}):
        assert ref in pinned, f"{rel} names <kit>/{ref}, which is not in skills/skills.lock"
        assert (ROOT / "skills" / "cursor-team-kit" / ref).is_file(), f"<kit>/{ref} is not on disk"
