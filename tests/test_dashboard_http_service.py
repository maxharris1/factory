import http.client
import os
import socket
import stat
import subprocess
import sys
import threading
import time
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
verb="$1" now=""
shift
if [[ "${1:-}" == --now ]]; then
    now=1
    shift
fi
unit="${1:-}"
if [[ -f "$dir/fail.$verb${unit:+.$unit}" ]]; then
    echo "Interactive authentication required." >&2
    exit 1
fi
start() {
    echo active >"$dir/active.$unit"
    for other in "$dir"/active.*; do
        [[ "$other" == "$dir/active.$unit" ]] || echo inactive >"$other"
    done
}
case "$verb" in
    daemon-reload) ;;
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
    enable)
        echo enabled >"$dir/enabled.$unit"
        [[ -z "$now" ]] || start
        ;;
    disable)
        echo disabled >"$dir/enabled.$unit"
        [[ -z "$now" ]] || echo inactive >"$dir/active.$unit"
        ;;
    stop) echo inactive >"$dir/active.$unit" ;;
    start) start ;;
    restart) start ;;
    *) exit 1 ;;
esac
"""

FAKE_GETENT = """#!/usr/bin/env bash
dir="$(dirname "$0")"
if [[ "$1 $2" == "passwd factory-dash" && -f "$dir/factory-dash.exists" ]]; then
    uid="$(cat "$dir/factory-dash.exists")"
    echo "factory-dash:x:${uid:-999}:999::/nonexistent:/usr/sbin/nologin"
    exit 0
fi
exit 2
"""

FAKE_USERADD = """#!/usr/bin/env bash
dir="$(dirname "$0")"
printf '%s\\n' "$*" >>"$dir/useradd.log"
touch "$dir/factory-dash.exists"
"""

FAKE_JOURNALCTL = """#!/usr/bin/env bash
echo "journal: $*"
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

    def fail(self, *call: str) -> None:
        (self.dir / ".".join(("fail", *call))).touch()

    def calls(self) -> list[str]:
        log = self.dir / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def state(self, unit: str) -> tuple[str, str]:
        def read(kind: str, default: str) -> str:
            path = self.dir / f"{kind}.{unit}"
            return path.read_text(encoding="utf-8").strip() if path.exists() else default
        return read("active", "inactive"), read("enabled", "disabled")


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


