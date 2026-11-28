import os
from pathlib import Path

import pytest

from lib.harness_env import ACCOUNT_ISOLATION
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
LEGACY = (
    "{bot, account} is deprecated, but keep {bot, account} until account placement "
    "(#1063 parts 2 and 3, then #1064) runs on every device, then move the account "
    "to accounts[].signed_in_on"
)


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

    assert (box.name, box.enabled, box.slot_max, box.ssh_user) == ("box", True, 6, None)
    assert box.tailscale_name == "grokbot-factory-box.tail68624b.ts.net"
    assert dict(box.harnesses) == bots
    assert (box.workdir_root, box.ssh_alias, box.factory_root) == (
        "/home/box/factory-work",
        "box-{bot}",
        "/home/box/factory",
    )
    assert {harness: box.login_for(harness) for harness in bots} == bots
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


def test_docs_place_an_unplaced_job_on_the_orch_host_not_the_box() -> None:
    spec = " ".join((ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8").split())
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    for text in (spec, readme):
        assert "listed under the box" not in text
        assert "one slot on the box" not in text
        assert "counted in the box's slots" not in text
    assert "a job with no placement is listed under the orch host" in spec
    assert "counts as one slot on the orch host" in spec
    assert "runs on the orch host (`orch_host` in `machines.yml`" in readme
    assert "refuses a placeholder unless `--allow-placeholders`" not in spec
    assert "skips a device with a placeholder" in spec


def test_shipped_docs_describe_the_login_the_shipped_box_selects() -> None:
    config, _ = load_machines_yml(ROOT / "machines.yml", overlay=False)
    box = config.device("box")
    logins = {box.login_for(harness) for harness in box.harnesses}
    assert box.ssh_user is None and logins == set(box.harnesses.values())

    def flat(path: str) -> str:
        return " ".join((ROOT / path).read_text(encoding="utf-8").split())

    readme_shipped = flat("README.md").split("The shipped file sets `orch_host: pc`", 1)[1]
    readme_shipped = readme_shipped.split("To stop new jobs on a device", 1)[0]
    spec_shipped = flat("docs/SPEC.md").split("The shipped file sets `orch_host: pc`", 1)[1]
    spec_shipped = spec_shipped.split("A device schedules only when", 1)[0]
    for shipped in (readme_shipped, spec_shipped):
        assert "ssh_user" not in shipped
        assert all(f"`{login}`" in shipped for login in logins)

    runbook = (ROOT / "docs" / "RUNBOOK-HARDENING.md").read_text(encoding="utf-8")
    hardening = runbook.split("\n## 3. ", 1)[1].split("\n## 4. ", 1)[0]
    assert "The shipped `machines.yml` now omits `ssh_user`" in " ".join(hardening.split())
    shared = [p for p in hardening.split("\n\n") if "`ssh_user: box`" in p]
    assert shared and all("legacy" in p.lower() for p in shared)


def test_example_passes_strict_check() -> None:
    config, findings = load_machines_yml(ROOT / "config" / "machines.example.yml")

    assert findings == []
    assert config is not None
    assert config.accounts_on("pc", "codex") == ("codex-default", "codex-pro-1")
    assert config.account_home("pc", "codex", "codex-pro-1") == (
        "/home/codex-bot/.factory-accounts/codex-pro-1"
    )
    for device in ("pc", "box"):
        assert config.accounts_on(device, "cursor") == ("cursor-ultra",)
        assert config.account_home(device, "cursor", "cursor-ultra") is None
    assert [a.id for a in config.external_accounts()] == ["cursor-grok-ultra"]
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


def test_drain_defaults_false_and_the_device_takes_new_jobs(tmp_path: Path) -> None:
    config, findings = load_machines_yml(write(tmp_path, "orch_host: pc\ndevices:\n" + remote()))

    assert findings == []
    assert config is not None
    (pc,) = config.devices
    assert pc.drain is False
    assert pc.takes_new("codex")
    assert not pc.takes_new("claude")


def test_test_suite_max_defaults_to_one(tmp_path: Path) -> None:
    config, findings = load_machines_yml(write(tmp_path, "orch_host: pc\ndevices:\n" + remote()))

    assert findings == []
    assert config is not None
    (pc,) = config.devices
    assert pc.test_suite_max == 1


def test_draining_device_stays_enabled_and_eligible_but_takes_no_new_jobs(
    tmp_path: Path,
) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, "orch_host: pc\ndevices:\n" + remote(drain="true"))
    )

    assert config is not None
    assert errors(findings) == []
    assert warnings(findings) == [
        "device 'pc' is draining (drain: true); "
        "it takes no new jobs and its running jobs finish"
    ]
    (pc,) = config.devices
    assert pc.drain is True
    assert pc.enabled is True, "drain is not disable"
    assert pc.schedulable and pc.eligible("codex"), "tokens, probes and deploys still reach it"
    assert not pc.takes_new("codex")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tailscale_name", "REPLACE_WITH_PC_TAILSCALE_NAME"),
        ("slot_max", "REPLACE_WITH_PC_SLOT_MAX"),
        ("harnesses", "REPLACE_WITH_PC_HARNESSES"),
        ("workdir_root", "REPLACE_WITH_PC_WORKDIR_ROOT"),
        ("home", "REPLACE_WITH_PC_HOME"),
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
        ("devices:\n" + remote(test_suite_max="0"), "devices[0].test_suite_max must be a positive"),
        ("devices:\n" + remote(test_suite_max="-1"), "devices[0].test_suite_max must be a positive"),
        ("devices:\n" + remote(test_suite_max="true"), "devices[0].test_suite_max must be a positive"),
        ("devices:\n" + remote(test_suite_max="many"), "devices[0].test_suite_max must be a positive"),
        ("devices:\n" + remote(enabled="yes please"), "devices[0].enabled must be true or false"),
        ("devices:\n" + remote(drain='"yes"'), "devices[0].drain must be true or false"),
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


