import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, ReviewShipper, RouteError, ShipError, ShipRequest
from lib.handoff import HandoffError, parse_comment
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import comment, factory, setup, ticket
from tests.merge_queue_support import Board, sha
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
        f"round=1/3 head={HEAD} ac=met verdict=APPROVE risk=high -->\nApproved.\n",
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


def test_old_builder_retry_does_not_replace_new_plan_route(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    old_go = comment(
        "old-go", _go("builder=codex reviewer=claude "), author="grok-login", created_at=100
    )
    dispatcher.process(ticket(old_go))
    old_build = queue.claim("codex")
    assert old_build is not None
    now[0] = 110
    new_go = comment(
        "new-go",
        _go("builder=claude reviewer=codex ").replace("state=building", "state=planning"),
        author="grok-login",
        created_at=110,
    )
    dispatcher.process(ticket(old_go, new_go))
    assert _outcome(queue, "new-go") == "queued-plan"
    plan = queue.claim("claude")
    assert plan is not None and plan.request.action == "plan"
    now[0] = 120
    queue.finish(old_build.id, owner="codex", handoff_comment_id=None)
    dispatcher.reconcile_jobs()
    queue.finish(plan.id, owner="claude", handoff_comment_id="new-plan")
    now[0] = 130
    plan_comment = comment(
        "new-plan",
        "<!-- orch:v1 kind=plan from=claude-bot role=builder round=1/2 ac=met -->\nEstimate: 40\nGood",
        author="claude-login",
        created_at=130,
    )
    dispatcher.process(ticket(old_go, new_go, plan_comment))
    build = queue.claim("claude")
    assert build is not None and build.request.action == "build"
    assert json.loads(build.request.detail)["routing"] == {
        "builder": "claude",
        "reviewer": "codex",
    }
    queue.finish(build.id, owner="claude", handoff_comment_id="new-ready")
    now[0] = 140
    ready = comment("new-ready", _ready("claude"), author="claude-login", created_at=140)
    dispatcher.process(
        ticket(old_go, new_go, plan_comment, ready, head=HEAD, ci={HEAD: CIResult("success")})
    )

    with queue.connect() as connection:
        row = connection.execute(
            "SELECT recipient FROM jobs WHERE action = 'review' "
            "AND source_comment_id = 'new-ready'"
        ).fetchone()
    assert row is not None and row["recipient"] == "codex"


def test_obsolete_retry_created_last_cannot_build_or_review(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    board.post(pr, "old-go", _go("builder=codex reviewer=claude "), "grok-login")
    board.poll()
    old = board.claim("codex")
    board.post(
        pr,
        "new-go",
        _go("builder=claude reviewer=codex ").replace("state=building", "state=planning"),
        "grok-login",
    )
    board.poll()
    board.finish("claude")
    board.post(
        pr,
        "new-plan",
        "<!-- orch:v1 kind=plan from=claude-bot role=builder round=1/2 ac=met -->\nEstimate: 40\nGood",
        "claude-login",
    )
    board.poll()
    board.finish("claude")
    board.queue.finish(old.id, owner="codex", handoff_comment_id=None)
    board.poll()
    board.ready(pr, pr.head, "new-ready")
    board.poll()
    assert _outcome(board.queue, "new-ready") == "queued-review"
    review = board.claim("codex")
    assert review.request.action == "review" and review.request.source_comment_id == "new-ready"
    retry = board.claim("codex")
    assert retry.request.action == "build" and retry.request.attempt == 2
    board.queue.finish(retry.id, owner="codex", handoff_comment_id="obsolete-ready")
    pr.push(sha(2))
    board.post(pr, "obsolete-ready", _ready("codex", pr.head), "codex-login")
    board.poll()

    assert _outcome(board.queue, "obsolete-ready") == "rejected-ready-author"
    with board.queue.connect() as connection:
        reviews = connection.execute(
            "SELECT recipient FROM jobs WHERE action = 'review' "
            "AND source_comment_id = 'obsolete-ready'"
        ).fetchall()
    assert reviews == []
    assert not pr.merged


def _routed_board(tmp_path: Path, *, conflict: bool):
    board = Board(tmp_path)
    board.factory = replace(board.factory, reviewer_harness="cursor", reviewer_model="gpt-5")
    board.dispatcher.trusted_logins["github"]["cursor-login"] = frozenset({"cursor-bot"})
    pr = board.open("42", sha(1))
    if conflict:
        pr.mergeable = False
        pr.mergeable_state = "dirty"
    board.post(
        pr,
        "go-route",
        "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building "
        "builder=claude -->\nGo.",
        "grok-login",
    )
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    return board, pr


def _drift(board) -> None:
    board.factory = replace(
        board.factory, builder_harness="codex", reviewer_model="claude-sonnet-4"
    )


def test_route_rejection_cancels_an_already_queued_job(tmp_path: Path) -> None:
    board, _ = _routed_board(tmp_path, conflict=False)
    assert board.jobs("review")[0]["state"] == "queued"
    _drift(board)

    board.poll()

    assert board.state("42")["stuck_reason"] == "route-same-family"
    assert board.jobs("review")[0]["state"] == "cancelled"
    assert board.queue.claim("cursor") is None


def test_closed_routed_pr_with_drift_still_ends_watch(tmp_path: Path) -> None:
    board, pr = _routed_board(tmp_path, conflict=True)
    board.finish("cursor")
    board.post(
        pr,
        "approve-route",
        "<!-- orch:v1 kind=verdict from=cursor-bot role=reviewer round=1/3 "
        f"head={pr.head} ac=met verdict=APPROVE risk=high -->\nApproved.",
        "cursor-login",
    )
    board.poll()
    assert board.state("42")["workflow_state"] == "updating"
    assert board.jobs("update-from-main")[0]["state"] == "queued"
    _drift(board)
    pr.closed = True

    board.poll()

    assert board.state("42")["workflow_state"] == "abandoned"
    assert board.jobs("update-from-main")[0]["state"] == "cancelled"
    assert board.queue.claim("claude") is None


def _plan(builder: str) -> str:
    return (
        f"<!-- orch:v1 kind=plan from={builder}-bot role=builder round=1/2 ac=met -->\n"
        "Estimate: 40\n"
        "Good.\n"
    )


def test_late_plan_retry_does_not_take_over_the_current_plan(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = board.open("42", sha(1))
    pr.mergeable = pr.mergeable_state = None
    planning = "state=planning"
    board.post(
        pr,
        "old-go",
        _go("builder=claude reviewer=codex ").replace("state=building", planning),
        "grok-login",
    )
    board.poll()
    old = board.claim("claude")
    board.post(pr, "build-go", _go("builder=codex reviewer=claude "), "grok-login")
    board.poll()
    board.finish("codex")
    board.post(pr, "current-ready", _ready("codex", pr.head), "codex-login")
    board.poll()
    board.finish("claude")
    board.post(
        pr,
        "approve",
        "<!-- orch:v1 kind=verdict from=claude-bot role=reviewer round=1/3 "
        f"head={pr.head} ac=met verdict=APPROVE risk=high -->\nApproved.",
        "claude-login",
    )
    board.poll()
    assert board.state("42")["workflow_state"] == "merge-queue"
    board.post(
        pr,
        "new-go",
        _go("builder=codex reviewer=claude ").replace("state=building", planning),
        "grok-login",
    )
    board.poll()
    current = board.claim("codex")
    assert current.request.action == "plan"
    board.queue.finish(old.id, owner="claude", handoff_comment_id=None)
    board.poll()
    board.queue.finish(current.id, owner="codex", handoff_comment_id="current-plan")
    board.post(pr, "current-plan", _plan("codex"), "codex-login")
    board.poll()

    assert board.outcome("current-plan") == "queued-plan-build"
    build = next(
        row for row in board.jobs("build") if row["source_comment_id"] == "current-plan"
    )
    assert build["recipient"] == "codex"
    assert json.loads(build["detail"])["reviewer_harness"] == "claude"

    retry = board.claim("claude")
    assert retry.request.action == "plan" and retry.request.attempt == 2
    board.queue.finish(retry.id, owner="claude", handoff_comment_id="obsolete-plan")
    board.post(pr, "obsolete-plan", _plan("claude"), "claude-login")
    board.poll()

    assert board.outcome("obsolete-plan") == "rejected-plan-author"
    assert not any(
        row["source_comment_id"] == "obsolete-plan" for row in board.jobs("build")
    )


def test_answer_after_a_late_retry_resumes_the_current_step(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    old_go = comment(
        "old-go", _go("builder=claude reviewer=codex "), author="grok-login", created_at=100
    )
    dispatcher.process(ticket(old_go))
    old = queue.claim("claude")
    now[0] = 110
    new_go = comment(
        "new-go",
        _go("builder=codex reviewer=claude ").replace("state=building", "state=planning"),
        author="grok-login",
        created_at=110,
    )
    dispatcher.process(ticket(old_go, new_go))
    assert queue.claim("codex").request.action == "plan"
    now[0] = 120
    queue.finish(old.id, owner="claude", handoff_comment_id=None)
    dispatcher.reconcile_jobs()
    now[0] = 130
    question = comment(
        "question",
        "<!-- orch:v1 kind=questions from=codex-bot role=builder -->\n"
        "Which option should I use?",
        author="codex-login",
        created_at=130,
    )
    dispatcher.process(ticket(old_go, new_go, question))
    now[0] = 140
    answer = comment(
        "answer",
        "<!-- orch:v1 kind=answers from=grok-bot role=orchestrator -->\nUse A.",
        author="grok-login",
        created_at=140,
    )
    dispatcher.process(ticket(old_go, new_go, question, answer))

    with queue.connect() as connection:
        row = connection.execute(
            "SELECT action, recipient, detail FROM jobs WHERE source_comment_id = 'answer'"
        ).fetchone()
    assert (row["action"], row["recipient"]) == ("plan", "codex")
    detail = json.loads(row["detail"])
    assert detail["reviewer_harness"] == "claude"
    assert detail["routing"] == {"builder": "codex", "reviewer": "claude"}
