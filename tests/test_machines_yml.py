from pathlib import Path

import pytest

from lib.machines_yml import DEFAULT_ORCH_HOST, load_machines_yml, orch_host
from tests.pool_helpers import write_target_machines

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
        "ssh_alias": "pc-{bot}",
        "factory_root": "/srv/factory",
        **fields,
    }
    lines = [f"  - name: {values.pop('name')}"]
    lines.extend(f"    {key}: {value}" for key, value in values.items() if value != "-")
    return "\n".join(lines) + "\n"


def test_shipped_machines_yml_runs_orch_on_pc_with_box_remote_and_mac_reported() -> None:
    config, findings = load_machines_yml(ROOT / "machines.yml")

    assert config is not None
    assert errors(findings) == []
    assert warnings(findings) == [
        "device 'mac' is disabled (enabled: false); it will not be scheduled",
        "skipping device 'mac': devices[2].workdir_root is an install placeholder; "
        "it will not be scheduled",
        "skipping device 'mac': devices[2].ssh_alias is an install placeholder; "
        "it will not be scheduled",
    ]
    assert orch_host(config) == "pc"
    assert not any(device.local for device in config.devices)
    pc, box, mac = config.devices
    bots = {"claude": "claude-bot", "codex": "codex-bot", "cursor": "cursor-bot"}

    assert (pc.name, pc.enabled, pc.slot_max, pc.ssh_user) == ("pc", True, 10, None)
    assert pc.tailscale_name == "pc-wsl-bots.tail68624b.ts.net"
    assert dict(pc.harnesses) == bots
    assert (pc.workdir_root, pc.ssh_alias, pc.factory_root) == (
        "/home/{bot}/factory-work",
        "pc-wsl-{bot}",
        "/home/{bot}/factory",
    )
    assert pc.login_for("codex") == "codex-bot"

    assert (box.name, box.enabled, box.slot_max, box.ssh_user) == ("box", True, 6, "box")
    assert box.tailscale_name == "grokbot-factory-box.tail68624b.ts.net"
    assert dict(box.harnesses) == bots
    assert (box.workdir_root, box.ssh_alias, box.factory_root) == (
        "/home/box/factory-work",
        "box-{bot}",
        "/home/box/factory",
    )
    assert {harness: box.login_for(harness) for harness in bots} == dict.fromkeys(bots, "box")
    assert {harness: box.ssh_alias_for(harness) for harness in bots} == {
        "claude": "box-claude-bot",
        "codex": "box-codex-bot",
        "cursor": "box-cursor-bot",
    }
    assert pc.schedulable and box.schedulable

    assert mac.tailscale_name == "maxs-macbook-pro.tail68624b.ts.net"
    assert (mac.name, mac.slot_max, mac.enabled, dict(mac.harnesses)) == ("mac", 2, False, {})
    assert not mac.schedulable


def test_shipped_machines_yml_header_describes_orch_host_and_ssh_user() -> None:
    header = (ROOT / "machines.yml").read_text(encoding="utf-8").split("\ndevices:", 1)[0]
    comment = " ".join(line for line in header.splitlines() if line.startswith("#"))
    assert "orch_host names the device that runs orch" in comment
    assert "ssh_user" in comment and "identity" in comment
    assert "Phase 1" not in header


def test_shipped_pc_workdir_is_each_bots_home_off_the_windows_drive() -> None:
    """Factory decided (technical), Nov 11: /home/<bot>/factory-work on
    pc-wsl-bots, never under /mnt/c."""
    config, _ = load_machines_yml(ROOT / "machines.yml")
    assert config is not None
    pc = config.device("pc")
    assert pc is not None
    assert {harness: pc.workdir_for(harness) for harness in pc.harnesses} == {
        "claude": "/home/claude-bot/factory-work",
        "codex": "/home/codex-bot/factory-work",
        "cursor": "/home/cursor-bot/factory-work",
    }
    for harness in pc.harnesses:
        assert not pc.workdir_for(harness).startswith("/mnt")
    box = config.device("box")
    assert box is not None and box.workdir_for("claude") == "/home/box/factory-work"


def test_shipped_mac_stays_disabled_and_only_pc_and_box_schedule() -> None:
    config, findings = load_machines_yml(ROOT / "machines.yml")
    assert config is not None
    mac = config.device("mac")
    assert mac is not None
    assert mac.enabled is False
    for harness in ("claude", "codex", "cursor"):
        assert not mac.eligible(harness), harness
    assert (
        "device 'mac' is disabled (enabled: false); it will not be scheduled"
        in warnings(findings)
    )
    assert [d.name for d in config.devices if d.schedulable] == ["pc", "box"]


