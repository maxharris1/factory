import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib import cli, heal, self_deploy
from lib.dispatcher import Dispatcher
from lib.github_actions import GhResult
from lib.grok_delivery import (
    ClaudeDriverDelivery,
    GrokDeliveryError,
    WebhookGrokDelivery,
    WebhookResult,
    send_fire,
    wake_body,
    wake_delivery,
)
from lib.orch_yml import load_orch_yml
from lib.queue import Job, JobRequest, SQLiteJobQueue

FIRE_URL = "https://fire-secret.test/v1/claude_code/routines/trig_1/fire"
TOKEN = "fire-token-secret"
LOGICAL_KEY = "grok:acme/widgets:12:stuck:7"
ENVIRON = {"CLAUDE_FIRE_URL": FIRE_URL, "CLAUDE_FIRE_TOKEN": TOKEN}


def wake_job() -> Job:
    request = JobRequest(
        repo="acme/widgets",
        ticket_id="12",
        action="stuck",
        recipient="grok",
        source_comment_id="7",
        timeout_seconds=60,
        generation=2,
        attempt=2,
        head="abc123",
        detail='{"project_bot":"widgets-bot","link":"https://example.test/7"}',
    )
    return Job("job-1", "dedupe", request, "running", 1, 1, None, None)


def claude_delivery(
    transport, environ: dict[str, str] | None = None
) -> ClaudeDriverDelivery:
    return ClaudeDriverDelivery(
        url_env="CLAUDE_FIRE_URL",
        token_env="CLAUDE_FIRE_TOKEN",
        environ=ENVIRON if environ is None else environ,
        transport=transport,
    )


def test_wake_body_keeps_the_grok_bytes() -> None:
    assert wake_body(wake_job(), LOGICAL_KEY) == (
        b'{"action":"stuck","attempt":2,'
        b'"detail":"{\\"project_bot\\":\\"widgets-bot\\",'
        b'\\"link\\":\\"https://example.test/7\\"}",'
        b'"generation":2,"head":"abc123","job_id":"job-1",'
        b'"logical_key":"grok:acme/widgets:12:stuck:7","repo":"acme/widgets",'
        b'"source_comment_id":"7","target_bot":"widgets-bot","ticket_id":"12"}'
    )


@pytest.mark.parametrize(
    ("headers", "receipt"),
    [({"request-id": "req_123"}, "req_123"), ({}, LOGICAL_KEY)],
    ids=["request-id", "no-request-id"],
)
def test_claude_wake_sends_the_grok_body_as_text(
    headers: dict[str, str], receipt: str
) -> None:
    fired: list[tuple[str, str, bytes]] = []
    grok_bodies: list[bytes] = []

    def fire(url, authorization, body, timeout):
        del timeout
        fired.append((url, authorization, body))
        return WebhookResult(200, headers)

    def grok(url, authorization, body, timeout):
        del url, authorization, timeout
        grok_bodies.append(body)
        return WebhookResult(200, {})

    assert claude_delivery(fire).deliver(wake_job(), logical_key=LOGICAL_KEY) == receipt
    WebhookGrokDelivery(
        url_env="GROK_URL",
        key_env="GROK_KEY",
        environ={"GROK_URL": "https://grok.test/hook", "GROK_KEY": "k"},
        transport=grok,
    ).deliver(wake_job(), logical_key=LOGICAL_KEY)

    [(url, authorization, body)] = fired
    assert url == FIRE_URL
    assert authorization == f"Bearer {TOKEN}"
    text = json.loads(body)["text"]
    assert json.loads(text) == json.loads(grok_bodies[0])


def raising_transport(url, authorization, body, timeout):
    del body, timeout
    raise RuntimeError(authorization + url)


