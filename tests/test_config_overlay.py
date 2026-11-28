"""Host-local orch.local.yml and machines.local.yml overlays (issue #107)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from lib import live_settings, settings_apply
from lib.config_overlay import merge, overlay_keys, overlay_path
from lib.machines_yml import load_machines_yml
from lib.orch_yml import load_orch_yml
from tests.pool_helpers import LOCAL_BOX_MACHINES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def overlay_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY", raising=False)


@pytest.fixture
def shipped(tmp_path: Path) -> tuple[Path, Path]:
    orch = tmp_path / "orch.yml"
    machines = tmp_path / "machines.yml"
    shutil.copy(ROOT / "orch.yml", orch)
    shutil.copy(ROOT / "machines.yml", machines)
    return orch, machines


def test_overlay_path_sits_beside_the_base_file() -> None:
    assert overlay_path(Path("/x/orch.yml")) == Path("/x/orch.local.yml")
    assert overlay_path(Path("/x/machines.yml")) == Path("/x/machines.local.yml")


def test_projects_merge_by_repo_and_new_ones_append() -> None:
    base = {"projects": [{"repo": "a/b", "factory_ref": "main", "intake": {"label": "x"}},
                         {"repo": "c/d"}]}
    merged = merge(base, {"projects": [
        {"repo": "a/b", "ready_starts_review": True, "intake": {"trusted": ["m"]}},
        {"repo": "e/f"},
    ]})
    assert merged["projects"] == [
        {"repo": "a/b", "factory_ref": "main", "ready_starts_review": True,
         "intake": {"label": "x", "trusted": ["m"]}},
        {"repo": "c/d"},
        {"repo": "e/f"},
    ]
    assert base["projects"][0] == {"repo": "a/b", "factory_ref": "main", "intake": {"label": "x"}}


def test_devices_merge_by_name() -> None:
    base = {"devices": [{"name": "box", "slot_max": 3}, {"name": "pc", "enabled": False}]}
    merged = merge(base, {"devices": [{"name": "pc", "enabled": True, "slot_max": 2}]})
    assert merged["devices"] == [{"name": "box", "slot_max": 3},
                                 {"name": "pc", "enabled": True, "slot_max": 2}]


def test_scalars_override_mappings_merge_and_other_lists_replace() -> None:
    base = {"poll_seconds": 30, "grok": {"a": 1, "b": 2},
            "trusted_logins": {"max": ["grok-bot", "claude-bot"]}}
    merged = merge(base, {"poll_seconds": 60, "grok": {"b": 3},
                          "trusted_logins": {"max": ["grok-bot"]}})
    assert merged == {"poll_seconds": 60, "grok": {"a": 1, "b": 3},
                      "trusted_logins": {"max": ["grok-bot"]}}


def test_either_linear_project_spelling_replaces_the_other() -> None:
    base = {"projects": [{"repo": "a/b", "linear_project_id": "one"},
                         {"repo": "c/d", "linear_project_ids": ["two"]}]}
    overlay = {"projects": [{"repo": "a/b", "linear_project_ids": ["one", "three"]},
                            {"repo": "c/d", "linear_project_id": "four"}]}

    assert merge(base, overlay)["projects"] == [
        {"repo": "a/b", "linear_project_ids": ["one", "three"]},
        {"repo": "c/d", "linear_project_id": "four"}]


def test_a_placeholder_project_id_moves_to_a_list_in_the_overlay(
    overlay_on: None, tmp_path: Path
) -> None:
    digests = {}
    for name in ("edited", "layered"):
        folder = tmp_path / name
        folder.mkdir()
        orch, machines = folder / "orch.yml", folder / "machines.yml"
        shutil.copy(ROOT / "machines.yml", machines)
        text = (ROOT / "orch.yml").read_text(encoding="utf-8")
        if name == "edited":
            text = text.replace("linear_project_id: REPLACE_WITH_LINEAR_PROJECT_ID",
                                "linear_project_ids:\n      - real-project")
        orch.write_text(text, encoding="utf-8")
        if name == "layered":
            overlay_path(orch).write_text(
                "projects:\n  - repo: aut0-code/aut0\n    linear_project_ids:\n"
                "      - real-project\n", encoding="utf-8")
        result = check_config(orch, machines, "--digest")
        assert result.returncode == 0, result.stderr
        digests[name] = [line for line in result.stdout.splitlines()
                         if line.startswith("effective ")]

    assert digests["edited"] == digests["layered"]


def test_overlay_keys_name_fields_never_values() -> None:
    keys = overlay_keys({
        "linear_webhook": {"public_url": "https://secret.example.ts.net/hook"},
        "projects": [{"repo": "a/b", "linear_project_id": "proj-123"}, {"repo": "c/d"}],
        "trusted_linear_users": {"user-1": ["grok-bot"]},
    })
    assert keys == [
        "linear_webhook.public_url",
        "projects[a/b].linear_project_id",
        "projects[c/d]",
        "trusted_linear_users",
    ]


def test_overlay_fills_a_placeholder(overlay_on: None, shipped: tuple[Path, Path]) -> None:
    orch, _ = shipped
    config, findings = load_orch_yml(orch, allow_placeholders=True)
    assert any("placeholder" in f.message for f in findings)
    overlay_path(orch).write_text(
        "projects:\n  - repo: aut0-code/aut0\n    linear_project_id: real-project\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(orch)
    assert findings == []
    assert config is not None
    project = next(p for p in config.projects if p.repo == "aut0-code/aut0")
    assert project.linear_project_ids == frozenset({"real-project"})
    assert project.job_timeout == 150


def test_machines_overlay_enables_a_device(overlay_on: None, shipped: tuple[Path, Path]) -> None:
    _, machines = shipped
    shutil.copy(LOCAL_BOX_MACHINES, machines)
    overlay_path(machines).write_text(
        "devices:\n  - name: pc\n    enabled: true\n    slot_max: 1\n", encoding="utf-8")
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    pc = config.device("pc")
    assert pc is not None and pc.enabled and pc.slot_max == 1
    assert config.device("box").slot_max == 3


def test_test_suite_max_is_set_per_host_and_the_tracked_file_refuses_it(
    overlay_on: None, shipped: tuple[Path, Path]
) -> None:
    orch, machines = shipped
    overlay_path(machines).write_text(
        "devices:\n  - name: pc\n    test_suite_max: 2\n", encoding="utf-8")
    config, findings = load_machines_yml(machines)
    assert config is not None, findings
    assert config.device("pc").test_suite_max == 2
    assert config.device("box").test_suite_max == 1

    base = machines.read_text(encoding="utf-8")
    machines.write_text(
        base.replace("    slot_max: 6\n", "    slot_max: 6\n    test_suite_max: 3\n", 1),
        encoding="utf-8",
    )
    result = check_config(orch, machines)
    assert result.returncode == 1
    assert any("machines.local.yml" in line for line in result.stderr.splitlines()), result.stderr

    assert "test_suite_max" not in settings_apply.FIELDS
    assert "test_suite_max" not in {item.name for item in fields(live_settings.Switch)}


@pytest.mark.parametrize("text, expected", [
    ("projects: [\n", "orch.local.yml: invalid YAML"),
    ("- a\n- b\n", "orch.local.yml: an overlay must be a mapping"),
    ("projects:\n  - factory_ref: main\n", "orch.local.yml: projects[0] must be a mapping with a repo"),
    ("projects: {repo: a/b}\n", "orch.local.yml: projects must be a list"),
])
def test_a_bad_overlay_is_an_error_naming_the_file(
    overlay_on: None, shipped: tuple[Path, Path], text: str, expected: str
) -> None:
    orch, _ = shipped
    overlay_path(orch).write_text(text, encoding="utf-8")
    config, findings = load_orch_yml(orch, allow_placeholders=True)
    assert config is None
    assert [f.severity for f in findings] == ["error"]
    assert findings[0].message.startswith(expected)


def test_the_env_switch_and_the_argument_turn_it_off(
    monkeypatch: pytest.MonkeyPatch, shipped: tuple[Path, Path]
) -> None:
    orch, _ = shipped
    overlay_path(orch).write_text("poll_seconds: 60\n", encoding="utf-8")
    config, _ = load_orch_yml(orch, allow_placeholders=True)
    assert config is not None and config.poll_seconds == 30
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY")
    config, _ = load_orch_yml(orch, allow_placeholders=True)
    assert config is not None and config.poll_seconds == 60
    config, _ = load_orch_yml(orch, allow_placeholders=True, overlay=False)
    assert config is not None and config.poll_seconds == 30


@pytest.mark.parametrize("base, example, load, options", [
    ("orch.yml", "orch.local.example.yml", load_orch_yml, {"allow_placeholders": True}),
    ("machines.yml", "machines.local.example.yml", load_machines_yml, {}),
])
def test_the_shipped_examples_load_as_overlays(
    overlay_on: None, tmp_path: Path, base: str, example: str, load, options: dict
) -> None:
    target = tmp_path / base
    shutil.copy(ROOT / base, target)
    shutil.copy(ROOT / "config" / example, overlay_path(target))
    config, findings = load(target, **options)
    assert config is not None
    assert not [f for f in findings if f.severity == "error"]


def test_check_config_lists_overlay_key_names_and_no_values(
    overlay_on: None, shipped: tuple[Path, Path]
) -> None:
    orch, machines = shipped
    overlay_path(orch).write_text(
        "projects:\n  - repo: aut0-code/aut0\n    linear_project_id: secret-project-id\n"
        "trusted_linear_users:\n  secret-user-id:\n    - grok-bot\n",
        encoding="utf-8",
    )
    overlay_path(machines).write_text(
        "devices:\n  - name: box\n    slot_max: 2\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "check-config",
         "--orch", str(orch), "--machines", str(machines), "--allow-placeholders"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "overlay orch.local.yml sets: projects[aut0-code/aut0].linear_project_id, "
        "trusted_linear_users",
        "overlay machines.local.yml sets: devices[box].slot_max",
        "ok",
    ]
    assert "secret-project-id" not in result.stdout + result.stderr
    assert "secret-user-id" not in result.stdout + result.stderr


def check_config(orch: Path, machines: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "check-config",
         "--orch", str(orch), "--machines", str(machines), "--allow-placeholders", *extra],
        capture_output=True, text=True, check=False,
    )


def test_check_config_warns_on_uncommitted_edits_to_a_tracked_config_file(
    shipped: tuple[Path, Path]
) -> None:
    orch, machines = shipped
    git = ["git", "-C", str(orch.parent), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "orch.yml", "machines.yml"], check=True)
    subprocess.run([*git, "commit", "-qm", "seed"], check=True)
    clean = check_config(orch, machines)
    orch.write_text(orch.read_text(encoding="utf-8").replace(
        "REPLACE_WITH_LINEAR_PROJECT_ID", "real-project"), encoding="utf-8")

    dirty = check_config(orch, machines)

    assert (clean.returncode, dirty.returncode) == (0, 0), dirty.stderr
    assert "uncommitted" not in clean.stderr
    assert [line for line in dirty.stderr.splitlines() if "uncommitted" in line] == [
        f"{orch}: warning: uncommitted edits to a tracked config file; "
        "move them into orch.local.yml"]


def test_check_config_digest_is_the_same_for_a_tracked_edit_and_its_overlay(
    overlay_on: None, tmp_path: Path
) -> None:
    digests = {}
    for name in ("base", "edited", "layered"):
        folder = tmp_path / name
        folder.mkdir()
        orch, machines = folder / "orch.yml", folder / "machines.yml"
        shutil.copy(ROOT / "machines.yml", machines)
        text = (ROOT / "orch.yml").read_text(encoding="utf-8")
        if name == "edited":
            text = text.replace("REPLACE_WITH_LINEAR_PROJECT_ID", "real-project")
        orch.write_text(text, encoding="utf-8")
        if name == "layered":
            overlay_path(orch).write_text(
                "projects:\n  - repo: aut0-code/aut0\n    linear_project_id: real-project\n",
                encoding="utf-8")
        result = check_config(orch, machines, "--digest")
        digests[name] = [line for line in result.stdout.splitlines()
                         if line.startswith("effective ")]

    assert [line.split(" sha256=")[0] for line in digests["edited"]] == [
        "effective orch.yml", "effective machines.yml"]
    assert digests["edited"] == digests["layered"]
    assert digests["base"][0] != digests["edited"][0]
    assert digests["base"][1] == digests["edited"][1]


def test_the_dashboard_reads_machines_yml_through_its_overlay(
    overlay_on: None, shipped: tuple[Path, Path], tmp_path: Path
) -> None:
    from lib.dashboard import collect_snapshot
    from lib.queue import SQLiteJobQueue

    _, machines = shipped
    shutil.copy(LOCAL_BOX_MACHINES, machines)
    overlay_path(machines).write_text("devices:\n  - name: box\n    slot_max: 7\n",
                                      encoding="utf-8")
    SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1000.0)

    snapshot = collect_snapshot(tmp_path / "orch.db", tmp_path / "none.log", now=1000.0,
                                machines_path=machines)

    assert snapshot["slots"]["box"]["slot_max"] == 7
