import base64
import json
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import LINEAR_HANDOFF_QUERY
from lib.github_actions import GhResult
from lib.grok_delivery import GrokDeliveryError, WebhookGrokDelivery, WebhookResult
from lib.orch_yml import (
    GrokConfig,
    LinearConfig,
    OrchConfig,
    Project,
)
from lib.poll_source import HTTPResult, LivePollSource, PollSourceError
from lib.queue import Job, JobRequest

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


def gh_fixture(factory: str, extra: list[object]):
    encoded = base64.b64encode(factory.encode()).decode()
    responses = [
        {"default_branch": "main"},
        {
            "encoding": "base64",
            "content": "\n".join(
                encoded[index : index + 60]
                for index in range(0, len(encoded), 60)
            ),
        },
        *extra,
    ]
    calls: list[list[str]] = []

    def run(argv, timeout):
        del timeout
        calls.append(list(argv))
        return GhResult(0, json.dumps(responses.pop(0)), "")

    return run, calls


def config(*, linear: bool = False) -> OrchConfig:
    return OrchConfig(
        poll_seconds=60,
        projects=(
            Project(
                "acme/widgets",
                linear_project="project-1" if linear else None,
            ),
        ),
        linear=LinearConfig("LINEAR_KEY") if linear else None,
        grok=GrokConfig("FACTORY_WEBHOOK_URL", "FACTORY_WEBHOOK_KEY"),
        trusted_logins={"grok-login": "grok-bot"},
    )


def test_github_candidate_parsing_uses_gh_without_network() -> None:
    head = "a" * 40
    comment = {
        "id": 7,
        "user": {"login": "grok-login"},
        "body": "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator risk=low -->\n",
        "created_at": "2026-11-11T12:00:00Z",
        "html_url": "https://github.test/comment/7",
    }
    runner, calls = gh_fixture(
        FACTORY.format(tickets="github", ship="merge"),
        [
            [{"number": 12, "pull_request": {}, "labels": [{"name": "orch:state/intake"}]}],
            {"head": {"sha": head}},
            [comment],
            {"total_count": 1, "check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]},
            {"total_count": 0, "state": "pending"},
        ],
    )
    source = LivePollSource(config(), gh_runner=runner)

    tickets = source.github_candidates("orch:state/")

    assert len(tickets) == 1
    assert tickets[0].ticket_id == "12"
    assert tickets[0].current_head == head
    assert tickets[0].risk == "low"
    assert tickets[0].ci[head].status == "success"
    assert all("gh" in call for call in calls)


def test_linear_candidate_parsing_uses_handoff_query() -> None:
    runner, _calls = gh_fixture(
        FACTORY.format(tickets="linear", ship="handoff"),
        [],
    )
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
                                    "project": {"id": "project-1", "name": "Aut0"},
                                    "comments": {
                                        "nodes": [
                                            {
                                                "id": "comment-id",
                                                "body": "<!-- orch:v1 kind=go from=grok-bot role=orchestrator state=building risk=high -->\n",
                                                "createdAt": "2026-11-11T12:00:00Z",
                                                "user": {"name": "grok-login"},
                                            }
                                        ]
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
        gh_runner=runner,
        linear_transport=transport,
    )

    tickets = source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert tickets[0].ticket_id == "AUT-42"
    assert tickets[0].comments[0].author == "grok-login"
    assert requests[0]["variables"]["term"] == LINEAR_HANDOFF_QUERY


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


def test_poll_once_runs_one_cycle(monkeypatch, tmp_path: Path) -> None:
    class FakeDispatcher:
        def __init__(self):
            self.polls = 0

        def poll_once(self, source):
            assert source is fake_source
            self.polls += 1

    fake_source = object()
    dispatcher = FakeDispatcher()
    monkeypatch.setattr(cli, "_runtime", lambda db, orch: (dispatcher, fake_source))

    result = cli.main(["poll", "--db", str(tmp_path / "orch.db"), "--once"])

    assert result == 0
    assert dispatcher.polls == 1
