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


def test_shipped_machines_yml_has_box_pc_and_mac_with_placeholders_reported() -> None:
    strict, strict_findings = load_machines_yml(ROOT / "machines.yml")
    assert strict is None
    assert errors(strict_findings) == [
        "devices[2].workdir_root is an install placeholder; "
        "Max must provide this inventory",
        "devices[2].ssh_alias is an install placeholder; "
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
        "device 'mac' is disabled (enabled: false); it will not be scheduled",
        "accepted placeholder devices[2].workdir_root; device 'mac' will not be scheduled",
        "accepted placeholder devices[2].ssh_alias; device 'mac' will not be scheduled",
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
    assert (pc.slot_max, pc.enabled) == (3, False)
    assert pc.workdir_root == "/home/{bot}/factory-work"
    assert pc.ssh_alias == "pc-wsl-{bot}"
    assert not pc.placeholder
    assert dict(pc.harnesses) == dict(box.harnesses)
    assert mac.tailscale_name == "maxs-macbook-pro.tail68624b.ts.net"
    assert (mac.slot_max, mac.enabled, dict(mac.harnesses)) == (2, False, {})
    assert not pc.schedulable and not mac.schedulable


def test_shipped_pc_workdir_is_each_bots_home_off_the_windows_drive() -> None:
    """Factory decided (technical), Nov 11: /home/<bot>/factory-work on
    pc-wsl-bots, never under /mnt/c."""
    config, _ = load_machines_yml(ROOT / "machines.yml", allow_placeholders=True)
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
    assert box is not None and box.workdir_for("claude") is None


def test_shipped_pc_and_mac_stay_disabled_until_f2_02_remote_start() -> None:
    """Factory decided (technical), Nov 11, kept by F2-02 (Nov 12): both
    remote devices ship enabled: false until Max turns them on, so neither
    is ever eligible for any harness."""
    config, findings = load_machines_yml(ROOT / "machines.yml", allow_placeholders=True)
    assert config is not None
    for name in ("pc", "mac"):
        device = config.device(name)
        assert device is not None
        assert device.enabled is False, name
        assert not device.schedulable, name
        for harness in ("claude", "codex", "cursor"):
            assert not device.eligible(harness), (name, harness)
        assert (
            f"device {name!r} is disabled (enabled: false); it will not be scheduled"
            in warnings(findings)
        )
    assert [d.name for d in config.devices if d.schedulable] == ["box"]


def test_shipped_machines_yml_names_the_pc_ssh_aliases_and_nothing_secret() -> None:
    text = (ROOT / "machines.yml").read_text(encoding="utf-8")
    for alias in ("pc-wsl-claude-bot", "pc-wsl-codex-bot", "pc-wsl-cursor-bot"):
        assert alias in text
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


def test_shipped_box_slot_max_states_its_real_basis() -> None:
    """A2: the box keeps 3 slots because the box runs at most 3 heavy local
    agent sessions at once. Slots are generic (3 claude jobs can take all
    3), so "one per harness bot" is not the reason and must not be stated."""
    config, _ = load_machines_yml(ROOT / "machines.yml", allow_placeholders=True)
    assert config is not None
    box = config.device("box")
    assert box is not None
    assert box.slot_max == 3

    machines = (ROOT / "machines.yml").read_text(encoding="utf-8")
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for text in (machines, spec, readme):
        assert "one per harness bot" not in text
        assert "one slot per Phase 1 owning bot" not in text.replace("\n    # ", " ")
    box_comment = machines.split("- name: box", 1)[1].split("slot_max: 3", 1)[0]
    assert "3 heavy local" in box_comment
    assert "generic" in box_comment
    assert "OWNING_BOTS" not in box_comment
    assert "at most 3 heavy local" in spec
    assert "at most 3 heavy local" in readme


def test_example_passes_strict_check() -> None:
    config, findings = load_machines_yml(ROOT / "config" / "machines.example.yml")

    assert findings == []
    assert config is not None
    assert [device.name for device in config.devices] == ["box"]


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
def test_placeholder_is_refused_unless_allowed_and_then_never_eligible(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    path = write(tmp_path, "orch_host: pc\ndevices:\n" + remote(**{field: value}))

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
    box, pc = config.device("box"), config.device("pc")
    assert (box.ssh_user, pc.ssh_user) == ("box", None)
    assert box.login_for("claude") == "box"
    assert pc.login_for("codex") == "codex-bot"


def test_shipped_machines_yml_derives_orch_host_from_its_local_device() -> None:
    config, _ = load_machines_yml(ROOT / "machines.yml", allow_placeholders=True)

    assert config is not None
    assert orch_host(config) == "box"


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
