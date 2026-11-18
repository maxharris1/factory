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
from lib.qa_evidence import EvidenceRefused, RunFile, run_dir, walk_run, write_run_tar
from tests.test_qa_evidence_recycle import REPO, TICKET, _lane, root  # noqa: F401

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


DEPLOY_PUSH = """\
dashboard:
  push:
    transport: ssh
    host: pc-wsl-cursor-bot
    ssh_config: /home/box/.ssh/config
    remote_dir: factory-dashboard
    method: scp
    every_minutes: 5
"""


def test_pc_wsl_bots_deploy_push_section_parses(tmp_path: Path) -> None:
    path = tmp_path / "orch.yml"
    path.write_text(BASE_ORCH + DEPLOY_PUSH, encoding="utf-8")
    config, findings = load_orch_yml(path)
    assert errors(findings) == []
    assert config is not None
    assert config.dashboard.push == PushConfig(
        transport="ssh",
        host="pc-wsl-cursor-bot",
        ssh_config="/home/box/.ssh/config",
        remote_dir="factory-dashboard",
        method="scp",
        every_minutes=5,
    )


def test_ssh_push_defaults_to_factory_dashboard_in_the_remote_home_each_cycle() -> None:
    config, findings = parse({"push": {"transport": "ssh", "host": "pc-wsl-cursor-bot"}})
    assert errors(findings) == []
    assert config is not None
    assert config.push.remote_dir == "factory-dashboard"
    assert config.push.ssh_config is None
    assert config.push.every_minutes is None


def test_shipped_orch_yml_documents_the_push_but_leaves_it_off() -> None:
    path = ROOT / "orch.yml"
    text = path.read_text(encoding="utf-8")
    for needle in (
        "#     host: pc-wsl-cursor-bot",
        "#     ssh_config: /home/box/.ssh/config",
        "#     remote_dir: factory-dashboard",
        "#     method: scp",
        "http://pc-wsl-bots.tail68624b.ts.net:8090/",
    ):
        assert needle in text, needle
    config, findings = load_orch_yml(path, allow_placeholders=True)
    assert errors(findings) == []
    assert config is not None
    assert config.dashboard.push == PushConfig()