def test_shipped_machines_yml_names_nothing_secret() -> None:
    text = (ROOT / "machines.yml").read_text(encoding="utf-8")
    for secret_hint in ("IdentityFile", ".ssh/", "id_ed25519", "id_rsa", "2222"):
        assert secret_hint not in text


def test_workdir_root_bot_token_resolves_to_each_harness_bot(tmp_path: Path) -> None:
    config, findings = load_machines_yml(
        write(
            tmp_path,
            "orch_host: pc\ndevices:\n"
            + remote(
                harnesses="{claude: claude-bot, codex: codex-bot}",
                workdir_root="/home/{bot}/factory-work",
            ),
        )
    )

    assert findings == []
    assert config is not None
    (pc,) = config.devices
    assert pc.workdir_root == "/home/{bot}/factory-work"
    assert pc.workdir_for("claude") == "/home/claude-bot/factory-work"
    assert pc.workdir_for("codex") == "/home/codex-bot/factory-work"
    assert pc.workdir_for("cursor") is None


def test_workdir_root_without_token_is_shared_by_every_harness(tmp_path: Path) -> None:
    config, _ = load_machines_yml(write(tmp_path, "orch_host: pc\ndevices:\n" + remote()))
    assert config is not None
    assert config.devices[0].workdir_for("codex") == "/home/work"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("/home/{user}/w", "only '{bot}' may appear in braces"),
        ("/home/x{bot}/w", "'{bot}' must be a whole path component"),
        ("/home/{bot}{bot}/w", "'{bot}' must be a whole path component"),
        ("/home/{bot/w", "only '{bot}' may appear in braces"),
        ("/mnt/{bot}/w", "must not be under /mnt/"),
        ("/mnt/c/Users/{bot}", "must not be under /mnt/"),
        ("{bot}/w", "must be an absolute POSIX path"),
        ("/home/{bot}/../w", "must not contain '.' or '..'"),
    ],
)
def test_workdir_root_bot_token_rules(tmp_path: Path, value: str, message: str) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, "devices:\n" + remote(workdir_root=f'"{value}"'))
    )

    assert config is None
    assert any(
        item.startswith("devices[0].workdir_root ") and message in item
        for item in errors(findings)
    ), findings


def test_shipped_slots_are_never_described_as_one_per_bot() -> None:
    machines = (ROOT / "machines.yml").read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for text in (machines, spec, readme):
        assert "one per harness bot" not in text
        assert "one slot per Phase 1 owning bot" not in text.replace("\n    # ", " ")
    assert "OWNING_BOTS" not in machines
    assert "slots are generic" in readme.replace("\n", " ").lower()


def test_example_passes_strict_check() -> None:
    config, findings = load_machines_yml(ROOT / "config" / "machines.example.yml")

    assert findings == []
    assert config is not None
    assert orch_host(config) == "pc"
    assert [device.name for device in config.devices] == ["pc", "box"]
    assert not any(device.local for device in config.devices)
    box = config.device("box")
    assert box.ssh_user == "box"
    assert box.login_for("codex") == "box"
    assert box.ssh_alias_for("codex") == "box-codex-bot"
    assert box.harnesses["codex"] == "codex-bot"


def test_enabled_defaults_true_and_remote_device_schedules(tmp_path: Path) -> None:
    config, findings = load_machines_yml(write(tmp_path, "orch_host: pc\ndevices:\n" + remote()))

    assert findings == []
    assert config is not None
    (pc,) = config.devices
    assert pc.enabled and pc.schedulable
    assert pc.eligible("codex")
    assert not pc.eligible("claude")
    assert pc.workdir_root == "/home/work"


