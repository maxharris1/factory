from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import LINEAR_HANDOFF_QUERY, TicketRef
from lib.intake_live import LinearIntakeClient
from lib.linear_webhook import WebhookApp
from lib.orch_yml import load_orch_yml
from lib.poll_source import HTTPResult, LivePollSource
from lib.queue import SQLiteJobQueue
from tests.test_cli import run
from tests.test_feeder import LINEAR_FACTORY_MD, FakeLinear, headers, make_feeder, rows
from tests.test_feeder import linear_issue as intake_issue
from tests.test_intake_live import PagedLinearSource, linear_node
from tests.test_linear_webhook import NOW, SECRET, dirty_rows, issue_event, post
from tests.test_live_poll import FACTORY, FakeGh, config, linear_issue, linear_search_page

BASE = """poll_seconds: 60
linear:
  api_key_env: LINEAR_KEY
trusted_logins:
  maxharris1: [grok-bot]
trusted_linear_users:
  linear:user-1: [grok-bot]
projects:
"""


def load(tmp_path: Path, projects: str):
    path = tmp_path / "orch.yml"
    path.write_text(BASE + projects, encoding="utf-8")
    return load_orch_yml(path)


def errors(findings) -> list[str]:
    return [item.message for item in findings if item.severity == "error"]


def test_a_project_can_name_two_linear_projects(tmp_path: Path) -> None:
    config, findings = load(
        tmp_path,
        "  - repo: acme/app\n"
        "    linear_project_ids: [proj-a, proj-b]\n"
        "    linear_ready_for_review: true\n"
        "    intake:\n"
        "      linear_state: In Progress\n",
    )

    assert errors(findings) == []
    assert config is not None
    project = config.projects[0]
    assert project.linear_project_ids == frozenset({"proj-a", "proj-b"})
    assert project.linear_ready_for_review is True
    assert project.intake is not None and project.intake.linear_state == "In Progress"


def test_the_single_key_is_a_one_project_set(tmp_path: Path) -> None:
    config, findings = load(
        tmp_path, "  - repo: acme/app\n    linear_project_id: proj-a\n"
    )

    assert errors(findings) == []
    assert config is not None
    assert config.projects[0].linear_project_ids == frozenset({"proj-a"})


@pytest.mark.parametrize(
    ("projects", "message"),
    [
        (
            "  - repo: acme/app\n"
            "    linear_project_id: proj-a\n"
            "    linear_project_ids: [proj-b]\n",
            "projects[0] sets both linear_project_id and linear_project_ids",
        ),
        (
            "  - repo: acme/app\n    linear_project_ids: []\n",
            "projects[0].linear_project_ids must be a non-empty list of project ids",
        ),
        (
            "  - repo: acme/app\n    linear_project_ids: proj-a\n",
            "projects[0].linear_project_ids must be a non-empty list of project ids",
        ),
        (
            "  - repo: acme/app\n    linear_project_ids: [proj-a, 7]\n",
            "projects[0].linear_project_ids must be a project id",
        ),
        (
            "  - repo: acme/app\n    linear_project_ids: [proj-a, proj-a]\n",
            "projects[0].linear_project_ids names 'proj-a' twice",
        ),
        (
            "  - repo: acme/app\n    linear_project_ids: [proj-a, proj-b]\n"
            "  - repo: acme/other\n    linear_project_id: proj-b\n",
            "duplicate Linear project 'proj-b'",
        ),
        (
            "  - repo: acme/app\n    linear_project_id: proj-a\n"
            "  - repo: acme/other\n    linear_project_ids: [proj-c, proj-a]\n",
            "duplicate Linear project 'proj-a'",
        ),
        (
            "  - repo: acme/app\n    linear_project_ids: [proj-a]\n"
            "  - repo: acme/app\n    linear_project_ids: [proj-b]\n",
            "duplicate project 'acme/app'",
        ),
    ],
)
def test_bad_linear_project_lists_are_refused(
    tmp_path: Path, projects: str, message: str
) -> None:
    config, findings = load(tmp_path, projects)

    assert config is None
    assert message in errors(findings)