@pytest.mark.parametrize(
    ("transport", "detail"),
    [
        (lambda url, authorization, body, timeout: WebhookResult(500, {}), "HTTP-500"),
        (raising_transport, "RuntimeError"),
    ],
    ids=["http-500", "transport-raises"],
)
def test_claude_wake_failure_names_no_secret(transport, detail: str) -> None:
    with pytest.raises(GrokDeliveryError) as raised:
        claude_delivery(transport).deliver(wake_job(), logical_key=LOGICAL_KEY)

    assert raised.value.detail == detail
    assert raised.value.__cause__ is None
    for text in (str(raised.value), raised.value.detail):
        assert FIRE_URL not in text and "fire-secret" not in text
        assert TOKEN not in text


@pytest.mark.parametrize(
    ("environ", "detail"),
    [
        ({"CLAUDE_FIRE_TOKEN": TOKEN}, "missing-env:CLAUDE_FIRE_URL"),
        ({"CLAUDE_FIRE_URL": FIRE_URL}, "missing-env:CLAUDE_FIRE_TOKEN"),
        (
            {"CLAUDE_FIRE_URL": "http://fire-secret.test/fire", "CLAUDE_FIRE_TOKEN": TOKEN},
            "bad-env:CLAUDE_FIRE_URL",
        ),
    ],
    ids=["missing-url", "missing-token", "plain-http-url"],
)
def test_claude_wake_env_errors_name_the_variable(
    environ: dict[str, str], detail: str
) -> None:
    transport = Mock()

    with pytest.raises(GrokDeliveryError) as raised:
        claude_delivery(transport, environ).deliver(wake_job(), logical_key=LOGICAL_KEY)

    assert raised.value.detail == detail
    assert "fire-secret" not in str(raised.value)
    assert TOKEN not in str(raised.value)
    transport.assert_not_called()


