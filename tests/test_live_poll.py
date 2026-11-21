import base64
import io
import json
import math
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib import cli, github_app_token
from lib.dispatch.cycle_clock import CycleClock
from lib.dispatcher import (
    LINEAR_HANDOFF_QUERY,
    Comment,
    Dispatcher,
    STOPPED_POLL_SECONDS,
    RetryableStateError,
    TicketRef,
    TrackedTicket,
    _spread,
)
from lib.factory_md import FactoryConfig
from lib.github_actions import (
    GhResult,
    GitHubAdapterError,
    GitHubReviewActions,
    LinearReviewActions,
    _is_read,
)
from lib.intake_live import GitHubIntakeClient
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
    GH_CALL_ATTEMPTS,
    GH_CALL_TIMEOUT_SECONDS,
    GH_RETRY_BACKOFF_SECONDS,
    HTTPResult,
    LINEAR_COMPLEXITY_PER_HOUR,
    LINEAR_ISSUE_COMPLEXITY,
    LINEAR_ISSUE_QUERY,
    LINEAR_PROCESS_HEADROOM,
    LINEAR_REQUESTS_PER_HOUR,
    LINEAR_SEARCH_MAX_PAGES,
    LINEAR_SEARCH_COMPLEXITY,
    LINEAR_STATES_COMPLEXITY,
    LINEAR_SHARE,
    LINEAR_TRACKED_RESERVE,
    LivePollSource,
    PollCycleError,
    PollSourceError,
    QuotaDeferred,
    RATE_PAUSE_REMAINING,
    TIMEOUT_SECONDS,
    _linear_ready_state_id,
    _risk,
    _linear_state_phase,
)
from lib.queue import Job, JobRequest, SQLiteJobQueue
from tests.test_dispatcher import FakeReviewActions, setup
from tests.test_github_actions import pr

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
        self.branch_pulls: list[dict] = [{"number": 12, "state": "open"}]
        self.calls: list[list[str]] = []
        self.fail_factory = False
        self.pull: dict = {}
        self.files: list[dict] = []
        self.label_events: list[dict] = []

    def __call__(self, argv, timeout):
        del timeout
        call = list(argv)
        self.calls.append(call)
        query = next((part for part in call if part.startswith("query=")), "")
        if query.endswith("fragment pull on PullRequest { state }"):
            return GhResult(0, json.dumps(self.pull_states(query)), "")
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
        elif endpoint.endswith("/files?per_page=100"):
            value = self.files
        elif endpoint.endswith("/labels?per_page=100"):
            number = int(endpoint.split("/")[-2])
            value = next(
                (issue["labels"] for issue in self.issues if issue["number"] == number),
                [],
            )
        elif endpoint.endswith("/events?per_page=100"):
            value = self.label_events
        elif endpoint.endswith("/pulls"):
            value = self.branch_pulls
        elif "/pulls/" in endpoint:
            value = {**self.pull, "head": {"sha": self.head, **self.pull.get("head", {})}}
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
        else:
            raise AssertionError(call)
        return GhResult(0, json.dumps(value), "")

    def pull_states(self, query: str) -> dict:
        if self.pull.get("merged") is True:
            state = "MERGED"
        else:
            state = "CLOSED" if self.pull.get("state") == "closed" else "OPEN"
        aliases = re.findall(r"(p[0-9]+): pullRequest", query)
        return {"data": {"repository": {alias: {"state": state} for alias in aliases}}}


def config(*, linear: bool = False) -> OrchConfig:
    return OrchConfig(
        poll_seconds=60,
        projects=(
            Project(
                "acme/widgets",
                factory_ref="main",
                linear_project_ids=frozenset({"project-1"} if linear else ()),
            ),
        ),
        linear=LinearConfig("LINEAR_KEY"),
        grok=GrokConfig("FACTORY_WEBHOOK_URL", "FACTORY_WEBHOOK_KEY"),
        trusted_logins={"grok-login": frozenset({"grok-bot"})},
        trusted_linear_users={"linear:user-1": frozenset({"grok-bot"})},
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
        "gitBranchName": f"max/{identifier.lower()}-fixture",
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
    }


def linear_states_page(variables: dict, state: str = "In Progress") -> HTTPResult:
    team = variables["team"]
    nodes = [
        {
            "identifier": f"{team}-{number:04d}",
            "branchName": f"max/{team.lower()}-{number}-fixture",
            "state": {"name": state, "type": "started"},
        }
        for number in variables["numbers"]
    ]
    return HTTPResult(200, json.dumps({"data": {"issues": {"nodes": nodes}}}))


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


def test_review_actions_use_each_projects_linear_ready_flag() -> None:
    source = LivePollSource(
        OrchConfig(
            poll_seconds=60,
            projects=(
                Project(
                    "aut0-code/aut0",
                    factory_ref="dev",
                    linear_project_ids=frozenset({"aut0"}),
                    linear_ready_for_review=True,
                ),
                Project(
                    "acme/linear",
                    factory_ref="main",
                    linear_project_ids=frozenset({"linear"}),
                ),
                Project("acme/github", factory_ref="main"),
            ),
            linear=LinearConfig("LINEAR_KEY"),
        )
    )

    actions = source.review_actions_by_repo()

    assert isinstance(actions["aut0-code/aut0"], LinearReviewActions)
    assert actions["aut0-code/aut0"].move_ready_for_review is True
    assert actions["aut0-code/aut0"].cache_ttl is None
    assert actions["aut0-code/aut0"].fresh_ticket == source.fresh_ticket
    assert isinstance(actions["acme/linear"], LinearReviewActions)
    assert actions["acme/linear"].move_ready_for_review is False
    assert actions["acme/linear"].cache_ttl is None
    assert type(actions["acme/github"]) is GitHubReviewActions
    assert actions["acme/github"].cache_ttl is None


@pytest.mark.parametrize(
    ("enabled", "issue_id", "state_id"),
    [
        (False, "issue-1", "state-1"),
        (True, None, "state-1"),
        (True, "issue-1", None),
    ],
)
def test_move_linear_ready_fails_closed_without_complete_enabled_metadata(
    enabled: bool,
    issue_id: str | None,
    state_id: str | None,
) -> None:
    requests: list[dict] = []
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=lambda url, headers, body, timeout: (
            requests.append(json.loads(body))
            or HTTPResult(200, json.dumps({"data": {"issueUpdate": {"success": True}}}))
        ),
    )
    tracked = TrackedTicket(
        repo="acme/widgets",
        ticket_id="AUT-42",
        factory=FactoryConfig(
            base_branch="main",
            tickets="linear",
            builder_harness="claude",
            reviewer_harness="codex",
            rounds_low=1,
            rounds_high=3,
            verify="none",
            ship="handoff",
            skills=(),
        ),
        comments=(),
        current_head="a" * 40,
        ci={},
        risk="high",
        linear_issue_id=issue_id,
        linear_ready_state_id=state_id,
        linear_ready_for_review=enabled,
    )

    with pytest.raises(PollSourceError, match="metadata is unavailable"):
        source._move_linear_ready(tracked)

    assert requests == []


def test_move_linear_ready_writes_only_the_exact_state_update() -> None:
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        requests.append(json.loads(body))
        return HTTPResult(
            200,
            json.dumps({"data": {"issueUpdate": {"success": True}}}),
        )

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=transport,
    )
    tracked = TrackedTicket(
        repo="acme/widgets",
        ticket_id="AUT-42",
        factory=FactoryConfig(
            base_branch="main",
            tickets="linear",
            builder_harness="claude",
            reviewer_harness="codex",
            rounds_low=1,
            rounds_high=3,
            verify="none",
            ship="handoff",
            skills=(),
        ),
        comments=(),
        current_head="a" * 40,
        ci={},
        risk="high",
        linear_issue_id="issue-1",
        linear_ready_state_id="state-1",
        linear_ready_for_review=True,
    )

    source._move_linear_ready(tracked)

    assert len(requests) == 1
    assert requests[0]["variables"] == {"issue": "issue-1", "state": "state-1"}
    assert "stateId: $state" in requests[0]["query"]
    assert "issueUpdate" in requests[0]["query"]


def test_linear_ready_state_is_selected_only_by_exact_name() -> None:
    issue = {
        "team": {
            "states": {
                "nodes": [
                    {"id": "wrong", "name": "ready for review"},
                    {"id": "right", "name": "Ready for Review"},
                ]
            }
        }
    }

    assert _linear_ready_state_id(issue) == "right"
    assert _linear_ready_state_id(
        {"team": {"states": {"nodes": [{"id": "wrong", "name": "Review"}]}}}
    ) is None


def linear_transport_for(issue: dict):
    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        if request["query"].startswith("query FactoryPoll"):
            summary = {
                key: issue.get(key)
                for key in ("id", "identifier", "project")
            }
            return linear_search_page([summary])
        if request["query"].startswith("query FactoryIssue"):
            return HTTPResult(200, json.dumps({"data": {"issue": issue}}))
        raise AssertionError(request)

    return transport


def _risk_after(*comments: tuple[str, str]) -> str:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    gh.comments = [
        {
            "id": number,
            "user": {"login": login},
            "body": body,
            "created_at": f"2026-11-11T12:00:{number:02d}Z",
            "html_url": f"https://github.test/comment/{number}",
        }
        for number, (login, body) in enumerate(comments, start=1)
    ]
    gh.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]
    (ticket,) = LivePollSource(config(), gh_runner=gh).github_candidates("orch:state/")
    return ticket.risk


GROK_LOW = "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=low -->\n"
GROK_HIGH = "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=high -->\n"


@pytest.mark.parametrize(
    ("login", "body"),
    [
        ("stranger", GROK_HIGH),
        (
            "grok-login",
            "<!-- orch:v1 kind=verdict from=codex-bot role=reviewer verdict=REVISE risk=high -->\n",
        ),
    ],
)
def test_untrusted_risk_comment_leaves_the_ticket_low(login: str, body: str) -> None:
    # #68: risk= counts only from a login trusted for the comment's `from`.
    assert _risk_after(("grok-login", GROK_LOW), (login, body)) == "low"


def test_a_later_trusted_risk_does_not_move_the_intake_risk() -> None:
    # #195: the first trusted orchestrator brief or go sets the risk.
    assert _risk_after(("grok-login", GROK_LOW), ("grok-login", GROK_HIGH)) == "low"


def test_linear_risk_counts_only_from_trusted_linear_users() -> None:
    trusted = {"github": {}, "linear": {"linear:user-1": frozenset({"grok-bot"})}}

    def said(author: str) -> Comment:
        return Comment(id=author, author=author, body=GROK_LOW, created_at=1)

    assert _risk((said("linear:user-2"),), trusted) == "high"
    assert _risk((said("linear:user-1"),), trusted) == "low"


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
    expected_prefix = [
        "env",
        "-u", "GH_TOKEN",
        "-u", "GITHUB_TOKEN",
        "-u", "GH_ENTERPRISE_TOKEN",
        "-u", "GITHUB_ENTERPRISE_TOKEN",
        "-u", "LINEAR_KEY",
        "-u", "FACTORY_WEBHOOK_URL",
        "-u", "FACTORY_WEBHOOK_KEY",
        "-u", "FACTORY_WEBHOOK_AUTH",
        "gh", "api",
    ]
    assert all(call[: len(expected_prefix)] == expected_prefix for call in gh.calls)
    for name in (
        "LINEAR_KEY",
        "FACTORY_WEBHOOK_URL",
        "FACTORY_WEBHOOK_KEY",
        "FACTORY_WEBHOOK_AUTH",
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


@pytest.mark.parametrize(
    ("updated_at", "edited_at"),
    [
        (None, None),
        ("2026-11-11T12:00:00Z", None),
        ("2026-11-11T12:05:00Z", 1_794_398_700.0),
    ],
    ids=["no-updated-at", "never-edited", "edited"],
)
def test_github_comment_carries_when_it_was_edited(
    updated_at: str | None,
    edited_at: float | None,
) -> None:
    raw = {
        "id": 7,
        "user": {"login": "grok-login"},
        "body": "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\n",
        "created_at": "2026-11-11T12:00:00Z",
    }
    if updated_at is not None:
        raw["updated_at"] = updated_at
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    gh.comments = [raw]
    gh.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]

    (candidate,) = LivePollSource(config(), gh_runner=gh).github_candidates("orch:state/")

    assert candidate.comments[0].edited_at == edited_at


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
    assert errors == [
        ("bad/repo", "-", "PollSourceError:gh api GET contents/factory.md returned HTTP 404")
    ]
    assert any(
        "repos/acme/widgets/issues?state=open&per_page=100" in call
        for call in gh.calls
    )


def _factory_payload(text: str) -> dict:
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def _with_unknown_field(text: str) -> str:
    return text.replace("skills: []\n", "skills: []\nnew_field: [docs/]\n")


def test_unknown_factory_field_still_polls_and_feeds() -> None:
    text = _with_unknown_field(FACTORY.format(tickets="github", ship="merge"))
    assert "new_field" in text
    gh = FakeGh(text)
    gh.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]
    errors: list[tuple[str, str, str]] = []
    source = LivePollSource(
        config(),
        gh_runner=gh,
        error_sink=lambda repo, ticket, error: errors.append((repo, ticket, error)),
    )
    source.begin_cycle()

    (ticket,) = source.github_candidates("orch:state/")
    context = source.factory_context(source.config.projects[0])

    assert ticket.repo == "acme/widgets"
    assert context.config.ship == "merge"
    assert errors == []


