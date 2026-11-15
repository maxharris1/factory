"""Render one brief snapshot per job kind into tests/snapshots/brief/<kind>.txt.

Run `python tests/regenerate_brief_snapshots.py` after a deliberate brief change.
The snapshots are regenerated, never merged by hand.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.brief import render_brief  # noqa: E402
from lib.factory_md import parse_factory_context  # noqa: E402
from lib.queue import Job, JobRequest  # noqa: E402

SNAPSHOTS = ROOT / "tests" / "snapshots" / "brief"
HEAD = "3f9c2ab0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"
REMOTE_DEVICE = "mac"
BOX_FACTORY_ROOT = "/srv/orch/factory"

FACTORY_MD = """---
base_branch: dev
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: verify-cleffy
ship: merge
skills:
  - verify-cleffy
---

## Review
The reviewer runs `verify-cleffy`.

## Context
- Never apply migrations on a hosted Supabase project from an agent session.
"""

CI_LOG = "FAILED tests/test_pricing.py::test_start_free - AssertionError\n```\ntraceback\n```"

# kind: (action, recipient, attempt, head, detail extras)
CASES: dict[str, tuple[str, str, int, str | None, dict[str, object]]] = {
    "plan": ("plan", "claude", 1, None, {}),
    "build": ("build", "claude", 1, None, {
        "gitBranchName": "factory/issue-42",
        "instruction": "https://github.com/owner/repo/pull/42#issuecomment-5",
    }),
    "resume": ("build", "claude", 1, HEAD, {
        "gitBranchName": "factory/issue-42",
        "instruction": "https://github.com/owner/repo/pull/42#issuecomment-5",
        "resume": True,
    }),
    "retry": ("build", "claude", 2, None, {"gitBranchName": "factory/issue-42"}),
    "fix": ("fix", "claude", 1, HEAD, {
        "instruction": "https://github.com/owner/repo/pull/42#issuecomment-7",
        "round": "2/3",
    }),
    "ci-fix": ("fix", "claude", 1, HEAD, {"instruction": CI_LOG, "round": "1/3"}),
    "merge-only": ("fix", "claude", 1, HEAD, {"kind": "merge-only", "round": "1/3"}),
    "review": ("review", "codex", 1, HEAD, {"round": "1/3", "risk": "low"}),
    "review-round-3": ("review", "codex", 1, HEAD, {"round": "3/3"}),
    "review-before-ci": ("review", "codex", 1, HEAD, {"round": "2/3", "risk": "low"}),
}

FACTORY_MDS = {
    "review-before-ci": FACTORY_MD.replace("  verify: verify-cleffy\n", "  verify: verify-cleffy\n  start_before_ci: true\n"),
}


def _job(action: str, recipient: str, attempt: int, head: str | None, extra: dict[str, object]) -> Job:
    detail: dict[str, object] = {
        "action": action,
        "base_branch": "dev",
        "brief": "Add a Start free button to the pricing page.",
        "gitBranchName": None,
        "head": None,
        "linear_url": "https://github.com/owner/repo/pull/42",
        "post_to": "PR #42 issue comments (not reviews or inline comments)",
        "pr_number": "42",
        "project_bot": None,
        "repo_skill_paths": [".claude/skills/verify-cleffy/SKILL.md"],
        "reviewer_harness": "codex",
        "risk": "high",
        "round": None,
        "verify_skill": "verify-cleffy",
    }
    detail.update(extra)
    request = JobRequest(
        repo="owner/repo",
        ticket_id="42",
        action=action,
        recipient=recipient,
        source_comment_id="c-9",
        timeout_seconds=3600,
        attempt=attempt,
        head=head,
        detail=json.dumps(detail, separators=(",", ":"), sort_keys=True),
    )
    return Job(
        id="0123456789abcdef0123456789abcdef",
        dedupe_key="k",
        request=request,
        state="running",
        created_at=0.0,
        last_activity_at=0.0,
        finished_at=None,
        handoff_comment_id=None,
    )


def render_snapshot(kind: str) -> str:
    action, recipient, attempt, head, extra = CASES[kind]
    job = _job(action, recipient, attempt, head, extra)
    context, findings = parse_factory_context(FACTORY_MDS.get(kind, FACTORY_MD))
    assert context is not None, findings
    box = render_brief(
        job, bot=f"{recipient}-bot", context=context, context_ref="dev", factory_root=BOX_FACTORY_ROOT
    )
    remote = render_brief(
        job,
        bot=f"{recipient}-bot",
        context=None,
        context_note="no factory.md on dev",
        remote_device=REMOTE_DEVICE,
        factory_root=f"/home/{recipient}-bot/factory",
    )
    return f"===== box =====\n{box}===== remote ({REMOTE_DEVICE}) =====\n{remote}"


def main() -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    for stale in SNAPSHOTS.glob("*.txt"):
        if stale.stem not in CASES:
            stale.unlink()
    for kind in CASES:
        (SNAPSHOTS / f"{kind}.txt").write_text(render_snapshot(kind), encoding="utf-8")


if __name__ == "__main__":
    main()