def _run(units: Units, *args: str, script: Path = SERVICE, **env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("DASHBOARD_")}
    base.update({"DASHBOARD_SYSTEMCTL": str(units.path), **env})
    return subprocess.run(["bash", str(script), *args], env=base, capture_output=True,
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


@pytest.mark.parametrize("template", [
    'TAILNET_IP="127.0.0.1"\nDASHBOARD_PORT="{port}"\n',
    "TAILNET_IP='127.0.0.1'\nDASHBOARD_PORT='{port}'\n",
    "  TAILNET_IP=127.0.0.1\n\tDASHBOARD_PORT={port}\n",
    "TAILNET_IP = 127.0.0.1  \nDASHBOARD_PORT=  {port}\t\n",
    "TAILNET_IP=127.0.0.1\r\nDASHBOARD_PORT={port}\r\n",
    "TAILNET_IP=10.255.255.1\nDASHBOARD_PORT={dead}\nTAILNET_IP=127.0.0.1\nDASHBOARD_PORT={port}\n",
    "# TAILNET_IP=10.255.255.1\n; DASHBOARD_PORT={dead}\nTAILNET_IP=127.0.0.1\nDASHBOARD_PORT={port}\n",
    'TAILNET_IP=127.0.0.1\nDASHBOARD_PORT="{port}"\nDASHBOARD_DIR="/a \\"quoted\\" dir"\n',
    "TAILNET_IP=127.0.0.1\nDASHBOARD_PORT={port}",
], ids=["double-quoted", "single-quoted", "leading-whitespace", "whitespace-around-equals",
        "crlf", "last-assignment-wins", "comments", "other-quoted-values", "no-final-newline"])
def test_status_reads_the_address_as_systemd_environmentfile_does(
        tmp_path: Path, units: Units, app: HTTPServer, template: str) -> None:
    _live(units)
    env_file = tmp_path / "http.env"
    env_file.write_text(template.format(port=app.server_address[1], dead=_free_port()),
                        encoding="utf-8")
    result = _run(units, "status", DASHBOARD_ENV_FILE=str(env_file))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"serves {_addr(app)}" in result.stdout


@pytest.mark.parametrize("contents", [
    None,
    "TAILNET_IP=127.0.0.1\n",
    "export TAILNET_IP=127.0.0.1\nexport DASHBOARD_PORT=8090\n",
    "#TAILNET_IP=127.0.0.1\n#DASHBOARD_PORT=8090\n",
], ids=["missing", "no-port", "export-prefix", "commented-out"])
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


@pytest.mark.parametrize("static_state", ["active", "activating", "reloading"])
def test_restart_refuses_while_the_static_unit_serves(
        units: Units, app: HTTPServer, static_state: str) -> None:
    units.set(HTTP_UNIT, active="inactive", enabled="enabled")
    units.set(STATIC_UNIT, active=static_state)
    result = _run(units, "restart", DASHBOARD_PROBE_ADDR=_addr(app))
    assert result.returncode == 3
    assert STATIC_UNIT in result.stderr
    assert "cutover" in result.stderr
    assert not any(call.startswith("restart") for call in units.calls())
    assert (units.dir / f"active.{STATIC_UNIT}").read_text(encoding="utf-8").strip() == static_state


def test_restart_fails_when_systemctl_restart_fails(units: Units, app: HTTPServer) -> None:
    _live(units)
    units.fail("restart", HTTP_UNIT)
    result = _run(units, "restart", DASHBOARD_PROBE_ADDR=_addr(app))
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"systemctl restart {HTTP_UNIT} failed" in result.stderr
    assert "live: " in result.stdout


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
    assert "usage: dashboard-http-service.sh {install|status|restart|rollback}" in result.stderr


REQUIRED_KEYS = ["TAILNET_IP", "DASHBOARD_DIR", "DASHBOARD_PORT"]
PATH_KEYS = ["DASHBOARD_DIR", "DASHBOARD_SETTINGS_SPOOL", "DASHBOARD_SIGNIN_RELAY",
             "DASHBOARD_ACTIONS_AUDIT"]
APP = ROOT / "deploy" / "factory-dashboard-http.py"
UNIT = ROOT / "deploy" / HTTP_UNIT


class Node:
    def __init__(self, root: Path, units: Units) -> None:
        self.units = units
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, text in (("getent", FAKE_GETENT), ("useradd", FAKE_USERADD),
                           ("journalctl", FAKE_JOURNALCTL)):
            path = self.bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)
        self.etc = root / "etc"
        self.etc.mkdir()
        self.env_file = self.etc / "http.env"
        self.dashboard = root / "dashboard"
        self.dashboard.mkdir(mode=0o700)
        self.dashboard.chmod(0o700)
        self.unit_dir = root / "systemd"
        self.lib_dir = root / "lib"
        units.set(STATIC_UNIT, active="active", enabled="enabled")
        units.set(HTTP_UNIT, active="inactive", enabled="disabled")

    def write_env(self, port: int, **keys: str | None) -> None:
        values: dict[str, str | None] = {
            "TAILNET_IP": "127.0.0.1", "DASHBOARD_DIR": str(self.dashboard),
            "DASHBOARD_PORT": str(port), **keys}
        self.env_file.write_text(
            "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8")

    def install(self, script: Path = SERVICE, **env: str) -> subprocess.CompletedProcess:
        return self._verb("install", script, **env)

    def rollback(self, **env: str) -> subprocess.CompletedProcess:
        return self._verb("rollback", SERVICE, **env)

    def _verb(self, verb: str, script: Path, **env: str) -> subprocess.CompletedProcess:
        return _run(self.units, verb, script=script,
                    PATH=f"{self.bin}:{os.environ['PATH']}",
                    DASHBOARD_ENV_FILE=str(self.env_file),
                    DASHBOARD_UNIT_DIR=str(self.unit_dir),
                    DASHBOARD_LIB_DIR=str(self.lib_dir), **env)

    def write_static_unit(self) -> None:
        self.unit_dir.mkdir(exist_ok=True)
        self.static_unit.write_text(
            "[Unit]\nDescription=Factory dashboard (static)\n\n"
            "[Service]\n"
            f"ExecStart=/usr/bin/python3 -m http.server --bind 127.0.0.1 8090 --directory {self.dashboard}\n"
            "Restart=on-failure\n\n"
            "[Install]\nWantedBy=multi-user.target\n",
            encoding="utf-8")

    def user_added(self) -> list[str]:
        log = self.bin / "useradd.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    @property
    def installed_app(self) -> Path:
        return self.lib_dir / "factory-dashboard-http.py"

    @property
    def installed_unit(self) -> Path:
        return self.unit_dir / HTTP_UNIT

    @property
    def drop_in(self) -> Path:
        return self.unit_dir / f"{HTTP_UNIT}.d" / "paths.conf"

    @property
    def static_unit(self) -> Path:
        return self.unit_dir / STATIC_UNIT

    def assert_untouched(self) -> None:
        assert self.units.calls() == []
        assert not self.unit_dir.exists()
        assert not self.lib_dir.exists()
        assert self.user_added() == []
        assert stat.S_IMODE(self.dashboard.stat().st_mode) == 0o700


