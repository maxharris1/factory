import ast
import shlex
from pathlib import Path

import pytest

from lib.pstack_models import rules_text
from lib.pstack_pin import RULES_PATH, ROOT, install_skills, installed_errors, load_pin, remove_claude_extras
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
