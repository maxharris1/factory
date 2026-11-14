import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lib.pstack_models import (
    MAP_PATH,
    SETUP_SKILL,
    ModelMapError,
    brief_lines,
    load_map,
    pinned_roles,
    rules_text,
)
from lib.pstack_pin import ROOT, load_pin
from lib.schema import HARNESSES

PANELS = {
    "arena runners",
    "arena cross-judge pool",
    "architect runners",
    "interrogate reviewers",
}
CLAUDE_VALUE = re.compile(r"^(opus|sonnet|haiku|fable) (max|xhigh|high|medium|low)$")


def step5_lines() -> list[str]:
    text = (ROOT / SETUP_SKILL).read_text(encoding="utf-8")
    block = text.split("### 5. Write the rule", 1)[1].split("```", 2)[1]
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# budget:"))
    return [line for line in lines[start + 1 :] if line.strip()]


def entries(value: str | tuple[str, ...]) -> tuple[str, ...]:
    return value if isinstance(value, tuple) else (value,)


def tmp_root(tmp_path: Path) -> Path:
    for rel in (SETUP_SKILL, MAP_PATH):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dest)
    return tmp_path


def edit_map(root: Path, old: str, new: str, count: int = 1) -> None:
    path = root / MAP_PATH
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def role_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def test_roles_read_from_pin():
    roles = pinned_roles()
    expected = [line.split(": ", 1)[0] for line in step5_lines()]
    assert [role for role, _ in roles] == expected
    assert len(roles) == 17
    assert {role for role, panel in roles if panel} == PANELS
    digest = hashlib.sha256((ROOT / SETUP_SKILL).read_bytes()).hexdigest()
    assert load_pin(ROOT).hashes()["skills/setup-pstack/SKILL.md"] == digest


def test_every_role_every_harness():
    mapping = load_map()
    assert set(mapping) == set(HARNESSES)
    order = [role for role, _ in pinned_roles()]
    for harness in HARNESSES:
        lines = mapping[harness]
        assert [line.role for line in lines] == order
        for line in lines:
            assert isinstance(line.value, tuple) == (line.role in PANELS)
            if line.role in PANELS:
                assert len(line.value) == 2


def test_new_pinned_role_fails(tmp_path):
    root = tmp_root(tmp_path)
    skill = root / SETUP_SKILL
    text = skill.read_text(encoding="utf-8")
    anchor = "interrogate reviewers: claude-opus-5-5-xhigh, grok-4.7-xhigh-fast\n"
    assert anchor in text
    skill.write_text(text.replace(anchor, anchor + "new role: grok-4.7-xhigh-fast\n"), encoding="utf-8")
    assert "new role" in [role for role, _ in pinned_roles(root)]
    with pytest.raises(ModelMapError, match="new role"):
        load_map(root)


def test_dropped_pinned_role_fails(tmp_path):
    root = tmp_root(tmp_path)
    skill = root / SETUP_SKILL
    text = skill.read_text(encoding="utf-8")
    line = "hillclimb: grok-4.7-xhigh-fast\n"
    assert line in text
    skill.write_text(text.replace(line, ""), encoding="utf-8")
    with pytest.raises(ModelMapError, match="not pinned: hillclimb"):
        load_map(root)


def test_map_role_missing_fails(tmp_path):
    root = tmp_root(tmp_path)
    edit_map(root, "  - swarm workers\n", "")
    with pytest.raises(ModelMapError, match="missing: swarm workers"):
        load_map(root)


def test_missing_harness_fails(tmp_path):
    root = tmp_root(tmp_path)
    path = root / MAP_PATH
    text = path.read_text(encoding="utf-8")
    start = text.index("  cursor:\n")
    end = text.index("overrides:")
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    with pytest.raises(ModelMapError, match="cursor"):
        load_map(root)


def test_unknown_override_role_fails(tmp_path):
    root = tmp_root(tmp_path)
    edit_map(root, "overrides: {}", "overrides:\n  claude:\n    no such role: opus high")
    with pytest.raises(ModelMapError, match="no such role"):
        load_map(root)


