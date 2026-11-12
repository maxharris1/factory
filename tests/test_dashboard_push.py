import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib.dashboard_config import DashboardConfig, PushConfig, parse_dashboard
from lib.dashboard_push import (
    NoopTransport,
    PushError,
    SshTransport,
    run_push,
    transport_for,
)
from lib.load import Finding
from lib.orch_yml import load_orch_yml

ROOT = Path(__file__).resolve().parents[1]
TARGET = "pc-wsl-bots.tail68624b.ts.net"

BASE_ORCH = """\
poll_seconds: 60
projects:
  - repo: maxharris1/factory
"""


def parse(value: object) -> tuple[DashboardConfig | None, list[Finding]]:
    findings: list[Finding] = []
    return parse_dashboard(value, findings), findings


def errors(findings: list[Finding]) -> list[str]:
    return [item.message for item in findings if item.severity == "error"]


# --- config -------------------------------------------------------------------


def test_absent_dashboard_section_defaults_to_no_push(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(BASE_ORCH, encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert errors(findings) == []
    assert config is not None
    assert config.dashboard == DashboardConfig()
    assert config.dashboard.refresh_seconds == 20
    assert config.dashboard.push == PushConfig()
    assert config.dashboard.push.transport == "none"


def test_ssh_push_section_parses(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(
        BASE_ORCH
        + f"""\
dashboard:
  refresh_seconds: 15
  push:
    transport: ssh
    host: {TARGET}
    remote_dir: /home/factory-dash/site
    method: scp
    interval_seconds: 30
""",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert errors(findings) == []
    assert config is not None
    assert config.dashboard == DashboardConfig(
        refresh_seconds=15,
        push=PushConfig(
            transport="ssh",
            host=TARGET,
            remote_dir="/home/factory-dash/site",
            method="scp",
            interval_seconds=30,
        ),
    )


def test_explicit_none_transport_is_valid() -> None:
    config, findings = parse({"push": {"transport": "none"}})
    assert errors(findings) == []
    assert config == DashboardConfig()


@pytest.mark.parametrize(
    "value,message",
    [
        ("ssh", "dashboard must be a mapping"),
        ({"bogus": 1}, "unknown field 'dashboard.bogus'"),
        ({"refresh_seconds": 10}, "dashboard.refresh_seconds must be an integer from 15 to 30"),
        ({"refresh_seconds": True}, "dashboard.refresh_seconds must be an integer from 15 to 30"),
        ({"push": {"transport": "http"}}, "dashboard.push.transport must be none or ssh"),
        ({"push": {"transport": "ssh"}}, "dashboard.push.host is required for ssh"),
        (
            {"push": {"transport": "ssh", "host": TARGET}},
            "dashboard.push.remote_dir is required for ssh",
        ),
        (
            {"push": {"transport": "ssh", "host": "-oProxyCommand=sh", "remote_dir": "/srv/d"}},
            "dashboard.push.host must be an SSH host alias or tailnet name",
        ),
        (
            {"push": {"transport": "ssh", "host": "max@" + TARGET, "remote_dir": "/srv/d"}},
            "dashboard.push.host must be an SSH host alias or tailnet name",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "relative/dir"}},
            "dashboard.push.remote_dir must be an absolute path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/../etc"}},
            "dashboard.push.remote_dir must be an absolute path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/d;rm -rf ~"}},
            "dashboard.push.remote_dir must be an absolute path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/"}},
            "dashboard.push.remote_dir must be an absolute path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/REPLACE_WITH_DIR"}},
            "dashboard.push.remote_dir is a REPLACE_WITH_ placeholder",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/d", "method": "ftp"}},
            "dashboard.push.method must be rsync or scp",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/d", "interval_seconds": 5}},
            "dashboard.push.interval_seconds must be an integer from 15 to 30",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/d", "identity_file": "~/.ssh/id"}},
            "unknown field 'dashboard.push.identity_file'",
        ),
        (
            {"push": {"transport": "none", "host": TARGET}},
            "dashboard.push.host is only for transport ssh",
        ),
    ],
)
def test_invalid_dashboard_config_is_refused(value: object, message: str) -> None:
    config, findings = parse(value)
    assert config is None
    assert any(message in error for error in errors(findings)), errors(findings)