def test_bad_factory_md_keeps_the_last_good_config_and_warns_once() -> None:
    good = FACTORY.format(tickets="github", ship="merge")
    gh = FakeGh(good)
    gh.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]
    errors: list[tuple[str, str, str]] = []
    warnings: list[str] = []
    source = LivePollSource(
        config(),
        gh_runner=gh,
        error_sink=lambda repo, ticket, error: errors.append((repo, ticket, error)),
    )
    source.warning_sink = warnings.append
    project = source.config.projects[0]
    source.begin_cycle()
    assert len(source.github_candidates("orch:state/")) == 1
    source.factory_context(project)

    gh.factory = _factory_payload(good.replace("ship: merge", "ship: yolo"))
    for _cycle in range(3):
        source.begin_cycle()
        (ticket,) = source.github_candidates("orch:state/")
        assert ticket.repo == "acme/widgets"
        assert source.factory_context(project).config.ship == "merge"

    assert errors == []
    assert warnings == [
        "acme/widgets factory.md: ship must be merge, handoff, or after:<approver>; "
        "keeping the last good factory.md"
    ]

    gh.factory = _factory_payload(good)
    source.begin_cycle()
    source.github_candidates("orch:state/")
    gh.factory = _factory_payload(good.replace("ship: merge", "ship: yolo"))
    source.begin_cycle()
    source.github_candidates("orch:state/")
    assert len(warnings) == 2


def test_a_rejected_base_branch_never_replaces_the_last_good_factory_md() -> None:
    good = FACTORY.format(tickets="github", ship="merge")
    gh = FakeGh(good)
    gh.issues = [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}]
    errors: list[tuple[str, str, str]] = []
    source = LivePollSource(
        config(),
        gh_runner=gh,
        error_sink=lambda repo, ticket, error: errors.append((repo, ticket, error)),
    )
    project = source.config.projects[0]
    counts = []
    contexts = []
    for text in (
        good,
        good.replace("base_branch: main", "base_branch: dev"),
        good.replace("ship: merge", "ship: yolo"),
    ):
        gh.factory = _factory_payload(text)
        source.begin_cycle()
        counts.append(len(source.github_candidates("orch:state/")))
        try:
            contexts.append(source.factory_context(project).config.base_branch)
        except PollSourceError:
            contexts.append(None)

    assert counts == [1, 0, 1]
    assert contexts == ["main", None, "main"]


def test_bad_factory_md_with_no_last_good_config_is_still_an_error() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="yolo"))
    errors: list[tuple[str, str, str]] = []
    source = LivePollSource(
        config(),
        gh_runner=gh,
        error_sink=lambda repo, ticket, error: errors.append((repo, ticket, error)),
    )
    source.begin_cycle()

    assert source.github_candidates("orch:state/") == ()
    assert [repo for repo, _ticket, _error in errors] == ["acme/widgets"]
    with pytest.raises(PollSourceError):
        source.factory_context(source.config.projects[0])


def test_gh_timeout_is_retried_with_backoff_and_a_short_timeout() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    timeouts: list[float] = []
    sleeps: list[float] = []
    remaining = [2]

    def runner(argv, timeout):
        timeouts.append(timeout)
        if remaining[0]:
            remaining[0] -= 1
            raise subprocess.TimeoutExpired(argv, timeout)
        return gh(argv, timeout)

    source = LivePollSource(config(), gh_runner=runner, sleep=sleeps.append)

    assert source._gh_pages("repos/acme/widgets/issues?state=open&per_page=100")
    assert sleeps == [GH_RETRY_BACKOFF_SECONDS, GH_RETRY_BACKOFF_SECONDS * 2]
    assert set(timeouts) == {GH_CALL_TIMEOUT_SECONDS}
    assert GH_CALL_TIMEOUT_SECONDS < TIMEOUT_SECONDS
    assert len(timeouts) == 3


def test_gh_timeout_gives_up_with_a_retryable_error() -> None:
    sleeps: list[float] = []
    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(list(argv))
        raise subprocess.TimeoutExpired(argv, timeout)

    source = LivePollSource(config(), gh_runner=runner, sleep=sleeps.append)

    with pytest.raises(RetryableStateError, match="gh api GET pulls/12 timed out"):
        source._gh_call("repos/acme/widgets/pulls/12")
    assert len(calls) == GH_CALL_ATTEMPTS
    assert len(sleeps) == GH_CALL_ATTEMPTS - 1


def test_gh_timeout_on_one_ref_records_it_and_the_cycle_continues(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {"grok-login": {"grok-bot"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    with queue.connect() as connection:
        for number in ("12", "13"):
            connection.execute(
                """
                INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)
                VALUES ('acme/widgets', ?, 'github', 0)
                """,
                (number,),
            )
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))

    def runner(argv, timeout):
        if any(part.endswith("/pulls/12") for part in argv):
            raise subprocess.TimeoutExpired(argv, timeout)
        return gh(argv, timeout)

    source = LivePollSource(
        OrchConfig(
            poll_seconds=60,
            projects=(Project("acme/widgets", factory_ref="main"),),
        ),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        sleep=lambda seconds: None,
    )
    dispatcher.process = Mock()
    dispatcher.reconcile_jobs = Mock()

    with pytest.raises(PollCycleError):
        dispatcher.poll_once(source)

    assert [call.args[0].ticket_id for call in dispatcher.process.call_args_list] == [
        "13"
    ]
    dispatcher.reconcile_jobs.assert_called_once_with()
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT repo, ticket_id, error FROM poll_errors"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "acme/widgets",
            "12",
            f"PollSourceError:gh api GET pulls/12 timed out after {GH_CALL_ATTEMPTS} attempts",
        )
    ]


def test_project_error_is_recorded_in_dispatcher_database(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={
            "github": {"grok-login": {"grok-bot"}},
            "linear": {},
        },
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
        "PollSourceError:gh api GET contents/factory.md returned HTTP 404",
    )


def test_linear_failure_does_not_stop_tracked_github_or_reconcile(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={
            "github": {"grok-login": {"grok-bot"}},
            "linear": {},
        },
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    with queue.connect() as connection:
        connection.execute(
            """
            INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)
            VALUES ('acme/widgets', '12', 'github', 0)
            """
        )
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    source = LivePollSource(
        OrchConfig(
            poll_seconds=60,
            projects=(
                Project(
                    "aut0/repo",
                    factory_ref="main",
                    linear_project_ids=frozenset({"linear-project"}),
                ),
                Project("acme/widgets", factory_ref="main"),
            ),
            linear=LinearConfig("LINEAR_KEY"),
        ),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=lambda url, headers, body, timeout: HTTPResult(
            200,
            json.dumps({"errors": [{"message": "unavailable"}]}),
        ),
        error_sink=dispatcher.record_poll_error,
    )
    dispatcher.process = Mock()
    dispatcher.reconcile_jobs = Mock()

    with pytest.raises(PollCycleError):
        dispatcher.poll_once(source)

    assert dispatcher.process.call_count == 1
    assert dispatcher.process.call_args.args[0].ticket_id == "12"
    dispatcher.reconcile_jobs.assert_called_once_with()
    with queue.connect() as connection:
        errors = connection.execute(
            "SELECT repo, ticket_id FROM poll_errors ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in errors] == [
        ("linear", "-"),
        ("linear", "-"),
    ]


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
    assert len(gh.calls) == 2 + 5 * 3
    cached = [
        call
        for call in gh.calls
        if not any(
            marker in part
            for call_part in call
            for marker in ("/comments?", "/check-runs?", "/status", "/files?")
            for part in (call_part,)
        )
    ]
    assert all("--cache" in call and "60s" in call for call in cached)
    assert all(
        "--cache" not in call
        for call in gh.calls
        if any(
            marker in part
            for part in call
            for marker in ("/comments?", "/check-runs?", "/status", "/files?")
        )
    )


@pytest.mark.parametrize("tracker", ["github", "linear"])
def test_live_source_refuses_a_short_head(tracker: str) -> None:
    gh = FakeGh(FACTORY.format(tickets=tracker, ship="handoff"))
    gh.head = "abcdef1"
    errors: list[tuple[str, str, str]] = []

    def transport(url, headers, body, timeout):
        del url, headers, body, timeout
        return HTTPResult(200, json.dumps({"data": {"issue": linear_issue()}}))

    source = LivePollSource(
        config(linear=tracker == "linear"),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport,
        error_sink=lambda repo, ticket_id, error: errors.append(
            (repo, ticket_id, error)
        ),
    )
    ticket_id = "12" if tracker == "github" else "AUT-42"

    tickets = source.tracked_tickets((TicketRef("acme/widgets", ticket_id, tracker),))

    assert tickets == ()
    assert errors == [
        (
            "acme/widgets",
            ticket_id,
            "PollSourceError:acme/widgets#12 head 'abcdef1' is not a full SHA",
        )
    ]


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
        terminal_sink=lambda repo, ticket, state, _merged: terminals.append(
            (repo, ticket, state)
        ),
    )

    found = source.tracked_tickets(
        (TicketRef("acme/widgets", "12", "github"),)
    )

    assert found == ()
    assert terminals == [("acme/widgets", "12", "merged")]
    assert not any("/comments" in part for call in gh.calls for part in call)


def test_merged_pr_with_pending_ship_work_is_returned() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))

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

    source = LivePollSource(config(), gh_runner=runner)

    found = source.tracked_tickets(
        (TicketRef("acme/widgets", "12", "github", allow_merged=True),)
    )

    assert len(found) == 1
    assert found[0].ticket_id == "12"
    assert any("/comments" in part for call in gh.calls for part in call)


def test_pr_merged_between_discovery_and_fetch_is_not_an_error() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    gh.issues = [
        {
            "number": 12,
            "pull_request": {},
            "labels": [{"name": "orch:state/intake"}],
        }
    ]

    def runner(argv, timeout):
        endpoint = next(
            part
            for part in argv
            if part.startswith(("repos/", "search/"))
        )
        if endpoint == "repos/acme/widgets/pulls/12":
            return GhResult(
                0,
                json.dumps({"merged": True, "head": {"sha": gh.head}}),
                "",
            )
        return gh(argv, timeout)

    source = LivePollSource(config(), gh_runner=runner)
    source.begin_cycle()

    assert source.github_candidates("orch:state/") == ()
    source.end_cycle()


def test_github_rate_limit_enters_backoff() -> None:
    calls = 0

    def runner(argv, timeout):
        nonlocal calls
        del timeout
        calls += "rate_limit" not in argv
        return GhResult(1, "", "rate limited (HTTP 429)")

    source = LivePollSource(
        config(),
        gh_runner=runner,
        clock=lambda: 100.0,
    )

    assert source.github_candidates("orch:state/") == ()
    assert source.github_candidates("orch:state/") == ()
    assert calls == 1


def test_permission_403_does_not_back_off_unrelated_github_reads() -> None:
    calls = 0

    def runner(argv, timeout):
        nonlocal calls
        del argv, timeout
        calls += 1
        return GhResult(1, "", "gh: HTTP 403")

    source = LivePollSource(
        config(),
        gh_runner=runner,
        clock=lambda: 100.0,
    )

    assert source.github_candidates("orch:state/") == ()
    assert source.github_candidates("orch:state/") == ()
    assert calls == 2


def test_rate_limit_403_backs_off_github_reads() -> None:
    calls = 0

    def runner(argv, timeout):
        nonlocal calls
        del timeout
        calls += "rate_limit" not in argv
        return GhResult(
            1,
            "HTTP/2.0 403 Forbidden\r\nX-RateLimit-Remaining: 0\r\n\r\n",
            "gh: HTTP 403",
        )

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
        return GhResult(
            1,
            "HTTP/2.0 304 Not Modified\r\nDate: now\r\n\r\n",
            "gh: HTTP 304\n",
        )

    source = LivePollSource(config(), gh_runner=runner)
    source.begin_cycle()
    first = source._factory(source.config.projects[0])
    source.begin_cycle()
    second = source._factory(source.config.projects[0])
    source.begin_cycle()
    third = source._factory(source.config.projects[0])

    assert first == second == third
    assert len(calls) == 3


@pytest.mark.parametrize("exit_code", [0, 1])
def test_304_with_empty_etag_cache_refetches_without_conditional_or_gh_cache(
    exit_code: int,
) -> None:
    # After a restart the in-memory ETag cache is empty, but gh's own
    # --cache can replay a 304 it stored for a conditional request made by
    # the previous process (If-None-Match is not part of gh's cache key).
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
                exit_code,
                "HTTP/2.0 304 Not Modified\r\nDate: now\r\n\r\n",
                "gh: HTTP 304\n" if exit_code else "",
            )
        if len(calls) == 2:
            return GhResult(
                0,
                f'HTTP/2.0 200 OK\nETag: "factory-v2"\n\n{payload}',
                "",
            )
        assert 'If-None-Match: "factory-v2"' in call
        return GhResult(1, "HTTP/2.0 304 Not Modified\r\n\r\n", "gh: HTTP 304\n")

    source = LivePollSource(config(), gh_runner=runner)
    source.begin_cycle()
    first = source._factory(source.config.projects[0])
    source.begin_cycle()
    second = source._factory(source.config.projects[0])

    assert first == second
    assert len(calls) == 3
    assert "--cache" in calls[0]
    refetch = calls[1]
    assert "--cache" not in refetch
    assert "--include" in refetch
    assert not any(part.startswith("If-None-Match") for part in refetch)
    assert refetch[refetch.index("api") + 1 :] == [
        part
        for part in calls[0][calls[0].index("api") + 1 :]
        if part not in ("--cache", "60s")
    ]


def test_304_on_the_unconditional_refetch_is_still_an_error() -> None:
    calls: list[list[str]] = []

    def runner(argv, timeout):
        del timeout
        calls.append(list(argv))
        return GhResult(1, "HTTP/2.0 304 Not Modified\r\n\r\n", "gh: HTTP 304\n")

    source = LivePollSource(config(), gh_runner=runner)
    with pytest.raises(PollSourceError, match="304 without a cached response"):
        source._gh_call("repos/acme/widgets/contents/factory.md")
    assert len(calls) == 2


OPEN_LIST = "repos/acme/widgets/issues?state=open&per_page=100"
NOT_MODIFIED = GhResult(1, "HTTP/2.0 304 Not Modified\r\n\r\n", "gh: HTTP 304\n")


def included(etag: str, value: object, *, link: str | None = None) -> GhResult:
    headers = f'HTTP/2.0 200 OK\nETag: "{etag}"\n' + (f"Link: {link}\n" if link else "")
    return GhResult(0, f"{headers}\n{json.dumps(value)}", "")


