from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from lib.factory_md import load_factory_md
from lib.pre_ready import check_environ, run

ROOT = Path(__file__).resolve().parent.parent


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.serial
@pytest.mark.timeout(360)
def test_the_gate_passes_when_path_python3_is_not_the_install_venv(tmp_path: Path) -> None:
    config, _ = load_factory_md(ROOT / "factory.md")
    assert config is not None and config.pre_ready is not None
    head = git(ROOT, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", "--no-checkout", str(ROOT), str(checkout))
    git(checkout, "checkout", "-q", "--detach", head)
    bare_host = tmp_path / "bin"
    bare_host.mkdir()
    for name in ("python3", "python"):
        fake = bare_host / name
        fake.write_text("#!/bin/sh\necho \"no factory packages here\"\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
    caller = {"PATH": f"{bare_host}:/usr/bin:/bin", "HOME": os.environ.get("HOME", str(tmp_path))}

    result = run(config.pre_ready, checkout, head, check_environ(caller, None))

    assert result.passed, f"{result.reason}\n{result.tail}"
    assert "no factory packages here" not in result.tail


def requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9_.-]+)==", line)
        if match:
            names.add(match.group(1).lower())
    return names


def test_the_install_requirements_provision_what_the_gate_imports() -> None:
    assert {"pyyaml", "pytest", "pytest-timeout"} <= requirement_names(ROOT / "requirements.txt")
