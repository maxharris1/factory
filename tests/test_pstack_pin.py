import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.pstack_pin import (
    ROOT,
    bump_pin,
    doctor,
    install_skills,
    load_pin,
    pinned_token,
    verify_pin,
)
from tests.test_cli import run

PIN_COMMIT = "c742dd61706fbacc1b95f1a20b7b988cf096ce6c"
OWNERS = {
    "claude": "claude-bot",
    "codex": "codex-bot",
    "cursor": "cursor-bot",
}


def skill_names() -> list[str]:
    base = ROOT / "skills" / "pstack" / "skills"
    return sorted(path.name for path in base.iterdir() if path.is_dir())


def write_replies(
    directory: Path,
    token: str,
    *,
    skip: str | None = None,
    skill: dict[str, str] | None = None,
    token_for: dict[str, str] | None = None,
    started_by: dict[str, str] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for harness, owner in OWNERS.items():
        if harness == skip:
            continue
        who = (started_by or {}).get(harness, owner)
        name = (skill or {}).get(harness, "poteto-mode")
        quoted = (token_for or {}).get(harness, token)
        text = f"started-by: {who}\nskill: {name}\ntoken: {quoted}\n"
        (directory / harness).write_text(text, encoding="utf-8")


def tiny_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    skill = root / "skills" / "pstack" / "skills" / "poteto-mode"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: poteto-mode\ndescription: token-one\n---\n\n",
        encoding="utf-8",
    )
    playbook = skill / "playbooks"
    playbook.mkdir()
    (playbook / "orchestrate.md").write_text(
        "### Orchestrate\n\n**token-one** the rest.\n",
        encoding="utf-8",
    )
    manifest = '{"name": "pstack", "version": "0.15.36"}\n'
    for rel in (".cursor-plugin/plugin.json", "plugin.json"):
        path = root / "skills" / "pstack" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest, encoding="utf-8")
    return root


def test_lock_pins_pstack_0_15_36() -> None:
    pin = load_pin(ROOT)
    assert pin.version == "0.15.36"
    assert pin.commit == PIN_COMMIT
    assert verify_pin(ROOT) == []
    manifest = json.loads((ROOT / "skills" / "pstack" / ".cursor-plugin" / "plugin.json").read_text())
    other = json.loads((ROOT / "skills" / "pstack" / "plugin.json").read_text())
    assert manifest["version"] == "0.15.36"
    assert other["version"] == "0.15.36"
    assert "skills/poteto-mode/SKILL.md" in pin.hashes()
    assert "skills/statechart/SKILL.md" in pin.hashes()


def test_canary_asks_for_poteto_mode_and_a_token_from_the_skill() -> None:
    result = run(["doctor", "--print-canary"])
    assert result.returncode == 0, result.stderr
    prompt = result.stdout
    token = pinned_token(ROOT)
    assert "poteto-mode" in prompt
    assert "playbooks/orchestrate.md" in prompt
    assert "whole first bold passage" in prompt
    assert "exactly as written" in prompt
    assert "without the ** markers" in prompt
    assert "claude-bot" in prompt
    assert "codex-bot" in prompt
    assert "cursor-bot" in prompt
    playbook = ROOT / "skills" / "pstack" / "skills" / "poteto-mode" / "playbooks" / "orchestrate.md"
    assert token in playbook.read_text(encoding="utf-8")
    assert token not in prompt
    assert "ok" not in prompt.splitlines()