class ListGh(FakeGh):
    def __init__(self) -> None:
        super().__init__(FACTORY.format(tickets="github", ship="merge"))
        self.list_etags: list[str | None] = []
        self.link: str | None = None
        self.later_pages: list[dict] = []

    def __call__(self, argv, timeout):
        call = list(argv)
        if call[-1] == OPEN_LIST and "--paginate" not in call:
            self.calls.append(call)
            etag = self.list_etags.pop(0)
            return NOT_MODIFIED if etag is None else included(etag, self.issues, link=self.link)
        if call[-1] == f"{OPEN_LIST}&page=2":
            self.calls.append(call)
            return GhResult(0, json.dumps(self.later_pages), "")
        return super().__call__(argv, timeout)


def ready_starts_review_config() -> OrchConfig:
    base = config()
    return replace(base, projects=(replace(base.projects[0], ready_starts_review=True),))


def open_pull(number: int, *, labels: tuple[str, ...] = ()) -> dict:
    return {
        "number": number,
        "pull_request": {},
        "labels": [{"name": name} for name in labels],
        "comments": 1,
        "updated_at": "2026-11-11T12:00:00Z",
    }


def ready_comment(comment_id: int, head: str) -> dict:
    return {
        "id": comment_id,
        "user": {"login": "maxharris1"},
        "body": f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={head} -->\n",
        "created_at": "2026-11-11T12:00:00Z",
        "html_url": f"https://github.test/pr/12#issuecomment-{comment_id}",
    }


def candidate_cycle(source: LivePollSource, gh: FakeGh) -> tuple[tuple[TrackedTicket, ...], list[list[str]]]:
    start = len(gh.calls)
    source.begin_cycle()
    tickets = source.github_candidates("orch:state/")
    return tickets, gh.calls[start:]


def test_unchanged_candidate_list_is_a_304_with_no_comments_read() -> None:
    gh = ListGh()
    gh.comments = [ready_comment(9, gh.head)]
    gh.issues = [open_pull(12)]
    gh.list_etags = ["list-v1", None]
    source = LivePollSource(ready_starts_review_config(), gh_runner=gh)

    first, cycle_one = candidate_cycle(source, gh)
    second, cycle_two = candidate_cycle(source, gh)

    assert [ticket.ticket_id for ticket in first] == ["12"]
    assert second == first
    assert [call[-1] for call in cycle_one].count(
        "repos/acme/widgets/issues/12/comments?per_page=100"
    ) == 1
    assert 'If-None-Match: "list-v1"' in next(call for call in cycle_two if call[-1] == OPEN_LIST)
    assert not any("/comments" in call[-1] for call in cycle_two)
    assert len(cycle_two) - 1 <= len(cycle_one)


@pytest.mark.parametrize(
    "change",
    [{"comments": 2}, {"updated_at": "2026-11-11T12:30:00Z"}],
    ids=["comments-count", "updated-at"],
)
def test_changed_comment_count_refetches_comments(change: dict) -> None:
    gh = ListGh()
    gh.comments = [ready_comment(9, gh.head)]
    gh.issues = [open_pull(12)]
    gh.list_etags = ["list-v1", "list-v2"]
    source = LivePollSource(ready_starts_review_config(), gh_runner=gh)
    candidate_cycle(source, gh)

    gh.comments = [*gh.comments, ready_comment(10, gh.head)]
    gh.issues = [{**open_pull(12), **change}]
    tickets, cycle_two = candidate_cycle(source, gh)

    assert 'If-None-Match: "list-v1"' in next(call for call in cycle_two if call[-1] == OPEN_LIST)
    assert [call[-1] for call in cycle_two].count(
        "repos/acme/widgets/issues/12/comments?per_page=100"
    ) == 1
    assert [len(ticket.comments) for ticket in tickets] == [2]


def test_rel_next_falls_back_to_paginate() -> None:
    gh = ListGh()
    gh.issues = [open_pull(12, labels=("orch:state/intake",))]
    gh.later_pages = [open_pull(13, labels=("orch:state/intake",))]
    gh.link = (
        "<https://api.github.com/repositories/1/issues?state=open&per_page=100&page=2>; "
        'rel="next"'
    )
    gh.list_etags = ["list-v1", None]
    source = LivePollSource(config(), gh_runner=gh)

    for _ in range(2):
        tickets, cycle = candidate_cycle(source, gh)
        page_one = next(index for index, call in enumerate(cycle) if call[-1] == OPEN_LIST)
        assert cycle[page_one + 1][-2:] == ["--paginate", f"{OPEN_LIST}&page=2"]
        assert sorted(ticket.ticket_id for ticket in tickets) == ["12", "13"]


def test_intake_list_304_makes_no_events_call() -> None:
    endpoint = "repos/acme/widgets/issues?state=open&labels=factory%3Aqueue&per_page=100"
    issue = {
        "number": 5,
        "title": "Queue me",
        "body": "",
        "labels": [{"name": "factory:queue"}],
        "created_at": "2026-11-11T12:00:00Z",
        "updated_at": "2026-11-11T12:00:00Z",
        "html_url": "https://github.test/acme/widgets/issues/5",
    }
    labeled = {"event": "labeled", "label": {"name": "factory:queue"}, "actor": {"login": "maxharris1"}}
    calls: list[list[str]] = []

    def runner(argv, timeout):
        del timeout
        call = list(argv)
        calls.append(call)
        if call[-1] == endpoint:
            return included("intake-v1", [issue]) if len(calls) == 1 else NOT_MODIFIED
        if call[-1] == "repos/acme/widgets/issues/5/events?per_page=100":
            return GhResult(0, json.dumps([labeled]), "")
        raise AssertionError(call)

    source = LivePollSource(config(), gh_runner=runner)
    client = GitHubIntakeClient(runner=source._adapter_runner, list_pages=source._gh_list)
    first = client.github_intake("acme/widgets", "factory:queue")
    before = len(calls)
    second = client.github_intake("acme/widgets", "factory:queue")
    cycle_two = calls[before:]

    assert [issue.applied_by for issue in first] == ["maxharris1"]
    assert second == first
    assert [call[-1] for call in cycle_two] == [endpoint]
    assert 'If-None-Match: "intake-v1"' in cycle_two[0]


class GoneGh(ListGh):
    def __init__(self) -> None:
        super().__init__()
        self.pull_gone = False

    def __call__(self, argv, timeout):
        if self.pull_gone and list(argv)[-1] == "repos/acme/widgets/pulls/12":
            self.calls.append(list(argv))
            return GhResult(1, "", "gh: Not Found (HTTP 404)\n")
        return super().__call__(argv, timeout)


@pytest.mark.parametrize("ending", ["merged", "pull-404"])
def test_labelled_candidate_ending_on_the_pull_read_reads_no_comments(ending: str) -> None:
    gh = GoneGh()
    gh.issues = [open_pull(12, labels=("orch:state/intake",))]
    gh.list_etags = ["list-v1"]
    gh.pull = {"merged": True, "state": "closed"}
    gh.pull_gone = ending == "pull-404"
    source = LivePollSource(config(), gh_runner=gh)

    tickets, cycle = candidate_cycle(source, gh)

    assert tickets == ()
    assert not any("/comments" in call[-1] for call in cycle)
    assert [next(part for part in call if part.startswith("repos/")) for call in cycle] == [
        "repos/acme/widgets/contents/factory.md",
        OPEN_LIST,
        "repos/acme/widgets/pulls/12",
    ]


class DiskCachedGh:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.now = 0.0
        self.disk: dict[str, tuple[float, int, str]] = {}
        self.billed = 0

    def page(self, url: str) -> int:
        match = re.search(r"&page=([0-9]+)$", url)
        return int(match.group(1)) if match else 1

    def respond(self, url: str, if_none_match: str | None, cache: bool) -> tuple[int, str]:
        stored = self.disk.get(url)
        if cache and stored is not None and self.now - stored[0] < 60:
            return stored[1], stored[2]
        page = self.page(url)
        etag = f'"page-{page}"'
        link = f'Link: <{self.endpoint}&page={page + 1}>; rel="next"\n' if page < 3 else ""
        if if_none_match == etag:
            status, text = 304, "HTTP/2.0 304 Not Modified\n\n"
        else:
            self.billed += 1
            body = json.dumps([open_pull(page, labels=("orch:state/intake",))])
            status, text = 200, f"HTTP/2.0 200 OK\nETag: {etag}\n{link}\n{body}"
        if cache:
            self.disk[url] = (self.now, status, text)
        return status, text

    def __call__(self, argv, timeout):
        del timeout
        call = list(argv)
        cache = "--cache" in call
        header = next(
            (part for part in call if part.startswith("If-None-Match: ")), None
        )
        if_none_match = None if header is None else header.removeprefix("If-None-Match: ")
        url = call[-1]
        if "--paginate" not in call:
            status, text = self.respond(url, if_none_match, cache)
            if "--include" not in call:
                text = text.split("\n\n", 1)[1]
            return GhResult(0 if status == 200 else 1, text, "" if status == 200 else "gh: HTTP 304\n")
        bodies = []
        while url is not None:
            status, text = self.respond(url, None, cache)
            if status != 200:
                return GhResult(1, "", f"gh: HTTP {status}\n")
            headers, body = text.split("\n\n", 1)
            bodies.append(body)
            found = re.search(r"<([^>]+)>; rel=\"next\"", headers)
            url = found.group(1) if found else None
        return GhResult(0, "".join(bodies), "")


@pytest.mark.parametrize(
    "endpoint",
    [OPEN_LIST, "repos/acme/widgets/issues?state=open&labels=factory%3Aqueue&per_page=100"],
    ids=["candidates", "intake"],
)
def test_restarts_after_a_cached_304_bill_no_more_than_paginated_reads(endpoint: str) -> None:
    def billed(read) -> tuple[int, list[list[object]]]:
        gh = DiskCachedGh(endpoint)
        source = LivePollSource(config(), gh_runner=gh)
        results = [read(source)]
        gh.now = 61
        results.append(read(source))
        for restart in (62, 63, 64):
            gh.now = restart
            results.append(read(LivePollSource(config(), gh_runner=gh)))
        return gh.billed, results

    conditional, listed = billed(lambda source: source._gh_list(endpoint))
    paginated, pages = billed(lambda source: source._gh_pages(endpoint))

    assert listed == pages
    assert [len(read) for read in listed] == [3] * 5
    assert conditional <= paginated, (conditional, paginated)


def test_fresh_ticket_bypasses_ticket_and_gh_caches() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="after:approver"))
    gh.comments = [
        {
            "id": 1,
            "user": {"login": "grok-login"},
            "body": "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building -->\n",
            "created_at": "2026-11-11T12:00:00Z",
        }
    ]
    source = LivePollSource(config(), gh_runner=gh)
    ref = TicketRef("acme/widgets", "12", "github")
    original = source.tracked_tickets((ref,))[0]
    before = len(gh.calls)
    gh.comments.append(
        {
            "id": 2,
            "user": {"login": "reviewer"},
            "body": "<!-- orch:v1 kind=verdict from=reviewer role=reviewer round=1 head="
            + gh.head
            + " verdict=REVISE risk=high -->\n",
            "created_at": "2026-11-11T12:01:00Z",
        }
    )

    fresh = source.fresh_ticket(ref)

    assert fresh is not None
    assert len(original.comments) == 1
    assert len(fresh.comments) == 2
    fresh_calls = gh.calls[before:]
    assert fresh_calls
    assert all("--cache" not in call for call in fresh_calls)


def test_fresh_linear_ticket_skips_ticket_cache_and_all_github_caches() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    issue = linear_issue()

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        assert request["query"].startswith("query FactoryIssue")
        return HTTPResult(200, json.dumps({"data": {"issue": issue}}))

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=transport,
    )
    project = source.config.projects[0]
    original = source._linear_ticket(project, issue)
    before = len(gh.calls)
    issue["comments"]["nodes"].append(
        {
            "id": "comment-2",
            "body": (
                "`orch:v1 kind=stuck from=grok-bot role=orchestrator "
                "state=blocked`\n"
            ),
            "createdAt": "2026-11-11T12:02:00Z",
            "user": {"id": "user-1"},
        }
    )

    fresh = source.fresh_ticket(
        TicketRef("acme/widgets", "AUT-42", "linear")
    )

    assert fresh is not None
    assert fresh is not original
    assert len(original.comments) + 1 == len(fresh.comments)
    fresh_calls = gh.calls[before:]
    assert any(
        "repos/acme/widgets/pulls" in call and "head=acme:max/aut-42-fixture" in call
        for call in fresh_calls
    )
    assert all("--cache" not in call for call in fresh_calls)


def test_linear_candidate_parsing_uses_handoff_query() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.comments = [
        {
            "id": 77,
            "user": {"login": "maxharris1"},
            "body": (
                "<!-- orch:v1 kind=ready from=claude-bot role=builder "
                f"head={gh.head} -->\n"
            ),
            "created_at": "2026-11-11T12:01:00Z",
            "html_url": "https://github.test/pr/12#issuecomment-77",
        }
    ]
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, timeout
        assert headers["Authorization"] == "secret-linear-key"
        request = json.loads(body)
        requests.append(request)
        if request["query"].startswith("query FactoryPoll"):
            return linear_search_page(
                [
                    {
                        "id": "issue-id",
                        "identifier": "AUT-42",
                        "project": {"id": "project-1"},
                    }
                ]
            )
        return HTTPResult(
            200,
            json.dumps({"data": {"issue": linear_issue()}}),
        )

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "secret-linear-key"},
        gh_runner=gh,
        linear_transport=transport,
    )

    tickets = source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert tickets[0].ticket_id == "AUT-42"
    assert [comment.author for comment in tickets[0].comments] == [
        "linear:user-1",
        "maxharris1",
    ]
    assert tickets[0].pr_number == "12"
    assert tickets[0].current_head == gh.head
    assert tickets[0].ci[gh.head].status == "success"
    assert [request["variables"] for request in requests[:2]] == [
        {
            "term": term,
            "first": 50,
            "after": None,
            "projectIds": ["project-1"],
            "includeComments": True,
        }
        for term in ("orch:v1 kind=brief", "orch:v1 kind=go")
    ]
    assert requests[2]["variables"] == {"id": "issue-id"}
    search_query = requests[0]["query"]
    assert "comments(" not in search_query
    assert "attachments(" not in search_query
    assert "project: { id: { in: $projectIds } }" in search_query
    assert 'state: { type: { nin: ["completed", "canceled"] } }' in search_query


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