def test_a_pipe_loads_and_a_directory_is_not_found(tmp_path: Path) -> None:
    read, written = os.pipe()
    os.write(written, f"orch_host: box\ndevices:{BOX}".encode())
    os.close(written)
    try:
        config, findings = load_machines_yml(Path(f"/dev/fd/{read}"))
    finally:
        os.close(read)
    assert errors(findings) == []
    assert [device.name for device in config.devices] == ["box"]
    assert errors(load_machines_yml(tmp_path)[1]) == ["file not found"]


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


ACCOUNTS = """
orch_host: pc
accounts:
  - {id: codex-a, harness: codex, plan: pro, label: "Codex Pro (A)"}
  - {id: codex-b, harness: codex}
  - {id: cursor-grok-ultra, harness: external, plan: ultra}
"""
SIGNED_IN = ACCOUNTS.replace('"Codex Pro (A)"}', '"Codex Pro (A)", signed_in_on: [pc]}').replace(
    "{id: codex-b, harness: codex}", "{id: codex-b, harness: codex, signed_in_on: [pc]}"
)


def test_shipped_machines_yml_maps_every_bot_to_its_default_account() -> None:
    config, findings = load_machines_yml(ROOT / "machines.yml", overlay=False)

    assert config is not None, findings
    for device in config.devices:
        assert {h: device.account_for(h) for h in device.harnesses} == {
            h: f"{h}-default" for h in device.harnesses
        }
    assert config.serving("codex") == frozenset({"codex-default"})


def test_a_file_without_accounts_gives_every_device_its_default_account(tmp_path: Path) -> None:
    text = "devices:\n" + BOX + remote(harnesses="{claude: claude-bot, codex: codex-bot}")

    config, findings = load_machines_yml(write(tmp_path, text))

    assert findings == []
    assert config is not None
    assert config.accounts_on("box", "claude") == ("claude-default",)
    assert config.accounts_on("pc", "codex") == ("codex-default",)
    assert config.accounts_on("box", "codex") == ()
    assert config.account_home("pc", "claude", "claude-default") is None
    assert config.serving("claude") == frozenset({"claude-default"})
    assert config.devices[1].home is None and config.devices[1].home_for("claude") is None


