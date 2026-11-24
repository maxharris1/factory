from __future__ import annotations

import sqlite3
from pathlib import Path

from lib.dispatch.types import StackedOn, StackParent
from lib.dispatcher import TicketRef
from lib.feeder import FEEDER_TABLE, ensure_tables
from lib.poll_source import FedPulls, LivePollSource
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue, linear_transport_for

PARENT = "factory/issue-1449"
LIVE_PARENT_SHA = "7d0e" * 10


def record(database: Path, issue: str, ticket_id: str, tracker: str) -> None:
    with sqlite3.connect(database) as connection:
        ensure_tables(connection)
        connection.execute(
            f"""
            INSERT INTO {FEEDER_TABLE} (repo, issue, tracker, title, harness, state, risk,
                issue_created_at, branch, base_branch, pr_number, ticket_id, first_seen_at, seen_at,
                stack_parent_pr, stack_parent_branch, stack_parent_head_at_feed)
            VALUES ('acme/widgets', ?, ?, 'Child', 'claude', 'fed', 'high',
                1, 'factory/child', ?, '12', ?, 1, 1, 1449, ?, ?)
            """,
            (issue, tracker, PARENT, ticket_id, PARENT, "1" * 40),
        )


def github_ticket(tmp_path: Path, base: str):
    database = tmp_path / "orch.db"
    record(database, "1450", "12", "github")
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    gh.pull = {"state": "open", "base": {"ref": base, "sha": LIVE_PARENT_SHA}}
    source = LivePollSource(config(), gh_runner=gh, fed_pulls=FedPulls(database))
    (ticket,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))
    return ticket


def test_a_pull_on_its_recorded_parent_is_stacked_on_the_live_parent_head(tmp_path: Path) -> None:
    ticket = github_ticket(tmp_path, PARENT)

    assert ticket.stack == StackParent(1449, PARENT, LIVE_PARENT_SHA)
    assert ticket.factory.base_branch == PARENT


def test_a_pull_retargeted_to_main_is_not_stacked(tmp_path: Path) -> None:
    ticket = github_ticket(tmp_path, "main")

    assert ticket.stack is None
    assert ticket.factory.base_branch == "main"
    assert ticket.stacked_on == StackedOn(1449, PARENT, "main")


def test_a_ticket_targeted_child_keeps_the_project_default_branch(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    record(database, "1450", "12", "github")
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {FEEDER_TABLE} SET target_source = 'ticket'")
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = {"state": "open", "base": {"ref": "main", "sha": LIVE_PARENT_SHA}}
    source = LivePollSource(config(), gh_runner=gh, fed_pulls=FedPulls(database))
    (ticket,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))

    assert ticket.stacked_on == StackedOn(1449, PARENT, "main")


def test_a_linear_ticket_on_its_recorded_parent_is_stacked(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    record(database, "AUT-42", "AUT-42", "linear")
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = {"state": "open", "base": {"ref": PARENT, "sha": LIVE_PARENT_SHA}}
    issue = linear_issue()
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=linear_transport_for(issue),
        fed_pulls=FedPulls(database),
    )

    ticket = source._linear_ticket(source.config.projects[0], issue)

    assert ticket.stack == StackParent(1449, PARENT, LIVE_PARENT_SHA)
    assert ticket.factory.base_branch == PARENT
    assert ticket.stacked_on == StackedOn(1449, PARENT, "main")


def test_no_feeder_table_means_no_stack(tmp_path: Path) -> None:
    assert FedPulls(tmp_path / "orch.db").stack_parent("acme/widgets", "12") is None
