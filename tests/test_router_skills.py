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
        "skills/principle-prove-it-works/SKILL.md",
        "skills/no-comments/SKILL.md",
        "agents/comment-sicko.md",
        "docs/harness/claude-code.md",
    },
    REVIEWER: {
        "skills/poteto-mode/SKILL.md",
        "skills/interrogate/SKILL.md",
        "skills/blast-radius/SKILL.md",
        "docs/harness/claude-code.md",
    },
    CODEX: {
        "docs/harness/claude-code.md",
        "agents/poteto-agent.md",
        "agents/comment-sicko.md",
    },
}

PSTACK_REF = re.compile(r"<pstack>/([^\s`|]+)")
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
