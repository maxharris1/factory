from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from lib import device_monitor, job_run, oom, remote
from lib.process_tree import RUN_TAG_ENV
from tests.test_job_run import LOCAL_BOX_MACHINES, _orch, _queue, _running_job
from tests.test_remote_start import LocalShell, config_of, machines_file

ROOT = Path(__file__).resolve().parents[1]

linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="oom_score_adj is a Linux /proc file"
)


def score(pid: int | str) -> int:
    return int(Path(f"/proc/{pid}/oom_score_adj").read_text())


def at_300() -> None:
    Path("/proc/self/oom_score_adj").write_text("300")


def tagged(name: str, value: str) -> set[int]:
    needle = f"{name}={value}".encode()
    found = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.add(int(entry.name))
    return found


def environ_of(pid: int) -> dict[str, str]:
    pairs = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(pair.decode().partition("=")[::2] for pair in pairs if b"=" in pair)


def cannot_write_from_300(value: int) -> bool:
    probe = subprocess.run(
        [sys.executable, "-c",
         f"open('/proc/self/oom_score_adj', 'w').write('{value}')"],
        preexec_fn=at_300, capture_output=True,
    )
    return probe.returncode != 0


WATCHED = """import os, subprocess, sys, time
from pathlib import Path
here = Path(sys.argv[1])
child = subprocess.Popen(["sleep", "60"])
(here / "pids.tmp").write_text(f"{os.getpid()} {child.pid}")
os.replace(here / "pids.tmp", here / "pids")
deadline = time.monotonic() + 30
while not (here / "done").exists() and time.monotonic() < deadline:
    time.sleep(0.05)
child.kill()
child.wait()
"""


def wait_for(path: Path, seconds: float = 20.0) -> str:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} was written"
        time.sleep(0.05)
    return path.read_text()


@linux
def test_lower_to_floor_reaches_the_kernel_floor() -> None:
    probe = subprocess.run(
        [sys.executable, "-c",
         "from lib import oom; print(oom.lower_to_floor(print)); "
         "print(open('/proc/self/oom_score_adj').read().strip())"],
        preexec_fn=at_300, capture_output=True, text=True, cwd=ROOT,
    )
    assert probe.returncode == 0, probe.stderr
    *_, returned, now = probe.stdout.split()
    assert returned == now
    floor = int(now)
    assert 0 <= floor < 300
    assert floor == 0 or cannot_write_from_300(floor - 1), "nothing lower is allowed"


def test_lower_to_floor_never_raises_a_score_below_0(tmp_path: Path) -> None:
    adj = tmp_path / "oom_score_adj"
    adj.write_text("-500\n")
    logged: list[str] = []
    assert oom.lower_to_floor(logged.append, adj) == -500
    assert adj.read_text() == "-500\n"
    assert logged == []


def test_lower_to_floor_logs_and_starts_anyway_when_the_file_cannot_be_read(
    tmp_path: Path,
) -> None:
    logged: list[str] = []
    assert oom.lower_to_floor(logged.append, tmp_path / "missing") is None
    assert len(logged) == 1 and "oom_score_adj left as is" in logged[0]


@linux
def test_a_gated_worker_and_its_child_run_at_800_and_the_runner_at_its_floor(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    job = _running_job(queue)
    proc = _orch(
        "job", "run", "codex-bot", job.id, "--machines", str(LOCAL_BOX_MACHINES),
        "--db", str(tmp_path / "orch.db"), "--", sys.executable, "-c", WATCHED, str(tmp_path),
        preexec_fn=at_300, start_new_session=True,
    )
    try:
        worker, child = map(int, wait_for(tmp_path / "pids").split())
        tag = environ_of(worker)[RUN_TAG_ENV]
        others = tagged(RUN_TAG_ENV, tag) - {worker, child}
        scores = {"worker": score(worker), "child": score(child), "runner": score(proc.pid)}
        kept = {pid: score(pid) for pid in others}
    finally:
        (tmp_path / "done").touch()
        _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr
    assert scores["worker"] == 800 and scores["child"] == 800, scores
    runner = scores["runner"]
    assert 0 <= runner < 300, scores
    assert runner == 0 or cannot_write_from_300(runner - 1), "the runner is at the kernel floor"
    assert others, "the session keeper is tagged"
    assert set(kept.values()) == {runner}, "the keeper shares the runner's score"


@linux
def test_a_failed_worker_raise_is_logged_and_the_command_still_runs(tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text(
        "import sys\n"
        "from lib import job_run\n"
        "job_run._GATE = job_run._GATE.replace('800', '1001')\n"
        "sys.exit(job_run.run([sys.executable, '-c', sys.argv[1], sys.argv[2]], beat=lambda: None,\n"
        "    interval=60, log=print, started=lambda pid: None))\n"
    )
    before = score("self")
    proc = subprocess.Popen(
        [sys.executable, str(script), WATCHED, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        worker, child = map(int, wait_for(tmp_path / "pids").split())
        tag = environ_of(worker)[RUN_TAG_ENV]
        everyone = tagged(RUN_TAG_ENV, tag) | {proc.pid}
        scores = {pid: score(pid) for pid in everyone}
    finally:
        (tmp_path / "done").touch()
        _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, stderr
    assert "worker oom_score_adj left as is (not 1001)" in stderr
    assert set(scores.values()) == {before}, "no runner, keeper or worker was raised"


@linux
def test_a_remote_worker_and_its_child_run_at_800_and_its_wrappers_do_not(
    tmp_path: Path,
) -> None:
    config = config_of(machines_file(tmp_path))
    tag = uuid.uuid4().hex
    target = device_monitor.remote_target(config.device("pc"), "claude", "job-1", tag)
    assert target is not None
    shell = LocalShell()
    start = remote.start_script(target, [sys.executable, "-c", WATCHED, str(tmp_path)], {})
    launch = remote.launch_script(
        target, start, size=0, not_after=int(time.time()) + 600, budget=120, grace=1
    )
    try:
        result = subprocess.run(
            ["sh", "-c", launch], stdin=subprocess.DEVNULL, capture_output=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        worker, child = map(int, wait_for(tmp_path / "pids").split())
        others = tagged(remote.TAG_ENV, tag) - {worker, child}
        scores = {"worker": score(worker), "child": score(child)}
        wrapped = {pid: score(pid) for pid in others}
    finally:
        (tmp_path / "done").touch()
        remote.stop_worker(shell, target, kill=True)
    assert scores == {"worker": 800, "child": 800}
    assert others, "the launch wrapper is tagged"
    assert 800 not in wrapped.values(), "only the harness is raised"


@linux
def test_a_failed_remote_raise_is_logged_and_the_harness_still_execs() -> None:
    result = subprocess.run(
        ["sh", "-c", f"{oom.raise_sh(1001)}exec cat /proc/self/oom_score_adj"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == score("self")
    assert "worker oom_score_adj left as is (not 1001)" in result.stderr
