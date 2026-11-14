"""Linear webhook listener (issue #55): verify, filter, mark dirty, nothing else."""

import hashlib
import hmac
import http.client
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from lib import cli
from lib.linear_webhook import (
    LINEAR_SOURCE_IPS,
    MAX_BODY_BYTES,
    TIMESTAMP_TOLERANCE_SECONDS,
    LinearDirtyQueue,
    WebhookApp,
    WebhookSecretError,
    ensure_tables,
    make_server,
    read_secret,
)
from lib.queue import SQLiteJobQueue

SECRET = "fake-linear-signing-secret-0123456789abcdef"
NOW = 1_800_000_000.0
PATH = "/linear-webhook"
LINEAR_IP = "35.231.147.226"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def issue_event(
    *,
    project: str | None = "project-1",
    identifier: str = "AUT-42",
    issue_id: str = "issue-uuid-42",
    action: str = "update",
    timestamp_ms: int | None = None,
    updated_from: dict | None = None,
) -> dict:
    event = {
        "action": action,
        "type": "Issue",
        "createdAt": "2027-01-15T08:00:00.000Z",
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "projectId": project,
            "teamId": "team-1",
            "title": "PHI title that must never be logged",
            "description": "PHI description that must never be logged",
        },
        "url": f"https://linear.app/aut0/issue/{identifier}",
        "webhookTimestamp": int(NOW * 1000) if timestamp_ms is None else timestamp_ms,
        "webhookId": "webhook-1",
        "organizationId": "org-1",
    }
    if updated_from is not None:
        event["updatedFrom"] = updated_from
    return event


def comment_event(
    *,
    body: str = "PHI comment body that must never be logged",
    identifier: str | None = "AUT-42",
    issue_id: str | None = "issue-uuid-42",
) -> dict:
    data: dict = {
        "id": "comment-1",
        "body": body,
        "userId": "user-1",
        "createdAt": "2027-01-15T08:00:00.000Z",
        "updatedAt": "2027-01-15T08:00:00.000Z",
    }
    if issue_id is not None:
        data["issueId"] = issue_id
        data["issue"] = {
            "id": issue_id,
            "identifier": identifier,
            "teamId": "team-1",
            "title": "PHI title",
            "url": "https://linear.app/aut0/issue/x",
        }
    return {
        "action": "create",
        "type": "Comment",
        "createdAt": "2027-01-15T08:00:00.000Z",
        "data": data,
        "url": "https://linear.app/aut0/issue/x#comment-1",
        "webhookTimestamp": int(NOW * 1000),
        "webhookId": "webhook-1",
        "organizationId": "org-1",
    }


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "orch.db"
    SQLiteJobQueue(path, clock=lambda: NOW)
    return path


@pytest.fixture
def logs() -> list[str]:
    return []


@pytest.fixture
def app(database: Path, logs: list[str]) -> WebhookApp:
    return WebhookApp(
        secret=SECRET.encode(),
        project_ids=frozenset({"project-1"}),
        db_path=database,
        clock=lambda: NOW,
        log=logs.append,
    )


def post(
    app: WebhookApp,
    event: object,
    *,
    signature: str | None | bool = True,
    path: str = PATH,
    method: str = "POST",
    client_ip: str = "127.0.0.1",
    headers: dict | None = None,
    raw: bytes | None = None,
):
    body = raw if raw is not None else json.dumps(event).encode()
    sent = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if signature is True:
        sent["Linear-Signature"] = sign(body)
    elif isinstance(signature, str):
        sent["Linear-Signature"] = signature
    sent.update(headers or {})
    return app.handle(method, path, sent, body, client_ip)


def dirty_rows(database: Path) -> list[tuple]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT issue_id, identifier, event FROM linear_dirty ORDER BY issue_id"
        ).fetchall()


def test_signed_issue_event_in_a_configured_project_marks_it_dirty(
    app: WebhookApp,
    database: Path,
    logs: list[str],
) -> None:
    result = post(app, issue_event())

    assert (result.status, result.outcome) == (200, "accepted")
    assert dirty_rows(database) == [("issue-uuid-42", "AUT-42", "Issue.update")]
    with sqlite3.connect(database) as connection:
        received = connection.execute(
            "SELECT received_at FROM linear_dirty"
        ).fetchone()[0]
        health = connection.execute(
            "SELECT last_received_at, last_event FROM linear_webhook_health"
        ).fetchone()
    assert received == NOW
    assert health == (NOW, "Issue.update")
    assert logs == ["linear-webhook: accepted Issue.update AUT-42"]


