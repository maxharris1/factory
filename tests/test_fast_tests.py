import subprocess
import sys
from pathlib import Path

from lib.factory_md import load_factory_md
from tests.conftest import FAST_TEST_FILES

ROOT = Path(__file__).resolve().parents[1]


def _collected(*args: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_every_fast_test_file_exists() -> None:
    missing = sorted(name for name in FAST_TEST_FILES if not (ROOT / "tests" / name).is_file())
    assert missing == []


def test_fast_selects_the_listed_files_and_no_other() -> None:
    collected = _collected("-m", "fast", "tests/test_spec_approved_ready.py", "tests/test_qa_outbound_doc.py")
    assert collected, "a listed file has fast tests"
    assert all(node.startswith("tests/test_spec_approved_ready.py::") for node in collected)


def test_pre_ready_runs_the_fast_tests_alongside_the_eval_report_check() -> None:
    config, findings = load_factory_md(ROOT / "factory.md")
    assert config is not None and config.pre_ready is not None, findings
    command = config.pre_ready.command
    assert "tests/test_eval_report.py" in command
    assert '"$ORCH_PYTHON" -m pytest -q -p no:cacheprovider -m fast' in command