@pytest.fixture
def node(tmp_path: Path, units: Units) -> Node:
    return Node(tmp_path, units)


@pytest.fixture
def real_static_server(node: Node):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", "--bind", "127.0.0.1", str(port),
         "--directory", str(node.dashboard)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            assert proc.poll() is None, "python3 -m http.server exited before it listened"
            assert time.monotonic() < deadline, "python3 -m http.server never listened"
            time.sleep(0.05)
    yield port
    proc.terminate()
    proc.wait(timeout=10)


def _changes(units: Units) -> list[str]:
    return [call for call in units.calls() if not call.startswith("is-")]


def test_install_cuts_over_and_ends_live(node: Node, app: HTTPServer) -> None:
    node.write_env(app.server_address[1])
    result = node.install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "live: " in result.stdout
    assert _changes(node.units) == [
        "daemon-reload", f"disable {STATIC_UNIT}", f"enable {HTTP_UNIT}", f"restart {HTTP_UNIT}"]
    assert node.units.state(STATIC_UNIT) == ("inactive", "disabled")
    assert node.units.state(HTTP_UNIT) == ("active", "enabled")
    status = _run(node.units, "status", DASHBOARD_ENV_FILE=str(node.env_file))
    assert status.returncode == 0, status.stdout + status.stderr


def test_install_reverts_to_the_static_unit_when_the_app_never_goes_live(
        node: Node, static_server: HTTPServer) -> None:
    node.write_env(static_server.server_address[1])
    result = node.install(DASHBOARD_START_WAIT="1")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "SimpleHTTP/0.6" in result.stderr
    assert f"journal: -u {HTTP_UNIT}" in result.stderr
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")


def test_install_reverts_when_systemctl_restart_fails(node: Node, app: HTTPServer) -> None:
    node.write_env(app.server_address[1])
    node.units.fail("restart", HTTP_UNIT)
    result = node.install()
    assert result.returncode == 1, result.stdout + result.stderr
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")


@pytest.mark.parametrize("call", [
    ("daemon-reload",), ("disable", STATIC_UNIT), ("enable", HTTP_UNIT), ("restart", HTTP_UNIT),
], ids=lambda call: call[0])
def test_install_rerun_fails_when_a_cutover_systemctl_call_fails(
        node: Node, app: HTTPServer, call: tuple[str, ...]) -> None:
    node.write_env(app.server_address[1])
    assert node.install().returncode == 0
    node.units.fail(*call)
    result = node.install()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "live: " not in result.stdout
    assert f"systemctl {' '.join(call)} failed" in result.stderr
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")