class Response:
    status = 200
    headers = {"request-id": "req_fire"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def recording_opener(monkeypatch) -> list[tuple[str, dict[str, str], bytes]]:
    sent: list[tuple[str, dict[str, str], bytes]] = []

    def fake_open(request, timeout):
        del timeout
        headers = {name.lower(): value for name, value in request.header_items()}
        sent.append((request.full_url, headers, request.data))
        return Response()

    monkeypatch.setattr("lib.grok_delivery._OPENER.open", fake_open)
    return sent


def test_send_fire_sends_the_routine_headers(monkeypatch) -> None:
    sent = recording_opener(monkeypatch)

    result = send_fire(FIRE_URL, f"Bearer {TOKEN}", b'{"text":"x"}', 1)

    assert result.status == 200
    [(url, headers, body)] = sent
    assert url == FIRE_URL
    assert body == b'{"text":"x"}'
    assert headers == {
        "authorization": f"Bearer {TOKEN}",
        "anthropic-beta": "experimental-cc-routine-2026-04-01",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def test_send_fire_refuses_plain_http(monkeypatch) -> None:
    sent = recording_opener(monkeypatch)

    with pytest.raises(GrokDeliveryError) as raised:
        send_fire("http://fire-secret.test/fire", f"Bearer {TOKEN}", b"{}", 1)

    assert sent == []
    assert raised.value.detail == "ValueError"
    assert "Claude fire" in str(raised.value)
    assert "fire-secret" not in str(raised.value)


ORCH_YML = """\
poll_seconds: 30
{driver}grok:
  webhook_url_env: TEST_GROK_URL
  webhook_auth_env: TEST_GROK_AUTH
  webhook_key_env: TEST_GROK_KEY
claude:
  fire_url_env: TEST_CLAUDE_FIRE_URL
  fire_token_env: TEST_CLAUDE_FIRE_TOKEN
trusted_logins:
  grok-login: [grok-bot]
projects:
  - repo: acme/widgets
"""


def config_with(tmp_path: Path, driver: str):
    path = tmp_path / "orch.yml"
    path.write_text(ORCH_YML.format(driver=driver), encoding="utf-8")
    config, findings = load_orch_yml(path, overlay=False)
    assert findings == []
    assert config is not None
    return config


def test_claude_driver_wakes_only_the_claude_routine(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_GROK_URL", "https://grok-host.test/hook")
    monkeypatch.setenv("TEST_GROK_KEY", "grok-key")
    monkeypatch.setenv("TEST_CLAUDE_FIRE_URL", "https://claude-host.test/fire")
    monkeypatch.setenv("TEST_CLAUDE_FIRE_TOKEN", "claude-token")
    sent = recording_opener(monkeypatch)
    delivery = wake_delivery(config_with(tmp_path, "driver: claude\n"))
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {"grok-login": {"grok-bot"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=delivery,
    )
    queue.enqueue(wake_job().request, dedupe_key="grok-1")

    dispatcher.reconcile_jobs()

    assert [url for url, _, _ in sent] == ["https://claude-host.test/fire"]
    with queue.connect() as connection:
        receipts = [
            row[0]
            for row in connection.execute("SELECT handoff_comment_id FROM jobs")
        ]
    assert receipts == ["req_fire"]


def test_no_driver_key_keeps_the_grok_webhook(tmp_path: Path) -> None:
    config = config_with(tmp_path, "")

    delivery = wake_delivery(config)

    assert config.driver == "grok"
    assert isinstance(delivery, WebhookGrokDelivery)


SECRETS = {
    name: f"{name.lower()}-value"
    for name in (
        "TEST_LINEAR_KEY",
        "TEST_GROK_URL",
        "TEST_GROK_AUTH",
        "TEST_GROK_KEY",
        "TEST_CLAUDE_FIRE_URL",
        "TEST_CLAUDE_FIRE_TOKEN",
    )
}
MACHINES_YML = (
    "devices:\n  - name: box\n    local: true\n    slot_max: 1\n"
    "    harnesses: {claude: claude-bot}\n"
)


def orch_with_every_secret(tmp_path: Path, driver: str) -> Path:
    path = tmp_path / "orch.yml"
    text = ORCH_YML.format(
        driver=f"driver: {driver}\nlinear:\n  api_key_env: TEST_LINEAR_KEY\n"
    )
    path.write_text(
        text + "    factory_ref: main\n    intake:\n      label: factory:ready\n",
        encoding="utf-8",
    )
    return path


def gh_child_env(argv) -> dict[str, str]:
    prefix = list(argv[: list(argv).index("gh")])
    shown = subprocess.run(
        [*prefix, "env"],
        env={**SECRETS, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split("=", 1) for line in shown.splitlines())


@pytest.mark.parametrize("driver", ["grok", "claude"])
def test_feeder_gh_children_never_see_a_delivery_secret(
    tmp_path: Path, driver: str
) -> None:
    orch = orch_with_every_secret(tmp_path, driver)
    machines = tmp_path / "machines.yml"
    machines.write_text(MACHINES_YML, encoding="utf-8")
    database = tmp_path / "orch.db"
    cli._runtime(database, orch, create=True)
    dispatcher, source = cli._runtime(database, orch, create=False, machines_path=machines)
    children: list[dict[str, str]] = []

    def gh(argv, timeout):
        del timeout
        children.append(gh_child_env(argv))
        return GhResult(0, "[]", "")

    source.gh_runner = gh

    assert dispatcher.feeder.github.github_intake("acme/widgets", "factory:ready") == []
    assert children
    for env in children:
        assert "PATH" in env
        assert set(SECRETS) & env.keys() == set()


@pytest.mark.parametrize("driver", ["grok", "claude"])
def test_deploy_children_never_see_a_delivery_secret(tmp_path: Path, driver: str) -> None:
    webhook = heal._webhook(orch_with_every_secret(tmp_path, driver))
    environ = {**SECRETS, "PATH": "/bin", "ORCH_STATE_DIR": str(tmp_path)}
    spawned: list[dict[str, str]] = []

    git = self_deploy.git_env(environ, webhook)
    self_deploy.MergeKick(
        lambda: {"acme/widgets"},
        repo="acme/widgets",
        on_error=pytest.fail,
        environ=environ,
        secret_names=webhook.secret_names,
        spawn=lambda argv, env, log_path: spawned.append(dict(env)),
    )()

    [kick] = spawned
    for env in (git, kick):
        assert env["PATH"] == "/bin"
        assert set(SECRETS) & env.keys() == set()
