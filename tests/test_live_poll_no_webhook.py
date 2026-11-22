"""No linear_webhook in orch.yml: the poller behaves exactly as it did before.

These pins were recorded on main at 5a1fbeb, before the Linear webhook change
(issue #55), and pass unchanged on both sides of it. This module imports only
code that existed then, so it can be run against the old tree.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

from lib.dispatcher import Dispatcher
from lib.poll_source import HTTPResult, LivePollSource
from lib.queue import SQLiteJobQueue
from tests.test_live_poll import (
    FACTORY,
    FakeGh,
    config,
    linear_issue,
    linear_search_page,
)

REPO = "acme/widgets"


def issue(identifier: str, *, state: str = "started", project: str = "project-1"):
    value = linear_issue(identifier=identifier, project=project)
    value["id"] = f"id-{identifier}"
    value["comments"]["nodes"][0]["id"] = f"comment-{identifier}"
    value["state"] = {"id": f"state-{state}", "type": state, "position": 1}
    return value


class Linear:
    """Fake Linear GraphQL: search hits plus direct issue reads, logged."""

    def __init__(self, issues: list[dict], hits: list[str] | None = None):
        self.by_key = {}
        for value in issues:
            self.by_key[value["id"]] = value
            self.by_key[value["identifier"]] = value
        self.hits = hits if hits is not None else [i["identifier"] for i in issues]
        self.log: list[tuple[str, str]] = []

    def __call__(self, url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        if request["query"].startswith("query FactoryPoll"):
            self.log.append(("search", request["variables"]["term"]))
            return linear_search_page(
                [
                    {
                        "id": self.by_key[key]["id"],
                        "identifier": self.by_key[key]["identifier"],
                        "project": self.by_key[key]["project"],
                    }
                    for key in self.hits
                ]
            )
        key = request["variables"]["id"]
        self.log.append(("issue", key))
        return HTTPResult(
            200, json.dumps({"data": {"issue": self.by_key.get(key)}})
        )

    def take(self) -> list[tuple[str, str]]:
        taken = list(self.log)
        self.log.clear()
        return taken


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def source_for(cfg, linear: Linear, *, clock=None, errors=None, deferrals=None):
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    source = LivePollSource(
        cfg,
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=linear,
        clock=clock or Clock(),
        error_sink=(
            (lambda repo, ticket, error: errors.append((repo, ticket, error)))
            if errors is not None
            else None
        ),
        deferral_sink=deferrals.append if deferrals is not None else None,
    )
    return source, gh


SEARCHES = [
    ("search", "orch:v1 kind=brief"),
    ("search", "orch:v1 kind=go"),
]


def _dispatcher(database: Path) -> Dispatcher:
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    return Dispatcher(
        database,
        queue,
        trusted_logins={"github": {}, "linear": {"linear:user-1": {"grok-bot"}}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )


def _pinned_run(tmp_path: Path, *, attach_raising: bool) -> tuple[list, list]:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    with dispatcher.queue.connect() as connection:
        for identifier in ("AUT-1", "AUT-2"):
            connection.execute(
                "INSERT INTO tracked_tickets VALUES (?, ?, 'linear', 0)",
                (REPO, identifier),
            )
    linear = Linear(
        [issue("AUT-1"), issue("AUT-2"), issue("AUT-3")],
        hits=["AUT-1", "AUT-2", "AUT-3"],
    )
    clock = Clock()
    source, gh = source_for(config(linear=True), linear, clock=clock)
    source.error_sink = dispatcher.record_poll_error
    source.deferral_sink = dispatcher.record_poll_deferral
    if attach_raising:

        class Explodes:
            def pending(self):
                raise AssertionError("no webhook: dirty queue must not be read")

            def ack(self, rows):
                raise AssertionError("no webhook: dirty queue must not be acked")

        def no_active():
            raise AssertionError("no webhook: active jobs must not be read")

        source.linear_dirty = Explodes()
        source.active_tickets = no_active
    reads = []
    gh_calls = []
    for _ in range(3):
        dispatcher.poll_once(source)
        reads.append(linear.take())
        gh_calls.append(len(gh.calls))
        gh.calls.clear()
        clock.now += 60
    return reads, gh_calls


# Recorded on main at 5a1fbeb (before the webhook change).
PINNED_READS = [
    SEARCHES + [("issue", "id-AUT-3"), ("issue", "AUT-1"), ("issue", "AUT-2")],
    SEARCHES + [("issue", "AUT-3"), ("issue", "AUT-1"), ("issue", "AUT-2")],
    SEARCHES + [("issue", "AUT-3"), ("issue", "AUT-1"), ("issue", "AUT-2")],
]
PINNED_GH_CALLS = [20, 19, 19]


def test_no_webhook_poll_cycles_make_exactly_todays_linear_reads(
    tmp_path: Path,
) -> None:
    reads, gh_calls = _pinned_run(tmp_path, attach_raising=False)

    assert reads == PINNED_READS
    assert gh_calls == PINNED_GH_CALLS


def test_no_webhook_poller_never_touches_a_dirty_queue_or_job_list(
    tmp_path: Path,
) -> None:
    plain_reads, plain_gh = _pinned_run(tmp_path / "a", attach_raising=False)
    attached_reads, attached_gh = _pinned_run(tmp_path / "b", attach_raising=True)

    assert attached_reads == plain_reads == PINNED_READS
    assert attached_gh == plain_gh == PINNED_GH_CALLS


def test_no_webhook_dispatcher_creates_no_webhook_tables(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    _pinned_run(tmp_path, attach_raising=False)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "linear_dirty" not in tables
    assert "linear_webhook_health" not in tables
    assert "linear_webhook_polling" not in tables
    assert "linear_project_issues" not in tables
