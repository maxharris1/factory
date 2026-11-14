import hashlib
import shutil
from pathlib import Path

import pytest

from lib.pstack_pin import (
    ROOT,
    bump_pin,
    canary_prompt,
    doctor,
    install_skills,
    load_pin,
    pinned_token,
    verify_pin,
)
from tests.test_cli import run
from tests.test_pstack_pin import tiny_tree

SKILL_DIRS = (".claude/skills", ".agents/skills", ".cursor/skills")
ROUTERS = ("factory-builder", "factory-reviewer")
PSTACK_SKILLS = ROOT / "skills" / "pstack" / "skills"
FACTORY = ROOT / "skills" / "factory"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_files(name: str, mode: str = "local") -> dict[str, str]:
    pin = load_pin(ROOT)
    if name in ROUTERS:
        return {rel.removeprefix(f"{name}/"): digest for rel, digest in pin.factory if rel.startswith(f"{name}/")}
    prefix = f"skills/{name}/"
    files = {rel.removeprefix(prefix): digest for rel, digest in pin.files if rel.startswith(prefix)}
    if mode == "cloud":
        files["LICENSE"] = pin.hashes()["LICENSE"]
    return files


def installed_files(skill: Path) -> dict[str, str]:
    return {
        path.relative_to(skill).as_posix(): sha256(path)
        for path in skill.rglob("*")
        if path.is_file() and path.name != ".orch-owned"
    }


def snapshot(anchor: Path) -> dict[str, str]:
    shot: dict[str, str] = {}
    for path in sorted(anchor.rglob("*")):
        rel = path.relative_to(anchor).as_posix()
        if path.is_symlink():
            shot[rel] = "link " + str(path.readlink())
        elif path.is_file():
            shot[rel] = sha256(path)
        else:
            shot[rel] = "dir"
    return shot


def write_replies(directory: Path, *, skip: str | None = None, skill: str = "factory-builder") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for harness, owner in (("claude", "claude-bot"), ("codex", "codex-bot")):
        if harness == skip:
            continue
        text = f"started-by: {owner}\nskill: {skill}\ntoken: {pinned_token(ROOT)}\n"
        (directory / harness).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_clean_home_gets_both_routers_and_poteto_mode_matching_the_lock(tmp_path: Path, mode: str) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, mode) == []
    for rel in SKILL_DIRS:
        for name in (*ROUTERS, "poteto-mode"):
            skill = home / rel / name
            assert (skill / "SKILL.md").is_file(), f"{skill} is missing"
            assert installed_files(skill) == expected_files(name, mode), f"{skill} differs from the lock"
    for name in ROUTERS:
        link = home / ".claude" / "skills" / name
        if mode == "local":
            assert link.is_symlink() and link.resolve() == (FACTORY / name).resolve()
        else:
            assert not link.is_symlink() and (link / ".orch-owned").is_file()
    for rel in SKILL_DIRS:
        assert not (home / rel / "harness").exists()


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_rerunning_the_install_is_idempotent(tmp_path: Path, mode: str) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, mode) == []
    first = snapshot(home)
    assert install_skills(ROOT, home, mode) == []
    assert snapshot(home) == first
    replies = tmp_path / "replies"
    write_replies(replies)
    code, _prompt, errors = doctor(ROOT, home, mode, replies, print_canary=False)
    assert (code, errors) == (0, [])


def test_the_lock_covers_every_file_under_skills_factory() -> None:
    on_disk = {
        path.relative_to(FACTORY).as_posix(): sha256(path)
        for path in FACTORY.rglob("*")
        if path.is_file()
    }
    assert {"factory-builder/SKILL.md", "factory-reviewer/SKILL.md", "harness/codex.md"} <= set(on_disk)
    assert load_pin(ROOT).factory_hashes() == on_disk
    assert verify_pin(ROOT) == []