def test_listener_only_marks_dirty_and_runs_no_handoff_logic(
    app: WebhookApp,
    database: Path,
) -> None:
    with sqlite3.connect(database) as connection:
        before = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("jobs",)
        }

    post(app, issue_event())
    post(app, comment_event(body="<!-- orch:v1 kind=go from=grok-bot -->"))

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        after = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("jobs",)
        }
    assert before == after
    assert "tracked_tickets" not in tables
    assert "handled_comments" not in tables


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "not-hex",
        sign(b"some other body"),
        sign(json.dumps(issue_event()).encode(), secret="wrong-secret"),
        sign(json.dumps(issue_event()).encode()).upper() + "00",
    ],
    ids=["unsigned", "empty", "not-hex", "other-body", "wrong-secret", "padded"],
)
def test_unsigned_or_badly_signed_bodies_are_rejected_and_write_nothing(
    app: WebhookApp,
    database: Path,
    logs: list[str],
    signature: str | None,
) -> None:
    result = post(app, issue_event(), signature=signature)

    assert (result.status, result.outcome) == (401, "rejected")
    assert dirty_rows(database) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM linear_webhook_health WHERE last_received_at IS NOT NULL"
        ).fetchone()[0] == 0
    assert logs == ["linear-webhook: rejected signature"]


def test_signature_compare_is_constant_time(monkeypatch, app: WebhookApp) -> None:
    calls: list[tuple] = []
    real = hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr("lib.linear_webhook.hmac.compare_digest", spy)

    assert post(app, issue_event()).status == 200
    assert post(app, issue_event(), signature="0" * 64).status == 401
    assert len(calls) == 2


@pytest.mark.parametrize(
    "offset_seconds",
    [-(TIMESTAMP_TOLERANCE_SECONDS + 1), TIMESTAMP_TOLERANCE_SECONDS + 1, -3600],
)
def test_stale_or_future_webhook_timestamp_is_rejected(
    app: WebhookApp,
    database: Path,
    logs: list[str],
    offset_seconds: int,
) -> None:
    event = issue_event(timestamp_ms=int((NOW + offset_seconds) * 1000))

    result = post(app, event)

    assert (result.status, result.outcome) == (401, "rejected")
    assert dirty_rows(database) == []
    assert logs == ["linear-webhook: rejected stale timestamp"]


@pytest.mark.parametrize("timestamp", [None, "1800000000000", 1.5e12, True])
def test_missing_or_malformed_webhook_timestamp_is_rejected(
    app: WebhookApp,
    database: Path,
    timestamp: object,
) -> None:
    event = issue_event()
    if timestamp is None:
        del event["webhookTimestamp"]
    else:
        event["webhookTimestamp"] = timestamp

    assert post(app, event).status == 401
    assert dirty_rows(database) == []


def test_timestamp_within_the_window_is_accepted(app: WebhookApp) -> None:
    event = issue_event(
        timestamp_ms=int((NOW - TIMESTAMP_TOLERANCE_SECONDS + 1) * 1000)
    )

    assert post(app, event).status == 200


def test_issue_in_another_project_gets_200_and_is_ignored(
    app: WebhookApp,
    database: Path,
    logs: list[str],
) -> None:
    for project in ("project-2", None):
        result = post(app, issue_event(project=project))
        assert (result.status, result.outcome) == (200, "ignored")
    assert dirty_rows(database) == []
    assert logs == [
        "linear-webhook: ignored Issue.update AUT-42 (not a configured project)"
    ] * 2


def test_issue_moved_out_of_a_configured_project_is_marked_dirty(
    app: WebhookApp,
    database: Path,
) -> None:
    event = issue_event(project="project-2", updated_from={"projectId": "project-1"})

    assert post(app, event).outcome == "accepted"
    assert dirty_rows(database) == [("issue-uuid-42", "AUT-42", "Issue.update")]


@pytest.mark.parametrize(
    "kind",
    ["Project", "Cycle", "IssueLabel", "Reaction", "Attachment", "", None],
)
def test_other_event_types_get_200_and_are_ignored(
    app: WebhookApp,
    database: Path,
    kind: object,
) -> None:
    event = issue_event()
    event["type"] = kind

    result = post(app, event)

    assert (result.status, result.outcome) == (200, "ignored")
    assert dirty_rows(database) == []


