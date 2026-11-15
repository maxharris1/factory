import ast
from pathlib import Path

import lib.dispatch.caps
import lib.dispatcher
from lib.dispatch.caps import CapsMixin
from tests.test_dispatch_split import _runtime_imports
from tests.test_wait import _exec_calls

ROOT = Path(__file__).resolve().parents[1]
CAPS = ROOT / "lib" / "dispatch" / "caps"
CAPS_FILES = sorted(CAPS.glob("*.py"))
RULE_FILES = [path for path in CAPS_FILES if path.name != "__init__.py"]

NAMES_BEFORE_SPLIT = (
    "CapsMixin", "Comment", "JobRequest", "Mapping", "TrackedTicket", "WATCHED_STATES",
    "_comment_link", "_route_go", "_scope", "annotations", "sqlite3",
)

RULE_MODULE = {
    "_t1": "t1",
    "_resume_after_cap": "resume",
    "_resume_unsatisfiable": "resume",
    "_unsat_parked": "resume",
    "_resume_after_blocked": "resume",
    "_over_cap": "effective",
    "_config_cap": "effective",
    "_effective_cap": "effective",
    "_wake_capped": "wake",
    "_auto_granted_cap_extra": "auto_extra",
    "_record_auto_extra": "auto_extra",
    "_drain_auto_extra": "auto_extra",
}

DISPATCH_MIXIN_MODULES = {
    f"lib.dispatch.{name}"
    for name in (
        "caps", "comments", "detail", "holds", "jobs", "merge_queue", "plan", "poll",
        "questions", "ready", "schema", "ship", "tracking", "verdict",
    )
}


def test_every_name_the_old_module_exported_still_resolves() -> None:
    for name in NAMES_BEFORE_SPLIT:
        assert hasattr(lib.dispatch.caps, name), name
        assert hasattr(lib.dispatcher, name), name
    assert lib.dispatcher.CapsMixin is CapsMixin


def test_each_cap_method_is_defined_once_in_its_rule_module() -> None:
    assert not [value for value in vars(CapsMixin).values() if callable(value)]
    owners: dict[str, type] = {}
    for cls in CapsMixin.__mro__[1:-1]:
        for name, value in vars(cls).items():
            if callable(value):
                assert name not in owners, (name, owners.get(name), cls)
                owners[name] = cls
    assert {name: cls.__module__ for name, cls in owners.items()} == {
        name: f"lib.dispatch.caps.{module}" for name, module in RULE_MODULE.items()
    }


def test_the_package_init_only_imports_and_composes() -> None:
    tree = ast.parse((CAPS / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        elif isinstance(node, ast.ClassDef):
            assert node.name == "CapsMixin"
            assert all(isinstance(item, ast.Pass) for item in node.body)
        else:
            assert isinstance(node, (ast.Import, ast.ImportFrom)), ast.dump(node)


def test_no_rule_module_imports_another_mixin_or_the_dispatcher() -> None:
    own_rules = {f"lib.dispatch.caps.{path.stem}" for path in RULE_FILES}
    for path in RULE_FILES:
        imports = _runtime_imports(path)
        assert "lib.dispatcher" not in imports, path
        assert not {name for name in imports if name.startswith("lib.dispatch.caps")}, path
        assert not DISPATCH_MIXIN_MODULES & imports, path
    init_imports = _runtime_imports(CAPS / "__init__.py")
    assert "lib.dispatcher" not in init_imports
    assert not DISPATCH_MIXIN_MODULES & init_imports
    assert {name for name in init_imports if name.startswith("lib.dispatch.caps")} == own_rules


def test_the_package_keeps_the_dispatcher_invariants() -> None:
    forbidden = {"subprocess", "os", "lib.waiter", "lib.job_run", "lib.brief", "lib.placed_run"}
    for path in CAPS_FILES:
        assert not forbidden & _runtime_imports(path), path
        assert _exec_calls(path) == [], path
        text = path.read_text(encoding="utf-8")
        assert "90 minutes" not in text, path
        assert "job_timeout: 90" not in text, path


def test_the_readme_names_every_caps_module() -> None:
    readme = (ROOT / "lib" / "dispatch" / "README.md").read_text(encoding="utf-8")
    for path in CAPS_FILES:
        assert f"`caps/{path.name}`" in readme, path.name
