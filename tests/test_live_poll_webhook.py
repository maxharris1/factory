"""Poller side of the Linear webhook (issue #55).

With linear_webhook configured, a cycle fetches only dirty tickets and
tracked tickets with active jobs in full; the discovery search and every
tracked read become a reconcile every reconcile_seconds. With no webhook
configured, the poller behaves exactly as before: the pins below were
recorded on main at 5a1fbeb and must not change.
"""

import hashlib
import hmac
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import LINEAR_HANDOFF_QUERY, TicketRef
from lib.linear_webhook import DirtyRow, LinearDirtyQueue, WebhookApp, ensure_tables
from lib.orch_yml import LinearWebhookConfig
from lib.poll_source import PollCycleError
from lib.queue import JobRequest
from tests.test_live_poll import config
from tests.test_live_poll_no_webhook import (
    REPO,
    SEARCHES,
    Clock,
    Linear,
    _dispatcher,
    issue,
    source_for,
)

WEBHOOK = LinearWebhookConfig(
    host="127.0.0.1",
    port=8787,
    secret_file=Path("/nonexistent/linear-webhook.secret"),
)


def webhook_config(**changes):
    return replace(config(linear=True), linear_webhook=replace(WEBHOOK, **changes))


class FakeDirty:
    def __init__(self, rows=()):
        self.rows = {row.issue_id: row for row in rows}
        self.acked: list[list[str]] = []
        self.fail = False
        self.seen: dict[str, float] = {}
        self.started: float | None = None
        self.missed: list[tuple] = []

    def add(self, issue_id: str, identifier: str | None, at: float) -> None:
        self.rows[issue_id] = DirtyRow(issue_id, identifier, "Issue.update", at)

    def pending(self):
        if self.fail:
            raise sqlite3.OperationalError("database is locked")
        return tuple(sorted(self.rows.values(), key=lambda row: row.received_at))

    def ack(self, rows):
        self.acked.append(sorted(row.issue_id for row in rows))
        for row in rows:
            current = self.rows.get(row.issue_id)
            if current is not None and current.received_at == row.received_at:
                del self.rows[row.issue_id]

    def record_polling(self, now):
        del now

    def seen_at(self, issue_id):
        return self.seen.get(issue_id)

    def listener_started_at(self):
        return self.started

    def record_missed(self, issue_id, identifier, event_at, found_at):
        self.missed.append((issue_id, identifier, event_at, found_at))


def cycle(source, refs, *, exclude=frozenset()):
    source.begin_cycle()
    candidates = source.linear_candidates(
        LINEAR_HANDOFF_QUERY,
        exclude=exclude,
        reserve=len(refs),
    )
    tracked = source.tracked_tickets(refs)
    source.end_cycle()
    return candidates, tracked


def refs(*identifiers: str) -> tuple[TicketRef, ...]:
    return tuple(TicketRef(REPO, identifier, "linear") for identifier in identifiers)




def test_dirty_tracked_ticket_is_fetched_and_a_clean_one_reuses_its_last_read() -> None:
    linear = Linear([issue("AUT-1"), issue("AUT-2")], hits=[])
    clock = Clock()
    source, gh = source_for(webhook_config(), linear, clock=clock)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    tracked = refs("AUT-1", "AUT-2")

    cycle(source, tracked, exclude=frozenset({"AUT-1", "AUT-2"}))
    assert linear.take() == SEARCHES + [("issue", "AUT-1"), ("issue", "AUT-2")]

    clock.now += 60
    dirty.add("id-AUT-1", "AUT-1", 1.0)
    gh.calls.clear()
    _, found = cycle(source, tracked, exclude=frozenset({"AUT-1", "AUT-2"}))

    assert linear.take() == [("issue", "AUT-1")]
    assert [ticket.ticket_id for ticket in found] == ["AUT-1", "AUT-2"]
    # The clean ticket is still rebuilt each cycle, so its GitHub PR head,
    # CI and PR comments stay current.
    assert sum("repos/acme/widgets/pulls/12" in call for call in gh.calls) == 2
    assert dirty.acked[-1] == ["id-AUT-1"]
    assert dirty.rows == {}


