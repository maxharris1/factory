import base64
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib import cli
from lib.dispatcher import (
    LINEAR_HANDOFF_QUERY,
    Comment,
    Dispatcher,
    TicketRef,
    TrackedTicket,
)
from lib.factory_md import FactoryConfig
from lib.github_actions import GhResult
from lib.grok_delivery import (
    GrokDeliveryError,
    WebhookGrokDelivery,
    WebhookResult,
    send_webhook,
)
from lib.orch_yml import (
    GrokConfig,
    LinearConfig,
    OrchConfig,
    Project,
)
from lib.poll_source import (
    HTTPResult,
    LivePollSource,
    PollCycleError,
    PollSourceError,
)
from lib.queue import Job, JobRequest, SQLiteJobQueue

FACTORY = """---
base_branch: main
tickets: {tickets}
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: none
ship: {ship}
skills: []
---
"""


class FakeGh:
    def __init__(self, factory: str):
        encoded = base64.b64encode(factory.encode()).decode()
        self.factory = {
            "encoding": "base64",
            "content": "\n".join(
                encoded[index : index + 60]
                for index in range(0, len(encoded), 60)
            ),
        }
        self.head = "a" * 40
        self.comments: list[dict] = []
        self.issues: list[dict] = []
        self.search_items: list[dict] = []
        self.calls: list[list[str]] = []
        self.fail_factory = False

    def __call__(self, argv, timeout):
        del timeout
        call = list(argv)
        self.calls.append(call)
        endpoint = next(
            part
            for part in call
            if part.startswith(("repos/", "search/"))
        )
        if "/contents/factory.md" in endpoint:
            if self.fail_factory:
                return GhResult(1, "", "failed (HTTP 404)")
            value = self.factory
        elif endpoint.endswith("/issues?state=open&per_page=100"):
            value = self.issues
        elif "/pulls/" in endpoint:
            value = {"head": {"sha": self.head}}
        elif endpoint.endswith("/comments?per_page=100"):
            value = self.comments
        elif endpoint.endswith("/check-runs?per_page=100"):
            value = {
                "total_count": 1,
                "check_runs": [
                    {
                        "name": "test",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            }
        elif endpoint.endswith("/status"):
            value = {"total_count": 0, "state": "pending"}
        elif endpoint == "search/issues":
            value = {"items": self.search_items}
        else:
            raise AssertionError(call)
        return GhResult(0, json.dumps(value), "")


def config(*, linear: bool = False) -> OrchConfig:
    return OrchConfig(
        poll_seconds=60,
        projects=(
            Project(
                "acme/widgets",
                factory_ref="main",
                linear_project_id="project-1" if linear else None,
            ),
        ),
        linear=LinearConfig("LINEAR_KEY"),
        grok=GrokConfig("FACTORY_WEBHOOK_URL", "FACTORY_WEBHOOK_KEY"),
        trusted_logins={"grok-login": "grok-bot"},
        trusted_linear_users={"linear:user-1": "grok-bot"},
    )


def linear_issue(
    *,
    project: object = "project-1",
    user: object = "user-1",
    identifier: str = "AUT-42",
) -> dict:
    return {
        "id": "issue-id",
        "identifier": identifier,
        "url": f"https://linear.test/{identifier}",
        "project": {"id": project} if isinstance(project, str) else project,
        "comments": {
            "nodes": [
                {
                    "id": "comment-id",
                    "body": "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=high -->\n",
                    "createdAt": "2026-11-11T12:00:00Z",
                    "user": {"id": user} if isinstance(user, str) else user,
                }
            ],
            "pageInfo": {"hasNextPage": False},
        },
        "attachments": {
            "nodes": [
                {"url": "https://github.com/acme/widgets/pull/12"}
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }


def linear_search_page(
    nodes: list[object],
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> HTTPResult:
    return HTTPResult(
        200,
        json.dumps(
            {
                "data": {
                    "searchIssues": {
                        "nodes": nodes,
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": cursor,
                        },
                    }
                }
            }
        ),
    )


def test_github_candidate_parsing_uses_gh_without_network() -> None:
    comment = {
        "id": 7,
        "user": {"login": "grok-login"},
        "body": "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=low -->\n",
        "created_at": "2026-11-11T12:00:00Z",
        "html_url": "https://github.test/comment/7",
    }
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    gh.comments = [comment]
    gh.issues = [
        {
            "number": 12,
            "pull_request": {},
            "labels": [{"name": "orch:state/intake"}],
        }
    ]
    source = LivePollSource(config(), gh_runner=gh)

    tickets = source.github_candidates("orch:state/")

    assert len(tickets) == 1
    assert tickets[0].ticket_id == "12"
    assert tickets[0].current_head == gh.head
    assert tickets[0].risk == "low"
    assert tickets[0].ci[gh.head].status == "success"
    assert all(
        call[: len(source.gh) + 1] == [*source.gh, "api"]
        for call in gh.calls
    )
    for name in (
        "LINEAR_KEY",
        "FACTORY_WEBHOOK_URL",
        "FACTORY_WEBHOOK_KEY",
    ):
        assert ("-u", name) in tuple(
            zip(source.gh, source.gh[1:])
        )
    factory_calls = [
        call for call in gh.calls if "repos/acme/widgets/contents/factory.md" in call
    ]
    assert len(factory_calls) == 1
    assert "ref=main" in factory_calls[0]
    assert not any(
        "repos/acme/widgets" in call
        and not any(
            part.startswith("repos/acme/widgets/")
            for part in call
        )
        for call in gh.calls
    )


def test_failing_project_does_not_stop_healthy_project() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    errors: list[tuple[str, str, str]] = []

    def runner(argv, timeout):
        endpoint = next(
            part
            for part in argv
            if part.startswith(("repos/", "search/"))
        )
        if endpoint == "repos/bad/repo/contents/factory.md":
            return GhResult(1, "", "failed (HTTP 404)")
        return gh(argv, timeout)

    source = LivePollSource(
        OrchConfig(
            poll_seconds=60,
            projects=(
                Project("bad/repo", factory_ref="main"),
                Project("acme/widgets", factory_ref="main"),
            ),
        ),
        gh_runner=runner,
        error_sink=lambda repo, ticket, error: errors.append(
            (repo, ticket, error)
        ),
    )

    assert source.github_candidates("orch:state/") == ()
    assert errors == [("bad/repo", "-", "PollSourceError:gh returned HTTP 404")]
    assert any(
        "repos/acme/widgets/issues?state=open&per_page=100" in call
        for call in gh.calls
    )


def test_project_error_is_recorded_in_dispatcher_database(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"grok-login": "grok-bot"},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    source = LivePollSource(
        OrchConfig(
            poll_seconds=60,
            projects=(Project("bad/repo", factory_ref="main"),),
        ),
        gh_runner=lambda argv, timeout: GhResult(
            1,
            "",
            "failed (HTTP 404)",
        ),
        error_sink=dispatcher.record_poll_error,
    )

    with pytest.raises(PollCycleError):
        dispatcher.poll_once(source)

    with queue.connect() as connection:
        row = connection.execute(
            "SELECT repo, ticket_id, error FROM poll_errors"
        ).fetchone()
    assert tuple(row) == (
        "bad/repo",
        "-",
        "PollSourceError:gh returned HTTP 404",
    )


def test_github_ticket_is_fetched_once_per_cycle() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    gh.issues = [
        {
            "number": number,
            "pull_request": {},
            "labels": [{"name": "orch:state/intake"}],
        }
        for number in (12, 13, 14)
    ]
    source = LivePollSource(config(), gh_runner=gh)
    source.begin_cycle()

    candidates = source.github_candidates("orch:state/")
    tracked = source.tracked_tickets(
        tuple(
            TicketRef("acme/widgets", str(number), "github")
            for number in (12, 13, 14)
        )
    )

    assert len(candidates) == len(tracked) == 3
    assert len(gh.calls) == 2 + 4 * 3
    assert all("--cache" in call and "60s" in call for call in gh.calls)


def test_already_merged_tracked_pr_is_marked_terminal_without_detail_reads() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    terminals: list[tuple[str, str, str]] = []

    def runner(argv, timeout):
        endpoint = next(
            part
            for part in argv
            if part.startswith(("repos/", "search/"))
        )
        if endpoint == "repos/acme/widgets/pulls/12":
            gh.calls.append(list(argv))
            return GhResult(
                0,
                json.dumps({"merged": True, "head": {"sha": gh.head}}),
                "",
            )
        return gh(argv, timeout)

    source = LivePollSource(
        config(),
        gh_runner=runner,
        terminal_sink=lambda repo, ticket, state: terminals.append(
            (repo, ticket, state)
        ),
    )

    found = source.tracked_tickets(
        (TicketRef("acme/widgets", "12", "github"),)
    )

    assert found == ()
    assert terminals == [("acme/widgets", "12", "merged")]
    assert not any("/comments" in part for call in gh.calls for part in call)


def test_github_rate_limit_enters_backoff() -> None:
    calls = 0

    def runner(argv, timeout):
        nonlocal calls
        del argv, timeout
        calls += 1
        return GhResult(1, "", "rate limited (HTTP 429)")

    source = LivePollSource(
        config(),
        gh_runner=runner,
        clock=lambda: 100.0,
    )

    assert source.github_candidates("orch:state/") == ()
    assert source.github_candidates("orch:state/") == ()
    assert calls == 1


def test_factory_read_uses_etag_across_cycles() -> None:
    encoded = base64.b64encode(
        FACTORY.format(tickets="github", ship="merge").encode()
    ).decode()
    payload = json.dumps({"encoding": "base64", "content": encoded})
    calls: list[list[str]] = []

    def runner(argv, timeout):
        del timeout
        call = list(argv)
        calls.append(call)
        if len(calls) == 1:
            return GhResult(
                0,
                f'HTTP/2.0 200 OK\nETag: "factory-v1"\n\n{payload}',
                "",
            )
        assert "If-None-Match: \"factory-v1\"" in call
        return GhResult(0, "HTTP/2.0 304 Not Modified\n\n", "")

    source = LivePollSource(config(), gh_runner=runner)
    source.begin_cycle()
    first = source._factory(source.config.projects[0])
    source.begin_cycle()
    second = source._factory(source.config.projects[0])

    assert first == second
    assert len(calls) == 2


def test_linear_candidate_parsing_uses_handoff_query() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, timeout
        assert headers["Authorization"] == "secret-linear-key"
        requests.append(json.loads(body))
        return HTTPResult(
            200,
            json.dumps(
                {
                    "data": {
                        "searchIssues": {
                            "nodes": [
                                {
                                    "id": "issue-id",
                                    "identifier": "AUT-42",
                                    "url": "https://linear.test/AUT-42",
                                    "project": {"id": "project-1"},
                                    "comments": {
                                        "nodes": [
                                            {
                                                "id": "comment-id",
                                                "body": "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=high -->\n",
                                                "createdAt": "2026-11-11T12:00:00Z",
                                                "user": {"id": "user-1"},
                                            }
                                        ],
                                        "pageInfo": {"hasNextPage": False},
                                    },
                                    "attachments": {
                                        "nodes": [
                                            {
                                                "url": "https://github.com/acme/widgets/pull/12"
                                            }
                                        ],
                                        "pageInfo": {"hasNextPage": False},
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            ),
        )

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "secret-linear-key"},
        gh_runner=gh,
        linear_transport=transport,
    )

    tickets = source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert tickets[0].ticket_id == "AUT-42"
    assert tickets[0].comments[0].author == "linear:user-1"
    assert tickets[0].current_head == gh.head
    assert tickets[0].ci[gh.head].status == "success"
    assert [request["variables"] for request in requests] == [
        {
            "term": term,
            "first": 50,
            "after": None,
            "includeComments": True,
        }
        for term in ("orch:v1 kind=brief", "orch:v1 kind=go")
    ]


def test_linear_search_paginates() -> None:
    pages = [
        linear_search_page([], has_next=True, cursor="next"),
        linear_search_page([]),
    ]
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        requests.append(json.loads(body))
        return pages.pop(0)

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=transport,
    )

    assert source._linear_issues("one term") == ()
    assert [item["variables"]["after"] for item in requests] == [None, "next"]


@pytest.mark.parametrize(
    "result",
    [
        HTTPResult(503, ""),
        HTTPResult(200, json.dumps({"errors": [{"message": "no"}]})),
    ],
)
def test_linear_search_reports_http_and_graphql_errors(result: HTTPResult) -> None:
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=lambda url, headers, body, timeout: result,
    )

    with pytest.raises(PollSourceError):
        source.linear_candidates(LINEAR_HANDOFF_QUERY)


def test_linear_null_project_is_skipped() -> None:
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=lambda url, headers, body, timeout: linear_search_page(
            [linear_issue(project=None)]
        ),
    )

    assert source.linear_candidates(LINEAR_HANDOFF_QUERY) == ()


def test_linear_null_user_is_untrusted_and_skipped() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=lambda url, headers, body, timeout: linear_search_page(
            [linear_issue(user=None)]
        ),
    )

    tickets = source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert len(tickets) == 1
    assert tickets[0].comments == ()


def test_linear_comment_overflow_is_recorded_and_skipped() -> None:
    issue = linear_issue()
    issue["comments"]["pageInfo"]["hasNextPage"] = True
    errors: list[tuple[str, str, str]] = []
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=lambda url, headers, body, timeout: linear_search_page(
            [issue]
        ),
        error_sink=lambda repo, ticket, error: errors.append(
            (repo, ticket, error)
        ),
    )

    assert source.linear_candidates(LINEAR_HANDOFF_QUERY) == ()
    assert errors == [
        (
            "acme/widgets",
            "AUT-42",
            "PollSourceError:Linear issue has more than 250 comments",
        )
    ]


def test_tracked_linear_ticket_uses_direct_issue_lookup() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        requests.append(request)
        return HTTPResult(
            200,
            json.dumps({"data": {"issue": linear_issue()}}),
        )

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport,
    )

    tickets = source.tracked_tickets(
        (TicketRef("acme/widgets", "AUT-42", "linear"),)
    )

    assert len(tickets) == 1
    assert requests[0]["query"].startswith("query FactoryIssue")
    assert requests[0]["variables"] == {"id": "AUT-42"}


def test_missing_linear_key_names_env_without_exposing_values(caplog) -> None:
    source = LivePollSource(
        config(linear=True),
        environ={},
        gh_runner=lambda argv, timeout: GhResult(1, "", ""),
    )

    with pytest.raises(PollSourceError, match="LINEAR_KEY") as raised:
        source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert "secret-linear-key" not in str(raised.value)
    assert "secret-linear-key" not in caplog.text


def test_linear_transport_error_does_not_expose_key() -> None:
    def transport(url, headers, body, timeout):
        del url, headers, body, timeout
        raise RuntimeError("secret-linear-key")

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "secret-linear-key"},
        gh_runner=lambda argv, timeout: GhResult(1, "", ""),
        linear_transport=transport,
    )

    with pytest.raises(PollSourceError) as raised:
        source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert "secret-linear-key" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_webhook_delivery_uses_job_contract_and_does_not_log_key(caplog) -> None:
    sent: list[dict] = []

    def transport(url, key, body, timeout):
        del timeout
        assert url == "https://webhook.test"
        assert key == "secret-webhook-key"
        sent.append(json.loads(body))
        return WebhookResult(200, {"X-Request-Id": "receipt-1"})

    delivery = WebhookGrokDelivery(
        url_env="FACTORY_WEBHOOK_URL",
        key_env="FACTORY_WEBHOOK_KEY",
        environ={
            "FACTORY_WEBHOOK_URL": "https://webhook.test",
            "FACTORY_WEBHOOK_KEY": "secret-webhook-key",
        },
        transport=transport,
    )
    request = JobRequest(
        repo="acme/widgets",
        ticket_id="12",
        action="stuck",
        recipient="grok",
        source_comment_id="7",
        timeout_seconds=60,
        detail="failed twice",
    )
    job = Job("job-1", "dedupe", request, "running", 1, 1, None, None)

    receipt = delivery.deliver(job, logical_key="logical-1")

    assert receipt == "receipt-1"
    assert sent == [
        {
            "action": "stuck",
            "attempt": 1,
            "detail": "failed twice",
            "generation": 1,
            "head": None,
            "job_id": "job-1",
            "logical_key": "logical-1",
            "repo": "acme/widgets",
            "source_comment_id": "7",
            "ticket_id": "12",
        }
    ]
    assert "secret-webhook-key" not in caplog.text