def test_disabled_device_is_reported_and_never_eligible(tmp_path: Path) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, "orch_host: pc\ndevices:\n" + remote(enabled="false"))
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
def test_placeholder_device_loads_with_a_warning_and_is_never_eligible(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    path = write(tmp_path, "orch_host: pc\ndevices:\n" + remote(**{field: value}))

    config, findings = load_machines_yml(path)
    assert config is not None
    assert errors(findings) == []
    assert warnings(findings) == [
        f"skipping device 'pc': devices[0].{field} is an install placeholder; "
        "it will not be scheduled"
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
    config, findings = load_machines_yml(write(tmp_path, text))

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
        write(tmp_path, "orch_host: pc\ndevices:\n" + remote(workdir_root="/home/mnt-work"))
    )

    assert findings == []
    assert config is not None
    assert config.devices[0].workdir_root == "/home/mnt-work"


def test_missing_file_and_non_mapping(tmp_path: Path) -> None:
    assert errors(load_machines_yml(tmp_path / "missing.yml")[1]) == ["file not found"]
    assert errors(load_machines_yml(write(tmp_path, "- box\n"))[1]) == [
        "machines.yml must be a mapping"
    ]


@pytest.mark.parametrize(
    "second",
    ["pc.example.ts.net", "PC.Example.TS.net"],
)
def test_two_devices_may_not_share_a_tailscale_name(tmp_path: Path, second: str) -> None:
    """A1: one physical host has one slot budget. Two entries for the same
    Tailscale name (case-insensitive) would double it."""
    text = (
        "devices:\n"
        + remote(name="pc-a", slot_max="1", harnesses="{claude: claude-bot}")
        + remote(
            name="pc-b",
            tailscale_name=second,
            slot_max="1",
            harnesses="{claude: claude-bot}",
        )
    )

    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is None
    assert any(
        "pc-a" in item and "pc-b" in item and "tailscale_name" in item
        for item in errors(findings)
    ), findings


def test_a_disabled_duplicate_tailscale_name_is_also_refused(tmp_path: Path) -> None:
    text = "devices:\n" + remote(name="pc-a") + remote(name="pc-b", enabled="false")

    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is None
    assert any("pc-a" in item and "pc-b" in item for item in errors(findings)), findings


def test_distinct_tailscale_names_are_accepted(tmp_path: Path) -> None:
    text = "orch_host: pc-a\ndevices:\n" + remote(name="pc-a") + remote(
        name="pc-b", tailscale_name="pc-b.example.ts.net"
    )

    config, findings = load_machines_yml(write(tmp_path, text))

    assert findings == []
    assert config is not None


@pytest.mark.parametrize(
    "value",
    [
        "/home/work\\0tail",
        "/home/work\\ttail",
        "/home/work\\ntail",
        "/home/work\\x7ftail",
        "/home/work\\x1btail",
        "/home/work\\x85tail",
    ],
)
def test_workdir_root_rejects_nul_and_control_characters(
    tmp_path: Path,
    value: str,
) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, "devices:\n" + remote(workdir_root=f'"{value}"'))
    )

    assert config is None
    assert any(
        item.startswith("devices[0].workdir_root ") and "control character" in item
        for item in errors(findings)
    ), findings


def test_target_shape_names_pc_as_orch_host_and_box_logs_in_as_box(tmp_path: Path) -> None:
    config, findings = load_machines_yml(write_target_machines(tmp_path))

    assert errors(findings) == [], findings
    assert config is not None
    assert orch_host(config) == "pc"
    assert not any(device.local for device in config.devices)
    assert [device.name for device in config.devices] == ["pc", "box", "mac"]
    box, pc, mac = config.device("box"), config.device("pc"), config.device("mac")
    assert (pc.enabled, pc.slot_max) == (True, 10)
    assert (box.enabled, box.slot_max) == (True, 6)
    assert not mac.enabled
    assert box.tailscale_name
    assert (box.ssh_alias, box.workdir_root, box.factory_root) == (
        "box-{bot}",
        "/home/box/factory-work",
        "/home/box/factory",
    )
    assert (box.ssh_user, pc.ssh_user) == ("box", None)
    assert box.login_for("claude") == "box"
    assert pc.login_for("codex") == "codex-bot"


def test_orch_host_is_derived_from_the_one_local_device(tmp_path: Path) -> None:
    config, findings = load_machines_yml(write(tmp_path, "devices:\n" + BOX + remote()))

    assert errors(findings) == []
    assert orch_host(config) == "box"


def test_orch_host_falls_back_to_box_only_without_a_config() -> None:
    assert orch_host(None) == DEFAULT_ORCH_HOST == "box"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("orch_host: lab\ndevices:\n" + remote(), "orch_host 'lab' is not a listed device"),
        ("orch_host: pc\ndevices:\n" + BOX + remote(), "local device 'box' must be orch_host"),
        ("devices:\n" + remote(), "orch_host is required when no device is local"),
        ("orch_host: [pc]\ndevices:\n" + remote(), "orch_host must be a device name"),
        ("orch_host: 3\ndevices:\n" + remote(), "orch_host must be a device name"),
        (
            "devices:\n" + BOX + "    ssh_user: box\n",
            "devices[0].ssh_user must be omitted on the local device",
        ),
        ("orch_host: pc\ndevices:\n" + remote(ssh_user="Box"), "devices[0].ssh_user must be"),
        ("orch_host: pc\ndevices:\n" + remote(ssh_user="box@pc"), "devices[0].ssh_user must be"),
        ("orch_host: pc\ndevices:\n" + remote(ssh_user="-oProxy"), "devices[0].ssh_user must be"),
        ("orch_host: pc\ndevices:\n" + remote(ssh_user="a" * 33), "devices[0].ssh_user must be"),
        ("orch_host: pc\ndevices:\n" + remote(ssh_user="7"), "devices[0].ssh_user must be"),
    ],
)
def test_orch_host_and_ssh_user_errors(tmp_path: Path, text: str, message: str) -> None:
    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is None
    assert any(message in item for item in errors(findings)), findings
