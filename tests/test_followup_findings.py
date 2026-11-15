"""Follow-up findings from review round N (issue #259)."""

from __future__ import annotations

import copy

import pytest
import yaml

from lib.brief import render_brief
from lib.factory_md import parse_factory_context, parse_factory_md
from lib.feeder_gates import github_ref, parse_gate_lines
from lib.followups import followup_findings, followup_issue
from tests import regenerate_brief_snapshots as snapshots

VALID = {
    "base_branch": "main",
    "tickets": "github",
    "builder": {"harness": "claude"},
    "reviewer": {"harness": "codex"},
    "review": {"rounds": {"low": 1, "high": 3}, "verify": "none"},
    "ship": "merge",
    "skills": [],
}
BAD_ROUND = "review.followup_from_round must be an integer of at least 2"


def factory_text(followup: object = None, **top: object) -> str:
    data = copy.deepcopy(VALID)
    data.update(top)
    if followup is not None:
        data["review"]["followup_from_round"] = followup
    return f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n"


def test_followup_from_round_accepts_an_integer_of_at_least_two() -> None:
    config, findings = parse_factory_md(factory_text(4))
    assert findings == []
    assert config is not None and config.followup_from_round == 4

    absent, absent_findings = parse_factory_md(factory_text())
    assert absent_findings == []
    assert absent is not None and absent.followup_from_round is None


@pytest.mark.parametrize("value", [1, 0, "4", True])
def test_followup_from_round_refuses_anything_else(value: object) -> None:
    config, findings = parse_factory_md(factory_text(value))
    assert config is None
    assert [(f.severity, f.message) for f in findings] == [("error", BAD_ROUND)]

    lenient, lenient_findings = parse_factory_md(factory_text(value), strict=False)
    assert lenient is not None and lenient.followup_from_round is None
    assert [(f.severity, f.message) for f in lenient_findings] == [("warning", BAD_ROUND)]


def test_followup_from_round_is_refused_with_linear_tickets() -> None:
    config, findings = parse_factory_md(factory_text(4, tickets="linear", ship="handoff"))
    assert config is None
    assert [(f.severity, f.message) for f in findings] == [
        ("error", "tickets is linear and review.followup_from_round is set")
    ]


VERDICT = """<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=4/5 head=abc ac=met verdict=APPROVE risk=high -->
**Review round 4 of 5: APPROVE**

1. [follow-up] lib/a.py:10 breaks the path-format rule from round 2: `C:\\x` is not normalized.
   Repro:
   ```
   Blocked by: #1
   Touches: lib/x.py
   ```
   Conflicts with #2

2. [minor] lib/b.py:3 naming.
3) [Follow-up] lib/c.py:7 breaks the exception rule from round 3: `OSError` escapes.
Touches: lib/y.py

## Checks
- CI green.
"""

FIRST = """1. [follow-up] lib/a.py:10 breaks the path-format rule from round 2: `C:\\x` is not normalized.
   Repro:
   ```
   Blocked by: #1
   Touches: lib/x.py
   ```
   Conflicts with #2"""
SECOND = """3) [Follow-up] lib/c.py:7 breaks the exception rule from round 3: `OSError` escapes.
Touches: lib/y.py"""


def test_follow_up_items_are_kept_verbatim_and_others_dropped() -> None:
    assert followup_findings(VERDICT) == (FIRST, SECOND)
    assert followup_findings("1. [blocking] a\n2. [minor] b\n") == ()


def test_follow_up_ticket_names_the_pr_verdict_and_findings() -> None:
    url = "https://github.com/owner/repo/pull/42#issuecomment-99"
    title, body = followup_issue("owner/repo", 42, 4, url, "99", (FIRST, SECOND))

    assert title == "Follow-ups from #42 r4"
    assert "Blocked by: #42" in body.splitlines()
    assert url in body
    for finding in (FIRST, SECOND):
        quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in finding.splitlines())
        assert quoted in body
    assert body.splitlines()[-1] == "<!-- orch-followup verdict=99 -->"


def test_only_the_follow_up_ticket_own_blocker_is_a_feeder_gate() -> None:
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", (FIRST, SECOND))
    gates = parse_gate_lines(body, "owner/repo")
    assert gates.blocked_by == frozenset({github_ref("owner/repo", 42)})
    assert gates.touches == frozenset()
    assert gates.conflicts_with == frozenset()


def test_follow_up_ticket_body_fits_github_limit() -> None:
    huge = tuple(f"{n}. [follow-up] " + "x" * 9000 for n in range(1, 11))
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", huge)
    assert len(body) <= 65536
    assert "Blocked by: #42" in body.splitlines()
    assert body.splitlines()[-1] == "<!-- orch-followup verdict=99 -->"


def review_brief(round_text: str, followup: int | None) -> str:
    action, recipient, attempt, head, extra = snapshots.CASES["review"]
    job = snapshots._job(action, recipient, attempt, head, {**extra, "round": round_text})
    text = snapshots.FACTORY_MD
    if followup is not None:
        text = text.replace("  verify: verify-cleffy\n", f"  verify: verify-cleffy\n  followup_from_round: {followup}\n")
    context, findings = parse_factory_context(text)
    assert context is not None, findings
    assert context.config.followup_from_round == followup
    return render_brief(
        job, bot="codex-bot", context=context, context_ref="dev", factory_root=snapshots.BOX_FACTORY_ROOT
    )


FOLLOWUP_STEP = "4. Mark a finding `[follow-up]` instead of `[blocking]`"
FOLLOWUP_RULES = (
    "- From round 4 (`review.followup_from_round`), variants of an already-raised rule are follow-ups, "
    "not blockers. A real new failure still blocks at any round.",
    "- A verdict whose only findings are `[follow-up]` or `[minor]` is `APPROVE` with `ac=met`; "
    "the poller files the `[follow-up]` findings as one ticket. Builders do not fix `[follow-up]` findings.",
)


def test_review_brief_without_the_key_is_the_snapshot() -> None:
    snapshot = (snapshots.SNAPSHOTS / "review.txt").read_text(encoding="utf-8")
    box = snapshot.split("===== box =====\n", 1)[1].split("===== remote", 1)[0]
    assert review_brief("2/3", None) == box
    assert review_brief("2/3", 4) == box


def test_review_brief_before_the_follow_up_round_is_unchanged() -> None:
    assert review_brief("3/5", 4) == review_brief("3/5", None)
    assert "[follow-up]" not in review_brief("3/5", 4)


@pytest.mark.parametrize("round_text", ["4/5", "5/5"])
def test_review_brief_from_the_follow_up_round_defines_the_tag(round_text: str) -> None:
    brief = review_brief(round_text, 4)
    assert FOLLOWUP_STEP in brief
    assert "5. Pick `ac` and the verdict" in brief
    assert "`[blocking]`, `[follow-up]` or `[minor]`" in brief
    for rule in FOLLOWUP_RULES:
        assert rule in brief.splitlines()
