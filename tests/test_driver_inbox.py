import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import Dispatcher
from lib.github_actions import GitHubAdapterError
from lib.grok_delivery import GrokDeliveryError
from lib.orch_yml import DriverInbox, Project
from lib.queue import Job, SQLiteJobQueue
from tests.test_hooks import (
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    T7Hook,
    T8Hook,
    ticket,
)

INBOX = DriverInbox("owner/repo", 7)
AUT0 = "aut0-code/aut0"
SECRET = "patient Jane Roe DOB 1970-01-01"
LINEAR = "https://linear.app/aut0/issue/AUT-2233"
CAUSE = "https://github.com/owner/repo/pull/42#issuecomment-901"


class InboxIssue(FakeReviewActions):
    def __init__(self) -> None:
        super().__init__()
        self.posts: list[tuple[str, int, str]] = []
        self.fail: BaseException | None = None
        self.marker_checks = 0

    def post_issue_comment(self, repo: str, number: int, body: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.posts.append((repo, number, body))

    def issue_has_marker(self, repo: str, number: int, marker: str) -> bool:
        self.marker_checks += 1
        return any(
            (r, n) == (repo, number) and marker in body.splitlines() for r, n, body in self.posts
        )


class FlakyGrok(FakeGrokDelivery):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def deliver(self, job: Job, *, logical_key: str) -> str:
        if self.failures:
            self.failures -= 1
            raise GrokDeliveryError("webhook down", detail="HTTP-502")
        return super().deliver(job, logical_key=logical_key)


def make(
    tmp_path: Path,
    *,
    issue: InboxIssue | None = None,
    grok: FakeGrokDelivery | None = None,
    projects: tuple[Project, ...] | None = None,
) -> tuple[Dispatcher, InboxIssue, FakeGrokDelivery]:
    issue = issue or InboxIssue()
    grok = grok if grok is not None else FakeGrokDelivery()
    path = tmp_path / "d.db"
    clock = [100.0]
    queue = SQLiteJobQueue(path, clock=lambda: clock[0])
    if projects is None:
        projects = (
            Project("owner/repo", driver_inbox=INBOX),
            Project(AUT0, driver_inbox=INBOX),
        )
    dispatcher = Dispatcher(
        path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions={"owner/repo": issue, AUT0: FakeReviewActions()},
        grok_delivery=grok,
        projects=projects,
        hooks=(T7Hook(), T8Hook(None)),
    )
    return dispatcher, issue, grok


def wake(dispatcher: Dispatcher, *, detail: str = "go came from tracy", **changes) -> None:
    dispatcher.wake_grok_once(
        replace(ticket(), **changes),
        reason="go-not-from-approver",
        source_comment_id="901",
        detail=detail,
    )


def inbox_rows(tmp_path: Path) -> list[tuple[str, int]]:
    with sqlite3.connect(tmp_path / "d.db") as connection:
        return connection.execute("SELECT status, attempts FROM driver_inbox").fetchall()


def poll_errors(tmp_path: Path) -> list[str]:
    with sqlite3.connect(tmp_path / "d.db") as connection:
        return [row[0] for row in connection.execute("SELECT error FROM poll_errors")]


def test_a_wake_is_posted_on_the_inbox_with_kind_repo_ticket_pr_cause_and_detail(
    tmp_path: Path,
) -> None:
    dispatcher, issue, grok = make(tmp_path)
    with sqlite3.connect(tmp_path / "d.db") as connection:
        connection.execute(
            "INSERT INTO comment_links (repo, ticket_id, comment_id, link) VALUES (?, ?, ?, ?)",
            ("owner/repo", "42", "901", CAUSE),
        )

    wake(dispatcher)
    dispatcher.reconcile_jobs()

    [job] = grok.jobs
    [(repo, number, body)] = issue.posts
    assert (repo, number) == ("owner/repo", 7)
    assert body.splitlines() == [
        "**Wake: go-not-from-approver** on owner/repo 42",
        "PR: https://github.com/owner/repo/pull/42",
        f"Cause: {CAUSE}",
        "Detail: ",
        "```",
        "go came from tracy",
        "```",
        f"<!-- orch:inbox job={job.id} -->",
    ]


def test_a_wake_from_another_repo_carries_ids_and_links_only(tmp_path: Path) -> None:
    dispatcher, issue, _ = make(tmp_path)
    aut0 = replace(ticket(), repo=AUT0, ticket_id="AUT-2233", pr_number="123")
    dispatcher.process(aut0)
    with sqlite3.connect(tmp_path / "d.db") as connection:
        connection.execute(
            "INSERT INTO comment_links (repo, ticket_id, comment_id, link) VALUES (?, ?, ?, ?)",
            (AUT0, "AUT-2233", "901", f"{LINEAR}/jane-roe-intake#comment-ab12"),
        )

    wake(dispatcher, detail=SECRET, repo=AUT0, ticket_id="AUT-2233", pr_number="123")
    dispatcher.reconcile_jobs()

    [(_, _, body)] = issue.posts
    assert "jane" not in body.lower() and "1970" not in body
    assert body.splitlines()[:4] == [
        f"**Wake: go-not-from-approver** on {AUT0} AUT-2233",
        f"PR: https://github.com/{AUT0}/pull/123",
        f"Cause: {LINEAR}#comment-ab12",
        "Detail: (left out: wake from another repo)",
    ]


def test_same_repo_detail_is_redacted(tmp_path: Path) -> None:
    dispatcher, issue, _ = make(tmp_path)
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

    wake(dispatcher, detail=f"auth failed with {token}")
    dispatcher.reconcile_jobs()

    [(_, _, body)] = issue.posts
    assert token not in body
    assert "auth failed with" in body


def test_a_project_without_an_inbox_posts_nothing_and_grok_gets_today_s_wake(
    tmp_path: Path,
) -> None:
    dispatcher, issue, grok = make(tmp_path, projects=())

    wake(dispatcher)
    dispatcher.reconcile_jobs()

    assert issue.posts == []
    assert len(grok.jobs) == 1
    assert inbox_rows(tmp_path) == []


def test_a_wake_claimed_three_times_is_posted_once(tmp_path: Path) -> None:
    dispatcher, issue, grok = make(tmp_path, grok=FlakyGrok(failures=2))

    wake(dispatcher)
    for _ in range(3):
        dispatcher.reconcile_jobs()

    assert len(grok.jobs) == 1
    assert len(issue.posts) == 1


def test_a_restart_posts_a_pending_wake_once_and_a_posted_one_never(tmp_path: Path) -> None:
    issue = InboxIssue()
    issue.fail = GitHubAdapterError("gh exited 1")
    dispatcher, _, _ = make(tmp_path, issue=issue)
    wake(dispatcher)
    dispatcher.reconcile_jobs()
    assert issue.posts == []

    issue.fail = None
    restarted, _, _ = make(tmp_path, issue=issue)
    restarted.reconcile_jobs()
    again, _, _ = make(tmp_path, issue=issue)
    again.reconcile_jobs()

    assert len(issue.posts) == 1
    assert inbox_rows(tmp_path) == [("posted", 2)]


def test_a_row_left_sending_checks_the_marker_before_posting(tmp_path: Path) -> None:
    dispatcher, issue, _ = make(tmp_path)
    wake(dispatcher)
    dispatcher.reconcile_jobs()
    [first] = issue.posts
    assert issue.marker_checks == 0

    with sqlite3.connect(tmp_path / "d.db") as connection:
        connection.execute("UPDATE driver_inbox SET status = 'sending'")
    dispatcher.reconcile_jobs()
    assert issue.posts == [first]
    assert issue.marker_checks == 1
    assert inbox_rows(tmp_path) == [("posted", 1)]

    issue.posts.clear()
    with sqlite3.connect(tmp_path / "d.db") as connection:
        connection.execute("UPDATE driver_inbox SET status = 'sending'")
    dispatcher.reconcile_jobs()
    assert [body for _, _, body in issue.posts] == [first[2]]


def test_webhook_off_finishes_the_wake_on_the_inbox_without_grok(tmp_path: Path) -> None:
    dispatcher, issue, grok = make(
        tmp_path,
        projects=(Project("owner/repo", driver_inbox=INBOX, grok_webhook=False),),
    )

    wake(dispatcher)
    dispatcher.reconcile_jobs()

    assert grok.jobs == []
    assert len(issue.posts) == 1
    with sqlite3.connect(tmp_path / "d.db") as connection:
        [(state, receipt)] = connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE recipient = 'grok'"
        ).fetchall()
    assert state == "done"
    assert receipt.startswith("inbox:")


