"""Render one brief snapshot per job kind into tests/snapshots/brief/<kind>.txt,
and one QA lane brief per lane case into tests/snapshots/qa/<case>.txt.

Run `python tests/regenerate_brief_snapshots.py` after a deliberate brief change.
The snapshots are regenerated, never merged by hand.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.brief import render_brief  # noqa: E402
from lib.brief.qa import LaneFacts, Scenario, render_qa_brief  # noqa: E402
from lib.brief_comment import before_factory_md  # noqa: E402
from lib.dispatcher import Comment  # noqa: E402
from lib.evidence import criteria_evidence  # noqa: E402
from lib.dispatch.types import FailedCheck  # noqa: E402
from lib.evidence.ci import ci_evidence  # noqa: E402
from lib.evidence.criteria import canonical_brief  # noqa: E402
from lib.evidence.findings import findings_evidence  # noqa: E402
from lib.factory_md import QaEvidence, QaLane, parse_factory_context  # noqa: E402
from lib.journeys import Journey, QaCheck, QaPlan  # noqa: E402
from lib.qa_storage import DownloadLink, LaneLinks, UploadLink  # noqa: E402
from lib.queue import Job, JobRequest  # noqa: E402

SNAPSHOTS = ROOT / "tests" / "snapshots" / "brief"
QA_SNAPSHOTS = ROOT / "tests" / "snapshots" / "qa"
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

QA_FACTORY_MD = FACTORY_MD.replace(
    "ship: merge\n",
    """ship: merge
qa:
  enabled: true
  runners: [claude-cloud]
  profile: none
  claude_cloud:
    environment: qa-env
  lane:
    boot: bash boot.sh
    base_url: http://127.0.0.1:8080
""",
)

CI_LOG = "FAILED tests/test_pricing.py::test_start_free - AssertionError\n```\ntraceback\n```"

RUN_URL = "https://github.com/owner/repo/actions/runs/123456"
CI_FAILED = (
    FailedCheck("pytest", run_id="123456", job_id="31000000004", run_url=RUN_URL, cause="code"),
    FailedCheck("check-config", run_id="123456", job_id="31000000005", run_url=RUN_URL, cause="code"),
)
PYTEST_LOG = (
    "".join(f"tests/test_pricing.py::test_case_{n} PASSED\n" for n in range(1, 154))
    + "FAILED tests/test_pricing.py::test_start_free - AssertionError\n"
    + "export GH_TOKEN=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n"
    + "1 failed, 153 passed\n"
)


def ci_bundle(logs: dict[str, str | None]) -> dict[str, object]:
    return {"ci": asdict(ci_evidence(failed=CI_FAILED, logs=logs))}


BRIEF_URL = "https://github.com/owner/repo/pull/42#issuecomment-3"
PLAN_URL = "https://github.com/owner/repo/pull/42#issuecomment-5"
ANSWERS_URL = "https://github.com/owner/repo/pull/42#issuecomment-9"

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
- ac-2: `StartFree` links to `/signup?plan=free`. Proof: `e2e/start-free.spec.ts`."""
PROOF = [
    {"id": "ac-1", "kind": "review", "journeys": []},
    {"id": "ac-2", "kind": "qa", "journeys": ["start-free"]},
]
FIRST_PLAN_TEXT = """## Plan for #41: Add a Start free button to the pricing page

- ac-1: `app/pricing/page.tsx` renders `<StartFree />` above the plan grid. Proof: `tests/pricing.test.tsx`.

Expected size: 60 lines."""

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

