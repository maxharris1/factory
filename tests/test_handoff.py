import re
from pathlib import Path

import pytest

from lib.handoff import (
    KEYS,
    KINDS,
    REQUIRED_BY_KIND,
    HandoffError,
    format_comment,
    format_header,
    labels_for,
    parse_comment,
    parse_header_line,
)
from lib.schema import AUT0_REPO

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "comments"


def test_round_trip_every_kind() -> None:
    seen: set[str] = set()
    for path in sorted(FIXTURES.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        parsed = parse_comment(text)
        assert format_comment(parsed) == text
        assert format_comment(parse_comment(format_comment(parsed))) == text
        seen.add(parsed.as_dict()["kind"])
    assert seen == set(KINDS)


def test_kinds_and_example_keys_match_spec_section_5() -> None:
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    kind_line = next(line for line in spec.splitlines() if line.startswith("Comment kinds:"))
    assert tuple(re.findall(r"`([a-z]+)`", kind_line)) == KINDS
    key_line = next(line for line in spec.splitlines() if "header keys in the example" in line)
    example_keys = tuple(re.findall(r"`([a-z]+)`", key_line))
    assert example_keys == KEYS[:8]
    assert KEYS[8:] == ("state", "builder")
    assert set(REQUIRED_BY_KIND) == set(KINDS)


def test_spec_verdict_header_round_trips() -> None:
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    match = re.search(r"<!-- orch:v1 kind=verdict.*?-->", spec)
    assert match is not None
    header = match.group(0)
    assert format_header(parse_header_line(header)) == header


def test_writer_normalizes_key_order() -> None:
    header = format_header(
        {
            "verdict": "REVISE",
            "kind": "verdict",
            "head": "3f9c2ab",
            "from": "codex-bot",
        }
    )
    assert header == (
        "<!-- orch:v1 kind=verdict from=codex-bot head=3f9c2ab verdict=REVISE -->"
    )
    assert parse_header_line(header)["kind"] == "verdict"


def test_github_issue_repo_gets_the_section_5_labels() -> None:
    handoff = parse_comment((FIXTURES / "github-verdict.txt").read_text(encoding="utf-8"))
    assert labels_for(handoff, tickets="github", repo="maxharris1/Cleffy") == (
        "orch:state/review",
        "orch:round/2",
        "orch:risk/high",
        "orch:builder/codex",
    )
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    for label in (
        "orch:state/<name>",
        "orch:round/<n>",
        "orch:risk/<low|high>",
        "orch:builder/<harness>",
    ):
        assert label in spec


def test_aut0_fixtures_keep_facts_in_the_header_and_write_no_labels() -> None:
    paths = sorted(FIXTURES.glob("aut0-*.txt"))
    assert [path.name for path in paths] == ["aut0-ready.txt", "aut0-verdict.txt"]
    for path in paths:
        handoff = parse_comment(path.read_text(encoding="utf-8"))
        fields = handoff.as_dict()
        for key in ("state", "round", "risk", "builder"):
            assert key in fields
        assert labels_for(handoff, tickets="linear", repo=AUT0_REPO) == ()
        assert labels_for(handoff, tickets="github", repo=AUT0_REPO) == ()
        assert labels_for(handoff, tickets="linear") == ()


def test_label_helper_does_not_perform_label_writes() -> None:
    source = (ROOT / "lib" / "handoff.py").read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "urllib" not in source
    assert "api.github.com" not in source


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("<!-- orch:v2 kind=brief -->", "not an orch:v1 header"),
        ("<!-- orch:v1 kind=go -->", "unknown kind 'go'"),
        ("<!-- orch:v1 kind=ready -->", "missing required key 'head'"),
        ("<!-- orch:v1 kind=ok -->", "missing required key 'head'"),
        (
            "<!-- orch:v1 kind=verdict verdict=APPROVE -->",
            "missing required key 'head'",
        ),
        ("<!-- orch:v1 kind=ok sha=3f9c2ab -->", "unknown key 'sha'"),
        ("<!-- orch:v1 kind=decline ac=partial -->", "bad ac 'partial'"),
        ("<!-- orch:v1 -->", "missing required key 'kind'"),
    ],
)
def test_parser_rejects_bad_headers(line: str, message: str) -> None:
    with pytest.raises(HandoffError, match=re.escape(message)):
        parse_header_line(line)


def test_decline_without_a_head_parses() -> None:
    parsed = parse_header_line("<!-- orch:v1 kind=decline from=tracy-site -->")
    assert parsed == {"kind": "decline", "from": "tracy-site"}
