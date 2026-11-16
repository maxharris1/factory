import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import reap_home, sandbox_env


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
    return sandbox_env(
        tmp_path,
        EXPECTED_REQUIREMENTS=str(ROOT / "requirements.txt"),
        FAKE_VENV_PYTHON=FAKE_VENV_PYTHON,
        ORCH_CONFIG=str(orch_config),
        PATH=f"{fake_bin}:/usr/bin:/bin",
        ORCH_CRONTAB=str(fake_bin / "no-such-crontab"),
        ORCH_SYSTEMCTL=str(fake_bin / "no-such-systemctl"),
    )


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
        env=sandbox_env(tmp_path),
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
        env=sandbox_env(tmp_path, PATH="/usr/bin:/bin"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "poll --once|1|set|/tmp/orch.db\n"


def test_orch_poll_service_starts_only_through_run_poller() -> None:
    """#107 f: one launcher; run-poller.sh sources the env and sets PYTHONUNBUFFERED."""
    unit = (ROOT / "deploy/orch-poll.service").read_text(encoding="utf-8")
    assert "ExecStart=%h/factory/deploy/run-poller.sh\n" in unit
    assert "EnvironmentFile" not in unit
    assert "TimeoutStopSec=300" in unit
    service = (ROOT / "deploy/poller-service.sh").read_text(encoding="utf-8")
    assert 'RUNNER="${HERE}/run-poller.sh"' in service


@pytest.mark.parametrize("missing", ["webhook.env", "orch-poll.env"])
def test_run_poller_refuses_to_start_without_its_env(tmp_path: Path, missing: str) -> None:
    config = tmp_path / ".config/factory"
    config.mkdir(parents=True)
    for name in ("webhook.env", "orch-poll.env"):
        if name != missing:
            (config / name).write_text('FACTORY_WEBHOOK_URL="https://webhook.test/x"\n',
                                       encoding="utf-8")
    orch = tmp_path / ".local/bin/orch"
    orch.parent.mkdir(parents=True)
    orch.write_text("#!/bin/sh\ntouch \"$HOME/started\"\n", encoding="utf-8")
    orch.chmod(0o755)

    result = subprocess.run(["bash", "deploy/run-poller.sh"], cwd=ROOT,
                            env=sandbox_env(tmp_path, PATH="/usr/bin:/bin"),
                            text=True, capture_output=True, check=False)

    assert result.returncode == 78
    assert result.stderr == f"run-poller: missing ~/.config/factory/{missing}\n"
    assert "webhook.test" not in result.stderr + result.stdout
    assert not (tmp_path / "started").exists()


def test_ci_runs_once_per_push() -> None:
    """#107 j: push on main only, plus pull_request."""
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    triggers = workflow[True]
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request" in triggers


def test_ci_cancels_superseded_pr_runs_and_runs_every_shard_and_serial_test() -> None:
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    concurrency = workflow["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "github.event.pull_request.number || github.run_id" in concurrency["group"]

    test = workflow["jobs"]["test"]
    shards = test["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, len(shards) + 1))
    pytest_runs = [step["run"] for step in test["steps"] if "pytest" in step.get("run", "")]
    assert len(pytest_runs) == 1
    assert '-m "not serial"' in pytest_runs[0]
    assert "--shard ${{ matrix.shard }}/${{ strategy.job-total }}" in pytest_runs[0]

    runs = [step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"]]
    assert "pytest -m serial -p no:xdist" in runs


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
    lines = (tmp_path / "crontab.txt").read_text(encoding="utf-8").splitlines()
    assert lines and all(line.endswith("# factory-self-heal") for line in lines)
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
    return sandbox_env(
        tmp_path,
        PATH="/usr/bin:/bin",
        ORCH_POLL_START_WAIT="10",
        ORCH_CRONTAB=str(tmp_path / "no-such-crontab"),
    )


def test_a_sandboxed_bot_shell_may_autostart_while_the_suite_holds_a_job_proof(
    tmp_path: Path,
) -> None:
    assert os.environ.get("ORCH_JOB_PROOF")
    result = subprocess.run(
        ["sh", "-c", ". deploy/host-pidns.sh; factory_may_autostart"],
        cwd=ROOT, env=sandbox_env(tmp_path), text=True,
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr


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
    (config / "self-heal-autostart").unlink()
    pidfile = tmp_path / ".local/state/factory/poller.pid"
    source = ["bash", "-c", "set -e; . deploy/profile.sh; printf ok"]

    try:
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
        assert time.monotonic() - started < 5
        deadline = time.monotonic() + 15
        while not pidfile.exists():
            assert time.monotonic() < deadline, (
                tmp_path / ".local/state/factory/poller-service.log"
            ).read_text() if (tmp_path / ".local/state/factory/poller-service.log").exists() else "no log"
            time.sleep(0.1)
    finally:
        try:
            subprocess.run(["bash", "deploy/poller-service.sh", "stop"], cwd=ROOT,
                           env=_profile_env(tmp_path), capture_output=True, timeout=60, check=False)
        finally:
            reap_home(tmp_path)


# --- #107: profile, self-heal and legacy launchers ---------------------------------------------


def _fake(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / "fake-bin" / name
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


FAKE_SYSTEMCTL = """#!/usr/bin/python3
import os, sys
from pathlib import Path

with open(Path(os.environ["HOME"]) / "systemctl.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
"""


def test_install_puts_self_heal_on_a_systemd_timer_when_there_is_one(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    environment["ORCH_SYSTEMCTL"] = str(_fake(tmp_path, "systemctl", FAKE_SYSTEMCTL))

    result = _install(environment)

    assert result.returncode == 0, result.stderr
    units = tmp_path / ".config/systemd/user"
    service = (units / "factory-self-heal.service").read_text(encoding="utf-8")
    assert f"ExecStart={ROOT}/deploy/self-heal.sh once" in service
    assert "KillMode=process" in service
    assert "OnUnitActiveSec=2min" in (units / "factory-self-heal.timer").read_text(encoding="utf-8")
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert "--user enable --now factory-self-heal.timer" in calls
    assert not (tmp_path / ".config/factory/self-heal-autostart").exists()


def test_install_falls_back_to_the_profile_loop_and_cron_without_systemd(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    environment["ORCH_CRONTAB"] = str(_fake(tmp_path, "crontab", FAKE_CRONTAB))

    assert _install(environment).returncode == 0
    assert _install(environment).returncode == 0

    lines = (tmp_path / "crontab.txt").read_text(encoding="utf-8").splitlines()
    job = (f"{ROOT}/deploy/self-heal.sh ensure >>{tmp_path}/.local/state/factory/"
           "self-heal-service.log 2>&1 # factory-self-heal")
    assert lines == [f"@reboot {job}", f"*/5 * * * * {job}"]
    assert (tmp_path / ".config/factory/self-heal-autostart").is_file()
    profile = (tmp_path / ".config/factory/profile.sh").read_text(encoding="utf-8")
    assert f'"{ROOT}/deploy/self-heal.sh" ensure' in profile
    assert f'"{tmp_path}/.local/share/factory/linear-webhook-service.sh" ensure' in profile


def test_the_profile_is_guarded_and_makes_its_state_dir(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    (tmp_path / ".config/factory/self-heal-autostart").unlink()
    state = tmp_path / ".local/state/factory"
    for child in state.iterdir():
        if child.is_file():
            child.unlink()
    state.rmdir()

    result = subprocess.run(
        ["bash", "-c", 'set -e; . deploy/profile.sh; PATH=/x; . deploy/profile.sh; '
                       'printf "%s" "$PATH"'],
        cwd=ROOT, env=_profile_env(tmp_path), text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "/x"
    assert state.is_dir()


def test_install_autostart_adds_the_listener_only_with_a_linear_webhook(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    environment["ORCH_CRONTAB"] = str(_fake(tmp_path, "crontab", FAKE_CRONTAB))
    assert _install(environment, "--autostart").returncode == 0
    assert not (tmp_path / ".config/factory/linear-webhook-autostart").exists()

    config = Path(environment["ORCH_CONFIG"])
    config.with_name("orch.local.yml").write_text(
        "linear_webhook:\n  public_url: https://box.example.ts.net/linear-webhook\n",
        encoding="utf-8")
    assert _install(environment, "--autostart").returncode == 0

    assert (tmp_path / ".config/factory/linear-webhook-autostart").is_file()
    table = (tmp_path / "crontab.txt").read_text(encoding="utf-8")
    assert table.count("# factory-linear-webhook") == 2


def _child(pid_file: Path) -> int:
    while not pid_file.is_file() or not pid_file.read_text(encoding="utf-8").endswith("\n"):
        time.sleep(0.01)
    return int(pid_file.read_text(encoding="utf-8"))


def _ends(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if stat.read_text(encoding="utf-8").rsplit(") ", 1)[1].startswith("Z"):
                return True
        except (FileNotFoundError, ProcessLookupError):
            return True
        time.sleep(0.01)
    return False


def test_install_retires_the_legacy_launchers(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    environment["ORCH_CRONTAB"] = str(_fake(tmp_path, "crontab", FAKE_CRONTAB))
    legacy = tmp_path / ".local/share/factory"
    legacy.mkdir(parents=True)
    poller = legacy / "run-poller.sh"
    poller.write_text('#!/bin/sh\nsleep 60 &\necho $! >"$HOME/child-$$"\nwait\n'
                      'touch "$HOME/poller-continued"\n', encoding="utf-8")
    poller.chmod(0o755)
    listener = legacy / "linear-webhook-service.sh"
    listener.write_text('#!/bin/sh\n[ "$1" = stop ] && touch "$HOME/legacy-stopped"\n',
                        encoding="utf-8")
    listener.chmod(0o755)
    (tmp_path / "crontab.txt").write_text(
        f"*/5 * * * * {poller} >>/tmp/x 2>&1\n"
        f"@reboot {listener} ensure\n"
        "0 3 * * * /usr/bin/true # keep\n",
        encoding="utf-8")
    poller_env = sandbox_env(tmp_path)
    pollers: list[subprocess.Popen] = []
    bystander = subprocess.Popen(["/bin/sh", "-c", f"sleep 60 # {poller}"],
                                 start_new_session=True)
    try:
        pollers.extend(subprocess.Popen(["/bin/sh", str(poller)], env=poller_env)
                       for _ in range(20))
        children = [_child(tmp_path / f"child-{proc.pid}") for proc in pollers]
        result = _install(environment)

        assert result.returncode == 0, result.stderr
        assert [proc.wait(timeout=10) for proc in pollers] == [-signal.SIGTERM] * 20
        assert not (tmp_path / "poller-continued").exists()
        assert all(_ends(child) for child in children)
        assert bystander.poll() is None
        assert not poller.exists()
        # Merged with #134: the listener path now holds the service script's
        # installed copy, which a later install leaves in place.
        assert listener.read_bytes() == (ROOT / "deploy/linear-webhook-service.sh").read_bytes()
        assert (tmp_path / "legacy-stopped").is_file()
        table = (tmp_path / "crontab.txt").read_text(encoding="utf-8")
        assert "0 3 * * * /usr/bin/true # keep" in table
        assert str(legacy) not in table
        assert f"stopped legacy {poller}" in result.stdout
        again = _install(environment)
        assert again.returncode == 0, again.stderr
        assert f"removed legacy launcher {listener}" not in again.stdout
        assert listener.exists()
    finally:
        os.killpg(bystander.pid, signal.SIGKILL)
        reap_home(tmp_path)
        for proc in (*pollers, bystander):
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def test_install_leaves_the_legacy_launchers_when_the_crontab_is_unreadable(
    tmp_path: Path,
) -> None:
    environment = _install_environment(tmp_path, "venv")
    environment["ORCH_CRONTAB"] = str(_fake(
        tmp_path, "crontab", "#!/bin/sh\necho 'permission denied' >&2\nexit 1\n"))
    legacy = tmp_path / ".local/share/factory/run-poller.sh"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\n", encoding="utf-8")

    result = _install(environment)

    assert result.returncode == 1
    assert "cannot read the crontab" in result.stderr
    assert legacy.exists()


def test_profile_makes_the_state_dir_before_logging(tmp_path: Path) -> None:
    """#66 fix 3: the profile redirects into the state dir, which a reboot lost."""
    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    profile = (tmp_path / ".config/factory/profile.sh").read_text(encoding="utf-8")
    state = f"{tmp_path}/.local/state/factory"

    assert profile.index(f'mkdir -p "{state}"') < profile.index(f'>>"{state}/poller-service.log"')


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_install_copies_the_linear_service_and_the_profile_runs_its_ensure(
    tmp_path: Path,
) -> None:
    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    installed = tmp_path / ".local/share/factory/linear-webhook-service.sh"
    assert installed.read_bytes() == (ROOT / "deploy/linear-webhook-service.sh").read_bytes()
    assert installed.stat().st_mode & 0o777 == 0o755
    wrapper = tmp_path / ".local/bin/orch"
    wrapper.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **_profile_env(tmp_path),
        "LINEAR_WEBHOOK_PORT": str(port),
        "LINEAR_WEBHOOK_TUNNEL_HOST": "",
    }
    source = ["bash", "-c", "set -e; . deploy/profile.sh; printf ok"]
    lock = tmp_path / ".local/state/factory/linear-webhook-listener.lock"
    (tmp_path / ".config/factory/self-heal-autostart").unlink()

    try:
        off = subprocess.run(source, cwd=ROOT, env=env, text=True,
                             capture_output=True, timeout=30, check=False)
        assert off.returncode == 0 and off.stdout == "ok", off.stderr
        time.sleep(0.5)
        assert not lock.exists()

        (tmp_path / ".config/factory/linear-webhook-autostart").touch()
        on = subprocess.run(source, cwd=ROOT, env=env, text=True,
                            capture_output=True, timeout=30, check=False)
        assert on.returncode == 0 and on.stdout == "ok", on.stderr
        deadline = time.monotonic() + 15
        while not (tmp_path / ".local/state/factory/linear-webhook-listener.pid").exists():
            assert time.monotonic() < deadline, "the profile hook never started the listener loop"
            time.sleep(0.1)
        status = subprocess.run([str(installed), "status"], env=env, text=True,
                                capture_output=True, timeout=30, check=False)
        assert status.returncode == 0, status.stdout + status.stderr
    finally:
        try:
            stopped = subprocess.run([str(installed), "stop"], env=env, text=True,
                                     capture_output=True, timeout=60, check=False)
        finally:
            reap_home(tmp_path)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr


@pytest.mark.parametrize("installer", ["uv", "venv"])
def test_install_update_keeps_the_venv_running_processes_use(
    tmp_path: Path, installer: str
) -> None:
    """#219: self-deploy's `install.sh --update` installs requirements into
    the existing venv instead of recreating it; with no venv it makes one."""
    environment = _install_environment(tmp_path, installer)
    first = _install(environment, "--update")
    assert first.returncode == 0, first.stderr
    witness = tmp_path / ".local/share/factory/venv/witness"
    witness.write_text("in use", encoding="utf-8")
    (tmp_path / "requirements-installed").unlink()

    second = _install(environment, "--update")

    assert second.returncode == 0, second.stderr
    assert witness.read_text(encoding="utf-8") == "in use"
    assert (tmp_path / "requirements-installed").is_file()
    assert (tmp_path / ".local/bin/orch").is_file()
    assert (tmp_path / ".config/factory/profile.sh").is_file()

    plain = _install(environment)
    assert plain.returncode == 0, plain.stderr
    assert not witness.exists()


def _fake_proc(tmp_path: Path, nspid: str, ns: str) -> Path:
    proc = tmp_path / "fake-proc"
    (proc / "self/ns").mkdir(parents=True)
    (proc / "self/status").write_text(f"Name:\tsh\nNSpid:\t{nspid}\n", encoding="utf-8")
    (proc / "self/ns/pid").symlink_to(ns)
    return proc


def test_install_records_the_host_pid_namespace(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")

    result = _install(environment)

    assert result.returncode == 0, result.stderr
    record = tmp_path / ".config/factory/host-pidns"
    assert record.read_text(encoding="utf-8").strip() == os.readlink("/proc/self/ns/pid")
    helper = tmp_path / ".local/share/factory/host-pidns.sh"
    assert helper.read_bytes() == (ROOT / "deploy/host-pidns.sh").read_bytes()


@pytest.mark.parametrize(
    ("nspid", "ns", "extra"),
    [
        ("1728748\t61", "pid:[1]", {}),
        ("61", "pid:[4026532782]", {}),
        ("4484", None, {"ORCH_JOB_PROOF": "x"}),
        ("4484", None, {"CODEX_SANDBOX": "seatbelt"}),
    ],
    ids=["two-field-nspid", "own-proc-mount", "job", "codex-sandbox"],
)
def test_install_update_keeps_the_host_record_outside_the_host(
    tmp_path: Path, nspid: str, ns: str | None, extra: dict[str, str],
) -> None:
    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    record = tmp_path / ".config/factory/host-pidns"
    host = os.readlink("/proc/self/ns/pid")
    environment |= extra
    environment["ORCH_PROC_DIR"] = str(_fake_proc(tmp_path, nspid, ns or host))

    again = _install(environment, "--update")

    assert again.returncode == 0, again.stderr
    assert "not recording the host PID namespace" in again.stderr
    assert record.read_text(encoding="utf-8").strip() == host


@pytest.mark.parametrize(
    ("nspid", "ns", "extra"),
    [
        ("1728748\t61", None, {}),
        ("61", "pid:[4026532782]", {}),
        (None, None, {"ORCH_JOB_PROOF": "x"}),
        (None, None, {"CODEX_SANDBOX": "seatbelt"}),
    ],
    ids=["two-field-nspid", "recorded-namespace-differs", "job", "codex-sandbox"],
)
def test_profile_hook_starts_nothing_outside_the_host_pid_namespace(
    tmp_path: Path, nspid: str | None, ns: str | None, extra: dict[str, str],
) -> None:
    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    ran = tmp_path / "ran"
    (tmp_path / ".local/bin/orch").write_text(
        f"#!/bin/sh\ntouch {ran}\nsleep 30\n", encoding="utf-8",
    )
    config = tmp_path / ".config/factory"
    for flag in ["poller-autostart", "waiter-autostart-claude-bot", "self-heal-autostart",
                 "linear-webhook-autostart"]:
        (config / flag).touch()
    state = tmp_path / ".local/state/factory"
    pidfiles = {state / "poller.pid": "1599489\n", state / "waiter-claude-bot.pid": "1667506\n"}
    for path, text in pidfiles.items():
        path.write_text(text, encoding="utf-8")
    stamps = {path: path.stat().st_mtime_ns for path in pidfiles}
    logs_before = sorted(state.glob("*.log"))
    env = _profile_env(tmp_path) | extra
    if nspid is not None:
        host = os.readlink("/proc/self/ns/pid")
        env["ORCH_PROC_DIR"] = str(_fake_proc(tmp_path, nspid, ns or host))

    try:
        sourced = subprocess.run(
            ["bash", "-c", "set -e; . deploy/profile.sh; printf ok"], cwd=ROOT, env=env,
            text=True, capture_output=True, timeout=30, check=False,
        )

        assert sourced.returncode == 0 and sourced.stdout == "ok", sourced.stderr
        assert sourced.stderr == ""
        time.sleep(2)
        assert not ran.exists()
        for path, text in pidfiles.items():
            assert path.read_text(encoding="utf-8") == text
            assert path.stat().st_mtime_ns == stamps[path]
        assert sorted(state.glob("*.log")) == logs_before
    finally:
        reap_home(tmp_path)


def test_profile_hook_still_runs_in_the_recorded_host_namespace(tmp_path: Path) -> None:
    environment = _install_environment(tmp_path, "venv")
    assert _install(environment).returncode == 0
    (tmp_path / ".config/factory/self-heal-autostart").unlink()
    (tmp_path / ".config/factory/poller-autostart").touch()
    env = _profile_env(tmp_path)
    env.pop("ORCH_JOB_PROOF", None)
    env.pop("CODEX_SANDBOX", None)
    env["ORCH_PROC_DIR"] = str(_fake_proc(tmp_path, "4484", os.readlink("/proc/self/ns/pid")))

    try:
        sourced = subprocess.run(
            ["bash", "-c", "set -e; . deploy/profile.sh; printf ok"], cwd=ROOT, env=env,
            text=True, capture_output=True, timeout=30, check=False,
        )

        assert sourced.returncode == 0 and sourced.stdout == "ok", sourced.stderr
        log = tmp_path / ".local/state/factory/poller-service.log"
        deadline = time.monotonic() + 10
        while not log.exists():
            assert time.monotonic() < deadline, "the hook did not run poller-service.sh ensure"
            time.sleep(0.1)
    finally:
        reap_home(tmp_path)