TRUSTED = {"github": {"factory-app[bot]": frozenset({"grok-bot"}), "codex-app[bot]": frozenset({"codex-bot"})}}
TICKET_BRIEF = Comment(
    id="1",
    author="factory-app[bot]",
    created_at=1.0,
    url="https://github.com/owner/repo/pull/42#issuecomment-1",
    body="""<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=high -->
**Brief for #42: Start free button**

### Ticket
```
## Why
New visitors cannot try the product without a card.

## Acceptance criteria
- ac-1: The pricing page shows a `Start free` button above the plans.
- ac-2: The button opens sign-up with no card field.
```
""",
)
ROUND_ONE_VERDICT = Comment(
    id="2",
    author="codex-app[bot]",
    created_at=2.0,
    url="https://github.com/owner/repo/pull/42#issuecomment-2",
    body=f"""<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=1/3 head={"a" * 40} ac=unmet verdict=REVISE risk=high -->
**Review round 1 of 3: REVISE**

1. [blocking] f-1. Rule: sign-up never asks for a card on the free plan. `app/signup.tsx:40`. Open /signup?plan=free and see the card field.
2. [blocking] f-2. Rule: every new route has a test. `app/pricing.tsx:12`. No test covers the button.
3. [minor] f-3. Rule: button labels use sentence case. `app/pricing.tsx:14`. Reads `Start Free`.
""",
)


QA_PLAN = QaPlan(
    "verify-cleffy",
    "c0ffee" + "0" * 34,
    (Journey("pricing"), Journey("start-free", new=True)),
    (QaCheck("ac-2", ("pricing", "start-free"), "The button opens the sign-up form with the free plan chosen."),),
)
QA_BRIEF_COMMENT = "\n".join([
    "**Brief for #42: Start free button**",
    "",
    *QA_PLAN.render(),
    "",
    "### Ticket",
    "```",
    "## Acceptance criteria",
    "- ac-1: The pricing page shows a `Start free` button above the plans.",
    "- ac-2: The button opens the sign-up form with the free plan chosen.",
    "```",
    "",
])
QA_BRIEF = Comment(
    id="1",
    author="factory-app[bot]",
    created_at=1.0,
    url="https://github.com/owner/repo/pull/42#issuecomment-1",
    body="<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=high -->\n" + QA_BRIEF_COMMENT,
)
QA_EVIDENCE = {
    "criteria": asdict(criteria_evidence(
        issue_key="#42",
        issue_url="https://github.com/owner/repo/issues/42",
        brief=canonical_brief([QA_BRIEF], TRUSTED),
        issue_body=None,
        pr_body=None,
        ready_starts_review=False,
    )),
    "findings": {"findings": [], "reviewed_head": None, "reviewed_base": None, "verdict_url": None},
    "ci": None,
}


def evidence_bundle(current_round: int) -> dict[str, object]:
    criteria = criteria_evidence(
        issue_key="#42",
        issue_url="https://github.com/owner/repo/issues/42",
        brief=canonical_brief([TICKET_BRIEF], TRUSTED),
        issue_body=None,
        pr_body=None,
        ready_starts_review=False,
    )
    findings = findings_evidence(
        comments=[TICKET_BRIEF, ROUND_ONE_VERDICT],
        trusted_logins=TRUSTED,
        reviewer="codex-bot",
        current_round=current_round,
        bases={1: "e" * 40},
    )
    return {"criteria": asdict(criteria), "findings": asdict(findings), "ci": None}


