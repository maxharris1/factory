"""#173: an approver's plain `/go` releases a capped or waiting-max ticket."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.approver_commands import SlashGo, parse_slash_go
from lib.dispatcher import Comment
from tests.test_approver_credential import APP_BOT, MAX, AppFlow, grok_wakes
from tests.test_dispatcher import QUESTIONS, VERDICT, job_count
from tests.test_rounds_and_caps import HEADS


def jobs(flow: AppFlow) -> list[tuple[str, str, str]]:
    with flow.queue.connect() as connection:
        return [
            (row["action"], row["recipient"], row["source_comment_id"])
            for row in connection.execute(
                "SELECT action, recipient, source_comment_id FROM jobs ORDER BY rowid"
            )
        ]


def to_waiting_max(flow: AppFlow) -> None:
    assert flow.go("go-1") == "queued-build"
    assert flow.post("q-1", QUESTIONS, "claude-login") == "waiting-max"
    assert flow.state()["workflow_state"] == "waiting-max"


@pytest.mark.parametrize(
    ("body", "parsed"),
    [
        ("/go", SlashGo(None)),
        ("  /go  \n", SlashGo(None)),
        ("/GO\n\nrewrite ac-3 as: the CLI exits 0\n", SlashGo("rewrite ac-3 as: the CLI exits 0")),
        ("\n/Go\nkeep the parser strict", SlashGo("keep the parser strict")),
    ],
)
def test_parse_slash_go_accepts_go_and_one_instruction_line(body: str, parsed: SlashGo) -> None:
    assert parse_slash_go(body) == parsed


@pytest.mark.parametrize(
    "body",
    [
        "",
        "please /go",
        "/go now please",
        "/goo",
        "/go\none\ntwo",
        "I think we should /go\non this",
        "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\n/go",
    ],
)
def test_parse_slash_go_refuses_chat(body: str) -> None:
    assert parse_slash_go(body) is None


@pytest.mark.parametrize("enabled", [True, False, None])
def test_slash_go_from_max_on_a_capped_ticket_queues_the_round(
    tmp_path: Path, enabled: bool | None
) -> None:
    flow = AppFlow(tmp_path, enabled=enabled)
    flow.to_cap()
    fixes = job_count(flow.queue, "fix")

    assert flow.post("slash-1", "/go", MAX) == "queued-fix"

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    assert job_count(flow.queue, "fix") == fixes + 1
    assert grok_wakes(flow, "go-not-from-approver") == []
    before = jobs(flow)
    flow.dispatcher.process(flow.snapshot())
    assert jobs(flow) == before
    assert flow.state()["cap_extra"] == 1


def test_slash_go_instruction_reaches_the_fix_evidence(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()

    assert flow.post("slash-1", "/go\nTry the smaller parser.", MAX) == "queued-fix"

    job = flow.queue.claim("claude")
    assert job is not None and job.request.source_comment_id == "slash-1"
    instructions = json.loads(job.request.detail)["evidence"]["instructions"]
    assert (instructions["cause"], instructions["prose"]) == ("extra-round", "Try the smaller parser.")


def test_slash_go_from_max_releases_a_waiting_max_ticket(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_waiting_max(flow)
    builds = job_count(flow.queue, "build")

    assert flow.post("slash-1", "/go\nUse the default.", MAX) == "resumed-build"

    state = flow.state()
    assert state["workflow_state"] == "building"
    assert (state["paused_action"], state["paused_question_id"]) == (None, None)
    assert job_count(flow.queue, "build") == builds + 1
    before = jobs(flow)
    flow.dispatcher.process(flow.snapshot())
    assert jobs(flow) == before


def revise(flow: AppFlow, round_text: str, comment_id: str, extra: str = "") -> str:
    body = VERDICT.format(round=round_text, head=flow.head, verdict="REVISE", risk="high")
    body = body.replace(" -->", f"{extra} -->", 1)
    return flow.post(comment_id, body + "- [blocking] f-1: the parser accepts prose.\n", "codex-login")


@pytest.mark.xfail(strict=True, reason="#189: T16c records the repeat block as waiting-max")
def test_slash_go_releases_a_repeat_block(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")
    revise(flow, "1/3", "revise-1")
    flow.finish("claude")
    assert flow.ready(HEADS[2]) == "queued-review"
    flow.finish("codex")
    revise(flow, "2/3", "revise-2", " repeat=f-1")
    assert flow.state()["workflow_state"] == "waiting-max"

    assert flow.post("slash-1", "/go", MAX).startswith("resumed-")
    assert flow.state()["workflow_state"] != "waiting-max"


@pytest.mark.xfail(strict=True, reason="#190: T16d records an unsatisfiable criterion as waiting-max")
def test_slash_go_releases_an_unsatisfiable_criterion(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    assert flow.go("go-1") == "queued-build"
    flow.finish("claude")
    assert flow.ready(HEADS[1]) == "queued-review"
    flow.finish("codex")
    revise(flow, "1/3", "revise-1", " unsat=ac-1")
    assert flow.state()["workflow_state"] == "waiting-max"

    assert flow.post("slash-1", "/go\nac-1 now reads: the CLI exits 0.", MAX).startswith("resumed-")
    assert flow.state()["workflow_state"] != "waiting-max"


@pytest.mark.parametrize(
    ("enabled", "author"),
    [(True, APP_BOT), (False, "grok-login"), (True, "grok-login"), (True, "claude-login"), (False, "codex-login")],
)
def test_slash_go_from_a_bot_on_a_capped_ticket_wakes_factory(
    tmp_path: Path, enabled: bool, author: str
) -> None:
    flow = AppFlow(tmp_path, enabled=enabled)
    flow.to_cap()
    before = jobs(flow)

    assert flow.post("slash-1", "/go", author) == "go-not-from-approver"
    flow.dispatcher.process(flow.snapshot())

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)
    wakes = grok_wakes(flow, "go-not-from-approver")
    assert [row["source_comment_id"] for row in wakes] == ["slash-1"]
    assert [job for job in jobs(flow) if job not in before] == [("go-not-from-approver", "grok", "slash-1")]


def test_slash_go_from_a_bot_on_a_waiting_max_ticket_keeps_it_waiting(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    to_waiting_max(flow)

    assert flow.post("slash-1", "/go", "claude-login") == "go-not-from-approver"

    assert flow.state()["workflow_state"] == "waiting-max"
    assert flow.state()["paused_action"] == "build"
    assert [row["source_comment_id"] for row in grok_wakes(flow, "go-not-from-approver")] == ["slash-1"]


def test_slash_go_from_a_stranger_is_ignored_without_a_wake(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    before = jobs(flow)

    assert flow.post("slash-1", "/go", "drive-by") == "ignored-slash-go-author"

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)
    assert jobs(flow) == before


@pytest.mark.parametrize("state", ["building", "review", "fixing", "handed-off", "merge-queue", "blocked"])
def test_slash_go_on_a_ticket_with_no_target_is_ignored(tmp_path: Path, state: str) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    assert flow.go("go-1") == "queued-build"
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET workflow_state = ?", (state,))
    before = jobs(flow)

    assert flow.post("slash-1", "/go", MAX) == "slash-go-no-target"

    assert flow.state()["workflow_state"] == state
    assert jobs(flow) == before


def test_slash_go_on_a_ticket_with_no_state_row_is_ignored(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)

    assert flow.post("slash-1", "/go", MAX) == "slash-go-no-target"

    assert jobs(flow) == []


def test_slash_go_on_a_stuck_ticket_is_left_to_retry(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    with flow.queue.connect() as connection:
        connection.execute("UPDATE ticket_state SET stuck = 1")
    fixes = job_count(flow.queue, "fix")

    assert flow.post("slash-1", "/go", MAX) == "ticket-stuck"

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)
    assert job_count(flow.queue, "fix") == fixes


def test_an_edited_slash_go_is_refused(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    fixes = job_count(flow.queue, "fix")
    flow.now[0] += 10
    flow.comments.append(
        Comment(id="slash-1", author=MAX, body="/go", created_at=flow.now[0], edited_at=flow.now[0] + 1)
    )
    flow.dispatcher.process(flow.snapshot())

    assert flow.dispatcher._handled_outcome(flow.snapshot(), "slash-1") == "rejected-edited"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)
    assert job_count(flow.queue, "fix") == fixes


def test_prose_mentioning_slash_go_changes_nothing(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    before = jobs(flow)

    flow.now[0] += 10
    flow.comments.append(Comment(id="chat-1", author=MAX, body="Should I /go on this one?", created_at=flow.now[0]))
    flow.dispatcher.process(flow.snapshot())

    assert flow.dispatcher._handled_outcome(flow.snapshot(), "chat-1") is None

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)
    assert jobs(flow) == before
