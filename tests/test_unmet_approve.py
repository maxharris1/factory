from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, Dispatcher
from lib.handoff import HandoffError, parse_comment
from lib.handoff_check import Expected, problems
from lib.hook_t7 import T7Hook
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    READY,
    VERDICT,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    comment,
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


def verdict(head: str, *, verdict: str = "APPROVE", round_: str = "1", ac: str | None = None) -> str:
    body = VERDICT.format(round=round_, head=head, verdict=verdict, risk="high")
    if ac is None:
        return body
    return body.replace(f" verdict={verdict}", f" ac={ac} verdict={verdict}")


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
            "body": PR_124_HEADER + "\nLooks good.\n",
        }
    }.__getitem__
    found = problems(expected, "101", read, {"codex-login": frozenset({"codex-bot"})})
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
    assert outcome(queue, "verdict-" + head) == "woke-grok-unparsed"
    wakes = jobs(queue, "grok")
    assert len(wakes) == 1
    assert "ac=unmet" in wakes[0]["detail"]


@pytest.mark.parametrize("ac", ["met", None])
def test_an_ac_met_or_legacy_approve_still_ships(tmp_path: Path, ac: str | None) -> None:
    actions = FakeReviewActions()
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    head = "a" * 40
    seed_review_job(dispatcher, queue, head)
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
    stale = comment("verdict-" + head, verdict(reported, verdict=refused, round_=round_), author="codex-login")
    dispatcher.process(ticket(ready, stale, head=head, ci={head: CIResult("success")}))

    assert outcome(queue, stale.id).startswith(("stale-", "rejected-", "refused-"))
    assert state(queue) == ("review", 0)
    wakes = jobs(queue, "grok")
    assert len(wakes) == 1
    assert outcome(queue, stale.id) in wakes[0]["detail"]
    retry = queue.claim("codex")
    assert retry is not None
    assert (retry.request.action, retry.request.head, retry.request.attempt) == ("review", head, 2)
    assert json.loads(retry.request.detail)["round"] == "1/3"

    queue.finish(retry.id, owner="codex", handoff_comment_id="verdict-again")
    again = comment("verdict-again", verdict(reported, verdict=refused, round_=round_), author="codex-login", created_at=2)
    dispatcher.process(ticket(ready, stale, again, head=head, ci={head: CIResult("success")}))

    assert queue.claim("codex") is None
    assert state(queue) == ("review", 0)
