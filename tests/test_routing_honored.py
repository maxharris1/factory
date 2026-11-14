import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult
from lib.handoff import HandoffError, parse_comment
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import comment, factory, setup, ticket

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
