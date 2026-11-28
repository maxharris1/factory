import os
import pty
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import suite_slot

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "bin" / "suite-slot"

HOLD = (
    "import pathlib, sys, time\n"
    "pathlib.Path(sys.argv[1]).touch()\n"
    "deadline = time.monotonic() + 30\n"
    "while not pathlib.Path(sys.argv[2]).exists() and time.monotonic() < deadline:\n"
    "    time.sleep(0.02)\n"
)

STAMP = (
    "import pathlib, sys, time\n"
    "start = time.time()\n"
    "time.sleep(0.5)\n"
    "pathlib.Path(sys.argv[1]).write_text(f'{start} {time.time()}')\n"
)


def launch(command, slot_dir, *, wait=5.0, limit=None):
    env = {k: v for k, v in os.environ.items() if k != suite_slot.LIMIT_ENV}
    if limit is not None:
        env[suite_slot.LIMIT_ENV] = limit
    code = (
        "import sys\n"
        "from lib.suite_slot import main\n"
        f"raise SystemExit(main(sys.argv[1:], slot_dir={str(slot_dir)!r}, wait_seconds={wait!r}))\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code, *command],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def wait_for(path, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.02)


def holder(tmp_path, name, slot_dir, **kw):
    started, release = tmp_path / f"{name}.started", tmp_path / f"{name}.release"
    proc = launch([sys.executable, "-c", HOLD, str(started), str(release)], slot_dir, **kw)
    wait_for(started)
    return proc, release


def finish(proc):
    out, err = proc.communicate(timeout=30)
    return proc.returncode, out, err


def assert_busy(tmp_path, slot_dir, **kw):
    marker = tmp_path / "busy-ran"
    code, out, err = finish(launch([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], slot_dir, wait=0.3, **kw))
    assert code == suite_slot.BUSY_EXIT == 75
    assert not marker.exists()
    assert out.splitlines() == [out.strip()] and out.startswith("ORCH-SUITE-SLOT: waited ")
    assert out.strip().endswith("s, busy")
    assert "busy" in err and "targeted tests" in err and "CI" in err


def test_one_slot_blocks_a_second_helper_until_the_holder_exits(tmp_path):
    first, release = holder(tmp_path, "first", tmp_path)
    assert_busy(tmp_path, tmp_path)
    release.touch()
    assert finish(first)[0] == 0
    code, out, _ = finish(launch([sys.executable, "-c", "print('after')"], tmp_path))
    assert (code, out) == (0, "ORCH-SUITE-SLOT: waited 0s, running\nafter\n")


def test_two_slots_admit_two_helpers_and_turn_away_a_third(tmp_path):
    first, release_first = holder(tmp_path, "first", tmp_path, limit="2")
    second, release_second = holder(tmp_path, "second", tmp_path, limit="2")
    assert_busy(tmp_path, tmp_path, limit="2")
    release_first.touch()
    release_second.touch()
    assert finish(first)[0] == finish(second)[0] == 0
    assert sorted(p.name for p in tmp_path.glob("orch-suite-slot-*.lock")) == [
        "orch-suite-slot-0.lock", "orch-suite-slot-1.lock",
    ]


def test_a_holder_killed_with_sigkill_frees_its_slot(tmp_path):
    first, _ = holder(tmp_path, "first", tmp_path)
    os.kill(first.pid, signal.SIGKILL)
    first.wait(timeout=10)
    code, out, _ = finish(launch([sys.executable, "-c", "print('next')"], tmp_path, wait=2.0))
    assert (code, out) == (0, "ORCH-SUITE-SLOT: waited 0s, running\nnext\n")


def test_a_slot_file_that_already_exists_read_only_is_locked_not_recreated(tmp_path):
    slot = tmp_path / "orch-suite-slot-0.lock"
    fd = os.open(slot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
    os.close(fd)
    inode = slot.stat().st_ino
    first, release = holder(tmp_path, "first", tmp_path)
    assert_busy(tmp_path, tmp_path)
    release.touch()
    assert finish(first)[0] == 0
    assert slot.stat().st_ino == inode


def test_a_slot_file_the_helper_creates_is_world_readable_and_read_only(tmp_path):
    old = os.umask(0o077)
    try:
        assert finish(launch(["true"], tmp_path))[0] == 0
    finally:
        os.umask(old)
    assert (tmp_path / "orch-suite-slot-0.lock").stat().st_mode & 0o777 == 0o444


def test_two_helpers_started_together_with_one_slot_run_one_after_the_other(tmp_path):
    stamps = [tmp_path / "a.stamp", tmp_path / "b.stamp"]
    procs = [launch([sys.executable, "-c", STAMP, str(s)], tmp_path) for s in stamps]
    assert [finish(p)[0] for p in procs] == [0, 0]
    (a_start, a_end), (b_start, b_end) = sorted(
        tuple(map(float, s.read_text().split())) for s in stamps
    )
    assert a_end <= b_start


def test_an_unopenable_slot_file_fails_closed_with_the_busy_exit(tmp_path):
    assert_busy(tmp_path, tmp_path / "missing")


@pytest.mark.parametrize(("value", "limit"), [
    (None, 1), ("", 1), ("abc", 1), ("0", 1), ("-3", 1), ("2.5", 1), ("1", 1), ("3", 3),
])
def test_the_limit_comes_from_orch_test_suite_max(value, limit):
    environ = {} if value is None else {suite_slot.LIMIT_ENV: value}
    assert suite_slot.LIMIT_ENV == "ORCH_TEST_SUITE_MAX"
    assert suite_slot.slot_limit(environ) == limit


def test_an_invalid_limit_holds_the_helper_to_one_slot(tmp_path):
    first, release = holder(tmp_path, "first", tmp_path, limit="many")
    assert_busy(tmp_path, tmp_path, limit="many")
    release.touch()
    assert finish(first)[0] == 0


def test_the_executable_with_no_command_exits_2_under_python_s():
    proc = subprocess.run([sys.executable, "-S", str(HELPER)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "usage" in proc.stderr


def test_waited_reads_the_line_a_real_helper_run_prints(tmp_path):
    code, out, _ = finish(launch([sys.executable, "-c", "print('suite output')"], tmp_path))
    assert code == 0
    assert out.splitlines()[0] == "ORCH-SUITE-SLOT: waited 0s, running"
    assert suite_slot.waited("bin/suite-slot python -m pytest -q", out) == 0
    assert suite_slot.waited("python -m pytest -q", out) is None
    assert suite_slot.waited("bin/suite-slot pytest", "no slot line here") is None


def test_waited_reads_a_busy_line_and_clamps_to_the_wait_bound():
    assert suite_slot.waited("suite-slot pytest", "ORCH-SUITE-SLOT: waited 12s, busy\n") == 12
    assert suite_slot.waited("/repo/bin/suite-slot pytest", "x\nORCH-SUITE-SLOT: waited 999s, running\n") == suite_slot.WAIT_SECONDS
    assert suite_slot.waited("bin/suite-slotter pytest", "ORCH-SUITE-SLOT: waited 3s, running\n") is None


def run_in_pty(command, slot_dir):
    env = {k: v for k, v in os.environ.items() if k != suite_slot.LIMIT_ENV}
    code = (
        "import sys\n"
        "from lib.suite_slot import main\n"
        f"raise SystemExit(main(sys.argv[1:], slot_dir={str(slot_dir)!r}))\n"
    )
    leader, follower = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", code, *command], cwd=ROOT, env=env, stdout=follower, stderr=follower,
        )
        os.close(follower)
        chunks = []
        while True:
            try:
                chunk = os.read(leader, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        assert proc.wait(timeout=30) == 0
    finally:
        os.close(leader)
    return b"".join(chunks).decode()


def test_waited_reads_the_line_a_helper_run_on_a_terminal_prints(tmp_path):
    out = run_in_pty([sys.executable, "-c", "print(1)"], tmp_path)
    assert out.startswith("ORCH-SUITE-SLOT: waited 0s, running\r\n")
    assert suite_slot.waited("bin/suite-slot python3 -c print(1)", out) == 0


@pytest.mark.parametrize("state", ["running", "busy"])
def test_waited_reads_a_crlf_terminated_line(state):
    assert suite_slot.waited("bin/suite-slot pytest", f"ORCH-SUITE-SLOT: waited 90s, {state}\r\n") == 90


@pytest.mark.parametrize("command", [
    "/bin/bash -lc 'suite-slot pytest -q'",
    "/bin/zsh -lc 'bin/suite-slot pytest -q'",
    '/bin/bash -lc "suite-slot pytest -q"',
    shlex.join(["/bin/bash", "-lc", "bin/suite-slot pytest -q"]),
    '"/home/bot/my repo/bin/suite-slot" pytest',
    shlex.join(["/home/bot/my repo/bin/suite-slot", "pytest"]),
    "cd repo && bin/suite-slot pytest",
    "cd repo;suite-slot pytest",
    "(suite-slot pytest)",
    "/bin/bash -lc 'cd repo && suite-slot'",
])
def test_waited_reads_any_shell_call_that_names_the_helper(command):
    assert suite_slot.waited(command, "ORCH-SUITE-SLOT: waited 12s, running\n") == 12


@pytest.mark.parametrize("command", [
    "bin/suite-slotter pytest",
    "bin/my-suite-slot pytest",
    "/bin/bash -lc 'suite-slot.py pytest'",
    "pytest -q",
])
def test_waited_ignores_calls_that_do_not_name_the_helper(command):
    assert suite_slot.waited(command, "ORCH-SUITE-SLOT: waited 12s, running\n") is None
