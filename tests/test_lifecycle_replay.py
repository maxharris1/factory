"""#108 replay: the real evidence threads, driven through poll_once cycles.

#89 was approved at 1/1 and handed off, then got two more rounds outside the
factory and was merged by hand; the poller never saw any of it. AUT-2284 was
finished when #66 lost the state dir, and the fresh database queued its
build again. #580 parked on an unsat= REVISE, and Max's state=building go
re-queued the same review instead of a fix (#611).
"""

from __future__ import annotations

import pytest

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from lib.dispatcher import CIResult, Comment, Dispatcher, TrackedTicket
from lib.poll_source import IntakeRisks
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, factory


pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

FIXTURES = Path(__file__).parent / "fixtures" / "lifecycle"
TRUSTED = {
    "github": {"maxharris1": {"grok-bot", "claude-bot", "codex-bot", "cursor-bot"}},
    "linear": {"linear:grok": {"grok-bot"}},
}


def _load(name: str) -> tuple[dict, list[Comment]]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    comments = [
        Comment(
            id=comment_id,
            author=author,
            body=header + "\n",
            created_at=datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp(),
        )
        for comment_id, author, at, header in data["comments"]
    ]
    return data, comments


class Thread:
    """A PollSource over one ticket whose comments arrive in time order."""

    def __init__(self, dispatcher: Dispatcher, base: TrackedTicket, comments: list[Comment]):
        self.dispatcher = dispatcher
        self.base = base
        self.comments = comments
        self.shown = 0
        self.merged = False
        self.config = type("Config", (), {"poll_seconds": 30})()

    def snapshot(self) -> TrackedTicket:
        visible = tuple(self.comments[: self.shown])
        heads = [
            part.split("=", 1)[1]
            for item in visible
            for part in item.body.split()
            if part.startswith("head=") and "kind=ready" in item.body
        ]
        head = heads[-1] if heads else self.base.current_head
        return replace(self.base, comments=visible, current_head=head, ci={head: CIResult("success")})

    def github_candidates(self, label_prefix: str, *, exclude=frozenset()):
        return (self.snapshot(),) if self.base.factory.tickets == "github" else ()

    def linear_candidates(self, query, *, exclude, reserve):
        return (self.snapshot(),) if self.base.factory.tickets == "linear" else ()

    def tracked_tickets(self, refs):
        found = []
        for ref in refs:
            if self.merged:
                self.dispatcher.record_terminal(ref.repo, ref.ticket_id, "merged")
                if not ref.allow_merged:
                    continue
            found.append(self.snapshot())
        return tuple(found)


def _dispatcher(tmp_path: Path, actions: FakeReviewActions, now: list[float]):
    path = tmp_path / "orch.db"  # empty: a fresh database, as after #66
    queue = SQLiteJobQueue(path, clock=lambda: now[0])
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED,
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
    )
    return dispatcher, queue


def _run(
    dispatcher: Dispatcher, source: Thread, now: list[float], cycles: int = 11
) -> None:
    for _ in range(cycles):
        dispatcher.poll_once(source)
        now[0] += 30


def _rows(queue: SQLiteJobQueue, sql: str) -> list[tuple]:
    with queue.connect() as connection:
        return [tuple(row) for row in connection.execute(sql)]


