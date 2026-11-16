"""Per-Linear-project assignee allowlist for feeder intake (issue #583)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from lib.config_overlay import overlay_keys, overlay_path
from lib.dispatcher import TicketRef
from lib.intake_live import LINEAR_ASSIGNED_INTAKE_QUERY, LINEAR_INTAKE_QUERY, LinearIntakeClient
from lib.orch_yml import load_orch_yml
from tests.test_config_overlay import ROOT, overlay_on, shipped  # noqa: F401
from tests.test_feeder import LINEAR_FACTORY_MD, FakeLinear, linear_issue, make_feeder, rows
from tests.test_feeder_template import flags, wakes
from tests.test_intake_live import FakeLinearSource, PagedLinearSource, linear_node

OURS = "11111111-1111-4111-8111-111111111111"
ALSO_OURS = "22222222-2222-4222-8222-222222222222"
THEIRS = "33333333-3333-4333-8333-333333333333"

TWO_PROJECTS = """
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
{assignees}"""

ALLOWLIST = f"      linear_assignees:\n        proj-1: [{OURS}, {ALSO_OURS}]\n"
ALLOWLISTED = TWO_PROJECTS.replace("{assignees}", ALLOWLIST)


def load(tmp_path: Path, assignees: str):
    path = tmp_path / "orch.yml"
    path.write_text(TWO_PROJECTS.replace("{assignees}", assignees), encoding="utf-8")
    return load_orch_yml(path, allow_placeholders=True)


def errors(findings) -> list[str]:
    return [item.message for item in findings if item.severity == "error"]


def in_project(identifier: str, project: str, assignee: str | None, **kwargs):
    return replace(linear_issue(identifier, **kwargs), linear_project=project, assignee=assignee)


def feeder_for(tmp_path: Path, *issues, orch: str = ALLOWLISTED):
    linear = FakeLinear()
    linear.issues = list(issues)
    feeder, _, github, _ = make_feeder(
        tmp_path, orch=orch, linear=linear, factory_text=LINEAR_FACTORY_MD
    )
    return feeder, github, linear


def fed(report) -> list[str]:
    return [ref.ticket_id for ref in report.fed]


# --- ac-5: config ------------------------------------------------------------


def test_the_allowlist_is_a_mapping_of_project_id_to_user_ids(tmp_path: Path) -> None:
    config, findings = load(tmp_path, ALLOWLIST)

    assert errors(findings) == []
    assert config is not None
    intake = config.projects[0].intake
    assert dict(intake.linear_assignees) == {"proj-1": frozenset({OURS, ALSO_OURS})}


def test_no_allowlist_is_an_empty_mapping(tmp_path: Path) -> None:
    config, findings = load(tmp_path, "")

    assert errors(findings) == []
    assert dict(config.projects[0].intake.linear_assignees) == {}


@pytest.mark.parametrize(
    ("assignees", "message"),
    [
        ("      linear_assignees:\n        proj-1: []\n",
         "linear_assignees.proj-1 must be a non-empty list of Linear user ids"),
        ("      linear_assignees:\n        proj-1: [Max Harris]\n",
         "linear_assignees.proj-1 must be a non-empty list of Linear user ids"),
        ("      linear_assignees: {}\n", "linear_assignees must be a non-empty mapping"),
        (f"      linear_assignees:\n        proj-9: [{OURS}]\n",
         "linear_assignees names a project this entry does not watch"),
        (f"      linear_assignee:\n        proj-1: [{OURS}]\n",
         "unknown field 'projects[0].intake.linear_assignee'"),
    ],
    ids=["empty-list", "display-name", "empty-mapping", "unwatched-project", "unknown-field"],
)
def test_a_bad_allowlist_is_a_config_error(tmp_path: Path, assignees: str, message: str) -> None:
    config, findings = load(tmp_path, assignees)

    assert config is None
    assert any(message in item for item in errors(findings)), findings


def test_a_github_project_cannot_set_an_allowlist(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\ntrusted_logins:\n  maxharris1: [grok-bot]\nprojects:\n"
        "  - repo: acme/app\n    intake:\n      label: factory:ready\n"
        f"      linear_assignees:\n        proj-1: [{OURS}]\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path, allow_placeholders=True)

    assert config is None
    assert any("linear_assignees needs linear_project_id" in item for item in errors(findings))


def test_overlay_keys_name_the_allowlist_without_ids() -> None:
    keys = overlay_keys({"projects": [{"repo": "a/b", "intake": {
        "linear_state": "In Progress",
        "linear_assignees": {"secret-project": [OURS]},
    }}]})

    assert keys == ["projects[a/b].intake.linear_state", "projects[a/b].intake.linear_assignees"]


def test_check_config_names_the_allowlist_overlay_and_prints_no_ids(
    overlay_on: None, shipped: tuple[Path, Path]  # noqa: F811
) -> None:
    orch, machines = shipped
    overlay_path(orch).write_text(
        "projects:\n  - repo: aut0-code/aut0\n"
        "    linear_project_id: secret-project-b\n"
        f"    intake:\n      linear_assignees:\n        secret-project-b: [{OURS}]\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "check-config",
         "--orch", str(orch), "--machines", str(machines), "--allow-placeholders"],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == (
        "overlay orch.local.yml sets: projects[aut0-code/aut0].linear_project_id, "
        "projects[aut0-code/aut0].intake.linear_assignees"
    )
    output = result.stdout + result.stderr
    assert "secret-project" not in output and OURS not in output


# --- ac-1, ac-2, ac-3: the feeder ----------------------------------------------


def test_an_issue_assigned_outside_the_allowlist_is_invisible(tmp_path: Path) -> None:
    broken = replace(in_project("AUT-8", "proj-1", THEIRS), body="No template at all.")
    feeder, github, _ = feeder_for(tmp_path, in_project("AUT-7", "proj-1", THEIRS), broken)

    for _ in range(2):
        report = feeder.feed_once()
        assert (report.fed, report.refused) == ((), 0)
    assert rows(tmp_path / "orch.db") == {}
    assert wakes(tmp_path) == []
    assert flags(tmp_path) == set()
    assert github.pulls == {}


def test_an_unassigned_issue_in_an_allowlisted_project_is_invisible(tmp_path: Path) -> None:
    feeder, github, _ = feeder_for(tmp_path, in_project("AUT-7", "proj-1", None))

    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db") == {} and github.pulls == {}


def test_an_issue_assigned_on_the_allowlist_is_fed(tmp_path: Path) -> None:
    feeder, github, linear = feeder_for(tmp_path, in_project("AUT-7", "proj-1", ALSO_OURS))

    report = feeder.feed_once()

    assert report.fed == (TicketRef("acme/linear-app", "AUT-7", "linear"),)
    assert len(github.pulls) == 1
    assert linear.assignees == [{"proj-1": frozenset({OURS, ALSO_OURS})}]


def test_an_allowlisted_issue_is_still_refused_for_its_template(tmp_path: Path) -> None:
    broken = replace(in_project("AUT-7", "proj-1", OURS), body="No template at all.")
    feeder, github, _ = feeder_for(tmp_path, broken)

    report = feeder.feed_once()

    assert (report.fed, report.refused) == ((), 1)
    assert rows(tmp_path / "orch.db")["AUT-7"]["state"] == "refused-intake"
    assert len(wakes(tmp_path)) == 1


def test_an_allowlisted_issue_moved_by_an_untrusted_user_is_not_fed(tmp_path: Path) -> None:
    feeder, github, _ = feeder_for(
        tmp_path, in_project("AUT-7", "proj-1", OURS, applied_by="someone-else")
    )

    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db") == {}


def test_a_sibling_project_without_an_allowlist_is_not_filtered(tmp_path: Path) -> None:
    feeder, github, _ = feeder_for(
        tmp_path,
        in_project("AUT-7", "proj-2", THEIRS, created_at=10.0),
        in_project("AUT-8", "proj-2", None, created_at=20.0),
        in_project("AUT-9", "proj-1", THEIRS, created_at=30.0),
    )

    seen: list[str] = []
    for _ in range(3):
        seen += fed(feeder.feed_once())

    assert sorted(seen) == ["AUT-7", "AUT-8"]
    assert "AUT-9" not in rows(tmp_path / "orch.db")


def test_no_allowlist_keeps_todays_intake(tmp_path: Path) -> None:
    feeder, _, linear = feeder_for(
        tmp_path, linear_issue("AUT-7"), orch=TWO_PROJECTS.replace("{assignees}", "")
    )

    assert fed(feeder.feed_once()) == ["AUT-7"]
    assert linear.assignees == [{}]


def test_an_issue_from_an_unwatched_project_is_dropped_when_an_allowlist_is_set(
    tmp_path: Path,
) -> None:
    feeder, _, _ = feeder_for(
        tmp_path, in_project("AUT-7", "proj-9", OURS), in_project("AUT-8", None, OURS)
    )

    assert feeder.feed_once().fed == ()
    assert rows(tmp_path / "orch.db") == {}


# --- ac-4: the GraphQL intake filter ---------------------------------------------


def test_an_allowlisted_project_is_queried_with_its_assignees_alone() -> None:
    source = PagedLinearSource([[]])
    client = LinearIntakeClient(source, {"proj-1": "acme/app", "proj-2": "acme/app"})

    client.linear_intake(
        frozenset({"proj-1", "proj-2"}), "In Progress", {"proj-1": frozenset({OURS, ALSO_OURS})}
    )

    assert [(doc["query"], doc["variables"]) for doc in source.documents] == [
        (LINEAR_INTAKE_QUERY,
         {"projects": ["proj-2"], "state": "In Progress", "after": None}),
        (LINEAR_ASSIGNED_INTAKE_QUERY,
         {"projects": ["proj-1"], "state": "In Progress", "assignees": [OURS, ALSO_OURS],
          "after": None}),
    ]
    assert "assignee: { id: { in: $assignees } }" in LINEAR_ASSIGNED_INTAKE_QUERY
    assert "project: { id: { in: $projects } }" in LINEAR_ASSIGNED_INTAKE_QUERY
    assert "assignee: { id:" not in LINEAR_INTAKE_QUERY


def test_only_allowlisted_projects_skip_the_unfiltered_query() -> None:
    source = PagedLinearSource([[]])
    client = LinearIntakeClient(source, {"proj-1": "acme/app"})

    client.linear_intake(frozenset({"proj-1"}), "In Progress", {"proj-1": frozenset({OURS})})

    assert [doc["query"] for doc in source.documents] == [LINEAR_ASSIGNED_INTAKE_QUERY]


def test_without_an_allowlist_one_unfiltered_query_reads_every_project() -> None:
    source = PagedLinearSource([[]])
    client = LinearIntakeClient(source, {"proj-1": "acme/app", "proj-2": "acme/app"})

    client.linear_intake(frozenset({"proj-1", "proj-2"}), "In Progress", {})

    assert [(doc["query"], doc["variables"]["projects"]) for doc in source.documents] == [
        (LINEAR_INTAKE_QUERY, ["proj-1", "proj-2"]),
    ]


def test_the_intake_reads_each_issues_project_and_assignee() -> None:
    assigned = {**linear_node(7), "project": {"id": "proj-1"}, "assignee": {"id": OURS}}
    unassigned = {**linear_node(8), "project": {"id": "proj-1"}, "assignee": None}
    source = FakeLinearSource({"issues": {"nodes": [assigned, unassigned]}})

    found = LinearIntakeClient(source, {"proj-1": "acme/app"}).linear_intake(
        frozenset({"proj-1"}), "In Progress", {}
    )

    assert [(item.key, item.linear_project, item.assignee) for item in found] == [
        ("AUT-7", "proj-1", OURS), ("AUT-8", "proj-1", None),
    ]
