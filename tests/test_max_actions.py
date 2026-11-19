"""lib/max_actions.py and the comments deploy/factory-dashboard-http.py posts
for those actions (#822). The server is one standard-library file and keeps
its own copy of the action table; these tests hold the two together and
replay the server's bodies through the real dispatcher as maxharris1."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.dispatcher import Dispatcher
from lib.handoff import parse_comment
from lib.max_actions import ACTIONS, applicable, may_hand_off, wait_of
from lib.orch_yml import Approvals
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    QUESTIONS,
    RETRY_HEAD,
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    RaisingGrokDelivery,
    comment,
    fail_twice_through_t6,
    handled_outcome,
    job_count,
    seed_builder_job,
    seed_review_job,
    seed_state,
    ticket,
    ticket_row,
    workflow_state,
)

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "deploy" / "factory-dashboard-http.py"
spec = importlib.util.spec_from_file_location("factory_dashboard_http_actions", SERVER_PATH)
assert spec is not None and spec.loader is not None
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

HEAD = "a" * 40
AT = datetime(2026, 11, 18, 20, 4, tzinfo=timezone.utc)


PAUSED_BUILD = {"paused_action": "build"}


@pytest.mark.parametrize("state,stuck,facts,paused,actions", [
    ("awaiting-ok", False, {"approve_comment_id": "5"}, {}, ["ok", "close", "hand-off"]),
    ("waiting-max", False, {"parked_outcome": "waiting-max-size", "question_id": "7"}, PAUSED_BUILD,
     ["approve-plan", "split", "close", "hand-off"]),
    ("waiting-max", False, {"parked_outcome": "waiting-max-repeat"}, {"paused_action": "fix"},
     ["another-round", "close", "hand-off"]),
    ("waiting-max", False, {"parked_outcome": "waiting-max-unsat"}, {"paused_action": "review"},
     ["another-round", "close", "hand-off"]),
    ("waiting-max", False, {"question_id": "7"}, PAUSED_BUILD, ["answer", "close", "hand-off"]),
    ("waiting-max", False, {"question_id": "7"}, {"paused_action": "clarify"}, ["answer", "close"]),
    ("waiting-max", False, {"question_id": "7"}, {}, ["answer", "close"]),
    ("capped", False, {}, {}, ["another-round", "close", "hand-off"]),
    ("blocked", False, {}, {}, ["resume", "close", "hand-off"]),
    ("fixing", True, {"stuck_reason": "job-failed-2"}, {}, ["retry", "close", "hand-off"]),
    ("building", True, {"stuck_reason": "ci-red-3"}, {}, ["retry", "close", "hand-off"]),
    ("waiting-max", True, {"stuck_reason": "job-failed-2"}, PAUSED_BUILD, ["retry", "close", "hand-off"]),
    ("questions", True, {"stuck_reason": "job-failed-2"}, {}, ["retry", "close"]),
    ("waiting-external", False, {}, {}, ["close", "hand-off"]),
    ("awaiting-ok", False, {"approve_comment_id": "5"}, {"running": True}, ["ok", "close"]),
    ("fixing", True, {"stuck_reason": "job-failed-2"}, {"running": True}, ["retry", "close"]),
    ("building", False, {}, {}, []),
])
def test_each_waiting_state_offers_exactly_its_actions(state, stuck, facts, paused, actions) -> None:
    hand_off = may_hand_off(state, stuck, paused.get("paused_action"), paused.get("running", False))
    assert applicable(wait_of(state, stuck, **facts), hand_off=hand_off) == actions


def test_the_server_knows_exactly_the_action_names() -> None:
    assert set(server.ACTIONS) == set(ACTIONS)


def test_the_server_binds_the_same_actions_to_a_head() -> None:
    assert {name for name, item in server.ACTIONS.items() if item.head_bound} == {
        name for name, item in ACTIONS.items() if item.head_bound
    }


GO = {"kind": "go", "from": "grok-bot", "role": "orchestrator", "state": "building"}
ANSWERS = {"kind": "answers", "from": "grok-bot", "role": "orchestrator"}
CLOSE = {**ANSWERS, "state": "abandoned"}


def body(action: str, words: str = "Ship it.", *, repo: str = "owner/repo",
         ticket_id: str = "42", number: int = 42, head: str | None = None,
         plan_comment_id: str | None = None) -> str:
    facts = server.ActFacts(repo, ticket_id, number, head, plan_comment_id,
                            server.words_block(words, AT))
    return server.ACTIONS[action].body(facts)


@pytest.mark.parametrize("action,fields", [
    ("ok", {"kind": "ok", "head": HEAD}),
    ("approve-plan", GO),
    ("split", CLOSE),
    ("another-round", GO),
    ("answer", ANSWERS),
    ("retry", {"kind": "retry", "from": "grok-bot", "role": "orchestrator"}),
    ("resume", GO),
    ("close", CLOSE),
    ("hand-off", {**ANSWERS, "state": "handed-off"}),
])
def test_each_body_parses_to_its_header(action: str, fields: dict) -> None:
    assert parse_comment(body(action, head=HEAD, plan_comment_id="9")).as_dict() == fields


def test_the_server_never_reads_the_dispatcher_database() -> None:
    text = SERVER_PATH.read_text(encoding="utf-8")
    assert "import sqlite3" not in text and "orch.db" not in text


MAX_TRUST = {
    **TRUSTED_LOGINS,
    "github": {**TRUSTED_LOGINS["github"], "maxharris1": frozenset({"grok-bot"})},
}


def max_dispatcher(tmp_path: Path, delivery: FakeGrokDelivery | None = None):
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(path, queue, trusted_logins=MAX_TRUST,
                            review_actions=FakeReviewActions(),
                            grok_delivery=delivery or FakeGrokDelivery(),
                            approvals=Approvals(logins=frozenset({"maxharris1"})))
    return dispatcher, queue


def test_max_closing_from_the_dashboard_abandons_the_ticket(tmp_path: Path) -> None:
    dispatcher, queue = max_dispatcher(tmp_path)
    seed_builder_job(queue)

    dispatcher.process(ticket(comment("close-1", body("close", "Not worth it."),
                                      author="maxharris1", created_at=9)))

    assert (workflow_state(queue), handled_outcome(queue, "close-1")) == (
        "abandoned", "abandoned")


def test_max_handing_off_from_the_dashboard_parks_a_capped_ticket(tmp_path: Path) -> None:
    dispatcher, queue = max_dispatcher(tmp_path)
    seed_state(queue, state="capped")

    dispatcher.process(ticket(comment("hand-off-1", body("hand-off", "I'll QA it myself."),
                                      author="maxharris1", created_at=9)))

    assert (workflow_state(queue), handled_outcome(queue, "hand-off-1")) == (
        "handed-off", "handed-off")


def test_max_splitting_from_the_dashboard_abandons_the_ticket(tmp_path: Path) -> None:
    dispatcher, queue = max_dispatcher(tmp_path)
    seed_builder_job(queue)

    dispatcher.process(ticket(comment("split-1", body("split", plan_comment_id="9"),
                                      author="maxharris1", created_at=9)))

    assert workflow_state(queue) == "abandoned"


def test_max_answering_from_the_dashboard_resumes_a_waiting_question(tmp_path: Path) -> None:
    dispatcher, queue = max_dispatcher(tmp_path)
    seed_review_job(dispatcher, queue, HEAD)
    question = comment("questions-1", QUESTIONS, author="claude-login", created_at=1)
    dispatcher.process(ticket(question, head=HEAD))
    assert workflow_state(queue) == "waiting-max"

    answer = comment("answer-1", body("answer", "Use the house font."),
                     author="maxharris1", created_at=2)
    dispatcher.process(ticket(question, answer, head=HEAD))

    assert handled_outcome(queue, "answer-1") == "resumed-review"
    assert job_count(queue, "review") == 2


def test_max_requeueing_from_the_dashboard_retries_a_failed_job(tmp_path: Path) -> None:
    dispatcher, queue = max_dispatcher(tmp_path, RaisingGrokDelivery(RuntimeError("down")))
    seed_state(queue, state="fixing")
    fail_twice_through_t6(dispatcher, queue, action="fix", head=RETRY_HEAD)
    assert ticket_row(queue)["stuck_reason"] == "job-failed-2"

    retry = comment("retry-1", body("retry", "The runner was down."),
                    author="maxharris1", created_at=50)
    dispatcher.process(ticket(retry, head=RETRY_HEAD))

    assert handled_outcome(queue, "retry-1") == "queued-retry-fix"
    assert (ticket_row(queue)["stuck"], workflow_state(queue)) == (0, "fixing")