def test_dirty_untracked_issue_is_discovered_without_a_search() -> None:
    linear = Linear([issue("AUT-9")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    dirty = FakeDirty()
    source.linear_dirty = dirty

    cycle(source, ())
    assert linear.take() == SEARCHES
    clock.now += 60
    dirty.add("id-AUT-9", "AUT-9", 5.0)
    candidates, _ = cycle(source, ())

    assert linear.take() == [("issue", "id-AUT-9")]
    assert [ticket.ticket_id for ticket in candidates] == ["AUT-9"]
    assert dirty.rows == {}


@pytest.mark.parametrize(
    "value",
    [
        issue("AUT-7", project="project-2"),
        issue("AUT-7", state="completed"),
        issue("AUT-7", state="canceled"),
        None,
    ],
    ids=["other-project", "completed", "canceled", "deleted"],
)
def test_dirty_issue_outside_scope_is_read_once_dropped_quietly_and_acked(
    value: dict | None,
) -> None:
    linear = Linear([value] if value is not None else [], hits=[])
    clock = Clock()
    errors: list = []
    source, _gh = source_for(webhook_config(), linear, clock=clock, errors=errors)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    cycle(source, ())
    linear.take()
    clock.now += 60
    dirty.add("id-AUT-7", "AUT-7", 5.0)

    candidates, _ = cycle(source, ())

    assert candidates == ()
    assert linear.take() == [("issue", "id-AUT-7")]
    assert errors == []
    assert dirty.rows == {}


def test_reconcile_fallback_runs_the_full_search_when_no_webhook_arrives() -> None:
    linear = Linear([issue("AUT-1"), issue("AUT-5")], hits=["AUT-1", "AUT-5"])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    source.linear_dirty = FakeDirty()
    tracked = refs("AUT-1")
    reads_by_cycle = []

    for _ in range(31):
        cycle(source, tracked, exclude=frozenset({"AUT-1"}))
        reads_by_cycle.append(linear.take())
        clock.now += 60

    full = SEARCHES + [("issue", "id-AUT-5"), ("issue", "AUT-1")]
    assert [index for index, reads in enumerate(reads_by_cycle) if reads] == [
        0,
        15,
        30,
    ]
    assert reads_by_cycle[0] == reads_by_cycle[15] == reads_by_cycle[30] == full


def test_reconcile_interval_is_configurable() -> None:
    linear = Linear([issue("AUT-1")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(reconcile_seconds=300), linear, clock=clock)
    source.linear_dirty = FakeDirty()
    tracked = refs("AUT-1")
    busy = []

    for index in range(11):
        cycle(source, tracked, exclude=frozenset({"AUT-1"}))
        if linear.take():
            busy.append(index)
        clock.now += 60

    assert busy == [0, 5, 10]


def test_deferred_reconcile_search_stays_due_next_cycle() -> None:
    linear = Linear([issue(f"AUT-{index}") for index in range(30)])
    clock = Clock()
    deferrals: list[str] = []
    source, _gh = source_for(
        webhook_config(), linear, clock=clock, deferrals=deferrals
    )
    source.linear_dirty = FakeDirty()

    cycle(source, ())
    first = linear.take()
    clock.now += 60
    cycle(source, ())
    second = linear.take()

    assert first[:2] == SEARCHES and second[:2] == SEARCHES
    assert any("deferred discovery" in item for item in deferrals)
    assert {key for kind, key in first + second if kind == "issue"} == {
        f"id-AUT-{index}" for index in range(30)
    }


def test_tracked_ticket_with_an_active_job_is_fetched_every_cycle() -> None:
    linear = Linear([issue("AUT-1"), issue("AUT-2")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    source.linear_dirty = FakeDirty()
    source.active_tickets = lambda: frozenset({(REPO, "AUT-2")})
    tracked = refs("AUT-1", "AUT-2")
    cycle(source, tracked, exclude=frozenset({"AUT-1", "AUT-2"}))
    linear.take()

    for _ in range(3):
        clock.now += 60
        cycle(source, tracked, exclude=frozenset({"AUT-1", "AUT-2"}))
        assert linear.take() == [("issue", "AUT-2")]


def test_dirty_row_from_a_comment_matches_a_tracked_ticket_by_issue_id() -> None:
    linear = Linear([issue("AUT-1")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    tracked = refs("AUT-1")
    cycle(source, tracked, exclude=frozenset({"AUT-1"}))
    linear.take()

    clock.now += 60
    dirty.add("id-AUT-1", None, 7.0)
    cycle(source, tracked, exclude=frozenset({"AUT-1"}))

    assert linear.take() == [("issue", "AUT-1")]
    assert dirty.rows == {}


def test_dirty_rows_deferred_by_the_budget_stay_queued_for_the_next_cycle() -> None:
    issues = [issue(f"AUT-{index:02d}") for index in range(25)]
    linear = Linear(issues, hits=[])
    clock = Clock()
    deferrals: list[str] = []
    source, _gh = source_for(
        webhook_config(), linear, clock=clock, deferrals=deferrals
    )
    dirty = FakeDirty()
    source.linear_dirty = dirty
    cycle(source, ())
    linear.take()
    clock.now += 60
    for index, value in enumerate(issues):
        dirty.add(value["id"], value["identifier"], float(index))

    cycle(source, ())
    first = linear.take()
    clock.now += 60
    cycle(source, ())
    second = linear.take()

    assert 0 < len(first) < 25
    assert deferrals and "Linear poll budget deferred discovery" in deferrals[0]
    assert {key for _, key in first + second} == {value["id"] for value in issues}
    assert dirty.rows == {}


def test_event_arriving_mid_cycle_is_not_lost() -> None:
    linear = Linear([issue("AUT-1")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    tracked = refs("AUT-1")
    cycle(source, tracked, exclude=frozenset({"AUT-1"}))
    linear.take()
    clock.now += 60
    dirty.add("id-AUT-1", "AUT-1", 1.0)

    source.begin_cycle()
    source.linear_candidates(
        LINEAR_HANDOFF_QUERY, exclude=frozenset({"AUT-1"}), reserve=1
    )
    source.tracked_tickets(tracked)
    dirty.add("id-AUT-1", "AUT-1", 2.0)  # a new event lands after the read
    source.end_cycle()

    assert dirty.rows["id-AUT-1"].received_at == 2.0
    clock.now += 60
    cycle(source, tracked, exclude=frozenset({"AUT-1"}))
    assert linear.take()[-1] == ("issue", "AUT-1")
    assert dirty.rows == {}


def test_unreadable_dirty_queue_falls_back_to_a_full_cycle_and_is_recorded() -> None:
    linear = Linear([issue("AUT-1"), issue("AUT-5")], hits=["AUT-1", "AUT-5"])
    clock = Clock()
    errors: list = []
    source, _gh = source_for(webhook_config(), linear, clock=clock, errors=errors)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    tracked = refs("AUT-1")
    cycle(source, tracked, exclude=frozenset({"AUT-1"}))
    linear.take()
    clock.now += 60
    dirty.fail = True

    with pytest.raises(PollCycleError):
        cycle(source, tracked, exclude=frozenset({"AUT-1"}))

    assert linear.take() == SEARCHES + [("issue", "id-AUT-5"), ("issue", "AUT-1")]
    assert errors == [("linear-webhook", "-", "OperationalError: database is locked")]


def test_webhook_event_to_dirty_row_to_fetch_to_tracking_through_orch_db(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    linear = Linear([issue("AUT-9")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    source.linear_dirty = LinearDirtyQueue(database)
    source.active_tickets = dispatcher.active_job_tickets
    source.error_sink = dispatcher.record_poll_error
    dispatcher.poll_once(source)  # startup reconcile: nothing found yet
    assert linear.take() == SEARCHES
    app = WebhookApp(
        secret=b"fake-secret",
        project_ids=frozenset({"project-1"}),
        db_path=database,
        clock=lambda: 1_800_000_000.0,
        log=lambda _line: None,
    )
    # Linear sends Issue.create for a new issue in the project before any
    # comment on it; that is how the listener knows the issue is in scope.
    assert _deliver(app, _issue_event("AUT-9", action="create")).outcome == "accepted"
    body = json.dumps(
        {
            "action": "create",
            "type": "Comment",
            "data": {
                "id": "c-1",
                "body": "<!-- orch:v1 kind=go from=grok-bot role=orchestrator -->",
                "issueId": "id-AUT-9",
                "issue": {"id": "id-AUT-9", "identifier": "AUT-9"},
            },
            "webhookTimestamp": 1_800_000_000_000,
        }
    ).encode()
    result = app.handle(
        "POST",
        "/linear-webhook",
        {
            "Content-Length": str(len(body)),
            "Linear-Signature": hmac.new(b"fake-secret", body, hashlib.sha256).hexdigest(),
        },
        body,
        "127.0.0.1",
    )
    assert result.status == 200

    clock.now += 60
    dispatcher.poll_once(source)

    assert linear.take() == [("issue", "id-AUT-9")]
    with sqlite3.connect(database) as connection:
        tracked = connection.execute(
            "SELECT repo, ticket_id, tracker FROM tracked_tickets"
        ).fetchall()
        remaining = connection.execute("SELECT count(*) FROM linear_dirty").fetchone()[0]
    assert tracked == [(REPO, "AUT-9", "linear")]
    assert remaining == 0


def test_dispatcher_reports_tickets_with_queued_or_running_jobs(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    for ticket, state in (("AUT-1", "queued"), ("AUT-2", "running"), ("AUT-3", "done")):
        job = dispatcher.queue.enqueue(
            JobRequest(
                repo="Acme/Widgets",
                ticket_id=ticket,
                action="build",
                recipient="claude",
                source_comment_id=f"c-{ticket}",
                timeout_seconds=3600,
                generation=1,
            ),
            dedupe_key=f"k-{ticket}",
        )
        with dispatcher.queue.connect() as connection:
            connection.execute(
                "UPDATE jobs SET state = ? WHERE id = ?", (state, job.id)
            )

    assert dispatcher.active_job_tickets() == frozenset(
        {(REPO, "AUT-1"), (REPO, "AUT-2")}
    )


def test_runtime_wires_the_dirty_queue_only_when_a_webhook_is_configured(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    _dispatcher(database)
    base = (
        "poll_seconds: 60\n"
        "linear:\n  api_key_env: LINEAR_KEY\n"
        "grok:\n  webhook_url_env: U\n  webhook_key_env: K\n"
        "trusted_logins:\n  grok-login: [grok-bot]\n"
        "trusted_linear_users:\n  user-1: [grok-bot]\n"
        "projects:\n  - repo: acme/widgets\n    factory_ref: main\n"
        "    linear_project_id: project-1\n"
    )
    plain = tmp_path / "plain.yml"
    plain.write_text(base, encoding="utf-8")
    hooked = tmp_path / "hooked.yml"
    hooked.write_text(
        base + "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n",
        encoding="utf-8",
    )

    _, plain_source = cli._runtime(database, plain, create=False)
    dispatcher, hooked_source = cli._runtime(database, hooked, create=False)

    assert getattr(plain_source, "linear_dirty", None) is None
    assert getattr(plain_source, "active_tickets", None) is None
    assert getattr(plain_source, "unpolled_tickets", None) is None
    assert isinstance(hooked_source.linear_dirty, LinearDirtyQueue)
    assert hooked_source.linear_dirty.path == database
    assert hooked_source.active_tickets == dispatcher.active_job_tickets
    assert hooked_source.unpolled_tickets == dispatcher.unpolled_linear_tickets


LISTENER_NOW = 1_800_000_000.0


def _listener(database: Path) -> WebhookApp:
    return WebhookApp(
        secret=b"fake-secret",
        project_ids=frozenset({"project-1"}),
        db_path=database,
        clock=lambda: LISTENER_NOW,
        log=lambda _line: None,
    )


def _deliver(app: WebhookApp, event: dict):
    body = json.dumps(event).encode()
    return app.handle(
        "POST",
        "/linear-webhook",
        {
            "Content-Length": str(len(body)),
            "Linear-Signature": hmac.new(b"fake-secret", body, hashlib.sha256).hexdigest(),
        },
        body,
        "127.0.0.1",
    )


def _issue_event(
    identifier: str,
    *,
    project: str = "project-1",
    action: str = "update",
) -> dict:
    return {
        "action": action,
        "type": "Issue",
        "data": {
            "id": f"id-{identifier}",
            "identifier": identifier,
            "projectId": project,
        },
        "webhookTimestamp": int(LISTENER_NOW * 1000),
    }


def _handoff_comment(identifier: str) -> dict:
    return {
        "action": "create",
        "type": "Comment",
        "data": {
            "id": f"c-{identifier}",
            "body": "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->",
            "issueId": f"id-{identifier}",
            "issue": {"id": f"id-{identifier}", "identifier": identifier},
        },
        "webhookTimestamp": int(LISTENER_NOW * 1000),
    }


def _wire(source, dispatcher, database: Path) -> None:
    """What cli._runtime wires in webhook mode."""
    source.linear_dirty = LinearDirtyQueue(database)
    source.active_tickets = dispatcher.active_job_tickets
    source.unpolled_tickets = getattr(dispatcher, "unpolled_linear_tickets", None)
    source.error_sink = dispatcher.record_poll_error


def _dirty_count(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT count(*) FROM linear_dirty").fetchone()[0]


# F1.1: the dispatcher excludes every tracked Linear ticket from discovery
# but polls only non-terminal ones, so a terminal ticket's dirty row had no
# reader. It must drain, without reopening the ticket or queueing work.
@pytest.mark.parametrize("delivery", ["issue-event", "uuid-only-row"])
@pytest.mark.parametrize("state", ["done", "abandoned", "merged"])
def test_dirty_row_for_a_terminal_tracked_ticket_drains_without_reopening_work(
    tmp_path: Path,
    state: str,
    delivery: str,
) -> None:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    with dispatcher.queue.connect() as connection:
        connection.execute(
            "INSERT INTO tracked_tickets VALUES (?, 'AUT-1', 'linear', 0)", (REPO,)
        )
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) "
            "VALUES (?, 'AUT-1', ?)",
            (REPO, state),
        )
    linear = Linear([issue("AUT-1")], hits=[])
    clock = Clock()
    errors: list = []
    source, _gh = source_for(webhook_config(), linear, clock=clock, errors=errors)
    _wire(source, dispatcher, database)
    source.error_sink = lambda repo, ticket, error: errors.append((repo, ticket, error))
    dispatcher.poll_once(source)  # startup reconcile; a terminal ticket is not read
    assert linear.take() == SEARCHES
    app = _listener(database)
    if delivery == "issue-event":
        assert _deliver(app, _issue_event("AUT-1")).outcome == "accepted"
    else:  # a row that names only the issue UUID
        with sqlite3.connect(database) as connection:
            ensure_tables(connection)
            connection.execute(
                "INSERT INTO linear_dirty VALUES ('id-AUT-1', NULL, 'Comment.create', 5.0)"
            )

    reads = []
    for _ in range(16):  # 16 minutes: crosses the 900 s reconcile
        clock.now += 60
        dispatcher.poll_once(source)
        reads += linear.take()
        assert _dirty_count(database) == 0

    assert reads.count(SEARCHES[0]) == 1  # the reconcile ran
    if delivery == "issue-event":
        # Terminal tickets are not read with or without a webhook.
        assert ("issue", "AUT-1") not in reads
        assert ("issue", "id-AUT-1") not in reads
    assert errors == []
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = 'AUT-1'"
        ).fetchone() == (state,)
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
        assert connection.execute(
            "SELECT repo, ticket_id FROM tracked_tickets"
        ).fetchall() == [(REPO, "AUT-1")]


def test_dispatcher_reports_tracked_linear_tickets_it_no_longer_polls(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    with dispatcher.queue.connect() as connection:
        for ticket, state in (
            ("AUT-1", None),
            ("AUT-2", "building"),
            ("AUT-3", "done"),
            ("AUT-4", "abandoned"),
            ("AUT-5", "merged"),
        ):
            connection.execute(
                "INSERT INTO tracked_tickets VALUES (?, ?, 'linear', 0)", (REPO, ticket)
            )
            if state is not None:
                connection.execute(
                    "INSERT INTO ticket_state (repo, ticket_id, workflow_state) "
                    "VALUES (?, ?, ?)",
                    (REPO, ticket, state),
                )
        connection.execute(
            "INSERT INTO tracked_tickets VALUES (?, '77', 'github', 0)", (REPO,)
        )

    assert dispatcher.unpolled_linear_tickets() == frozenset({"AUT-3", "AUT-4", "AUT-5"})


def lenient_cycle(source, refs, *, exclude=frozenset()):
    """cycle(), but a deleted ticket's recorded read error is expected."""
    source.begin_cycle()
    candidates = source.linear_candidates(
        LINEAR_HANDOFF_QUERY,
        exclude=exclude,
        reserve=len(refs),
    )
    tracked = source.tracked_tickets(refs)
    try:
        source.end_cycle()
    except PollCycleError:
        pass
    return candidates, tracked


# F1.2: an authoritative null read must evict every cached alias before its
# dirty row is acked, or a later cycle rebuilds the deleted issue.
@pytest.mark.parametrize("identifier", ["AUT-1", None], ids=["by-identifier", "uuid-only"])
def test_deleted_tracked_issue_is_never_rebuilt_from_an_older_read(
    identifier: str | None,
) -> None:
    linear = Linear([issue("AUT-1"), issue("AUT-2")], hits=[])
    clock = Clock()
    errors: list = []
    source, _gh = source_for(webhook_config(), linear, clock=clock, errors=errors)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    tracked = refs("AUT-1", "AUT-2")
    exclude = frozenset({"AUT-1", "AUT-2"})
    _, found = cycle(source, tracked, exclude=exclude)
    assert [ticket.ticket_id for ticket in found] == ["AUT-1", "AUT-2"]

    del linear.by_key["AUT-1"], linear.by_key["id-AUT-1"]  # deleted: Linear answers null
    dirty.add("id-AUT-1", identifier, 1.0)
    for _ in range(4):
        clock.now += 60
        _, found = lenient_cycle(source, tracked, exclude=exclude)
        assert [ticket.ticket_id for ticket in found] == ["AUT-2"]

    assert dirty.rows == {}
    assert {ticket for _repo, ticket, _error in errors} == {"AUT-1"}


def test_deleted_discovered_issue_leaves_no_cached_alias() -> None:
    linear = Linear([issue("AUT-9")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    cycle(source, ())
    clock.now += 60
    dirty.add("id-AUT-9", "AUT-9", 1.0)
    candidates, _ = cycle(source, ())  # read by UUID, cached under both keys
    assert [ticket.ticket_id for ticket in candidates] == ["AUT-9"]

    del linear.by_key["AUT-9"], linear.by_key["id-AUT-9"]
    clock.now += 60
    dirty.add("id-AUT-9", "AUT-9", 2.0)
    candidates, _ = cycle(source, ())
    linear.take()

    assert candidates == ()
    assert dirty.rows == {}
    # Now tracked (say the dispatcher picked it up), it is read, not reused.
    clock.now += 60
    _, found = lenient_cycle(source, refs("AUT-9"), exclude=frozenset({"AUT-9"}))
    assert found == ()
    assert linear.take() == [("issue", "AUT-9")]


# F1.3 end to end: an out-of-project comment carrying the marker is ignored
# by the listener, so the poller spends no Linear read on it.
def test_handoff_comment_outside_the_configured_projects_costs_no_linear_read(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    linear = Linear([issue("AUT-9", project="project-2")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    _wire(source, dispatcher, database)
    dispatcher.poll_once(source)
    assert linear.take() == SEARCHES
    app = _listener(database)

    result = _deliver(app, _handoff_comment("AUT-9"))

    assert (result.status, result.outcome) == (200, "ignored")
    assert _dirty_count(database) == 0
    clock.now += 60
    dispatcher.poll_once(source)
    assert linear.take() == []


# Sweep of F1.1: a ticket still tracked under a repo no longer in orch.yml
# is excluded from discovery and skipped by tracked_tickets, so its dirty
# row (a Comment on a tracked issue is admitted by identifier) had no reader.
def test_dirty_row_for_a_ticket_tracked_under_a_removed_repo_drains() -> None:
    linear = Linear([issue("AUT-1")], hits=[])
    clock = Clock()
    source, _gh = source_for(webhook_config(), linear, clock=clock)
    dirty = FakeDirty()
    source.linear_dirty = dirty
    retired = (TicketRef("acme/retired", "AUT-1", "linear"),)
    cycle(source, retired, exclude=frozenset({"AUT-1"}))
    assert linear.take() == SEARCHES
    dirty.add("id-AUT-1", "AUT-1", 1.0)

    for _ in range(2):
        clock.now += 60
        _, found = cycle(source, retired, exclude=frozenset({"AUT-1"}))
        assert found == ()

    assert dirty.rows == {}
    assert linear.take() == []


def _commented(identifier: str, at: float) -> dict:
    from datetime import datetime, timezone

    value = issue(identifier)
    value["comments"]["nodes"][0]["createdAt"] = datetime.fromtimestamp(
        at, tz=timezone.utc).isoformat()
    return value


@pytest.mark.parametrize(
    "seen, started, missed",
    [
        (None, 0.0, [("id-AUT-1", "AUT-1", 500.0, 1000.0)]),
        (470.0, 0.0, []),
        (None, 480.0, []),
        (None, None, []),
    ],
    ids=["missed", "delivered", "listener-started-after", "no-listener"],
)
def test_a_reconcile_read_records_a_comment_no_delivery_covered(seen, started, missed) -> None:
    """#79: the direct read finds the miss; a delivered or pre-start comment is not one."""
    linear = Linear([_commented("AUT-1", 500.0)], hits=[])
    source, _gh = source_for(webhook_config(), linear, clock=Clock())
    dirty = FakeDirty()
    dirty.started = started
    if seen is not None:
        dirty.seen["id-AUT-1"] = seen
    source.linear_dirty = dirty

    cycle(source, refs("AUT-1"), exclude=frozenset({"AUT-1"}))

    assert dirty.missed == missed


def test_a_miss_is_recorded_through_orch_db_and_shown_as_a_warning(tmp_path: Path) -> None:
    from lib.linear_webhook import read_health

    database = tmp_path / "orch.db"
    _dispatcher(database)
    with sqlite3.connect(database) as connection:
        ensure_tables(connection)
        connection.execute("INSERT INTO linear_webhook_health VALUES (1, 0, NULL, NULL)")
    linear = Linear([_commented("AUT-1", 500.0)], hits=[])
    source, _gh = source_for(webhook_config(), linear, clock=Clock())
    source.linear_dirty = LinearDirtyQueue(database)

    cycle(source, refs("AUT-1"), exclude=frozenset({"AUT-1"}))

    with sqlite3.connect(database) as connection:
        health = read_health(connection)
    assert health is not None and health.missed == (("AUT-1", 1000.0),)
    assert health.warning(1000.0) == (
        "Linear webhook missed 1 delivery in 24h (newest AUT-1, found 0s ago)")
