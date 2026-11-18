"""Lessons batch 1, item 1: the post-APPROVE Linear Ready for Review move.

Max's standing rule is that the factory may move approved Aut0 tickets to
Ready for Review, with no state exception, so an issue still in Backlog or
Triage moves too. Completed and canceled issues are still left alone.

#48 G2 F2.2: on a team with no Ready for Review state the move cannot be
made. The PR is still marked ready, the handoff completes, and the done
wake reports that the Linear issue was not moved. It is not a ship error.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import CIResult, Dispatcher, TrackedTicket
from lib.github_actions import GhResult, LinearReviewActions
from lib.poll_source import (
    HTTPResult,
    LivePollSource,
    _linear_ready_state_id,
    _linear_state_phase,
)
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    VERDICT,
    FakeGrokDelivery,
    comment,
    factory,
    seed_review_job,
)
from tests.test_github_actions import (
    HEAD,
    MARK_READY,
    PULL,
    RecordedGh,
    draft_pull,
    pr,
    served,
)
from tests.test_live_poll import RFR, config, phase_issue


pytestmark = pytest.mark.usefixtures("every_wake_reaches_grok")

NO_READY_NOTE = "linear-not-moved: team has no Ready for Review state"


@pytest.mark.parametrize(
    ("issue", "phase"),
    [
        (phase_issue({"id": "b", "type": "backlog", "position": 0.0}), "before-review"),
        (phase_issue({"id": "t", "type": "triage", "position": 0.0}), "before-review"),
        (phase_issue({"id": "b", "type": "backlog"}), "before-review"),
        (
            phase_issue({"id": "b", "type": "backlog", "position": 0.0}, ready=None),
            "no-ready-state",
        ),
        (
            phase_issue({"id": "t", "type": "triage", "position": 0.0}, ready=None),
            "no-ready-state",
        ),
        (
            phase_issue({"id": "todo", "type": "unstarted", "position": 1.0}, ready=None),
            "no-ready-state",
        ),
        (phase_issue({"id": "doing", "type": "started"}, ready=None), "no-ready-state"),
        (
            phase_issue({"id": "done", "type": "completed", "position": 5.0}, ready=None),
            "moved-on",
        ),
        (
            phase_issue({"id": "x", "type": "canceled", "position": 6.0}, ready=None),
            "moved-on",
        ),
        ({"state": {"id": "b", "type": "backlog", "position": 0.0}}, None),
        ({"state": {"id": "b", "type": "backlog"}, "team": None}, None),
        (phase_issue({"id": "m", "type": "mystery"}, ready=None), None),
    ],
    ids=[
        "backlog",
        "triage",
        "backlog-no-position",
        "backlog-no-rfr-state",
        "triage-no-rfr-state",
        "todo-no-rfr-state",
        "started-no-rfr-state",
        "done-no-rfr-state",
        "canceled-no-rfr-state",
        "team-not-read",
        "team-null",
        "unknown-type-no-rfr-state",
    ],
)
def test_backlog_and_triage_are_before_review_and_a_team_without_rfr_is_named(
    issue: dict,
    phase: str | None,
) -> None:
    assert _linear_state_phase(issue) == phase


def linked(phase: str) -> TrackedTicket:
    return replace(
        pr("handoff", tickets="linear", ticket_id="AUT-42"),
        pr_number="42",
        linear_ready_for_review=True,
        linear_state_phase=phase,
    )


@pytest.mark.parametrize("draft", [True, False], ids=["draft", "ready-pr"])
def test_handoff_on_a_team_with_no_rfr_state_marks_ready_and_reports(
    draft: bool,
) -> None:
    gh = RecordedGh()
    gh.route(PULL, draft_pull() if draft else served("pull-42-open.json"))
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    writes: list[str] = []
    snapshot = linked("before-review")
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: writes.append(ticket.ticket_id),
        fresh_ticket=lambda ref: linked("no-ready-state"),
        runner=gh,
    )

    note = actions.handoff(snapshot, HEAD)

    assert note == NO_READY_NOTE
    assert writes == []
    assert gh.keys() == ([PULL, MARK_READY] if draft else [PULL])


@pytest.mark.parametrize(
    ("phase", "writes"),
    [("before-review", ["AUT-42"]), ("ready-for-review", []), ("moved-on", [])],
)
def test_handoff_reports_nothing_when_the_linear_move_is_decided(
    phase: str,
    writes: list[str],
) -> None:
    """Preservation guard: only the no-RFR-state case carries a note."""
    gh = RecordedGh()
    gh.route(PULL, served("pull-42-open.json"))
    moved: list[str] = []
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=lambda ticket: moved.append(ticket.ticket_id),
        fresh_ticket=lambda ref: linked(phase),
        runner=gh,
    )

    assert actions.handoff(linked("before-review"), HEAD) is None
    assert moved == writes


# End to end through T4, with the production Linear write
# (LivePollSource._move_linear_ready) and the issue read the poller makes.


def linear_issue_in(state: dict, *, ready: dict | None) -> dict:
    return {"id": "issue-id", **phase_issue(state, ready=ready)}


def t4_setup(
    tmp_path: Path,
    issue: dict,
) -> tuple[Dispatcher, SQLiteJobQueue, RecordedGh, list[dict], TrackedTicket]:
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        requests.append(json.loads(body))
        return HTTPResult(
            200, json.dumps({"data": {"issueUpdate": {"success": True}}})
        )

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=transport,
    )
    snapshot = TrackedTicket(
        repo="owner/repo",
        ticket_id="42",
        factory=replace(factory(ship="handoff"), tickets="linear"),
        comments=(
            comment(
                "approve-1",
                VERDICT.format(round=1, head=HEAD, verdict="APPROVE", risk="high"),
                author="codex-login",
            ),
        ),
        current_head=HEAD,
        ci={HEAD: CIResult("success")},
        risk="high",
        pr_number="42",
        linear_issue_id=issue["id"],
        linear_ready_state_id=_linear_ready_state_id(issue),
        linear_ready_for_review=True,
        linear_state_phase=_linear_state_phase(issue),
    )
    gh = RecordedGh()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=LinearReviewActions(
            move_ready_for_review=True,
            linear_ready=source._move_linear_ready,
            fresh_ticket=lambda ref: snapshot,
            runner=gh,
        ),
        grok_delivery=FakeGrokDelivery(),
    )
    seed_review_job(dispatcher, queue, HEAD)
    return dispatcher, queue, gh, requests, snapshot


def ship_rows(queue: SQLiteJobQueue) -> tuple:
    with queue.connect() as connection:
        intent = connection.execute(
            "SELECT status, error FROM ship_intents WHERE comment_id = 'approve-1'"
        ).fetchone()
        state = connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = '42'"
        ).fetchone()
        wakes = connection.execute(
            "SELECT action, detail FROM jobs WHERE recipient = 'grok' ORDER BY rowid"
        ).fetchall()
    return (
        tuple(intent),
        state[0],
        [(row["action"], json.loads(row["detail"])) for row in wakes],
    )


@pytest.mark.parametrize("kind", ["backlog", "triage"])
def test_an_approved_backlog_or_triage_issue_moves_to_ready_for_review(
    tmp_path: Path,
    kind: str,
) -> None:
    issue = linear_issue_in({"id": "b", "type": kind, "position": 0.0}, ready=RFR)
    dispatcher, queue, gh, requests, snapshot = t4_setup(tmp_path, issue)

    dispatcher.process(snapshot)

    assert [request["variables"] for request in requests] == [
        {"issue": "issue-id", "state": "rfr"}
    ]
    assert gh.keys() == [PULL, MARK_READY]
    intent, state, wakes = ship_rows(queue)
    assert intent == ("completed", None)
    assert state == "handed-off"
    assert [action for action, _ in wakes] == ["done"]
    assert "note" not in wakes[0][1]


@pytest.mark.parametrize("kind", ["started", "unstarted", "backlog"])
def test_a_team_with_no_rfr_state_completes_the_handoff_and_reports_it(
    tmp_path: Path,
    kind: str,
) -> None:
    issue = linear_issue_in({"id": "s", "type": kind, "position": 2.0}, ready=None)
    dispatcher, queue, gh, requests, snapshot = t4_setup(tmp_path, issue)

    dispatcher.process(snapshot)

    assert requests == []
    assert gh.keys() == [PULL, MARK_READY]
    intent, state, wakes = ship_rows(queue)
    assert intent == ("completed", None)
    assert state == "handed-off"
    assert [action for action, _ in wakes] == ["done"]
    assert wakes[0][1]["outcome"] == "handed-off"
    assert wakes[0][1]["note"] == NO_READY_NOTE


@pytest.mark.parametrize("kind", ["completed", "canceled"])
def test_a_done_or_canceled_issue_is_still_left_where_it_is(
    tmp_path: Path,
    kind: str,
) -> None:
    """Preservation guard: the completed/canceled skip stays."""
    issue = linear_issue_in({"id": "d", "type": kind, "position": 5.0}, ready=RFR)
    dispatcher, queue, gh, requests, snapshot = t4_setup(tmp_path, issue)

    dispatcher.process(snapshot)

    assert requests == []
    intent, state, wakes = ship_rows(queue)
    assert intent == ("completed", None)
    assert [action for action, _ in wakes] == ["done"]
    assert "note" not in wakes[0][1]


def test_a_re_drive_on_a_team_with_no_rfr_state_reports_it_in_the_done_wake(
    tmp_path: Path,
) -> None:
    issue = linear_issue_in({"id": "b", "type": "backlog", "position": 0.0}, ready=None)
    dispatcher, queue, gh, requests, snapshot = t4_setup(tmp_path, issue)
    gh.route(PULL, GhResult(1, "", "gh: Server Error (HTTP 502)\n"), draft_pull())

    dispatcher.process(snapshot)
    assert ship_rows(queue)[0][0] == "failed"
    dispatcher.process(snapshot)

    assert requests == []
    assert gh.keys() == [PULL, PULL, MARK_READY]
    intent, state, wakes = ship_rows(queue)
    assert intent == ("completed", None)
    assert state == "handed-off"
    assert [action for action, _ in wakes] == ["ship-failed", "done"]
    assert wakes[1][1]["note"] == NO_READY_NOTE


# #895: a reviewer's rework state (linear_rework_states) is moved back to
# the project's ready state by the next approved handoff.

CHANGES_REQUESTED = {
    "id": "cr",
    "name": "Changes Requested",
    "type": "started",
    "position": 3.5,
}
QA_REVIEW = {"id": "qar", "name": "QA Review", "type": "started", "position": 3.0}
REWORK = frozenset({"Changes Requested"})
RFR_NAME = RFR["name"]


def rework_issue(state: dict, *, ready: dict | None = RFR, extra: dict = CHANGES_REQUESTED) -> dict:
    issue = phase_issue(state, ready=ready)
    issue["team"]["states"]["nodes"].append(extra)
    return issue


@pytest.mark.parametrize(
    ("issue", "ready_name", "phase"),
    [
        (rework_issue({"id": "cr", "type": "started", "position": 3.5}), RFR_NAME, "rework"),
        (rework_issue({"id": "qa", "type": "started", "position": 4.0}), RFR_NAME, "moved-on"),
        (rework_issue({"id": "done", "type": "completed", "position": 5.0}), RFR_NAME, "moved-on"),
        (rework_issue({"id": "x", "type": "canceled", "position": 6.0}), RFR_NAME, "moved-on"),
        (
            rework_issue({"id": "rfr", "type": "started", "position": 3.0}),
            RFR_NAME,
            "ready-for-review",
        ),
        (
            rework_issue(
                {"id": "cr", "type": "completed", "position": 5.5},
                extra={**CHANGES_REQUESTED, "type": "completed", "position": 5.5},
            ),
            RFR_NAME,
            "moved-on",
        ),
        (
            rework_issue({"id": "cr", "type": "started", "position": 3.5}, ready=None),
            RFR_NAME,
            "no-ready-state",
        ),
        (
            rework_issue({"id": "qar", "type": "started", "position": 3.0}, ready=QA_REVIEW),
            "QA Review",
            "ready-for-review",
        ),
        (
            rework_issue({"id": "doing", "type": "started", "position": 2.0}, ready=QA_REVIEW),
            "QA Review",
            "before-review",
        ),
        (
            rework_issue({"id": "qa", "type": "started", "position": 4.0}, ready=QA_REVIEW),
            "QA Review",
            "moved-on",
        ),
    ],
    ids=[
        "changes-requested",
        "in-qa-after-rfr",
        "done",
        "canceled",
        "ready-for-review",
        "rework-name-of-completed-type",
        "team-without-the-ready-state",
        "custom-ready-state",
        "before-custom-ready-state",
        "after-custom-ready-state",
    ],
)
def test_a_rework_state_is_its_own_phase_and_the_ready_state_is_configurable(
    issue: dict,
    ready_name: str,
    phase: str,
) -> None:
    assert _linear_state_phase(issue, ready_name, REWORK) == phase


def test_the_ready_state_id_follows_the_configured_name() -> None:
    issue = rework_issue({"id": "doing", "type": "started", "position": 2.0}, ready=QA_REVIEW)
    issue["team"]["states"]["nodes"].append(RFR)

    assert _linear_ready_state_id(issue, "QA Review") == "qar"
    assert _linear_ready_state_id(issue) == "rfr"


def test_an_approved_rework_issue_moves_back_to_ready_for_review(tmp_path: Path) -> None:
    issue = {"id": "issue-id", **rework_issue({"id": "cr", "type": "started", "position": 3.5})}
    dispatcher, queue, gh, requests, snapshot = t4_setup(tmp_path, issue)
    snapshot = replace(
        snapshot, linear_state_phase=_linear_state_phase(issue, rework=REWORK)
    )
    assert snapshot.linear_state_phase == "rework"
    dispatcher.review_actions.fresh_ticket = lambda ref: snapshot

    dispatcher.process(snapshot)

    assert [request["variables"] for request in requests] == [
        {"issue": "issue-id", "state": "rfr"}
    ]
    assert gh.keys() == [PULL, MARK_READY]
    intent, state, wakes = ship_rows(queue)
    assert intent == ("completed", None)
    assert state == "handed-off"
    assert [action for action, _ in wakes] == ["done"]
