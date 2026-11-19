from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib.dashboard import _read_db
from lib.dispatch.types import TicketRef
from lib.github_actions import GitHubAdapterError
from lib.poll_source import HTTPResult, LivePollSource
from lib.pr_title import SOURCES_TABLE
from tests.merge_queue_support import APPROVE, PR, Board, sha
from tests.test_dispatcher import (
    BASE_705,
    READY,
    BranchActions705,
    _jobs_705,
    _outcomes,
    _pr_705,
    _running_build_705,
    comment,
    setup,
)
from tests.test_live_poll import (
    FACTORY,
    FakeGh,
    config,
    linear_issue,
    linear_search_page,
    linear_states_page,
)

UNSAT = APPROVE.replace("verdict=APPROVE", "verdict=REVISE unsat=ac-1")


def reviewed_once(board: Board) -> PR:
    pr = board.open("42", sha(1))
    board.go(pr, "go-42")
    board.poll()
    board.finish("claude")
    board.ready(pr, pr.head)
    board.poll()
    board.finish("codex")
    return pr


def waiting_on_max(board: Board) -> PR:
    pr = reviewed_once(board)
    board.post(pr, "revise-1", UNSAT.format(round="1/3", head=pr.head), "codex-login")
    board.poll()
    assert board.state("42")["workflow_state"] == "waiting-max"
    assert board.state("42")["approved_head"] is None
    return pr


