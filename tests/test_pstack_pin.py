import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from lib import cli
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

LOCAL_INSTALL = ["install-skills", "--allow-unstable-checkout", "--home"]
PIN_COMMIT = "c742dd61706fbacc1b95f1a20b7b988cf096ce6c"
OWNERS = {
    "claude": "claude-bot",
    "codex": "codex-bot",
}
ROUTERS = ("factory-builder", "factory-reviewer")


def skill_names() -> list[str]:
    base = ROOT / "skills" / "pstack" / "skills"
    return sorted([path.name for path in base.iterdir() if path.is_dir()] + list(ROUTERS))


def skill_source(name: str) -> Path:
    if name in ROUTERS:
        return ROOT / "skills" / "factory" / name
    return ROOT / "skills" / "pstack" / "skills" / name


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
        name = (skill or {}).get(harness, "factory-builder")
        quoted = (token_for or {}).get(harness, token)
        text = f"started-by: {who}\nskill: {name}\ntoken: {quoted}\n"
        (directory / harness).write_text(text, encoding="utf-8")


OLD_SKILL = "---\nname: poteto-mode\ndescription: token-one\n---\n\n"
ROUTER_SKILL = "---\nname: factory-builder\ndescription: router\n---\n\n"


def tiny_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    skill = root / "skills" / "pstack" / "skills" / "poteto-mode"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(OLD_SKILL, encoding="utf-8")
    playbook = skill / "playbooks"
    playbook.mkdir()
    (playbook / "orchestrate.md").write_text(
        "### Orchestrate\n\n**token-one** the rest.\n",
        encoding="utf-8",
    )
    router = root / "skills" / "factory" / "factory-builder"
    router.mkdir(parents=True)
    (router / "SKILL.md").write_text(ROUTER_SKILL, encoding="utf-8")
    manifest = '{"name": "pstack", "version": "0.15.36"}\n'
    for rel in (".cursor-plugin/plugin.json", "plugin.json"):
        path = root / "skills" / "pstack" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest, encoding="utf-8")
    return root


def install_then_bump(tmp_path: Path) -> tuple[Path, Path]:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    home = tmp_path / "home"
    assert install_skills(root, home, "cloud") == []
    skill = root / "skills" / "pstack" / "skills" / "poteto-mode" / "SKILL.md"
    skill.write_text(OLD_SKILL.replace("token-one", "token-two"), encoding="utf-8")
    bump_pin(root, "b" * 40, "0.15.36")
    return root, home


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


def test_canary_asks_for_factory_builder_and_a_token_from_poteto_mode() -> None:
    result = run(["doctor", "--print-canary"])
    assert result.returncode == 0, result.stderr
    prompt = result.stdout
    token = pinned_token(ROOT)
    assert "skill: factory-builder" in prompt
    assert "poteto-mode" in prompt
    assert "playbooks/orchestrate.md" in prompt
    assert "whole first bold passage" in prompt
    assert "exactly as written" in prompt
    assert "without the ** markers" in prompt
    assert "claude-bot" in prompt
    assert "codex-bot" in prompt
    assert "cursor-bot" not in prompt
    playbook = ROOT / "skills" / "pstack" / "skills" / "poteto-mode" / "playbooks" / "orchestrate.md"
    assert token in playbook.read_text(encoding="utf-8")
    assert token not in prompt
    assert "ok" not in prompt.splitlines()


def test_install_symlinks_each_skill_into_the_local_harness_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = run(LOCAL_INSTALL + [str(home)], cwd=tmp_path)
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
            assert link.resolve() == skill_source(name).resolve()
    assert not (home / ".codex").exists()
    assert not (home / ".cursor" / "plugins").exists()


def test_install_vendors_each_skill_into_the_cloud_project_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    result = run(["install-skills", "--cloud", "--project", str(project)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    names = skill_names()
    pin = load_pin(ROOT)
    pinned = {rel: digest for rel, digest in pin.hashes().items() if rel.startswith("skills/")}
    pinned |= {f"skills/{rel}": digest for rel, digest in pin.factory_hashes().items() if rel.split("/")[0] in ROUTERS}
    for rel in (".claude/skills", ".agents/skills", ".cursor/skills"):
        installed = sorted(path.name for path in (project / rel).iterdir())
        assert installed == names
        copied: dict[str, str] = {}
        for path in (project / rel).rglob("*"):
            assert not path.is_symlink()
            if path.is_file():
                inside = path.relative_to(project / rel).as_posix()
                copied["skills/" + inside] = hashlib.sha256(path.read_bytes()).hexdigest()
        markers = {f"skills/{name}/.orch-owned" for name in names}
        assert set(copied) == set(pinned) | markers
        assert {key: copied[key] for key in pinned} == pinned
    assert not (project / ".codex").exists()


def test_install_is_idempotent_for_an_owned_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    again = run(LOCAL_INSTALL + [str(home)])
    assert again.returncode == 0, again.stderr
    link = home / ".claude" / "skills" / "poteto-mode"
    assert link.is_symlink()
    assert link.resolve() == (ROOT / "skills" / "pstack" / "skills" / "poteto-mode").resolve()


def test_doctor_passes_when_each_owning_bot_quotes_the_pinned_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    replies = tmp_path / "replies"
    token = pinned_token(ROOT)
    assert ". " in token
    write_replies(replies, token)
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 0, result.stderr
    assert "factory-builder" in result.stdout
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
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), skip="codex")
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "codex: did not load the pinned version" in result.stderr
    assert "ok" not in result.stdout.splitlines()


