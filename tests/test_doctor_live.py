import ast
import shlex
from pathlib import Path

import pytest

from lib.harnesses import all_drivers
from lib.machines_yml import load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.pstack_models import rules_text
from lib.pstack_pin import (
    RULES_PATH,
    ROOT,
    canary_prompt_for,
    install_skills,
    installed_errors,
    load_pin,
    pinned_token,
    remove_claude_extras,
    score_reply,
)
from lib.skill_probe import launch_argv, plan_canary
from tests.test_pstack_install import snapshot

PSTACK = ROOT / "skills" / "pstack"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, "local", rules=rules_text(ROOT)) == []
    assert installed_errors(ROOT, home) == []
    return home


def install(home: Path) -> str:
    return f"bin/orch install-skills --home {shlex.quote(str(home))}"


def run_fix(home: Path) -> str:
    return f"run {install(home)}"


def remove_then_install(home: Path) -> str:
    return f"run {install(home)} --remove-claude-extras, then {install(home)}"


def move_aside(home: Path) -> str:
    return f"move it aside, then {run_fix(home)}"


def move_blocker(home: Path, blocker: Path) -> str:
    return f"move {blocker} aside, then {run_fix(home)}"


def flagged(home: Path) -> list[str]:
    before = snapshot(home)
    errors = installed_errors(ROOT, home)
    assert snapshot(home) == before, "the doctor wrote to the home"
    return errors


def install_fixes(home: Path) -> None:
    assert install_skills(ROOT, home, "local", rules=rules_text(ROOT)) == []
    assert installed_errors(ROOT, home) == []


def test_a_stale_rules_file_is_flagged_with_the_install_command(home: Path) -> None:
    rules = home / RULES_PATH
    rules.write_text(rules_text(ROOT).splitlines(keepends=True)[0] + "feature, refactoring: sonnet low\n")
    assert flagged(home) == [f"claude: did not load the pinned version ({rules} is stale); {run_fix(home)}"]
    install_fixes(home)


def test_a_missing_rules_file_is_flagged(home: Path) -> None:
    rules = home / RULES_PATH
    rules.unlink()
    assert flagged(home) == [f"claude: did not load the pinned version ({rules} is missing); {run_fix(home)}"]
    install_fixes(home)


def test_a_symlinked_rules_file_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    rules = home / RULES_PATH
    elsewhere = tmp_path / "pstack-models.md"
    elsewhere.write_text(rules_text(ROOT), encoding="utf-8")
    rules.unlink()
    rules.symlink_to(elsewhere)
    assert flagged(home) == [f"claude: did not load the pinned version ({rules} is a symlink); {move_aside(home)}"]
    assert rules.is_symlink()


def test_a_rules_file_orch_did_not_write_must_be_moved_aside(home: Path) -> None:
    rules = home / RULES_PATH
    rules.write_text("my own model rule\n", encoding="utf-8")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({rules} was not written by orch); {move_aside(home)}"
    ]
    assert rules.read_text(encoding="utf-8") == "my own model rule\n"


def test_a_removed_agent_link_is_flagged_as_missing(home: Path) -> None:
    agent = home / ".claude" / "agents" / "poteto-agent.md"
    agent.unlink()
    assert flagged(home) == [f"claude: did not load the pinned version ({agent} is missing); {run_fix(home)}"]
    install_fixes(home)


def test_a_dangling_docs_link_is_flagged(home: Path, tmp_path: Path) -> None:
    doc = home / ".claude" / "docs" / "harness" / "claude-code.md"
    doc.unlink()
    doc.symlink_to(tmp_path / "old-checkout" / "skills" / "pstack" / "docs" / "harness" / "claude-code.md")
    assert flagged(home) == [f"claude: did not load the pinned version ({doc} is a dangling link); {run_fix(home)}"]
    install_fixes(home)


def test_an_agent_linked_elsewhere_is_flagged_as_the_wrong_target(home: Path, tmp_path: Path) -> None:
    agent = home / ".claude" / "agents" / "comment-sicko.md"
    mine = tmp_path / "comment-sicko.md"
    mine.write_text("my agent\n", encoding="utf-8")
    agent.unlink()
    agent.symlink_to(mine)
    assert flagged(home) == [
        f"claude: did not load the pinned version ({agent} is not the pinned file); {move_aside(home)}"
    ]
    assert agent.resolve() == mine


def test_a_leftover_owned_link_the_pin_no_longer_lists_is_flagged(home: Path) -> None:
    pinned = load_pin(ROOT).hashes()
    assert "agents/retired-agent.md" not in pinned
    leftover = home / ".claude" / "agents" / "retired-agent.md"
    leftover.symlink_to(PSTACK.resolve() / "agents" / "retired-agent.md")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({leftover} is not in the pin); {remove_then_install(home)}"
    ]
    assert leftover.is_symlink()
    assert remove_claude_extras(home, rules_text(ROOT)) == []
    install_fixes(home)
    assert not leftover.is_symlink()


def test_the_repair_command_passes_a_home_with_spaces_as_one_argument(tmp_path: Path) -> None:
    home = tmp_path / "home with spaces"
    install_fixes(home)
    (home / ".claude" / "agents" / "poteto-agent.md").unlink()
    [error] = flagged(home)
    command = error.rpartition("; run ")[2]
    assert shlex.split(command) == ["bin/orch", "install-skills", "--home", str(home)]
    install_fixes(home)