def test_verify_flags_factory_files_that_drift_from_the_lock(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    assert verify_pin(root) == []
    factory = root / "skills" / "factory"
    (factory / "factory-builder" / "SKILL.md").write_text("edited\n", encoding="utf-8")
    (factory / "factory-builder" / "extra.md").write_text("new\n", encoding="utf-8")
    errors = verify_pin(root)
    assert "skills.lock: factory/factory-builder/SKILL.md hash does not match the pin" in errors
    assert "skills.lock: extra factory/factory-builder/extra.md" in errors
    assert install_skills(root, tmp_path / "home", "local") == errors
    assert not (tmp_path / "home").exists()


def test_bump_pin_writes_the_factory_hashes(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    builder = root / "skills" / "factory" / "factory-builder" / "SKILL.md"
    assert load_pin(root).factory_hashes() == {"factory-builder/SKILL.md": sha256(builder)}
    builder.write_text(builder.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
    bump_pin(root, "b" * 40, "0.15.36")
    assert load_pin(root).factory_hashes() == {"factory-builder/SKILL.md": sha256(builder)}
    assert verify_pin(root) == []


def test_lock_without_a_factory_section_is_refused(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    lock = root / "skills" / "skills.lock"
    text = lock.read_text(encoding="utf-8")
    lock.write_text(text[: text.index("factory:")] + text[text.index("kit:") :], encoding="utf-8")
    assert verify_pin(root) == ["skills.lock is missing factory"]


def test_install_relinks_a_stale_home_missing_poteto_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    gone = tmp_path / "old-checkout" / "skills"
    (skills / "factory-builder").symlink_to(gone / "factory" / "factory-builder")
    (skills / "statechart").symlink_to(gone / "pstack" / "skills" / "statechart")
    other = tmp_path / "other-checkout" / "skills" / "factory" / "factory-reviewer"
    other.mkdir(parents=True)
    (skills / "factory-reviewer").symlink_to(other)
    assert not (skills / "poteto-mode").exists()
    assert install_skills(ROOT, home, "local") == []
    for name in (*ROUTERS, "poteto-mode", "statechart"):
        link = skills / name
        source = FACTORY / name if name in ROUTERS else PSTACK_SKILLS / name
        assert link.is_symlink() and link.resolve() == source.resolve(), name


def test_install_refuses_an_unowned_router_folder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mine = home / ".agents" / "skills" / "factory-builder"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own\n", encoding="utf-8")
    assert install_skills(ROOT, home, "local") == [f"refusing to replace unowned path {mine}"]
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "my own\n"


def test_canary_reads_the_factory_builder_path_and_skips_cursor() -> None:
    result = run(["doctor", "--print-canary"])
    assert result.returncode == 0, result.stderr
    prompt = result.stdout
    assert prompt == canary_prompt()
    assert ".claude/skills/factory-builder/SKILL.md" in prompt
    assert ".agents/skills/factory-builder/SKILL.md" in prompt
    assert "skill: factory-builder" in prompt
    assert "playbooks/orchestrate.md" in prompt
    assert "claude-bot" in prompt and "codex-bot" in prompt
    assert "cursor" not in prompt.lower()
    assert pinned_token(ROOT) not in prompt


def test_doctor_passes_with_only_claude_and_codex_replies(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, "local") == []
    replies = tmp_path / "replies"
    write_replies(replies)
    assert not (replies / "cursor").exists()
    code, _prompt, errors = doctor(ROOT, home, "local", replies, print_canary=False)
    assert (code, errors) == (0, [])


def test_doctor_with_no_replies_names_only_claude_and_codex(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, "local") == []
    code, _prompt, errors = doctor(ROOT, home, "local", None, print_canary=False)
    assert code == 1
    assert errors == [
        "claude: did not load the pinned version (no canary response)",
        "codex: did not load the pinned version (no canary response)",
    ]


def test_doctor_fails_when_a_reply_names_poteto_mode_not_the_router(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, "local") == []
    replies = tmp_path / "replies"
    write_replies(replies, skill="poteto-mode")
    code, _prompt, errors = doctor(ROOT, home, "local", replies, print_canary=False)
    assert code == 1
    assert "codex: did not load the pinned version (did not name factory-builder)" in errors


def test_doctor_fails_when_a_codex_reply_is_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, "local") == []
    replies = tmp_path / "replies"
    write_replies(replies, skip="codex")
    code, _prompt, errors = doctor(ROOT, home, "local", replies, print_canary=False)
    assert (code, errors) == (1, ["codex: did not load the pinned version (no canary response)"])


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_doctor_checks_the_installed_routers(tmp_path: Path, mode: str) -> None:
    home = tmp_path / "home"
    assert install_skills(ROOT, home, mode) == []
    router = home / ".agents" / "skills" / "factory-reviewer"
    if router.is_symlink():
        router.unlink()
    else:
        shutil.rmtree(router)
    replies = tmp_path / "replies"
    write_replies(replies)
    code, _prompt, errors = doctor(ROOT, home, mode, replies, print_canary=False)
    assert code == 1
    assert f"codex: did not load the pinned version ({router} is missing)" in errors