def test_bot_and_account_value_resolves_to_the_named_account(tmp_path: Path) -> None:
    text = (
        ACCOUNTS
        + "devices:\n"
        + remote(harnesses="{codex: {bot: codex-bot, account: codex-a}}")
        + remote(
            name="mac",
            tailscale_name="mac.example.ts.net",
            harnesses="{codex: {bot: codex-bot, account: codex-b}}",
        )
    )

    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is not None, findings
    assert warnings(findings) == [
        f"device 'pc' harness 'codex' names its login account inline; {LEGACY}",
        f"device 'mac' harness 'codex' names its login account inline; {LEGACY}",
    ]
    pc, mac = config.device("pc"), config.device("mac")
    assert (pc.harnesses["codex"], pc.account_for("codex")) == ("codex-bot", "codex-a")
    assert (mac.harnesses["codex"], mac.account_for("codex")) == ("codex-bot", "codex-b")
    assert config.account("codex-a").plan == "pro"
    assert config.account("codex-a").label == "Codex Pro (A)"
    assert config.serving("codex") == frozenset({"codex-a", "codex-b"})
    assert [a.id for a in config.external_accounts()] == ["cursor-grok-ultra"]
    assert config.accounts_on("pc", "codex") == ("codex-a",)
    assert config.account_home("pc", "codex", "codex-a") is None


def test_bare_bot_value_beside_accounts_is_the_default_account(tmp_path: Path) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, SIGNED_IN + "devices:\n" + remote(home="/home/{bot}"))
    )

    assert findings == []
    assert config is not None
    assert config.device("pc").account_for("codex") == "codex-default"
    assert config.account("codex-default").harness == "codex"


def test_draining_device_account_does_not_serve(tmp_path: Path) -> None:
    text = SIGNED_IN + "devices:\n" + remote(drain="true", home="/home/{bot}")

    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is not None, findings
    assert config.serving("codex") == frozenset()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "accounts:\n  - {id: codex-a, harness: codex}\n  - {id: codex-a, harness: codex}\n"
            "devices:\n" + remote(),
            "duplicate account 'codex-a'",
        ),
        *(
            (
                f"accounts:\n  - {{id: {account_id}, harness: codex}}\ndevices:\n" + remote(),
                "accounts[0].id must be 1-64 letters, digits, '.', '_' or '-'",
            )
            for account_id in ("max@example.com", '"."', '".."', "a/b", '"../x"', "/x")
        ),
        (
            "accounts:\n  - {id: codex-a, harness: grok}\ndevices:\n" + remote(),
            "accounts[0].harness must be one of claude, codex, cursor, gemini, external",
        ),
        (
            "accounts:\n  - {id: codex-a, harness: codex, token: x}\ndevices:\n" + remote(),
            "unknown field 'accounts[0].token'",
        ),
        (
            "accounts:\n  - {id: codex-a, harness: codex, plan: 7}\ndevices:\n" + remote(),
            "accounts[0].plan must be text",
        ),
        ("accounts: {codex-a: codex}\ndevices:\n" + remote(), "accounts must be a list"),
        (
            "devices:\n" + remote(harnesses="{codex: {bot: codex-bot, account: codex-z}}"),
            "devices[0].harnesses.codex names unknown account 'codex-z'",
        ),
        (
            "accounts:\n  - {id: claude-a, harness: claude}\ndevices:\n"
            + remote(harnesses="{codex: {bot: codex-bot, account: claude-a}}"),
            "devices[0].harnesses.codex names account 'claude-a' of harness 'claude'",
        ),
        (
            ACCOUNTS + "devices:\n"
            + remote(harnesses="{codex: {bot: codex-bot, account: cursor-grok-ultra}}"),
            "devices[0].harnesses.codex names account 'cursor-grok-ultra' of harness 'external'",
        ),
        (
            "devices:\n" + remote(harnesses="{codex: {bot: codex-bot, account: claude-default}}"),
            "devices[0].harnesses.codex names account 'claude-default' of harness 'claude'",
        ),
        (
            "devices:\n" + remote(harnesses="{codex: {bot: codex-bot, account: []}}"),
            "devices[0].harnesses.codex.account must be an account id",
        ),
        (
            "devices:\n" + remote(harnesses="{codex: {bot: codex-bot, account: {}}}"),
            "devices[0].harnesses.codex.account must be an account id",
        ),
        (
            "accounts:\n  - {id: codex-default, harness: claude}\ndevices:\n" + remote(),
            "devices[0].harnesses.codex names account 'codex-default' of harness 'claude'",
        ),
        (
            ACCOUNTS + "devices:\n"
            + remote(harnesses="{codex: {bot: codex-bot, account: codex-a, token: x}}"),
            "unknown field 'devices[0].harnesses.codex.token'",
        ),
        (
            ACCOUNTS + "devices:\n" + remote(harnesses="{codex: {account: codex-a}}"),
            "devices[0].harnesses.codex must be 'codex-bot'",
        ),
        (
            ACCOUNTS + "devices:\n"
            + remote(harnesses="{codex: {bot: claude-bot, account: codex-a}}"),
            "devices[0].harnesses.codex must be 'codex-bot'",
        ),
        (
            "devices:\n" + remote(name="external"),
            "devices[0].name 'external' is reserved for external usage readings",
        ),
    ],
)
def test_account_errors(tmp_path: Path, text: str, message: str) -> None:
    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is None
    assert any(message in item for item in errors(findings)), findings