def test_install_names_the_three_lines_when_http_env_is_missing(node: Node) -> None:
    result = node.install()
    assert result.returncode == 78
    assert str(node.env_file) in result.stderr
    for line in ("TAILNET_IP=<tailscale ip -4>", "DASHBOARD_DIR=<pushed dashboard directory>",
                 "DASHBOARD_PORT=8090"):
        assert line in result.stderr
    node.assert_untouched()


def test_install_asks_for_root_when_http_env_is_unreadable(node: Node, app: HTTPServer) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a mode 000 file")
    node.write_env(app.server_address[1])
    node.env_file.chmod(0)
    result = node.install()
    assert result.returncode == 78
    assert "root" in result.stderr
    node.assert_untouched()


@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_install_names_only_the_missing_key(
        node: Node, app: HTTPServer, key: str, value: str | None) -> None:
    node.write_env(app.server_address[1], **{key: value})
    result = node.install()
    assert result.returncode == 78
    assert f"{key}=" in result.stderr
    for other in REQUIRED_KEYS:
        if other != key:
            assert f"{other}=" not in result.stderr
    assert "127.0.0.1" not in result.stderr
    assert str(node.dashboard) not in result.stderr
    node.assert_untouched()


@pytest.mark.parametrize("value", [
    '"/srv/a b"', '"/srv/a\nExecStartPre=/bin/false"', "/srv/a:/srv/b", "srv/a",
    "/srv/%h", "/srv/caf\u00e9", "'/srv/a\"b'",
], ids=["space", "newline", "colon", "relative", "specifier", "non-ascii", "quote"])
@pytest.mark.parametrize("key", PATH_KEYS)
def test_install_refuses_a_path_systemd_would_misread(
        node: Node, app: HTTPServer, key: str, value: str) -> None:
    node.write_env(app.server_address[1], **{key: value})
    result = node.install()
    assert result.returncode == 78, result.stdout + result.stderr
    assert key in result.stderr
    assert "/srv/" not in result.stderr
    node.assert_untouched()


def test_install_refuses_a_dashboard_dir_that_does_not_exist(node: Node, app: HTTPServer) -> None:
    node.write_env(app.server_address[1], DASHBOARD_DIR=str(node.dashboard / "nowhere"))
    result = node.install()
    assert result.returncode == 78
    assert "DASHBOARD_DIR" in result.stderr
    node.assert_untouched()


def test_install_never_prints_or_writes_other_http_env_values(node: Node, app: HTTPServer) -> None:
    node.write_env(app.server_address[1],
                   DASHBOARD_GITHUB_CLIENT_ID="SENTINEL-client-id",
                   DASHBOARD_APPROVER="SENTINEL-approver",
                   DASHBOARD_ACTION_CLIENTS="SENTINEL-clients")
    before = node.env_file.read_bytes(), node.env_file.stat().st_mtime_ns
    result = node.install()
    assert result.returncode == 0, result.stdout + result.stderr
    for text in (result.stdout, result.stderr, node.installed_unit.read_text(encoding="utf-8"),
                 node.drop_in.read_text(encoding="utf-8")):
        assert "SENTINEL" not in text
    assert (node.env_file.read_bytes(), node.env_file.stat().st_mtime_ns) == before


def test_install_opens_no_secret_beside_http_env(node: Node, app: HTTPServer) -> None:
    node.write_env(app.server_address[1])
    for name in ("webhook.env", "token"):
        secret = node.etc / name
        secret.write_text("SENTINEL\n", encoding="utf-8")
        secret.chmod(0)
    result = node.install()
    assert result.returncode == 0, result.stdout + result.stderr
    text = SERVICE.read_text(encoding="utf-8").lower()
    assert "webhook" not in text
    assert "token" not in text


def test_install_copies_the_app_and_unit_and_opens_the_dashboard_to_its_group(
        node: Node, app: HTTPServer) -> None:
    node.write_env(app.server_address[1])
    result = node.install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert node.installed_app.read_bytes() == APP.read_bytes()
    assert node.installed_unit.read_bytes() == UNIT.read_bytes()
    assert stat.S_IMODE(node.dashboard.stat().st_mode) & 0o050 == 0o050