def test_webhook_transport_error_does_not_expose_key() -> None:
    def transport(url, key, body, timeout):
        del url, body, timeout
        raise RuntimeError(key)

    delivery = WebhookGrokDelivery(
        url_env="FACTORY_WEBHOOK_URL",
        key_env="FACTORY_WEBHOOK_KEY",
        environ={
            "FACTORY_WEBHOOK_URL": "https://webhook.test",
            "FACTORY_WEBHOOK_KEY": "secret-webhook-key",
        },
        transport=transport,
    )
    request = JobRequest(
        repo="acme/widgets",
        ticket_id="12",
        action="stuck",
        recipient="grok",
        source_comment_id="7",
        timeout_seconds=60,
    )
    job = Job("job-1", "dedupe", request, "running", 1, 1, None, None)

    with pytest.raises(GrokDeliveryError) as raised:
        delivery.deliver(job, logical_key="logical-1")

    assert "secret-webhook-key" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_webhook_transport_refuses_non_https_without_network() -> None:
    with pytest.raises(GrokDeliveryError, match="ValueError"):
        send_webhook("http://webhook.test", "key", b"{}", 1)


def test_poll_once_runs_one_cycle(monkeypatch, tmp_path: Path) -> None:
    class FakeDispatcher:
        def __init__(self):
            self.polls = 0

        def poll_once(self, source):
            assert source is fake_source
            self.polls += 1

    fake_source = object()
    dispatcher = FakeDispatcher()
    database = tmp_path / "orch.db"
    database.write_bytes(b"database")
    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda db, orch, create: (dispatcher, fake_source),
    )

    result = cli.main(["poll", "--db", str(database), "--once"])

    assert result == 0
    assert dispatcher.polls == 1