@pytest.mark.parametrize(
    ("projects", "code", "message"),
    [
        ("    linear_project_ids: [proj-a, proj-b, proj-c]\n", 0, ""),
        ("    linear_project_id: proj-a\n", 0, ""),
        (
            "    linear_project_id: proj-a\n    linear_project_ids: [proj-b]\n",
            1,
            "sets both linear_project_id and linear_project_ids",
        ),
        ("    linear_project_ids: []\n", 1, "must be a non-empty list"),
        ("    linear_project_ids: [proj-a, proj-a]\n", 1, "names 'proj-a' twice"),
    ],
)
def test_check_config_end_to_end(
    tmp_path: Path, projects: str, code: int, message: str
) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(BASE + "  - repo: acme/app\n" + projects, encoding="utf-8")

    result = run(["check-config", "--orch", str(path)])

    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout + result.stderr


TWO_PROJECT_ORCH = """
poll_seconds: 60
linear:
  api_key_env: LINEAR_API_KEY
grok:
  webhook_url_env: FACTORY_WEBHOOK_URL
  webhook_key_env: FACTORY_WEBHOOK_KEY
trusted_logins:
  maxharris1: [grok-bot, claude-bot, codex-bot]
trusted_linear_users:
  max-linear-id: [grok-bot]
projects:
  - repo: acme/linear-app
    factory_ref: dev
    linear_project_ids: [proj-1, proj-2]
    intake:
      linear_state: In Progress
"""


class LinearByIssueProject(FakeLinear):
    def __init__(self) -> None:
        super().__init__()
        self.where: dict[str, str] = {}

    def linear_intake(self, project_ids: frozenset[str], state: str, assignees):
        self.queries.append((project_ids, state))
        self.assignees.append(dict(assignees))
        return [issue for issue in self.issues if self.where[issue.key] in project_ids]


def test_the_feeder_feeds_a_ticket_from_either_project_once(tmp_path: Path) -> None:
    linear = LinearByIssueProject()
    linear.issues = [intake_issue("AUT-7")]
    linear.where["AUT-7"] = "proj-2"
    feeder, queue, _, _ = make_feeder(
        tmp_path, orch=TWO_PROJECT_ORCH, linear=linear, factory_text=LINEAR_FACTORY_MD
    )

    first = feeder.feed_once()
    linear.where["AUT-7"] = "proj-1"
    second = feeder.feed_once()

    assert linear.queries[0] == (frozenset({"proj-1", "proj-2"}), "In Progress")
    assert first.fed == (TicketRef("acme/linear-app", "AUT-7", "linear"),)
    assert second.fed == ()
    assert list(rows(tmp_path / "orch.db")) == ["AUT-7"]
    assert [item["kind"] for item in headers(linear.posted["uuid-AUT-7"])] == ["brief", "go"]


def test_an_untrusted_move_in_the_second_project_is_not_fed(tmp_path: Path) -> None:
    linear = LinearByIssueProject()
    linear.issues = [intake_issue("AUT-8", applied_by="someone-else")]
    linear.where["AUT-8"] = "proj-2"
    feeder, _, _, _ = make_feeder(
        tmp_path, orch=TWO_PROJECT_ORCH, linear=linear, factory_text=LINEAR_FACTORY_MD
    )

    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db") == {}


def test_the_intake_client_reads_every_project_in_one_query() -> None:
    source = PagedLinearSource([[linear_node(7)]])
    client = LinearIntakeClient(source, {"proj-2": "acme/app", "proj-1": "acme/app"})

    found = client.linear_intake(frozenset({"proj-2", "proj-1"}), "In Progress", {})

    assert len(source.documents) == 1
    assert "project: { id: { in: $projects } }" in source.documents[0]["query"]
    assert source.documents[0]["variables"]["projects"] == ["proj-1", "proj-2"]
    assert [(item.repo, item.key) for item in found] == [("acme/app", "AUT-7")]


