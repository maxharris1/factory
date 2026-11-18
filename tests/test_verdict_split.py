import ast
from pathlib import Path

import lib.dispatch.verdict
import lib.dispatcher
from lib.dispatch.verdict import VerdictMixin
from tests.test_dispatch_split import _runtime_imports
from tests.test_wait import _exec_calls

ROOT = Path(__file__).resolve().parents[1]
VERDICT = ROOT / "lib" / "dispatch" / "verdict"
VERDICT_FILES = sorted(VERDICT.glob("*.py"))
RULE_FILES = [path for path in VERDICT_FILES if path.name != "__init__.py"]

NAMES_BEFORE_SPLIT = (
    "AlreadyMergedShip", "CIResult", "Comment", "HandoffError", "JobRequest",
    "MERGED_BEFORE_HANDOFF", "Mapping", "ReviseCIGate", "TrackedTicket", "VerdictMixin",
    "_NOT_FINISHED", "_comment_link", "_comment_order", "_holds", "_is_full_sha",
    "_is_patch_id", "_matching_head", "_round_number", "_scope", "annotations",
    "dataclass", "parse_comment", "sqlite3", "write_workflow_state",
)

RULE_MODULE = {
    "_t3": "t3",
    "_record_revise": "t3",
    "_spend_review_round": "t3",
    "_t3_unsat": "t3",
    "_criterion_text": "t3",
    "_revise_ci_gate": "t3",
    "_repeat_trigger": "repeat",
    "_accepted_revises": "repeat",
    "_repeat_stops": "repeat",
    "_repeat_detail": "repeat",
    "_t3_repeat": "repeat",
    "_t4": "t4",
    "_approve_ci_red_fix": "t4",
    "_held_ci_red": "t4",
    "_held_ci_fix_out": "t4",
    "_held_approve_covers": "t4",
    "_latest_fix_answer": "t4",
    "_held_approval_gate": "t4",
    "_ship_approval": "t4",
    "_ship_covered": "t4",
    "_approve_refusal": "t4",
    "_paused_for_qa": "t4",
    "_t_blocked": "blocked",
    "_blocked_by_another": "blocked",
    "_blocked_review_recorded": "blocked",
    "_record_blocked_review": "blocked",
    "_record_approval": "record",
    "_approval_covers": "coverage",
    "_registered_review_head": "coverage",
    "_red_ci_approval_covering": "pending",
    "_shipping_approval": "pending",
    "_shipped_approval": "shipped",
    "_previewed_head": "preview",
    "_latest_review_verdict": "latest",
}

DISPATCH_MIXIN_MODULES = {
    f"lib.dispatch.{name}"
    for name in (
        "caps", "comments", "detail", "followups", "holds", "jobs", "merge_queue",
        "plan", "poll", "questions", "ready", "retry", "schema", "ship", "tracking",
    )
}


def test_every_name_the_old_module_exported_still_resolves() -> None:
    for name in NAMES_BEFORE_SPLIT:
        assert hasattr(lib.dispatch.verdict, name), name
        assert hasattr(lib.dispatcher, name), name
    assert lib.dispatcher.VerdictMixin is VerdictMixin
    assert lib.dispatcher.ReviseCIGate is lib.dispatch.verdict.ReviseCIGate
    assert lib.dispatch.verdict.ReviseCIGate.__module__ == "lib.dispatch.verdict.t3"


def test_each_verdict_method_is_defined_once_in_its_rule_module() -> None:
    assert not [value for value in vars(VerdictMixin).values() if callable(value)]
    owners: dict[str, type] = {}
    for cls in VerdictMixin.__mro__[1:-1]:
        for name, value in vars(cls).items():
            if callable(value):
                assert name not in owners, (name, owners.get(name), cls)
                owners[name] = cls
    assert {name: cls.__module__ for name, cls in owners.items()} == {
        name: f"lib.dispatch.verdict.{module}" for name, module in RULE_MODULE.items()
    }


def test_the_package_init_only_imports_and_composes() -> None:
    tree = ast.parse((VERDICT / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        elif isinstance(node, ast.ClassDef):
            assert node.name == "VerdictMixin"
            assert all(isinstance(item, ast.Pass) for item in node.body)
        else:
            assert isinstance(node, (ast.Import, ast.ImportFrom)), ast.dump(node)


def test_no_rule_module_imports_another_mixin_or_the_dispatcher() -> None:
    own_rules = {f"lib.dispatch.verdict.{path.stem}" for path in RULE_FILES}
    for path in RULE_FILES:
        imports = _runtime_imports(path)
        assert "lib.dispatcher" not in imports, path
        assert not {name for name in imports if name.startswith("lib.dispatch.verdict")}, path
        assert not DISPATCH_MIXIN_MODULES & imports, path
    init_imports = _runtime_imports(VERDICT / "__init__.py")
    assert "lib.dispatcher" not in init_imports
    assert not DISPATCH_MIXIN_MODULES & init_imports
    assert {
        name for name in init_imports if name.startswith("lib.dispatch.verdict")
    } == own_rules


def test_the_package_keeps_the_dispatcher_invariants() -> None:
    forbidden = {"subprocess", "os", "lib.waiter", "lib.job_run", "lib.brief", "lib.placed_run"}
    for path in VERDICT_FILES:
        assert not forbidden & _runtime_imports(path), path
        assert _exec_calls(path) == [], path
        text = path.read_text(encoding="utf-8")
        assert "90 minutes" not in text, path
        assert "job_timeout: 90" not in text, path


def test_the_readme_names_every_verdict_module() -> None:
    readme = (ROOT / "lib" / "dispatch" / "README.md").read_text(encoding="utf-8")
    for path in VERDICT_FILES:
        assert f"`verdict/{path.name}`" in readme, path.name