def test_overlay_adds_an_account_by_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_NO_LOCAL_OVERLAY")
    path = write(
        tmp_path,
        SIGNED_IN
        + "devices:\n"
        + remote(harnesses="{claude: claude-bot, codex: codex-bot}", home="/home/{bot}"),
    )
    (tmp_path / "machines.local.yml").write_text(
        "accounts:\n  - {id: codex-a, plan: plus}\n"
        "  - {id: claude-a, harness: claude, signed_in_on: [pc]}\n",
        encoding="utf-8",
    )

    config, findings = load_machines_yml(path)

    assert config is not None, findings
    assert config.account("codex-a").plan == "plus"
    assert config.account("claude-a").harness == "claude"
    assert config.accounts_on("pc", "claude") == ("claude-default", "claude-a")


TWO_CLAUDE_ACCOUNTS = (
    "orch_host: pc\n"
    "accounts:\n"
    "  - {id: claude-a, harness: claude, signed_in_on: [pc, box]}\n"
    "  - {id: codex-a, harness: codex, signed_in_on: [pc]}\n"
    "  - {id: claude-b, harness: claude, signed_in_on: [mac], max_jobs: 2}\n"
    "devices:\n"
    + remote(harnesses="{claude: claude-bot, codex: codex-bot}", home="/home/{bot}")
    + remote(
        name="box",
        tailscale_name="box.example.ts.net",
        harnesses="{claude: claude-bot}",
        ssh_alias="box-{bot}",
        ssh_user="box",
        home="/home/box",
    )
    + remote(
        name="mac",
        tailscale_name="mac.example.ts.net",
        harnesses="{claude: claude-bot}",
        drain="true",
        home="/Users/{bot}",
    )
)


def test_accounts_signed_in_on_devices_run_beside_each_devices_own_login(
    tmp_path: Path,
) -> None:
    config, findings = load_machines_yml(write(tmp_path, TWO_CLAUDE_ACCOUNTS))

    assert config is not None, findings
    assert errors(findings) == []
    assert warnings(findings) == [
        "device 'mac' is draining (drain: true); "
        "it takes no new jobs and its running jobs finish"
    ]
    assert config.account("claude-b").max_jobs == 2
    assert config.account("claude-a").max_jobs is None
    assert config.account("claude-a").signed_in_on == frozenset({"pc", "box"})

    assert config.accounts_on("pc", "claude") == ("claude-default", "claude-a")
    assert config.accounts_on("box", "claude") == ("claude-default", "claude-a")
    assert config.accounts_on("mac", "claude") == ("claude-default", "claude-b")
    assert config.accounts_on("pc", "codex") == ("codex-default", "codex-a")
    assert config.accounts_on("box", "codex") == ()
    assert config.accounts_on("lab", "claude") == ()

    assert config.account_home("pc", "claude", "claude-default") is None
    assert config.account_home("pc", "claude", "claude-a") == (
        "/home/claude-bot/.factory-accounts/claude-a"
    )
    assert config.account_home("pc", "codex", "codex-a") == (
        "/home/codex-bot/.factory-accounts/codex-a"
    )
    assert config.account_home("box", "claude", "claude-a") == (
        "/home/box/.factory-accounts/claude-a"
    )
    assert config.account_home("mac", "claude", "claude-b") == (
        "/Users/claude-bot/.factory-accounts/claude-b"
    )
    assert config.device("mac").home_for("claude") == "/Users/claude-bot"
    assert config.device("mac").home_for("codex") is None
    for device, harness, account in (
        ("pc", "claude", "claude-b"),
        ("pc", "claude", "codex-a"),
        ("box", "codex", "codex-default"),
        ("lab", "claude", "claude-default"),
    ):
        with pytest.raises(ValueError):
            config.account_home(device, harness, account)

    assert config.serving("claude") == frozenset({"claude-default", "claude-a"})
    assert config.serving("codex") == frozenset({"codex-default", "codex-a"})