def test_real_dispatcher_is_idempotent_across_two_once_runs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=time.time)
    actions = Mock()
    delivery = Mock()
    delivery.deliver.return_value = "receipt"
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"grok-login": "grok-bot"},
        review_actions=actions,
        grok_delivery=delivery,
    )
    factory = FactoryConfig(
        base_branch="main",
        tickets="github",
        builder_harness="claude",
        reviewer_harness="codex",
        rounds_low=1,
        rounds_high=3,
        verify="none",
        ship="merge",
        skills=(),
    )
    ticket = TrackedTicket(
        repo="acme/widgets",
        ticket_id="12",
        factory=factory,
        comments=(
            Comment(
                id="7",
                author="grok-login",
                body="<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\n",
                created_at=1,
            ),
        ),
        current_head="a" * 40,
        ci={},
        risk="high",
    )

    class Source:
        def github_candidates(self, label_prefix):
            del label_prefix
            return (ticket,)

        def linear_candidates(self, query):
            del query
            return ()

        def tracked_tickets(self, refs):
            del refs
            return (ticket,)

    source = Source()
    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda db, orch, create: (dispatcher, source),
    )

    assert cli.main(["poll", "--db", str(database), "--once"]) == 0
    assert cli.main(["poll", "--db", str(database), "--once"]) == 0

    with queue.connect() as connection:
        jobs = connection.execute("SELECT count(*) FROM jobs").fetchone()[0]
        handled = connection.execute(
            "SELECT count(*) FROM handled_comments"
        ).fetchone()[0]
    assert jobs == 1
    assert handled == 1
