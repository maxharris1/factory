import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib.dashboard import _ticket_ref, collect_snapshot, render_pages
from lib.dashboard_page import _ticket
from lib.github_actions import GhResult, GitHubAdapterError, LinearReviewActions
from lib.pr_title import SOURCES_TABLE, github_source, label, prefixed
from lib.queue import JobRequest
from lib.status import SQLiteStatusReader, build_status, render
from tests.test_dispatcher import GO, FakeReviewActions, FakeSource, comment, setup, ticket
from tests.test_dashboard_layout import _run_page, needs_node
from tests.test_github_actions import REPO, adapter, pr


def pull(*, body: str | None = None, branch: str = "feature/x", title: str = "Do it") -> dict:
    return {"body": body, "head": {"ref": branch}, "title": title}


@pytest.mark.parametrize(
    ("given", "source"),
    [
        (pull(body="Fixes #115\n\nTicket: ..."), "#115"),
        (pull(body="This closes #7 and fixes #8."), "#7"),
        (pull(body="Resolved: #12"), "#12"),
        (pull(branch="factory/issue-115"), "#115"),
        (pull(title="#107: Status shows issues first"), "#107"),
        (pull(body="Fixes #115", branch="factory/issue-99"), "#115"),
        (pull(title="#107: Kept", body="Fixes #115"), "#107"),
        (pull(body="Fixes owner/other#5, see #3"), None),
        (pull(body="Fixes owner/repo#11"), "#11"),
        (pull(body="closes Owner/Repo#11"), "#11"),
        (pull(body="Fixes owner/other#5 and fixes owner/repo#6"), "#6"),
        (pull(body="Resolves https://github.com/owner/repo/issues/11"), "#11"),
        (pull(body="Fixes https://github.com/owner/other/issues/5"), None),
        (pull(body="Fixes https://github.com/owner/repo/pull/5"), None),
        (pull(body="Fixes owner/repo.js#5"), None),
        (pull(body="Fixes xowner/repo#5"), None),
        (pull(body="Fixes owner/repo#116"), None),
        (pull(body="prefixes #9"), None),
        (pull(body="Fixes #116"), None),  # the PR itself is not its source
        (pull(), None),
        ({"body": None, "head": None, "title": None}, None),
    ],
)
def test_a_github_prs_source_issue(given: dict, source: str | None) -> None:
    assert github_source(given, "owner/repo", "116") == source


@pytest.mark.parametrize(
    ("source", "title", "wanted"),
    [
        ("#115", "PR titles start with their source issue number",
         "#115: PR titles start with their source issue number"),
        ("#115", "#115: Already prefixed", "#115: Already prefixed"),
        ("#115", "#115 no colon", "#115: no colon"),
        ("#115", "#1150: another issue", "#115: #1150: another issue"),
        ("AUT-2284", "Fix login", "AUT-2284: Fix login"),
        ("AUT-2284", "AUT-2284: Fix login", "AUT-2284: Fix login"),
        ("AUT-2284", "[AUT-2284] Fix login", "AUT-2284: Fix login"),
        ("AUT-2284", "AUT-22840: other", "AUT-2284: AUT-22840: other"),
        ("AUT-2284", "AUT-2284", "AUT-2284"),
    ],
)
def test_prefixed_titles_are_idempotent(source: str, title: str, wanted: str) -> None:
    assert prefixed(source, title) == wanted
    assert prefixed(source, wanted) == wanted


@pytest.mark.parametrize("source", ["115", "#0", "aut-1", "AUT-", "#1; rm"])
def test_prefixed_refuses_a_source_that_is_not_an_issue_or_key(source: str) -> None:
    with pytest.raises(ValueError):
        prefixed(source, "title")


