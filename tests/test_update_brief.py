"""#130: the update-from-main job's brief is the merge-only brief."""

from __future__ import annotations

from lib.brief import brief_kind, render_brief
from lib.handoff import parse_header_line
from tests.test_brief import job

HEAD = "3f9c2ab0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"


def update_job():
    return job("update-from-main", head=HEAD, round="2/3", risk="high")


def test_update_from_main_uses_the_merge_only_brief() -> None:
    assert brief_kind(update_job()) == "merge-only"


def test_update_brief_names_base_head_and_one_side_per_hunk() -> None:
    text = render_brief(update_job(), bot="claude-bot", context=None)
    assert "Merge-only job: bring `origin/dev` into the PR branch, nothing else." in text
    assert f"Check out the PR branch at {HEAD}" in text
    assert "`git merge origin/dev`: a merge commit only, no rebase, no force-push." in text
    assert "taking exactly one side" in text
    assert "Any other edit sends the PR back to review." in text


def test_update_brief_ends_with_a_ready_carrying_the_job_round_and_risk() -> None:
    text = render_brief(update_job(), bot="claude-bot", context=None)
    [header] = [line for line in text.splitlines() if line.startswith("<!-- orch:v1 kind=ready")]
    assert header == (
        "<!-- orch:v1 kind=ready from=claude-bot role=builder round=2/3 "
        "head=<pushed-head-sha> risk=high -->"
    )
    fields = parse_header_line(header.replace("<pushed-head-sha>", HEAD))
    assert fields["kind"] == "ready"


def test_a_merge_only_fix_job_has_no_approval_line() -> None:
    text = render_brief(job("fix", head=HEAD, kind="merge-only"), bot="claude-bot", context=None)
    assert "Merge-only job" in text
    assert "taking exactly one side" not in text
