"""Poller side of the Linear webhook (issue #55).

With linear_webhook configured, a cycle fetches only dirty tickets and
tracked tickets with active jobs in full; the discovery search and every
tracked read become a reconcile every reconcile_seconds. With no webhook
configured, the poller behaves exactly as before: the pins below were
recorded on main at 5a1fbeb and must not change.
"""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import LINEAR_HANDOFF_QUERY, TicketRef
from lib.linear_webhook import DirtyRow, LinearDirtyQueue, WebhookApp
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
    assert errors == [("linear-webhook", "-", "OperationalError")]


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
    import hashlib
    import hmac

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
    assert isinstance(hooked_source.linear_dirty, LinearDirtyQueue)
    assert hooked_source.linear_dirty.path == database
    assert hooked_source.active_tickets == dispatcher.active_job_tickets
