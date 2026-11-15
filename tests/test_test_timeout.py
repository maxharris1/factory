"""A per-test timeout fails a hung test and kills the children it left behind (#238)."""

from __future__ import annotations

import builtins
import configparser
import io
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests import child_reaper
from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]

INNER = textwrap.dedent(
    """
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    PIDS = Path(os.environ["REAPER_PIDS"])
    FAKE_SSH = os.environ["REAPER_FAKE_SSH"]


    def record(name, pid):
        with PIDS.open("a") as handle:
            handle.write(f"{name} {pid}\\n")


    def test_hung_with_a_child_in_its_own_session():
        child = subprocess.Popen(["sleep", "600"], start_new_session=True)
        record("session", child.pid)
        child.wait()


    def test_hung_after_its_ssh_client_died_leaving_the_remote_worker():
        pidfile = PIDS.parent / "worker.pid"
        client = subprocess.Popen(
            [sys.executable, FAKE_SSH, "-l", "claude-bot", "--", "fakepc",
             f"echo $$ > {pidfile}; exec sleep 600"],
        )
        while not pidfile.exists() or not pidfile.read_text().strip():
            time.sleep(0.05)
        client.kill()
        client.wait()
        record("worker", int(pidfile.read_text()))
        time.sleep(600)


    def test_passes_and_leaves_a_child_running_on_purpose():
        child = subprocess.Popen(["sleep", "600"], start_new_session=True)
        record("kept", child.pid)
    """
)


def read_pids(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return {name: int(pid) for name, pid in (line.split() for line in path.read_text().splitlines())}


def test_pytest_ini_sets_a_120s_signal_timeout_and_loads_the_reaper() -> None:
    ini = configparser.ConfigParser()
    ini.read(ROOT / "pytest.ini")
    section = ini["pytest"]
    assert section["timeout"] == "120"
    assert section["timeout_method"] == "signal"
    assert "-p tests.child_reaper" in section["addopts"]


def test_ci_installs_pytest_timeout() -> None:
    lines = (ROOT / "requirements-dev.txt").read_text().splitlines()
    assert any(line.startswith("pytest-timeout==") for line in lines)
    import pytest_timeout  # noqa: F401


TIMEOUT_FAILURE = re.compile(r"^E\s+Failed: Timeout \(>2\.0s\) from pytest-timeout\.$", re.MULTILINE)


@pytest.mark.parametrize("ci", [None, "true"], ids=["local", "ci"])
def test_a_hung_test_fails_on_timeout_and_its_children_are_killed(tmp_path: Path, ci: str | None) -> None:
    hosts = tmp_path / "hosts"
    hosts.mkdir()
    (hosts / "fakepc").write_text("claude-bot\n")
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntimeout = 2\ntimeout_method = signal\naddopts = -p tests.child_reaper -p no:cacheprovider\n"
    )
    (tmp_path / "test_inner.py").write_text(INNER)
    pids_file = tmp_path / "pids"
    env = {
        **{key: value for key, value in os.environ.items() if key != "CI"},
        **({"CI": ci} if ci else {}),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
        "REAPER_PIDS": str(pids_file),
        "REAPER_FAKE_SSH": str(ROOT / "tests" / "fake_ssh.py"),
        "FAKE_SSH_HOSTS": str(hosts),
        "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
    }
    began = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-rf", str(tmp_path / "test_inner.py")],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
        )
        elapsed = time.monotonic() - began
        pids = read_pids(pids_file)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "2 failed, 1 passed" in result.stdout, result.stdout
        assert len(TIMEOUT_FAILURE.findall(result.stdout)) == 2, result.stdout
        assert elapsed < 30
        assert set(pids) == {"session", "worker", "kept"}
        deadline = time.monotonic() + 5
        while (alive(pids["session"]) or alive(pids["worker"])) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(pids["session"])
        assert not alive(pids["worker"])
        assert alive(pids["kept"])
    finally:
        for pid in read_pids(pids_file).values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


REVIEW_CASES = {
    "teardown": textwrap.dedent(
        """
        import subprocess
        from pathlib import Path
        import pytest


        @pytest.fixture
        def child():
            p = subprocess.Popen(["sleep", "600"], start_new_session=True)
            Path("child.pid").write_text(str(p.pid))
            yield p
            p.wait()


        def test_hang(child):
            pass
        """
    ),
    "context": textwrap.dedent(
        """
        import subprocess
        from pathlib import Path


        def test_hang():
            with subprocess.Popen(["sleep", "600"], start_new_session=True) as p:
                Path("child.pid").write_text(str(p.pid))
                p.wait()
        """
    ),
    "group": textwrap.dedent(
        """
        import subprocess
        import sys
        import time
        from pathlib import Path

        SERVICE = '''
        import subprocess, time
        from pathlib import Path
        while not Path("go").exists():
            time.sleep(0.01)
        p = subprocess.Popen(["sleep", "600"])
        Path("child.pid").write_text(str(p.pid))
        time.sleep(600)
        '''


        def test_1_service():
            p = subprocess.Popen([sys.executable, "-c", SERVICE], start_new_session=True)
            Path("service.pid").write_text(str(p.pid))


        def test_2_hang():
            Path("go").touch()
            while not Path("child.pid").exists():
                time.sleep(0.01)
            time.sleep(600)
        """
    ),
}

REVIEW_EXPECTED = {
    "teardown": ("1 passed, 1 error", {"child.pid": False}),
    "context": ("1 failed", {"child.pid": False}),
    "group": ("1 failed, 1 passed", {"child.pid": False, "service.pid": True}),
}


@pytest.mark.parametrize("case", sorted(REVIEW_CASES))
def test_a_timeout_in_any_phase_kills_only_the_tests_own_children(tmp_path: Path, case: str) -> None:
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntimeout = 1\ntimeout_method = signal\naddopts = -p tests.child_reaper -p no:cacheprovider\n"
    )
    (tmp_path / "test_probe.py").write_text(REVIEW_CASES[case])
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
    }
    summary, expected_alive = REVIEW_EXPECTED[case]
    probe = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        try:
            output, _ = probe.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{case}: pytest still running 15s after a 1s timeout")
        assert summary in output, output
        assert "Timeout (>1.0s) from pytest-timeout" in output, output
        assert {path.name for path in tmp_path.glob("*.pid")} == set(expected_alive)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            alive(int((tmp_path / name).read_text())) != want for name, want in expected_alive.items()
        ):
            time.sleep(0.05)
        assert {name: alive(int((tmp_path / name).read_text())) for name in expected_alive} == expected_alive
    finally:
        for pidfile in tmp_path.glob("*.pid"):
            try:
                os.killpg(os.getpgid(int(pidfile.read_text())), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if probe.poll() is None:
            probe.kill()
        probe.communicate(timeout=5)


@pytest.mark.skipif(not Path("/proc/self/task").is_dir(), reason="the orphan reaper runs only on Linux")
def test_the_orphan_reaper_reads_proc_past_a_tests_open_and_path_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("a test patched file reads")

    child = subprocess.Popen(["sleep", "5"])
    try:
        for target, name in ((builtins, "open"), (io, "open"), (os, "open"), (os, "read"), (os, "listdir"),
                             (Path, "open"), (Path, "read_text"), (Path, "read_bytes"), (Path, "iterdir")):
            monkeypatch.setattr(target, name, refuse)
        assert child.pid in child_reaper._children()
        assert not child_reaper._zombie(child.pid)
        child_reaper.reap_orphans()
    finally:
        monkeypatch.undo()
        child.kill()
        child.wait()