# kind: (action, recipient, attempt, head, detail extras)
CASES: dict[str, tuple[str, str, int, str | None, dict[str, object]]] = {
    "plan": ("plan", "claude", 1, None, {}),
    "plan-qa": ("plan", "claude", 1, None, {"brief_text": QA_BRIEF_COMMENT}),
    "plan-round-2": ("plan", "claude", 1, None, {
        "plan_round": 2,
        "instruction": "round=2/2; previous plan https://github.com/owner/repo/pull/42#issuecomment-5",
        "plan_text": FIRST_PLAN_TEXT,
        "unmapped": "ac-2",
    }),
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
    "build-qa": ("build", "claude", 1, None, {
        "gitBranchName": "factory/issue-42",
        "proof": PROOF,
        "brief_text": QA_BRIEF_COMMENT,
    }),
    "resume": ("build", "claude", 1, HEAD, {
        "gitBranchName": "factory/issue-42",
        "instruction": ANSWERS_URL,
        "plan_text": PLAN_TEXT,
        "resume": True,
        "evidence": {
            "criteria": None,
            "findings": {"findings": [], "reviewed_head": None, "reviewed_base": None, "verdict_url": None},
            "instructions": {
                "cause": "answers",
                "prose": "Use the v2 table.",
                "url": ANSWERS_URL,
                "prior_instruction": PLAN_URL,
                "plan_attempt": None,
                "retry_of": None,
                "findings": [],
            },
            "ci": None,
        },
    }),
    "retry": ("build", "claude", 2, None, {"gitBranchName": "factory/issue-42"}),
    "fix": ("fix", "claude", 1, HEAD, {
        "instruction": "https://github.com/owner/repo/pull/42#issuecomment-7",
        "round": "2/3",
        "proof": PROOF,
    }),
    "build-pre-ready": ("build", "claude", 1, None, {
        "gitBranchName": "factory/issue-42",
        "instruction": "https://github.com/owner/repo/pull/42#issuecomment-5",
    }),
    "fix-pre-ready": ("fix", "claude", 1, HEAD, {
        "instruction": "https://github.com/owner/repo/pull/42#issuecomment-7",
        "round": "2/3",
    }),
    "ci-fix": ("fix", "claude", 1, HEAD, {
        "instruction": "failed: pytest, check-config",
        "round": "1/3",
        "evidence": ci_bundle({"31000000004": PYTEST_LOG, "31000000005": "check-config: ok\nerror: bad key\n"}),
    }),
    "ci-fix-log-unavailable": ("fix", "claude", 1, HEAD, {
        "instruction": "failed: pytest, check-config",
        "round": "1/3",
        "evidence": ci_bundle({"31000000004": None}),
    }),
    "merge-only": ("fix", "claude", 1, HEAD, {"kind": "merge-only", "round": "1/3"}),
    "review": ("review", "codex", 1, HEAD, {"round": "1/3", "risk": "low"}),
    "review-qa-block": ("review", "codex", 1, HEAD, {"round": "1/3", "risk": "low", "evidence": QA_EVIDENCE}),
    "review-qa-report": ("review", "codex", 1, HEAD, {"round": "1/3", "risk": "low"}),
    "review-round-3": ("review", "codex", 1, HEAD, {"round": "3/3"}),
    "review-evidence": ("review", "codex", 1, HEAD, {"round": "1/3", "evidence": evidence_bundle(1)}),
    "review-round-2-evidence": ("review", "codex", 1, HEAD, {"round": "2/3", "evidence": evidence_bundle(2)}),
    "review-followups": ("review", "codex", 1, HEAD, {"round": "3/3"}),
    "review-before-ci": ("review", "codex", 1, HEAD, {"round": "2/3", "risk": "low"}),
    "review-rebased": ("review", "codex", 1, HEAD, {"round": "2/3", "rebase": {
        "last_reviewed_head": "a" * 40,
        "reviewed_base": "e" * 40,
        "base": "f" * 40,
    }}),
    "review-user-impact": ("review", "codex", 1, HEAD, {"round": "1/3", "evidence": evidence_bundle(1)}),
    "review-user-impact-round-2": ("review", "codex", 1, HEAD, {"round": "2/3", "evidence": evidence_bundle(2)}),
    "review-update": ("review", "codex", 1, HEAD, {"round": "1/3", **UPDATE}),
    "review-update-commits": ("review", "codex", 1, HEAD, {
        "round": "1/3",
        **UPDATE,
        "update_commits": ["c" * 40, "d" * 40],
    }),
}

QA_BLOCK = """qa:
  enabled: true
  gate: {gate}
  runners: [claude-cloud]
  claude_cloud:
    environment: qa-env
  profile: full-stack
  lane:
    boot: bash scripts/qa/boot-lane.sh
    doctor: [bash scripts/qa/doctor.sh]
    base_url: http://127.0.0.1:8080
"""


def with_qa(gate: str) -> str:
    return FACTORY_MD.replace("---\n\n## Review", QA_BLOCK.format(gate=gate) + "---\n\n## Review")


PRE_READY = "review:\n  pre_ready:\n    command: pytest -q tests/fast\n    timeout_seconds: 300\n"

