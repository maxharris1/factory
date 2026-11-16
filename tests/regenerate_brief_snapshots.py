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
from lib.brief_comment import before_factory_md  # noqa: E402
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

BRIEF_URL = "https://github.com/owner/repo/pull/42#issuecomment-3"
PLAN_URL = "https://github.com/owner/repo/pull/42#issuecomment-5"

BRIEF_COMMENT = """**Brief for #41: Add a Start free button to the pricing page** (factory feeder)

- Goal: Add a Start free button to the pricing page
- Ticket: https://github.com/owner/repo/issues/41
- PR: #42 on branch `factory/issue-42` off `dev`
- Risk: high (from the ticket's Risk line)
- Start: planning (the ticket says Plan first: yes)
- Builder: claude; reviewer: codex

### Acceptance criteria
- ac-1: The pricing page shows a Start free button above the plans. Proof: a page test.
- ac-2: The button opens the sign-up form with the free plan chosen. Proof: an end-to-end test.

### Ticket
````
Plan first: yes
Risk: high

## Why
Visitors leave the pricing page without a way to try the product.

## Context
The button reuses the sign-up link:

```tsx
<a href="/signup?plan=free">Start free</a>
```
````

### factory.md (from `dev`)
### Review
The reviewer runs `verify-cleffy`.

### Ship
Squash-merge after APPROVE.

### Context
- Never apply migrations on a hosted Supabase project from an agent session.
"""

PLAN_TEXT = """## Plan for #41: Add a Start free button to the pricing page

- ac-1: `app/pricing/page.tsx` renders `<StartFree />` above the plan grid. Proof: `tests/pricing.test.tsx`.
- ac-2: `StartFree` links to `/signup?plan=free`. Proof: `e2e/start-free.spec.ts`.

Skills read: /home/claude-bot/factory/skills/factory/factory-builder/SKILL.md"""

GITHUB_BRIEF: dict[str, object] = {
    "brief": BRIEF_URL,
    "brief_text": before_factory_md(BRIEF_COMMENT).strip(),
    "brief_url": BRIEF_URL,
}

UPDATE: dict[str, object] = {
    "kind": "update",
    "last_reviewed_head": "a" * 40,
    "update_merge": "b" * 40,
    "update_commits": [],
}

# kind: (action, recipient, attempt, head, detail extras)
CASES: dict[str, tuple[str, str, int, str | None, dict[str, object]]] = {
    "plan": ("plan", "claude", 1, None, {}),
    "plan-round-2": ("plan", "claude", 1, None, {"plan_round": 2}),
    "plan-github": ("plan", "claude", 1, None, {**GITHUB_BRIEF, "gitBranchName": "factory/issue-42"}),
    "build-github": ("build", "claude", 1, None, {
        **GITHUB_BRIEF,
        "gitBranchName": "factory/issue-42",
        "instruction": PLAN_URL,
        "plan_text": PLAN_TEXT,
    }),
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
    "review-followups": ("review", "codex", 1, HEAD, {"round": "3/3"}),
    "review-before-ci": ("review", "codex", 1, HEAD, {"round": "2/3", "risk": "low"}),
    "review-rebased": ("review", "codex", 1, HEAD, {"round": "2/3", "rebase": {
        "last_reviewed_head": "a" * 40,
        "reviewed_base": "e" * 40,
        "base": "f" * 40,
    }}),
    "review-update": ("review", "codex", 1, HEAD, {"round": "1/3", **UPDATE}),
    "review-update-commits": ("review", "codex", 1, HEAD, {
        "round": "1/3",
        **UPDATE,
        "update_commits": ["c" * 40, "d" * 40],
    }),
}

FACTORY_MDS = {
    "review-before-ci": FACTORY_MD.replace("  verify: verify-cleffy\n", "  verify: verify-cleffy\n  start_before_ci: true\n"),
    "review-followups": FACTORY_MD.replace("review:\n", "review:\n  followup_from_round: 2\n"),
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