def test_check_config_reports_dashboard_errors(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(BASE_ORCH + "dashboard:\n  push:\n    transport: carrier-pigeon\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "check-config", "--orch", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "dashboard.push.transport must be none or ssh" in result.stderr


# --- transports -----------------------------------------------------------------


class Recorder:
    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


def files(tmp_path: Path) -> list[Path]:
    status = tmp_path / "status.json"
    index = tmp_path / "index.html"
    status.write_text("{}", encoding="utf-8")
    index.write_text("<!doctype html>", encoding="utf-8")
    return [status, index]


def test_noop_transport_does_nothing(tmp_path: Path) -> None:
    NoopTransport().send(files(tmp_path))


def test_transport_for_unconfigured_is_the_noop() -> None:
    assert isinstance(transport_for(PushConfig()), NoopTransport)


def test_rsync_command_construction(tmp_path: Path) -> None:
    runner = Recorder()
    paths = files(tmp_path)
    transport = SshTransport(TARGET, "/srv/factory-dashboard", runner=runner)
    transport.send(paths)
    ((argv, kwargs),) = runner.calls
    assert argv == [
        "rsync",
        "--quiet",
        "--timeout=30",
        "--chmod=F644",
        "-e",
        "ssh -o BatchMode=yes -o ConnectTimeout=10",
        "--",
        str(paths[0]),
        str(paths[1]),
        f"{TARGET}:/srv/factory-dashboard/",
    ]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 60
    assert kwargs["check"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_scp_command_construction(tmp_path: Path) -> None:
    runner = Recorder()
    paths = files(tmp_path)
    SshTransport("pc-wsl-bots", "/srv/factory-dashboard", method="scp", runner=runner).send(paths)
    ((argv, _),) = runner.calls
    assert argv == [
        "scp",
        "-q",
        "-B",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        str(paths[0]),
        str(paths[1]),
        "pc-wsl-bots:/srv/factory-dashboard/",
    ]


def test_ssh_transport_never_names_keys_or_credentials(tmp_path: Path) -> None:
    for method in ("rsync", "scp"):
        runner = Recorder()
        SshTransport(TARGET, "/srv/d", method=method, runner=runner).send(files(tmp_path))
        argv = " ".join(runner.calls[0][0])
        for needle in ("-i ", "IdentityFile", ".ssh", "id_", "Password", "StrictHostKeyChecking=no"):
            assert needle not in argv


def test_ssh_transport_failure_raises_redacted(tmp_path: Path) -> None:
    secret = "gh" + "p_" + "A1b2C3d4E5" * 4
    runner = Recorder(returncode=255, stderr=f"ssh: connect failed {secret}\nmore\n")
    with pytest.raises(PushError) as caught:
        SshTransport(TARGET, "/srv/d", runner=runner).send(files(tmp_path))
    assert "exit 255" in str(caught.value)
    assert secret not in str(caught.value)


def test_ssh_transport_timeout_raises(tmp_path: Path) -> None:
    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(PushError, match="timed out"):
        SshTransport(TARGET, "/srv/d", runner=slow).send(files(tmp_path))


def test_ssh_transport_refuses_unvalidated_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SshTransport("-oProxyCommand=sh", "/srv/d")
    with pytest.raises(ValueError):
        SshTransport(TARGET, "/srv/$(id)")


def test_transport_for_ssh_config(tmp_path: Path) -> None:
    runner = Recorder()
    transport = transport_for(
        PushConfig(transport="ssh", host=TARGET, remote_dir="/srv/d", method="scp"),
        runner=runner,
    )
    assert isinstance(transport, SshTransport)
    transport.send(files(tmp_path))
    assert runner.calls[0][0][0] == "scp"


# --- push loop ------------------------------------------------------------------


class FakeTransport:
    def __init__(self, fail_on: set[int] | None = None):
        self.sent: list[list[Path]] = []
        self.fail_on = fail_on or set()

    def send(self, paths):
        self.sent.append(list(paths))
        if len(self.sent) in self.fail_on:
            raise PushError("ssh exit 255")


def snapshotter(tmp_path: Path):
    calls: list[int] = []

    def snapshot() -> tuple[Path, Path]:
        calls.append(1)
        return tuple(files(tmp_path))

    return snapshot, calls


def test_unconfigured_push_is_a_noop_that_logs_one_line(tmp_path: Path) -> None:
    lines: list[str] = []
    snapshot, calls = snapshotter(tmp_path)
    sleeps: list[float] = []
    code = run_push(
        transport=NoopTransport(),
        snapshot=snapshot,
        interval=20,
        once=False,
        sleep=sleeps.append,
        log=lines.append,
    )
    assert code == 0
    assert lines == [
        "dashboard push: not configured (dashboard.push.transport is none); nothing pushed"
    ]
    assert calls == []
    assert sleeps == []


def test_configured_push_sends_snapshot_files(tmp_path: Path) -> None:
    transport = FakeTransport()
    snapshot, calls = snapshotter(tmp_path)
    code = run_push(
        transport=transport,
        snapshot=snapshot,
        interval=20,
        once=True,
        sleep=lambda _: pytest.fail("once must not sleep"),
        log=lambda _: None,
    )
    assert code == 0
    assert calls == [1]
    assert transport.sent == [[tmp_path / "status.json", tmp_path / "index.html"]]


def test_push_loop_sleeps_interval_and_survives_errors(tmp_path: Path) -> None:
    transport = FakeTransport(fail_on={2})
    snapshot, calls = snapshotter(tmp_path)
    sleeps: list[float] = []
    lines: list[str] = []

    class Stop(Exception):
        pass

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    with pytest.raises(Stop):
        run_push(
            transport=transport,
            snapshot=snapshot,
            interval=25,
            once=False,
            sleep=sleep,
            log=lines.append,
        )
    assert len(transport.sent) == 3
    assert sleeps == [25, 25, 25]
    assert lines == ["dashboard push: ssh exit 255"]


def test_push_once_reports_failure(tmp_path: Path) -> None:
    lines: list[str] = []
    snapshot, _ = snapshotter(tmp_path)
    code = run_push(
        transport=FakeTransport(fail_on={1}),
        snapshot=snapshot,
        interval=20,
        once=True,
        sleep=lambda _: None,
        log=lines.append,
    )
    assert code == 1
    assert lines == ["dashboard push: ssh exit 255"]


@pytest.mark.parametrize("interval", [14, 31])
def test_push_interval_out_of_range_refused(tmp_path: Path, interval: int) -> None:
    snapshot, _ = snapshotter(tmp_path)
    with pytest.raises(ValueError, match="15 to 30"):
        run_push(
            transport=FakeTransport(),
            snapshot=snapshot,
            interval=interval,
            once=True,
            sleep=lambda _: None,
            log=lambda _: None,
        )


def test_snapshot_failure_is_logged_redacted_and_not_sent(tmp_path: Path) -> None:
    transport = FakeTransport()
    lines: list[str] = []
    secret = "lin" + "_api_" + "Q7w8E9r0T1" * 4

    def broken():
        raise OSError(f"cannot read {secret}")

    code = run_push(
        transport=transport,
        snapshot=broken,
        interval=20,
        once=True,
        sleep=lambda _: None,
        log=lines.append,
    )
    assert code == 1
    assert transport.sent == []
    assert len(lines) == 1 and secret not in lines[0]


# --- CLI ------------------------------------------------------------------------


def run_orch(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {
        key: value for key, value in os.environ.items() if not key.startswith("ORCH_")
    }
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), *args],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def test_cli_push_unconfigured_is_noop(tmp_path: Path) -> None:
    orch = tmp_path / "orch.yml"
    orch.write_text(BASE_ORCH, encoding="utf-8")
    out = tmp_path / "out"
    result = run_orch(
        "dashboard",
        "push",
        "--orch",
        str(orch),
        "--db",
        str(tmp_path / "never-opened.db"),
        "--out",
        str(out),
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.strip().splitlines() == [
        "dashboard push: not configured (dashboard.push.transport is none); nothing pushed"
    ]
    assert not out.exists()
    assert not (tmp_path / "never-opened.db").exists()


def test_cli_push_with_shipped_orch_yml_is_noop(tmp_path: Path) -> None:
    result = run_orch("dashboard", "push", "--db", str(tmp_path / "x.db"), "--once")
    assert result.returncode == 0, result.stderr
    assert "not configured" in result.stderr


def test_cli_push_refuses_invalid_orch_yml(tmp_path: Path) -> None:
    orch = tmp_path / "orch.yml"
    orch.write_text(BASE_ORCH + "dashboard:\n  push:\n    transport: ssh\n", encoding="utf-8")
    result = run_orch("dashboard", "push", "--orch", str(orch), "--db", str(tmp_path / "x.db"))
    assert result.returncode == 1
    assert "dashboard.push.host is required for ssh" in result.stderr