def test_override_changes_one_role(tmp_path):
    root = tmp_root(tmp_path)
    edit_map(root, "overrides: {}", "overrides:\n  claude:\n    bug-fix: sonnet medium")
    lines = {line.role: line.value for line in load_map(root)["claude"]}
    assert lines["bug-fix"] == "sonnet medium"
    assert lines["perf-issue"] == "opus high"


def test_claude_values_adapter_format():
    for line in load_map()["claude"]:
        for entry in entries(line.value):
            assert CLAUDE_VALUE.fullmatch(entry), entry
            assert entry.split()[0] in {"opus", "sonnet", "haiku", "fable"}


@pytest.mark.parametrize("bad", ["opus-5.5 high", "claude-opus-5-5-high", "opus", "gpt-5 high"])
def test_claude_rejects_versioned(tmp_path, bad):
    root = tmp_root(tmp_path)
    edit_map(root, "default: opus high", f"default: {bad}")
    with pytest.raises(ModelMapError, match="claude"):
        load_map(root)


def test_claude_panel_entry_checked(tmp_path):
    root = tmp_root(tmp_path)
    edit_map(root, "panel: [opus high, opus high]", "panel: [opus high, opus-5.5 high]")
    with pytest.raises(ModelMapError, match="opus-5.5 high"):
        load_map(root)


def test_codex_helpers_sol():
    for line in load_map()["codex"]:
        for entry in entries(line.value):
            assert entry.split()[0] == "sol", entry
    for line in role_lines(brief_lines("codex")):
        value = line.split(": ", 1)[1]
        assert all(entry.split()[0] == "sol" for entry in value.split(", ")), line


def test_no_foreign_families():
    claude_text = brief_lines("claude") + rules_text()
    codex_text = brief_lines("codex")
    assert "grok-" not in claude_text
    assert "grok-" not in codex_text
    assert all("claude-" not in line for line in role_lines(codex_text))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("default: opus high", "default: grok-4.7-xhigh-fast"),
        ("default: sol high", "default: claude-opus-5-5-high"),
        ("default: sol high", "default: grok-4.7 high"),
        ("panel: [sol high, sol high]", "panel: [sol high, claude-opus-5-5 high]"),
    ],
)
def test_foreign_family_rejected(tmp_path, old, new):
    root = tmp_root(tmp_path)
    edit_map(root, old, new)
    with pytest.raises(ModelMapError):
        load_map(root)


def test_brief_and_rules_same_source():
    assert role_lines(rules_text()) == brief_lines("claude").splitlines()


def test_rules_follow_yaml(tmp_path):
    root = tmp_root(tmp_path)
    edit_map(root, "default: opus high", "default: sonnet medium")
    brief = brief_lines("claude", root)
    rules = rules_text(root)
    assert "feature, refactoring: sonnet medium" in brief.splitlines()
    assert "feature, refactoring: sonnet medium" in rules.splitlines()
    assert role_lines(rules) == brief.splitlines()


def test_rules_say_brief_wins():
    assert "the brief wins" in rules_text()


def test_rules_shape():
    text = rules_text()
    lines = text.splitlines()
    assert lines[0] != "---"
    assert lines[0].startswith("# pstack model configuration.")
    budget = [line for line in lines if line.startswith("# budget:")]
    assert budget == ["# budget: medium (high)"]
    after = lines[lines.index(budget[0]) + 1 :]
    assert [line.split(": ", 1)[0] for line in after] == [role for role, _ in pinned_roles()]
    assert "interrogate reviewers: opus high, opus high" in after
    assert text.endswith("\n")


def test_unknown_harness_brief_fails():
    with pytest.raises(ModelMapError, match="grok"):
        brief_lines("grok")


def test_cli_prints_outputs():
    rules = subprocess.run(
        [sys.executable, "-m", "lib.pstack_models", "rules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert rules.stdout == rules_text()
    brief = subprocess.run(
        [sys.executable, "-m", "lib.pstack_models", "brief", "codex"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert brief.stdout == brief_lines("codex")
