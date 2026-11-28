from __future__ import annotations

import ast
import re
from pathlib import Path

from lib.dispatch.constants import GROK_WAKE_REASONS
from lib.orchestrate import answers_comment, choose_route, go_comment
from tests.test_hooks import FakeBot, say, setup
from tests.test_orchestrate import request_for
from tests.test_wake_payload import GO, on_pr

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "grok" / "orchestrate-project" / "SKILL.md"
DETAIL_REASONS = ("repeat-block", "criterion-unsatisfiable", "preflight-missing")
GO_WITHOUT_STATE = "kind=go must set state=planning or state=building"


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


def wake_table() -> dict[str, tuple[str, str]]:
    text = SKILL.read_text(encoding="utf-8")
    section = text.split("\n## Wakes\n", 1)[1].split("\n## ", 1)[0]
    table = {}
    for line in section.splitlines():
        if line.startswith("| `"):
            wake, cause, todo = (cell.strip() for cell in line.strip("|").split(" | "))
            assert wake.strip("`") not in table, wake
            table[wake.strip("`")] = (cause, todo)
    return table


def wake_rows() -> list[str]:
    return list(wake_table())


def test_code_wake_actions_include_the_constant_and_clarify():
    actions = code_wake_actions()
    assert set(GROK_WAKE_REASONS) <= actions
    assert "clarify" in actions


def test_detail_reasons_are_written_by_code():
    source = lib_source()
    for reason in DETAIL_REASONS:
        assert f'"{reason}: ' in source or f'f"{reason}: ' in source, reason
    assert f'"{GO_WITHOUT_STATE}"' in source


def test_every_wake_reason_in_code_has_one_row():
    assert set(wake_rows()) == code_wake_actions() | set(DETAIL_REASONS) | {GO_WITHOUT_STATE}


def test_go_without_state_recovers_through_a_generated_go(tmp_path):
    todo = wake_table()[GO_WITHOUT_STATE][1]
    assert "go_comment" in todo
    assert "Do not post `answers_comment`" in todo
    assert "`go`" not in wake_table()["clarify"][0]

    dispatcher, queue = setup(tmp_path)
    bare_go = say("906", GO.format(state=""), "grok-login", 1)
    dispatcher.process(on_pr(bare_go))
    wake = FakeBot(queue, "grok").drain()[0].request
    assert wake.action == "clarify"
    assert GO_WITHOUT_STATE in str(wake.detail)

    answer = say("907", answers_comment("Which state?", "building"), "grok-login", 2)
    dispatcher.process(on_pr(bare_go, answer))
    assert queue.claim("claude") is None

    generated = go_comment(choose_route(request_for("examples/cleffy/factory.md")), "none")
    rego = say("908", generated, "grok-login", 3)
    dispatcher.process(on_pr(bare_go, answer, rego))
    assert queue.claim("claude").request.action in ("plan", "build")


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


def test_the_engine_notice_marker_the_code_posts_is_named_in_commands():
    from lib.dispatch.review import engine_marker

    commands = SKILL.read_text(encoding="utf-8").split("\n## Commands\n", 1)[1].split("\n## ", 1)[0]
    marker = engine_marker("cap", "1").split()[1]
    assert marker == "orch-engine"
    [line] = [line for line in commands.splitlines() if f"`{marker}`" in line]
    assert "needs no action" in line