FACTORY_MDS = {
    "plan-qa": QA_FACTORY_MD,
    "build-qa": QA_FACTORY_MD,
    "review-before-ci": FACTORY_MD.replace("  verify: verify-cleffy\n", "  verify: verify-cleffy\n  start_before_ci: true\n"),
    "review-followups": FACTORY_MD.replace("review:\n", "review:\n  followup_from_round: 2\n"),
    "build-pre-ready": FACTORY_MD.replace("review:\n", PRE_READY),
    "fix-pre-ready": FACTORY_MD.replace("review:\n", PRE_READY),
    "review-user-impact": FACTORY_MD.replace("review:\n", "review:\n  block_on: user-impact\n"),
    "review-user-impact-round-2": FACTORY_MD.replace("review:\n", "review:\n  block_on: user-impact\n"),
    "review-qa-block": with_qa("block"),
    "review-qa-report": with_qa("report"),
}

BASE_COMMIT = "9b8a7c6d5e4f3021fedcba9876543210abcdef01"
UPLOADS = "https://uploads.example.com/factory-qa"
LANE_KEY = "qa/app/qa-run-7/lane-1"


def upload_link(name: str) -> UploadLink:
    key = f"{LANE_KEY}/{name}"
    return UploadLink(key, 1024, 300, url=UPLOADS,
                      fields={"key": key, "policy": "eyJmYWtlIjp0cnVlfQ==", "x-amz-signature": "4f2a"})


def download_link(pack: str) -> DownloadLink:
    return DownloadLink(f"packs/{pack}", 300, url=f"{UPLOADS}/packs/{pack}?X-Amz-Signature=4f2a")


QA_FACTS = LaneFacts(
    repo="acme/cleffy",
    pr=42,
    run_id="qa-run-7",
    commit=HEAD,
    base_commit=BASE_COMMIT,
    lane=1,
    kind="head",
    scenarios=(
        Scenario(
            id="s-1",
            criterion="ac-1",
            steps=("Open /pricing.", "Click `Start free`."),
            pass_test="The sign-up form shows with the free plan chosen and no card field.",
            pack="pricing-visitor",
            save=("start-free.png", "signup-form.png", "after-screenshot.png", "pass-test-output.txt"),
        ),
        Scenario(
            id="s-2",
            criterion="ac-2",
            steps=("Sign up with a new address.", "Open the confirmation email and follow its link."),
            pass_test="The account page says the email address is confirmed.",
            pack="new-visitor",
            save=("confirmed.png", "after-screenshot.png", "pass-test-output.txt"),
        ),
    ),
    lane_config=QaLane(
        boot="bash scripts/qa/boot-lane.sh",
        base_url="http://127.0.0.1:8080",
        doctor=("bash scripts/qa/doctor.sh", "curl --fail http://127.0.0.1:8080/health"),
        recover="bash scripts/qa/recover.sh",
    ),
    verify="verify-live",
    evidence=QaEvidence(video="multi-step", trace="on-failure"),
    links=LaneLinks(
        result=upload_link("result.json"),
        bundle=upload_link("bundle.tar.gz"),
        packs=(download_link("pricing-visitor"), download_link("new-visitor")),
    ),
)

QA_CASES: dict[str, LaneFacts] = {
    "head": QA_FACTS,
    "head-fallback": replace(QA_FACTS, verify=None),
    "regression": replace(QA_FACTS, kind="regression", lane=2),
    "regression-fallback": replace(QA_FACTS, kind="regression", lane=2, verify=None),
    "base-check": replace(QA_FACTS, kind="base-check", lane=3),
    "base-check-fallback": replace(QA_FACTS, kind="base-check", lane=3, verify=None),
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


def render_qa_snapshot(case: str) -> str:
    return render_qa_brief(QA_CASES[case], "claude-cloud")


def _write(folder: Path, cases: dict, render) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.txt"):
        if stale.stem not in cases:
            stale.unlink()
    for name in cases:
        (folder / f"{name}.txt").write_text(render(name), encoding="utf-8")


def main() -> None:
    _write(SNAPSHOTS, CASES, render_snapshot)
    _write(QA_SNAPSHOTS, QA_CASES, render_qa_snapshot)


if __name__ == "__main__":
    main()
