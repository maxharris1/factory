import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, ReviewShipper, RouteError, ShipError, ShipRequest
from lib.handoff import HandoffError, parse_comment
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import comment, factory, setup, ticket
from tests.test_rounds_and_caps import Flow

HEAD = "a" * 40


def _go(extra: str) -> str:
    return (
        "<!-- orch:v1 kind=go from=grok-bot role=orchestrator "
        f"state=building {extra}-->\nGo.\n"
    )


def _outcome(queue: SQLiteJobQueue, comment_id: str) -> str:
    with queue.connect() as connection:
        return connection.execute(
            "SELECT outcome FROM handled_comments WHERE comment_id = ?",
            (comment_id,),
        ).fetchone()[0]


def _ready(builder: str, head: str = HEAD) -> str:
    return (
        f"<!-- orch:v1 kind=ready from={builder}-bot role=builder head={head} -->\n"
        "Ready.\n"
    )


def test_go_header_parses_builder_and_reviewer() -> None:
    fields = parse_comment(_go("builder=codex reviewer=claude ")).as_dict()
    assert (fields["builder"], fields["reviewer"]) == ("codex", "claude")
    with pytest.raises(HandoffError, match="bad reviewer"):
        parse_comment(_go("reviewer=gemini "))


def test_go_naming_builder_and_reviewer_queues_those(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", _go("builder=codex reviewer=claude "), author="grok-login")
    dispatcher.process(ticket(go))

    assert _outcome(queue, "go-1") == "queued-build"
    assert queue.claim("claude") is None
    build = queue.claim("codex")
    assert build is not None and build.request.action == "build"
    detail = json.loads(build.request.detail)
    assert detail["routing"] == {"builder": "codex", "reviewer": "claude"}
    assert detail["reviewer_harness"] == "claude"
    queue.finish(build.id, owner="codex", handoff_comment_id="ready-1")

    ready = comment("ready-1", _ready("codex"), author="codex-login", created_at=2)
    dispatcher.process(ticket(go, ready, head=HEAD, ci={HEAD: CIResult("success")}))

    assert _outcome(queue, "ready-1").startswith("queued-review")
    assert queue.claim("codex") is None
    review = queue.claim("claude")
    assert review is not None and review.request.action == "review"
    assert json.loads(review.request.detail)["reviewer_harness"] == "claude"
    queue.finish(review.id, owner="claude", handoff_comment_id="verdict-1")

    verdict = comment(
        "verdict-1",
        "<!-- orch:v1 kind=verdict from=claude-bot role=reviewer "
        f"round=1/3 head={HEAD} verdict=REVISE risk=high -->\nRevise.\n",
        author="claude-login",
        created_at=3,
    )
    dispatcher.process(
        ticket(go, ready, verdict, head=HEAD, ci={HEAD: CIResult("success")})
    )

    assert _outcome(queue, "verdict-1") == "queued-review-fix"
    assert queue.claim("claude") is None
    fix = queue.claim("codex")
    assert fix is not None and fix.request.action == "fix"
    assert json.loads(fix.request.detail)["routing"] == {
        "builder": "codex",
        "reviewer": "claude",
    }


def test_ready_from_the_factory_builder_is_refused_after_routing(
    tmp_path: Path,
) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", _go("builder=codex reviewer=claude "), author="grok-login")
    dispatcher.process(ticket(go))
    build = queue.claim("codex")
    assert build is not None

    ready = comment("ready-1", _ready("claude"), author="claude-login", created_at=2)
    dispatcher.process(ticket(go, ready, head=HEAD, ci={HEAD: CIResult("success")}))

    assert _outcome(queue, "ready-1").startswith("rejected-")
    assert queue.claim("claude") is None


@pytest.mark.parametrize(
    "extra",
    ["builder=codex reviewer=codex ", "builder=claude reviewer=claude ", "builder=codex "],
    ids=["both-openai", "both-anthropic", "builder-matches-factory-reviewer"],
)
def test_same_family_pair_is_refused(tmp_path: Path, extra: str) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(ticket(comment("go-1", _go(extra), author="grok-login")))

    assert _outcome(queue, "go-1") == "rejected-go-same-family"
    assert queue.claim("claude") is None
    assert queue.claim("codex") is None


def test_unresolved_cursor_family_is_refused(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment("go-1", _go("builder=cursor reviewer=claude "), author="grok-login")
    dispatcher.process(ticket(go))

    assert _outcome(queue, "go-1") == "rejected-go-same-family"
    assert queue.claim("cursor") is None


def test_factory_model_hint_resolves_a_cursor_side(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    config = replace(factory(), reviewer_harness="cursor", reviewer_model="gpt-5")
    go = comment("go-1", _go("builder=claude "), author="grok-login")
    dispatcher.process(ticket(go, factory_config=config))

    assert _outcome(queue, "go-1") == "queued-build"
    build = queue.claim("claude")
    assert build is not None
    assert json.loads(build.request.detail)["reviewer_harness"] == "cursor"


def test_go_without_routing_keeps_factory_harnesses(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    dispatcher.process(ticket(comment("go-1", _go(""), author="grok-login")))

    build = queue.claim("claude")
    assert build is not None
    detail = json.loads(build.request.detail)
    assert "routing" not in detail
    assert detail["reviewer_harness"] == "codex"


@pytest.mark.parametrize(
    ("extra", "outcome", "recipient", "routing"),
    [
        (
            "builder=codex reviewer=claude ",
            "queued-fix",
            "codex",
            {"builder": "codex", "reviewer": "claude"},
        ),
        ("builder=codex reviewer=codex ", "rejected-go-same-family", None, None),
    ],
    ids=["cross-family", "same-family"],
)
def test_go_on_a_capped_ticket_is_routed(
    tmp_path: Path,
    extra: str,
    outcome: str,
    recipient: str | None,
    routing: dict[str, str] | None,
) -> None:
    flow = Flow(tmp_path)
    flow.to_cap()

    assert flow.post("resume", _go(extra), "grok-login") == outcome
    with flow.queue.connect() as connection:
        row = connection.execute(
            "SELECT recipient, detail FROM jobs "
            "WHERE source_comment_id = 'resume' AND action = 'fix'"
        ).fetchone()
    if recipient is None:
        assert row is None
        assert flow.state()["workflow_state"] == "capped"
        return
    assert row["recipient"] == recipient
    detail = json.loads(row["detail"])
    assert detail["routing"] == routing
    assert detail["reviewer_harness"] == routing["reviewer"]


def _drifted(tmp_path: Path):
    """A ticket routed to claude / cursor(gpt-5) whose factory.md then moves
    to codex / cursor(claude-sonnet-4): each config is cross-family, the
    restored claude / cursor(claude-sonnet-4) pair is not."""
    dispatcher, queue = setup(tmp_path)
    old = replace(factory(), reviewer_harness="cursor", reviewer_model="gpt-5")
    new = replace(old, builder_harness="codex", reviewer_model="claude-sonnet-4")
    go = comment("go-1", _go("builder=claude "), author="grok-login")
    dispatcher.process(ticket(go, factory_config=old))
    build = queue.claim("claude")
    assert build is not None
    queue.finish(build.id, owner="claude", handoff_comment_id="ready-1")
    ready = comment("ready-1", _ready("claude"), author="claude-login", created_at=2)
    return dispatcher, queue, go, ready, old, new


def _is_stuck(queue: SQLiteJobQueue) -> bool:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT stuck, stuck_reason FROM ticket_state WHERE ticket_id = '42'"
        ).fetchone()
    return bool(row["stuck"]) and row["stuck_reason"] == "route-same-family"


def test_restored_same_family_pair_queues_no_review(tmp_path: Path) -> None:
    dispatcher, queue, go, ready, _, new = _drifted(tmp_path)

    dispatcher.process(
        ticket(go, ready, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=new)
    )

    assert queue.claim("cursor") is None
    assert _is_stuck(queue)
    wake = queue.claim("grok")
    assert wake is not None and wake.request.action == "stuck"


def test_restored_same_family_pair_does_not_ship(tmp_path: Path) -> None:
    dispatcher, queue, go, ready, old, new = _drifted(tmp_path)
    dispatcher.process(
        ticket(go, ready, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=old)
    )
    review = queue.claim("cursor")
    assert review is not None
    queue.finish(review.id, owner="cursor", handoff_comment_id="verdict-1")
    dispatcher.trusted_logins["github"]["cursor-login"] = frozenset({"cursor-bot"})
    verdict = comment(
        "verdict-1",
        "<!-- orch:v1 kind=verdict from=cursor-bot role=reviewer "
        f"round=1/3 head={HEAD} verdict=APPROVE risk=high -->\nApproved.\n",
        author="cursor-login",
        created_at=3,
    )

    dispatcher.process(
        ticket(
            go, ready, verdict, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=new
        )
    )

    assert dispatcher.review_actions.merges == []
    assert _is_stuck(queue)


def test_restored_same_family_pair_is_refused_by_the_shipper(tmp_path: Path) -> None:
    dispatcher, _, go, ready, _, new = _drifted(tmp_path)
    fresh = ticket(go, ready, head=HEAD, ci={HEAD: CIResult("success")}, factory_config=new)

    with pytest.raises(RouteError, match="same-family"):
        dispatcher._routed(fresh)
    with dispatcher.queue.connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO tracked_tickets VALUES (?, ?, 'github', 0)",
            (fresh.repo.lower(), fresh.ticket_id),
        )

    class Source:
        def tracked_tickets(self, wanted):
            return (fresh,)

    request = ShipRequest(fresh.repo, fresh.ticket_id, HEAD, "max", "ok-1", "verdict-1")
    with pytest.raises(ShipError, match="same-family"):
        ReviewShipper(dispatcher, Source())._ticket(request)