def closed_state(board: Board) -> tuple[str, str]:
    with board.queue.connect() as connection:
        row = connection.execute(
            """
            SELECT to_state, source FROM ticket_transitions
            WHERE repo = 'owner/repo' AND ticket_id = '42'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    return str(row["to_state"]), str(row["source"])


def ship_failed_rows(board: Board) -> list[dict]:
    with board.queue.connect() as connection:
        flags = _read_db(connection, board.now[0])["attention_flags"]
    return [flag for flag in flags if flag["reason"] == "ship-failed"]


def assert_closed_out(board: Board) -> None:
    assert closed_state(board) == ("abandoned", "closed-unmerged")
    assert [job for job in board.jobs() if job["state"] == "queued"] == []
    assert board.actions.handoffs == []
    assert board.actions.merges == []
    assert ship_failed_rows(board) == []


def test_a_pr_closed_while_waiting_on_max_ends_its_ticket(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = waiting_on_max(board)
    pr.closed = True
    board.poll()

    assert_closed_out(board)
    assert board.actions.states[-1] == "abandoned"


def test_an_approve_on_a_pr_closed_while_waiting_on_max_ships_nothing(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = waiting_on_max(board)
    pr.closed = True
    board.approve(pr)
    board.poll()
    board.poll()

    assert_closed_out(board)


def test_an_approve_on_a_pr_closed_during_review_ships_nothing(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = reviewed_once(board)
    pr.closed = True
    board.approve(pr)
    board.poll()
    board.poll()

    assert_closed_out(board)


def test_a_pr_closed_while_shipping_clears_the_failed_ship(tmp_path: Path) -> None:
    board = Board(tmp_path)
    pr = reviewed_once(board)
    handoff = board.actions.handoff

    def closed_under_the_ship(ticket, head):
        pr.closed = True
        raise GitHubAdapterError("cannot hand off a pull request that is not open")

    board.actions.handoff = closed_under_the_ship
    board.approve(pr)
    board.poll()
    assert ship_failed_rows(board)

    board.actions.handoff = handoff
    board.poll()

    assert_closed_out(board)


@pytest.mark.job_bound
def test_a_pr_github_closed_for_an_emptied_branch_keeps_building(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path, review_actions=BranchActions705(BASE_705, ahead=0))
    build = _running_build_705(queue)
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) "
            "VALUES ('owner/repo', '42', 'building')"
        )
    ready = comment("ready-base", READY.format(head=BASE_705), author="claude-login")
    queue.finish(build.id, owner="claude", handoff_comment_id=ready.id)
    dispatcher.process(_pr_705(ready, head=BASE_705, closed=True, commits=0))
    dispatcher.reconcile_jobs()

    with queue.connect() as connection:
        state = connection.execute("SELECT workflow_state FROM ticket_state").fetchone()[0]
    assert state == "building"
    assert _outcomes(queue)["ready-base"] == "rejected-ready-at-base"
    assert _jobs_705(queue) == [("build", "done", 1), ("build", "queued", 1)]


BLOCKED = APPROVE.replace("verdict=APPROVE", "verdict=BLOCKED")


def test_a_pr_closed_while_its_ticket_is_blocked_ends_it_within_one_poll(
    tmp_path: Path,
) -> None:
    board = Board(tmp_path)
    pr = reviewed_once(board)
    board.post(pr, "blocked-1", BLOCKED.format(round="1/3", head=pr.head), "codex-login")
    board.poll()
    board.poll()
    assert board.state("42")["workflow_state"] == "blocked"

    pr.closed = True
    board.poll()

    assert closed_state(board) == ("abandoned", "closed-unmerged")
    assert ship_failed_rows(board) == []


def test_the_closed_probe_reads_only_each_pulls_state(monkeypatch) -> None:
    from lib.github_actions import GitHubReviewActions

    queries: list[str] = []

    def answer(*args: str, **kwargs) -> dict:
        queries.append(next(arg for arg in args if arg.startswith("query=")))
        return {
            "data": {
                "repository": {
                    "p1": {"state": "OPEN"},
                    "p2": {"state": "CLOSED"},
                    "p3": {"state": "MERGED"},
                    "p4": None,
                }
            },
            "errors": [{"path": ["repository", "p4"], "message": "not found"}],
        }

    actions = GitHubReviewActions()
    monkeypatch.setattr(actions, "_json", answer)

    assert actions.closed_pulls("owner/repo", ["1", "2", "3", "4"]) == {"2", "3"}
    assert len(queries) == 1
    assert "fragment pull on PullRequest { state }" in queries[0]


def live_source(dispatcher, gh, now, requests=None, **kwargs) -> LivePollSource:
    def transport(url, headers, body, timeout):
        request = json.loads(body)
        if requests is not None:
            requests.append(request)
        query = request["query"]
        if query.startswith("query FactoryStates"):
            return linear_states_page(request["variables"])
        if query.startswith("query FactoryPoll"):
            return linear_search_page([])
        return HTTPResult(200, json.dumps({"data": {"issue": linear_issue()}}))

    return LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "test-only"},
        gh_runner=gh,
        linear_transport=transport,
        terminal_sink=dispatcher.record_terminal,
        error_sink=dispatcher.record_poll_error,
        clock=lambda: now[0],
        sleep=lambda _: None,
        **kwargs,
    )


def hold(queue, ticket_id: str, tracker: str, now: float, pr_number: str | None = None) -> None:
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO tracked_tickets VALUES ('acme/widgets', ?, ?, 0)", (ticket_id, tracker)
        )
        connection.execute(
            "INSERT INTO ticket_state (repo, ticket_id, workflow_state) "
            "VALUES ('acme/widgets', ?, 'blocked')",
            (ticket_id,),
        )
        connection.execute("INSERT INTO ticket_reads VALUES ('acme/widgets', ?, ?)", (ticket_id, now))
        if pr_number is not None:
            connection.execute(
                f"INSERT INTO {SOURCES_TABLE} (repo, ticket_id, source, pr_number) "
                "VALUES ('acme/widgets', ?, ?, ?)",
                (ticket_id, ticket_id, pr_number),
            )


def outcome(queue, ticket_id: str) -> tuple[str, list[tuple[str, str]]]:
    with queue.connect() as connection:
        state = connection.execute(
            "SELECT workflow_state FROM ticket_state WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()[0]
        transitions = connection.execute(
            "SELECT to_state, source FROM ticket_transitions WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
    return str(state), [tuple(row) for row in transitions]


def linear_gh(state: str) -> FakeGh:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = {"state": state, "merged": False, "commits": 1}
    gh.branch_pulls = [
        {
            "number": 12,
            "state": state,
            "head": {"ref": "max/aut-42-fixture", "repo": {"full_name": "acme/widgets"}},
        }
    ]
    return gh


def branch_lookups(gh: FakeGh) -> list[list[str]]:
    return [call for call in gh.calls if "repos/acme/widgets/pulls" in call]


def pull_reads(gh: FakeGh) -> list[list[str]]:
    return [call for call in gh.calls if "repos/acme/widgets/pulls/12" in call]


def state_probes(gh: FakeGh) -> list[list[str]]:
    return [call for call in gh.calls if "graphql" in call]


ABANDONED = ("abandoned", [("abandoned", "closed-unmerged")])


def test_a_held_linear_ticket_whose_pr_closed_ends_on_the_first_poll(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "AUT-42", "linear", now[0])
    gh = linear_gh("closed")

    dispatcher.poll_once(live_source(dispatcher, gh, now))

    assert outcome(queue, "AUT-42") == ABANDONED


def test_a_held_linear_ticket_with_a_stored_pr_is_probed_by_number(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "AUT-42", "linear", now[0], pr_number="12")
    gh = linear_gh("closed")

    dispatcher.poll_once(live_source(dispatcher, gh, now))

    assert outcome(queue, "AUT-42") == ABANDONED
    assert state_probes(gh)
    assert gh.calls.index(state_probes(gh)[0]) < gh.calls.index(branch_lookups(gh)[0])
    assert len(branch_lookups(gh)) == 1


def test_a_held_linear_ticket_with_an_open_pr_keeps_its_cadence(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "AUT-42", "linear", now[0])
    gh = linear_gh("open")
    requests: list[dict] = []
    source = live_source(dispatcher, gh, now, requests)

    dispatcher.poll_once(source)
    now[0] = 160.0
    dispatcher.poll_once(source)

    assert outcome(queue, "AUT-42") == ("blocked", [])
    assert not [r for r in requests if r["query"].startswith("query FactoryIssue")]
    assert pull_reads(gh) == []
    assert len(branch_lookups(gh)) == 1
    assert len(state_probes(gh)) == 1


class StaleCacheGh(FakeGh):
    def __call__(self, argv, timeout):
        if "--cache" not in argv:
            return super().__call__(argv, timeout)
        pull, branch_pulls = self.pull, self.branch_pulls
        self.pull = {**pull, "state": "open"}
        self.branch_pulls = [{**item, "state": "open"} for item in branch_pulls]
        try:
            return super().__call__(argv, timeout)
        finally:
            self.pull, self.branch_pulls = pull, branch_pulls


def assert_ended_from_an_uncached_read(dispatcher, queue, gh, ticket_id: str, read: str) -> None:
    assert outcome(queue, ticket_id) == ABANDONED
    with queue.connect() as connection:
        flags = _read_db(connection, dispatcher.queue.now())["attention_flags"]
    assert [flag for flag in flags if flag["reason"] == "ship-failed"] == []
    with queue.connect() as connection:
        assert connection.execute("SELECT id FROM jobs WHERE state = 'queued'").fetchall() == []
    reads = [call for call in gh.calls if read in call]
    assert reads
    assert all("--cache" not in call for call in reads)


def test_after_a_restart_a_closed_github_pr_is_not_read_from_gh_cache(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "7", "github", now[0])
    dispatcher.flag_attention("acme/widgets", "7", "ship-failed", "handoff ship failed")
    gh = StaleCacheGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = {"state": "closed", "merged": False, "commits": 1}

    dispatcher.poll_once(live_source(dispatcher, gh, now))

    assert_ended_from_an_uncached_read(dispatcher, queue, gh, "7", "repos/acme/widgets/pulls/7")


def test_after_a_restart_a_closed_linear_pr_is_not_read_from_gh_cache(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "AUT-42", "linear", now[0], pr_number="12")
    dispatcher.flag_attention("acme/widgets", "AUT-42", "ship-failed", "handoff ship failed")
    gh = StaleCacheGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = linear_gh("closed").pull
    gh.branch_pulls = linear_gh("closed").branch_pulls

    dispatcher.poll_once(live_source(dispatcher, gh, now))

    assert_ended_from_an_uncached_read(
        dispatcher, queue, gh, "AUT-42", "repos/acme/widgets/pulls/12"
    )


def slow_source(
    now, seconds: float, budget: float = 10.0, pull: dict | None = None
) -> LivePollSource:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = {"state": "closed", "merged": False, "commits": 1} if pull is None else pull

    def runner(argv, timeout):
        if any(re.fullmatch(r"repos/acme/widgets/pulls/[0-9]+", part) for part in argv):
            now[0] += seconds
        return gh(argv, timeout)

    return LivePollSource(
        config(),
        environ={"LINEAR_KEY": "test-only"},
        gh_runner=runner,
        clock=lambda: now[0],
        sleep=lambda _: None,
        tracked_read_budget_seconds=budget,
    )


def active(number: str) -> TicketRef:
    return TicketRef("acme/widgets", number, "github")


def closed(number: str) -> TicketRef:
    return TicketRef("acme/widgets", number, "github", closed=True)


def read_ids(source: LivePollSource, refs: tuple[TicketRef, ...]) -> list[str]:
    source.begin_cycle()
    return [ticket.ticket_id for ticket in source.tracked_tickets(refs)]


def test_a_closed_ref_is_read_even_when_active_reads_spend_the_budget() -> None:
    now = [100.0]
    source = slow_source(now, seconds=4)

    assert read_ids(source, (active("1"), active("2"), active("3"), closed("9"))) == ["9", "1"]


def test_every_newly_closed_ref_is_read_past_the_budget() -> None:
    now = [100.0]
    source = slow_source(now, seconds=12)

    assert read_ids(source, (closed("8"), closed("9"))) == ["8", "9"]


def test_a_closed_ref_skips_the_budget_only_on_its_first_read_after_closure() -> None:
    now = [100.0]
    source = slow_source(now, seconds=6)

    assert read_ids(source, (closed("9"),)) == ["9"]
    assert read_ids(source, (active("1"), active("2"), closed("9"))) == ["1"]


def test_a_linear_ticket_held_after_a_full_read_is_probed_by_that_reads_pr(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    with queue.connect() as connection:
        connection.execute("INSERT INTO tracked_tickets VALUES ('acme/widgets', 'AUT-42', 'linear', 0)")
    gh = linear_gh("open")
    source = live_source(dispatcher, gh, now)
    dispatcher.poll_once(source)
    with queue.connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO ticket_state (repo, ticket_id, workflow_state) "
            "VALUES ('acme/widgets', 'AUT-42', 'blocked')"
        )
        connection.execute("UPDATE ticket_reads SET read_at = 160 WHERE ticket_id = 'AUT-42'")
    now[0] = 160.0
    gh.pull = {**gh.pull, "state": "closed"}

    dispatcher.poll_once(source)

    assert outcome(queue, "AUT-42")[0] == "abandoned"
    assert len(state_probes(gh)) == 1
    assert gh.calls.index(state_probes(gh)[0]) < gh.calls.index(branch_lookups(gh)[1])


def test_a_held_linear_ticket_whose_branch_lost_its_pr_ends_from_its_stored_pr(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "AUT-42", "linear", now[0], pr_number="12")
    dispatcher.flag_attention("acme/widgets", "AUT-42", "ship-failed", "handoff ship failed")
    gh = StaleCacheGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = linear_gh("closed").pull
    gh.branch_pulls = []

    dispatcher.poll_once(live_source(dispatcher, gh, now))

    assert_ended_from_an_uncached_read(
        dispatcher, queue, gh, "AUT-42", "repos/acme/widgets/pulls/12"
    )


def test_a_newer_open_pr_on_the_branch_supersedes_the_stored_pr(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "AUT-42", "linear", now[0], pr_number="12")
    gh = linear_gh("closed")
    gh.branch_pulls = [{**gh.branch_pulls[0], "number": 15, "state": "open"}]
    closed_pull, open_pull = gh.pull, {**gh.pull, "state": "open"}

    def runner(argv, timeout):
        gh.pull = open_pull if "repos/acme/widgets/pulls/15" in argv else closed_pull
        return gh(argv, timeout)

    dispatcher.poll_once(live_source(dispatcher, runner, now))

    assert outcome(queue, "AUT-42") == ("blocked", [])
    assert [call for call in gh.calls if "repos/acme/widgets/pulls/15" in call]
    assert pull_reads(gh) == []


def test_a_stopped_linear_ticket_due_for_its_read_ends_when_its_pr_closed(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "AUT-42", "linear", now[0], pr_number="12")
    dispatcher.flag_attention("acme/widgets", "AUT-42", "ship-failed", "handoff ship failed")
    gh = StaleCacheGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = linear_gh("closed").pull
    gh.branch_pulls = linear_gh("closed").branch_pulls
    now[0] = 401.0

    dispatcher.poll_once(live_source(dispatcher, gh, now))

    assert_ended_from_an_uncached_read(
        dispatcher, queue, gh, "AUT-42", "repos/acme/widgets/pulls/12"
    )


def test_a_stopped_github_ticket_due_for_its_read_ends_when_its_pr_closed(
    tmp_path: Path,
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "7", "github", now[0])
    gh = StaleCacheGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = {"state": "closed", "merged": False, "commits": 1}
    now[0] = 401.0

    dispatcher.poll_once(live_source(dispatcher, gh, now))

    assert_ended_from_an_uncached_read(dispatcher, queue, gh, "7", "repos/acme/widgets/pulls/7")


def test_a_pr_closed_again_after_a_read_saw_it_open_ends_its_ticket(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    hold(queue, "9", "github", now[0])
    for number in ("1", "2"):
        with queue.connect() as connection:
            connection.execute(
                "INSERT INTO tracked_tickets VALUES ('acme/widgets', ?, 'github', 0)", (number,)
            )
            connection.execute(
                "INSERT INTO ticket_state (repo, ticket_id, workflow_state) "
                "VALUES ('acme/widgets', ?, 'review')",
                (number,),
            )
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    reopened = [True]

    def runner(argv, timeout):
        read = next(
            (part for part in argv if re.fullmatch(r"repos/acme/widgets/pulls/[0-9]+", part)),
            None,
        )
        if read is None:
            state = "closed"
        else:
            now[0] += 6
            state = "closed" if read.endswith("/9") and not reopened[0] else "open"
        gh.pull = {"state": state, "merged": False, "commits": 1}
        return gh(argv, timeout)

    source = live_source(dispatcher, runner, now, tracked_read_budget_seconds=10)
    dispatcher.poll_once(source)
    assert outcome(queue, "9") == ("blocked", [])

    reopened[0] = False
    now[0] = 160.0
    dispatcher.poll_once(source)

    assert outcome(queue, "9") == ABANDONED


def test_a_closed_ref_whose_read_saw_its_pr_open_keeps_its_exemption() -> None:
    now = [100.0]
    pull = {"state": "open", "merged": False, "commits": 1}
    source = slow_source(now, seconds=4, pull=pull)

    assert read_ids(source, (closed("9"),)) == ["9"]
    pull["state"] = "closed"
    assert read_ids(source, (active("1"), active("2"), closed("9"))) == ["9", "1"]