def test_comment_on_a_tracked_issue_is_marked_dirty(
    app: WebhookApp,
    database: Path,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE tracked_tickets (
                repo TEXT NOT NULL, ticket_id TEXT NOT NULL,
                tracker TEXT NOT NULL, tracked_at REAL NOT NULL,
                PRIMARY KEY (repo, ticket_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO tracked_tickets VALUES ('aut0-code/aut0', 'AUT-42', 'linear', 0)"
        )

    result = post(app, comment_event())

    assert result.outcome == "accepted"
    assert dirty_rows(database) == [("issue-uuid-42", "AUT-42", "Comment.create")]


HANDOFF_BODY = "<!-- orch:v1 kind=brief from=grok-bot role=orchestrator -->\nbrief"


def _clear_dirty(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM linear_dirty")


def test_handoff_comment_on_an_untracked_issue_in_a_configured_project_is_marked_dirty(
    app: WebhookApp,
    database: Path,
) -> None:
    # A signed Issue event placed the issue in project-1, so the listener
    # knows it is in scope even though the factory does not track it yet.
    post(app, issue_event(action="create"))
    _clear_dirty(database)

    assert post(app, comment_event(body=HANDOFF_BODY)).outcome == "accepted"
    assert dirty_rows(database) == [("issue-uuid-42", "AUT-42", "Comment.create")]


# F1.3: an orch:v1 marker is comment text anyone can type; it does not put
# the issue in a configured project. Each case is authenticated and fresh.
@pytest.mark.parametrize(
    "before",
    [
        [],
        [issue_event(project="project-2", action="create")],
        [
            issue_event(action="create"),
            issue_event(project="project-2", updated_from={"projectId": "project-1"}),
        ],
        [issue_event(action="create"), issue_event(action="remove")],
    ],
    ids=["never-seen", "other-project", "moved-out", "removed"],
)
def test_handoff_comment_outside_the_configured_projects_is_ignored(
    app: WebhookApp,
    database: Path,
    logs: list[str],
    before: list[dict],
) -> None:
    for event in before:
        post(app, event)
    _clear_dirty(database)
    logs.clear()

    result = post(app, comment_event(body=HANDOFF_BODY))

    assert (result.status, result.outcome) == (200, "ignored")
    assert dirty_rows(database) == []
    assert logs == [
        "linear-webhook: ignored Comment.create AUT-42 (not a configured project)"
    ]


def test_plain_comment_on_an_untracked_issue_is_ignored(
    app: WebhookApp,
    database: Path,
    logs: list[str],
) -> None:
    result = post(app, comment_event())

    assert (result.status, result.outcome) == (200, "ignored")
    assert dirty_rows(database) == []
    assert logs == [
        "linear-webhook: ignored Comment.create AUT-42 (untracked issue)"
    ]


def test_comment_not_on_an_issue_is_ignored(app: WebhookApp, database: Path) -> None:
    event = comment_event(
        body="<!-- orch:v1 kind=go -->",
        issue_id=None,
        identifier=None,
    )

    assert post(app, event).outcome == "ignored"
    assert dirty_rows(database) == []


def test_repeated_events_keep_one_row_per_issue_with_the_newest_receipt(
    database: Path,
) -> None:
    now = [NOW]
    app = WebhookApp(
        secret=SECRET.encode(),
        project_ids=frozenset({"project-1"}),
        db_path=database,
        clock=lambda: now[0],
        log=lambda _line: None,
    )
    post(app, issue_event())
    now[0] = NOW + 5
    event = issue_event(timestamp_ms=int(now[0] * 1000), action="create")
    post(app, event)

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT issue_id, event, received_at FROM linear_dirty"
        ).fetchall()
    assert rows == [("issue-uuid-42", "Issue.create", NOW + 5)]


def test_wrong_method_and_path_are_refused(app: WebhookApp, database: Path) -> None:
    assert post(app, issue_event(), method="GET").status == 405
    assert post(app, issue_event(), method="PUT").status == 405
    assert post(app, issue_event(), path="/other").status == 404
    assert post(app, issue_event(), path=PATH + "/x").status == 404
    assert dirty_rows(database) == []


def test_oversized_body_is_rejected_before_it_is_read(app: WebhookApp) -> None:
    body = b"{" + b" " * MAX_BODY_BYTES + b"}"

    result = app.handle(
        "POST",
        PATH,
        {"Content-Length": str(len(body)), "Linear-Signature": sign(body)},
        body,
        "127.0.0.1",
    )

    assert (result.status, result.outcome) == (413, "rejected")


class NeverRead:
    """A request body reader that fails the test if it is ever called."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, size: int) -> bytes:
        self.calls.append(size)
        raise AssertionError("the body must not be read")


# F1.5: production hands WebhookApp a reader (rfile.read), not bytes. These
# observe the read itself, so moving it ahead of a check fails them.
@pytest.mark.parametrize(
    ("headers", "client_ip", "status"),
    [
        ({"Content-Length": str(MAX_BODY_BYTES + 1)}, "127.0.0.1", 413),
        ({"Content-Length": str(10 * MAX_BODY_BYTES)}, "127.0.0.1", 413),
        ({"Content-Length": "-1"}, "127.0.0.1", 411),
        ({}, "127.0.0.1", 411),
        ({"Content-Length": "10"}, "203.0.113.9", 403),
    ],
    ids=["cap-plus-one", "far-over-cap", "negative", "missing", "bad-source"],
)
def test_refused_requests_never_read_the_body(
    app: WebhookApp,
    database: Path,
    headers: dict,
    client_ip: str,
    status: int,
) -> None:
    reader = NeverRead()
    headers = {"Linear-Signature": "0" * 64, **headers}

    result = app.handle("POST", PATH, headers, reader, client_ip)

    assert (result.status, result.outcome) == (status, "rejected")
    assert reader.calls == []
    assert dirty_rows(database) == []


def test_body_within_the_cap_is_read_exactly_once_at_its_declared_length(
    app: WebhookApp,
    database: Path,
) -> None:
    body = json.dumps(issue_event()).encode()
    calls: list[int] = []

    def reader(size: int) -> bytes:
        calls.append(size)
        return body

    result = app.handle(
        "POST",
        PATH,
        {"Content-Length": str(len(body)), "Linear-Signature": sign(body)},
        reader,
        "127.0.0.1",
    )

    assert result.outcome == "accepted"
    assert calls == [len(body)]


@pytest.mark.parametrize("length", [None, "abc", "-1"])
def test_missing_or_invalid_content_length_is_rejected(
    app: WebhookApp,
    length: str | None,
) -> None:
    body = json.dumps(issue_event()).encode()
    headers = {"Linear-Signature": sign(body)}
    if length is not None:
        headers["Content-Length"] = length

    assert app.handle("POST", PATH, headers, body, "127.0.0.1").status == 411


# Sweep of F1.5: "HMAC before JSON parsing" is an ordering claim too. An
# unsigned body that is not JSON must fail the signature (401), not the
# parse (400), so parsing ahead of the check fails this.
@pytest.mark.parametrize("raw", [b"not json", b"{", b"\xff\xfe"], ids=["text", "truncated", "binary"])
def test_unsigned_body_fails_the_signature_before_any_parse(
    app: WebhookApp,
    database: Path,
    logs: list[str],
    raw: bytes,
    monkeypatch,
) -> None:
    """#69 F2.2: a bad signature is refused before json.loads ever runs."""
    parsed: list[bytes] = []

    def counting(value, *args, **kwargs):
        parsed.append(value)
        return json.JSONDecoder().decode(value.decode() if isinstance(value, bytes) else value)

    monkeypatch.setattr("lib.linear_webhook.json.loads", counting)
    result = post(app, None, raw=raw, signature="0" * 64)

    assert (result.status, result.reason) == (401, "signature")
    assert logs == ["linear-webhook: rejected signature"]
    assert dirty_rows(database) == []
    assert parsed == []


def test_signed_body_that_is_not_json_is_rejected(app: WebhookApp) -> None:
    raw = b"not json"

    assert post(app, None, raw=raw).status == 400


def test_source_ip_outside_linear_allowlist_is_refused(
    app: WebhookApp,
    database: Path,
) -> None:
    assert post(app, issue_event(), client_ip="203.0.113.9").status == 403
    assert post(app, issue_event(), client_ip=LINEAR_IP).status == 200
    assert dirty_rows(database) == [("issue-uuid-42", "AUT-42", "Issue.update")]


def test_forwarded_client_from_a_local_proxy_is_checked_against_the_allowlist(
    app: WebhookApp,
) -> None:
    assert post(
        app,
        issue_event(),
        headers={"X-Forwarded-For": "203.0.113.9"},
    ).status == 403
    # The proxy appends the address it saw; a spoofed first hop is ignored.
    assert post(
        app,
        issue_event(),
        headers={"X-Forwarded-For": f"{LINEAR_IP}, 203.0.113.9"},
    ).status == 403
    assert post(
        app,
        issue_event(),
        headers={"X-Forwarded-For": f"203.0.113.9, {LINEAR_IP}"},
    ).status == 200


def test_forwarded_header_from_a_non_local_peer_is_not_trusted(
    app: WebhookApp,
) -> None:
    assert post(
        app,
        issue_event(),
        client_ip="203.0.113.9",
        headers={"X-Forwarded-For": LINEAR_IP},
    ).status == 403


def test_published_linear_source_ips_are_the_default_allowlist() -> None:
    assert LINEAR_IP in LINEAR_SOURCE_IPS
    assert len(LINEAR_SOURCE_IPS) == 15


def test_database_failure_returns_500_so_linear_retries(
    app: WebhookApp,
    database: Path,
    logs: list[str],
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE linear_dirty")
        connection.execute("CREATE VIEW linear_dirty AS SELECT 1 AS issue_id")

    result = post(app, issue_event())

    assert result.status == 500
    assert logs == ["linear-webhook: error OperationalError"]


def test_secret_and_bodies_never_reach_logs_or_results(
    app: WebhookApp,
    logs: list[str],
) -> None:
    events = [
        (issue_event(), True),
        (issue_event(), "0" * 64),
        (issue_event(project="project-2"), True),
        (comment_event(), True),
        (issue_event(timestamp_ms=0), True),
    ]
    results = [post(app, event, signature=signature) for event, signature in events]
    text = "\n".join(logs) + "\n".join(repr(result) for result in results)

    assert SECRET not in text
    assert "PHI" not in text
    assert sign(json.dumps(issue_event()).encode()) not in text


def test_read_secret_strips_one_trailing_newline(tmp_path: Path) -> None:
    secret_file = tmp_path / "linear-webhook.secret"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")
    secret_file.chmod(0o600)

    assert read_secret(secret_file) == SECRET.encode()


@pytest.mark.parametrize(
    ("content", "mode", "reason"),
    [
        (SECRET, 0o644, "readable by group or others"),
        (SECRET, 0o640, "readable by group or others"),
        ("\n", 0o600, "is empty"),
        (None, None, "cannot be read"),
    ],
)
def test_read_secret_refuses_unsafe_or_empty_files_without_echoing_them(
    tmp_path: Path,
    content: str | None,
    mode: int | None,
    reason: str,
) -> None:
    secret_file = tmp_path / "linear-webhook.secret"
    if content is not None:
        secret_file.write_text(content, encoding="utf-8")
        assert mode is not None
        secret_file.chmod(mode)

    with pytest.raises(WebhookSecretError) as raised:
        read_secret(secret_file)

    assert reason in str(raised.value)
    assert str(secret_file) in str(raised.value)
    assert SECRET not in str(raised.value)
    assert raised.value.__cause__ is None


def test_dirty_queue_ack_keeps_a_row_updated_after_it_was_read(
    database: Path,
) -> None:
    with sqlite3.connect(database) as connection:
        ensure_tables(connection)
        connection.execute(
            "INSERT INTO linear_dirty VALUES ('id-1', 'AUT-1', 'Issue.update', 10.0)"
        )
        connection.execute(
            "INSERT INTO linear_dirty VALUES ('id-2', 'AUT-2', 'Issue.update', 11.0)"
        )
    queue = LinearDirtyQueue(database)
    pending = queue.pending()
    assert [row.issue_id for row in pending] == ["id-1", "id-2"]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE linear_dirty SET received_at = 12.0 WHERE issue_id = 'id-2'"
        )

    queue.ack(pending)

    assert [row.issue_id for row in queue.pending()] == ["id-2"]


def test_dirty_queue_without_the_table_is_empty_and_creates_nothing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orch.db"
    SQLiteJobQueue(database, clock=lambda: NOW)
    queue = LinearDirtyQueue(database)

    assert queue.pending() == ()
    queue.ack(())
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'linear_dirty'"
        ).fetchone()[0] == 0


def test_http_server_answers_quickly_and_marks_dirty(
    database: Path,
    logs: list[str],
) -> None:
    app = WebhookApp(
        secret=SECRET.encode(),
        project_ids=frozenset({"project-1"}),
        db_path=database,
        log=logs.append,
    )
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        event = issue_event(timestamp_ms=None)
        import time as _time

        event["webhookTimestamp"] = int(_time.time() * 1000)
        body = json.dumps(event).encode()
        statuses = []
        for signature, method in (
            (sign(body), "POST"),
            ("0" * 64, "POST"),
            (sign(body), "GET"),
        ):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                method,
                PATH,
                body=body,
                headers={
                    "Linear-Signature": signature,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            statuses.append((response.status, response.read()))
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert [status for status, _ in statuses] == [200, 401, 405]
    assert all(SECRET.encode() not in payload for _, payload in statuses)
    assert dirty_rows(database) == [("issue-uuid-42", "AUT-42", "Issue.update")]


def _orch(tmp_path: Path, webhook: str) -> Path:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\n"
        "linear:\n  api_key_env: LINEAR_KEY\n"
        "projects:\n"
        "  - repo: acme/widgets\n"
        "    factory_ref: main\n"
        "    linear_project_id: project-1\n"
        + webhook,
        encoding="utf-8",
    )
    return path


def test_serve_refuses_when_no_webhook_is_configured(
    tmp_path: Path,
    capsys,
    database: Path,
) -> None:
    orch = _orch(tmp_path, "")

    code = cli.main(
        ["linear-webhook", "serve", "--db", str(database), "--orch", str(orch)]
    )

    assert code == 2
    assert "linear_webhook is not configured" in capsys.readouterr().err


def test_serve_refuses_a_missing_database_and_unsafe_secret_without_echo(
    tmp_path: Path,
    capsys,
    database: Path,
) -> None:
    secret_file = tmp_path / "linear-webhook.secret"
    secret_file.write_text(SECRET, encoding="utf-8")
    secret_file.chmod(0o644)
    orch = _orch(
        tmp_path,
        "linear_webhook:\n"
        "  bind: 127.0.0.1:8787\n"
        f"  secret_file: {secret_file}\n",
    )

    missing = cli.main(
        [
            "linear-webhook",
            "serve",
            "--db",
            str(tmp_path / "missing.db"),
            "--orch",
            str(orch),
        ]
    )
    missing_err = capsys.readouterr().err
    unsafe = cli.main(
        ["linear-webhook", "serve", "--db", str(database), "--orch", str(orch)]
    )
    unsafe_err = capsys.readouterr().err

    assert missing == 1
    assert "does not exist" in missing_err
    assert unsafe == 1
    assert "readable by group or others" in unsafe_err
    assert SECRET not in missing_err + unsafe_err
    assert not (tmp_path / "missing.db").exists()


def test_serve_binds_the_configured_address_and_creates_its_tables(
    tmp_path: Path,
    monkeypatch,
    capsys,
    database: Path,
) -> None:
    secret_file = tmp_path / "linear-webhook.secret"
    secret_file.write_text(SECRET, encoding="utf-8")
    secret_file.chmod(0o600)
    orch = _orch(
        tmp_path,
        "linear_webhook:\n"
        "  bind: 127.0.0.1:8787\n"
        f"  secret_file: {secret_file}\n",
    )
    seen: dict = {}

    class FakeServer:
        server_address = ("127.0.0.1", 8787)

        def serve_forever(self) -> None:
            seen["served"] = True

        def server_close(self) -> None:
            seen["closed"] = True

    def fake_make_server(app, host, port):
        seen["bind"] = (host, port)
        seen["projects"] = app.project_ids
        seen["secret_ok"] = app.secret == SECRET.encode()
        return FakeServer()

    monkeypatch.setattr("lib.linear_webhook.make_server", fake_make_server)

    code = cli.main(
        ["linear-webhook", "serve", "--db", str(database), "--orch", str(orch)]
    )
    override = cli.main(
        [
            "linear-webhook",
            "serve",
            "--db",
            str(database),
            "--orch",
            str(orch),
            "--bind",
            "127.0.0.1:9999",
        ]
    )
    output = capsys.readouterr()

    assert (code, override) == (0, 0)
    assert seen["bind"] == ("127.0.0.1", 9999)
    assert seen["projects"] == frozenset({"project-1"})
    assert seen["secret_ok"] is True
    assert seen["served"] and seen["closed"]
    assert SECRET not in output.out + output.err
    assert "linear-webhook: listening on 127.0.0.1:8787/linear-webhook" in output.out
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"linear_dirty", "linear_webhook_health"} <= tables



