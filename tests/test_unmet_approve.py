from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, Dispatcher
from lib.handoff import HandoffError, parse_comment
from lib.handoff_check import Expected, expected_for, problems
from lib.hook_t7 import T7Hook
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    READY,
    VERDICT,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
    factory,
    seed_builder_job,
    seed_review_job,
    ticket,
)

PR_124_HEAD = "6509f58ed23fcf8749c686c28d61f4e97a3007a2"
PR_124_HEADER = (
    "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer round=1/1 "
    f"head={PR_124_HEAD} ac=unmet verdict=APPROVE risk=low -->"
)


def setup(
    tmp_path: Path, review_actions: FakeReviewActions | None = None
) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=review_actions or FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        hooks=(T7Hook(),),
    )
    return dispatcher, queue


def verdict(
    head: str, *, verdict: str = "APPROVE", round_: str = "1", ac: str | None = None, body: str = ""
) -> str:
    text = str.format(VERDICT, round=round_, head=head, verdict=verdict, risk="high") + body
    if ac is None:
        return text
    return text.replace(f" verdict={verdict}", f" ac={ac} verdict={verdict}")


def set_floor(queue, since: float) -> None:
    with queue.connect() as connection:
        connection.execute("UPDATE approve_rule_floor SET since = ?", (since,))
        connection.commit()


def check(body: str) -> list[str]:
    expected = Expected(
        repo="owner/repo",
        pr=124,
        branch=None,
        bot="codex-bot",
        role="reviewer",
        kind="verdict",
        head=PR_124_HEAD,
        round="1/1",
    )
    read = {
        "repos/owner/repo/issues/comments/101": {
            "issue_url": "https://api.github.com/repos/owner/repo/issues/124",
            "user": {"login": "codex-login"},
            "body": body,
        }
    }.__getitem__
    return problems(expected, "101", read, {"codex-login": frozenset({"codex-bot"})})


def state(queue) -> tuple[str, int]:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT workflow_state, review_round FROM ticket_state"
            " WHERE repo = 'owner/repo' AND ticket_id = '42'"
        ).fetchone()
    return str(row["workflow_state"]), int(row["review_round"])


def outcome(queue, comment_id: str) -> str:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT outcome FROM handled_comments WHERE comment_id = ?", (comment_id,)
        ).fetchone()[0]


def jobs(queue, recipient: str) -> list:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT * FROM jobs WHERE recipient = ? ORDER BY created_at, id", (recipient,)
        ).fetchall()


def ship_intents(queue) -> list:
    with queue.connect() as connection:
        return connection.execute("SELECT * FROM ship_intents").fetchall()


def test_the_pr_124_header_does_not_parse() -> None:
    with pytest.raises(HandoffError, match="ac=unmet"):
        parse_comment(PR_124_HEADER + "\nLooks good.\n")


@pytest.mark.parametrize("ac", ["met", None])
def test_an_approve_with_ac_met_or_no_ac_still_parses(ac: str | None) -> None:
    fields = parse_comment(verdict("a" * 40, ac=ac)).as_dict()
    assert fields["verdict"] == "APPROVE"
    assert fields.get("ac") == ac


def test_a_revise_with_ac_unmet_still_parses() -> None:
    fields = parse_comment(verdict("a" * 40, verdict="REVISE", ac="unmet")).as_dict()
    assert (fields["verdict"], fields["ac"]) == ("REVISE", "unmet")


def test_the_pr_124_header_is_an_invalid_review_handoff() -> None:
    found = check(PR_124_HEADER + "\nLooks good.\n")
    assert len(found) == 1
    assert "ac=unmet" in found[0]


