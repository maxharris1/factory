import os
import signal
import sqlite3
import subprocess
import sys
import time
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


def _install_environment(tmp_path: Path, installer: str) -> dict[str, str]:
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
            # Never a real crontab; autostart tests point this at a fake.
            "ORCH_CRONTAB": str(fake_bin / "no-such-crontab"),
        }
    )
    return environment


@pytest.mark.parametrize("installer", ["uv", "venv"])
def test_install_is_idempotent_and_preserves_the_shared_database(
    tmp_path: Path,
    installer: str,
) -> None:
    environment = _install_environment(tmp_path, installer)

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


DASHBOARD_UNIT = ROOT / "deploy" / "factory-dashboard-http.service"
DASHBOARD_SERVER = ROOT / "deploy" / "factory-dashboard-http.py"
SERVER_ON_NODE = "%h/.local/lib/factory-dashboard/factory-dashboard-http.py"


def _unit_values(key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in DASHBOARD_UNIT.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{key}=")
    ]


def test_dashboard_server_unit_binds_only_the_tailnet_address() -> None:
    text = DASHBOARD_UNIT.read_text(encoding="utf-8")
    # Preservation guard: no wildcard, loopback or other bind in the unit.
    for forbidden in ("0.0.0.0", "--bind ::", "[::]", "--bind 127.", "--bind localhost"):
        assert forbidden not in text
    assert _unit_values("EnvironmentFile") == ["%h/.config/factory-dashboard/http.env"]


def test_dashboard_server_unit_runs_the_repo_handler_not_http_server() -> None:
    (exec_start,) = _unit_values("ExecStart")
    (guard,) = _unit_values("ExecStartPre")
    assert exec_start == f"/usr/bin/python3 -I {SERVER_ON_NODE}"
    assert guard == f"/usr/bin/python3 -I {SERVER_ON_NODE} --check"
    text = DASHBOARD_UNIT.read_text(encoding="utf-8")
    assert "http.server" not in "".join(_unit_values("ExecStart") + _unit_values("ExecStartPre"))
    # No ${VAR} expansion on the command line: an unset DASHBOARD_DIR can
    # never turn into an empty --directory argument.
    assert "${" not in exec_start and "$" not in guard
    assert "--directory" not in text
    assert DASHBOARD_SERVER.is_file()


@pytest.mark.parametrize(
    "key,value",
    [
        ("NoNewPrivileges", "yes"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "read-only"),
        ("PrivateTmp", "yes"),
        ("PrivateDevices", "yes"),
        ("PrivateUsers", "yes"),
        ("ProtectKernelTunables", "yes"),
        ("ProtectKernelModules", "yes"),
        ("ProtectControlGroups", "yes"),
        ("RestrictAddressFamilies", "AF_INET AF_UNIX"),
        ("RestrictNamespaces", "yes"),
        ("RestrictRealtime", "yes"),
        ("RestrictSUIDSGID", "yes"),
        ("LockPersonality", "yes"),
        ("SystemCallArchitectures", "native"),
        ("SystemCallFilter", "@system-service"),
        ("UMask", "0077"),
    ],
)
def test_dashboard_server_unit_is_sandboxed(key: str, value: str) -> None:
    assert _unit_values(key) == [value]


def test_dashboard_server_unit_hides_credential_directories() -> None:
    (paths,) = _unit_values("InaccessiblePaths")
    for name in (".ssh", ".gnupg", ".config/gh", ".aws", ".netrc"):
        assert f"-%h/{name}" in paths.split()


