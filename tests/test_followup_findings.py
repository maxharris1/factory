from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from markdown_it import MarkdownIt

from lib.brief import render_brief
from lib.dispatcher import CIResult, Dispatcher, TrackedTicket
from lib.factory_md import load_factory_md, parse_factory_context, parse_factory_md
from lib.feeder import parse_intake
from lib.feeder_gates import GateLines, Holds, github_ref, parse_gate_lines
from lib.followups import followup_findings, followup_issue
from tests import regenerate_brief_snapshots as snapshots
from tests.merge_queue_support import Board, sha
from tests.test_hooks import FakeBot
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
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
    title, body = followup_issue("owner/repo", 42, 4, url, "99", (FIRST, SECOND), "low")

    assert title == "Follow-ups from #42 r4"
    assert "Blocked by: #42" in body.splitlines()
    assert url in body
    for finding in (FIRST, SECOND):
        quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in finding.splitlines())
        assert quoted in body
    assert body.splitlines()[-1] == "<!-- orch-followup verdict=99 -->"


def test_only_the_follow_up_ticket_own_blocker_is_a_feeder_gate() -> None:
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", (FIRST, SECOND), "low")
    gates = parse_gate_lines(body, "owner/repo")
    assert gates.blocked_by == frozenset({github_ref("owner/repo", 42)})
    assert gates.touches == frozenset({"lib/a.py", "lib/x.py", "lib/c.py", "lib/y.py"})
    assert gates.conflicts_with == frozenset()


@pytest.mark.parametrize("risk", ["low", "high"])
def test_follow_up_ticket_passes_feeder_intake(risk: str) -> None:
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", (FIRST, SECOND), risk)
    intake = parse_intake(body, labels=("factory:ready", "urgent"))
    assert intake.defects == ()
    assert intake.risk == risk
    assert intake.plan_first is False
    assert intake.depends_on == "#42"
    assert len(intake.criteria) == 2


@pytest.mark.parametrize(
    "path",
    [
        "bin/orch",
        "deploy/orch-poll.service",
        "deploy/orch-waiter@.service",
        ".gitignore",
        ".gitattributes",
        "lib/a.py",
        "factory.md",
    ],
)
@pytest.mark.parametrize("citation", ["{}:10", "[source]({}#L3)", "[source]({}#L3-L5)"])
def test_every_cited_file_is_touched_and_collides_with_its_holder(path: str, citation: str) -> None:
    finding = (
        f"1. [follow-up] {citation.format(path)} drops the exit status (round 4/5, owner/repo#7, "
        "https://github.com/owner/repo/pull/7, /home/bot/x.py, lib.followups)."
    )
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", (finding,), "low")
    gates = parse_gate_lines(body, "owner/repo")
    holds = Holds()
    holds.take(github_ref("owner/repo", 100), (), GateLines(touches=frozenset({path})))

    assert gates.touches == frozenset({path})
    assert holds.collision(github_ref("owner/repo", 101), gates) is not None


def test_unit_templates_and_root_dotfiles_appear_exactly_in_touches() -> None:
    finding = (
        "1. [follow-up] deploy/orch-waiter@.service:3 and .gitignore miss the new path; "
        "so does .gitattributes."
    )
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", (finding,), "low")
    assert "Touches: deploy/orch-waiter@.service, .gitignore, .gitattributes" in body.splitlines()


def test_follow_up_ticket_without_cited_files_has_no_touches_line() -> None:
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", ("1. [follow-up] rename it",), "low")
    assert parse_intake(body, labels=()).defects == ()
    assert parse_gate_lines(body, "owner/repo").touches == frozenset()


def test_follow_up_ticket_body_fits_github_limit() -> None:
    huge = tuple(f"{n}. [follow-up] " + "x" * 9000 for n in range(1, 11))
    _title, body = followup_issue("owner/repo", 42, 4, "https://x", "99", huge, "low")
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


