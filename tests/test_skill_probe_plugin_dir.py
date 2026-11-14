"""bin/skill-probe --plugin-dir (#176): load the pstack tree in place, score skill and agents."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path

import pytest

from lib import skill_probe as sp
from tests.test_skill_probe import CLAUDE_CMD, CODEX_CMD, FIXTURES, MACHINES, plan

TREE = FIXTURES / "plugin_tree" / "pstack"
TREE_CANARY = sp.skill_canary((TREE / "skills" / "poteto-mode" / "SKILL.md").read_text())
BOX_ONLY = MACHINES.split("  - name: mac")[0]
INSTALL_TOKENS = {"install", "marketplace", "plugin"}


def evidence(fixture: str, *, plugin: bool = True) -> sp.Evidence:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    return sp.claude_evidence(sp.parse_jsonl(text), TREE_CANARY, plugin=plugin)


def test_loaded_tree_records_skill_yes():
    e = evidence("claude_plugin_dir_loaded.jsonl")
    assert (e.read, e.read_via) == ("yes", "Skill")


def test_unloaded_tree_records_skill_no():
    e = evidence("claude_plugin_dir_unloaded.jsonl")
    assert (e.read, e.read_via) == ("no", None)


def test_prefixed_agents_resolve_yes():
    e = evidence("claude_plugin_dir_loaded.jsonl")
    assert (e.helper, e.pstack_poteto_agent, e.pstack_comment_sicko) == ("yes", "yes", "yes")


def test_prefixed_agents_missing_is_no():
    e = evidence("claude_plugin_dir_unloaded.jsonl")
    assert (e.pstack_poteto_agent, e.pstack_comment_sicko) == ("missing", "missing")
    e = evidence("claude_read_tool.jsonl")
    assert (e.pstack_poteto_agent, e.pstack_comment_sicko) == ("no", "no")
    assert sp.claude_evidence([], TREE_CANARY, plugin=True).pstack_comment_sicko == "unknown"


def test_plugin_fields_are_na_without_plugin_dir():
    for fixture in ("claude_plugin_dir_loaded.jsonl", "claude_slash.jsonl"):
        e = evidence(fixture, plugin=False)
        assert (e.pstack_poteto_agent, e.pstack_comment_sicko) == ("n/a", "n/a")
    old = {
        "date": "2026-11-14", "device": "box", "harness": "claude", "style": "slash",
        "note": "", "evidence": {"read": "no", "read_via": None, "helper": "yes",
                                 "poteto_agent": "missing", "harness_version": "2.1.318"},
    }
    result = sp.ProbeResult.from_json(json.dumps(old))
    assert result.plugin_dir is None
    assert result.evidence.pstack_poteto_agent == "n/a"
    row = sp.render([result]).splitlines()[2]
    assert row.endswith("| missing | n/a | n/a |  |")


def test_render_shows_prefixed_columns_and_plugin_dir_note():
    e = evidence("claude_plugin_dir_unloaded.jsonl")
    result = sp.ProbeResult("2026-11-14", "box", "claude", sp.Style.SLASH, e, "", str(TREE))
    assert sp.ProbeResult.from_json(result.to_json()) == result
    table = sp.render([result]).splitlines()
    assert "| poteto-agent | pstack:poteto-agent | pstack:comment-sicko | Note |" in table[0]
    assert table[2].endswith("| slash | no |  | yes | missing | missing | missing | plugin-dir |")


def test_plugin_dir_inserted_after_executable(tmp_path):
    launches = plan(tmp_path, plugin_dir=str(TREE))
    assert {l.remote is None for l in launches} == {True, False}
    for launch in launches:
        assert launch.argv == (CLAUDE_CMD[0], "--plugin-dir", str(TREE), *CLAUDE_CMD[1:])
        assert launch.skill_md == str(TREE / "skills" / "poteto-mode" / "SKILL.md")
        argv = sp.launch_argv(launch)
        assert not INSTALL_TOKENS & set(argv)
        if launch.remote is not None:
            assert f"--plugin-dir {TREE}" in argv[-1]


def test_plugin_dir_rejected_for_codex_and_relative_paths(tmp_path, capsys):
    with pytest.raises(sp.ProbeError, match="Claude flag"):
        plan(tmp_path, "codex-bot", CODEX_CMD, plugin_dir=str(TREE))
    machines_path = tmp_path / "machines.yml"
    machines_path.write_text(BOX_ONLY)
    config = _waiter_config(tmp_path, ["claude", "-p"])
    base = ["run", "--bot", "claude-bot", "--machines", str(machines_path),
            "--config", str(config), "--dry-run"]
    assert sp.main([*base, "--plugin-dir", "plugin_tree/pstack"]) == 1
    assert "absolute path" in capsys.readouterr().err
    empty = tmp_path / "empty-tree"
    empty.mkdir()
    assert sp.main([*base, "--plugin-dir", str(empty)]) == 1
    assert "skills/poteto-mode/SKILL.md" in capsys.readouterr().err
    assert sp.main([*base, "--plugin-dir", str(TREE)]) == 0
    dry = capsys.readouterr().out
    assert f"claude --plugin-dir {TREE} -p" in dry
    assert "/pstack:poteto-mode" in dry


@pytest.mark.parametrize("style", [s.style for s in sp.STYLES if "claude" in s.harnesses])
def test_plugin_prompt_asks_for_prefixed_agents_and_never_holds_canary(style):
    path = str(TREE / "skills" / "poteto-mode" / "SKILL.md")
    prompt = sp.prompt_for(style, "claude", path, TREE_CANARY, plugin=True)
    assert TREE_CANARY not in prompt
    assert "of type pstack:poteto-agent" in prompt
    assert "of type pstack:comment-sicko" in prompt
    first = prompt.splitlines()[0]
    assert first == {
        sp.Style.RUN_AS: "Run as poteto-mode.",
        sp.Style.READ_PATH: f"Read {path} and follow it.",
        sp.Style.SLASH: "/pstack:poteto-mode",
    }[style]
    with pytest.raises(sp.ProbeError):
        sp.prompt_for(sp.Style.READ_PATH, "claude", TREE_CANARY, TREE_CANARY, plugin=True)


FAKE_CLAUDE = """\
#!/bin/sh
[ "$1" = "--plugin-dir" ] || { echo "no --plugin-dir first" >&2; exit 9; }
tree=$2
cat > stdin.txt
slug=$(printf %s "$PWD" | sed 's/[^A-Za-z0-9]/-/g')
mkdir -p "$CLAUDE_CONFIG_DIR/projects/$slug"
if [ -d "$tree/agents" ]; then t=loaded; else t=unloaded; fi
cp "$FIXTURES/claude_plugin_dir_$t.jsonl" "$CLAUDE_CONFIG_DIR/projects/$slug/s.jsonl"
"""


def _waiter_config(tmp_path: Path, command: list[str]) -> Path:
    folder = tmp_path / "cfg"
    folder.mkdir(mode=0o700)
    path = folder / "waiter-claude-bot.yml"
    path.write_text(json.dumps({"bot": "claude-bot", "command": command}))
    path.chmod(0o600)
    return path


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.mark.parametrize("stripped", [False, True], ids=["full-tree", "no-agents"])
def test_fake_tree_run_writes_nothing_into_bot_home(tmp_path, monkeypatch, stripped):
    home = tmp_path / "bot-home"
    (home / ".claude" / "skills" / "other").mkdir(parents=True)
    (home / ".claude" / "skills" / "other" / "SKILL.md").write_text("# other\n\nbody\n")
    (home / ".claude" / "settings.json").write_text("{}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("FIXTURES", str(FIXTURES))
    tree = tmp_path / "tree" / "pstack"
    shutil.copytree(TREE, tree)
    if stripped:
        shutil.rmtree(tree / "agents")
    harness = tmp_path / "bin" / "claude"
    harness.parent.mkdir()
    harness.write_text(FAKE_CLAUDE)
    harness.chmod(harness.stat().st_mode | stat.S_IXUSR)
    machines_path = tmp_path / "machines.yml"
    machines_path.write_text(BOX_ONLY)
    config = _waiter_config(tmp_path, [str(harness), "-p"])
    before_home, before_tree = _snapshot(home), _hashes(tree)
    out = tmp_path / "out"
    base = ["run", "--bot", "claude-bot", "--machines", str(machines_path), "--config",
            str(config), "--device", "box", "--plugin-dir", str(tree)]

    assert sp.main([*base, "--work", str(tmp_path / "work"), "--out", str(out)]) == 0

    results = [sp.ProbeResult.from_json(p.read_text()) for p in sorted(out.glob("*.json"))]
    assert [r.style for r in results] == [sp.Style.READ_PATH, sp.Style.RUN_AS, sp.Style.SLASH]
    want = ("no", "missing", "missing") if stripped else ("yes", "yes", "yes")
    for result in results:
        e = result.evidence
        assert (e.read, e.pstack_poteto_agent, e.pstack_comment_sicko) == want
        assert result.plugin_dir == str(tree)
    assert _snapshot(home) == before_home
    assert not (home / ".claude" / "plugins").exists()
    assert _hashes(tree) == before_tree
    assert sp.main([*base, "--work", str(tmp_path / "w2"),
                    "--out", str(home / ".claude" / "probe")]) == 1
    assert sp.main([*base, "--work", str(home / ".agents" / "w"),
                    "--out", str(tmp_path / "o2")]) == 1
    assert _snapshot(home) == before_home