def test_install_symlinks_each_skill_into_the_local_harness_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run(["install-skills", "--home", str(home)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    names = skill_names()
    assert "poteto-mode" in names
    assert "statechart" in names
    for rel in (".claude/skills", ".agents/skills", ".cursor/skills"):
        installed = sorted(path.name for path in (home / rel).iterdir())
        assert installed == names
        for name in names:
            link = home / rel / name
            assert link.is_symlink()
            assert link.resolve() == (ROOT / "skills" / "pstack" / "skills" / name).resolve()
    assert not (home / ".codex").exists()
    assert not (home / ".cursor" / "plugins").exists()


def test_install_vendors_each_skill_into_the_cloud_project_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    result = run(["install-skills", "--cloud", "--project", str(project)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    names = skill_names()
    for rel in (".claude/skills", ".agents/skills", ".cursor/skills"):
        installed = sorted(path.name for path in (project / rel).iterdir())
        assert installed == names
        for name in ("poteto-mode", "statechart"):
            dest = project / rel / name
            assert dest.is_dir()
            assert not dest.is_symlink()
            src = ROOT / "skills" / "pstack" / "skills" / name
            for path in src.rglob("*"):
                if not path.is_file():
                    continue
                copied = dest / path.relative_to(src)
                assert copied.read_bytes() == path.read_bytes()
    assert not (project / ".codex").exists()


def test_install_is_idempotent_for_an_owned_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    again = run(["install-skills", "--home", str(home)])
    assert again.returncode == 0, again.stderr
    link = home / ".claude" / "skills" / "poteto-mode"
    assert link.is_symlink()
    assert link.resolve() == (ROOT / "skills" / "pstack" / "skills" / "poteto-mode").resolve()


def test_doctor_passes_when_each_owning_bot_quotes_the_pinned_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    replies = tmp_path / "replies"
    token = pinned_token(ROOT)
    assert ". " in token
    write_replies(replies, token)
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 0, result.stderr
    assert "poteto-mode" in result.stdout
    assert result.stdout.endswith("ok\n")


def test_doctor_cloud_passes_on_vendored_copies(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert run(["install-skills", "--cloud", "--project", str(project)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT))
    result = run(["doctor", "--cloud", "--project", str(project), "--responses", str(replies)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("ok\n")


def test_doctor_fails_when_a_harness_response_is_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), skip="cursor")
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "cursor: did not load the pinned version" in result.stderr
    assert "ok" not in result.stdout.splitlines()


def test_doctor_fails_when_the_reply_does_not_name_poteto_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), skill={"codex": "other-skill"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "codex: did not load the pinned version (did not name poteto-mode)" in result.stderr


def test_doctor_fails_when_the_token_is_not_the_pinned_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), token_for={"claude": "some other sentence"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (token does not match the pin)" in result.stderr


def test_doctor_fails_when_reply_quotes_only_first_sentence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    token = pinned_token(ROOT)
    first_sentence, separator, _rest = token.partition(". ")
    assert separator
    replies = tmp_path / "replies"
    write_replies(replies, token, token_for={"claude": f"{first_sentence}."})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (token does not match the pin)" in result.stderr


def test_doctor_fails_when_reply_keeps_the_bold_markers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    token = pinned_token(ROOT)
    replies = tmp_path / "replies"
    write_replies(replies, token, token_for={"claude": f"**{token}**"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (token does not match the pin)" in result.stderr


def test_doctor_fails_when_the_wrong_bot_starts_the_harness(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), started_by={"claude": "cursor-bot"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (not started by claude-bot)" in result.stderr


def test_doctor_fails_when_the_installed_skill_is_a_different_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    link = home / ".claude" / "skills" / "poteto-mode"
    link.unlink()
    shutil.copytree(ROOT / "skills" / "pstack" / "skills" / "poteto-mode", link)
    skill = link / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8").replace("poteto-mode", "poteto-mode-old", 1), encoding="utf-8")
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT))
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version" in result.stderr


def test_doctor_fails_with_no_responses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    result = run(["doctor", "--home", str(home)])
    assert result.returncode == 1
    for harness in OWNERS:
        assert f"{harness}: did not load the pinned version (no canary response)" in result.stderr


def test_doctor_does_not_start_a_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT))
    started: list[object] = []

    def boom(*args: object, **kwargs: object) -> None:
        started.append((args, kwargs))
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "call", boom)
    monkeypatch.setattr(os, "system", boom)
    code, _prompt, errors = doctor(ROOT, home, "local", replies, print_canary=False)
    assert code == 0, errors
    assert started == []


def test_install_writes_nothing_when_the_tree_disagrees_with_the_lock(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    skill = root / "skills" / "pstack" / "skills" / "poteto-mode" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    home = tmp_path / "home"
    errors = install_skills(root, home, "local")
    assert errors
    assert any("hash does not match the pin" in error for error in errors)
    assert not home.exists()


def test_pin_bump_updates_the_pin_and_the_lock_together(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    first = bump_pin(root, "a" * 40, "0.15.36")
    skill_rel = "skills/poteto-mode/SKILL.md"
    skill = root / "skills" / "pstack" / skill_rel
    skill.write_text("---\nname: poteto-mode\ndescription: token-two\n---\n\n", encoding="utf-8")
    assert any("hash does not match the pin" in error for error in verify_pin(root))
    manifest = '{"name": "pstack", "version": "0.15.37"}\n'
    for rel in (".cursor-plugin/plugin.json", "plugin.json"):
        (root / "skills" / "pstack" / rel).write_text(manifest, encoding="utf-8")
    second = bump_pin(root, "b" * 40, "0.15.37")
    assert second.commit == "b" * 40
    assert second.version == "0.15.37"
    assert second.hashes()[skill_rel] != first.hashes()[skill_rel]
    assert second.hashes()["plugin.json"] != first.hashes()["plugin.json"]
    assert verify_pin(root) == []
    text = (root / "skills" / "skills.lock").read_text(encoding="utf-8")
    assert f"commit: {'b' * 40}" in text
    assert second.hashes()[skill_rel] in text


def test_bump_pin_does_not_write_a_commit_for_a_different_version(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    before = (root / "skills" / "skills.lock").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="plugin.json version"):
        bump_pin(root, "b" * 40, "9.9.9")
    assert (root / "skills" / "skills.lock").read_text(encoding="utf-8") == before
    assert verify_pin(root) == []


def test_bump_pin_refuses_a_wrapped_first_bold_passage(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    playbook = root / "skills" / "pstack" / "skills" / "poteto-mode" / "playbooks" / "orchestrate.md"
    playbook.write_text("**token\none** then **two**\n", encoding="utf-8")
    message = "pinned poteto-mode first bold passage is not on one line"
    with pytest.raises(ValueError, match=message):
        bump_pin(root, "b" * 40, "0.15.36")
    assert message in verify_pin(root)


TOKEN_0_15_35 = (
    "poteto's agent style for concise, detailed responses, deliberate subagents, "
    "unslopped prose, simple code, and verified work. Use for poteto, /poteto-mode, "
    "or requests to work in this style."
)


def test_install_refuses_unowned_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    notes = home / ".claude" / "skills" / "poteto-mode" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("keep-notes", encoding="utf-8")
    state = home / ".agents" / "skills" / "statechart"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"keep-state")
    result = run(["install-skills", "--home", str(home)])
    assert result.returncode == 1
    assert "refusing to replace unowned path" in result.stderr
    assert "poteto-mode" in result.stderr
    assert "statechart" in result.stderr
    assert notes.read_text(encoding="utf-8") == "keep-notes"
    assert state.read_bytes() == b"keep-state"
    assert list(notes.parent.iterdir()) == [notes]
    assert list(state.parent.iterdir()) == [state]
    assert not (home / ".cursor").exists()


def test_install_refuses_a_symlinked_skills_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "user.txt").write_text("keep", encoding="utf-8")
    skills = home / ".claude" / "skills"
    skills.parent.mkdir(parents=True)
    skills.symlink_to(outside, target_is_directory=True)
    result = run(["install-skills", "--home", str(home)])
    assert result.returncode == 1
    assert "symlinked skills dir" in result.stderr
    assert "outside the target" in result.stderr
    assert (outside / "user.txt").read_text(encoding="utf-8") == "keep"
    assert not (outside / "poteto-mode").exists()
    assert not (home / ".agents").exists()


def test_interrupted_install_leaves_the_old_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    home = tmp_path / "home"
    assert install_skills(root, home, "cloud") == []
    dest = home / ".claude" / "skills" / "poteto-mode"
    skill = dest / "SKILL.md"
    skill.write_text("owned-old\n", encoding="utf-8")

    def boom(_src: Path, _dest: Path) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr("lib.pstack_pin._copy_tree", boom)
    errors = install_skills(root, home, "cloud")
    assert errors
    assert "interrupted" in errors[0]
    assert skill.read_text(encoding="utf-8") == "owned-old\n"
    assert (dest / ".orch-owned").is_file()


def _orch_leftovers(home: Path) -> list[Path]:
    return sorted(path for path in home.rglob(".orch-*") if path.name != ".orch-owned")


def _is_publish(src: object, dst: object) -> bool:
    src_path, dst_path = Path(str(src)), Path(str(dst))
    return src_path.parent.name.startswith(".orch-staging-") and src_path.name == dst_path.name


def test_cloud_install_replaces_owned_local_links(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "local") == []
    assert install_skills(ROOT, project, "cloud") == []
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT))
    code, _prompt, errors = doctor(ROOT, project, "cloud", replies, print_canary=False)
    assert code == 0, errors
    assert _orch_leftovers(project) == []


def test_crash_between_renames_is_recovered_on_next_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    home = tmp_path / "home"
    assert install_skills(root, home, "cloud") == []
    dest = home / ".claude" / "skills" / "poteto-mode"
    (dest / "SKILL.md").write_text("owned-old\n", encoding="utf-8")
    real_rename = os.rename
    dead: list[bool] = []

    def dying_rename(src: object, dst: object) -> None:
        if dead or _is_publish(src, dst):
            dead.append(True)
            raise KeyboardInterrupt
        real_rename(src, dst)

    monkeypatch.setattr("lib.pstack_pin.os.rename", dying_rename)
    monkeypatch.setattr("lib.pstack_pin._discard", lambda _path: None)
    with pytest.raises(KeyboardInterrupt):
        install_skills(root, home, "cloud")
    monkeypatch.undo()
    assert not dest.exists()
    assert _orch_leftovers(home)

    user = home / ".cursor" / "skills" / "poteto-mode"
    shutil.rmtree(user)
    user.mkdir()
    (user / "notes.md").write_text("keep", encoding="utf-8")
    refused = install_skills(root, home, "cloud")
    assert any("refusing to replace unowned path" in error for error in refused)
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "owned-old\n"

    shutil.rmtree(user)
    assert install_skills(root, home, "cloud") == []
    assert _orch_leftovers(home) == []
    assert (dest / "SKILL.md").read_text(encoding="utf-8") != "owned-old\n"


def test_legacy_backup_leftover_is_restored_then_discarded(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    home = tmp_path / "home"
    assert install_skills(root, home, "cloud") == []
    dest = home / ".claude" / "skills" / "poteto-mode"
    (dest / "SKILL.md").write_text("owned-old\n", encoding="utf-8")
    os.rename(dest, dest.parent / ".orch-backup-poteto-mode-ab12_cd3")
    stray = home / ".agents" / "skills" / ".orch-backup-poteto-mode-zz99"
    shutil.copytree(home / ".agents" / "skills" / "poteto-mode", stray)
    user = home / ".cursor" / "skills" / "poteto-mode"
    shutil.rmtree(user)
    user.mkdir()
    assert install_skills(root, home, "cloud")
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "owned-old\n"
    assert not stray.exists()
    user.rmdir()
    assert install_skills(root, home, "cloud") == []
    assert _orch_leftovers(home) == []


def test_failed_publish_rolls_back_the_old_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    home = tmp_path / "home"
    assert install_skills(root, home, "cloud") == []
    skill = home / ".claude" / "skills" / "poteto-mode" / "SKILL.md"
    skill.write_text("owned-old\n", encoding="utf-8")
    real_rename = os.rename

    def failing_rename(src: object, dst: object) -> None:
        if _is_publish(src, dst):
            raise OSError("publish failed")
        real_rename(src, dst)

    monkeypatch.setattr("lib.pstack_pin.os.rename", failing_rename)
    errors = install_skills(root, home, "cloud")
    assert errors and "publish failed" in errors[0]
    assert skill.read_text(encoding="utf-8") == "owned-old\n"
    assert _orch_leftovers(home) == []


def test_pin_rejects_a_symlink_and_a_non_regular_file(tmp_path: Path) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("escaped", encoding="utf-8")
    link = root / "skills" / "pstack" / "skills" / "extra"
    link.symlink_to(outside, target_is_directory=True)
    fifo = root / "skills" / "pstack" / "skills" / "poteto-mode" / "fifo"
    os.mkfifo(fifo)
    errors = verify_pin(root)
    assert any("symlink is not allowed: skills/extra" in error for error in errors)
    assert any("not a regular file: skills/poteto-mode/fifo" in error for error in errors)
    home = tmp_path / "home"
    installed = install_skills(root, home, "local")
    assert installed
    assert not home.exists()
    assert (outside / "SKILL.md").read_text(encoding="utf-8") == "escaped"


def test_doctor_rejects_the_0_15_35_token(tmp_path: Path) -> None:
    description = TOKEN_0_15_35
    skill = (ROOT / "skills" / "pstack" / "skills" / "poteto-mode" / "SKILL.md").read_text(encoding="utf-8")
    assert description in skill
    assert pinned_token(ROOT) != description
    home = tmp_path / "home"
    assert run(["install-skills", "--home", str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), token_for={"claude": description})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (token does not match the pin)" in result.stderr


def test_doctor_cloud_rejects_symlinked_copies(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "local") == []
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT))
    code, _prompt, errors = doctor(ROOT, project, "cloud", replies, print_canary=False)
    assert code == 1
    assert any("must be a real copy" in error for error in errors)
    link = project / ".claude" / "skills" / "poteto-mode"
    assert link.is_symlink()
    assert link.resolve() == (ROOT / "skills" / "pstack" / "skills" / "poteto-mode").resolve()


def test_project_flag_requires_cloud(tmp_path: Path) -> None:
    result = run(["install-skills", "--project", str(tmp_path)])
    assert result.returncode == 2
    assert "--cloud" in result.stderr


def test_print_canary_rejects_responses(tmp_path: Path) -> None:
    result = run(["doctor", "--print-canary", "--responses", str(tmp_path)])
    assert result.returncode == 2
