"""Issue #47: safe ship_intents.error and the one handoff re-drive.

These run the real GitHub and Linear adapters against the RecordedGh seam,
so the gh argv is the one production sends.
"""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dispatcher import (
    CIResult,
    Dispatcher,
    RetryableShipError,
    RetryableStateError,
    TrackedTicket,
    safe_ship_error,
)
from lib.github_actions import (
    GhResult,
    GitHubAdapterError,
    GitHubReviewActions,
    LinearReviewActions,
)
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    VERDICT,
    FakeGrokDelivery,
    comment,
    factory,
    seed_review_job,
    ticket,
)
from tests.test_github_actions import (
    HEAD,
    MARK_READY,
    PULL,
    RecordedGh,
    draft_pull,
    served,
)

SECRET_TOKEN = "ghp_" + "Z" * 36
LEAKY_STDERR = (
    "gh: Resource not accessible by integration (HTTP 403)\n"
    "POST https://api.github.com/graphql\n"
    f"Authorization: Bearer {SECRET_TOKEN}\n"
)
LEAKY_STDOUT = (
    '{"data":null,"errors":[{"type":"FORBIDDEN","path":["markPullRequestReadyForReview"],'
    '"message":"Resource not accessible by integration; see '
    f'https://docs.github.com/graphql token={SECRET_TOKEN}"}}]}}'
)
AUT0 = "aut0-code/aut0"
AUT0_PULL = (f"repos/{AUT0}/pulls/1360",)


def approve_snapshot(
    *,
    head: str = HEAD,
    ci: str = "success",
    ship: str = "handoff",
) -> TrackedTicket:
    return ticket(
        comment(
            "approve-1",
            VERDICT.format(round=1, head=HEAD, verdict="APPROVE", risk="high"),
            author="codex-login",
        ),
        head=head,
        ci={head: CIResult(ci)},
        factory_config=factory(ship=ship),
    )


class UnlabeledGitHubActions(GitHubReviewActions):
    """The real adapter; orch:state label writes are out of scope here."""

    def set_state(self, ticket: TrackedTicket, state: str) -> None:
        del ticket, state


def github_setup(tmp_path: Path) -> tuple[Dispatcher, SQLiteJobQueue, RecordedGh]:
    gh = RecordedGh()
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=UnlabeledGitHubActions(gh),
        grok_delivery=FakeGrokDelivery(),
    )
    seed_review_job(dispatcher, queue, HEAD)
    return dispatcher, queue, gh


def intent(queue: SQLiteJobQueue, comment_id: str = "approve-1") -> sqlite3.Row:
    with queue.connect() as connection:
        row = connection.execute(
            "SELECT status, error, redrive FROM ship_intents WHERE comment_id = ?",
            (comment_id,),
        ).fetchone()
    assert row is not None
    return row


def outcome(queue: SQLiteJobQueue, comment_id: str = "approve-1") -> str:
    with queue.connect() as connection:
        return str(
            connection.execute(
                "SELECT outcome FROM handled_comments WHERE comment_id = ?",
                (comment_id,),
            ).fetchone()[0]
        )


def grok_wakes(queue: SQLiteJobQueue) -> list[tuple[str, str]]:
    with queue.connect() as connection:
        rows = connection.execute(
            """
            SELECT action, coalesce(detail, '') AS detail FROM jobs
            WHERE recipient = 'grok' ORDER BY rowid
            """
        ).fetchall()
    return [(str(row["action"]), str(row["detail"])) for row in rows]


# Criterion 2: a safe detail, never a secret, URL or body.


def test_ship_intent_error_records_a_safe_detail_without_secrets_urls_or_bodies(
    tmp_path: Path,
) -> None:
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, GhResult(1, LEAKY_STDOUT, LEAKY_STDERR))

    dispatcher.process(approve_snapshot())

    row = intent(queue)
    assert row["status"] == "failed"
    assert row["error"] == (
        "GitHubAdapterError: gh api POST graphql exited 1 (HTTP 403) "
        "graphql errors: FORBIDDEN"
    )
    wakes = grok_wakes(queue)
    assert [action for action, _ in wakes] == ["ship-failed"]
    for text in (row["error"], wakes[0][1]):
        assert SECRET_TOKEN not in text
        assert "ghp_" not in text
        assert "Bearer" not in text
        assert "http" not in text
        assert "api.github.com" not in text
        assert "Resource not accessible" not in text
        assert '"errors"' not in text
        assert "markPullRequestReadyForReview" not in text
    assert "{" not in row["error"]


