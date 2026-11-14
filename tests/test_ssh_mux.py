"""#222: factory SSH calls to one device reuse one ControlMaster.

Every call goes through the real `SshTransport` and tests/fake_ssh.py on
PATH; the fake records data calls in $FAKE_SSH_LOG (with `via`) and control
calls (master starts, checks, stops) in `$FAKE_SSH_LOG.control`.
"""

from __future__ import annotations

import io
import json
import os
import socket
import time
from pathlib import Path

import pytest

from lib import remote, ssh_mux
from lib.remote_follow import EXITED, Follower
from tests.test_detached_remote import ALIAS, Box, Stop
from tests.test_remote_start import Ssh, use_fake_ssh

USER = "codex-bot"


@pytest.fixture
def ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ssh:
    fake = Ssh(tmp_path)
    use_fake_ssh(monkeypatch, fake)
    return fake


def target(tmp_path: Path, alias: str = ALIAS, user: str = USER) -> remote.RemoteTarget:
    return remote.RemoteTarget(
        device="pc", alias=alias, user=user, workdir_root=str(tmp_path / "work"),
        job_id="job1", tag=remote.new_tag(),
    )


def control(ssh: Ssh) -> list[dict]:
    path = Path(f"{ssh.log}.control")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def ops(ssh: Ssh) -> list[tuple[str, int]]:
    return [(entry["op"], entry["code"]) for entry in control(ssh)]


def socket_of(alias: str = ALIAS, user: str = USER) -> Path:
    return Path(ssh_mux.socket_path(os.environ["ORCH_SSH_CONTROL_DIR"], alias, user))


def vias(ssh: Ssh) -> list[str]:
    return [call["via"] for call in ssh.calls()]


def test_repeated_calls_to_one_alias_share_one_master(ssh: Ssh, tmp_path: Path) -> None:
    t = target(tmp_path)
    transport = remote.SshTransport()
    assert remote.probe_device(transport, t)
    assert remote.check_worker(transport, t) == remote.GONE
    remote.preflight(transport, t, "sh")
    assert transport.call(remote.ssh_argv(t, "echo hi"), timeout=30).stdout == "hi\n"
    assert transport.exchange(remote.ssh_argv(t, "printf x"), timeout=30).stdout == b"x"

    assert [op for op, _ in ops(ssh)].count("master") == 1
    assert vias(ssh) == ["master"] * 5
    for call in ssh.calls():
        assert f"ControlPath={socket_of()}" in call["options"]
        assert "ControlMaster=no" in call["options"]


def test_each_alias_and_user_gets_its_own_master(ssh: Ssh, tmp_path: Path) -> None:
    transport = remote.SshTransport()
    claude = target(tmp_path, alias="fakepc-claude-bot", user="claude-bot")
    assert remote.probe_device(transport, target(tmp_path))
    assert remote.probe_device(transport, claude)
    assert remote.probe_device(transport, target(tmp_path))
    masters = [e["path"] for e in control(ssh) if e["op"] == "master"]
    assert masters == [str(socket_of()), str(socket_of("fakepc-claude-bot", "claude-bot"))]
    assert vias(ssh) == ["master"] * 3


def test_a_follower_run_goes_through_one_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = Box(tmp_path, monkeypatch)
    try:
        sink = io.BytesIO()
        end = Follower(box.transport, box.target, sink, log=lambda _: None,
                       sleep=lambda s: time.sleep(min(s, 0.1))).run(
            box.launch(lines=5, pause=0.05), b"the brief", Stop()
        )
    finally:
        box.cleanup()
    assert (end.kind, end.code) == (EXITED, 0)
    assert sink.getvalue().endswith(b"ORCH-HANDOFF: 123456\n")
    assert len(box.ssh.calls()) >= 2
    assert set(vias(box.ssh)) == {"master"}
    assert [op for op, _ in ops(box.ssh)].count("master") == 1


def test_a_dead_master_with_its_socket_left_is_rebuilt_inside_the_call(
    ssh: Ssh, tmp_path: Path
) -> None:
    """A box VM pause or a killed master: the socket stays, nothing answers on it."""
    t = target(tmp_path)
    transport = remote.SshTransport()
    assert remote.probe_device(transport, t)
    Path(f"{socket_of()}.fake-master").unlink()
    assert socket_of().is_socket()
    assert remote.check_worker(transport, t) == remote.GONE
    assert ops(ssh) == [("check", 255), ("master", 0), ("check", 255), ("master", 0)]
    assert vias(ssh) == ["master", "master"]


def test_a_deleted_socket_is_rebuilt_inside_the_call(ssh: Ssh, tmp_path: Path) -> None:
    t = target(tmp_path)
    transport = remote.SshTransport()
    assert remote.probe_device(transport, t)
    socket_of().unlink()
    result = transport.call(remote.ssh_argv(t, "echo back"), timeout=30)
    assert (result.code, result.stdout) == (0, "back\n")
    assert [op for op, _ in ops(ssh)].count("master") == 2
    assert vias(ssh) == ["master", "master"]


def test_a_stale_socket_left_from_before_a_reboot_is_replaced(ssh: Ssh, tmp_path: Path) -> None:
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_of()))
    stale.close()
    assert remote.probe_device(remote.SshTransport(), target(tmp_path))
    assert ops(ssh) == [("check", 255), ("master", 0)]
    assert vias(ssh) == ["master"]


def test_a_live_master_is_checked_not_rebuilt(ssh: Ssh, tmp_path: Path) -> None:
    t = target(tmp_path)
    transport = remote.SshTransport()
    for _ in range(3):
        assert remote.probe_device(transport, t)
    assert ops(ssh) == [("check", 255), ("master", 0), ("check", 0), ("check", 0)]