FOLLOWUP_STEP = "9. Mark a finding `[follow-up]` instead of `[blocking]`"
FOLLOWUP_RULES = (
    "- From round 4 (`review.followup_from_round`), variants of an already-raised rule are follow-ups, "
    "not blockers. A real new failure still blocks at any round.",
    "- A verdict whose only findings are `[follow-up]` or `[minor]` is `APPROVE` with `ac=met`; "
    "the poller files the `[follow-up]` findings as one ticket. Builders do not fix `[follow-up]` findings.",
)


def test_review_brief_without_the_key_is_the_snapshot() -> None:
    snapshot = (snapshots.SNAPSHOTS / "review.txt").read_text(encoding="utf-8")
    box = snapshot.split("===== box =====\n", 1)[1].split("===== remote", 1)[0]
    assert review_brief("1/3", None) == box
    assert review_brief("1/3", 4) == box


def test_review_brief_before_the_follow_up_round_is_unchanged() -> None:
    assert review_brief("3/5", 4) == review_brief("3/5", None)
    assert "[follow-up]" not in review_brief("3/5", 4)


@pytest.mark.parametrize("round_text", ["4/5", "5/5"])
def test_review_brief_from_the_follow_up_round_defines_the_tag(round_text: str) -> None:
    brief = review_brief(round_text, 4)
    assert FOLLOWUP_STEP in brief
    assert "10. Pick `ac` and the verdict" in brief
    assert "`[blocking]`, `[follow-up]` or `[minor]`" in brief
    for rule in FOLLOWUP_RULES:
        assert rule in brief.splitlines()


HEAD = "a" * 40
VERDICT_ID = f"verdict-{HEAD}"
FOLLOWUP_A = "1. [follow-up] lib/a.py:10 breaks the path-format rule from round 2.\n   Repro: `x`."
FOLLOWUP_B = "2. [follow-up] lib/c.py:7 breaks the exception rule from round 3."
MINOR = "3. [minor] lib/b.py:3 naming."


