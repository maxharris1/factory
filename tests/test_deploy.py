import os
import sqlite3
import subprocess
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
    template = tmp_path / ".config/factory/webhook.env.example"
    assert template.read_text(encoding="utf-8") == (
        ROOT / "deploy/webhook.env.example"
    ).read_text(encoding="utf-8")
    assert template.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".config/factory/webhook.env").exists()


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


def test_webhook_template_names_every_accepted_variable() -> None:
    template = (ROOT / "deploy/webhook.env.example").read_text(encoding="utf-8")
    orch = (ROOT / "orch.yml").read_text(encoding="utf-8")
    for name in (
        "FACTORY_WEBHOOK_URL",
        "FACTORY_WEBHOOK_AUTH",
        "FACTORY_WEBHOOK_KEY",
    ):
        assert f"{name}=" in template
        assert name in orch


def test_run_poller_exports_both_env_files_unbuffered(tmp_path: Path) -> None:
    config = tmp_path / ".config/factory"
    config.mkdir(parents=True)
    (config / "webhook.env").write_text(
        'FACTORY_WEBHOOK_URL="https://webhook.test"\n'
        'FACTORY_WEBHOOK_AUTH="Bearer test-value"\n',
        encoding="utf-8",
    )
    (config / "orch-poll.env").write_text(
        'ORCH_DB="/tmp/orch.db"\n',
        encoding="utf-8",
    )
    orch = tmp_path / ".local/bin/orch"
    orch.parent.mkdir(parents=True)
    orch.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s|%s|%s|%s\\n" "$*" "$PYTHONUNBUFFERED" '
        '"${FACTORY_WEBHOOK_AUTH:+set}" "$ORCH_DB"\n',
        encoding="utf-8",
    )
    orch.chmod(0o755)

    result = subprocess.run(
        ["bash", "deploy/run-poller.sh", "--once"],
        cwd=ROOT,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "poll --once|1|set|/tmp/orch.db\n"


def test_orch_poll_service_is_unbuffered_and_reads_webhook_env() -> None:
    unit = (ROOT / "deploy/orch-poll.service").read_text(encoding="utf-8")
    assert "Environment=PYTHONUNBUFFERED=1" in unit
    assert "EnvironmentFile=-%h/.config/factory/webhook.env" in unit