def test_doctor_fails_when_the_reply_does_not_name_factory_builder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), skill={"codex": "other-skill"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "codex: did not load the pinned version (did not name factory-builder)" in result.stderr


def test_doctor_fails_when_the_token_is_not_the_pinned_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), token_for={"claude": "some other sentence"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (token does not match the pin)" in result.stderr


def test_doctor_fails_when_reply_quotes_only_first_sentence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
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
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    token = pinned_token(ROOT)
    replies = tmp_path / "replies"
    write_replies(replies, token, token_for={"claude": f"**{token}**"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (token does not match the pin)" in result.stderr


@pytest.mark.parametrize("key", ["started-by", "skill", "token"])
@pytest.mark.parametrize("extra", ["conflicting", "identical"])
def test_doctor_fails_when_a_reply_repeats_or_conflicts_a_key(tmp_path: Path, key: str, extra: str) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    replies = tmp_path / "replies"
    token = pinned_token(ROOT)
    write_replies(replies, token)
    reply = replies / "codex"
    lines = reply.read_text(encoding="utf-8").splitlines()
    line = next(item for item in lines if item.startswith(f"{key}: "))
    repeat = line if extra == "identical" else f"{key}: something-else"
    reply.write_text("\n".join(["Here is the canary.", *lines, repeat, "Thanks."]) + "\n", encoding="utf-8")
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert f"codex: did not load the pinned version (more than one {key}: line)" in result.stderr
    assert "claude:" not in result.stderr


def test_doctor_allows_prose_around_the_three_lines(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT))
    reply = replies / "claude"
    reply.write_text("Loaded the skill.\n" + reply.read_text(encoding="utf-8") + "Done.\n", encoding="utf-8")
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 0, result.stderr


def test_doctor_fails_when_the_wrong_bot_starts_the_harness(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT), started_by={"claude": "cursor-bot"})
    result = run(["doctor", "--home", str(home), "--responses", str(replies)])
    assert result.returncode == 1
    assert "claude: did not load the pinned version (not started by claude-bot)" in result.stderr


def test_doctor_fails_when_the_installed_skill_is_a_different_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
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
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
    result = run(["doctor", "--home", str(home)])
    assert result.returncode == 1
    for harness in OWNERS:
        assert f"{harness}: did not load the pinned version (no canary response)" in result.stderr


def test_doctor_does_not_start_a_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
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
    result = run(LOCAL_INSTALL + [str(home)])
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
    result = run(LOCAL_INSTALL + [str(home)])
    assert result.returncode == 1
    assert "symlinked skills dir" in result.stderr
    assert "outside the target" in result.stderr
    assert (outside / "user.txt").read_text(encoding="utf-8") == "keep"
    assert not (outside / "poteto-mode").exists()
    assert not (home / ".agents").exists()


def test_interrupted_install_leaves_the_old_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, home = install_then_bump(tmp_path)
    dest = home / ".claude" / "skills" / "poteto-mode"
    skill = dest / "SKILL.md"

    def boom(_src: Path, _dest: Path) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr("lib.pstack_pin._copy_tree", boom)
    errors = install_skills(root, home, "cloud")
    assert errors
    assert "interrupted" in errors[0]
    assert skill.read_text(encoding="utf-8") == OLD_SKILL
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
    root, home = install_then_bump(tmp_path)
    dest = home / ".claude" / "skills" / "poteto-mode"
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
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == OLD_SKILL

    shutil.rmtree(user)
    assert install_skills(root, home, "cloud") == []
    assert _orch_leftovers(home) == []
    assert (dest / "SKILL.md").read_text(encoding="utf-8") != OLD_SKILL


def test_legacy_backup_leftover_is_restored_then_discarded(tmp_path: Path) -> None:
    root, home = install_then_bump(tmp_path)
    dest = home / ".claude" / "skills" / "poteto-mode"
    os.rename(dest, dest.parent / ".orch-backup-poteto-mode-ab12_cd3")
    stray = home / ".agents" / "skills" / ".orch-backup-poteto-mode-zz99"
    shutil.copytree(home / ".agents" / "skills" / "poteto-mode", stray)
    user = home / ".cursor" / "skills" / "poteto-mode"
    shutil.rmtree(user)
    user.mkdir()
    assert install_skills(root, home, "cloud")
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == OLD_SKILL
    assert not stray.exists()
    user.rmdir()
    assert install_skills(root, home, "cloud") == []
    assert _orch_leftovers(home) == []


def test_failed_publish_rolls_back_the_old_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, home = install_then_bump(tmp_path)
    skill = home / ".claude" / "skills" / "poteto-mode" / "SKILL.md"
    real_rename = os.rename

    def failing_rename(src: object, dst: object) -> None:
        if _is_publish(src, dst):
            raise OSError("publish failed")
        real_rename(src, dst)

    monkeypatch.setattr("lib.pstack_pin.os.rename", failing_rename)
    errors = install_skills(root, home, "cloud")
    assert errors and "publish failed" in errors[0]
    assert skill.read_text(encoding="utf-8") == OLD_SKILL
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
    assert run(LOCAL_INSTALL + [str(home)]).returncode == 0
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


def test_install_refuses_an_edited_cloud_copy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "cloud") == []
    skill = project / ".claude" / "skills" / "poteto-mode" / "SKILL.md"
    skill.write_text("my edits\n", encoding="utf-8")
    notes = project / ".agents" / "skills" / "statechart" / "notes.md"
    notes.write_text("keep", encoding="utf-8")
    errors = install_skills(ROOT, project, "cloud")
    assert errors == [
        f"refusing to replace unowned path {skill.parent}",
        f"refusing to replace unowned path {notes.parent}",
    ]
    assert skill.read_text(encoding="utf-8") == "my edits\n"
    assert notes.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "marker",
    [
        f"installer: orch-install-skills\ncommit: {'a' * 40}\n",
        f"installer: orch-install-skills\ncommit: {'a' * 40}\nfile: {'0' * 64} notes.md\n",
    ],
    ids=["legacy", "wrong-hash"],
)
def test_install_refuses_a_forged_marker(tmp_path: Path, marker: str) -> None:
    project = tmp_path / "project"
    user = project / ".claude" / "skills" / "poteto-mode"
    user.mkdir(parents=True)
    (user / "notes.md").write_text("keep", encoding="utf-8")
    (user / ".orch-owned").write_text(marker, encoding="utf-8")
    errors = install_skills(ROOT, project, "cloud")
    assert errors == [f"refusing to replace unowned path {user}"]
    assert (user / "notes.md").read_text(encoding="utf-8") == "keep"