CURSOR_LOGIN = "{cursor: {bot: cursor-bot, account: cursor-ultra}}"


def test_cursor_account_is_each_devices_cursor_login(tmp_path: Path) -> None:
    text = (
        "orch_host: pc\naccounts:\n"
        '  - {id: cursor-ultra, harness: cursor, plan: ultra, label: "Cursor Ultra"}\n'
        "devices:\n"
        + "".join(
            remote(name=name, tailscale_name=f"{name}.example.ts.net", harnesses=CURSOR_LOGIN)
            for name in ("pc", "box", "mac")
        )
    )

    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is not None, findings
    assert findings == []
    for device in ("pc", "box", "mac"):
        assert config.accounts_on(device, "cursor") == ("cursor-ultra",)
        assert config.account_home(device, "cursor", "cursor-ultra") is None
    assert config.serving("cursor") == frozenset({"cursor-ultra"})


def signed_in(account: str, **device: str) -> str:
    fields = {"harnesses": "{claude: claude-bot}", "home": "/home/{bot}", **device}
    return (
        "orch_host: pc\naccounts:\n  - {id: claude-a, harness: claude"
        + (f", {account}" if account else "")
        + "}\ndevices:\n"
        + remote(**fields)
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        *(
            (
                signed_in(f"signed_in_on: [pc], max_jobs: {value}"),
                "accounts[0].max_jobs must be a positive integer",
            )
            for value in ("null", "~", "true", "0", "-1", '"2"', "1.5")
        ),
        *(
            (
                signed_in(f"signed_in_on: [pc], paused: {value}"),
                "accounts[0].paused must be true or false",
            )
            for value in ("null", "1", '"true"', "yes-please")
        ),
        *(
            (
                signed_in(f"signed_in_on: {value}"),
                "accounts[0].signed_in_on must list device names",
            )
            for value in ("pc", "[PC]", "[pc, pc]", "[3]", "{pc: 1}", "[[pc]]")
        ),
        (
            signed_in("signed_in_on: [lab]"),
            "accounts[0].signed_in_on names unknown device 'lab'",
        ),
        (
            signed_in("signed_in_on: [pc]", harnesses="{codex: codex-bot}"),
            "device 'pc' does not list harness 'claude'",
        ),
        (
            "orch_host: pc\naccounts:\n  - {id: grok, harness: external, signed_in_on: [pc]}\n"
            "devices:\n" + remote(home="/home/{bot}"),
            "an external account is not signed in on a device",
        ),
        (
            "orch_host: pc\naccounts:\n  - {id: cursor-a, harness: cursor, signed_in_on: [pc]}\n"
            "devices:\n" + remote(harnesses="{cursor: cursor-bot}", home="/home/{bot}"),
            "accounts[0].signed_in_on is not supported for a cursor account",
        ),
        (
            "orch_host: pc\naccounts:\n  - {id: cursor-ultra, harness: cursor, signed_in_on: [pc, box, mac]}\n"
            "devices:\n"
            + "".join(
                remote(name=name, tailscale_name=f"{name}.example.ts.net",
                       harnesses="{cursor: cursor-bot}", home="/home/{bot}")
                for name in ("pc", "box", "mac")
            ),
            "accounts[0].signed_in_on is not supported for a cursor account",
        ),
        (
            signed_in(
                "signed_in_on: [pc]",
                harnesses="{claude: {bot: claude-bot, account: claude-a}}",
            ),
            "accounts[0].signed_in_on names device 'pc', whose claude login is "
            "already 'claude-a'",
        ),
        (
            signed_in("signed_in_on: [pc]", home="-"),
            "devices[0].home is required: account 'claude-a' is signed in on it",
        ),
        (
            signed_in(""),
            "account 'claude-a' is not signed in on any device; list it in signed_in_on",
        ),
        (
            signed_in("signed_in_on: []"),
            "account 'claude-a' is not signed in on any device; list it in signed_in_on",
        ),
        (
            "accounts:\n  - {id: cursor-pro, harness: cursor}\ndevices:\n" + remote(),
            "account 'cursor-pro' is not signed in on any device; "
            "name it as a device's cursor login",
        ),
        (
            signed_in("signed_in_on: [pc]", ssh_user="box"),
            "devices[0].home may not use '{bot}' when ssh_user is set",
        ),
        (
            signed_in("signed_in_on: [pc]", home='"/mnt/c/Users/{bot}"'),
            "devices[0].home must not be under /mnt/",
        ),
        (
            signed_in("signed_in_on: [pc]", home="home/{bot}"),
            "devices[0].home must be an absolute POSIX path",
        ),
        (
            signed_in("signed_in_on: [pc]", home='"/home/x{bot}"'),
            "devices[0].home '{bot}' must be a whole path component",
        ),
    ],
)
def test_signed_in_on_errors(tmp_path: Path, text: str, message: str) -> None:
    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is None
    assert any(message in item for item in errors(findings)), findings


