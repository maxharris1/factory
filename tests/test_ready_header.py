from __future__ import annotations

import pytest

from lib.brief import render_brief
from lib.handoff import parse_header_line
from lib.handoff_check import expected_for, problems
from tests.test_brief import FACTORY_ROOT, job

HEAD = "3f9c2ab0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"
NEW = f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={HEAD} -->"
OLD = f"<!-- orch:v1 kind=ready from=claude-bot role=builder round=4/4 head={HEAD} risk=high -->"


def _ready_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("<!-- orch:v1 kind=ready")]


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("build", {}),
        ("build", {"round": "1/1", "risk": "low"}),
        ("fix", {"round": "4/4"}),
        ("fix", {"round": "2/3", "kind": "merge-only"}),
        ("update-from-main", {"round": "2/3"}),
    ],
)
def test_the_ready_header_renders_kind_from_role_head_only(action: str, extra: dict) -> None:
    text = render_brief(job(action, **extra), bot="claude-bot", factory_root=FACTORY_ROOT, context=None)
    lines = _ready_lines(text)
    assert lines
    assert set(lines) == {
        "<!-- orch:v1 kind=ready from=claude-bot role=builder head=<pushed-head-sha> -->"
    }
    fields = parse_header_line(lines[0].replace("<pushed-head-sha>", HEAD))
    assert set(fields) == {"kind", "from", "role", "head"}


@pytest.mark.parametrize("header", [NEW, OLD])
def test_old_and_new_ready_headers_parse(header: str) -> None:
    fields = parse_header_line(header)
    assert (fields["kind"], fields["from"], fields["role"], fields["head"]) == (
        "ready",
        "claude-bot",
        "builder",
        HEAD,
    )


@pytest.mark.parametrize("header", [NEW, OLD])
def test_old_and_new_readies_fit_a_build_job_with_a_round(header: str) -> None:
    expected = expected_for(job("build", round="4/4"), "claude-bot")
    answers = {
        "repos/owner/repo/issues/comments/101": {
            "issue_url": "https://api.github.com/repos/owner/repo/issues/42",
            "user": {"login": "maxharris1"},
            "body": header + "\nWhat changed.\n",
        },
        "repos/owner/repo/pulls/42": {"head": {"sha": HEAD, "ref": "factory/issue-42"}},
    }
    found = problems(expected, "101", answers.__getitem__, {"maxharris1": frozenset({"claude-bot"})})
    assert found == []