@pytest.mark.parametrize(
    "message",
    [
        f"GET https://api.github.com/repos/o/r?access_token={SECRET_TOKEN} failed",
        f"Authorization: Bearer {SECRET_TOKEN}",
        "LINEAR_API_KEY=lin_api_" + "a" * 32 + " rejected",
        'gh exited 1: {"message": "Bad credentials", "body": "private"}',
        "push to github.com/owner/repo.git refused <html><body>x</body></html>",
        "password: hunter2 and xoxb-" + "1" * 20,
    ],
)
def test_safe_ship_error_drops_urls_tokens_and_bodies(message: str) -> None:
    text = safe_ship_error(RuntimeError(message))

    assert text.startswith("RuntimeError")
    for forbidden in (
        SECRET_TOKEN,
        "https://",
        "api.github.com",
        "github.com/owner",
        "lin_api_",
        "Bad credentials",
        "private",
        "<html>",
        "hunter2",
        "xoxb-",
    ):
        assert forbidden not in text


def test_safe_ship_error_prefers_safe_detail_and_is_capped() -> None:
    error = GitHubAdapterError("gh exited 1: " + "x" * 1000, safe_detail="gh api GET pulls/42 exited 1")
    assert safe_ship_error(error) == "GitHubAdapterError: gh api GET pulls/42 exited 1"
    assert len(safe_ship_error(RuntimeError("y " * 1000))) <= 240
    assert safe_ship_error(RuntimeError()) == "RuntimeError"


# Criterion 3: a handoff that failed before any confirmed write is re-driven once.


def test_handoff_that_failed_before_any_write_is_re_driven_once_and_completes(
    tmp_path: Path,
) -> None:
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(PULL, GhResult(1, "", "gh: Server Error (HTTP 502)\n"), draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    snapshot = approve_snapshot()

    dispatcher.process(snapshot)

    row = intent(queue)
    assert (row["status"], row["redrive"]) == ("failed", "eligible")
    assert row["error"] == "GitHubAdapterError: gh api GET pulls/42 exited 1 (HTTP 502)"
    assert outcome(queue) == "failed-verdict-GitHubAdapterError"
    assert gh.count(MARK_READY) == 0

    dispatcher.process(snapshot)

    row = intent(queue)
    assert (row["status"], row["error"], row["redrive"]) == ("completed", None, "spent")
    assert outcome(queue) == "ship-already-completed-handoff"
    assert gh.keys() == [PULL, PULL, MARK_READY]
    assert [action for action, _ in grok_wakes(queue)] == ["ship-failed", "done"]
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = '42'"
        ).fetchone()[0] == "handed-off"

    dispatcher.process(snapshot)
    assert len(gh.calls) == 3


def test_a_failed_re_drive_is_not_repeated(tmp_path: Path) -> None:
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(
        PULL,
        GhResult(1, "", "gh: Server Error (HTTP 502)\n"),
        GhResult(1, "", "gh: Bad credentials (HTTP 401)\n"),
    )
    snapshot = approve_snapshot()

    for _ in range(4):
        dispatcher.process(snapshot)

    assert gh.keys() == [PULL, PULL]
    row = intent(queue)
    assert (row["status"], row["redrive"]) == ("failed", "spent")
    assert row["error"] == "GitHubAdapterError: gh api GET pulls/42 exited 1 (HTTP 401)"
    assert [action for action, _ in grok_wakes(queue)] == ["ship-failed"]


@pytest.mark.parametrize(
    ("snapshot", "redrive"),
    [
        (approve_snapshot(head="b" * 40), "spent"),
        (approve_snapshot(ci="failure"), "spent"),
        (approve_snapshot(ci="pending"), "eligible"),
        (replace(approve_snapshot(), current_head=None), "eligible"),
        (approve_snapshot(ship="merge"), "eligible"),
    ],
    ids=["head-moved", "ci-red", "ci-pending", "head-unread", "policy-changed"],
)
def test_re_drive_writes_nothing_unless_head_ci_and_policy_still_hold(
    tmp_path: Path,
    snapshot: TrackedTicket,
    redrive: str,
) -> None:
    dispatcher, queue, gh = github_setup(tmp_path)
    gh.route(PULL, GhResult(1, "", "gh: Server Error (HTTP 502)\n"))
    dispatcher.process(approve_snapshot())
    calls = len(gh.calls)

    dispatcher._redrive_handoffs(snapshot)

    assert len(gh.calls) == calls
    assert (intent(queue)["status"], intent(queue)["redrive"]) == ("failed", redrive)


def linear_setup(
    tmp_path: Path,
    gh: RecordedGh,
    actions: LinearReviewActions,
) -> tuple[Dispatcher, SQLiteJobQueue]:
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
    )
    del gh
    seed_review_job(dispatcher, queue, HEAD)
    return dispatcher, queue