def test_labels_put_the_issue_first_and_the_pr_second() -> None:
    assert label("o/r", "116", "#115", "116") == "o/r#115 (PR #116)"
    assert label("o/r", "AUT-2284", "AUT-2284", "123") == "o/r#AUT-2284 (PR #123)"
    assert label("o/r", "AUT-2284", None, None) == "o/r#AUT-2284"
    assert label("o/r", "116", None, "116") == "o/r#116"


# --- the poller prefixes a PR title once (issue #115) -----------------------

def titled_ticket(
    title: str | None = "PR titles start with their source issue number",
    *,
    source: str | None = "#115",
    pr_number: str = "116",
    ticket_id: str = "116",
    tickets: str = "github",
    comments: tuple = (),
    repo: str = "owner/repo",
):
    return replace(
        ticket(*comments, tickets=tickets, ticket_id=ticket_id, repo=repo),
        pr_number=pr_number,
        source_issue=source,
        pr_title=title,
    )


def sources(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            f"SELECT repo, ticket_id, source, pr_number, titled_at IS NOT NULL FROM {SOURCES_TABLE}"
        ).fetchall()


def test_the_poller_prefixes_a_github_pr_title_once(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, _ = setup(tmp_path, review_actions=actions)

    dispatcher.process(titled_ticket())
    dispatcher.process(titled_ticket())
    # Max renames it afterwards: the poller does not fight the edit.
    dispatcher.process(titled_ticket("Renamed by hand"))

    assert actions.titles == [("116", "#115: PR titles start with their source issue number")]
    assert sources(tmp_path / "d.db") == [("owner/repo", "116", "#115", "116", 1)]


def test_the_poller_prefixes_a_linear_pr_title_with_the_ticket_key(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, _ = setup(tmp_path, review_actions=actions)

    dispatcher.process(
        titled_ticket("Fix login", source="AUT-2284", ticket_id="AUT-2284", pr_number="123",
                      tickets="linear")
    )

    assert actions.titles == [("123", "AUT-2284: Fix login")]
    assert sources(tmp_path / "d.db") == [("owner/repo", "AUT-2284", "AUT-2284", "123", 1)]


def test_a_title_that_already_has_the_prefix_is_not_edited(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, _ = setup(tmp_path, review_actions=actions)

    dispatcher.process(titled_ticket("#115: Already right"))

    assert actions.titles == []
    assert sources(tmp_path / "d.db") == [("owner/repo", "116", "#115", "116", 1)]


def test_a_failed_rename_is_retried_and_never_blocks_the_poll(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    actions.title_error = RuntimeError("gh down")
    dispatcher, queue = setup(tmp_path, review_actions=actions)
    go = comment("c1", GO.format(state="building"), author="grok-login")

    dispatcher.process(titled_ticket(comments=(go,)))

    assert [job.request.action for job in queue.outstanding()] == ["build"]
    assert sources(tmp_path / "d.db") == [("owner/repo", "116", "#115", "116", 0)]
    with sqlite3.connect(tmp_path / "d.db") as connection:
        assert connection.execute("SELECT error FROM poll_errors").fetchall() == [
            ("RuntimeError",)
        ]

    actions.title_error = None
    dispatcher.process(titled_ticket(comments=(go,)))

    assert actions.titles == [("116", "#115: PR titles start with their source issue number")]
    assert sources(tmp_path / "d.db") == [("owner/repo", "116", "#115", "116", 1)]


def test_a_long_title_is_cut_to_githubs_limit(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, _ = setup(tmp_path, review_actions=actions)

    dispatcher.process(titled_ticket("x" * 256))

    assert actions.titles == [("116", "#115: " + "x" * 250)]


def test_a_new_linked_pr_is_titled_again(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, _ = setup(tmp_path, review_actions=actions)
    linear = dict(source="AUT-7", ticket_id="AUT-7", tickets="linear")

    dispatcher.process(titled_ticket("First", pr_number="10", **linear))
    dispatcher.process(titled_ticket("Second", pr_number="11", **linear))

    assert actions.titles == [("10", "AUT-7: First"), ("11", "AUT-7: Second")]


def test_no_source_or_a_closed_pr_gets_no_rename(tmp_path: Path) -> None:
    actions = FakeReviewActions()
    dispatcher, _ = setup(tmp_path, review_actions=actions)

    dispatcher.process(titled_ticket(source=None))
    dispatcher.process(titled_ticket(None, ticket_id="117", pr_number="117"))

    assert actions.titles == []
    # The closed PR's source is still recorded for status and the dashboard.
    assert sources(tmp_path / "d.db") == [("owner/repo", "117", "#115", "117", 0)]


def test_the_build_job_carries_the_source_issue(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = comment("c1", GO.format(state="building"), author="grok-login")

    dispatcher.process(titled_ticket(comments=(go,)))

    (job,) = queue.outstanding()
    assert json.loads(job.request.detail)["source_issue"] == "#115"


def test_the_github_adapter_renames_the_pr_with_one_patch() -> None:
    actions, gh = adapter()
    patch = ("-X", "PATCH", f"{REPO}/pulls/42", "-f", "title=#7: Fix it")
    gh.route(patch, GhResult(0, "{}", ""))

    actions.set_title(pr(), "#7: Fix it")
    linear = replace(pr(tickets="linear", ticket_id="AUT-7"), pr_number="42")
    LinearReviewActions(move_ready_for_review=False, linear_ready=lambda _: None,
                        runner=gh).set_title(linear, "#7: Fix it")

    assert gh.keys() == [patch, patch]
    with pytest.raises(GitHubAdapterError):
        actions.set_title(pr(), "x" * 257)


def seeded(tmp_path: Path) -> tuple[str, dict]:
    """A dispatcher database with a GitHub and a Linear ticket: `orch status` and the snapshot."""
    dispatcher, queue = setup(tmp_path, now=[1_800_000_000.0])
    go = comment("c1", GO.format(state="building"), author="grok-login")
    github = titled_ticket(comments=(go,))
    dispatcher.poll_once(FakeSource(github, github=(github,)))
    dispatcher.process(
        titled_ticket("Fix login", source="AUT-2284", ticket_id="AUT-2284", pr_number="123",
                      tickets="linear")
    )
    queue.enqueue(
        JobRequest(repo="owner/repo", ticket_id="AUT-2284", action="review", recipient="codex",
                   source_comment_id="lin-1", timeout_seconds=3600, head="a" * 40, detail="{}"),
        dedupe_key="review-aut-2284",
    )
    reader = SQLiteStatusReader(tmp_path / "d.db")
    status = render(build_status(reader, reader, clock=lambda: 1_800_000_100.0))
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    return status, collect_snapshot(tmp_path / "d.db", log, now=1_800_000_100.0)


def test_orch_status_shows_the_issue_first_and_the_pr_second(tmp_path: Path) -> None:
    status, _ = seeded(tmp_path)

    assert "  owner/repo#115 (PR #116) build -> claude queued" in status
    assert "  owner/repo#AUT-2284 (PR #123) review -> codex queued" in status


def test_the_dashboard_shows_the_issue_first_and_the_pr_second(tmp_path: Path) -> None:
    _, snapshot = seeded(tmp_path)

    queued = {item["ticket_id"]: item for item in snapshot["queued"]}
    assert (queued["116"]["source"], queued["116"]["pr"]) == ("#115", "116")
    assert (queued["AUT-2284"]["source"], queued["AUT-2284"]["pr"]) == ("AUT-2284", "123")
    (tracked,) = snapshot["tickets"]
    assert (tracked["ticket_id"], tracked["source"], tracked["pr"]) == ("116", "#115", "116")
    pages = render_pages(snapshot)
    assert ">repo#115 (PR #116)<" in pages["index.html"]
    assert ">repo#115 (PR #116)<" in pages["job.html"]
    assert ">owner/repo#115 (PR #116)<" in pages["job.html"]


@pytest.mark.parametrize("source", ["ABCDEFGHIJ-1234567890", "#1234567890"])
def test_the_dashboard_keeps_every_accepted_source_whole(source: str) -> None:
    row = _ticket_ref({("owner/repo", "1234567891"): (source, "1234567891")}, "owner/repo", "1234567891")

    assert (row["source"], row["pr"]) == (source, "1234567891")
    assert _ticket(row, full=True) == f"owner/repo#{source.removeprefix('#')} (PR #1234567891)"


def test_the_dashboard_drops_a_stored_source_that_is_not_an_issue_or_key() -> None:
    row = _ticket_ref({("owner/repo", "116"): ("<script>", "116")}, "owner/repo", "116")

    assert (row["source"], row["pr"]) == (None, "116")
    assert _ticket(row, full=True) == "owner/repo#116"


@needs_node
def test_the_live_page_script_shows_the_issue_first_too(tmp_path: Path) -> None:
    _, snapshot = seeded(tmp_path)

    queue = _run_page(tmp_path, snapshot, "main", ["queue"])["text"]["queue"]
    tickets = _run_page(tmp_path, snapshot, "job", ["tickets"])["text"]["tickets"]

    assert "repo#115 (PR #116)" in queue
    assert "AUT-2284 (PR #123)" in queue
    assert "owner/repo#115 (PR #116)" in tickets


@pytest.mark.parametrize("repo", ["owner/" + "r" * 100, "owner/" + "r" * 130])
def test_the_dashboard_finds_the_source_of_a_repo_too_long_to_show_whole(
    tmp_path: Path, repo: str
) -> None:
    dispatcher, _ = setup(tmp_path, now=[1_800_000_000.0])
    go = comment("c1", GO.format(state="building"), author="grok-login")
    tracked = titled_ticket(comments=(go,), repo=repo)
    dispatcher.poll_once(FakeSource(tracked, github=(tracked,)))
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")

    snapshot = collect_snapshot(tmp_path / "d.db", log, now=1_800_000_100.0)

    assert (len(snapshot["queued"]), len(snapshot["tickets"])) == (1, 1)
    for row in (*snapshot["queued"], *snapshot["tickets"]):
        assert row["repo"].endswith("…")
        assert (row["source"], row["pr"]) == ("#115", "116")


def test_the_dashboard_finds_the_source_of_a_ticket_id_too_long_to_show_whole() -> None:
    ticket_id = "T" * 80

    row = _ticket_ref({("owner/repo", ticket_id): ("#115", "116")}, "owner/repo", ticket_id)

    assert row["ticket_id"].endswith("…")
    assert (row["source"], row["pr"]) == ("#115", "116")


def test_each_long_repo_keeps_its_own_source_when_the_shown_names_match(tmp_path: Path) -> None:
    dispatcher, _ = setup(tmp_path, now=[1_800_000_000.0])
    go = comment("c1", GO.format(state="building"), author="grok-login")
    repos = ["owner/" + "r" * 94 + suffix for suffix in "ab"]
    tracked = tuple(
        titled_ticket(comments=(go,), repo=repo, source=source)
        for repo, source in zip(repos, ("#115", "#114"))
    )
    dispatcher.poll_once(FakeSource(*tracked, github=tracked))
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")

    snapshot = collect_snapshot(tmp_path / "d.db", log, now=1_800_000_100.0)

    for section in ("queued", "tickets"):
        rows = snapshot[section]
        assert len({row["repo"] for row in rows}) == 1
        assert sorted((row["source"], row["pr"]) for row in rows) == [
            ("#114", "116"), ("#115", "116")
        ]
    page = render_pages(snapshot)["index.html"]
    assert "#115 (PR #116)" in page
    assert "#114 (PR #116)" in page
    assert "r" * 93 + "…#116" not in page
