import hashlib
import re
from pathlib import Path

from lib.pstack_models import rules_text
from lib.pstack_pin import ROOT, bump_pin, doctor, install_skills, load_pin, remove_claude_extras, verify_pin
from tests.test_cli import run
from tests.test_pstack_install import snapshot, write_replies
from tests.test_pstack_pin import tiny_tree

PSTACK = ROOT / "skills" / "pstack"
KIT = ROOT / "skills" / "cursor-team-kit"
BUILDER = ROOT / "skills" / "factory" / "factory-builder" / "SKILL.md"
NOTE = ROOT / "docs" / "decisions" / "178-plugin-dir-and-deslop.md"
SKILL_DIRS = (".claude/skills", ".agents/skills", ".cursor/skills")
AGENTS = ("poteto-agent", "comment-sicko")
RULES = Path(".claude") / "rules" / "pstack-models.md"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frontmatter_name(path: Path) -> str:
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
    assert match, f"{path} has no frontmatter"
    names = [line.removeprefix("name: ") for line in match.group(1).splitlines() if line.startswith("name: ")]
    assert len(names) == 1, f"{path} names {names}"
    return names[0]


def router_agent_names() -> set[str]:
    lines = [line for line in BUILDER.read_text(encoding="utf-8").splitlines() if "pstack:poteto-agent" in line]
    assert len(lines) == 1, "factory-builder needs one line for the adapter's agent names"
    return {name for name in re.findall(r"`([a-z:-]+)`", lines[0]) if not name.startswith("pstack:")}


def install(home: Path) -> list[str]:
    return install_skills(ROOT, home, "local", rules=rules_text(ROOT))


def test_clean_home_resolves_both_agents_by_the_names_the_router_uses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install(home) == []
    pinned = load_pin(ROOT).hashes()
    assert router_agent_names() == set(AGENTS)
    for name in AGENTS:
        agent = home / ".claude" / "agents" / f"{name}.md"
        assert agent.is_symlink() and agent.resolve() == (PSTACK / "agents" / f"{name}.md").resolve()
        assert frontmatter_name(agent) == name
        assert sha256(agent) == pinned[f"agents/{name}.md"]