def linear_snapshot() -> TrackedTicket:
    return replace(
        approve_snapshot(),
        factory=replace(factory(ship="handoff"), tickets="linear"),
        pr_number="42",
        linear_ready_for_review=True,
    )


def test_no_re_drive_after_the_pr_ready_write_was_confirmed(tmp_path: Path) -> None:
    def linear_fails(ticket: TrackedTicket) -> None:
        del ticket
        raise RetryableStateError("Linear budget")

    gh = RecordedGh()
    gh.route(PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=linear_fails,
        runner=gh,
    )
    dispatcher, queue = linear_setup(tmp_path, gh, actions)

    dispatcher.process(linear_snapshot())
    dispatcher.process(linear_snapshot())

    row = intent(queue)
    assert (row["status"], row["redrive"]) == ("failed", None)
    assert row["error"] == (
        "GitHubAdapterError: Linear handoff update was not confirmed: RetryableStateError"
    )
    assert gh.keys() == [PULL, MARK_READY]


def test_unconfirmed_linear_move_on_a_ready_pr_is_re_driven(tmp_path: Path) -> None:
    moves: list[str] = []

    def linear_fails_once(ticket: TrackedTicket) -> None:
        if not moves:
            moves.append("failed")
            raise RetryableStateError("Linear budget")
        moves.append(ticket.ticket_id)

    gh = RecordedGh()
    gh.route(PULL, served("pull-42-open.json"))
    actions = LinearReviewActions(
        move_ready_for_review=True,
        linear_ready=linear_fails_once,
        runner=gh,
    )
    dispatcher, queue = linear_setup(tmp_path, gh, actions)

    dispatcher.process(linear_snapshot())
    assert intent(queue)["redrive"] == "eligible"
    dispatcher.process(linear_snapshot())

    assert moves == ["failed", "42"]
    assert gh.keys() == [PULL, PULL]
    assert intent(queue)["status"] == "completed"


def test_a_retryable_error_during_the_re_drive_hands_the_claim_back(
    tmp_path: Path,
) -> None:
    fresh_calls: list[int] = []

    def fresh(ref):
        fresh_calls.append(1)
        if len(fresh_calls) == 2:
            raise RetryableStateError("Linear budget")
        return linear_snapshot()

    gh = RecordedGh()
    gh.route(PULL, GhResult(1, "", "gh: Server Error (HTTP 502)\n"), draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    actions = LinearReviewActions(
        move_ready_for_review=False,
        linear_ready=lambda ticket: None,
        fresh_ticket=fresh,
        runner=gh,
    )
    dispatcher, queue = linear_setup(tmp_path, gh, actions)

    dispatcher.process(linear_snapshot())
    with pytest.raises(RetryableShipError, match="Linear budget"):
        dispatcher.process(linear_snapshot())
    assert intent(queue)["redrive"] == "eligible"
    dispatcher.process(linear_snapshot())

    assert intent(queue)["status"] == "completed"
    assert gh.keys() == [PULL, PULL, MARK_READY]


# The AUT-2286 row as the live poller wrote it before this fix.

LEGACY_SHIP_INTENTS = """
CREATE TABLE ship_intents (
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    head TEXT NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY (repo, ticket_id, comment_id)
)
"""
LEGACY_HANDLED = """
CREATE TABLE handled_comments (
    repo TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    PRIMARY KEY (repo, ticket_id, comment_id)
)
"""


def legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_SHIP_INTENTS)
        connection.execute(LEGACY_HANDLED)
        connection.executemany(
            "INSERT INTO ship_intents VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (AUT0, "AUT-2286", "6813518921", HEAD, "handoff", "failed", "GitHubAdapterError"),
                (AUT0, "AUT-1", "1", HEAD, "handoff", "failed", "RuntimeError"),
                (AUT0, "AUT-2", "2", HEAD, "merge", "failed", "GitHubAdapterError"),
                (AUT0, "AUT-3", "3", HEAD, "handoff", "completed", None),
            ],
        )
        connection.execute(
            "INSERT INTO handled_comments VALUES (?, ?, ?, ?)",
            (AUT0, "AUT-2286", "6813518921", "failed-verdict-GitHubAdapterError"),
        )


