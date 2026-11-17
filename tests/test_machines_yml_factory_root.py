from pathlib import Path

import pytest

from lib.machines_yml import load_machines_yml

ROOT = Path(__file__).resolve().parents[1]

BOX = """\
  - name: box
    local: true
    slot_max: 2
    harnesses:
      claude: claude-bot
{box_extra}"""

PC = """\
  - name: pc
    tailscale_name: pc-wsl-bots.example.ts.net
    slot_max: 3
    harnesses:
      claude: claude-bot
      codex: codex-bot
    workdir_root: /home/{{bot}}/factory-work
    ssh_alias: pc-wsl-{{bot}}
{pc_extra}"""


def _load(tmp_path, text):
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    return load_machines_yml(path)


def _two_devices(tmp_path, *, box_extra="", pc_extra=""):
    text = "devices:\n" + BOX.format(box_extra=box_extra) + PC.format(pc_extra=pc_extra)
    return _load(tmp_path, text)


def _errors(findings):
    return [item.message for item in findings if item.severity == "error"]


def test_remote_device_without_factory_root_is_refused(tmp_path):
    config, findings = _two_devices(tmp_path)
    assert config is None
    assert _errors(findings) == [
        "missing required field 'devices[1].factory_root' (every remote device "
        "names its deployed factory checkout, not workdir_root)"
    ]


def test_remote_device_with_factory_root_loads(tmp_path):
    config, findings = _two_devices(
        tmp_path, pc_extra="    factory_root: /home/{bot}/factory\n"
    )
    assert _errors(findings) == []
    assert config.device("pc").factory_root == "/home/{bot}/factory"


@pytest.mark.parametrize(
    "box_extra, expected",
    [("", None), ("    factory_root: /home/box/factory\n", "/home/box/factory")],
)
def test_local_device_factory_root_is_optional(tmp_path, box_extra, expected):
    config, findings = _two_devices(
        tmp_path,
        box_extra=box_extra,
        pc_extra="    factory_root: /home/{bot}/factory\n",
    )
    assert _errors(findings) == []
    assert config.device("box").factory_root == expected
    assert config.factory_root("box", "claude") == expected


def test_placeholder_factory_root_skips_the_device_with_a_warning(tmp_path):
    config, findings = _two_devices(
        tmp_path, pc_extra="    factory_root: REPLACE_WITH_PC_FACTORY_ROOT\n"
    )
    assert _errors(findings) == []
    assert [item.message for item in findings if item.severity == "warning"] == [
        "skipping device 'pc': devices[1].factory_root is an install placeholder; "
        "it will not be scheduled"
    ]
    pc = config.device("pc")
    assert pc.placeholders == ("factory_root",)
    assert not pc.schedulable
    assert pc.factory_root is None
    assert config.factory_root("pc", "claude") is None


@pytest.mark.parametrize(
    "value, problem",
    [
        ("home/bot/factory", "must be an absolute POSIX path"),
        ("42", "must be an absolute POSIX path"),
        ("/mnt/c/factory", "must not be under /mnt/"),
        ("/home/../factory", "must not contain '.' or '..'"),
        ("/home/bot/factory/", "must be a normalized directory below /"),
        ("/home/x{bot}/factory", "'{bot}' must be a whole path component"),
        ("/home/{user}/factory", "only '{bot}' may appear in braces"),
    ],
)
def test_bad_factory_root_is_refused(tmp_path, value, problem):
    config, findings = _two_devices(
        tmp_path, pc_extra=f"    factory_root: '{value}'\n"
    )
    assert config is None
    [message] = _errors(findings)
    assert message.startswith("devices[1].factory_root ")
    assert problem in message


def test_non_string_factory_root_is_refused(tmp_path):
    config, findings = _two_devices(tmp_path, pc_extra="    factory_root: 42\n")
    assert config is None
    assert _errors(findings) == [
        "devices[1].factory_root must be an absolute POSIX path"
    ]


@pytest.mark.parametrize(
    "value",
    [
        "/home/{bot}/factory-work",
        "/home/{bot}/factory-work/factory",
        "/home/codex-bot/factory-work/factory",
    ],
)
def test_factory_root_equal_to_or_inside_workdir_root_is_refused(tmp_path, value):
    config, findings = _two_devices(tmp_path, pc_extra=f"    factory_root: {value}\n")
    assert config is None
    assert _errors(findings) == [
        "devices[1].factory_root must not be workdir_root or inside it "
        "(it is the deployed factory checkout, not where jobs run)"
    ]


def test_factory_root_beside_workdir_root_with_shared_prefix_loads(tmp_path):
    config, findings = _two_devices(
        tmp_path, pc_extra="    factory_root: /home/{bot}/factory-workshop\n"
    )
    assert _errors(findings) == []
    assert config.factory_root("pc", "codex") == "/home/codex-bot/factory-workshop"


def test_factory_root_for_resolves_bot_per_harness(tmp_path):
    config, _ = _two_devices(
        tmp_path, pc_extra="    factory_root: /home/{bot}/factory\n"
    )
    pc = config.device("pc")
    assert pc.factory_root_for("claude") == "/home/claude-bot/factory"
    assert pc.factory_root_for("codex") == "/home/codex-bot/factory"
    assert pc.factory_root_for("cursor") is None


def test_helper_returns_factory_root_for_box_pc_and_mac(tmp_path):
    text = (
        "devices:\n"
        + BOX.format(box_extra="    factory_root: /home/box/factory\n")
        + PC.format(pc_extra="    factory_root: /home/{bot}/factory\n")
        + """\
  - name: mac
    tailscale_name: mac.example.ts.net
    slot_max: 2
    harnesses:
      claude: claude-bot
    workdir_root: /Users/{bot}/factory-work
    ssh_alias: mac-{bot}
    factory_root: /Users/{bot}/factory
"""
    )
    config, findings = _load(tmp_path, text)
    assert _errors(findings) == []
    assert config.factory_root("box", "claude") == "/home/box/factory"
    assert config.factory_root("pc", "claude") == "/home/claude-bot/factory"
    assert config.factory_root("mac", "claude") == "/Users/claude-bot/factory"
    assert config.factory_root("nowhere", "claude") is None


def test_repo_machines_yml_names_factory_roots():
    config, findings = load_machines_yml(ROOT / "machines.yml")
    assert _errors(findings) == []
    assert config.factory_root("box", "claude") == "/home/box/factory"
    assert config.factory_root("pc", "claude") == "/home/claude-bot/factory"
    assert config.device("mac").factory_root == "/Users/{bot}/factory"


def test_example_documents_factory_root_apart_from_workdir_root():
    path = ROOT / "config" / "machines.example.yml"
    config, findings = load_machines_yml(path)
    assert _errors(findings) == []
    assert config.factory_root("box", "claude") == "/home/box/factory"
    assert config.factory_root("pc", "claude") == "/home/claude-bot/factory"
    assert config.device("pc").workdir_for("claude") == "/home/claude-bot/factory-work"
    comments = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("#")
    ]
    assert "# factory_root is the deployed factory checkout (skills/, bin/orch), not" in comments
    assert "# workdir_root, and may not sit inside it." in comments


def test_a_factory_root_with_a_backtick_is_refused(tmp_path):
    config, findings = _two_devices(tmp_path, pc_extra="    factory_root: '/home/a`b/factory'\n")
    assert config is None
    [message] = _errors(findings)
    assert message.startswith("devices[1].factory_root ")