def test_claude_home_mirrors_the_pstack_layout_for_the_adapter(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install(home) == []
    claude = home / ".claude"
    pinned = load_pin(ROOT).hashes()
    docs = {rel for rel in pinned if rel.startswith("docs/")}
    assert "docs/harness/claude-code.md" in docs
    for rel in docs:
        assert (claude / rel).is_symlink() and sha256(claude / rel) == pinned[rel], rel
    poteto = claude / "skills" / "poteto-mode"
    assert (poteto.parent.parent / "docs" / "harness" / "claude-code.md").is_file()
    assert (poteto.parent.parent / "agents" / "comment-sicko.md").is_file()


def test_cloud_install_adds_no_claude_agents_docs_or_rules(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "cloud", rules=rules_text(ROOT)) == []
    claude = project / ".claude"
    assert sorted(path.name for path in claude.iterdir()) == ["skills"]


def test_install_refuses_a_users_own_agent_and_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = home / ".claude" / "agents" / "poteto-agent.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("my agent\n", encoding="utf-8")
    assert install(home) == [f"refusing to replace unowned path {mine}"]
    assert mine.read_text(encoding="utf-8") == "my agent\n"
    assert not (home / ".claude" / "skills").exists()
    assert not (home / RULES).exists()


def test_install_keeps_other_agents_in_the_folder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    other = home / ".claude" / "agents" / "reviewer.md"
    other.parent.mkdir(parents=True)
    other.write_text("keep\n", encoding="utf-8")
    assert install(home) == []
    assert remove_claude_extras(home, rules_text(ROOT)) == []
    assert other.read_text(encoding="utf-8") == "keep\n"


def test_rerun_is_idempotent_and_doctor_checks_the_agents(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install(home) == []
    first = snapshot(home)
    assert install(home) == []
    assert snapshot(home) == first
    replies = tmp_path / "replies"
    write_replies(replies)
    code, _prompt, errors = doctor(ROOT, home, "local", replies, print_canary=False)
    assert (code, errors) == (0, [])
    agent = home / ".claude" / "agents" / "comment-sicko.md"
    agent.unlink()
    code, _prompt, errors = doctor(ROOT, home, "local", replies, print_canary=False)
    assert code == 1
    assert errors == [f"claude: did not load the pinned version ({agent} is not the pinned file)"]


def test_deslop_is_vendored_with_its_license_and_pinned_by_hash() -> None:
    kit = load_pin(ROOT).kit_hashes()
    on_disk = {path.relative_to(KIT).as_posix(): sha256(path) for path in KIT.rglob("*") if path.is_file()}
    assert on_disk == kit
    assert set(kit) == {"LICENSE", "deslop/SKILL.md"}
    assert (KIT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")
    assert frontmatter_name(KIT / "deslop" / "SKILL.md") == "deslop"
    assert verify_pin(ROOT) == []


def test_deslop_installs_beside_poteto_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install(home) == []
    for rel in SKILL_DIRS:
        skill = home / rel / "deslop"
        assert skill.is_symlink() and skill.resolve() == (KIT / "deslop").resolve()
    assert not (home / ".claude" / "skills" / "LICENSE").exists()


def test_verify_flags_a_changed_deslop(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    (root / "skills" / "cursor-team-kit" / "deslop" / "SKILL.md").write_text("edited\n", encoding="utf-8")
    errors = verify_pin(root)
    assert errors == ["skills.lock: cursor-team-kit/deslop/SKILL.md hash does not match the pin"]
    assert install_skills(root, tmp_path / "home", "local") == errors


def test_rules_file_is_the_model_map_text_written_whole(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install(home) == []
    rules = home / RULES
    assert not rules.is_symlink()
    assert rules.read_text(encoding="utf-8") == rules_text(ROOT)
    assert sorted(path.name for path in rules.parent.iterdir()) == ["pstack-models.md"]


def test_rules_file_replaces_an_older_generated_copy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rules = home / RULES
    rules.parent.mkdir(parents=True)
    old = rules_text(ROOT).splitlines(keepends=True)[0] + "feature, refactoring: sonnet low\n"
    rules.write_text(old, encoding="utf-8")
    assert install(home) == []
    assert rules.read_text(encoding="utf-8") == rules_text(ROOT)


def test_rules_file_written_by_hand_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rules = home / RULES
    rules.parent.mkdir(parents=True)
    rules.write_text("# my own models\nfeature, refactoring: opus xhigh\n", encoding="utf-8")
    assert install(home) == [f"refusing to replace unowned path {rules}"]
    assert rules.read_text(encoding="utf-8").startswith("# my own models\n")
    assert not (home / ".claude" / "agents").exists()


def test_install_without_rules_text_writes_no_rules_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, "local") == []
    assert not (home / RULES).exists()
    assert (home / ".claude" / "agents" / "poteto-agent.md").is_symlink()


def test_remove_takes_out_the_rules_file_agents_and_docs_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install(home) == []
    assert remove_claude_extras(home, rules_text(ROOT)) == []
    claude = home / ".claude"
    assert not (home / RULES).exists()
    assert not (claude / "agents").exists()
    assert not (claude / "docs").exists()
    assert not (claude / "rules").exists()
    assert (claude / "skills" / "poteto-mode").is_symlink()
    assert remove_claude_extras(home, rules_text(ROOT)) == []


def test_remove_keeps_a_hand_written_rules_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rules = home / RULES
    rules.parent.mkdir(parents=True)
    rules.write_text("# my own models\n", encoding="utf-8")
    assert remove_claude_extras(home, rules_text(ROOT)) == [f"refusing to remove unowned path {rules}"]
    assert rules.read_text(encoding="utf-8") == "# my own models\n"


def test_cli_installs_the_rules_file_and_the_revert_command_removes_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run(["install-skills", "--home", str(home)])
    assert result.returncode == 0, result.stderr
    assert (home / RULES).read_text(encoding="utf-8") == rules_text(ROOT)
    assert (home / ".claude" / "agents" / "poteto-agent.md").is_symlink()
    result = run(["install-skills", "--home", str(home), "--remove-claude-extras"])
    assert result.returncode == 0, result.stderr
    assert not (home / RULES).exists()
    assert not (home / ".claude" / "agents").exists()


def test_cli_refuses_remove_for_a_cloud_install(tmp_path: Path) -> None:
    result = run(["install-skills", "--cloud", "--project", str(tmp_path), "--remove-claude-extras"])
    assert result.returncode == 2
    assert "--remove-claude-extras" in result.stderr


def test_note_records_the_path_the_evidence_the_license_and_the_revert() -> None:
    text = NOTE.read_text(encoding="utf-8")
    for needed in (
        "--plugin-dir",
        "docs/skill-loading.md",
        "MIT",
        "3dbd22ff12742fb28c9700b9760baa29ed41dc13",
        "orch install-skills --remove-claude-extras",
        "~/.claude/rules/pstack-models.md",
    ):
        assert needed in text, needed


def test_builder_no_longer_skips_deslop() -> None:
    text = BUILDER.read_text(encoding="utf-8")
    assert "<kit>/deslop/SKILL.md" in text
    assert "`<kit>` is its `skills/cursor-team-kit`" in text
    assert "until T07" not in text


def test_every_cloud_copy_of_a_vendored_skill_carries_its_upstream_license(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "cloud") == []
    sources = {
        **{path.name: PSTACK / "LICENSE" for path in (PSTACK / "skills").iterdir() if path.is_dir()},
        **{path.name: KIT / "LICENSE" for path in KIT.iterdir() if path.is_dir()},
    }
    for rel in SKILL_DIRS:
        for name, license_file in sources.items():
            copied = project / rel / name / "LICENSE"
            assert copied.read_bytes() == license_file.read_bytes(), copied
            assert f"{sha256(license_file)} LICENSE" in (project / rel / name / ".orch-owned").read_text("utf-8")
    replies = tmp_path / "replies"
    write_replies(replies)
    assert doctor(ROOT, project, "cloud", replies, print_canary=False)[::2] == (0, [])
    dropped = project / ".agents" / "skills" / "deslop" / "LICENSE"
    dropped.unlink()
    code, _prompt, errors = doctor(ROOT, project, "cloud", replies, print_canary=False)
    assert code == 1
    assert errors == ["codex: did not load the pinned version (missing skills/deslop/LICENSE)"]


def test_a_cloud_copy_from_before_the_license_is_upgraded_in_place(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "cloud") == []
    skill = project / ".claude" / "skills" / "deslop"
    (skill / "LICENSE").unlink()
    marker = skill / ".orch-owned"
    lines = marker.read_text("utf-8").splitlines()
    marker.write_text("\n".join(line for line in lines if not line.endswith(" LICENSE")) + "\n", "utf-8")
    assert install_skills(ROOT, project, "cloud") == []
    assert (skill / "LICENSE").read_bytes() == (KIT / "LICENSE").read_bytes()
