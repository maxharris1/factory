from __future__ import annotations

import ast
from pathlib import Path

from lib.dispatch.helpers import _verdict_outcome

DISPATCH = Path(__file__).resolve().parent.parent / "lib" / "dispatch"


def _methods() -> tuple[dict[str, ast.FunctionDef], dict[str, str]]:
    methods: dict[str, ast.FunctionDef] = {}
    constants: dict[str, str] = {}
    for path in sorted(DISPATCH.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                methods.setdefault(node.name, node)
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return methods, constants


def _strings(node: ast.expr, constants: dict[str, str]) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in constants:
        return {constants[node.id]}
    if isinstance(node, ast.JoinedStr) and isinstance(node.values[0], ast.Constant):
        return {f"{node.values[0].value}<formatted>"}
    if isinstance(node, ast.IfExp):
        return _strings(node.body, constants) | _strings(node.orelse, constants)
    return set()


def _handled_outcomes(roots: set[str]) -> set[str]:
    methods, constants = _methods()
    seen: set[str] = set()
    pending = list(roots)
    outcomes: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen or name not in methods:
            continue
        seen.add(name)
        for node in ast.walk(methods[name]):
            if (
                isinstance(node, ast.Tuple)
                and len(node.elts) == 2
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value is True
            ):
                outcomes |= _strings(node.elts[1], constants)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "outcome"
                for target in node.targets
            ):
                outcomes |= _strings(node.value, constants)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "_mark_handled" and len(node.args) == 3:
                    outcomes |= _strings(node.args[2], constants)
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == "self"
            ):
                pending.append(node.value.func.attr)
    return outcomes


def _process_comments_outcomes() -> tuple[set[str], set[str]]:
    methods, constants = _methods()

    def marked(nodes: list[ast.stmt]) -> set[str]:
        outcomes: set[str] = set()
        for call in ast.walk(ast.Module(body=nodes, type_ignores=[])):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_mark_handled"
            ):
                outcomes |= _strings(call.args[2], constants)
        return outcomes

    process = methods["_process_comments"]
    verdict: set[str] = set()
    for node in ast.walk(process):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "kind"
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "verdict"
        ):
            verdict |= marked(node.body)
    return verdict, marked(process.body) - verdict


VERDICT_HANDLERS = {"_t3", "_t4", "_revise_ci_gate"}
OTHER_HANDLERS = {"_t1", "_t2", "_t_plan", "_t5_questions", "_t5_answers", "_t_retry"}


def test_every_outcome_a_verdict_is_handled_with_is_a_verdict_outcome():
    outcomes = _handled_outcomes(VERDICT_HANDLERS) | _process_comments_outcomes()[0]

    assert {"stale-revise", "merge-queue", "held-base-update", "duplicate-verdict"} <= outcomes
    assert sorted(o for o in outcomes if not _verdict_outcome(o)) == []


def test_go_plan_ready_questions_answers_and_retry_outcomes_are_not_verdict_outcomes():
    outcomes = _handled_outcomes(OTHER_HANDLERS)
    shared = _handled_outcomes(VERDICT_HANDLERS)

    assert {"deduped-<formatted>", "queued-review", "ci-red-in-flight"} <= outcomes
    assert sorted(o for o in outcomes - shared if _verdict_outcome(o)) == []
    assert sorted(outcomes & shared) == ["stuck-ci-red-3"]


def test_outcomes_built_from_a_kind_are_verdict_outcomes_only_for_a_verdict():
    assert not _verdict_outcome("deduped-build")
    assert _verdict_outcome("failed-verdict-RuntimeError")
    assert not _verdict_outcome("failed-go-RuntimeError")


def test_outcomes_any_kind_can_be_handled_with_are_not_verdict_outcomes():
    outcomes = _process_comments_outcomes()[1]

    assert {"ticket-abandoned", "ticket-stuck"} <= outcomes
    assert sorted(o for o in outcomes if _verdict_outcome(o)) == []


def test_t7_marks_a_blocked_verdict_with_a_verdict_outcome_and_nothing_else():
    assert _verdict_outcome("woke-grok-blocked")
    assert _verdict_outcome("rejected-blocked-author")
    assert not _verdict_outcome("woke-grok-unparsed")
    assert not _verdict_outcome("rejected-stuck-author")
    assert not _verdict_outcome("ignored-unparsed-untrusted")
