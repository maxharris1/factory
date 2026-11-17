from __future__ import annotations

from pathlib import Path

import pytest

from lib.config_overlay import overlay_keys, overlay_path
from lib.orch_yml import load_orch_yml
from tests.test_cli import run
from tests.test_config_overlay import overlay_on  # noqa: F401

UPLOAD = "proj-upload"
FEATURE = "feature/upload-only-intake"

ORCH = """poll_seconds: 60
linear:
  api_key_env: LINEAR_API_KEY
trusted_logins:
  maxharris1: [grok-bot]
trusted_linear_users:
  linear:user-1: [grok-bot]
projects:
  - repo: acme/app
    factory_ref: dev
    linear_project_ids: [proj-offer, proj-upload]
    intake:
      linear_state: In Progress
{base_branches}"""


def load(tmp_path: Path, base_branches: str):
    path = tmp_path / "orch.yml"
    path.write_text(ORCH.format(base_branches=base_branches), encoding="utf-8")
    return load_orch_yml(path)


def errors(findings) -> list[str]:
    return [item.message for item in findings if item.severity == "error"]


def test_an_overridden_project_gets_its_branch_and_the_rest_get_factory_ref(
    tmp_path: Path,
) -> None:
    config, findings = load(tmp_path, f"      base_branches:\n        {UPLOAD}: {FEATURE}\n")

    assert errors(findings) == []
    assert config is not None
    project = config.projects[0]
    assert project.base_for(UPLOAD) == FEATURE
    assert project.base_for("proj-offer") == "dev"
    assert project.base_for(None) == "dev"


def test_no_override_keeps_factory_ref_for_every_project(tmp_path: Path) -> None:
    config, findings = load(tmp_path, "")

    assert errors(findings) == []
    assert config is not None
    assert config.projects[0].base_for(UPLOAD) == "dev"


@pytest.mark.parametrize(
    ("base_branches", "message"),
    [
        (f"      base_branches:\n        proj-other: {FEATURE}\n",
         "base_branches names a project this entry does not watch"),
        (f"      base_branches:\n        {UPLOAD}: feature/../dev\n",
         "base_branches must map each project to a plain branch name"),
        (f"      base_branches:\n        {UPLOAD}: feature upload\n",
         "base_branches must map each project to a plain branch name"),
        (f"      base_branches:\n        {UPLOAD}: 7\n",
         "base_branches must map each project to a plain branch name"),
        (f"      base_branches: [{UPLOAD}]\n", "base_branches must be a non-empty mapping"),
        ("      base_branches: {}\n", "base_branches must be a non-empty mapping"),
    ],
    ids=["unwatched-project", "dot-dot", "space", "not-a-string", "list", "empty"],
)
def test_a_bad_override_is_a_config_error(
    tmp_path: Path, base_branches: str, message: str
) -> None:
    config, findings = load(tmp_path, base_branches)

    assert config is None
    assert any(message in item for item in errors(findings)), findings


def test_a_github_project_cannot_override_its_base(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\ntrusted_logins:\n  maxharris1: [grok-bot]\nprojects:\n"
        "  - repo: acme/app\n    factory_ref: dev\n    intake:\n      label: factory:ready\n"
        f"      base_branches:\n        {UPLOAD}: {FEATURE}\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)

    assert config is None
    assert any("base_branches needs linear_project_id" in item for item in errors(findings))


def test_overlay_keys_name_the_override_without_ids_or_branches() -> None:
    keys = overlay_keys({"projects": [{"repo": "a/b", "intake": {"base_branches": {UPLOAD: FEATURE}}}]})

    assert keys == ["projects[a/b].intake.base_branches"]


def check_config(tmp_path: Path, overlay: str):
    orch = tmp_path / "orch.yml"
    orch.write_text(ORCH.format(base_branches=""), encoding="utf-8")
    overlay_path(orch).write_text(overlay, encoding="utf-8")
    return run(["check-config", "--orch", str(orch)])


def overlay(branch: str, project: str = UPLOAD) -> str:
    return (
        "projects:\n  - repo: acme/app\n"
        f"    intake:\n      base_branches:\n        {project}: {branch}\n"
    )


def test_check_config_accepts_the_override_from_orch_local_yml(
    overlay_on: None, tmp_path: Path  # noqa: F811
) -> None:
    result = check_config(tmp_path, overlay(FEATURE))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "overlay orch.local.yml sets: projects[acme/app].intake.base_branches",
        "ok",
    ]
    assert FEATURE not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (overlay(FEATURE, project="proj-unknown"),
         "projects[0].intake.base_branches names a project this entry does not watch"),
        (overlay("feature/../dev"),
         "projects[0].intake.base_branches must map each project to a plain branch name"),
    ],
    ids=["unknown-linear-id", "bad-branch"],
)
def test_check_config_refuses_a_bad_override_from_orch_local_yml(
    overlay_on: None, tmp_path: Path, text: str, message: str  # noqa: F811
) -> None:
    result = check_config(tmp_path, text)

    assert result.returncode != 0
    assert message in result.stdout + result.stderr
    assert "proj-unknown" not in result.stdout + result.stderr