@pytest.mark.parametrize("mode", [0o755, 0o710, "symlink"])
def test_an_unsafe_control_dir_runs_plain_logins(
    ssh: Ssh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode
) -> None:
    real = tmp_path / "control"
    real.mkdir(mode=0o700)
    if mode == "symlink":
        unsafe = tmp_path / "link"
        unsafe.symlink_to(real)
    else:
        unsafe = real
        real.chmod(mode)
    monkeypatch.setenv("ORCH_SSH_CONTROL_DIR", str(unsafe))
    assert remote.probe_device(remote.SshTransport(), target(tmp_path))
    assert ops(ssh) == []
    assert vias(ssh) == ["login"]
    assert not any(o.startswith("Control") for o in ssh.calls()[0]["options"])


def test_a_foreign_file_at_the_socket_path_is_never_removed(ssh: Ssh, tmp_path: Path) -> None:
    socket_of().write_text("not a socket", encoding="utf-8")
    assert remote.probe_device(remote.SshTransport(), target(tmp_path))
    assert socket_of().read_text(encoding="utf-8") == "not a socket"
    assert ops(ssh) == [("check", 255)]
    assert vias(ssh) == ["login"]


def test_the_off_switch_runs_todays_argv(
    ssh: Ssh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORCH_SSH_MUX", "off")
    t = target(tmp_path)
    assert remote.probe_device(remote.SshTransport(), t)
    assert ops(ssh) == []
    [call] = ssh.calls()
    assert call["options"] == [o for o in remote.SSH_OPTIONS if o != "-o"]


def test_an_unreachable_device_is_still_255_with_one_master_attempt(
    ssh: Ssh, tmp_path: Path
) -> None:
    ssh.offline(ALIAS).touch()
    t = target(tmp_path)
    transport = remote.SshTransport()
    assert not remote.probe_device(transport, t)
    assert ops(ssh) == [("check", 255), ("master", 255)]
    assert vias(ssh) == ["login"]
    assert remote.check_worker(transport, t) == remote.UNREACHABLE
    with pytest.raises(remote.CheckoutError, match="unreachable"):
        remote.preflight(transport, t, "sh")
    exchanged = transport.exchange(remote.ssh_argv(t, "true"), timeout=30)
    assert exchanged.code == remote.SSH_UNREACHABLE
    assert len(ssh.calls()) == 4  # one data call each, never retried
    assert [op for op, _ in ops(ssh)].count("master") == 4


def test_a_drop_under_a_live_master_is_255_and_resets_the_master(
    ssh: Ssh, tmp_path: Path
) -> None:
    t = target(tmp_path)
    transport = remote.SshTransport()
    assert remote.probe_device(transport, t)
    ssh.offline(ALIAS).touch()
    assert remote.check_worker(transport, t) == remote.UNREACHABLE
    assert vias(ssh) == ["master", "master"]  # the one call, not retried
    assert ops(ssh)[-2:] == [("check", 0), ("stop", 0)]
    assert not socket_of().exists()
    ssh.offline(ALIAS).unlink()
    assert remote.check_worker(transport, t) == remote.GONE
    assert ops(ssh)[-2:] == [("check", 255), ("master", 0)]


def test_a_timeout_through_a_master_is_unreachable_and_resets_it(
    ssh: Ssh, tmp_path: Path
) -> None:
    t = target(tmp_path)
    transport = remote.SshTransport()
    assert remote.probe_device(transport, t)
    exchanged = transport.exchange(remote.ssh_argv(t, "sleep 5"), timeout=1)
    assert exchanged.code == remote.SSH_UNREACHABLE
    assert ops(ssh)[-1] == ("stop", 0)


def test_a_refused_session_falls_back_to_one_login(ssh: Ssh, tmp_path: Path) -> None:
    """sshd's MaxSessions reached on the master: ssh itself logs in plainly."""
    t = target(tmp_path)
    transport = remote.SshTransport()
    assert remote.probe_device(transport, t)
    (ssh.hosts / f"{ALIAS}.mux-refuse").touch()
    result = transport.call(remote.ssh_argv(t, "echo ok"), timeout=30)
    assert (result.code, result.stdout) == (0, "ok\n")
    assert "Session open refused" in result.stderr
    assert vias(ssh) == ["master", "login"]
    ssh.offline(ALIAS).touch()
    assert remote.check_worker(transport, t) == remote.UNREACHABLE
    assert len(ssh.calls()) == 3


def test_prepared_argv_adds_only_the_mux_options_and_names_no_key(
    ssh: Ssh, tmp_path: Path
) -> None:
    t = target(tmp_path)
    argv = remote.ssh_argv(t, remote.PROBE_SCRIPT)
    mux = ssh_mux.Multiplexer(binary=remote.SSH_BINARY, options=remote.SSH_OPTIONS)
    prepared = mux.prepare(argv)
    assert prepared.argv == [
        argv[0], "-o", "ControlMaster=no", "-o", f"ControlPath={socket_of()}", *argv[1:]
    ]
    added = " ".join(prepared.argv[1:5])
    assert not any(word in added for word in ("-i", "-F", ".ssh", "@", "Identity"))
    assert ALIAS not in str(socket_of()) and USER not in str(socket_of())


def test_argv_not_built_by_ssh_argv_runs_untouched(ssh: Ssh, tmp_path: Path) -> None:
    mux = ssh_mux.Multiplexer(binary=remote.SSH_BINARY, options=remote.SSH_OPTIONS)
    for argv in (["ssh", "-l", USER, "--", ALIAS, "true"], ["rsync", "-a", "x", "y"]):
        assert mux.prepare(argv).argv == argv
    assert ops(ssh) == []