def test_linear_search_page_cap_records_discovery_cutoff() -> None:
    requests: list[dict] = []
    errors: list[str] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        requests.append(request)
        page = len(requests)
        return linear_search_page(
            [
                {
                    "id": f"issue-{page:03d}",
                    "identifier": f"AUT-{page:03d}",
                    "project": {"id": "project-1"},
                }
            ],
            has_next=True,
            cursor=str(page),
        )

    deferrals: list[str] = []
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=transport,
        error_sink=lambda repo, ticket, error: errors.append(error),
        deferral_sink=deferrals.append,
    )
    source.begin_cycle()

    assert len(source._linear_issues("one term")) == LINEAR_SEARCH_MAX_PAGES
    assert len(requests) == LINEAR_SEARCH_MAX_PAGES
    assert errors == []
    source.end_cycle()
    assert deferrals == ["Linear search page limit deferred discovery x1"]


def test_linear_request_hard_cap_is_retryable() -> None:
    calls = 0

    def transport(url, headers, body, timeout):
        nonlocal calls
        del url, headers, body, timeout
        calls += 1
        return HTTPResult(200, json.dumps({"data": {"issue": None}}))

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=transport,
    )
    source.begin_cycle()
    limit = (
        int(LINEAR_REQUESTS_PER_HOUR * LINEAR_SHARE)
        * source.config.poll_seconds
        // 3600
    )

    for index in range(limit):
        assert source._linear_issue(f"AUT-{index:03d}") is None
    with pytest.raises(RetryableStateError, match="budget"):
        source._linear_issue("AUT-over-limit")

    assert calls == limit


def test_tracked_budget_deferral_keeps_cached_and_github_tickets() -> None:
    errors: list[tuple[str, str, str]] = []
    deferrals: list[str] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        identifier = request["variables"]["id"]
        return HTTPResult(
            200,
            json.dumps(
                {
                    "data": {
                        "issue": linear_issue(identifier=identifier)
                    }
                }
            ),
        )

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="linear", ship="handoff")),
        linear_transport=transport,
        error_sink=lambda repo, ticket, error: errors.append(
            (repo, ticket, error)
        ),
        deferral_sink=deferrals.append,
    )
    source.begin_cycle()
    source._linear_issue_cache["AUT-cached"] = linear_issue(
        identifier="AUT-cached"
    )
    refs = tuple(
        TicketRef("acme/widgets", f"AUT-{index:03d}", "linear")
        for index in range(41)
    ) + (
        TicketRef("acme/widgets", "AUT-cached", "linear"),
        TicketRef("acme/widgets", "12", "github"),
    )

    found = source.tracked_tickets(refs)

    assert len(
        [ticket for ticket in found if ticket.ticket_id.startswith("AUT-")]
    ) == 19
    assert any(ticket.ticket_id == "AUT-cached" for ticket in found)
    assert any(ticket.ticket_id == "12" for ticket in found)
    assert errors == []
    assert {f"AUT-{index:03d}" for index in range(18)} <= {
        ticket.ticket_id for ticket in found
    }
    source.end_cycle()
    assert deferrals == ["Linear poll budget deferred tracked ticket x23"]


def test_tracked_budget_rotation_serves_every_ticket() -> None:
    reads_by_cycle: list[list[str]] = []
    current_reads: list[str] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        identifier = request["variables"]["id"]
        current_reads.append(identifier)
        return HTTPResult(
            200,
            json.dumps(
                {
                    "data": {
                        "issue": linear_issue(identifier=identifier)
                    }
                }
            ),
        )

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="linear", ship="handoff")),
        linear_transport=transport,
    )
    refs = tuple(
        TicketRef("acme/widgets", f"AUT-{index:03d}", "linear")
        for index in range(41)
    )

    for _cycle in range(3):
        source.begin_cycle()
        source.tracked_tickets(refs)
        reads_by_cycle.append(list(current_reads))
        current_reads.clear()

    assert all(len(reads) == 18 for reads in reads_by_cycle)
    assert reads_by_cycle[0] != reads_by_cycle[1]
    assert {
        identifier
        for reads in reads_by_cycle
        for identifier in reads
    } == {f"AUT-{index:03d}" for index in range(41)}


@pytest.mark.parametrize(
    "result",
    [
        HTTPResult(503, ""),
        HTTPResult(200, json.dumps({"errors": [{"message": "no"}]})),
    ],
)
def test_linear_search_reports_http_and_graphql_errors(result: HTTPResult) -> None:
    errors: list[tuple[str, str, str]] = []
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=lambda url, headers, body, timeout: result,
        error_sink=lambda repo, ticket, error: errors.append(
            (repo, ticket, error)
        ),
    )

    source.begin_cycle()
    assert source.linear_candidates(LINEAR_HANDOFF_QUERY) == ()
    with pytest.raises(PollCycleError):
        source.end_cycle()
    assert len(errors) == 2


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
        linear_transport=linear_transport_for(linear_issue(user=None)),
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
        linear_transport=linear_transport_for(issue),
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


def test_tracked_linear_ticket_reuses_candidate_issue_in_the_same_cycle() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        requests.append(request)
        if request["query"].startswith("query FactoryPoll"):
            return linear_search_page(
                [
                    {
                        "id": "issue-id",
                        "identifier": "AUT-42",
                        "project": {"id": "project-1"},
                    }
                ]
            )
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
    source.begin_cycle()

    candidates = source.linear_candidates(LINEAR_HANDOFF_QUERY)
    tracked = source.tracked_tickets(
        (TicketRef("acme/widgets", "AUT-42", "linear"),)
    )

    assert tracked == candidates
    assert sum(
        request["query"].startswith("query FactoryIssue")
        for request in requests
    ) == 1


@pytest.mark.parametrize(
    "limited",
    [
        HTTPResult(429, "", {"Retry-After": "30"}),
        HTTPResult(
            200,
            json.dumps(
                {
                    "errors": [
                        {"extensions": {"code": "RATELIMITED"}}
                    ]
                }
            ),
            {"retry-after": "30"},
        ),
    ],
)
def test_linear_rate_limit_uses_retry_after_backoff(
    limited: HTTPResult,
) -> None:
    now = [100.0]
    results = [limited, linear_search_page([])]
    calls = 0

    def transport(url, headers, body, timeout):
        nonlocal calls
        del url, headers, body, timeout
        calls += 1
        return results.pop(0)

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        linear_transport=transport,
        clock=lambda: now[0],
    )

    with pytest.raises(
        RetryableStateError,
        match="429|RATELIMITED",
    ):
        source._linear_issues("one")
    now[0] = 129.0
    with pytest.raises(RetryableStateError, match="backoff"):
        source._linear_issues("one")
    assert calls == 1

    now[0] = 130.0
    assert source._linear_issues("one") == ()
    assert calls == 2


@pytest.mark.parametrize(
    (
        "matching",
        "active",
        "done",
        "expected_requests_per_hour",
        "expected_complexity_per_hour",
    ),
    [
        (5, 1, 2, 360, 169_140),
        (25, 5, 10, 1_080, 730_020),
        (100, 20, 40, 1_080, 650_460),
        (500, 20, 460, 1_080, 730_020),
    ],
)
def test_linear_poll_hour_stays_within_shared_budget(
    tmp_path: Path,
    matching: int,
    active: int,
    done: int,
    expected_requests_per_hour: int,
    expected_complexity_per_hour: int,
) -> None:
    issues = []
    by_id = {}
    for index in range(matching):
        identifier = f"AUT-{index:04d}"
        issue = linear_issue(identifier=identifier)
        issue["id"] = f"issue-{index:04d}"
        issue["comments"]["nodes"][0]["id"] = f"comment-{index:04d}"
        issues.append(issue)
        by_id[issue["id"]] = issue
        by_id[identifier] = issue

    searchable = [
        issue
        for index, issue in enumerate(issues)
        if index < active or index >= active + done
    ]
    requests: list[tuple[int, dict]] = []
    cycle = 0

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        requests.append((cycle, request))
        if request["query"].startswith("query FactoryPoll"):
            start = int(request["variables"]["after"] or 0)
            end = min(start + 50, len(searchable))
            return linear_search_page(
                [
                    {
                        "id": issue["id"],
                        "identifier": issue["identifier"],
                        "project": issue["project"],
                    }
                    for issue in searchable[start:end]
                ],
                has_next=end < len(searchable),
                cursor=str(end) if end < len(searchable) else None,
            )
        if request["query"].startswith("query FactoryStates"):
            return linear_states_page(request["variables"])
        identifier = request["variables"]["id"]
        return HTTPResult(
            200,
            json.dumps({"data": {"issue": by_id[identifier]}}),
        )

    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={
            "github": {},
            "linear": {"linear:user-1": {"grok-bot"}},
        },
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    with queue.connect() as connection:
        for index in range(active + done):
            identifier = f"AUT-{index:04d}"
            connection.execute(
                """
                INSERT INTO tracked_tickets (
                    repo, ticket_id, tracker, tracked_at
                ) VALUES ('acme/widgets', ?, 'linear', 0)
                """,
                (identifier,),
            )
            connection.execute(
                """
                INSERT INTO ticket_state (
                    repo, ticket_id, workflow_state,
                    gate_count, stuck, generation
                ) VALUES ('acme/widgets', ?, ?, 0, 0, 1)
                """,
                (
                    identifier,
                    "building" if index < active else "done",
                ),
            )
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="linear", ship="handoff")),
        linear_transport=transport,
        error_sink=dispatcher.record_poll_error,
        deferral_sink=dispatcher.record_poll_deferral,
    )

    cycles = 3600 // source.config.poll_seconds
    for cycle in range(cycles):
        dispatcher.poll_once(source)

    searches = sum(
        request["query"].startswith("query FactoryPoll")
        for _, request in requests
    )
    state_reads = sum(
        request["query"].startswith("query FactoryStates")
        for _, request in requests
    )
    issue_reads = len(requests) - searches - state_reads
    requests_per_hour = len(requests)
    complexity_per_hour = (
        searches * LINEAR_SEARCH_COMPLEXITY
        + state_reads * LINEAR_STATES_COMPLEXITY
        + issue_reads * LINEAR_ISSUE_COMPLEXITY
    )
    requests_by_cycle = [
        sum(request_cycle == index for request_cycle, _ in requests)
        for index in range(cycles)
    ]
    read_identifiers = {
        by_id[request["variables"]["id"]]["identifier"]
        for _, request in requests
        if request["query"].startswith("query FactoryIssue")
    }
    expected_read = {
        issue["identifier"]
        for issue in searchable
    }
    finished = {
        issues[index]["identifier"]
        for index in range(active, active + done)
    }
    shared_request_budget = int(
        LINEAR_REQUESTS_PER_HOUR * LINEAR_SHARE
    )
    shared_complexity_budget = int(
        LINEAR_COMPLEXITY_PER_HOUR * LINEAR_SHARE
    )
    cycle_request_limit = (
        shared_request_budget
        * source.config.poll_seconds
        // 3600
    )

    assert len(requests_by_cycle) == cycles == 60
    assert state_reads == cycles
    assert max(requests_by_cycle) <= cycle_request_limit
    assert requests_per_hour == expected_requests_per_hour
    assert complexity_per_hour == expected_complexity_per_hour
    assert read_identifiers == expected_read
    assert read_identifiers.isdisjoint(finished)
    assert requests_per_hour <= shared_request_budget
    assert complexity_per_hour <= shared_complexity_budget
    with queue.connect() as connection:
        recorded_errors = connection.execute(
            "SELECT count(*) FROM poll_errors"
        ).fetchone()[0]
        deferral_rows = connection.execute(
            "SELECT count(*) FROM poll_deferrals"
        ).fetchone()[0]
        discovery_cutoffs = connection.execute(
            """
            SELECT count(*) FROM poll_deferrals
            WHERE detail LIKE '%Linear poll budget deferred discovery%'
            """
        ).fetchone()[0]
    assert recorded_errors == 0
    assert deferral_rows <= cycles
    search_pages = 2 * ((len(searchable) + 49) // 50)
    discovery_capacity = (
        cycle_request_limit
        - search_pages
        - LINEAR_PROCESS_HEADROOM
        - min(active, LINEAR_TRACKED_RESERVE)
    )
    if matching - active - done > discovery_capacity:
        assert discovery_cutoffs > 0
    else:
        assert discovery_cutoffs == 0


def test_missing_linear_key_names_env_without_exposing_values(caplog) -> None:
    errors: list[str] = []
    source = LivePollSource(
        config(linear=True),
        environ={},
        gh_runner=lambda argv, timeout: GhResult(1, "", ""),
        error_sink=lambda repo, ticket, error: errors.append(error),
    )

    source.begin_cycle()
    assert source.linear_candidates(LINEAR_HANDOFF_QUERY) == ()
    with pytest.raises(PollCycleError):
        source.end_cycle()

    assert all("LINEAR_KEY" in error for error in errors)
    assert "secret-linear-key" not in " ".join(errors)
    assert "secret-linear-key" not in caplog.text


def test_linear_transport_error_does_not_expose_key() -> None:
    def transport(url, headers, body, timeout):
        del url, headers, body, timeout
        raise RuntimeError("secret-linear-key")

    errors: list[str] = []
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "secret-linear-key"},
        gh_runner=lambda argv, timeout: GhResult(1, "", ""),
        linear_transport=transport,
        error_sink=lambda repo, ticket, error: errors.append(error),
    )

    source.begin_cycle()
    assert source.linear_candidates(LINEAR_HANDOFF_QUERY) == ()
    with pytest.raises(PollCycleError):
        source.end_cycle()

    assert "secret-linear-key" not in " ".join(errors)