def test_an_ac_unmet_approve_does_not_ship_or_use_up_the_review(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    actions.states.clear()
    dispatcher.process(
        ticket(
            comment("verdict-" + head, verdict(head, ac="unmet"), author="codex-login"),
            head=head,
            ci={head: CIResult("success")},
        )
    )

    assert actions.merges == []
    assert actions.states == []
    assert ship_intents(queue) == []
    assert state(queue) == ("review", 0)
    assert outcome(queue, "verdict-" + head) == "rejected-approve-ac-unmet"
    wakes = jobs(queue, "grok")
    assert len(wakes) == 1
    assert "ac=unmet" in wakes[0]["detail"]


@pytest.mark.parametrize("ac", ["met", None])
def test_an_ac_met_or_legacy_approve_still_ships(tmp_path: Path, ac: str | None) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    set_floor(queue, 5.0)
    dispatcher.process(
        ticket(
            comment("verdict-" + head, verdict(head, ac=ac), author="codex-login"),
            head=head,
            ci={head: CIResult("success")},
        )
    )

    assert actions.merges == [(True, head)]
    assert [row["head"] for row in ship_intents(queue)] == [head]


def test_a_ready_and_an_unmet_approve_in_one_poll_still_queue_the_review(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 40
    seed_builder_job(queue)
    dispatcher.process(
        ticket(
            comment("ready-1", READY.format(head=head), author="claude-login", created_at=1),
            comment("verdict-1", verdict(head, ac="unmet"), author="codex-login", created_at=2),
            head=head,
            ci={head: CIResult("success")},
        )
    )

    assert outcome(queue, "ready-1") == "queued-review"
    review = queue.claim("codex")
    assert review is not None
    assert (review.request.action, review.request.head) == ("review", head)
    assert json.loads(review.request.detail)["round"] == "1/3"
    assert state(queue) == ("review", 0)


@pytest.mark.parametrize(
    ("refused", "round_", "reported"),
    [
        ("APPROVE", "2", "a" * 40),
        ("REVISE", "2", "a" * 40),
        ("APPROVE", "9", "a" * 40),
        ("REVISE", "1", "b" * 40),
    ],
)
def test_a_refused_verdict_gives_its_review_back_once(
    tmp_path: Path, refused: str, round_: str, reported: str
) -> None:
    dispatcher, queue = setup(tmp_path)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    ready = comment("ready-" + head, READY.format(head=head), author="claude-login")
    ac = "met" if refused == "APPROVE" else None
    stale = comment("verdict-" + head, verdict(reported, verdict=refused, round_=round_, ac=ac), author="codex-login")
    dispatcher.process(ticket(ready, stale, head=head, ci={head: CIResult("success")}))

    assert outcome(queue, stale.id).startswith(("stale-", "rejected-", "refused-"))
    assert state(queue) == ("review", 0)
    wakes = jobs(queue, "grok")
    assert len(wakes) == 1
    assert outcome(queue, stale.id) in wakes[0]["detail"]
    retry = queue.claim("codex")
    assert retry is not None
    assert (retry.request.action, retry.request.head, retry.request.attempt) == ("review", head, 1)
    assert json.loads(retry.request.detail)["round"] == "1/3"

    queue.finish(retry.id, owner="codex", handoff_comment_id="verdict-again")
    again = comment("verdict-again", verdict(reported, verdict=refused, round_=round_, ac=ac), author="codex-login", created_at=2)
    dispatcher.process(ticket(ready, stale, again, head=head, ci={head: CIResult("success")}))

    assert queue.claim("codex") is None
    assert state(queue) == ("review", 0)


HEAD = "a" * 40


def finish_with_edited_verdict(queue, job, comment_id: str = "101"):
    edited = replace(
        comment(comment_id, verdict(HEAD, ac="met", round_="1/3"), author="codex-login", created_at=101),
        edited_at=102,
    )
    data = {
        "issue_url": "https://api.github.com/repos/owner/repo/issues/42",
        "user": {"login": "codex-login"},
        "body": edited.body,
        "updated_at": "2026-11-15T10:02:00Z",
    }

    def read(path):
        return data if "/issues/comments/" in path else {"head": {"ref": "review", "sha": HEAD}}

    assert problems(expected_for(job, "codex-bot"), comment_id, read, {"codex-login": frozenset({"codex-bot"})}) == []
    queue.finish(job.id, owner="codex", handoff_comment_id=edited.id)
    return edited


def review_jobs(queue) -> list[tuple[str, int]]:
    return [(row["state"], row["attempt"]) for row in jobs(queue, "codex")]


def test_first_refusal_after_t6_retry_preserves_review(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    ready = comment("ready-" + HEAD, READY.format(head=HEAD), author="claude-login")
    snapshot = ticket(ready, head=HEAD, ci={HEAD: CIResult("success")})
    dispatcher.process(snapshot)
    first = queue.claim("codex")
    queue.finish(first.id, owner="codex", handoff_comment_id=None)
    dispatcher.reconcile_jobs()
    t6_retry = queue.claim("codex")
    assert t6_retry is not None and t6_retry.request.attempt == 2
    edited = finish_with_edited_verdict(queue, t6_retry)

    dispatcher.process(replace(snapshot, comments=(ready, edited)))

    assert outcome(queue, edited.id) == "rejected-edited"
    assert state(queue) == ("review", 0)
    retry = queue.claim("codex")
    assert retry is not None, review_jobs(queue)
    assert (retry.request.head, json.loads(retry.request.detail)["round"]) == (HEAD, "1/3")


def test_a_refusal_retry_t6_retried_and_refused_again_is_not_requeued(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    seed_builder_job(queue)
    ready = comment("ready-" + HEAD, READY.format(head=HEAD), author="claude-login")
    snapshot = ticket(ready, head=HEAD, ci={HEAD: CIResult("success")})
    dispatcher.process(snapshot)
    first = finish_with_edited_verdict(queue, queue.claim("codex"), "101")
    dispatcher.process(replace(snapshot, comments=(ready, first)))
    refusal_retry = queue.claim("codex")
    assert refusal_retry is not None
    queue.finish(refusal_retry.id, owner="codex", handoff_comment_id=None)
    dispatcher.reconcile_jobs()
    t6_retry = queue.claim("codex")
    assert t6_retry is not None and t6_retry.request.attempt == 2
    second = finish_with_edited_verdict(queue, t6_retry, "102")

    dispatcher.process(replace(snapshot, comments=(ready, first, second)))

    assert outcome(queue, second.id) == "rejected-edited"
    assert queue.claim("codex") is None, review_jobs(queue)
    assert state(queue) == ("review", 0)


def open_fix_and_running_review(tmp_path: Path):
    dispatcher, queue = setup(tmp_path)
    queue.clock = lambda: 1000
    config = replace(factory(), start_before_ci=True)
    seed_builder_job(queue)
    ready = comment("ready-" + HEAD, READY.format(head=HEAD), author="claude-login", created_at=1)
    dispatcher.process(ticket(ready, head=HEAD, factory_config=config, ci={HEAD: CIResult("pending")}))
    review = queue.claim("codex")
    assert review is not None
    newer = comment("ready-conflict", READY.format(head=HEAD), author="claude-login", created_at=2)
    snapshot = replace(ticket(ready, newer, head=HEAD, factory_config=config), mergeable_state="dirty")
    dispatcher.process(snapshot)
    with queue.connect() as connection:
        fix = connection.execute("SELECT id FROM jobs WHERE action = 'fix' AND state = 'queued'").fetchone()
    assert fix is not None
    edited = finish_with_edited_verdict(queue, review)
    snapshot = replace(snapshot, comments=(ready, newer, edited))
    dispatcher.process(snapshot)
    assert outcome(queue, edited.id) == "rejected-edited"
    return dispatcher, queue, snapshot, str(fix["id"])


def test_refusal_retry_waits_for_same_head_fix(tmp_path: Path) -> None:
    _, queue, _, _ = open_fix_and_running_review(tmp_path)

    assert state(queue) == ("review", 0)
    assert queue.claim("codex") is None, review_jobs(queue)


def test_refusal_retry_runs_once_the_fix_keeps_the_head(tmp_path: Path) -> None:
    dispatcher, queue, snapshot, fix_id = open_fix_and_running_review(tmp_path)
    queue.cancel(fix_id, "superseded")

    dispatcher.process(snapshot)

    retry = queue.claim("codex")
    assert retry is not None, review_jobs(queue)
    assert retry.request.head == HEAD


def test_a_review_cancelled_by_a_rejected_verdict_waits_for_same_head_fix(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    queue.clock = lambda: 1000
    config = replace(factory(), start_before_ci=True)
    seed_builder_job(queue)
    ready = comment("ready-" + HEAD, READY.format(head=HEAD), author="claude-login", created_at=1)
    dispatcher.process(ticket(ready, head=HEAD, factory_config=config, ci={HEAD: CIResult("pending")}))
    newer = comment("ready-conflict", READY.format(head=HEAD), author="claude-login", created_at=2)
    conflicted = replace(ticket(ready, newer, head=HEAD, factory_config=config), mergeable_state="dirty")
    dispatcher.process(conflicted)
    with queue.connect() as connection:
        review = connection.execute(
            "SELECT id FROM jobs WHERE action = 'review' AND state = 'queued'"
        ).fetchone()
    queue.cancel(str(review["id"]), "answered-by:101")
    edited = replace(
        comment("101", verdict(HEAD, ac="met", round_="1/3"), author="codex-login", created_at=101),
        edited_at=102,
    )

    dispatcher.process(replace(conflicted, comments=(ready, newer, edited)))

    assert outcome(queue, edited.id) == "rejected-edited"
    assert queue.claim("codex") is None, review_jobs(queue)


REFUSED_APPROVES = [
    ("rejected-approve-no-ac", None, ""),
    ("rejected-approve-open-blocking", "met", "\n1. [blocking] f-2. The ship path skips CI.\n"),
]


@pytest.mark.parametrize(("expected", "ac", "body"), REFUSED_APPROVES)
def test_a_refused_approve_does_not_ship_and_gives_the_review_back(
    tmp_path: Path, expected: str, ac: str | None, body: str
) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    ready = comment("ready-" + head, READY.format(head=head), author="claude-login")
    approve = comment("verdict-" + head, verdict(head, ac=ac, body=body), author="codex-login")
    dispatcher.process(ticket(ready, approve, head=head, ci={head: CIResult("success")}))

    assert actions.merges == []
    assert ship_intents(queue) == []
    assert state(queue) == ("review", 0)
    assert outcome(queue, approve.id) == expected
    wakes = jobs(queue, "grok")
    assert len(wakes) == 1
    detail = json.loads(wakes[0]["detail"])
    assert detail["outcome"] == expected
    assert "verdict=REVISE" in detail["note"]
    retry = queue.claim("codex")
    assert retry is not None
    assert (retry.request.action, retry.request.head, retry.request.attempt) == ("review", head, 1)
    assert json.loads(retry.request.detail)["round"] == "1/3"


def test_a_corrected_approve_after_a_refused_one_ships(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    ready = comment("ready-" + head, READY.format(head=head), author="claude-login")
    refused = comment("verdict-" + head, verdict(head), author="codex-login")
    dispatcher.process(ticket(ready, refused, head=head, ci={head: CIResult("success")}))
    retry = queue.claim("codex")
    assert retry is not None
    queue.finish(retry.id, owner="codex", handoff_comment_id="verdict-2")
    corrected = comment(
        "verdict-2",
        verdict(head, ac="met", body="\n1. [closed] f-2. Fixed.\n"),
        author="codex-login",
        created_at=2,
    )
    dispatcher.process(ticket(ready, refused, corrected, head=head, ci={head: CIResult("success")}))

    assert outcome(queue, refused.id) == "rejected-approve-no-ac"
    assert actions.merges == [(True, head)]
    assert [row["head"] for row in ship_intents(queue)] == [head]


def test_an_approve_posted_before_the_floor_is_not_read_under_the_rule(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
    set_floor(queue, 5.0)
    old = comment(
        "verdict-" + head,
        verdict(head, ac="met", body="\n1. [blocking] f-2. Open.\n"),
        author="codex-login",
        created_at=4,
    )
    dispatcher.process(ticket(old, head=head, ci={head: CIResult("success")}))

    assert actions.merges == [(True, head)]


@pytest.mark.parametrize(("expected", "ac", "body"), REFUSED_APPROVES)
def test_job_done_refuses_the_approve_with_what_to_change(
    expected: str, ac: str | None, body: str
) -> None:
    header = PR_124_HEADER.replace(" ac=unmet", "" if ac is None else f" ac={ac}")
    found = check(header + "\nLooks good.\n" + body)
    assert len(found) == 1
    assert "verdict=REVISE" in found[0]
    assert ("ac=met" if ac is None else "(f-2)") in found[0]


def test_job_done_accepts_an_ac_met_approve_with_only_closed_items() -> None:
    header = PR_124_HEADER.replace(" ac=unmet", " ac=met")
    assert check(header + "\n1. [closed] f-2. Fixed at 6509f58.\n") == []


def test_the_floor_is_zero_on_a_fresh_database_and_now_on_one_in_use(tmp_path: Path) -> None:
    _dispatcher, queue = setup(tmp_path)
    with queue.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT since FROM approve_rule_floor")] == [(0.0,)]
        connection.execute("DROP TABLE approve_rule_floor")
        connection.execute("INSERT INTO handled_comments VALUES ('owner/repo', '42', 'old', 'merged')")
        connection.commit()
    before = time.time()
    setup(tmp_path)
    with queue.connect() as connection:
        (since,) = connection.execute("SELECT since FROM approve_rule_floor").fetchone()
    assert before <= since <= time.time()