def test_the_intake_client_refuses_projects_of_two_repos() -> None:
    client = LinearIntakeClient(
        PagedLinearSource([[]]), {"proj-1": "acme/app", "proj-2": "acme/other"}
    )

    with pytest.raises(Exception, match="one repo"):
        client.linear_intake(frozenset({"proj-1", "proj-2"}), "In Progress", {})


def test_cli_maps_every_linear_id_to_its_repo(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        BASE
        + "  - repo: acme/app\n    linear_project_ids: [proj-a, proj-b]\n"
        + "  - repo: acme/solo\n    linear_project_id: proj-c\n",
        encoding="utf-8",
    )
    orch, findings = load_orch_yml(path)
    assert orch is not None, findings

    source = type("Source", (), {"factory_context": None, "_adapter_runner": None})()
    feeder = cli._feeder(orch, SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: NOW), None, source)

    assert feeder.linear.repo_for_project == {
        "proj-a": "acme/app",
        "proj-b": "acme/app",
        "proj-c": "acme/solo",
    }
    assert orch.secret_env_names
    for name in orch.secret_env_names:
        assert ("-u", name) in zip(feeder.github.gh, feeder.github.gh[1:])


def two_projects():
    base = config(linear=True)
    (project,) = base.projects
    return replace(
        base,
        projects=(replace(project, linear_project_ids=frozenset({"project-1", "project-2"})),),
    )


def transport_for(issues: dict[str, dict]):
    def transport(url, headers, body, timeout):
        del url, headers, timeout
        request = json.loads(body)
        if request["query"].startswith("query FactoryPoll"):
            wanted = set(request["variables"]["projectIds"])
            return linear_search_page(
                [
                    {key: issue.get(key) for key in ("id", "identifier", "project")}
                    for issue in issues.values()
                    if issue["project"]["id"] in wanted
                ]
            )
        if request["query"].startswith("query FactoryIssue"):
            wanted = request["variables"]["id"]
            (issue,) = [item for item in issues.values() if wanted in (item["id"], item["identifier"])]
            return HTTPResult(200, json.dumps({"data": {"issue": issue}}))
        raise AssertionError(request)

    return transport


def source_for(orch, issues: dict[str, dict], errors: list | None = None) -> LivePollSource:
    return LivePollSource(
        orch,
        environ={"LINEAR_KEY": "key"},
        gh_runner=FakeGh(FACTORY.format(tickets="linear", ship="handoff")),
        linear_transport=transport_for(issues),
        error_sink=(lambda repo, ticket, error: errors.append((repo, ticket, error)))
        if errors is not None
        else None,
    )


def test_discovery_finds_a_ticket_in_the_second_project() -> None:
    issues = {"AUT-42": linear_issue(project="project-2")}
    source = source_for(two_projects(), issues)

    tickets = source.linear_candidates(LINEAR_HANDOFF_QUERY)

    assert [(ticket.repo, ticket.ticket_id) for ticket in tickets] == [
        ("acme/widgets", "AUT-42")
    ]


def test_discovery_skips_a_ticket_in_an_unconfigured_project() -> None:
    issues = {"AUT-42": linear_issue(project="project-3")}
    source = source_for(two_projects(), issues)

    assert source.linear_candidates(LINEAR_HANDOFF_QUERY) == ()