def test_pr_89_is_flagged_after_the_cap_then_ends_merged(tmp_path: Path) -> None:
    data, comments = _load("pr-89.json")
    now = [1000.0]
    actions = FakeReviewActions()
    dispatcher, queue = _dispatcher(tmp_path, actions, now)
    IntakeRisks(dispatcher.state_path).keep(data["repo"], data["ticket_id"], data["risk"])
    base = TrackedTicket(
        repo=data["repo"],
        ticket_id=data["ticket_id"],
        factory=factory(ship="handoff", rounds_low=1),
        comments=(),
        current_head=None,
        ci={},
        risk=data["risk"],
        ready_starts_review=True,
    )
    source = Thread(dispatcher, base, comments)

    source.shown = 1  # ready 1/3: tracking starts and round 1 is queued
    _run(dispatcher, source, now, 1)
    review = queue.claim("codex")
    assert review is not None
    queue.finish(review.id, owner="codex", handoff_comment_id=comments[1].id)
    for shown in range(2, len(comments) + 1):
        source.shown = shown
        _run(dispatcher, source, now)
    after_cap = _rows(queue, "SELECT reason FROM needs_attention")
    source.merged = True
    _run(dispatcher, source, now)

    outcomes = dict(_rows(queue, "SELECT comment_id, outcome FROM handled_comments"))
    assert [outcomes.get(item.id) for item in comments] == [
        "queued-review",
        "handed-off",
        "ready-after-cap",
        "after-handoff",
        "ready-after-cap",
        "after-handoff",
    ]
    assert after_cap == [("ready-after-cap",)]
    assert _rows(queue, "SELECT workflow_state, stuck FROM ticket_state") == [("merged", 0)]
    assert _rows(queue, "SELECT reason FROM needs_attention") == []
    assert _rows(queue, "SELECT action, recipient FROM jobs ORDER BY created_at") == [
        ("review", "codex"),
        ("done", "grok"),
    ]
    assert actions.handoffs == [comments[0].body.split("head=")[1].split()[0]]
    assert dispatcher._tracked_refs(now[0] + 300) == ()


def test_aut_2284_on_an_empty_db_queues_no_build(tmp_path: Path) -> None:
    data, comments = _load("aut-2284.json")
    now = [1000.0]
    actions = FakeReviewActions()
    dispatcher, queue = _dispatcher(tmp_path, actions, now)
    base = TrackedTicket(
        repo=data["repo"],
        ticket_id=data["ticket_id"],
        factory=factory(tickets="linear", ship="handoff"),
        comments=(),
        current_head=None,
        ci={},
        risk=data["risk"],
    )
    source = Thread(dispatcher, base, comments)
    source.shown = len(comments)  # the whole finished thread, at once

    _run(dispatcher, source, now, 1)

    outcomes = dict(_rows(queue, "SELECT comment_id, outcome FROM handled_comments"))
    assert [outcomes.get(item.id) for item in comments] == [
        "answered-build",
        "answered-review",
        "handed-off",
    ]
    assert _rows(queue, "SELECT action, state FROM jobs WHERE recipient != 'grok'") == [
        ("build", "cancelled"),
        ("review", "cancelled"),
    ]
    assert _rows(queue, "SELECT workflow_state FROM ticket_state") == [("handed-off",)]
    assert len(actions.handoffs) == 1


def test_pr_580_building_go_on_an_unsat_park_fixes_then_reviews_the_new_head(tmp_path: Path) -> None:
    data, comments = _load("pr-580.json")
    now = [1000.0]
    dispatcher, queue = _dispatcher(tmp_path, FakeReviewActions(), now)
    base = TrackedTicket(
        repo=data["repo"],
        ticket_id=data["ticket_id"],
        factory=factory(),
        comments=(),
        current_head=None,
        ci={},
        risk=data["risk"],
    )
    source = Thread(dispatcher, base, comments)
    claimed = []

    for shown in range(1, len(comments) + 1):
        source.shown = shown
        _run(dispatcher, source, now)
        for recipient in ("claude", "codex"):
            job = queue.claim(recipient)
            if job is None:
                continue
            claimed.append(
                (job.request.action, recipient, job.request.head, json.loads(job.request.detail or "{}").get("round"))
            )
            answer = comments[shown].id if shown < len(comments) else None
            queue.finish(job.id, owner=recipient, handoff_comment_id=answer)

    outcomes = dict(_rows(queue, "SELECT comment_id, outcome FROM handled_comments"))
    assert [outcomes.get(item.id) for item in comments] == [
        None,
        "queued-plan",
        "queued-plan-build",
        "queued-review",
        "waiting-max-unsat",
        "queued-fix",
        "queued-review",
    ]
    parked, fixed = "1870b3b61486e62112c30d74d5efbcf2dbffa250", "2" * 40
    assert claimed == [
        ("plan", "claude", None, None),
        ("build", "claude", None, "1/3"),
        ("review", "codex", parked, "1/3"),
        ("fix", "claude", parked, "2/3"),
        ("review", "codex", fixed, "2/3"),
    ]
    assert _rows(queue, "SELECT action, head FROM jobs WHERE action = 'review' ORDER BY rowid") == [
        ("review", parked),
        ("review", fixed),
    ]