class FollowupActions(FakeReviewActions):
    def __init__(self) -> None:
        super().__init__()
        self.issues: dict[int, tuple[str, str, tuple[str, ...]]] = {}
        self.issue_comments: dict[int, list[str]] = {}
        self.closed: set[int] = set()
        self.pr_comments: list[str] = []
        self.creates = 0
        self.posts = 0
        self.additions = 0
        self.crash_after_create = False
        self.crash_after_post = False
        self.crash_after_addition = False

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
        if marker.startswith("<!-- orch-wake-skip "):
            super().comment_once(ticket, body, marker)
            return
        if any(marker in existing.splitlines() for existing in self.pr_comments):
            return
        self.pr_comments.append(body)
        self.posts += 1
        if self.crash_after_post:
            raise SystemExit("crash after the link comment was posted")

    def open_issue_labels(self, ticket, number) -> tuple[str, ...] | None:
        del ticket
        return None if number in self.closed else self.issues[number][2]

    def comment_on_issue_once(self, ticket, number, body, marker) -> None:
        del ticket
        existing = self.issue_comments.setdefault(number, [])
        if any(marker in text.splitlines() for text in existing):
            return
        existing.append(body)
        self.additions += 1
        if self.crash_after_addition:
            raise SystemExit("crash after the follow-ups were added to the issue")


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
    risk: str = "high",
    verdict_id: str = VERDICT_ID,
) -> TrackedTicket:
    body = (
        VERDICT.format(round=f"{round_number}/5", head=head, verdict=verdict, risk=risk)
        + "\n".join(findings)
        + "\n"
    )
    base = ticket(
        comment(verdict_id, body, author=author, url="https://github.com/owner/repo/pull/42#issuecomment-9"),
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
    approve = verdict_ticket("APPROVE", 4, (FOLLOWUP_A, MINOR, FOLLOWUP_B), risk="low")
    assert approve.risk == "high"
    dispatcher.process(approve)

    assert handled_outcome(queue, VERDICT_ID) == handled_outcome(plain_queue, VERDICT_ID) == "merged"
    assert actions.merges == plain.merges == [(True, HEAD)]
    assert actions.states == plain.states
    assert plain.issues == {} and plain.pr_comments == []
    [(title, body, labels)] = actions.issues.values()
    assert title == "Follow-ups from #42 r4"
    assert labels == ()
    assert "Blocked by: #42" in body.splitlines()
    assert "> 1. [follow-up] lib/a.py:10 breaks the path-format rule from round 2." in body
    assert "> 2. [follow-up] lib/c.py:7 breaks the exception rule from round 3." in body
    assert "[minor]" not in body
    intake = parse_intake(body, ("factory:ready",))
    assert intake.defects == ()
    assert intake.risk == "low"
    assert "Touches: lib/a.py, lib/c.py" in body.splitlines()
    assert actions.pr_comments == [
        "Follow-ups from review round 4 are parked on #300; they do not block this PR."
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


FOLLOWUP_C = "1. [follow-up] lib/e.py:2 breaks the path-format rule from round 2 again."
LATER_ID = f"verdict-r5-{HEAD}"


def two_rounds(tmp_path: Path, actions: FollowupActions):
    dispatcher, queue = at_round(tmp_path, actions, 4)
    dispatcher.process(verdict_ticket("REVISE", 4, (FOLLOWUP_A,)))
    later = verdict_ticket("REVISE", 5, (FOLLOWUP_C,), verdict_id=LATER_ID)
    with queue.connect() as connection:
        dispatcher._record_followups(connection, later, later.comments[0], 5)
        connection.commit()
    return dispatcher, queue, later


def test_a_later_round_adds_its_follow_ups_to_the_parked_issue(tmp_path: Path) -> None:
    actions = FollowupActions()
    dispatcher, queue, later = two_rounds(tmp_path, actions)
    dispatcher.process(later)

    assert list(actions.issues) == [300]
    [addition] = actions.issue_comments[300]
    assert "> 1. [follow-up] lib/e.py:2 breaks the path-format rule from round 2 again." in addition
    assert "round 5 review verdict on owner/repo#42" in addition
    assert addition.splitlines()[-1] == f"<!-- orch-followup verdict={LATER_ID} -->"
    assert actions.pr_comments == [
        "Follow-ups from review round 4 are parked on #300; they do not block this PR."
        f"\n\n<!-- orch-followup-link verdict={VERDICT_ID} -->",
        "Follow-ups from review round 5 are parked on #300; they do not block this PR."
        f"\n\n<!-- orch-followup-link verdict={LATER_ID} -->",
    ]
    assert filings(queue) == [
        (VERDICT_ID, 4, "commented", 300),
        (LATER_ID, 5, "commented", 300),
    ]


def test_a_crash_after_each_write_of_an_addition_never_writes_twice(tmp_path: Path) -> None:
    actions = FollowupActions()
    dispatcher, queue, later = two_rounds(tmp_path, actions)
    actions.crash_after_addition = True
    with pytest.raises(SystemExit):
        dispatcher.process(later)
    assert filings(queue)[1] == (LATER_ID, 5, "pending", 300)

    actions.crash_after_addition = False
    actions.closed.add(300)
    actions.crash_after_post = True
    restarted, _queue = setup(tmp_path, review_actions=actions)
    with pytest.raises(SystemExit):
        restarted.process(later)
    assert filings(queue)[1] == (LATER_ID, 5, "filed", 300)

    actions.crash_after_post = False
    again, _queue = setup(tmp_path, review_actions=actions)
    again.process(later)

    assert (actions.creates, actions.additions, actions.posts) == (1, 1, 2)
    assert list(actions.issues) == [300]
    assert filings(queue)[1] == (LATER_ID, 5, "commented", 300)


@pytest.mark.parametrize("left", ["closed", "promoted"])
def test_a_later_round_files_a_new_issue_once_the_last_one_left_the_park(
    tmp_path: Path, left: str
) -> None:
    actions = FollowupActions()
    dispatcher, queue, later = two_rounds(tmp_path, actions)
    if left == "closed":
        actions.closed.add(300)
    else:
        title, body, _labels = actions.issues[300]
        actions.issues[300] = (title, body, ("factory:ready",))
    dispatcher.process(later)

    assert list(actions.issues) == [300, 301]
    assert actions.issue_comments == {}
    assert actions.issues[301][0] == "Follow-ups from #42 r5"
    assert actions.issues[301][2] == ()
    assert filings(queue)[1] == (LATER_ID, 5, "commented", 301)


def test_the_done_wake_names_the_parked_issue(tmp_path: Path) -> None:
    actions = FollowupActions()
    dispatcher, queue = at_round(tmp_path, actions, 4)
    dispatcher.process(verdict_ticket("APPROVE", 4, (FOLLOWUP_A,)))

    [wake] = [job for job in FakeBot(queue, "grok").drain() if job.request.action == "done"]
    assert json.loads(str(wake.request.detail))["followup_issue"] == 300

    plain = FollowupActions()
    plain_dispatcher, plain_queue = at_round(tmp_path / "plain", plain, 4)
    plain_dispatcher.process(verdict_ticket("APPROVE", 4, (MINOR,)))
    # With nothing parked, a done is a PR notice, not a Grok wake (issue #543).
    assert [
        job for job in FakeBot(plain_queue, "grok").drain() if job.request.action == "done"
    ] == []


class FailingCreates(FollowupActions):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def file_issue(self, *args) -> int:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transient GitHub failure")
        return super().file_issue(*args)


@pytest.mark.parametrize("failures", [1, 2])
def test_the_done_wake_waits_for_a_failed_filing_and_names_its_issue(
    tmp_path: Path, failures: int
) -> None:
    actions = FailingCreates(failures)
    dispatcher, queue = at_round(tmp_path, actions, 4)
    approve = verdict_ticket("APPROVE", 4, (FOLLOWUP_A,))
    dispatcher.process(approve)
    bot = FakeBot(queue, "grok")
    if failures == 2:
        assert [job for job in bot.drain() if job.request.action == "done"] == []
        assert filings(queue) == [(VERDICT_ID, 4, "pending", None)]
        dispatcher.process(approve)
    dispatcher.process(approve)

    [wake] = [job for job in bot.drain() if job.request.action == "done"]
    assert list(actions.issues) == [300]
    detail = json.loads(str(wake.request.detail))
    assert (detail["outcome"], detail["followup_issue"]) == ("merged", 300)
    assert handled_outcome(queue, VERDICT_ID) == "merged"


def test_a_crash_after_filing_a_new_issue_never_adds_to_a_reopened_one(
    tmp_path: Path,
) -> None:
    actions = FollowupActions()
    dispatcher, queue, later = two_rounds(tmp_path, actions)
    actions.closed.add(300)
    actions.crash_after_create = True
    with pytest.raises(SystemExit):
        dispatcher.process(later)
    assert list(actions.issues) == [300, 301]

    actions.closed.clear()
    actions.crash_after_create = False
    restarted, _queue = setup(tmp_path, review_actions=actions)
    restarted.process(later)

    assert actions.issue_comments == {}
    assert actions.creates == 2
    assert filings(queue)[1] == (LATER_ID, 5, "commented", 301)


def test_a_pending_row_from_before_the_park_files_an_unlabelled_issue(
    tmp_path: Path,
) -> None:
    actions = FollowupActions()
    dispatcher, queue = at_round(tmp_path, actions, 4)
    approve = verdict_ticket("APPROVE", 4, (FOLLOWUP_A,))
    with queue.connect() as connection:
        dispatcher._record_followups(connection, approve, approve.comments[0], 4)
        connection.execute(
            "UPDATE followup_filings SET labels = ?",
            (json.dumps(["factory:ready", "urgent"]),),
        )
        connection.execute("ALTER TABLE followup_filings DROP COLUMN addition")
    restarted, _queue = setup(tmp_path, review_actions=actions)
    restarted.process(approve)

    assert actions.issues[300][2] == ()
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


NESTED = (
    "1. [follow-up] lib/a.py:10 repeats the round 2 path rule.\n"
    "   Repro:\n"
    "   1. Write a Windows path.\n"
    "   2. Run the parser.\n"
    "   ```\n"
    "   1. not a finding\n"
    "   ## not a heading\n"
    "   ```\n"
    "   Expected: normalized path. Actual: ValueError."
)


def test_nested_steps_and_fenced_lines_stay_in_their_finding() -> None:
    body = f"{NESTED}\n\n2. [minor] lib/b.py:3 naming.\n   1. [follow-up] nested, not its own.\n"
    assert followup_findings(body) == (NESTED,)


FENCED_FINDING = "2. [follow-up] lib/a.py:10 repeats the round 2 path rule.\n   Repro: run parser with C:\\x."


@pytest.mark.parametrize(
    "minor",
    [
        "1. [minor] docs/example.md:1 spelling.\n\n   ````markdown\n   ```\n   ````",
        "1. [minor] docs/example.md:1 spelling.\n\n   ~~~\n   ```\n   ~~~",
        "1. [minor] docs/example.md:1 spelling.\n\n   ```\n   ~~~\n   ```",
        "1. [minor] docs/example.md:1 spelling.\n\n   ```\n   ``` not a close\n   ```",
        "1. [minor] docs/example.md:1 spelling.\n\n   ```\n   left open",
        "1. [minor] docs/example.md:1 spelling.\n\n```\n```",
    ],
)
def test_a_follow_up_after_any_valid_code_block_survives(minor: str) -> None:
    body = f"{minor}\n\n{FENCED_FINDING}\n"
    assert MarkdownIt("commonmark").render(body).count("<li>") == 2
    assert followup_findings(body) == (FENCED_FINDING,)


def test_a_follow_up_inside_a_code_block_is_not_a_finding() -> None:
    body = f"1. [minor] spelling.\n\n````\n{FENCED_FINDING}\n```\n````\n"
    assert followup_findings(body) == ()


@pytest.mark.parametrize("fail_at", ["file", "link"])
@pytest.mark.parametrize("left_as", ["merged", "done", "abandoned"])
def test_a_failed_filing_retries_after_the_ticket_leaves_the_loop(
    tmp_path: Path, fail_at: str, left_as: str
) -> None:
    board = Board(tmp_path, ship="handoff", finish_line="merge")
    board.factory = replace(board.factory, followup_from_round=4)
    pr = board.open("42", sha(1))
    calls = {"file": 0, "link": 0}
    issues: list[int] = []
    links: list[str] = []

    def file_issue(*args) -> int:
        calls["file"] += 1
        if fail_at == "file" and calls["file"] == 1:
            raise RuntimeError("transient issue API failure")
        issues.append(300)
        return 300

    def comment_once(ticket, body, marker) -> None:
        calls["link"] += 1
        if fail_at == "link" and calls["link"] == 1:
            raise RuntimeError("transient comment API failure")
        links.append(body)

    board.actions.file_issue = file_issue
    board.actions.comment_once = comment_once
    board.go(pr, "go")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    with board.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET review_round = 3, cap_extra = 2 WHERE ticket_id = '42'"
        )
    body = VERDICT.format(round="4/5", head=pr.head, verdict="APPROVE", risk="high")
    board.post(pr, "verdict-4", body + FOLLOWUP_A + "\n", "codex-login")
    board.poll()
    assert board.state("42")["workflow_state"] == "merged"
    with board.queue.connect() as connection:
        connection.execute(
            "UPDATE ticket_state SET workflow_state = ? WHERE ticket_id = '42'", (left_as,)
        )
    board.dispatcher = Dispatcher(
        tmp_path / "d.db",
        board.queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=board.actions,
        grok_delivery=board.grok,
    )
    for _ in range(3):
        board.poll()

    assert filings(board.queue) == [("verdict-4", 4, "commented", 300)]
    assert issues == [300]
    assert len(links) == 1
    assert board.last_refs == ()


@pytest.mark.parametrize("separator", [" ", "\x0c", "\x1c", "\x85"])
def test_a_follow_up_after_unusual_line_separators_survives(separator: str) -> None:
    body = f"1. [minor] a{separator}b{separator}c\r\n\r\n{FENCED_FINDING}\r\n"
    assert followup_findings(body) == (FENCED_FINDING,)
