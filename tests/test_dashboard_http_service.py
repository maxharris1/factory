import os
import socket
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "dashboard-http-service.sh"
HTTP_UNIT = "factory-dashboard-http.service"
STATIC_UNIT = "factory-dashboard.service"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
dir="$(dirname "$0")"
printf '%s\\n' "$*" >>"$dir/calls.log"
verb="$1" unit="$2"
case "$verb" in
    is-active)
        state="$(cat "$dir/active.$unit" 2>/dev/null || echo inactive)"
        echo "$state"
        [[ "$state" == active ]]
        ;;
    is-enabled)
        state="$(cat "$dir/enabled.$unit" 2>/dev/null || echo disabled)"
        echo "$state"
        [[ "$state" == enabled ]]
        ;;
    restart)
        echo active >"$dir/active.$unit"
        ;;
    *) exit 1 ;;
esac
"""


class Units:
    def __init__(self, root: Path) -> None:
        self.dir = root / "systemctl"
        self.dir.mkdir()
        self.path = self.dir / "systemctl"
        self.path.write_text(FAKE_SYSTEMCTL, encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def set(self, unit: str, *, active: str | None = None, enabled: str | None = None) -> None:
        if active is not None:
            (self.dir / f"active.{unit}").write_text(active + "\n", encoding="utf-8")
        if enabled is not None:
            (self.dir / f"enabled.{unit}").write_text(enabled + "\n", encoding="utf-8")

    def calls(self) -> list[str]:
        log = self.dir / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _listener(server_version: str) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        sys_version = ""

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass

    Handler.server_version = server_version
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def app():
    server = _listener("factory-dashboard")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def static_server():
    server = _listener("SimpleHTTP/0.6")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def units(tmp_path: Path) -> Units:
    return Units(tmp_path)


def _addr(server: HTTPServer) -> str:
    return f"127.0.0.1:{server.server_address[1]}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _live(units: Units) -> None:
    units.set(HTTP_UNIT, active="active", enabled="enabled")
    units.set(STATIC_UNIT, active="inactive")


def _run(units: Units, *args: str, **env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("DASHBOARD_")}
    base.update({"DASHBOARD_SYSTEMCTL": str(units.path), **env})
    return subprocess.run(["bash", str(SERVICE), *args], env=base, capture_output=True,
                          text=True, timeout=60)


def test_script_parses_and_is_executable() -> None:
    assert subprocess.run(["bash", "-n", str(SERVICE)]).returncode == 0
    assert os.access(SERVICE, os.X_OK)


def test_status_is_0_when_the_app_is_the_live_server(units: Units, app: HTTPServer) -> None:
    _live(units)
    result = _run(units, "status", DASHBOARD_PROBE_ADDR=_addr(app))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("live: ")
    assert _addr(app) in result.stdout


def test_status_is_3_when_the_http_unit_is_not_active(units: Units, app: HTTPServer) -> None:
    _live(units)
    units.set(HTTP_UNIT, active="inactive")
    result = _run(units, "status", DASHBOARD_PROBE_ADDR=_addr(app))
    assert result.returncode == 3
    assert result.stdout.startswith("not live: ")
    assert HTTP_UNIT in result.stdout


def test_status_is_3_when_the_static_unit_is_active(units: Units, app: HTTPServer) -> None:
    _live(units)
    units.set(STATIC_UNIT, active="active")
    result = _run(units, "status", DASHBOARD_PROBE_ADDR=_addr(app))
    assert result.returncode == 3
    assert result.stdout.startswith("not live: ")
    assert STATIC_UNIT in result.stdout


def test_status_is_3_when_the_static_server_answers(units: Units, static_server: HTTPServer) -> None:
    _live(units)
    result = _run(units, "status", DASHBOARD_PROBE_ADDR=_addr(static_server))
    assert result.returncode == 3
    assert result.stdout.startswith("not live: ")
    assert "SimpleHTTP/0.6" in result.stdout


def test_status_is_3_when_nothing_listens(units: Units) -> None:
    _live(units)
    addr = f"127.0.0.1:{_free_port()}"
    result = _run(units, "status", DASHBOARD_PROBE_ADDR=addr)
    assert result.returncode == 3
    assert result.stdout.startswith("not live: ")
    assert addr in result.stdout


def test_status_reads_only_the_address_from_the_env_file(
        tmp_path: Path, units: Units, app: HTTPServer) -> None:
    _live(units)
    env_file = tmp_path / "http.env"
    env_file.write_text(
        "TAILNET_IP=127.0.0.1\n"
        "DASHBOARD_DIR=/nowhere\n"
        f"DASHBOARD_PORT={app.server_address[1]}\n"
        "DASHBOARD_GITHUB_CLIENT_ID=SENTINEL-not-to-print\n",
        encoding="utf-8")
    result = _run(units, "status", DASHBOARD_ENV_FILE=str(env_file))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _addr(app) in result.stdout
    assert "SENTINEL" not in result.stdout + result.stderr


@pytest.mark.parametrize("contents", [None, "TAILNET_IP=127.0.0.1\n"], ids=["missing", "no-port"])
def test_status_is_3_when_the_address_is_unknown(
        tmp_path: Path, units: Units, contents: str | None) -> None:
    _live(units)
    env_file = tmp_path / "http.env"
    if contents is not None:
        env_file.write_text(contents, encoding="utf-8")
    result = _run(units, "status", DASHBOARD_ENV_FILE=str(env_file))
    assert result.returncode == 3
    assert result.stdout.startswith("not live: ")
    assert str(env_file) in result.stdout
    assert not any(call.startswith("restart") for call in units.calls())


def test_restart_restarts_the_enabled_unit_and_waits_for_live(units: Units, app: HTTPServer) -> None:
    units.set(HTTP_UNIT, active="inactive", enabled="enabled")
    units.set(STATIC_UNIT, active="inactive")
    result = _run(units, "restart", DASHBOARD_PROBE_ADDR=_addr(app), DASHBOARD_START_WAIT="5")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"restart {HTTP_UNIT}" in units.calls()
    assert "live: " in result.stdout


def test_restart_is_3_when_the_app_never_goes_live(units: Units, static_server: HTTPServer) -> None:
    units.set(HTTP_UNIT, active="inactive", enabled="enabled")
    units.set(STATIC_UNIT, active="inactive")
    result = _run(units, "restart", DASHBOARD_PROBE_ADDR=_addr(static_server),
                  DASHBOARD_START_WAIT="1")
    assert result.returncode == 3
    assert f"restart {HTTP_UNIT}" in units.calls()
    assert "not live: " in result.stdout


def test_restart_refuses_a_disabled_unit_without_a_cutover(units: Units, app: HTTPServer) -> None:
    units.set(HTTP_UNIT, active="inactive", enabled="disabled")
    units.set(STATIC_UNIT, active="active")
    result = _run(units, "restart", DASHBOARD_PROBE_ADDR=_addr(app))
    assert result.returncode == 3
    assert "install" in result.stdout + result.stderr
    assert not any(call.startswith("restart") for call in units.calls())


@pytest.mark.parametrize("args", [(), ("start",), ("bogus",)])
def test_an_unknown_command_prints_usage_and_exits_2(units: Units, args: tuple[str, ...]) -> None:
    result = _run(units, *args)
    assert result.returncode == 2
    assert "usage: dashboard-http-service.sh {status|restart}" in result.stderr
