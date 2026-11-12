from pathlib import Path

import pytest

from lib.machines_yml import load_machines_yml

ROOT = Path(__file__).resolve().parents[1]

BOX = """
  - name: box
    local: true
    slot_max: 2
    harnesses:
      claude: claude-bot
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(text.lstrip(), encoding="utf-8")
    return path


def errors(findings) -> list[str]:
    return [item.message for item in findings if item.severity == "error"]


def warnings(findings) -> list[str]:
    return [item.message for item in findings if item.severity == "warning"]


def remote(**fields: str) -> str:
    values = {
        "name": "pc",
        "tailscale_name": "pc.example.ts.net",
        "slot_max": "2",
        "harnesses": "{codex: codex-bot}",
        "workdir_root": "/home/work",
        **fields,
    }
    lines = [f"  - name: {values.pop('name')}"]
    lines.extend(f"    {key}: {value}" for key, value in values.items() if value != "-")
    return "\n".join(lines) + "\n"


def test_shipped_machines_yml_has_box_pc_and_mac_with_placeholders_reported() -> None:
    strict, strict_findings = load_machines_yml(ROOT / "machines.yml")
    assert strict is None
    assert errors(strict_findings) == [
        "devices[1].workdir_root is an install placeholder; "
        "Max must provide this inventory",
        "devices[2].workdir_root is an install placeholder; "
        "Max must provide this inventory",
    ]

    config, findings = load_machines_yml(
        ROOT / "machines.yml",
        allow_placeholders=True,
    )

    assert config is not None
    assert errors(findings) == []
    assert warnings(findings) == [
        "device 'pc' is disabled (enabled: false); it will not be scheduled",
        "accepted placeholder devices[1].workdir_root; device 'pc' will not be scheduled",
        "device 'mac' is disabled (enabled: false); it will not be scheduled",
        "accepted placeholder devices[2].workdir_root; device 'mac' will not be scheduled",
    ]
    box, pc, mac = config.devices
    assert (box.name, box.local, box.enabled, box.slot_max) == ("box", True, True, 3)
    assert dict(box.harnesses) == {
        "claude": "claude-bot",
        "codex": "codex-bot",
        "cursor": "cursor-bot",
    }
    assert box.tailscale_name is None
    assert box.schedulable
    assert pc.tailscale_name == "pc-wsl-bots.tail68624b.ts.net"
    assert (pc.slot_max, pc.enabled, pc.workdir_root) == (3, False, None)
    assert dict(pc.harnesses) == dict(box.harnesses)
    assert mac.tailscale_name == "maxs-macbook-pro.tail68624b.ts.net"
    assert (mac.slot_max, mac.enabled, dict(mac.harnesses)) == (2, False, {})
    assert not pc.schedulable and not mac.schedulable


def test_example_passes_strict_check() -> None:
    config, findings = load_machines_yml(ROOT / "config" / "machines.example.yml")

    assert findings == []
    assert config is not None
    assert [device.name for device in config.devices] == ["box"]


def test_enabled_defaults_true_and_remote_device_schedules(tmp_path: Path) -> None:
    config, findings = load_machines_yml(write(tmp_path, "devices:\n" + remote()))

    assert findings == []
    assert config is not None
    (pc,) = config.devices
    assert pc.enabled and pc.schedulable
    assert pc.eligible("codex")
    assert not pc.eligible("claude")
    assert pc.workdir_root == "/home/work"


def test_disabled_device_is_reported_and_never_eligible(tmp_path: Path) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, "devices:\n" + remote(enabled="false"))
    )

    assert config is not None
    assert errors(findings) == []
    assert warnings(findings) == [
        "device 'pc' is disabled (enabled: false); it will not be scheduled"
    ]
    assert not config.devices[0].eligible("codex")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tailscale_name", "REPLACE_WITH_PC_TAILSCALE_NAME"),
        ("slot_max", "REPLACE_WITH_PC_SLOT_MAX"),
        ("harnesses", "REPLACE_WITH_PC_HARNESSES"),
        ("workdir_root", "REPLACE_WITH_PC_WORKDIR_ROOT"),
    ],
)
def test_placeholder_is_refused_unless_allowed_and_then_never_eligible(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    path = write(tmp_path, "devices:\n" + remote(**{field: value}))

    strict, strict_findings = load_machines_yml(path)
    assert strict is None
    assert errors(strict_findings) == [
        f"devices[0].{field} is an install placeholder; Max must provide this inventory"
    ]

    config, findings = load_machines_yml(path, allow_placeholders=True)
    assert config is not None
    assert warnings(findings) == [
        f"accepted placeholder devices[0].{field}; device 'pc' will not be scheduled"
    ]
    device = config.devices[0]
    assert device.placeholder
    assert not device.schedulable
    for harness in ("claude", "codex", "cursor"):
        assert not device.eligible(harness)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{}\n", "missing required field 'devices'"),
        ("devices: []\n", "devices must list at least one device"),
        ("devices:\n" + BOX + "extra: 1\n", "unknown field 'extra'"),
        ("devices:\n" + remote(port="22"), "unknown field 'devices[0].port'"),
        ("devices:\n" + remote(name="PC"), "devices[0].name must be a lowercase"),
        ("devices:\n" + remote(slot_max="0"), "devices[0].slot_max must be a positive"),
        ("devices:\n" + remote(slot_max="true"), "devices[0].slot_max must be a positive"),
        ("devices:\n" + remote(slot_max="-"), "missing required field 'devices[0].slot_max'"),
        ("devices:\n" + remote(enabled="yes please"), "devices[0].enabled must be true or false"),
        (
            "devices:\n" + remote(tailscale_name="-"),
            "missing required field 'devices[0].tailscale_name'",
        ),
        (
            "devices:\n" + remote(tailscale_name="100.64.0.1"),
            "devices[0].tailscale_name must be a Tailscale device name",
        ),
        (
            "devices:\n" + remote(tailscale_name="ssh://pc"),
            "devices[0].tailscale_name must be a Tailscale device name",
        ),
        (
            "devices:\n" + remote(harnesses="{grok: grok-bot}"),
            "devices[0].harnesses has unknown harness 'grok'",
        ),
        (
            "devices:\n" + remote(harnesses="{claude: codex-bot}"),
            "devices[0].harnesses.claude must be 'claude-bot'",
        ),
        (
            "devices:\n" + remote(harnesses="{claude: max}"),
            "devices[0].harnesses.claude must be 'claude-bot'",
        ),
        ("devices:\n" + remote(harnesses="[claude]"), "devices[0].harnesses must map"),
        ("devices:\n" + remote(harnesses="{}"), "devices[0].harnesses is empty"),
        ("devices:\n" + remote(harnesses="-"), "missing required field 'devices[0].harnesses'"),
        (
            "devices:\n" + remote(workdir_root="-"),
            "missing required field 'devices[0].workdir_root'",
        ),
        (
            "devices:\n" + BOX + "    tailscale_name: box.example.ts.net\n",
            "devices[0].tailscale_name must be omitted on the local device",
        ),
        (
            "devices:\n" + BOX + BOX.replace("box", "box2"),
            "only one device may be local",
        ),
        ("devices:\n" + remote() + remote(), "duplicate device 'pc'"),
        ("devices:\n  - name: box\n    name: box\n", "duplicate key"),
    ],
)
def test_schema_errors(tmp_path: Path, text: str, message: str) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, text),
        allow_placeholders=True,
    )

    assert config is None
    assert any(message in item for item in errors(findings)), findings


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("/mnt/c/Users/max/work", "must not be under /mnt/"),
        ("/mnt", "must not be under /mnt/"),
        ("/mnt/wsl", "must not be under /mnt/"),
        ("//mnt/c", "must be a normalized directory"),
        ("/home/../mnt/c", "must not contain '.' or '..'"),
        ("/home/work/", "must be a normalized directory"),
        ("/home//work", "must be a normalized directory"),
        ("/", "must be a normalized directory"),
        ("home/work", "must be an absolute POSIX path"),
        ("C:\\\\work", "must be an absolute POSIX path"),
        ("~/work", "must be an absolute POSIX path"),
    ],
)
def test_workdir_root_stays_off_mounted_windows_drives(
    tmp_path: Path,
    value: str,
    message: str,
) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, "devices:\n" + remote(workdir_root=f'"{value}"'))
    )

    assert config is None
    assert any(
        item.startswith("devices[0].workdir_root ") and message in item
        for item in errors(findings)
    ), findings


def test_workdir_root_allows_a_linux_home_path(tmp_path: Path) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, "devices:\n" + remote(workdir_root="/home/mnt-work"))
    )

    assert findings == []
    assert config is not None
    assert config.devices[0].workdir_root == "/home/mnt-work"


def test_missing_file_and_non_mapping(tmp_path: Path) -> None:
    assert errors(load_machines_yml(tmp_path / "missing.yml")[1]) == ["file not found"]
    assert errors(load_machines_yml(write(tmp_path, "- box\n"))[1]) == [
        "machines.yml must be a mapping"
    ]