def test_a_tracked_ticket_moved_between_configured_projects_keeps_working() -> None:
    issues = {"AUT-42": linear_issue(project="project-1")}
    found = []
    errors: list = []
    ref = TicketRef("acme/widgets", "AUT-42", "linear")
    for project in ("project-1", "project-2"):
        issues["AUT-42"] = linear_issue(project=project)
        source = source_for(two_projects(), issues, errors)
        found.append(source.tracked_tickets((ref,)))

    assert errors == []
    assert [len(tickets) for tickets in found] == [1, 1]
    assert found[0] == found[1]
    assert (found[1][0].repo, found[1][0].ticket_id) == ("acme/widgets", "AUT-42")
    assert source_for(two_projects(), issues).fresh_ticket(ref) is not None


def test_a_tracked_ticket_moved_to_an_unconfigured_project_is_still_refused() -> None:
    issues = {"AUT-42": linear_issue(project="project-3")}
    errors: list = []
    ref = TicketRef("acme/widgets", "AUT-42", "linear")
    source = source_for(two_projects(), issues, errors)

    assert source.tracked_tickets((ref,)) == ()
    assert errors == [
        ("acme/widgets", "AUT-42", "PollSourceError:Linear issue is not in its configured project")
    ]
    assert source_for(two_projects(), issues).fresh_ticket(ref) is None


def test_single_project_poll_still_refuses_the_other_project() -> None:
    issues = {"AUT-42": linear_issue(project="project-2")}
    errors: list = []
    source = source_for(config(linear=True), issues, errors)

    assert source.tracked_tickets((TicketRef("acme/widgets", "AUT-42", "linear"),)) == ()
    assert len(errors) == 1


@pytest.fixture
def two_project_app(tmp_path: Path) -> tuple[WebhookApp, Path]:
    database = tmp_path / "orch.db"
    SQLiteJobQueue(database, clock=lambda: NOW)
    app = WebhookApp(
        secret=SECRET.encode(),
        project_ids=frozenset({"project-1", "project-2"}),
        db_path=database,
        clock=lambda: NOW,
        log=lambda line: None,
    )
    return app, database


def test_the_webhook_accepts_the_second_project_and_a_move_between_them(
    two_project_app,
) -> None:
    app, database = two_project_app

    second = post(app, issue_event(project="project-2", identifier="AUT-1", issue_id="i-1"))
    moved = post(
        app,
        issue_event(
            project="project-2",
            identifier="AUT-2",
            issue_id="i-2",
            updated_from={"projectId": "project-1"},
        ),
    )
    other = post(app, issue_event(project="project-3", identifier="AUT-3", issue_id="i-3"))

    assert (second.outcome, moved.outcome) == ("accepted", "accepted")
    assert other.outcome != "accepted"
    assert [row[1] for row in dirty_rows(database)] == ["AUT-1", "AUT-2"]


def test_webhook_serve_passes_every_configured_id(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "orch.db"
    SQLiteJobQueue(database, clock=lambda: NOW)
    secret_file = tmp_path / "linear-webhook.secret"
    secret_file.write_text(SECRET, encoding="utf-8")
    secret_file.chmod(0o600)
    path = tmp_path / "orch.yml"
    path.write_text(
        BASE
        + "  - repo: acme/app\n    factory_ref: main\n"
        + "    linear_project_ids: [project-1, project-2]\n"
        + "  - repo: acme/solo\n    factory_ref: main\n    linear_project_id: project-3\n"
        + "linear_webhook:\n  bind: 127.0.0.1:8787\n"
        + f"  secret_file: {secret_file}\n",
        encoding="utf-8",
    )
    seen: dict = {}

    class FakeServer:
        server_address = ("127.0.0.1", 8787)

        def serve_forever(self) -> None:
            pass

        def server_close(self) -> None:
            pass

    def fake_make_server(app, host, port):
        seen["projects"] = app.project_ids
        return FakeServer()

    monkeypatch.setattr("lib.linear_webhook.make_server", fake_make_server)

    code = cli.main(
        ["linear-webhook", "serve", "--db", str(database), "--orch", str(path)]
    )

    assert code == 0
    assert seen["projects"] == frozenset({"project-1", "project-2", "project-3"})
