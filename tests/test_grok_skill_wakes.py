from __future__ import annotations

import ast
import re
from pathlib import Path

from lib.dispatch.constants import GROK_WAKE_REASONS

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "grok" / "orchestrate-project" / "SKILL.md"
DETAIL_REASONS = ("repeat-block", "criterion-unsatisfiable", "preflight-missing")


def _keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def code_wake_actions() -> set[str]:
    actions = set(GROK_WAKE_REASONS)
    for path in (ROOT / "lib").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if _callee(node) == "JobRequest" and _keyword(node, "recipient") == "grok":
                actions.add(_keyword(node, "action"))
            elif _callee(node) == "wake_grok_once":
                actions.add(_keyword(node, "reason"))
    actions.discard(None)
    return actions


def lib_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "lib").rglob("*.py"))


def wake_rows() -> list[str]:
    text = SKILL.read_text(encoding="utf-8")
    section = text.split("\n## Wakes\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    return [re.match(r"\| `([^`]+)`", row).group(1) for row in rows]


def test_code_wake_actions_include_the_constant_and_clarify():
    actions = code_wake_actions()
    assert set(GROK_WAKE_REASONS) <= actions
    assert "clarify" in actions


def test_detail_reasons_are_written_by_code():
    source = lib_source()
    for reason in DETAIL_REASONS:
        assert f'"{reason}: ' in source or f'f"{reason}: ' in source, reason


def test_every_wake_reason_in_code_has_one_row():
    rows = wake_rows()
    assert len(rows) == len(set(rows)), rows
    assert set(rows) == code_wake_actions() | set(DETAIL_REASONS)


def test_no_hand_written_header():
    text = SKILL.read_text(encoding="utf-8")
    assert "<!--" not in text
    assert re.search(r"orch:v1\s+kind=", text) is None


def test_no_ok_as_max():
    text = SKILL.read_text(encoding="utf-8")
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if "kind=ok" in sentence:
            assert re.match(r"\W*Never\b", sentence), sentence
    assert "round=" not in text