def test_webhook_delivery_uses_job_contract_and_does_not_log_key(caplog) -> None:
    sent: list[dict] = []

    def transport(url, authorization, body, timeout):
        del timeout
        assert url == "https://webhook.test"
        assert authorization == "Bearer secret-webhook-key"
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
            "target_bot": None,
        }
    ]
    assert "secret-webhook-key" not in caplog.text


def test_webhook_delivery_routes_json_project_bot() -> None:
    sent: list[dict] = []
    delivery = WebhookGrokDelivery(
        url_env="FACTORY_WEBHOOK_URL",
        key_env="FACTORY_WEBHOOK_KEY",
        environ={
            "FACTORY_WEBHOOK_URL": "https://webhook.test",
            "FACTORY_WEBHOOK_KEY": "key",
        },
        transport=lambda url, key, body, timeout: (
            sent.append(json.loads(body))
            or WebhookResult(200, {"X-Request-Id": "receipt"})
        ),
    )
    request = JobRequest(
        repo="aut0-code/aut0",
        ticket_id="AUT-42",
        action="stuck",
        recipient="grok",
        source_comment_id="7",
        timeout_seconds=60,
        detail=json.dumps({"project_bot": "offer-brief-project"}),
    )
    job = Job("job-1", "dedupe", request, "running", 1, 1, None, None)

    delivery.deliver(job, logical_key="logical-1")

    assert sent[0]["target_bot"] == "offer-brief-project"


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


def grok_job() -> Job:
    request = JobRequest(
        repo="acme/widgets",
        ticket_id="12",
        action="stuck",
        recipient="grok",
        source_comment_id="7",
        timeout_seconds=60,
    )
    return Job("job-1", "dedupe", request, "running", 1, 1, None, None)


def webhook_delivery(environ: dict[str, str], sent: list[str]) -> WebhookGrokDelivery:
    def transport(url, authorization, body, timeout):
        del url, body, timeout
        sent.append(authorization)
        return WebhookResult(200, {"X-Request-Id": "receipt"})

    return WebhookGrokDelivery(
        url_env="FACTORY_WEBHOOK_URL",
        key_env="FACTORY_WEBHOOK_KEY",
        auth_env="FACTORY_WEBHOOK_AUTH",
        environ={"FACTORY_WEBHOOK_URL": "https://webhook.test", **environ},
        transport=transport,
    )


@pytest.mark.parametrize(
    ("environ", "authorization"),
    [
        (
            {"FACTORY_WEBHOOK_AUTH": "Bearer secret-auth-value"},
            "Bearer secret-auth-value",
        ),
        ({"FACTORY_WEBHOOK_KEY": "secret-key-value"}, "Bearer secret-key-value"),
        (
            {
                "FACTORY_WEBHOOK_AUTH": "Bearer secret-auth-value",
                "FACTORY_WEBHOOK_KEY": "secret-key-value",
            },
            "Bearer secret-auth-value",
        ),
    ],
    ids=["auth-only", "key-only", "auth-wins"],
)
def test_webhook_auth_is_verbatim_and_key_gets_bearer(
    environ: dict[str, str],
    authorization: str,
) -> None:
    sent: list[str] = []

    webhook_delivery(environ, sent).deliver(grok_job(), logical_key="logical-1")

    assert sent == [authorization]


def test_webhook_without_auth_or_key_names_every_accepted_var() -> None:
    sent: list[str] = []
    delivery = webhook_delivery({}, sent)

    with pytest.raises(GrokDeliveryError) as raised:
        delivery.deliver(grok_job(), logical_key="logical-1")

    assert "FACTORY_WEBHOOK_AUTH" in str(raised.value)
    assert "FACTORY_WEBHOOK_KEY" in str(raised.value)
    assert raised.value.detail == (
        "missing-env:FACTORY_WEBHOOK_AUTH|FACTORY_WEBHOOK_KEY"
    )
    assert sent == []


@pytest.mark.parametrize("name", ["FACTORY_WEBHOOK_AUTH", "FACTORY_WEBHOOK_KEY"])
def test_webhook_errors_never_contain_the_secret(name: str) -> None:
    def transport(url, authorization, body, timeout):
        del url, body, timeout
        raise RuntimeError(authorization)

    delivery = WebhookGrokDelivery(
        url_env="FACTORY_WEBHOOK_URL",
        key_env="FACTORY_WEBHOOK_KEY",
        auth_env="FACTORY_WEBHOOK_AUTH",
        environ={
            "FACTORY_WEBHOOK_URL": "https://secret-host.test/hook",
            name: "secret-webhook-value",
        },
        transport=transport,
    )

    with pytest.raises(GrokDeliveryError) as raised:
        delivery.deliver(grok_job(), logical_key="logical-1")

    text = f"{raised.value} {raised.value.detail}"
    assert "secret-webhook-value" not in text
    assert "secret-host" not in text
    assert raised.value.detail == "RuntimeError"
    assert raised.value.__cause__ is None


def test_send_webhook_sends_auth_verbatim_and_derives_automation_key(
    monkeypatch,
) -> None:
    sent: list[dict[str, str]] = []

    class Response:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout):
        del timeout
        sent.append(dict(request.header_items()))
        return Response()

    monkeypatch.setattr("lib.grok_delivery._OPENER.open", fake_open)

    send_webhook("https://webhook.test", "Bearer secret-key", b"{}", 1)

    assert sent[0]["Authorization"] == "Bearer secret-key"
    assert sent[0]["X-automation-key"] == "secret-key"


@pytest.mark.parametrize(
    ("environ", "transport_status", "expected"),
    [
        (
            {"FACTORY_WEBHOOK_KEY": "secret-webhook-value"},
            503,
            "grok-delivery:GrokDeliveryError:HTTP-503",
        ),
        (
            {},
            200,
            "grok-delivery:GrokDeliveryError:"
            "missing-env:FACTORY_WEBHOOK_AUTH|FACTORY_WEBHOOK_KEY",
        ),
    ],
    ids=["http-status", "missing-env"],
)
def test_grok_delivery_failure_records_safe_detail(
    tmp_path: Path,
    caplog,
    capsys,
    environ: dict[str, str],
    transport_status: int,
    expected: str,
) -> None:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    delivery = WebhookGrokDelivery(
        url_env="FACTORY_WEBHOOK_URL",
        key_env="FACTORY_WEBHOOK_KEY",
        auth_env="FACTORY_WEBHOOK_AUTH",
        environ={
            "FACTORY_WEBHOOK_URL": "https://secret-host.test/hook",
            **environ,
        },
        transport=lambda url, authorization, body, timeout: WebhookResult(
            transport_status, {}
        ),
    )
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {"grok-login": {"grok-bot"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=delivery,
    )
    queue.enqueue(grok_job().request, dedupe_key="grok-1")

    dispatcher.reconcile_jobs()

    with queue.connect() as connection:
        errors = [
            str(row[0])
            for row in connection.execute("SELECT error FROM poll_errors")
        ]
    assert errors and set(errors) == {expected}
    captured = capsys.readouterr()
    for text in (" ".join(errors), caplog.text, captured.out, captured.err):
        assert "secret-webhook-value" not in text
        assert "secret-host" not in text


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
        lambda db, orch, create, **_: (dispatcher, fake_source),
    )

    result = cli.main(["poll", "--db", str(database), "--once"])

    assert result == 0
    assert dispatcher.polls == 1


def test_poll_line_buffers_stdout_and_stderr(monkeypatch, tmp_path: Path) -> None:
    streams = {
        name: io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        for name in ("stdout", "stderr")
    }
    for name, stream in streams.items():
        monkeypatch.setattr(cli.sys, name, stream)
    database = tmp_path / "orch.db"
    database.write_bytes(b"database")
    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda db, orch, create, **_: (Mock(), object()),
    )

    assert cli.main(["poll", "--db", str(database), "--once"]) == 0
    assert all(stream.line_buffering for stream in streams.values())


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
        trusted_logins={
            "github": {"grok-login": {"grok-bot"}},
            "linear": {},
        },
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
        def github_candidates(self, label_prefix, *, exclude=frozenset()):
            del label_prefix
            return (ticket,)

        def linear_candidates(self, query, *, exclude, reserve):
            del query, exclude, reserve
            return ()

        def tracked_tickets(self, refs):
            del refs
            return (ticket,)

    source = Source()
    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda db, orch, create, **_: (dispatcher, source),
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


ADAPTER_READS = {
    "comments": "/issues/12/comments?per_page=100",
    "check-runs": "/check-runs?per_page=100",
    "combined-status": "/status",
}


def _flaky_read_runner(gh: FakeGh, suffix: str, failures: int):
    timeouts: list[tuple[str, float]] = []
    remaining = [failures]

    def runner(argv, timeout):
        endpoint = next(part for part in argv if part.startswith("repos/"))
        timeouts.append((endpoint, timeout))
        if endpoint.endswith(suffix) and remaining[0]:
            remaining[0] -= 1
            raise subprocess.TimeoutExpired(argv, timeout)
        return gh(argv, timeout)

    return runner, timeouts


@pytest.mark.parametrize("read", sorted(ADAPTER_READS))
def test_github_ref_adapter_reads_retry_with_short_timeout_and_backoff(
    read: str,
) -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    runner, timeouts = _flaky_read_runner(gh, ADAPTER_READS[read], 2)
    sleeps: list[float] = []
    source = LivePollSource(config(), gh_runner=runner, sleep=sleeps.append)
    factory = source._factory(config().projects[0])

    ticket = source._github_ticket(config().projects[0], factory, "12")

    assert ticket.ci[gh.head].status == "success"
    assert sleeps == [GH_RETRY_BACKOFF_SECONDS, GH_RETRY_BACKOFF_SECONDS * 2]
    assert {timeout for _, timeout in timeouts} == {GH_CALL_TIMEOUT_SECONDS}
    assert (
        sum(endpoint.endswith(ADAPTER_READS[read]) for endpoint, _ in timeouts)
        == GH_CALL_ATTEMPTS
    )


@pytest.mark.parametrize("read", sorted(ADAPTER_READS))
def test_linear_linked_pr_adapter_reads_retry_with_short_timeout_and_backoff(
    read: str,
) -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    runner, timeouts = _flaky_read_runner(gh, ADAPTER_READS[read], 2)
    sleeps: list[float] = []
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=runner,
        sleep=sleeps.append,
    )

    ticket = source._linear_ticket(config(linear=True).projects[0], linear_issue())

    assert ticket.pr_number == "12"
    assert ticket.ci[gh.head].status == "success"
    assert sleeps == [GH_RETRY_BACKOFF_SECONDS, GH_RETRY_BACKOFF_SECONDS * 2]
    assert {timeout for _, timeout in timeouts} == {GH_CALL_TIMEOUT_SECONDS}
    assert (
        sum(endpoint.endswith(ADAPTER_READS[read]) for endpoint, _ in timeouts)
        == GH_CALL_ATTEMPTS
    )


def test_poll_adapter_never_retries_a_mutation() -> None:
    calls: list[float] = []
    sleeps: list[float] = []

    def runner(argv, timeout):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    source = LivePollSource(config(), gh_runner=runner, sleep=sleeps.append)

    with pytest.raises(GitHubAdapterError) as raised:
        source.github._call(
            "-X", "POST", "repos/acme/widgets/issues/12/comments", "-f", "body=x"
        )
    assert calls == [TIMEOUT_SECONDS]
    assert sleeps == []
    # Round 2 (F2.4): the message names the 60 s the write actually ran with.
    assert str(raised.value) == f"gh api timed out after {TIMEOUT_SECONDS:g}s"


NON_READ_CALLS = {
    "field-implies-post": ("repos/acme/widgets/issues/12/comments", "-f", "body=x"),
    "typed-field": ("repos/acme/widgets/issues/12/labels", "-F", "labels[]=x"),
    "long-field": ("repos/acme/widgets/issues/12/comments", "--field", "body=x"),
    "long-field-equals": ("repos/acme/widgets/issues/12/comments", "--field=body=x"),
    "raw-field": ("repos/acme/widgets/issues/12/comments", "--raw-field", "body=x"),
    "attached-field": ("repos/acme/widgets/issues/12/comments", "-fbody=x"),
    "input": ("--input", "-", "repos/acme/widgets/issues/12/comments"),
    "method-post": ("--method", "POST", "repos/acme/widgets/issues/12/comments"),
    "method-equals-patch": ("--method=PATCH", "repos/acme/widgets/pulls/12"),
    "attached-x-delete": ("-XDELETE", "repos/acme/widgets/issues/12/labels/x"),
    "lowercase-x-put": ("-X", "put", "repos/acme/widgets/pulls/12/merge"),
    "graphql": ("graphql", "-f", "query=mutation { addStar(input: {}) { clientMutationId } }"),
    "graphql-query-file": ("graphql", "-F", "query=@query.graphql"),
    "graphql-no-fields": ("graphql",),
    "get-with-input": ("-X", "GET", "repos/acme/widgets/pulls", "--input", "-"),
}


@pytest.mark.parametrize("name", sorted(NON_READ_CALLS))
def test_poll_adapter_runs_every_non_read_form_once(name: str) -> None:
    calls: list[float] = []
    sleeps: list[float] = []

    def runner(argv, timeout):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    source = LivePollSource(config(), gh_runner=runner, sleep=sleeps.append)

    with pytest.raises(GitHubAdapterError) as raised:
        source.github._call(*NON_READ_CALLS[name])
    assert calls == [TIMEOUT_SECONDS]
    assert sleeps == []
    assert str(raised.value) == f"gh api timed out after {TIMEOUT_SECONDS:g}s"


@pytest.mark.parametrize("name", sorted(NON_READ_CALLS))
def test_is_read_rejects_every_non_read_form(name: str) -> None:
    """Round 2 (F2.3): the classifier itself, so --cache is never added to a
    write either."""
    assert not _is_read(NON_READ_CALLS[name])


