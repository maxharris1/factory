from pathlib import Path

import pytest

from lib import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_NO_LOCAL_OVERLAY", "1")


def write_orch(tmp_path: Path, devices: str | None) -> Path:
    line = "" if devices is None else f"    devices: {devices}\n"
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 30\nprojects:\n  - repo: owner/repo\n"
        + line
        + "trusted_logins:\n  maxharris1: [grok-bot]\n",
        encoding="utf-8",
    )
    return orch


def write_machines(tmp_path: Path, *, pc_enabled: bool = True) -> Path:
    text = (ROOT / "config/machines.example.yml").read_text(encoding="utf-8")
    if not pc_enabled:
        text = text.replace("  - name: pc\n", "  - name: pc\n    enabled: false\n", 1)
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    return path


def check(tmp_path: Path, devices: str | None, *, machines: bool = True, pc_enabled: bool = True) -> int:
    argv = ["--orch", str(write_orch(tmp_path, devices)), "--factory", str(ROOT / "examples/cleffy/factory.md")]
    if machines:
        argv += ["--machines", str(write_machines(tmp_path, pc_enabled=pc_enabled))]
    return cli._check_config(argv)


@pytest.mark.parametrize("devices", ["pc", "[1]", '[""]', "[]", "{pc: true}", "[pc, [mac]]"])
def test_a_devices_entry_that_is_not_a_list_of_names_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], devices: str
) -> None:
    status = check(tmp_path, devices)

    assert status == 1
    assert "projects[0].devices must be a non-empty list of device names" in capsys.readouterr().err


def test_a_devices_entry_naming_an_unknown_device_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = check(tmp_path, "[pc, nope]")

    assert status == 1
    err = capsys.readouterr().err
    assert "orch.yml: projects[owner/repo].devices names nope, which machines.yml does not list" in err
    assert "names pc" not in err


def test_a_devices_entry_naming_a_disabled_device_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = check(tmp_path, "[pc]", pc_enabled=False)

    assert status == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[-1] == "ok"
    assert "warning: projects[owner/repo].devices names pc, which is disabled (enabled: false)" in err


@pytest.mark.parametrize("devices", ["[pc]", "[pc, box]", None])
def test_known_enabled_devices_or_no_devices_key_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], devices: str | None
) -> None:
    status = check(tmp_path, devices)

    assert status == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[-1] == "ok"
    assert "devices" not in err


def test_device_names_are_not_checked_without_machines_yml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = check(tmp_path, "[nope]", machines=False)

    assert status == 0
    assert "devices" not in capsys.readouterr().err
