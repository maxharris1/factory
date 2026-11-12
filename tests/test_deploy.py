import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

FAKE_VENV_PYTHON = """#!/usr/bin/python3
import os
import sqlite3
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] == ["-m", "pip", "install"]:
    if args[3:] != ["-r", os.environ["EXPECTED_REQUIREMENTS"]]:
        raise SystemExit(f"unexpected pip arguments: {args}")
    (Path(os.environ["HOME"]) / "requirements-installed").touch()
elif len(args) >= 2 and args[0].endswith("/bin/orch") and args[1] == "init-db":
    database = Path(os.environ["ORCH_DB"])
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS handled_comments (id TEXT)")
else:
    raise SystemExit(f"unexpected venv python arguments: {args}")
"""


@pytest.mark.parametrize("installer", ["uv", "venv"])
def test_install_is_idempotent_and_preserves_the_shared_database(
    tmp_path: Path,
    installer: str,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    installer_path = fake_bin / ("uv" if installer == "uv" else "python3")
    if installer == "uv":
        installer_source = """#!/usr/bin/python3
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
    python = target / "bin/python"
    python.write_text(os.environ["FAKE_VENV_PYTHON"], encoding="utf-8")
    python.chmod(0o755)
elif sys.argv[1:3] == ["pip", "install"]:
    expected = [
        "pip",
        "install",
        "--python",
        str(Path(os.environ["HOME"]) / ".local/share/factory/venv/bin/python"),
        "-r",
        os.environ["EXPECTED_REQUIREMENTS"],
    ]
    if sys.argv[1:] != expected:
        raise SystemExit(f"unexpected uv pip arguments: {sys.argv[1:]}")
    (Path(os.environ["HOME"]) / "requirements-installed").touch()
else:
    raise SystemExit(f"unexpected uv arguments: {sys.argv[1:]}")
"""
    else:
        installer_source = """#!/usr/bin/python3
import os
import shutil
import sys
from pathlib import Path

if sys.argv[1:4] != ["-m", "venv", "--clear"] or len(sys.argv) != 5:
    raise SystemExit(f"unexpected python3 arguments: {sys.argv[1:]}")
target = Path(sys.argv[4])
shutil.rmtree(target, ignore_errors=True)
(target / "bin").mkdir(parents=True)
python = target / "bin/python"
python.write_text(os.environ["FAKE_VENV_PYTHON"], encoding="utf-8")
python.chmod(0o755)
"""
    installer_path.write_text(installer_source, encoding="utf-8")
    installer_path.chmod(0o755)
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
            "EXPECTED_REQUIREMENTS": str(ROOT / "requirements.txt"),
            "FAKE_VENV_PYTHON": FAKE_VENV_PYTHON,
            "HOME": str(tmp_path),
            "ORCH_CONFIG": str(orch_config),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
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
    assert (tmp_path / "requirements-installed").is_file()
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


DASHBOARD_UNIT = ROOT / "deploy" / "factory-dashboard-http.service"


def _unit_values(key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in DASHBOARD_UNIT.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{key}=")
    ]


def test_dashboard_server_unit_binds_only_the_tailnet_address() -> None:
    text = DASHBOARD_UNIT.read_text(encoding="utf-8")
    (exec_start,) = _unit_values("ExecStart")
    assert "-m http.server" in exec_start
    assert "--bind ${TAILNET_IP}" in exec_start
    assert "--directory ${DASHBOARD_DIR}" in exec_start
    for forbidden in ("0.0.0.0", "--bind ::", "[::]", "--bind 127.", "--bind localhost"):
        assert forbidden not in text
    # A unit with no TAILNET_IP, or a non-tailnet one, refuses to start.
    (guard,) = _unit_values("ExecStartPre")
    assert "100.64.0.0/10" in guard
    assert _unit_values("EnvironmentFile") == ["%h/.config/factory-dashboard/http.env"]


@pytest.mark.parametrize(
    "address,ok",
    [
        ("100.101.102.103", True),
        ("100.64.0.1", True),
        ("0.0.0.0", False),
        ("127.0.0.1", False),
        ("192.168.1.20", False),
        ("8.8.8.8", False),
        ("::", False),
        ("", False),
    ],
)
def test_dashboard_server_guard_accepts_only_tailnet_ipv4(address: str, ok: bool) -> None:
    (guard,) = _unit_values("ExecStartPre")
    assert guard.startswith("/usr/bin/python3 -c ")
    code = guard[len("/usr/bin/python3 -c ") :].strip("'")
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"TAILNET_IP": address},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is ok, result.stderr


def test_dashboard_server_unit_is_documented_not_installed() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "deploy/factory-dashboard-http.service" in readme
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "factory-dashboard" not in install