@pytest.mark.parametrize(
    "args",
    [
        ("repos/acme/widgets/issues/12/comments",),
        ("repos/acme/widgets/issues/12/comments", "--paginate"),
        ("-X", "GET", "repos/acme/widgets/commits/abc/check-runs"),
        ("--method", "GET", "repos/acme/widgets/commits/abc/status"),
        ("--method=get", "repos/acme/widgets/pulls/12"),
    ],
)
def test_is_read_accepts_plain_gets(args: tuple[str, ...]) -> None:
    """Preservation guard: real reads stay reads (passes before and after)."""
    assert _is_read(args)


def test_adapter_read_timeout_on_one_ref_leaves_a_healthy_ref_alone(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {"grok-login": {"grok-bot"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    with queue.connect() as connection:
        for number in ("12", "13"):
            connection.execute(
                """
                INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)
                VALUES ('acme/widgets', ?, 'github', 0)
                """,
                (number,),
            )
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    sleeps: list[float] = []
    timeouts: list[float] = []

    def runner(argv, timeout):
        timeouts.append(timeout)
        if any(part.endswith("/issues/12/comments?per_page=100") for part in argv):
            raise subprocess.TimeoutExpired(argv, timeout)
        return gh(argv, timeout)

    source = LivePollSource(
        OrchConfig(
            poll_seconds=60,
            projects=(Project("acme/widgets", factory_ref="main"),),
        ),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        sleep=sleeps.append,
    )
    dispatcher.process = Mock()
    dispatcher.reconcile_jobs = Mock()

    with pytest.raises(PollCycleError):
        dispatcher.poll_once(source)

    assert [call.args[0].ticket_id for call in dispatcher.process.call_args_list] == [
        "13"
    ]
    assert sleeps == [GH_RETRY_BACKOFF_SECONDS, GH_RETRY_BACKOFF_SECONDS * 2]
    assert set(timeouts) == {GH_CALL_TIMEOUT_SECONDS}
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT repo, ticket_id, error FROM poll_errors"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "acme/widgets",
            "12",
            f"PollSourceError:gh api GET issues/12/comments timed out after {GH_CALL_ATTEMPTS} attempts",
        )
    ]


# Codex r1 F1.2: the handoff moves a Linear issue only from before review,
# so the issue read carries its current state and the team's state order.
# Lessons batch 1: Backlog and Triage are before review, and a team with no
# Ready for Review state is named (no-ready-state), not before-review.

RFR = {"id": "rfr", "name": "Ready for Review", "type": "started", "position": 3.0}


def phase_issue(state: object, *, ready: dict | None = RFR) -> dict:
    nodes = [
        {"id": "todo", "name": "Todo", "type": "unstarted", "position": 1.0},
        {"id": "doing", "name": "In Progress", "type": "started", "position": 2.0},
        {"id": "qa", "name": "In QA", "type": "started", "position": 4.0},
        {"id": "done", "name": "Done", "type": "completed", "position": 5.0},
    ]
    if ready is not None:
        nodes.append(ready)
    return {"state": state, "team": {"states": {"nodes": nodes}}}


def test_linear_issue_query_reads_the_issue_state_and_the_state_order() -> None:
    assert "    state { id type position }\n" in LINEAR_ISSUE_QUERY
    assert "states { nodes { id name type position } }" in LINEAR_ISSUE_QUERY


@pytest.mark.parametrize(
    ("issue", "phase"),
    [
        (phase_issue({"id": "doing", "type": "started", "position": 2.0}), "before-review"),
        (phase_issue({"id": "todo", "type": "unstarted", "position": 1.0}), "before-review"),
        (phase_issue({"id": "rfr", "type": "started", "position": 3.0}), "ready-for-review"),
        (phase_issue({"id": "qa", "type": "started", "position": 4.0}), "moved-on"),
        (phase_issue({"id": "done", "type": "completed", "position": 5.0}), "moved-on"),
        (phase_issue({"id": "x", "type": "canceled", "position": 6.0}), "moved-on"),
        (phase_issue({"id": "b", "type": "backlog", "position": 0.0}), "before-review"),
        (phase_issue({"id": "t", "type": "triage", "position": 0.0}), "before-review"),
        (phase_issue({"id": "doing", "type": "started", "position": 2.0}, ready=None), "no-ready-state"),
        (phase_issue({"id": "doing", "type": "started"}), None),
        (phase_issue({"id": "doing", "type": "mystery", "position": 2.0}), None),
        (phase_issue({"type": "started", "position": 2.0}), None),
        (phase_issue(None), None),
        ({}, None),
    ],
    ids=[
        "in-progress",
        "todo",
        "ready-for-review",
        "started-after-rfr",
        "done",
        "canceled",
        "backlog",
        "triage",
        "no-rfr-state",
        "no-position",
        "unknown-type",
        "no-id",
        "no-state",
        "empty-issue",
    ],
)
def test_linear_state_phase_places_the_issue_relative_to_ready_for_review(
    issue: dict,
    phase: str | None,
) -> None:
    assert _linear_state_phase(issue) == phase


def test_linear_ticket_carries_the_issue_state_phase() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    issue = {
        **linear_issue(),
        **phase_issue({"id": "done", "type": "completed", "position": 5.0}),
    }
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=linear_transport_for(issue),
    )

    ticket = source._linear_ticket(source.config.projects[0], issue)

    assert ticket.linear_state_phase == "moved-on"
    assert ticket.linear_ready_state_id == "rfr"


def test_a_linear_ticket_follows_the_projects_handoff_and_rework_states() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    issue = {
        **linear_issue(),
        **phase_issue(
            {"id": "cr", "type": "started", "position": 4.5},
            ready={"id": "qar", "name": "QA Review", "type": "started", "position": 3.0},
        ),
    }
    issue["team"]["states"]["nodes"] += [
        RFR,
        {"id": "cr", "name": "Changes Requested", "type": "started", "position": 4.5},
    ]
    base = config(linear=True)
    project = replace(
        base.projects[0],
        linear_handoff_states=("QA Review", "In Review"),
        linear_rework_states=("Changes Requested",),
    )
    source = LivePollSource(
        replace(base, projects=(project,)),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh,
        linear_transport=linear_transport_for(issue),
    )

    ticket = source._linear_ticket(project, issue)

    assert ticket.linear_ready_state_id == "qar"
    assert ticket.linear_state_phase == "rework"


def test_a_github_ticket_carries_its_source_issue_and_open_title() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = {"state": "open", "title": "Do it", "body": "Fixes #11", "head": {"ref": "x"}}
    source = LivePollSource(config(), gh_runner=gh)
    source.begin_cycle()

    (found,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))

    assert (found.source_issue, found.pr_title) == ("#11", "Do it")


@pytest.mark.parametrize(
    ("body", "wanted"),
    [
        ("Fixes #11", "#11"),
        ("Fixes acme/widgets#11", "#11"),
        ("Fixes ACME/Widgets#11", "#11"),
        ("Fixes https://github.com/acme/widgets/issues/11", "#11"),
        ("Fixes acme/other#11", None),
    ],
)
def test_a_closing_reference_to_the_prs_own_repo_names_its_source(
    tmp_path: Path, body: str, wanted: str | None
) -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = {"state": "open", "title": "Do it", "body": body, "head": {"ref": "feature/x"}}
    source = LivePollSource(config(), gh_runner=gh)
    source.begin_cycle()
    (found,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))
    actions = FakeReviewActions()
    dispatcher, _ = setup(tmp_path, review_actions=actions)

    dispatcher.process(found)

    assert found.source_issue == wanted
    assert actions.titles == ([("12", f"{wanted}: Do it")] if wanted else [])


def test_a_closed_prs_title_is_not_offered_for_a_rename() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.pull = {"state": "closed", "title": "Do it", "head": {"ref": "factory/issue-11"}}
    source = LivePollSource(config(), gh_runner=gh)
    source.begin_cycle()

    (found,) = source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),))

    assert (found.source_issue, found.pr_title) == ("#11", None)


def test_a_linear_ticket_with_a_linked_pr_has_its_key_as_source() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    gh.pull = {"state": "open", "title": "Fix login"}

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        if json.loads(body)["query"].startswith("query FactoryPoll"):
            return linear_search_page(
                [{"id": "issue-id", "identifier": "AUT-42", "project": {"id": "project-1"}}]
            )
        return HTTPResult(200, json.dumps({"data": {"issue": linear_issue()}}))

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "secret-linear-key"},
        gh_runner=gh,
        linear_transport=transport,
    )

    (found,) = source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert (found.pr_number, found.source_issue, found.pr_title) == ("12", "AUT-42", "Fix login")


# --- #108 Unit 3: every tracked ticket's merge is seen ------------------------


def _merged_runner(gh: FakeGh):
    def runner(argv, timeout):
        endpoint = next(part for part in argv if part.startswith(("repos/", "search/")))
        if endpoint == "repos/acme/widgets/pulls/12":
            gh.calls.append(list(argv))
            return GhResult(
                0,
                json.dumps({"merged": True, "head": {"sha": gh.head}, "html_url": "u"}),
                "",
            )
        return gh(argv, timeout)

    return runner


def test_merged_github_pr_passes_a_label_ticket_to_the_sink() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    sunk: list = []
    source = LivePollSource(
        config(),
        gh_runner=_merged_runner(gh),
        terminal_sink=lambda *args: sunk.append(args),
    )

    assert source.tracked_tickets((TicketRef("acme/widgets", "12", "github"),)) == ()

    [(repo, number, state, merged)] = sunk
    assert (repo, number, state) == ("acme/widgets", "12", "merged")
    assert (merged.pr_number, merged.current_head, merged.merged) == ("12", gh.head, True)


def test_merged_pr_with_a_replay_is_recorded_and_still_returned() -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    sunk: list = []
    source = LivePollSource(
        config(),
        gh_runner=_merged_runner(gh),
        terminal_sink=lambda *args: sunk.append(args[:3]),
    )

    found = source.tracked_tickets(
        (TicketRef("acme/widgets", "12", "github", allow_merged=True),)
    )

    assert [ticket.ticket_id for ticket in found] == ["12"]
    assert sunk == [("acme/widgets", "12", "merged")]


def test_fresh_read_of_a_merged_pr_records_nothing() -> None:
    """#42 N3: the fresh read is a caller's mid-step read; it decides."""
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    sunk: list = []
    source = LivePollSource(
        config(),
        gh_runner=_merged_runner(gh),
        terminal_sink=lambda *args: sunk.append(args),
    )

    fresh = source.fresh_ticket(TicketRef("acme/widgets", "12", "github"))

    assert fresh is not None and fresh.merged is True
    assert sunk == []


def test_linear_ticket_with_merged_pr_moves_to_merged() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))

    def transport(url, headers, body, timeout):
        del url, headers, body, timeout
        return HTTPResult(200, json.dumps({"data": {"issue": linear_issue()}}))

    sunk: list = []
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=_merged_runner(gh),
        linear_transport=transport,
        terminal_sink=lambda *args: sunk.append(args),
    )

    found = source.tracked_tickets((TicketRef("acme/widgets", "AUT-42", "linear"),))

    assert found == ()
    assert sunk == [("acme/widgets", "AUT-42", "merged", None)]
    assert not any("/comments" in part for call in gh.calls for part in call)


def test_a_discovered_linear_ticket_whose_pr_is_merged_queues_no_build(
    tmp_path: Path,
) -> None:
    """Discovery reads the ticket untracked and caches it; the tracked read
    in the same cycle reused that read and never saw the merge, so the go
    queued a build a waiter could claim before the next poll."""
    dispatcher, queue = setup(tmp_path)
    dispatcher.trusted_logins["linear"] = {"linear:user-1": {"grok-bot"}}
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        if json.loads(body)["query"].startswith("query FactoryPoll"):
            return linear_search_page([linear_issue()])
        return HTTPResult(200, json.dumps({"data": {"issue": linear_issue()}}))

    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=_merged_runner(gh),
        linear_transport=transport,
        terminal_sink=dispatcher.record_terminal,
        error_sink=dispatcher.record_poll_error,
    )

    dispatcher.poll_once(source)

    with queue.connect() as connection:
        states = [tuple(row) for row in connection.execute(
            "SELECT ticket_id, workflow_state FROM ticket_state"
        )]
        harness = [tuple(row) for row in connection.execute(
            "SELECT action, state FROM jobs WHERE recipient != 'grok'"
        )]
    assert states == [("AUT-42", "merged")]
    assert harness == []
    assert queue.claim("claude") is None


# --- #108 r1: stopped tickets on the live source's own cadence ---------------


CYCLE_SECONDS = 30