def aut0_2286() -> TrackedTicket:
    return TrackedTicket(
        repo=AUT0,
        ticket_id="AUT-2286",
        factory=replace(factory(ship="handoff"), tickets="linear"),
        comments=(
            comment(
                "6813518921",
                VERDICT.format(round=1, head=HEAD, verdict="APPROVE", risk="high"),
                author="codex-login",
            ),
        ),
        current_head=HEAD,
        ci={HEAD: CIResult("success")},
        risk="high",
        pr_number="1360",
        linear_ready_for_review=True,
    )


def test_aut_2286_legacy_failed_intent_completes_after_the_fix_is_installed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "orch.db"
    legacy_db(path)
    gh = RecordedGh()
    gh.route(AUT0_PULL, draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    moved: list[str] = []
    fresh_reads: list[str] = []

    def fresh(ref):
        fresh_reads.append(ref.ticket_id)
        return aut0_2286()

    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions={
            AUT0: LinearReviewActions(
                move_ready_for_review=True,
                linear_ready=lambda ticket: moved.append(ticket.ticket_id),
                fresh_ticket=fresh,
                runner=gh,
            )
        },
        grok_delivery=FakeGrokDelivery(),
    )
    with queue.connect() as connection:
        connection.execute(
            """
            INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)
            VALUES (?, 'AUT-2286', 'linear', 0)
            """,
            (AUT0,),
        )
        connection.execute(
            """
            INSERT INTO ticket_state (
                repo, ticket_id, workflow_state, paused_action, paused_recipient
            ) VALUES (?, 'AUT-2286', 'waiting-max', 'clarify', 'grok')
            """,
            (AUT0,),
        )
        redrive = dict(
            connection.execute(
                "SELECT ticket_id, redrive FROM ship_intents"
            ).fetchall()
        )
    assert redrive == {
        "AUT-2286": "eligible",
        "AUT-1": None,
        "AUT-2": None,
        "AUT-3": None,
    }
    assert any(ref.ticket_id == "AUT-2286" for ref in dispatcher._tracked_refs())

    dispatcher.process(aut0_2286())

    assert gh.keys() == [AUT0_PULL, MARK_READY]
    assert fresh_reads == ["AUT-2286"]
    assert moved == ["AUT-2286"]
    with queue.connect() as connection:
        row = connection.execute(
            """
            SELECT status, error, redrive FROM ship_intents
            WHERE ticket_id = 'AUT-2286'
            """
        ).fetchone()
        state = connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = 'AUT-2286'"
        ).fetchone()[0]
        handled = connection.execute(
            "SELECT outcome FROM handled_comments WHERE comment_id = '6813518921'"
        ).fetchone()[0]
    assert tuple(row) == ("completed", None, "spent")
    assert state == "handed-off"
    assert handled == "ship-already-completed-handoff"
    assert [action for action, _ in grok_wakes(queue)] == ["done"]
    assert not any(ref.ticket_id == "AUT-2286" for ref in dispatcher._tracked_refs())

    dispatcher.process(aut0_2286())
    Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=GitHubReviewActions(gh),
        grok_delivery=FakeGrokDelivery(),
    )
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT redrive FROM ship_intents WHERE ticket_id = 'AUT-1'"
        ).fetchone()[0] is None
    assert len(gh.calls) == 2
    assert moved == ["AUT-2286"]


def test_label_write_after_a_re_drive_is_retried_without_another_handoff(
    tmp_path: Path,
) -> None:
    class FlakyLabels(UnlabeledGitHubActions):
        def __init__(self, gh: RecordedGh) -> None:
            super().__init__(gh)
            self.states: list[str] = []

        def set_state(self, ticket: TrackedTicket, state: str) -> None:
            self.states.append(state)
            if self.states.count("handed-off") == 1 and state == "handed-off":
                raise RuntimeError("label write failed")

    gh = RecordedGh()
    gh.route(PULL, GhResult(1, "", "gh: Server Error (HTTP 502)\n"), draft_pull())
    gh.route(MARK_READY, served("graphql-mark-ready.json"))
    actions = FlakyLabels(gh)
    path = tmp_path / "d.db"
    queue = SQLiteJobQueue(path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
    )
    seed_review_job(dispatcher, queue, HEAD)
    snapshot = approve_snapshot()
    dispatcher.process(snapshot)

    with pytest.raises(RetryableStateError, match="handed-off"):
        dispatcher.process(snapshot)
    assert intent(queue)["status"] == "completed"
    with queue.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM handled_comments WHERE comment_id = 'approve-1'"
        ).fetchone()[0] == 0
    dispatcher.process(snapshot)

    assert outcome(queue) == "ship-already-completed-handoff"
    assert actions.states.count("handed-off") == 2
    assert gh.count(MARK_READY) == 1
    assert [action for action, _ in grok_wakes(queue)] == ["ship-failed", "done"]