# --- #79, #69 F2.1, #107 g ---------------------------------------------------------------------


def _health_row(database: Path) -> tuple:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT last_received_at, last_event FROM linear_webhook_health"
        ).fetchone()


def _seen(database: Path) -> dict[str, float]:
    with sqlite3.connect(database) as connection:
        return dict(connection.execute("SELECT issue_id, received_at FROM linear_webhook_seen"))


@pytest.mark.parametrize(
    "raw_or_event",
    [b"not json", issue_event(timestamp_ms=int((NOW - 600) * 1000))],
    ids=["malformed", "stale-timestamp"],
)
def test_any_signed_delivery_counts_as_received(
    app: WebhookApp, database: Path, raw_or_event
) -> None:
    raw = raw_or_event if isinstance(raw_or_event, bytes) else None
    result = post(app, None if raw else raw_or_event, raw=raw)

    assert result.outcome == "rejected"
    assert _health_row(database) == (NOW, "-")


def test_an_unsigned_delivery_does_not_count_as_received(app: WebhookApp, database: Path) -> None:
    post(app, issue_event(), signature="0" * 64)
    assert _health_row(database) == (None, None)


def test_an_ignored_event_type_still_counts(app: WebhookApp, database: Path) -> None:
    event = issue_event()
    event["type"] = "Attachment"
    assert post(app, event).outcome == "ignored"
    assert _health_row(database) == (NOW, "Attachment.update")