def test_example_orch_yml_pushes_to_pc_wsl_bots() -> None:
    path = ROOT / "config" / "orch.example.yml"
    config, findings = load_orch_yml(path)
    assert errors(findings) == []
    assert config is not None
    assert config.dashboard.push == PushConfig(
        transport="ssh",
        host="pc-wsl-cursor-bot",
        ssh_config="/home/box/.ssh/config",
        remote_dir="factory-dashboard",
        method="scp",
        every_minutes=None,
    )
    assert "http://pc-wsl-bots.tail68624b.ts.net:8090/" in path.read_text(encoding="utf-8")


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
            {"push": {"transport": "ssh", "host": "-oProxyCommand=sh", "remote_dir": "/srv/d"}},
            "dashboard.push.host must be an SSH host alias or tailnet name",
        ),
        (
            {"push": {"transport": "ssh", "host": "max@" + TARGET, "remote_dir": "/srv/d"}},
            "dashboard.push.host must be an SSH host alias or tailnet name",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/../etc"}},
            "dashboard.push.remote_dir must be a plain path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "a/../../etc"}},
            "dashboard.push.remote_dir must be a plain path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/srv/d;rm -rf ~"}},
            "dashboard.push.remote_dir must be a plain path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "/"}},
            "dashboard.push.remote_dir must be a plain path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "-rf"}},
            "dashboard.push.remote_dir must be a plain path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "remote_dir": "~/factory-dashboard"}},
            "dashboard.push.remote_dir must be a plain path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "ssh_config": "ssh/config"}},
            "dashboard.push.ssh_config must be an absolute path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "ssh_config": "/home/box/../root/.ssh/config"}},
            "dashboard.push.ssh_config must be an absolute path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "ssh_config": "/home/box/.ssh/config x"}},
            "dashboard.push.ssh_config must be an absolute path",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "ssh_config": "/home/box/.ssh/id_ed25519"}},
            "dashboard.push.ssh_config must name an ssh_config file, not a key",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "ssh_config": "/home/box/.ssh/deploy.pem"}},
            "dashboard.push.ssh_config must name an ssh_config file, not a key",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "ssh_config": "/home/REPLACE_WITH_USER/.ssh/config"}},
            "dashboard.push.ssh_config is a REPLACE_WITH_ placeholder",
        ),
        (
            {"push": {"transport": "none", "ssh_config": "/home/box/.ssh/config"}},
            "dashboard.push.ssh_config is only for transport ssh",
        ),
        (
            {"push": {"transport": "none", "every_minutes": 5}},
            "dashboard.push.every_minutes is only for transport ssh",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "every_minutes": 0}},
            "dashboard.push.every_minutes must be an integer from 1 to 1440",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "every_minutes": 1441}},
            "dashboard.push.every_minutes must be an integer from 1 to 1440",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "every_minutes": True}},
            "dashboard.push.every_minutes must be an integer from 1 to 1440",
        ),
        (
            {"push": {"transport": "ssh", "host": TARGET, "every_minutes": "5"}},
            "dashboard.push.every_minutes must be an integer from 1 to 1440",
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


# #224: connect and keepalive timeouts come from the host's ssh_config.
OPTIONS = ["-o", "BatchMode=yes"]
CONFIG_OPTIONS = ["-F", "/home/box/.ssh/config", *OPTIONS]
RENAME_BOTH = (
    "cd -- {dir} && mv -f -- .status.json.push-tmp status.json"
    " && mv -f -- .index.html.push-tmp index.html"
)


@pytest.mark.parametrize("method", ["scp", "rsync"])
@pytest.mark.parametrize("ssh_config", [None, "/home/box/.ssh/config"])
def test_push_argv_leaves_connect_and_keepalive_timeouts_to_ssh_config(
    tmp_path: Path, method: str, ssh_config: str | None
) -> None:
    runner = Recorder()
    SshTransport(
        TARGET, "factory-dashboard", method=method, ssh_config=ssh_config, runner=runner
    ).send(files(tmp_path))
    assert len(runner.calls) == 3
    for argv, _ in runner.calls:
        joined = " ".join(argv)
        for option in ("ConnectTimeout", "ServerAliveInterval", "ServerAliveCountMax"):
            assert option not in joined
        assert "BatchMode=yes" in joined


def test_rsync_uploads_to_temp_names_then_renames_both_in_one_ssh(tmp_path: Path) -> None:
    runner = Recorder()
    paths = files(tmp_path)
    transport = SshTransport(TARGET, "/srv/factory-dashboard", runner=runner)
    transport.send(paths)
    rsync = ["rsync", "--quiet", "--timeout=10", "--chmod=F644", "-e",
             "ssh " + " ".join(OPTIONS), "--"]
    assert [argv for argv, _ in runner.calls] == [
        [*rsync, str(paths[0]), f"{TARGET}:/srv/factory-dashboard/.status.json.push-tmp"],
        [*rsync, str(paths[1]), f"{TARGET}:/srv/factory-dashboard/.index.html.push-tmp"],
        ["ssh", *OPTIONS, "--", TARGET, RENAME_BOTH.format(dir="/srv/factory-dashboard")],
    ]
    for _, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 20
        assert kwargs["check"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL


def test_scp_uploads_to_temp_names_then_renames_both_in_one_ssh(tmp_path: Path) -> None:
    runner = Recorder()
    paths = files(tmp_path)
    SshTransport("pc-wsl-bots", "/srv/factory-dashboard", method="scp", runner=runner).send(paths)
    assert [argv for argv, _ in runner.calls] == [
        ["scp", "-q", "-B", *OPTIONS, "--", str(paths[0]),
         "pc-wsl-bots:/srv/factory-dashboard/.status.json.push-tmp"],
        ["scp", "-q", "-B", *OPTIONS, "--", str(paths[1]),
         "pc-wsl-bots:/srv/factory-dashboard/.index.html.push-tmp"],
        ["ssh", *OPTIONS, "--", "pc-wsl-bots",
         RENAME_BOTH.format(dir="/srv/factory-dashboard")],
    ]
    for _, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL


def test_pc_wsl_cursor_bot_push_uses_the_ssh_config_and_the_home_relative_dir(
    tmp_path: Path,
) -> None:
    """The documented target: `scp -F /home/box/.ssh/config <files>
    pc-wsl-cursor-bot:factory-dashboard/`, i.e. /home/cursor-bot/factory-dashboard."""
    runner = Recorder()
    paths = files(tmp_path)
    transport = transport_for(
        PushConfig(
            transport="ssh",
            host="pc-wsl-cursor-bot",
            ssh_config="/home/box/.ssh/config",
            remote_dir="factory-dashboard",
            method="scp",
        ),
        runner=runner,
    )
    transport.send(paths)
    assert [argv for argv, _ in runner.calls] == [
        ["scp", "-q", "-B", *CONFIG_OPTIONS, "--", str(paths[0]),
         "pc-wsl-cursor-bot:factory-dashboard/.status.json.push-tmp"],
        ["scp", "-q", "-B", *CONFIG_OPTIONS, "--", str(paths[1]),
         "pc-wsl-cursor-bot:factory-dashboard/.index.html.push-tmp"],
        ["ssh", *CONFIG_OPTIONS, "--", "pc-wsl-cursor-bot",
         RENAME_BOTH.format(dir="factory-dashboard")],
    ]


def test_rsync_passes_the_ssh_config_to_its_ssh(tmp_path: Path) -> None:
    runner = Recorder()
    SshTransport(
        "pc-wsl-cursor-bot",
        "factory-dashboard",
        ssh_config="/home/box/.ssh/config",
        runner=runner,
    ).send(files(tmp_path))
    first = runner.calls[0][0]
    assert first[first.index("-e") + 1] == (
        "ssh " + " ".join(CONFIG_OPTIONS)
    )
    assert runner.calls[-1][0][:3] == ["ssh", "-F", "/home/box/.ssh/config"]


class FakeRemote:
    """Runs scp, rsync and the one `ssh host 'cd -- dir && mv ...'` against a
    local directory standing in for the remote login's home. After every
    command it records what a reader of the served directory would see.
    `fail_scp_of` names a local file whose upload writes only a prefix of it,
    then exits 1 (a dropped link); `fail_rename` makes the ssh step exit 1."""

    def __init__(
        self,
        home: Path,
        served: str,
        fail_scp_of: str | None = None,
        fail_rename: bool = False,
    ):
        self.home = home
        self.served = served
        self.fail_scp_of = fail_scp_of
        self.fail_rename = fail_rename
        self.seen: list[tuple[str, str]] = []
        self.programs: list[str] = []

    def local(self, remote_path: str) -> Path:
        return self.home / remote_path.lstrip("/")

    def observe(self) -> None:
        site = self.local(self.served)
        self.seen.append(
            (
                (site / "status.json").read_text(encoding="utf-8"),
                (site / "index.html").read_text(encoding="utf-8"),
            )
        )

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.programs.append(argv[0])
        result = self._run(argv)
        self.observe()
        return result

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        if argv[0] in ("scp", "rsync"):
            separator = argv.index("--")
            source, destination = argv[separator + 1 :]
            target = self.local(destination.split(":", 1)[1])
            data = Path(source).read_bytes()
            if Path(source).name == self.fail_scp_of:
                target.write_bytes(data[: len(data) // 2])
                return subprocess.CompletedProcess(argv, 1, "", "lost connection")
            target.write_bytes(data)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "ssh":
            separator = argv.index("--")
            (command,) = argv[separator + 2 :]
            steps = command.split(" && ")
            cd = steps[0].split()
            assert cd[:2] == ["cd", "--"], command
            here = self.local(cd[2])
            if self.fail_rename:
                return subprocess.CompletedProcess(argv, 1, "", "mv: cannot move")
            for step in steps[1:]:
                words = step.split()
                assert words[:3] == ["mv", "-f", "--"], command
                os.replace(here / words[3], here / words[4])
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")


def _published(tmp_path: Path, served: str = "srv/d") -> Path:
    home = tmp_path / "remote-home"
    site = home / served
    site.mkdir(parents=True)
    (site / "status.json").write_text('{"old": true}', encoding="utf-8")
    (site / "index.html").write_text("<!doctype html><p>old page</p>", encoding="utf-8")
    return home


def _new_files(tmp_path: Path) -> list[Path]:
    local = tmp_path / "local"
    local.mkdir()
    status = local / "status.json"
    index = local / "index.html"
    status.write_text('{"new": true, "pad": "' + "s" * 400 + '"}', encoding="utf-8")
    index.write_text("<!doctype html><p>new page</p>" + "i" * 400, encoding="utf-8")
    return [status, index]


OLD_PAIR = ('{"old": true}', "<!doctype html><p>old page</p>")


@pytest.mark.parametrize("method", ["scp", "rsync"])
def test_interrupted_upload_leaves_the_published_pair_whole(tmp_path: Path, method: str) -> None:
    home = _published(tmp_path)
    runner = FakeRemote(home, "/srv/d", fail_scp_of="index.html")
    with pytest.raises(PushError, match="exit 1"):
        SshTransport(TARGET, "/srv/d", method=method, runner=runner).send(_new_files(tmp_path))
    assert runner.programs == [method, method]
    assert set(runner.seen) == {OLD_PAIR}


def test_failed_rename_is_a_push_error(tmp_path: Path) -> None:
    home = _published(tmp_path)
    runner = FakeRemote(home, "/srv/d", fail_rename=True)
    with pytest.raises(PushError, match="ssh to .* failed \\(exit 1\\)"):
        SshTransport(TARGET, "/srv/d", method="scp", runner=runner).send(_new_files(tmp_path))
    assert set(runner.seen) == {OLD_PAIR}


@pytest.mark.parametrize("method", ["scp", "rsync"])
@pytest.mark.parametrize("served", ["/srv/d", "factory-dashboard"])
def test_push_publishes_complete_files_and_never_a_mismatched_pair(
    tmp_path: Path, method: str, served: str
) -> None:
    home = _published(tmp_path, served.lstrip("/"))
    paths = _new_files(tmp_path)
    runner = FakeRemote(home, served)
    SshTransport(TARGET, served, method=method, runner=runner).send(paths)
    site = home / served.lstrip("/")
    for path in paths:
        assert (site / path.name).read_bytes() == path.read_bytes()
    assert sorted(path.name for path in site.iterdir()) == ["index.html", "status.json"]
    new_pair = tuple(path.read_text(encoding="utf-8") for path in paths)
    # Uploads first, then exactly one rename step: a reader sees the old pair
    # until the rename, then the new pair, never one of each.
    mismatched = [pair for pair in runner.seen if pair not in (OLD_PAIR, new_pair)]
    assert mismatched == []
    assert runner.seen == [OLD_PAIR, OLD_PAIR, new_pair]
    assert runner.programs == [method, method, "ssh"]


def test_ssh_transport_never_names_keys_or_credentials(tmp_path: Path) -> None:
    for method in ("rsync", "scp"):
        runner = Recorder()
        SshTransport(TARGET, "/srv/d", method=method, runner=runner).send(files(tmp_path))
        argv = " ".join(" ".join(call) for call, _ in runner.calls)
        for needle in ("-i ", "IdentityFile", ".ssh", "id_", "Password", "StrictHostKeyChecking=no"):
            assert needle not in argv


def test_ssh_config_is_the_only_path_under_dot_ssh_passed(tmp_path: Path) -> None:
    for method in ("rsync", "scp"):
        runner = Recorder()
        SshTransport(
            "pc-wsl-cursor-bot",
            "factory-dashboard",
            method=method,
            ssh_config="/home/box/.ssh/config",
            runner=runner,
        ).send(files(tmp_path))
        argv = " ".join(" ".join(call) for call, _ in runner.calls)
        argv = argv.replace("-F /home/box/.ssh/config", "-F CONFIG")
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
    with pytest.raises(ValueError):
        SshTransport(TARGET, "-rf")
    with pytest.raises(ValueError):
        SshTransport(TARGET, "factory-dashboard", ssh_config="relative/config")
    with pytest.raises(ValueError):
        SshTransport(TARGET, "factory-dashboard", ssh_config="/home/box/.ssh/id_rsa")
    transport = SshTransport(TARGET, "factory-dashboard", runner=Recorder())
    bad = tmp_path / "-rf;x"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        transport.commands([bad])


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


# --- QA evidence (#910) ---------------------------------------------------------


LANE_FILES = ["s-1/after-screenshot.png", "s-1/console.log"]


def test_a_file_that_changed_after_the_walk_is_refused(root: Path, tmp_path: Path) -> None:
    folder = run_dir(root, REPO, TICKET, "run-1")
    _lane(folder, 1, {"s-1": "pass"}, LANE_FILES)
    parts = folder.relative_to(root).parts
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        first, *rest = walk_run(root_fd, parts)
        grown = RunFile(first.path, first.size + 1, first.mtime_ns)
        with (tmp_path / "out.tar").open("wb") as out, pytest.raises(EvidenceRefused, match="changed since"):
            write_run_tar(out, root_fd, parts, (grown, *rest), b"<p>")
    finally:
        os.close(root_fd)