@pytest.mark.parametrize("field", ["home", "harnesses"])
def test_a_placeholder_device_may_be_named_in_signed_in_on(tmp_path: Path, field: str) -> None:
    text = signed_in("signed_in_on: [pc]", **{field: f"REPLACE_WITH_PC_{field.upper()}"})

    config, findings = load_machines_yml(write(tmp_path, text))

    assert config is not None, findings
    assert errors(findings) == []
    assert config.serving("claude") == frozenset()
    if field == "home":
        assert config.accounts_on("pc", "claude") == ("claude-default", "claude-a")
        with pytest.raises(ValueError):
            config.account_home("pc", "claude", "claude-a")


def test_signed_in_on_baseline_loads_clean(tmp_path: Path) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, signed_in("signed_in_on: [pc], max_jobs: 1"))
    )

    assert findings == []
    assert config is not None
    assert config.accounts_on("pc", "claude") == ("claude-default", "claude-a")


def test_an_account_is_unpaused_unless_its_entry_says_paused(tmp_path: Path) -> None:
    config, findings = load_machines_yml(
        write(tmp_path, signed_in("signed_in_on: [pc], paused: true").replace(
            "devices:", "  - {id: claude-b, harness: claude, signed_in_on: [pc], paused: false}\n"
            "  - {id: claude-c, harness: claude, signed_in_on: [pc]}\ndevices:", 1))
    )

    assert findings == []
    assert config is not None
    assert [(item.id, item.paused) for item in config.accounts] == [
        ("claude-a", True), ("claude-b", False), ("claude-c", False)
    ]
    assert config.account("claude-default").paused is False


@pytest.mark.parametrize("doc", ["README.md", "docs/SPEC.md"])
def test_docs_describe_usage_accounts_and_the_grok_routine_approval(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")

    for phrase in (
        "accounts:" if doc == "README.md" else "`accounts`",
        "orch usage migrate",
        "orch usage record",
        "signed_in_on",
        "max_jobs",
        "<home>/.factory-accounts/<id>",
        "lane_account_limits",
        "A scheduled Grok Bot routine that calls `orch usage record` is a separate step "
        "that needs Max's approval.",
    ):
        assert phrase in " ".join(text.split()), phrase


@pytest.mark.parametrize("doc", ["README.md", "docs/SPEC.md"])
def test_docs_describe_account_homes_and_the_token_variable_rule(doc: str) -> None:
    text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
    names = [name for isolation in ACCOUNT_ISOLATION.values() for name in (isolation.home, *isolation.tokens)]

    assert ".factory-accounts" in text
    assert [name for name in names if f"`{name}`" not in text] == []
    assert "skill-probe run --account" in text
    assert "Several bot users for one harness on one device" not in text


def test_the_account_homes_note_supersedes_the_772_scope_line() -> None:
    note = (ROOT / "docs/decisions/1184-account-homes.md").read_text(encoding="utf-8")

    assert "772-usage-accounts.md" in note
    for field in ("- **Decision:**", "- **When:**", "- **Source:**"):
        assert field in note


LEGACY_PREREQUISITE = (
    "keep {bot, account} until account placement (#1063 parts 2 and 3, then #1064) "
    "runs on every device"
)


def test_legacy_mapping_guidance_keeps_the_mapping_until_account_placement(
    tmp_path: Path,
) -> None:
    _config, findings = load_machines_yml(
        write(
            tmp_path,
            ACCOUNTS + "devices:\n"
            + remote(harnesses="{codex: {bot: codex-bot, account: codex-a}}"),
        )
    )
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    spec = " ".join((ROOT / "docs/SPEC.md").read_text(encoding="utf-8").split())

    assert any(LEGACY_PREREQUISITE in item for item in warnings(findings)), findings
    assert LEGACY_PREREQUISITE in readme
    assert LEGACY_PREREQUISITE in spec
    assert "use `signed_in_on` instead" not in readme
