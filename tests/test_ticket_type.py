from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["lib.dispatcher", "lib.dispatch.detail", "lib.poll_source", "lib.intake_live",
                                    "lib.feeder", "lib.ticket_type"])
def test_each_module_imports_on_its_own(module: str) -> None:
    run = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