def test_install_rerun_replaces_the_app_and_restarts_it(
        tmp_path: Path, node: Node, app: HTTPServer) -> None:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for source in (SERVICE, APP, UNIT):
        (deploy / source.name).write_bytes(source.read_bytes())
    script = deploy / SERVICE.name
    node.write_env(app.server_address[1])
    assert node.install(script=script).returncode == 0
    updated = APP.read_bytes() + b"\n# updated\n"
    (deploy / APP.name).write_bytes(updated)
    result = node.install(script=script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert node.installed_app.read_bytes() == updated
    assert node.units.calls().count(f"restart {HTTP_UNIT}") == 2


@pytest.mark.parametrize("exists", [True, False], ids=["user-exists", "no-user"])
def test_install_adds_factory_dash_only_when_missing(
        node: Node, app: HTTPServer, exists: bool) -> None:
    if exists:
        (node.bin / "factory-dash.exists").touch()
    node.write_env(app.server_address[1])
    result = node.install()
    assert result.returncode == 0, result.stdout + result.stderr
    expected = [] if exists else ["--system --no-create-home --shell /usr/sbin/nologin factory-dash"]
    assert node.user_added() == expected


def test_install_drop_in_binds_only_the_paths_http_env_sets(
        tmp_path: Path, node: Node, app: HTTPServer) -> None:
    spool, relay, audit = tmp_path / "spool", tmp_path / "relay", tmp_path / "audit" / "acts.jsonl"
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    sessions.chmod(0o700)
    (node.bin / "factory-dash.exists").write_text(str(os.getuid()), encoding="utf-8")
    port = app.server_address[1]
    node.write_env(port, DASHBOARD_SETTINGS_SPOOL=str(spool), DASHBOARD_SIGNIN_RELAY=str(relay),
                   DASHBOARD_ACTIONS_AUDIT=str(audit), DASHBOARD_SESSION_STORE=str(sessions))
    assert node.install().returncode == 0
    assert node.drop_in.read_text(encoding="utf-8").splitlines() == [
        "[Service]", f"BindReadOnlyPaths={node.dashboard}", f"BindPaths={spool}",
        f"BindPaths={relay}", f"BindPaths={audit.parent}", f"BindPaths={sessions}"]

    node.write_env(port, DASHBOARD_SETTINGS_SPOOL=str(spool), DASHBOARD_ACTIONS_AUDIT=str(audit))
    assert node.install().returncode == 0
    assert node.drop_in.read_text(encoding="utf-8").splitlines() == [
        "[Service]", f"BindReadOnlyPaths={node.dashboard}", f"BindPaths={spool}",
        f"BindPaths={audit.parent}"]

    node.write_env(port)
    assert node.install().returncode == 0
    assert node.drop_in.read_text(encoding="utf-8").splitlines() == [
        "[Service]", f"BindReadOnlyPaths={node.dashboard}"]
    assert [p.name for p in node.drop_in.parent.iterdir()] == ["paths.conf"]


@pytest.mark.parametrize("key,value", [
    ("DASHBOARD_DIR", "/"),
    ("DASHBOARD_SETTINGS_SPOOL", "/"),
    ("DASHBOARD_SIGNIN_RELAY", "/srv/../etc"),
    ("DASHBOARD_SETTINGS_SPOOL", "/srv/./spool"),
    ("DASHBOARD_ACTIONS_AUDIT", "/acts.jsonl"),
    ("DASHBOARD_SESSION_STORE", "/"),
    ("DASHBOARD_SESSION_STORE", "/var/lib/../etc"),
], ids=["dir-root", "spool-root", "dotdot", "dot", "audit-in-root", "sessions-root", "sessions-dotdot"])
def test_install_refuses_a_bind_that_would_unprotect_the_system(
        node: Node, app: HTTPServer, key: str, value: str) -> None:
    node.write_env(app.server_address[1], **{key: value})
    result = node.install()
    assert result.returncode == 78, result.stdout + result.stderr
    assert key in result.stderr
    node.assert_untouched()


def _session_store(tmp_path: Path, node: Node, case: str) -> Path:
    store = tmp_path / "sessions"
    if case == "missing":
        return store
    if case == "symlink":
        target = tmp_path / "real-sessions"
        target.mkdir(mode=0o700)
        target.chmod(0o700)
        store.symlink_to(target)
        return store
    store.mkdir(mode=0o700)
    store.chmod(0o755 if case == "mode-0755" else 0o700)
    if case == "other-owner":
        (node.bin / "factory-dash.exists").touch()
    return store


@pytest.mark.parametrize("case", ["mode-0755", "missing", "symlink", "other-owner"])
def test_install_refuses_an_unsafe_session_store_before_any_change(
        tmp_path: Path, node: Node, app: HTTPServer, case: str) -> None:
    store = _session_store(tmp_path, node, case)
    node.write_env(app.server_address[1], DASHBOARD_SESSION_STORE=str(store))
    result = node.install()
    assert result.returncode == 78, result.stdout + result.stderr
    assert "DASHBOARD_SESSION_STORE" in result.stderr
    node.assert_untouched()


def test_install_refuses_a_session_store_the_new_factory_dash_would_not_own(
        tmp_path: Path, node: Node, app: HTTPServer) -> None:
    store = tmp_path / "sessions"
    store.mkdir(mode=0o700)
    store.chmod(0o700)
    (node.bin / "useradd").write_text(
        FAKE_USERADD + f'echo {os.getuid() + 1} >"$dir/factory-dash.exists"\n', encoding="utf-8")
    node.write_env(app.server_address[1], DASHBOARD_SESSION_STORE=str(store))
    result = node.install()
    assert result.returncode == 78, result.stdout + result.stderr
    assert "DASHBOARD_SESSION_STORE" in result.stderr
    assert "not owned by factory-dash" in result.stderr
    assert node.units.calls() == []
    assert not node.unit_dir.exists()
    assert not node.lib_dir.exists()


def test_install_rerun_refuses_a_session_store_opened_to_others(
        tmp_path: Path, node: Node, app: HTTPServer) -> None:
    store = tmp_path / "sessions"
    store.mkdir(mode=0o700)
    store.chmod(0o700)
    (node.bin / "factory-dash.exists").write_text(str(os.getuid()), encoding="utf-8")
    node.write_env(app.server_address[1], DASHBOARD_SESSION_STORE=str(store))
    assert node.install().returncode == 0
    calls = node.units.calls()
    store.chmod(0o755)
    result = node.install()
    assert result.returncode == 78, result.stdout + result.stderr
    assert "DASHBOARD_SESSION_STORE" in result.stderr
    assert "0700" in result.stderr
    assert node.units.calls() == calls
    assert node.units.state(HTTP_UNIT) == ("active", "enabled")


def _installed(node: Node, app: HTTPServer, env_port: int) -> None:
    node.write_env(env_port)
    installed = node.install(DASHBOARD_PROBE_ADDR=_addr(app))
    assert installed.returncode == 0, installed.stdout + installed.stderr
    node.write_static_unit()


def _installed_then_rolled_back(node: Node, app: HTTPServer, static_port: int) -> subprocess.CompletedProcess:
    _installed(node, app, static_port)
    return node.rollback()


def test_rollback_after_install_restores_the_static_server(
        node: Node, app: HTTPServer, real_static_server: int) -> None:
    result = _installed_then_rolled_back(node, app, real_static_server)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("rolled back"), result.stdout
    assert f"127.0.0.1:{real_static_server}" in result.stdout
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")
    assert _changes(node.units)[-2:] == [f"enable --now {STATIC_UNIT}", f"disable --now {HTTP_UNIT}"]
    status = _run(node.units, "status", DASHBOARD_ENV_FILE=str(node.env_file))
    assert status.returncode == 3, status.stdout + status.stderr


def test_after_rollback_settings_is_read_only(
        node: Node, app: HTTPServer, real_static_server: int) -> None:
    result = _installed_then_rolled_back(node, app, real_static_server)
    assert result.returncode == 0, result.stdout + result.stderr
    conn = http.client.HTTPConnection("127.0.0.1", real_static_server, timeout=5)
    conn.request("GET", "/api/session")
    session = conn.getresponse()
    session.read()
    assert session.status == 404, "GET /api/session must not answer 200 after rollback"
    conn.request("GET", "/")
    index = conn.getresponse()
    index.read()
    conn.close()
    assert index.getheader("Server", "").startswith("SimpleHTTP"), index.getheader("Server")


def test_rollback_leaves_http_env_app_unit_and_drop_in_in_place(
        node: Node, app: HTTPServer, real_static_server: int) -> None:
    _installed(node, app, real_static_server)
    kept = [node.env_file, node.installed_app, node.installed_unit, node.drop_in, node.static_unit]
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in kept]
    users_before = node.user_added()
    result = node.rollback()
    assert result.returncode == 0, result.stdout + result.stderr
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in kept] == before
    assert node.user_added() == users_before