def _one_linear_ticket_per_phase() -> list[str]:
    by_phase: dict[int, str] = {}
    number = 0
    while len(by_phase) < 10:
        number += 1
        offset = _spread("acme/widgets", f"AUT-{number}") % STOPPED_POLL_SECONDS
        by_phase.setdefault(offset // CYCLE_SECONDS, f"AUT-{number}")
    return sorted(by_phase.values())


def _handed_off(queue, tracker: str, ticket_ids: list[str]) -> None:
    with queue.connect() as connection:
        for ticket_id in ticket_ids:
            connection.execute(
                "INSERT INTO tracked_tickets VALUES ('acme/widgets', ?, ?, 0)",
                (ticket_id, tracker),
            )
            connection.execute(
                "INSERT INTO ticket_state (repo, ticket_id, workflow_state) "
                "VALUES ('acme/widgets', ?, 'handed-off')",
                (ticket_id,),
            )


def _poll_every_30_seconds(dispatcher, source, now: list[float], cycles: int) -> None:
    for _ in range(cycles):
        dispatcher.poll_once(source)
        now[0] += CYCLE_SECONDS


def _stopped_linear_poll(tmp_path: Path, gh_runner, now: list[float]):
    dispatcher, queue = setup(tmp_path, now)
    identifiers = _one_linear_ticket_per_phase()
    _handed_off(queue, "linear", identifiers)
    reads: Counter[str] = Counter()

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        if request["query"].startswith("query FactoryPoll"):
            return linear_search_page([])
        identifier = request["variables"]["id"]
        reads[identifier] += 1
        issue = linear_issue(identifier=identifier)
        return HTTPResult(200, json.dumps({"data": {"issue": issue}}))

    source = LivePollSource(
        replace(config(linear=True), poll_seconds=30),
        environ={"LINEAR_KEY": "key"},
        gh_runner=gh_runner,
        linear_transport=transport,
        terminal_sink=dispatcher.record_terminal,
        error_sink=dispatcher.record_poll_error,
        sleep=lambda _: None,
    )
    return dispatcher, queue, source, identifiers, reads


def test_a_stopped_linear_ticket_is_read_within_ten_cycles_at_every_phase(
    tmp_path: Path,
) -> None:
    """At poll_seconds 30 Linear is read every other cycle. A stopped
    ticket whose turn fell on a GitHub-only cycle was never read, so its
    merge was never seen."""
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    now = [100.0]
    dispatcher, queue, source, identifiers, _ = _stopped_linear_poll(
        tmp_path, _merged_runner(gh), now
    )

    _poll_every_30_seconds(dispatcher, source, now, 11)

    with queue.connect() as connection:
        states = dict(
            connection.execute("SELECT ticket_id, workflow_state FROM ticket_state")
        )
    assert states == {identifier: "merged" for identifier in identifiers}


def test_a_stopped_linear_ticket_is_still_read_once_per_300_seconds(
    tmp_path: Path,
) -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    now = [100.0]
    dispatcher, _, source, identifiers, reads = _stopped_linear_poll(tmp_path, gh, now)

    _poll_every_30_seconds(dispatcher, source, now, 100)

    assert set(reads) == set(identifiers)
    assert set(reads.values()) <= {9, 10}


def test_github_discovery_does_not_read_a_tracked_pr(tmp_path: Path) -> None:
    """A tracked PR is read by tracked_tickets on its own cadence; discovery
    reading every labelled PR made a handed-off PR's 300-second budget a
    per-cycle one."""
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    _handed_off(queue, "github", ["12"])
    gh = FakeGh(FACTORY.format(tickets="github", ship="handoff"))
    gh.issues = [
        {"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/handed-off"}]}
    ]
    source = LivePollSource(
        replace(config(), poll_seconds=30),
        gh_runner=gh,
        terminal_sink=dispatcher.record_terminal,
    )

    reads = []
    for _ in range(10):
        before = len(gh.calls)
        dispatcher.poll_once(source)
        now[0] += 30
        reads.append(
            sum("/comments?" in part for call in gh.calls[before:] for part in call)
        )

    assert sum(reads) == 1


# --- #130: the merge queue reads the linked PR's facts on every tracker -------


@pytest.mark.parametrize("tracker, ticket_id", [("github", "12"), ("linear", "AUT-42")])
@pytest.mark.parametrize(
    "facts, expected",
    [
        (dict(mergeable=True, mergeable_state="clean", state="open"), (True, "clean", False)),
        (dict(mergeable=False, mergeable_state="dirty", state="open"), (False, "dirty", False)),
        (dict(mergeable=None, mergeable_state="unknown", state="closed"), (None, "unknown", True)),
    ],
)
def test_a_ticket_carries_its_pr_merge_facts(tracker, ticket_id, facts, expected) -> None:
    text = FACTORY.format(tickets=tracker, ship="handoff")
    gh = FakeGh(text.replace("skills: []", "finish_line: merge\nskills: []"))

    def runner(argv, timeout):
        if any(part == "repos/acme/widgets/pulls/12" for part in argv):
            return GhResult(0, json.dumps({"head": {"sha": gh.head}, "merged": False, **facts}), "")
        return gh(argv, timeout)

    source = LivePollSource(
        config(linear=tracker == "linear"),
        environ={"LINEAR_KEY": "test-placeholder"},
        gh_runner=runner,
        linear_transport=linear_transport_for(linear_issue()),
    )
    ticket = source.fresh_ticket(TicketRef("acme/widgets", ticket_id, tracker))

    assert ticket is not None
    assert (ticket.mergeable, ticket.mergeable_state, ticket.closed) == expected
    assert ticket.merged is False


def test_a_fresh_linear_read_of_a_merged_pr_says_merged() -> None:
    gh = FakeGh(FACTORY.format(tickets="linear", ship="handoff"))
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "test-placeholder"},
        gh_runner=_merged_runner(gh),
        linear_transport=linear_transport_for(linear_issue()),
    )
    ticket = source.fresh_ticket(TicketRef("acme/widgets", "AUT-42", "linear"))
    assert ticket is not None and ticket.merged is True and ticket.closed is False


def states_source(requests: list[dict]) -> LivePollSource:
    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        requests.append(request)
        return linear_states_page(request["variables"])

    return LivePollSource(
        config(linear=True), environ={"LINEAR_KEY": "key"}, linear_transport=transport
    )


def linear_refs(count: int) -> tuple[TicketRef, ...]:
    return tuple(
        TicketRef("acme/widgets", f"AUT-{index:04d}", "linear") for index in range(count)
    )


def test_state_reads_carry_when_linear_canceled_or_completed_the_issue() -> None:
    at = "2027-01-12T08:00:00.000Z"
    issues = {
        0: ("Canceled", "canceled", at, None),
        1: ("Done", "completed", None, at),
        2: ("In Progress", "started", at, at),
        3: ("Canceled", "canceled", "soon", None),
    }

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        variables = json.loads(body)["variables"]
        nodes = [
            {
                "identifier": f"{variables['team']}-{number:04d}",
                "state": {"name": issues[number][0], "type": issues[number][1]},
                "canceledAt": issues[number][2],
                "completedAt": issues[number][3],
            }
            for number in variables["numbers"]
        ]
        return HTTPResult(200, json.dumps({"data": {"issues": {"nodes": nodes}}}))

    source = LivePollSource(
        config(linear=True), environ={"LINEAR_KEY": "key"}, linear_transport=transport
    )
    source.begin_cycle()
    states = source.linear_states(linear_refs(4)).states
    assert {key: state.closed_at for key, state in states.items()} == {
        "AUT-0000": 1_800_000_000.0 - 3 * 86400,
        "AUT-0001": 1_800_000_000.0 - 3 * 86400,
        "AUT-0002": None,
        "AUT-0003": None,
    }


def test_state_reads_cut_by_the_budget_reach_every_ticket_in_later_cycles() -> None:
    requests: list[dict] = []
    source = states_source(requests)
    refs = linear_refs(2_100)
    read: set[str] = set()
    per_cycle = cycles = 0
    while not per_cycle or cycles < math.ceil(21 / per_cycle):
        source.begin_cycle()
        read |= set(source.linear_states(refs).states)
        source.end_cycle()
        per_cycle = per_cycle or len(requests)
        cycles += 1
    assert per_cycle < 21
    assert read == {ref.ticket_id for ref in refs}


def test_state_reads_leave_a_tracked_linear_read_available() -> None:
    source = states_source([])
    source.begin_cycle()
    source.linear_states(linear_refs(1_801))
    assert source._linear_request_available(
        LINEAR_ISSUE_QUERY, reserve=LINEAR_PROCESS_HEADROOM
    )


def test_one_factory_states_request_reads_every_open_ticket(tmp_path: Path) -> None:
    requests: list[dict] = []

    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        requests.append(request)
        if request["query"].startswith("query FactoryStates"):
            return linear_states_page(request["variables"], state="Ready for Review")
        if request["query"].startswith("query FactoryPoll"):
            return linear_search_page([])
        raise AssertionError(request["query"][:40])

    database = tmp_path / "orch.db"
    now = [5000.0]
    queue = SQLiteJobQueue(database, clock=lambda: now[0])
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {}, "linear": {"linear:user-1": {"grok-bot"}}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    states = ["capped"] * 20 + ["handed-off"] * 5 + ["handed-off-externally"] * 5
    states += ["merged", "done", "abandoned"]
    with queue.connect() as connection:
        for index, state in enumerate(states):
            identifier = f"AUT-{index:04d}"
            connection.execute(
                "INSERT INTO tracked_tickets VALUES ('acme/widgets', ?, 'linear', 0)",
                (identifier,),
            )
            connection.execute(
                """
                INSERT INTO ticket_state (repo, ticket_id, workflow_state, gate_count, stuck)
                VALUES ('acme/widgets', ?, ?, 0, 0)
                """,
                (identifier, state),
            )
            connection.execute(
                "INSERT INTO ticket_reads VALUES ('acme/widgets', ?, ?)",
                (identifier, now[0] - 10),
            )
    source = LivePollSource(
        config(linear=True),
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="linear", ship="handoff")),
        linear_transport=transport,
        error_sink=dispatcher.record_poll_error,
        deferral_sink=dispatcher.record_poll_deferral,
    )

    dispatcher.poll_once(source)

    state_requests = [
        request for request in requests if request["query"].startswith("query FactoryStates")
    ]
    assert len(state_requests) == 1
    assert state_requests[0]["variables"]["team"] == "AUT"
    assert state_requests[0]["variables"]["numbers"] == list(range(30))
    assert not any(request["query"].startswith("query FactoryIssue") for request in requests)
    with queue.connect() as connection:
        rows = connection.execute(
            "SELECT ticket_id, state_name, state_type, read_at FROM linear_states"
        ).fetchall()
        handed_off = connection.execute(
            """
            SELECT count(*) FROM ticket_state
            WHERE workflow_state = 'handed-off-externally' AND external_from_state = 'capped'
            """
        ).fetchone()[0]
    assert {row["ticket_id"] for row in rows} == {f"AUT-{index:04d}" for index in range(30)}
    assert {(row["state_name"], row["state_type"], row["read_at"]) for row in rows} == {
        ("Ready for Review", "started", 5000.0)
    }
    assert handed_off == 20


class LedgerGh(FakeGh):
    def __init__(self, wall: float) -> None:
        super().__init__(FACTORY.format(tickets="github", ship="merge"))
        self.wall = wall
        self.factory_reply: GhResult | None = None

    def __call__(self, argv, timeout):
        call = list(argv)
        if "POST" in call and "graphql" not in call:
            self.calls.append(call)
            return GhResult(0, json.dumps({"id": 5}), "")
        if any(
            part.startswith(("repos/acme/widgets/pulls?head=", "repos/acme/widgets/git/ref/"))
            for part in call
        ):
            self.calls.append(call)
            return GhResult(0, "[]", "")
        if any(part.endswith("/contents/factory.md") for part in call) and self.factory_reply:
            self.calls.append(call)
            return self.factory_reply
        return super().__call__(argv, timeout)

    def headers(self, status: str, date_age: float, **extra: object) -> str:
        lines = [f"HTTP/2.0 {status}", f"Date: {formatdate(self.wall - date_age, usegmt=True)}"]
        lines += [f"{name}: {value}" for name, value in extra.items()]
        return "\r\n".join(lines) + "\r\n\r\n"


def ledger_source(gh: LedgerGh) -> tuple[LivePollSource, CycleClock]:
    source = LivePollSource(config(), gh_runner=gh, wall=lambda: gh.wall)
    clock = CycleClock(wall=lambda: gh.wall)
    source.cycle_clock = clock
    clock.start()
    return source, clock


def test_the_end_line_counts_adapter_reads_gh_call_reads_and_writes() -> None:
    gh = LedgerGh(1_800_000_000.0)
    gh.factory_reply = GhResult(
        0,
        gh.headers(
            "200 OK",
            0,
            **{
                "ETag": '"v1"',
                "X-RateLimit-Limit": 5000,
                "X-RateLimit-Remaining": 4321,
                "X-RateLimit-Reset": int(gh.wall) + 299,
            },
        )
        + json.dumps(gh.factory),
        "",
    )
    source, clock = ledger_source(gh)

    source.github.list_comments("acme/widgets", "12")
    source._gh_call("repos/acme/widgets/contents/factory.md")
    source.github.post_issue_comment("acme/widgets", 12, "hello")
    source._gh_call("repos/acme/widgets/pulls?head=acme:factory/issue-7")
    source._gh_call("repos/acme/widgets/git/ref/heads/feature/login")
    source.github.pull_states("acme/widgets", ["12"])

    line = clock.end()
    assert line.endswith(
        "; github core 5 [GET contents/factory.md 1, GET git/ref/heads/<value> 1, "
        "GET issues/<value>/comments 1, GET pulls 1, POST issues/<value>/comments 1], "
        "free 0 (304 0, cached 0), graphql 1; core 4321/5000 resets 5m"
    )
    for value in ("acme", "factory/issue-7", "feature", "login"):
        assert value not in line


@pytest.mark.parametrize(("date_age", "kind"), [(0, "304 1, cached 0"), (600, "304 0, cached 1")])
def test_a_not_modified_read_counts_as_free(date_age: float, kind: str) -> None:
    gh = LedgerGh(1_800_000_000.0)
    gh.factory_reply = GhResult(
        0, gh.headers("200 OK", 0, ETag='"v1"') + json.dumps(gh.factory), ""
    )
    source, clock = ledger_source(gh)
    source._gh_call("repos/acme/widgets/contents/factory.md")
    clock.end()

    clock.start()
    gh.factory_reply = GhResult(1, gh.headers("304 Not Modified", date_age), "gh: HTTP 304\n")
    source._gh_call("repos/acme/widgets/contents/factory.md")

    assert clock.end().endswith(f"; github core 0, free 1 ({kind}), graphql 0")


