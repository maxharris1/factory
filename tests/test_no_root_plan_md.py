"""Plans live in the plan handoff comment, never in a root plan.md (#158)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from lib.brief import PLAN_HANDOFF_LIMIT, render_brief
from lib.queue import Job, JobRequest

ROOT = Path(__file__).resolve().parents[1]
GITHUB_COMMENT_LIMIT = 65_536
PLAN_LINK = "https://github.com/owner/repo/pull/42#issuecomment-7"


def root_plan_problems(root: Path) -> list[str]:
    return [f"{root.name}/plan.md exists"] if (root / "plan.md").exists() else []


def job(action: str, **extra: object) -> Job:
    detail = {
        "action": action,
        "base_branch": "main",
        "brief": "https://github.com/owner/repo/issues/42",
        "gitBranchName": "factory/issue-42",
        "head": None,
        "linear_url": "https://github.com/owner/repo/pull/42",
        "pr_number": 42,
        "reviewer_harness": "codex",
        "risk": "high",
        "round": None if action == "plan" else "1/3",
        "verify_skill": "none",
    }
    detail.update(extra)
    now = time.time()
    return Job(
        id="0123456789abcdef0123456789abcdef",
        dedupe_key="k",
        request=JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action=action,
            recipient="claude",
            source_comment_id="c-9",
            timeout_seconds=3600,
            detail=json.dumps(detail),
        ),
        state="running",
        created_at=now,
        last_activity_at=now,
        finished_at=None,
        handoff_comment_id=None,
    )


def test_the_repo_has_no_root_plan_md() -> None:
    assert root_plan_problems(ROOT) == []


def test_the_check_fails_on_a_fixture_with_a_root_plan_md(tmp_path: Path) -> None:
    assert root_plan_problems(tmp_path) == []
    (tmp_path / "plan.md").write_text("# Plan\n")
    assert root_plan_problems(tmp_path) == [f"{tmp_path.name}/plan.md exists"]


@pytest.mark.parametrize("instruction", [None, "round=2/2; previous plan https://x/1"])
def test_a_plan_brief_never_mentions_plan_md(instruction: str | None) -> None:
    extra = {"instruction": instruction} if instruction else {}
    text = render_brief(job("plan", **extra), bot="claude-bot", context=None)
    assert "plan.md" not in text
    assert "plan handoff" in text


def test_the_plan_handoff_cap_is_stated_with_margin_under_githubs_limit() -> None:
    assert 53_361 < PLAN_HANDOFF_LIMIT <= GITHUB_COMMENT_LIMIT - 4_096
    text = render_brief(job("plan"), bot="claude-bot", context=None)
    assert f"{PLAN_HANDOFF_LIMIT:,} characters" in text


def test_a_build_brief_reads_the_plan_from_the_linked_plan_handoff() -> None:
    text = render_brief(job("build", instruction=PLAN_LINK), bot="claude-bot", context=None)
    assert f"the plan handoff comment at {PLAN_LINK}" in text
    assert "plan.md" not in text


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"instruction": "not a link"},
        {"instruction": "https://github.com/owner/repo/pull/42#issuecomment-8", "resume": True},
    ],
)
def test_a_build_brief_without_a_plan_link_says_there_is_none(extra: dict) -> None:
    text = render_brief(job("build", **extra), bot="claude-bot", context=None)
    assert "No plan handoff comment is linked" in text
    assert not re.search(r"plan handoff comment at ", text)
    assert "plan.md" not in text