def test_rollback_refuses_when_the_static_unit_file_is_missing(
        node: Node, real_static_server: int) -> None:
    node.write_env(real_static_server)
    _live(node.units)
    result = node.rollback()
    assert result.returncode == 1, result.stdout + result.stderr
    assert str(node.static_unit) in result.stderr
    assert node.units.calls() == []


def test_rollback_keeps_the_app_when_starting_the_static_unit_fails(
        node: Node, app: HTTPServer) -> None:
    _installed(node, app, app.server_address[1])
    node.units.fail("enable", STATIC_UNIT)
    result = node.rollback()
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"enable --now {STATIC_UNIT}" in result.stderr
    assert node.units.state(HTTP_UNIT) == ("active", "enabled")
    assert f"disable --now {HTTP_UNIT}" not in node.units.calls()


def test_rollback_says_the_http_unit_is_still_enabled_when_disabling_it_fails(
        node: Node, app: HTTPServer, real_static_server: int) -> None:
    _installed(node, app, real_static_server)
    node.units.fail("disable", HTTP_UNIT)
    result = node.rollback()
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{HTTP_UNIT} is still enabled" in result.stderr
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")


def test_rollback_is_3_while_the_app_still_answers(
        node: Node, app: HTTPServer, real_static_server: int) -> None:
    _installed(node, app, real_static_server)
    result = node.rollback(DASHBOARD_PROBE_ADDR=_addr(app), DASHBOARD_START_WAIT="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.startswith("not rolled back: "), result.stdout
    assert "Server: factory-dashboard" in result.stdout
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")


def test_rollback_is_3_when_api_session_answers_200(
        node: Node, static_server: HTTPServer, real_static_server: int) -> None:
    node.write_env(real_static_server)
    node.write_static_unit()
    result = node.rollback(DASHBOARD_PROBE_ADDR=_addr(static_server), DASHBOARD_START_WAIT="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.startswith("not rolled back: "), result.stdout
    assert "/api/session" in result.stdout


def test_rollback_is_3_when_nothing_listens(node: Node, real_static_server: int) -> None:
    node.write_env(real_static_server)
    _live(node.units)
    node.write_static_unit()
    addr = f"127.0.0.1:{_free_port()}"
    result = node.rollback(DASHBOARD_PROBE_ADDR=addr, DASHBOARD_START_WAIT="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.startswith("not rolled back: "), result.stdout
    assert f"nothing answers at {addr}" in result.stdout
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")


def test_rollback_is_3_when_the_address_is_unknown(node: Node) -> None:
    _live(node.units)
    node.write_static_unit()
    result = node.rollback(DASHBOARD_START_WAIT="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.startswith("not rolled back: "), result.stdout
    assert str(node.env_file) in result.stdout
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")


def test_rollback_when_already_static_is_0(node: Node, real_static_server: int) -> None:
    node.write_env(real_static_server)
    node.write_static_unit()
    result = node.rollback()
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("rolled back"), result.stdout
    assert node.units.state(STATIC_UNIT) == ("active", "enabled")
    assert node.units.state(HTTP_UNIT) == ("inactive", "disabled")