def test_each_signed_fresh_delivery_records_its_issue_as_seen(
    app: WebhookApp, database: Path
) -> None:
    post(app, issue_event(project="project-2", issue_id="other-issue"))
    post(app, comment_event(issue_id="commented-issue"))
    post(app, issue_event())
    assert _seen(database) == {"other-issue": NOW, "commented-issue": NOW, "issue-uuid-42": NOW}


def test_a_handoff_comment_on_a_project_no_longer_configured_is_ignored(
    database: Path,
) -> None:
    """#69 F2.1: the stored project is checked against the current orch.yml."""
    first = WebhookApp(secret=SECRET.encode(), project_ids=frozenset({"project-1", "project-2"}),
                       db_path=database, clock=lambda: NOW, log=lambda line: None)
    assert post(first, issue_event(action="create")).outcome == "accepted"
    _clear_dirty(database)

    restarted = WebhookApp(secret=SECRET.encode(), project_ids=frozenset({"project-2"}),
                           db_path=database, clock=lambda: NOW, log=lambda line: None)
    result = post(restarted, comment_event(body=HANDOFF_BODY))

    assert (result.outcome, result.reason) == ("ignored", "not a configured project")
    assert dirty_rows(database) == []


def test_a_legacy_membership_row_without_a_project_is_not_admitted(
    database: Path, app: WebhookApp
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE linear_project_issues SET project_id = NULL")
        connection.execute(
            "INSERT INTO linear_project_issues (issue_id, seen_at) VALUES ('issue-uuid-42', ?)",
            (NOW,),
        )
    assert post(app, comment_event(body=HANDOFF_BODY)).outcome == "ignored"


def test_an_old_membership_table_gains_its_project_column(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    SQLiteJobQueue(database, clock=lambda: NOW)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE linear_project_issues (issue_id TEXT PRIMARY KEY, seen_at REAL NOT NULL)"
        )
        ensure_tables(connection)
        ensure_tables(connection)
        columns = [row[1] for row in connection.execute("PRAGMA table_info(linear_project_issues)")]
    assert columns == ["issue_id", "seen_at", "project_id"]


def test_log_lines_carry_a_utc_timestamp(capsys, monkeypatch) -> None:
    from lib import linear_webhook

    monkeypatch.setattr(linear_webhook.time, "time", lambda: NOW)
    linear_webhook.utc_log("linear-webhook: accepted Issue.update AUT-42")
    assert capsys.readouterr().out == (
        "2027-01-15T08:00:00Z linear-webhook: accepted Issue.update AUT-42\n"
    )