def test_a_failing_inbox_never_blocks_the_grok_wake_and_retries_next_poll(
    tmp_path: Path,
) -> None:
    issue = InboxIssue()
    issue.fail = GitHubAdapterError("gh api timed out after 60s")
    dispatcher, _, grok = make(tmp_path, issue=issue)

    wake(dispatcher)
    dispatcher.reconcile_jobs()

    assert len(grok.jobs) == 1
    assert issue.posts == []
    assert "driver-inbox:GitHubAdapterError" in poll_errors(tmp_path)

    issue.fail = None
    dispatcher.reconcile_jobs()
    assert len(issue.posts) == 1
    assert len(grok.jobs) == 1


def test_one_failure_stops_the_drain_for_the_cycle(tmp_path: Path) -> None:
    issue = InboxIssue()
    dispatcher, _, _ = make(tmp_path, issue=issue)
    for comment_id in ("901", "902", "903"):
        dispatcher.wake_grok_once(
            ticket(), reason="go-not-from-approver", source_comment_id=comment_id, detail="x"
        )
    issue.fail = GitHubAdapterError("gh exited 1")
    attempts = []
    original = issue.post_issue_comment

    def counting(repo: str, number: int, body: str) -> None:
        attempts.append(body)
        original(repo, number, body)

    issue.post_issue_comment = counting
    dispatcher.reconcile_jobs()

    assert len(attempts) == 1


def test_the_poller_needs_a_grok_block_only_while_a_project_keeps_the_webhook(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    SQLiteJobQueue(database, clock=lambda: 100.0)
    base = (
        "poll_seconds: 60\n"
        "trusted_logins:\n  grok-login: [grok-bot]\n"
        "projects:\n  - repo: acme/widgets\n    driver_inbox: acme/widgets#5\n"
    )
    webhook_on = tmp_path / "on.yml"
    webhook_on.write_text(base, encoding="utf-8")
    webhook_off = tmp_path / "off.yml"
    webhook_off.write_text(base + "    grok_webhook: false\n", encoding="utf-8")

    with pytest.raises(ValueError, match="grok webhook env names are required"):
        cli._runtime(database, webhook_on, create=False)
    dispatcher, _ = cli._runtime(database, webhook_off, create=False)

    route = dispatcher.wake_routes["acme/widgets"]
    assert route.driver_inbox == DriverInbox("acme/widgets", 5)
    assert route.grok_webhook is False