def test_a_file_in_place_of_a_docs_dir_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    harness = home / ".claude" / "docs" / "harness"
    for link in harness.iterdir():
        link.unlink()
    harness.rmdir()
    harness.write_text("mine\n", encoding="utf-8")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({harness} is not a directory); {move_blocker(home, harness)}"
    ]
    harness.rename(tmp_path / "harness-aside")
    install_fixes(home)


def test_a_symlinked_agents_dir_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    agents = home / ".claude" / "agents"
    for link in agents.iterdir():
        link.unlink()
    agents.rmdir()
    agents.symlink_to(PSTACK.resolve() / "agents")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({agents} is a symlink); {move_blocker(home, agents)}"
    ]
    agents.rename(tmp_path / "agents-aside")
    install_fixes(home)


def test_a_file_in_place_of_the_rules_dir_must_be_moved_aside(home: Path, tmp_path: Path) -> None:
    rules_dir = home / RULES_PATH.parent
    (home / RULES_PATH).unlink()
    rules_dir.rmdir()
    rules_dir.write_text("mine\n", encoding="utf-8")
    assert flagged(home) == [
        f"claude: did not load the pinned version ({rules_dir} is not a directory); {move_blocker(home, rules_dir)}"
    ]
    rules_dir.rename(tmp_path / "rules-aside")
    install_fixes(home)


def test_a_users_own_agent_beside_the_pinned_ones_is_not_flagged(home: Path) -> None:
    (home / ".claude" / "agents" / "reviewer.md").write_text("keep\n", encoding="utf-8")
    assert installed_errors(ROOT, home) == []


def test_pstack_models_does_not_import_the_pin() -> None:
    tree = ast.parse((ROOT / "lib" / "pstack_models.py").read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert "lib.pstack_pin" not in imported


CANARY_MACHINES = """\
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
  - name: mac
    tailscale_name: mac.example.ts.net
    slot_max: 2
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
    workdir_root: /Users/{bot}/factory-work
    ssh_alias: mac-{bot}
    factory_root: /Users/{bot}/factory
"""

WAITERS = {driver.name: (driver.name, "-p", f"--{driver.name}-waiter") for driver in all_drivers()}


def canary_rows(tmp_path: Path, refused: str = "[]"):
    machines_path = tmp_path / "machines.yml"
    machines_path.write_text(CANARY_MACHINES, encoding="utf-8")
    machines, findings = load_machines_yml(machines_path)
    assert machines is not None, findings
    orch_path = tmp_path / "orch.yml"
    orch_path.write_text(
        f"poll_seconds: 30\nprojects:\n  - repo: acme/widgets\nrefused_harnesses: {refused}\n",
        encoding="utf-8",
    )
    orch, findings = load_orch_yml(orch_path, allow_placeholders=True)
    assert orch is not None, findings
    return plan_canary(
        machines, orch.refused_harnesses, WAITERS, run_id="doctor-0000", local_root=str(tmp_path / "work")
    )


def test_plan_canary_has_a_row_per_harness_on_local_and_remote_devices(tmp_path: Path) -> None:
    rows = canary_rows(tmp_path)
    names = [driver.name for driver in all_drivers()]
    assert [(row.device, row.harness) for row in rows] == [
        (device, name) for device in ("box", "mac") for name in names
    ]
    bots = {driver.name: driver.bot_user for driver in all_drivers()}
    for row in rows:
        assert row.skip is None and row.launch is not None
        assert row.bot == bots[row.harness]
        argv = launch_argv(row.launch)
        if row.device == "box":
            assert argv == list(WAITERS[row.harness])
        else:
            assert argv[0] == "ssh"
            assert argv[argv.index("--") + 1] == f"mac-{row.bot}"


def test_plan_canary_skips_harnesses_refused_in_orch_yml(tmp_path: Path) -> None:
    rows = canary_rows(tmp_path, refused="[cursor]")
    refused = [row for row in rows if row.harness == "cursor"]
    assert [row.device for row in refused] == ["box", "mac"]
    assert all(row.skip == "refused in orch.yml" and row.launch is None for row in refused)
    others = [row for row in rows if row.harness != "cursor"]
    assert len(others) == 6
    assert all(row.skip is None and row.launch is not None for row in others)


def test_canary_prompt_for_names_each_drivers_skills_dir_and_bot() -> None:
    for driver in all_drivers():
        prompt = canary_prompt_for(driver)
        assert f"{driver.skills_dir}/factory-builder/SKILL.md" in prompt
        assert f"started-by: {driver.bot_user}" in prompt


def test_score_reply_needs_the_exact_pinned_passage() -> None:
    token = pinned_token(ROOT)
    changed = token[:-1] + ("x" if token[-1] != "x" else "y")
    for driver in all_drivers():
        name, bot = driver.name, driver.bot_user

        def reply(started_by: str = bot, passage: str = token) -> str:
            return f"started-by: {started_by}\nskill: factory-builder\ntoken: {passage}\n"

        assert score_reply(name, bot, reply(), token) is None
        mismatch = f"{name}: did not load the pinned version (token does not match the pin)"
        assert score_reply(name, bot, reply(passage=changed), token) == mismatch
        assert score_reply(name, bot, reply(passage=token + " and more"), token) == mismatch
        assert score_reply(name, bot, reply(started_by="someone-else"), token) == (
            f"{name}: did not load the pinned version (not started by {bot})"
        )