def _check(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(DASHBOARD_SERVER), "--check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "address,ok",
    [
        ("100.101.102.103", True),
        ("100.64.0.1", True),
        ("0.0.0.0", False),
        ("127.0.0.1", False),
        ("192.168.1.20", False),
        ("8.8.8.8", False),
        ("100.128.0.1", False),
        ("100.101.102.103 ", False),
        ("::", False),
        ("", False),
    ],
)
def test_dashboard_server_guard_accepts_only_tailnet_ipv4(
    tmp_path: Path, address: str, ok: bool
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    env = {"TAILNET_IP": address, "DASHBOARD_DIR": str(site), "HOME": str(tmp_path / "home")}
    result = _check(env)
    assert (result.returncode == 0) is ok, result.stderr
    if not ok:
        assert "TAILNET_IP" in result.stderr


def test_dashboard_server_guard_refuses_unset_tailnet_ip(tmp_path: Path) -> None:
    result = _check({"DASHBOARD_DIR": str(tmp_path), "HOME": str(tmp_path / "home")})
    assert result.returncode != 0
    assert "TAILNET_IP" in result.stderr


@pytest.mark.parametrize(
    "case",
    ["unset", "empty", "relative", "root", "home", "home-slash", "home-link", "home-parent",
     "missing", "file"],
)
def test_dashboard_server_guard_refuses_unsafe_directories(tmp_path: Path, case: str) -> None:
    home = tmp_path / "home" / "max"
    home.mkdir(parents=True)
    (tmp_path / "plain-file").write_text("x", encoding="utf-8")
    (tmp_path / "link-to-home").symlink_to(home)
    values = {
        "empty": "",
        "relative": "site",
        "root": "/",
        "home": str(home),
        "home-slash": str(home) + "/",
        "home-link": str(tmp_path / "link-to-home"),
        "home-parent": str(tmp_path / "home"),
        "missing": str(tmp_path / "nope"),
        "file": str(tmp_path / "plain-file"),
    }
    env = {"TAILNET_IP": "100.101.102.103", "HOME": str(home)}
    if case != "unset":
        env["DASHBOARD_DIR"] = values[case]
    result = _check(env)
    assert result.returncode != 0, case
    assert "DASHBOARD_DIR" in result.stderr


def test_dashboard_server_guard_accepts_a_site_directory_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    site = home / "factory-dashboard"
    site.mkdir(parents=True)
    result = _check(
        {"TAILNET_IP": "100.101.102.103", "DASHBOARD_DIR": str(site), "HOME": str(home)}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ok: would serve {site} on 100.101.102.103:8787"


@pytest.mark.parametrize("port,ok", [("8787", True), ("1024", True), ("80", False),
                                     ("65536", False), ("x", False), ("", False)])
def test_dashboard_server_guard_checks_the_port(tmp_path: Path, port: str, ok: bool) -> None:
    site = tmp_path / "site"
    site.mkdir()
    env = {"TAILNET_IP": "100.101.102.103", "DASHBOARD_DIR": str(site),
           "HOME": str(tmp_path / "home"), "DASHBOARD_PORT": port}
    result = _check(env)
    assert (result.returncode == 0) is ok, result.stderr


def test_dashboard_server_unit_is_documented_not_installed() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "deploy/factory-dashboard-http.service" in readme
    assert "deploy/factory-dashboard-http.py" in readme
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "factory-dashboard" not in install


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


FAKE_CRONTAB = """#!/usr/bin/python3
import os, sys
from pathlib import Path

table = Path(os.environ["HOME"]) / "crontab.txt"
if sys.argv[1:] == ["-l"]:
    if not table.exists():
        print("no crontab for test", file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.write(table.read_text())
elif sys.argv[1:] == ["-"]:
    table.write_text(sys.stdin.read())
else:
    raise SystemExit(f"unexpected crontab arguments {sys.argv[1:]}")
"""


def _install(environment: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "deploy/install.sh", *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_install_does_not_enable_autostart_by_default(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    crontab = tmp_path / "fake-bin" / "crontab"
    crontab.write_text(FAKE_CRONTAB, encoding="utf-8")
    crontab.chmod(0o755)
    environment["ORCH_CRONTAB"] = str(crontab)

    result = _install(environment)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "crontab.txt").exists()
    assert not (tmp_path / ".config/factory/poller-autostart").exists()


def test_install_autostart_installs_cron_and_the_profile_hook(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    crontab = tmp_path / "fake-bin" / "crontab"
    crontab.write_text(FAKE_CRONTAB, encoding="utf-8")
    crontab.chmod(0o755)
    environment["ORCH_CRONTAB"] = str(crontab)

    assert _install(environment, "--autostart").returncode == 0
    result = _install(environment, "--autostart")

    assert result.returncode == 0, result.stderr
    lines = (tmp_path / "crontab.txt").read_text(encoding="utf-8").splitlines()
    service = ROOT / "deploy" / "poller-service.sh"
    assert [line for line in lines if "# factory-orch-poll" in line] == [
        f"@reboot {service} ensure >>{tmp_path}/.local/state/factory/"
        "poller-service.log 2>&1 # factory-orch-poll",
        f"*/5 * * * * {service} ensure >>{tmp_path}/.local/state/factory/"
        "poller-service.log 2>&1 # factory-orch-poll",
    ]
    assert (tmp_path / ".config/factory/poller-autostart").is_file()


def test_install_refuses_an_unknown_option(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    result = _install(environment, "--bogus")
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert not (tmp_path / ".local").exists()


def _profile_env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "ORCH_POLL_START_WAIT": "10",
        "ORCH_CRONTAB": str(tmp_path / "no-such-crontab"),
    }


def test_profile_hook_runs_ensure_only_when_autostart_is_on(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    # Swap the installed wrapper for a fake long-running poller.
    wrapper = tmp_path / ".local/bin/orch"
    wrapper.write_text(
        "#!/usr/bin/python3\n"
        "import os, sys, time\n"
        "assert sys.argv[1:3] == ['poll', '--pidfile'], sys.argv\n"
        "open(sys.argv[3], 'w').write(f'{os.getpid()}\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    config = tmp_path / ".config/factory"
    (config / "webhook.env").write_text("", encoding="utf-8")
    (config / "orch-poll.env").write_text("", encoding="utf-8")
    pidfile = tmp_path / ".local/state/factory/poller.pid"
    source = ["bash", "-c", "set -e; . deploy/profile.sh; printf ok"]

    off = subprocess.run(
        source, cwd=ROOT, env=_profile_env(tmp_path), text=True,
        capture_output=True, timeout=30, check=False,
    )
    assert off.returncode == 0 and off.stdout == "ok", off.stderr
    time.sleep(0.5)
    assert not pidfile.exists()

    (config / "poller-autostart").touch()
    started = time.monotonic()
    on = subprocess.run(
        source, cwd=ROOT, env=_profile_env(tmp_path), text=True,
        capture_output=True, timeout=30, check=False,
    )
    assert on.returncode == 0 and on.stdout == "ok", on.stderr
    assert time.monotonic() - started < 5  # the hook does not block the shell
    try:
        deadline = time.monotonic() + 15
        while not pidfile.exists():
            assert time.monotonic() < deadline, (
                tmp_path / ".local/state/factory/poller-service.log"
            ).read_text() if (tmp_path / ".local/state/factory/poller-service.log").exists() else "no log"
            time.sleep(0.1)
    finally:
        time.sleep(0.2)
        if pidfile.exists():
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
