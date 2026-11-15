"""Follow-up findings from review round N (issue #259)."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from lib.brief import render_brief
from lib.dispatcher import CIResult, TrackedTicket
from lib.factory_md import load_factory_md, parse_factory_context, parse_factory_md
from lib.feeder_gates import github_ref, parse_gate_lines
from lib.followups import followup_findings, followup_issue
from tests import regenerate_brief_snapshots as snapshots
from tests.test_dispatcher import (
    VERDICT,
    FakeReviewActions,
    comment,
    handled_outcome,
    seed_review_job,
    setup,
    ticket,
)

VALID = {
    "base_branch": "main",
    "tickets": "github",
    "builder": {"harness": "claude"},
    "reviewer": {"harness": "codex"},
    "review": {"rounds": {"low": 1, "high": 3}, "verify": "none"},
    "ship": "merge",
    "skills": [],
}
ROOT = Path(__file__).resolve().parents[1]
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


def test_the_factory_repo_turns_follow_ups_on_from_round_four() -> None:
    config, findings = load_factory_md(ROOT / "factory.md")
    assert [f for f in findings if f.severity == "error"] == []
    assert config is not None and config.followup_from_round == 4


SAMPLE_VERDICT = """<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=4/5 head=abc ac=met verdict=APPROVE risk=high -->
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
    assert followup_findings(SAMPLE_VERDICT) == (FIRST, SECOND)
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


HEAD = "a" * 40
# seed_review_job finishes the review job with this handoff comment id.
VERDICT_ID = f"verdict-{HEAD}"
FOLLOWUP_A = "1. [follow-up] lib/a.py:10 breaks the path-format rule from round 2.\n   Repro: `x`."
FOLLOWUP_B = "2. [follow-up] lib/c.py:7 breaks the exception rule from round 3."
MINOR = "3. [minor] lib/b.py:3 naming."


class FollowupActions(FakeReviewActions):
    """Tracker fake that keeps what it wrote and honors the marker lookup."""

    def __init__(self) -> None:
        super().__init__()
        self.issues: dict[int, tuple[str, str, tuple[str, ...]]] = {}
        self.pr_comments: list[str] = []
        self.creates = 0
        self.posts = 0
        self.crash_after_create = False
        self.crash_after_post = False

    def file_issue(self, ticket, title, body, labels, marker, since) -> int:
        del ticket, since
        for number, (_title, existing, _labels) in self.issues.items():
            if marker in existing.splitlines():
                return number
        number = 300 + len(self.issues)
        self.issues[number] = (title, body, labels)
        self.creates += 1
        if self.crash_after_create:
            raise SystemExit("crash after the issue was created")
        return number

    def comment_once(self, ticket, body, marker) -> None:
        del ticket
        if any(marker in existing.splitlines() for existing in self.pr_comments):
            return
        self.pr_comments.append(body)
        self.posts += 1
        if self.crash_after_post:
            raise SystemExit("crash after the link comment was posted")


def at_round(tmp_path: Path, actions: FollowupActions, review_round: int):
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    seed_review_job(dispatcher, queue, HEAD)
    with queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET review_round = ?, cap_extra = 2 WHERE ticket_id = '42'",
            (review_round - 1,),
        )
    return dispatcher, queue


def verdict_ticket(
    verdict: str,
    round_number: int,
    findings: tuple[str, ...],
    *,
    followup: int | None = 4,
    author: str = "codex-login",
    head: str = HEAD,
) -> TrackedTicket:
    body = (
        VERDICT.format(round=f"{round_number}/5", head=head, verdict=verdict, risk="high")
        + "\n".join(findings)
        + "\n"
    )
    base = ticket(
        comment(VERDICT_ID, body, author=author, url="https://github.com/owner/repo/pull/42#issuecomment-9"),
        head=HEAD,
        ci={HEAD: CIResult("success")},
    )
    return replace(
        base,
        factory=replace(base.factory, followup_from_round=followup),
        intake_label="factory:ready",
    )


def filings(queue) -> list[tuple]:
    with queue.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT comment_id, round, status, issue_number FROM followup_filings"
            )
        ]


