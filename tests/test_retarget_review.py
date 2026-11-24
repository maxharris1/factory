from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.brief import render_brief
from lib.queue import JobRequest
from lib.update_check import RebaseScope, RetargetDelta
from tests.merge_queue_support import sha
from tests.regenerate_brief_snapshots import BOX_FACTORY_ROOT
from tests.test_lifecycle import HEADS, Flow
from tests.test_rebase_review import capped_at_cap, newest_review, rebase_then_ready
from tests.test_stack_retarget_transition import ONTO, land, ready_on, rebases, wired
from tests.test_ticket_base_branch import STACK_REPO, polled_child, stack_poll_errors

FROM_HEAD = sha(3)
PARENT_HEAD = sha(4)
NEW_HEAD = sha(8)
NEW_BASE = sha(6)
EDITED = RetargetDelta("before", "after", frozenset({"lib/a.py"}), frozenset({"lib/b.py"}))


class Delta:
    def __init__(self, result: RetargetDelta | Exception) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def __call__(self, repo, base, onto_from, from_head, parent_head, head) -> RetargetDelta:
        self.calls.append((repo, base, onto_from, from_head, parent_head, head))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def retargeted(tmp_path: Path, delta: RetargetDelta | Exception, exclusive: tuple[str, ...] = ()):
    queue, github, dispatcher = wired(tmp_path, state="review")
    queue.enqueue(
        JobRequest(
            repo=STACK_REPO, ticket_id="501", action="review", recipient="codex",
            source_comment_id="ready-1", timeout_seconds=600, head=FROM_HEAD,
            detail=json.dumps({"action": "review", "round": "1/3"}),
        ),
        dedupe_key="review-r1",
    )
    review = queue.claim("codex")
    queue.finish(review.id, owner="codex", handoff_comment_id="verdict-1")
    dispatcher.rebase_classifier = lambda repo, base, last, head: RebaseScope(last, sha(5), NEW_BASE)
    dispatcher.retarget_delta = Delta(delta)
    land(github)
    dispatcher.process(polled_child(tmp_path, github))
    (rebase,) = rebases(queue)
    assert (rebase["rebase"]["onto_from"], rebase["rebase"]["parent_head"]) == (ONTO, PARENT_HEAD)
    fix = queue.claim("claude")
    queue.finish(fix.id, owner="claude", handoff_comment_id="ready-9")

    def ready():
        ticket = ready_on(tmp_path, github, NEW_HEAD)
        return replace(ticket, factory=replace(ticket.factory, exclusive_paths=exclusive))

    dispatcher.process(ready())
    dispatcher.process(ready())
    return queue, dispatcher


def reviews(queue) -> list:
    return [job for job in queue.outstanding() if job.request.action == "review"]


def review_round(queue) -> int:
    with queue.connect() as connection:
        return connection.execute("SELECT review_round FROM ticket_state").fetchone()[0]


def test_an_edited_child_gets_one_rebase_review_ranged_from_onto_from(tmp_path: Path) -> None:
    queue, dispatcher = retargeted(tmp_path, EDITED)

    (review,) = reviews(queue)
    detail = json.loads(review.request.detail)
    assert (review.request.head, detail["source"], detail["round"]) == (NEW_HEAD, "rebase", "2/3")
    assert detail["rebase"] == {
        "last_reviewed_head": FROM_HEAD,
        "reviewed_base": ONTO,
        "base": NEW_BASE,
        "overlap": [],
    }
    assert review_round(queue) == 1, "the retarget review spends no round"
    assert dispatcher.retarget_delta.calls == [
        (STACK_REPO, "main", ONTO, FROM_HEAD, PARENT_HEAD, NEW_HEAD)
    ]


@pytest.mark.parametrize(
    ("child", "parent", "exclusive", "overlap"),
    [
        ({"lib/a.py", "lib/b.py"}, {"lib/b.py", "lib/c.py"}, (), ["lib/b.py"]),
        (
            {"lib/a.py", "db/migrations/002.sql"},
            {"db/migrations/001.sql"},
            ("db/migrations/",),
            ["db/migrations/002.sql"],
        ),
        ({"types/gen.ts"}, {"types/other.ts"}, ("types/*.ts",), ["types/gen.ts"]),
        ({"db/migrations/002.sql"}, {"lib/c.py"}, ("db/migrations/",), []),
    ],
    ids=["same-file", "exclusive-dir", "exclusive-glob", "exclusive-one-side"],
)
def test_the_parent_overlap_queues_one_scoped_review_naming_it(
    tmp_path: Path, child, parent, exclusive, overlap
) -> None:
    delta = RetargetDelta("same", "same", frozenset(child), frozenset(parent))
    queue, _dispatcher = retargeted(tmp_path, delta, exclusive)

    (review,) = reviews(queue)
    detail = json.loads(review.request.detail)
    assert detail["source"] == "rebase"
    assert detail["rebase"]["reviewed_base"] == ONTO
    assert detail["rebase"]["overlap"] == overlap
    assert review_round(queue) == 1


def test_the_retarget_review_brief_names_the_overlap_and_the_onto_from_range(
    tmp_path: Path,
) -> None:
    delta = RetargetDelta(
        "same", "same", frozenset({"lib/a.py", "lib/b.py"}), frozenset({"lib/b.py"})
    )
    queue, _dispatcher = retargeted(tmp_path, delta)
    (review,) = reviews(queue)

    text = render_brief(
        review, bot="codex-bot", context=None, context_note="no factory.md",
        factory_root=BOX_FACTORY_ROOT,
    )

    assert f"`git range-diff {ONTO}..{FROM_HEAD} {NEW_BASE}..{NEW_HEAD}`" in text
    assert "`lib/b.py`" in text
    assert "`lib/a.py`" not in text


def test_an_unreadable_delta_still_reviews_from_onto_from_once(tmp_path: Path) -> None:
    queue, _dispatcher = retargeted(tmp_path, ValueError("commit gone"))

    (review,) = reviews(queue)
    detail = json.loads(review.request.detail)
    assert detail["rebase"] == {
        "last_reviewed_head": FROM_HEAD,
        "reviewed_base": ONTO,
        "base": NEW_BASE,
    }
    assert any("retarget-delta" in error for error in stack_poll_errors(tmp_path))


def test_a_plain_rebase_never_reads_the_delta(tmp_path: Path) -> None:
    flow = Flow(tmp_path)
    capped_at_cap(flow)
    flow.dispatcher.rebase_classifier = lambda repo, base, last, head: RebaseScope(
        last, HEADS[7], HEADS[8]
    )
    flow.dispatcher.retarget_delta = Delta(AssertionError("a plain rebase read the delta"))

    assert rebase_then_ready(flow, HEADS[4]) == "queued-review"

    assert flow.dispatcher.retarget_delta.calls == []
    assert newest_review(flow)["rebase"] == {
        "last_reviewed_head": HEADS[3],
        "reviewed_base": HEADS[7],
        "base": HEADS[8],
    }