def test_legacy_marker_on_a_pinned_copy_is_owned(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "cloud") == []
    marker = project / ".claude" / "skills" / "poteto-mode" / ".orch-owned"
    marker.write_text(f"installer: orch-install-skills\ncommit: {'a' * 40}\n", encoding="utf-8")
    assert install_skills(ROOT, project, "cloud") == []
    assert "file: " in marker.read_text(encoding="utf-8")


def test_doctor_cloud_rejects_a_symlinked_harness_dir(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert install_skills(ROOT, project, "cloud") == []
    real = project / "real-claude"
    os.rename(project / ".claude", real)
    (project / ".claude").symlink_to(real, target_is_directory=True)
    replies = tmp_path / "replies"
    write_replies(replies, pinned_token(ROOT))
    code, _prompt, errors = doctor(ROOT, project, "cloud", replies, print_canary=False)
    assert code == 1
    assert any(error.startswith("claude:") and "must be a real copy" in error for error in errors)
    assert not any(error.startswith("codex:") for error in errors)


def test_install_replaces_a_dangling_owned_link(tmp_path: Path) -> None:
    home = tmp_path / "home"
    gone = tmp_path / "gone-checkout" / "skills" / "pstack" / "skills"
    link = home / ".claude" / "skills" / "poteto-mode"
    link.parent.mkdir(parents=True)
    link.symlink_to(gone / "poteto-mode", target_is_directory=True)
    stranger = home / ".agents" / "skills" / "statechart"
    stranger.parent.mkdir(parents=True)
    stranger.symlink_to(tmp_path / "elsewhere" / "statechart", target_is_directory=True)
    errors = install_skills(ROOT, home, "local")
    assert errors == [f"refusing to replace unowned path {stranger}"]
    stranger.unlink()
    assert install_skills(ROOT, home, "local") == []
    assert link.resolve() == (ROOT / "skills" / "pstack" / "skills" / "poteto-mode").resolve()


@pytest.mark.parametrize("why", ["worktree", "temp"])
def test_cli_local_install_refuses_a_worktree_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], why: str
) -> None:
    root = tiny_tree(tmp_path)
    bump_pin(root, "a" * 40, "0.15.36")
    if why == "worktree":
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "not-temp"))
    monkeypatch.setattr(cli, "ROOT", root)
    home = tmp_path / "home"
    assert cli.main(["install-skills", "--home", str(home)]) == 1
    assert "install from a stable checkout" in capsys.readouterr().err
    assert not home.exists()
    assert cli.main(["install-skills", "--allow-unstable-checkout", "--home", str(home)]) == 0
    assert (home / ".claude" / "skills" / "poteto-mode").is_symlink()
    assert cli.main(["install-skills", "--cloud", "--project", str(tmp_path / "project")]) == 0


def test_project_flag_requires_cloud(tmp_path: Path) -> None:
    result = run(["install-skills", "--project", str(tmp_path)])
    assert result.returncode == 2
    assert "--cloud" in result.stderr


def test_print_canary_rejects_responses(tmp_path: Path) -> None:
    result = run(["doctor", "--print-canary", "--responses", str(tmp_path)])
    assert result.returncode == 2
