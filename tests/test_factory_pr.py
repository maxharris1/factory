"""Issue #600: a Linear ticket stays bound to the PR the factory made for it."""

import json
import sqlite3
from pathlib import Path

from lib import cli
from lib.dashboard import collect_snapshot, render_pages
from lib.feeder import FEEDER_TABLE, ensure_tables
from lib.github_actions import GhResult
from lib.pr_title import SOURCES_TABLE
from lib.poll_source import FedPulls, LivePollSource
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue, linear_transport_for
from tests.test_live_poll_no_webhook import _dispatcher

BRANCH = "max/aut-2233-fix-login"


def contested_issue() -> dict:
    """AUT-2233 as Linear had it: someone else's PR 1338, titled with the key, linked too."""
    issue = linear_issue(identifier="AUT-2233")
    issue["gitBranchName"] = BRANCH
    issue["attachments"] = {
        "nodes": [
            {"url": "https://github.com/acme/widgets/pull/1338"},
            {"url": "https://github.com/acme/widgets/pull/1462"},
        ],
        "pageInfo": {"hasNextPage": False},
    }
    return issue


class TitleSearchGh(FakeGh):
    """A title search for the key finds only the other PR, 1338."""

    def __init__(self) -> None:
        super().__init__(FACTORY.format(tickets="linear", ship="handoff"))
        self.branch_pulls = []

    def __call__(self, argv, timeout):
        if "search/issues" in argv:
            self.calls.append(list(argv))
            return GhResult(0, json.dumps({"items": [{"number": 1338}]}), "")
        return super().__call__(argv, timeout)

    def searched(self) -> bool:
        return any("search/issues" in call for call in self.calls)


def record_fed_pull(database: Path, number: str) -> None:
    with sqlite3.connect(database) as connection:
        ensure_tables(connection)
        connection.execute(
            f"""
            INSERT INTO {FEEDER_TABLE} (repo, issue, tracker, title, harness, state, risk,
                issue_created_at, branch, pr_number, ticket_id, first_seen_at, seen_at)
            VALUES ('acme/widgets', 'AUT-2233', 'linear', 'Fix login', 'claude', 'done', 'high',
                1, ?, ?, 'AUT-2233', 1, 1)
            """,
            (BRANCH, number),
        )


def source_for(database: Path, gh: FakeGh, issue: dict) -> LivePollSource:
    return LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=linear_transport_for(issue),
        fed_pulls=FedPulls(database),
    )


def poll(database: Path, gh: FakeGh, issue: dict | None = None):
    source = source_for(database, gh, issue or contested_issue())
    return source._linear_ticket(source.config.projects[0], issue or contested_issue())


def test_the_feeders_recorded_pr_wins_over_linear_links_and_title_search(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    record_fed_pull(database, "1462")
    gh = TitleSearchGh()

    ticket = poll(database, gh)

    assert ticket.pr_number == "1462"
    assert not gh.searched()


def test_with_no_feeder_row_the_pr_on_the_issue_branch_is_the_factory_pr(tmp_path: Path) -> None:
    gh = TitleSearchGh()
    gh.branch_pulls = [
        {
            "number": 1462,
            "state": "open",
            "head": {"ref": BRANCH, "repo": {"full_name": "acme/widgets"}},
        }
    ]

    ticket = poll(tmp_path / "orch.db", gh)

    assert ticket.pr_number == "1462"
    assert not gh.searched()
    assert any(
        "repos/acme/widgets/pulls" in call and f"head=acme:{BRANCH}" in call and "state=all" in call
        for call in gh.calls
    )


def test_a_pr_named_or_linked_by_linear_alone_is_never_bound(tmp_path: Path) -> None:
    gh = TitleSearchGh()

    ticket = poll(tmp_path / "orch.db", gh)

    assert ticket.pr_number is None
    assert not gh.searched()


def test_the_open_branch_pr_wins_and_a_merged_one_is_still_seen(tmp_path: Path) -> None:
    def head(ref: str = BRANCH, repo: str = "acme/widgets") -> dict:
        return {"ref": ref, "repo": {"full_name": repo}}

    gh = TitleSearchGh()
    gh.branch_pulls = [
        {"number": 1500, "state": "closed", "head": head()},
        {"number": 1462, "state": "open", "head": head()},
        {"number": 1600, "state": "open", "head": head(ref=f"{BRANCH}-2")},
        {"number": 1700, "state": "open", "head": head(repo="fork/widgets")},
    ]
    assert poll(tmp_path / "orch.db", gh).pr_number == "1462"

    gh = TitleSearchGh()
    gh.branch_pulls = [
        {"number": 1400, "state": "closed", "head": head()},
        {"number": 1462, "state": "closed", "head": head()},
    ]
    gh.pull = {"state": "closed", "merged": True}
    ticket = poll(tmp_path / "orch.db", gh)
    assert (ticket.pr_number, ticket.merged) == ("1462", True)


def test_a_job_for_the_ticket_posts_to_the_factory_pr(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    record_fed_pull(database, "1462")

    dispatcher.process(poll(database, TitleSearchGh()))

    job = dispatcher.queue.claim("claude")
    assert job is not None
    detail = json.loads(job.request.detail or "{}")
    assert detail["pr_number"] == "1462"
    assert detail["post_to"].startswith("PR #1462 ")
    assert "1338" not in job.request.detail


def test_the_dashboard_row_shows_the_factory_pr_and_rebinds_a_stale_one(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    dispatcher = _dispatcher(database)
    record_fed_pull(database, "1462")
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"INSERT INTO {SOURCES_TABLE} (repo, ticket_id, source, pr_number, titled_at) "
            "VALUES ('acme/widgets', 'AUT-2233', 'AUT-2233', '1338', 5)"
        )
    gh = TitleSearchGh()
    gh.pull = {"state": "open", "title": "Fix login"}

    dispatcher.process(poll(database, gh))

    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    snapshot = collect_snapshot(database, log, now=100.0)
    (queued,) = [item for item in snapshot["queued"] if item["ticket_id"] == "AUT-2233"]
    assert (queued["source"], queued["pr"]) == ("AUT-2233", "1462")
    page = render_pages(snapshot)["index.html"]
    assert "(PR #1462)" in page
    assert "1338" not in page
    with sqlite3.connect(database) as connection:
        assert connection.execute(f"SELECT pr_number FROM {SOURCES_TABLE}").fetchall() == [
            ("1462",)
        ]


def test_the_poller_runtime_reads_the_feeders_recorded_pulls(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    _dispatcher(database)
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\n"
        "grok:\n  webhook_url_env: U\n  webhook_key_env: K\n"
        "trusted_logins:\n  grok-login: [grok-bot]\n"
        "projects:\n  - repo: acme/widgets\n    factory_ref: main\n",
        encoding="utf-8",
    )
    _, source = cli._runtime(database, orch, create=False)
    assert source.fed_pulls.get("ACME/Widgets", "AUT-2233") is None

    record_fed_pull(database, "1462")

    assert source.fed_pulls.get("ACME/Widgets", "AUT-2233") == "1462"