def test_approve_with_follow_ups_ships_as_before_and_files_one_ticket(tmp_path: Path) -> None:
    plain = FollowupActions()
    plain_dispatcher, plain_queue = at_round(tmp_path / "plain", plain, 4)
    plain_dispatcher.process(verdict_ticket("APPROVE", 4, (MINOR,)))

    actions = FollowupActions()
    dispatcher, queue = at_round(tmp_path / "followup", actions, 4)
    approve = verdict_ticket("APPROVE", 4, (FOLLOWUP_A, MINOR, FOLLOWUP_B))
    dispatcher.process(approve)

    assert handled_outcome(queue, VERDICT_ID) == handled_outcome(plain_queue, VERDICT_ID) == "merged"
    assert actions.merges == plain.merges == [(True, HEAD)]
    assert actions.states == plain.states
    assert plain.issues == {} and plain.pr_comments == []
    [(title, body, labels)] = actions.issues.values()
    assert title == "Follow-ups from #42 r4"
    assert labels == ("factory:ready", "urgent")
    assert "Blocked by: #42" in body.splitlines()
    assert "> 1. [follow-up] lib/a.py:10 breaks the path-format rule from round 2." in body
    assert "> 2. [follow-up] lib/c.py:7 breaks the exception rule from round 3." in body
    assert "[minor]" not in body
    assert actions.pr_comments == [
        "Follow-ups from review round 4 are filed as #300; they do not block this PR."
        f"\n\n<!-- orch-followup-link verdict={VERDICT_ID} -->"
    ]

    for _ in range(3):
        dispatcher.process(approve)
    restarted, _queue = setup(tmp_path / "followup", review_actions=actions)
    restarted.process(approve)

    assert (actions.creates, actions.posts) == (1, 1)
    assert filings(queue) == [(VERDICT_ID, 4, "commented", 300)]


def test_a_crash_after_each_tracker_write_never_writes_twice(tmp_path: Path) -> None:
    actions = FollowupActions()
    dispatcher, queue = at_round(tmp_path, actions, 4)
    approve = verdict_ticket("APPROVE", 4, (FOLLOWUP_A,))
    actions.crash_after_create = True
    with pytest.raises(SystemExit):
        dispatcher.process(approve)
    assert filings(queue) == [(VERDICT_ID, 4, "pending", None)]

    actions.crash_after_create = False
    actions.crash_after_post = True
    restarted, _queue = setup(tmp_path, review_actions=actions)
    with pytest.raises(SystemExit):
        restarted.process(approve)
    assert filings(queue) == [(VERDICT_ID, 4, "filed", 300)]

    actions.crash_after_post = False
    again, _queue = setup(tmp_path, review_actions=actions)
    again.process(approve)

    assert (actions.creates, actions.posts) == (1, 1)
    assert list(actions.issues) == [300]
    assert filings(queue) == [(VERDICT_ID, 4, "commented", 300)]


@pytest.mark.parametrize(
    ("review_round", "kwargs", "outcome"),
    [
        (4, {"followup": None}, "merged"),
        (3, {}, "merged"),
        (4, {"author": "grok-login"}, "rejected-approve-author"),
        (4, {"head": "b" * 40}, "stale-approve"),
    ],
    ids=["key-absent", "before-round", "untrusted", "stale-head"],
)
def test_no_filing_without_an_accepted_verdict_from_the_follow_up_round(
    tmp_path: Path, review_round: int, kwargs: dict, outcome: str
) -> None:
    actions = FollowupActions()
    dispatcher, queue = at_round(tmp_path, actions, review_round)
    dispatcher.process(verdict_ticket("APPROVE", review_round, (FOLLOWUP_A, FOLLOWUP_B), **kwargs))

    assert handled_outcome(queue, VERDICT_ID) == outcome
    assert filings(queue) == []
    assert actions.issues == {} and actions.pr_comments == []


def test_revise_with_a_blocking_finding_still_queues_the_same_fix(tmp_path: Path) -> None:
    blocking = "1. [blocking] lib/d.py:4 a new failure: crashes on empty input."
    follow_up = "2. [follow-up] lib/a.py:10 breaks the path-format rule from round 2."

    plain = FollowupActions()
    plain_dispatcher, plain_queue = at_round(tmp_path / "plain", plain, 4)
    plain_dispatcher.process(verdict_ticket("REVISE", 4, (blocking, follow_up), followup=None))

    actions = FollowupActions()
    dispatcher, queue = at_round(tmp_path / "followup", actions, 4)
    dispatcher.process(verdict_ticket("REVISE", 4, (blocking, follow_up)))

    assert handled_outcome(queue, VERDICT_ID) == "queued-review-fix"
    assert handled_outcome(plain_queue, VERDICT_ID) == "queued-review-fix"
    fix = queue.claim("claude")
    plain_fix = plain_queue.claim("claude")
    assert fix is not None and plain_fix is not None
    assert fix.request == plain_fix.request
    assert filings(plain_queue) == []
    assert filings(queue) == [(VERDICT_ID, 4, "commented", 300)]
    [(_title, body, _labels)] = actions.issues.values()
    assert "> 2. [follow-up] lib/a.py:10" in body
    assert "[blocking]" not in body
