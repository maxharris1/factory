import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_install_is_idempotent_and_preserves_the_shared_database(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        """#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

if sys.argv[1] == "venv":
    if os.environ.get("UV_VENV_CLEAR") != "1":
        raise SystemExit("UV_VENV_CLEAR was not set")
    target = Path(sys.argv[2])
    shutil.rmtree(target, ignore_errors=True)
    (target / "bin").mkdir(parents=True)
    (target / "bin/python").symlink_to(sys.executable)
elif sys.argv[1:3] != ["pip", "install"]:
    raise SystemExit(f"unexpected uv arguments: {sys.argv[1:]}")
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    orch_config = tmp_path / "orch.yml"
    orch_config.write_text(
        (ROOT / "orch.yml").read_text(encoding="utf-8").replace(
            "REPLACE_WITH_LINEAR_PROJECT_ID",
            "test-linear-project-id",
        )
        + "\ntrusted_linear_users:\n  test-user-id: [grok-bot]\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(tmp_path),
            "ORCH_CONFIG": str(orch_config),
            "PATH": f"{fake_bin}:{environment['PATH']}",
        }
    )

    first = subprocess.run(
        ["bash", "deploy/install.sh"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    database = tmp_path / ".local/state/factory/orch.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE install_witness (value TEXT)")
        connection.execute("INSERT INTO install_witness VALUES ('kept')")

    second = subprocess.run(
        ["bash", "deploy/install.sh"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM install_witness"
        ).fetchone() == ("kept",)
    wrapper = tmp_path / ".local/bin/orch"
    assert "# factory-orch-wrapper" in wrapper.read_text(encoding="utf-8")


def test_profile_snippet_is_safe_before_install(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -e; source deploy/profile.sh; printf ok",
        ],
        cwd=ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "ok"