def comment_rows(count: int) -> list[dict[str, object]]:
    return [
        {"id": i, "user": {"login": "tester"}, "body": "", "created_at": "2026-11-20T00:00:00Z"}
        for i in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    ("returncode", "stderr"), [(0, ""), (1, "gh: Server Error (HTTP 502)\n")]
)
def test_each_page_of_a_paginated_read_counts_as_one_billed_call(
    returncode: int, stderr: str
) -> None:
    rows = comment_rows(101)
    pages = json.dumps(rows[:100]) + ("\n" + json.dumps(rows[100:]) if returncode == 0 else "")

    def runner(argv, timeout):
        assert "--paginate" in argv
        return GhResult(returncode, pages, stderr)

    source = LivePollSource(config(), gh_runner=runner, wall=lambda: 1_800_000_000.0)
    source.cycle_clock = clock = CycleClock(wall=lambda: 1_800_000_000.0)
    clock.start()
    if returncode == 0:
        assert len(source.github.list_comments("acme/widgets", "12")) == 101
        assert len(source._gh_pages("repos/acme/widgets/pulls/12/files?per_page=100")) == 2
    else:
        with pytest.raises(GitHubAdapterError):
            source.github.list_comments("acme/widgets", "12")
        with pytest.raises(PollSourceError):
            source._gh_pages("repos/acme/widgets/pulls/12/files?per_page=100")

    assert clock.end().endswith(
        "; github core 4 [GET issues/<value>/comments 2, GET pulls/<value>/files 2], "
        "free 0 (304 0, cached 0), graphql 0"
    )


@pytest.mark.parametrize(
    ("read", "route", "pages"),
    [
        (lambda source: source.github.list_runners("acme/widgets"), "GET actions/runners", 1),
        (
            lambda source: source._gh_pages("repos/acme/widgets/pulls/12/files?per_page=100"),
            "GET pulls/<value>/files",
            2,
        ),
    ],
)
def test_a_read_gh_replays_from_its_cache_counts_as_cached(read, route, pages) -> None:
    now = [1_800_000_000.0]
    body = "\n".join(['{"runners":[]}'] * pages)

    def runner(argv, timeout):
        assert "--cache" in argv and "--include" not in argv
        return GhResult(0, body, "")

    source = LivePollSource(config(), gh_runner=runner, wall=lambda: now[0])
    source.cycle_clock = clock = CycleClock(wall=lambda: now[0])
    lines = []
    for wait in (0, 30, 31):
        now[0] += wait
        clock.start()
        read(source)
        lines.append(clock.end())

    billed = f"; github core {pages} [{route} {pages}], free 0 (304 0, cached 0), graphql 0"
    assert lines[0].endswith(billed)
    assert lines[1].endswith(f"; github core 0, free {pages} (304 0, cached {pages}), graphql 0")
    assert lines[2].endswith(billed)


def test_a_restarted_poller_counts_a_read_gh_still_caches_as_cached(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\n"
        "grok:\n  webhook_url_env: U\n  webhook_key_env: K\n"
        "trusted_logins:\n  grok-login: [grok-bot]\n"
        "projects:\n  - repo: acme/widgets\n    factory_ref: main\n",
        encoding="utf-8",
    )
    cli._runtime(database, orch, create=True)
    now = [1_800_000_000.0]
    gh_cache: dict[tuple[str, ...], float] = {}
    requests: list[list[str]] = []

    def gh(argv, timeout):
        assert "--cache" in argv
        if gh_cache.get(tuple(argv), 0.0) <= now[0]:
            requests.append(list(argv))
            gh_cache[tuple(argv)] = now[0] + 60
        return GhResult(0, '{"runners":[]}', "")

    def restarted_cycle() -> str:
        _, source = cli._runtime(database, orch, create=False)
        source.gh_runner = gh
        source.wall = lambda: now[0]
        source.cycle_clock = clock = CycleClock(wall=lambda: now[0])
        clock.start()
        source.github.list_runners("acme/widgets")
        return clock.end()

    billed = "; github core 1 [GET actions/runners 1], free 0 (304 0, cached 0), graphql 0"
    assert restarted_cycle().endswith(billed)
    now[0] += 30
    assert restarted_cycle().endswith("; github core 0, free 1 (304 0, cached 1), graphql 0")
    assert len(requests) == 1
    now[0] += 31
    assert restarted_cycle().endswith(billed)
    assert len(requests) == 2


TOKEN_SENTINEL = "ghs_poller_reads_app_sentinel"


def _reads_app_world(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    token_root = tmp_path / "app-tokens"
    (token_root / "claude-bot").mkdir(parents=True)
    (token_root / "claude-bot" / "acme.json").write_text(
        json.dumps(
            {
                "token": TOKEN_SENTINEL,
                "issued_at": "2026-11-20T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    environ = {"PATH": str(bin_dir), "ORCH_APP_TOKEN_ROOT": str(token_root)}
    return environ, token_root, fake_gh


def _reads_and_writes(source_config: OrchConfig, environ: dict[str, str]):
    payload = json.dumps(
        {
            "encoding": "base64",
            "content": base64.b64encode(
                FACTORY.format(tickets="github", ship="merge").encode()
            ).decode(),
        }
    )
    calls: list[list[str]] = []
    warnings: list[str] = []

    def runner(argv, timeout):
        del timeout
        call = list(argv)
        calls.append(call)
        if "rate_limit" in call:
            return GhResult(0, json.dumps({"resources": {"core": {}}}), "")
        if any("/contents/factory.md" in part for part in call):
            return GhResult(0, f'HTTP/2.0 200 OK\nETag: "f1"\n\n{payload}', "")
        if any(part.endswith("/labels?per_page=100") for part in call):
            return GhResult(0, json.dumps([{"name": "orch:state/intake"}]), "")
        if any("/issues/9/" in part for part in call):
            return GhResult(
                1,
                "HTTP/2.0 403 Forbidden\r\nX-RateLimit-Remaining: 0\r\n\r\n",
                "gh: API rate limit exceeded (HTTP 403)",
            )
        return GhResult(0, "[]", "")

    source = LivePollSource(
        source_config,
        environ=environ,
        gh_runner=runner,
        clock=lambda: 1_000.0,
        wall=lambda: 1_800_000_000.0,
        sleep=lambda seconds: None,
    )
    source.warning_sink = warnings.append
    source.cycle_clock = CycleClock(wall=lambda: 1_800_000_000.0, monotonic=lambda: 1_000.0)
    source.cycle_clock.start()
    with source.cycle_clock.phase("process"):
        source.begin_cycle()
        source._factory(source.config.projects[0])
        source.github.list_comments("acme/widgets", "7")
        source.review_actions_by_repo()["acme/widgets"].set_state(
            pr(repo="acme/widgets", ticket_id="7"), "refused"
        )
        with pytest.raises((GitHubAdapterError, PollSourceError)):
            source.github.list_comments("acme/widgets", "9")
    line = source.cycle_clock.end()
    return source, calls, warnings, line


def _is_write(call: list[str]) -> bool:
    return any("/labels" in part for part in call)


def _writes(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if _is_write(call)]


def _reads(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if not _is_write(call)]


def test_reads_argv_unchanged_without_poller_reads_app(tmp_path: Path) -> None:
    environ, _, _ = _reads_app_world(tmp_path)
    source, calls, _, _ = _reads_and_writes(config(), environ)

    reads = _reads(calls)
    assert any("rate_limit" in call for call in reads)
    assert any(any("/contents/factory.md" in part for part in call) for call in reads)
    assert any(any("/comments" in part for part in call) for call in reads)
    for call in calls:
        assert tuple(call[: len(source.gh) + 1]) == (*source.gh, "api"), call


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "api", "repos/acme/widgets/pulls/99"],
        ["env", "-u", "GH_TOKEN", "gh", "api", "repos/acme/widgets/issues/7/comments"],
    ],
)
def test_reads_hand_the_runner_the_caller_argv_without_poller_reads_app(
    tmp_path: Path, argv: list[str]
) -> None:
    environ, _, _ = _reads_app_world(tmp_path)
    seen: list[list[str]] = []

    def runner(call, timeout):
        del timeout
        seen.append(list(call))
        return GhResult(0, "[]", "")

    source = LivePollSource(config(), environ=environ, gh_runner=runner)
    source._run_gh_read(list(argv))

    assert seen == [argv]


def test_poller_reads_app_routes_reads_through_the_helper_and_not_writes(
    tmp_path: Path,
) -> None:
    environ, token_root, fake_gh = _reads_app_world(tmp_path)
    plain, plain_calls, _, plain_line = _reads_and_writes(config(), environ)
    source, calls, warnings, line = _reads_and_writes(
        replace(config(), poller_reads_app="claude-bot"), environ
    )

    helper = (
        *source.gh[:-1],
        sys.executable,
        "-I",
        str(Path(github_app_token.__file__).resolve()),
        "gh",
        "--dir",
        str(token_root / "claude-bot"),
        "--gh",
        str(fake_gh),
        "--",
        "api",
    )
    probes = [call for call in calls if "rate_limit" in call]
    assert [call[len(source.gh) - 1] for call in probes] == ["GH_REPO=acme/acme"]
    reads = [[part for part in call if part != "GH_REPO=acme/acme"] for call in _reads(calls)]
    assert len(reads) == len(_reads(plain_calls))
    for call, plain_call in zip(reads, _reads(plain_calls)):
        assert tuple(call[: len(helper)]) == helper, call
        assert call[len(helper) :] == plain_call[len(plain.gh) + 1 :]
    writes = _writes(calls)
    assert writes and writes == _writes(plain_calls)
    for call in writes:
        assert tuple(call[: len(source.gh) + 1]) == (*source.gh, "api"), call
        assert not any("github_app_token" in part for part in call)
    assert line == plain_line
    assert not any(TOKEN_SENTINEL in part for call in calls for part in call)
    assert not any(TOKEN_SENTINEL in warning for warning in warnings)


def test_poller_reads_app_without_gh_on_path_fails_at_start(tmp_path: Path) -> None:
    environ = {"PATH": str(tmp_path), "ORCH_APP_TOKEN_ROOT": str(tmp_path)}
    with pytest.raises(PollSourceError, match="poller_reads_app"):
        LivePollSource(replace(config(), poller_reads_app="claude-bot"), environ=environ)


WALL = 1_800_000_000.0


def _quota_reply(remaining: int) -> GhResult:
    return GhResult(
        0,
        "HTTP/2.0 200 OK\nX-RateLimit-Limit: 5000\n"
        f"X-RateLimit-Remaining: {remaining}\nX-RateLimit-Reset: {int(WALL) + 3500}\n\n[]",
        "",
    )


def _app_reads_source(environ: dict[str, str], runner) -> LivePollSource:
    return LivePollSource(
        replace(config(), poller_reads_app="claude-bot"),
        environ=environ,
        gh_runner=runner,
        clock=lambda: 1_000.0,
        wall=lambda: WALL,
        sleep=lambda seconds: None,
    )


def _endpoint(argv: list[str]) -> str:
    return next(part for part in argv if part.startswith("repos/"))


def test_poller_reads_app_writes_do_not_spend_the_installation_quota(tmp_path: Path) -> None:
    environ, _, _ = _reads_app_world(tmp_path)
    calls: list[str] = []

    def runner(argv, timeout):
        app = "--" in argv
        calls.append("app" if app else "personal")
        return _quota_reply(RATE_PAUSE_REMAINING + 1) if app else GhResult(0, "[]", "")

    source = _app_reads_source(environ, runner)
    source._gh_call("repos/acme/r/issues")
    source.github._call("-X", "POST", "repos/acme/r/issues/12/comments", "-f", "body=x")
    source._gh_call("repos/acme/r/issues")

    assert calls == ["app", "personal", "app"]


def test_poller_reads_app_keeps_a_quota_window_per_installation(tmp_path: Path) -> None:
    environ, _, _ = _reads_app_world(tmp_path)
    reached: list[str] = []

    def runner(argv, timeout):
        endpoint = _endpoint(argv)
        reached.append(endpoint)
        return _quota_reply(10 if endpoint.startswith("repos/acme/") else 4000)

    assert 10 <= RATE_PAUSE_REMAINING
    source = _app_reads_source(environ, runner)
    source._gh_call("repos/acme/r/issues")
    source._gh_call("repos/other/r/issues")
    with pytest.raises(QuotaDeferred):
        source._gh_call("repos/acme/r/pulls")

    assert reached == ["repos/acme/r/issues", "repos/other/r/issues"]
    assert source.pacing(60.0, 1, 1.0).reason == "pause"


def _helper_token(argv: list[str], directory: Path) -> str | None:
    head = argv[: argv.index("--")]
    assigned = dict(part.split("=", 1) for part in head if re.fullmatch(r"[A-Z_]+=.*", part))
    args = argv[argv.index("--") + 1 :]
    return github_app_token.gh_env(directory, args, assigned, datetime.now(timezone.utc)).get(
        "GH_TOKEN"
    )


def test_poller_reads_app_probes_and_backs_off_the_rate_limited_installation(
    tmp_path: Path,
) -> None:
    environ, token_root, _ = _reads_app_world(tmp_path)
    directory = token_root / "claude-bot"
    (directory / "other.json").write_text(
        json.dumps(
            {
                "token": f"{TOKEN_SENTINEL}_other",
                "issued_at": "2026-11-20T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    probes: list[list[str]] = []
    reached: list[str] = []

    def runner(argv, timeout):
        call = list(argv)
        if "rate_limit" in call:
            probes.append(call)
            core = {"limit": 5000, "remaining": 0, "reset": int(WALL) + 600}
            return GhResult(0, json.dumps({"resources": {"core": core}}), "")
        endpoint = _endpoint(call)
        reached.append(endpoint)
        if endpoint.startswith("repos/other/"):
            return GhResult(
                1,
                "HTTP/2.0 403 Forbidden\r\nX-RateLimit-Remaining: 0\r\n\r\n",
                "gh: API rate limit exceeded (HTTP 403)",
            )
        return _quota_reply(4000)

    source = _app_reads_source(environ, runner)
    with pytest.raises(PollSourceError):
        source._gh_call("repos/other/r/issues")

    assert len(probes) == 1
    assert _helper_token(probes[0], directory) == f"{TOKEN_SENTINEL}_other"
    with pytest.raises(QuotaDeferred):
        source._gh_call("repos/other/r/pulls")
    source._gh_call("repos/acme/r/issues")
    assert reached == ["repos/other/r/issues", "repos/acme/r/issues"]
