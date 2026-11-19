from __future__ import annotations

import fcntl
import json
import os
import random
import re
import shutil
import signal
import subprocess
import time
from itertools import pairwise, takewhile
from pathlib import Path

import pytest

from lib.factory_md import load_factory_md
from tests.procs import alive

ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "deploy/runner-pool-sync.sh"
STATE = Path(".local/state/factory")
HOLD = Path(".config/factory/runner-pool-sync-hold")

needs_proc = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="finds loop processes in /proc"
)
needs_proc_locks = pytest.mark.skipif(
    not Path("/proc/locks").exists(), reason="sees lock waiters in /proc/locks"
)


def sync_loops(home: Path) -> list[int]:
    script, env_home = str(SYNC_SCRIPT), f"HOME={home}".encode()
    parents = {}
    for entry in Path("/proc").glob("[1-9]*"):
        try:
            argv = (entry / "cmdline").read_bytes().decode().split("\0")
            environ = (entry / "environ").read_bytes().split(b"\0")
            ppid = int((entry / "stat").read_text().rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if script in argv and "loop" in argv and env_home in environ and alive(int(entry.name)):
            parents[int(entry.name)] = ppid
    return sorted(pid for pid, ppid in parents.items() if ppid not in parents)


def _locked(home: Path) -> bool:
    return subprocess.run(["flock", "-n", str(home / STATE / "runner-pool-sync.lock"), "true"]).returncode != 0


def _wait_for(predicate, what: str, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while not (value := predicate()):
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)
    return value


def _lock_waiters(lock: Path) -> int:
    inode = f":{lock.stat().st_ino}"
    return sum(
        "->" in line and line.split()[-3].endswith(inode)
        for line in Path("/proc/locks").read_text().splitlines()
    )


def _kill_loops(home: Path) -> None:
    for pid in sync_loops(home):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def runners(online: int = 0, busy: list[bool] | None = None, offline: int = 0, mac: bool = True) -> str:
    busy = busy if busy is not None else [False] * online
    assert len(busy) == online
    linux = [{"id": 1, "name": "self-hosted", "type": "read-only"}, {"id": 2, "name": "Linux", "type": "read-only"},
             {"id": 3, "name": "X64", "type": "read-only"}, {"id": 4, "name": "factory-linux", "type": "custom"}]
    macos = [{"id": 1, "name": "self-hosted", "type": "read-only"}, {"id": 5, "name": "macOS", "type": "read-only"},
             {"id": 6, "name": "ARM64", "type": "read-only"}, {"id": 7, "name": "factory-mac", "type": "custom"}]
    pool = [("online", b) for b in busy] + [("offline", False)] * offline
    listed = [
        {"id": 10 + i, "name": f"pc-runner-{i}", "os": "Linux", "status": status, "busy": b, "labels": linux}
        for i, (status, b) in enumerate(pool)
    ]
    if mac:
        listed.append({"id": 99, "name": "mac-runner", "os": "macOS", "status": "online", "busy": True, "labels": macos})
    return json.dumps({"total_count": len(listed), "runners": listed})


def test_the_sync_script_publishes_the_online_count_of_the_factory_md_pool() -> None:
    config, findings = load_factory_md(ROOT / "factory.md")
    assert config is not None, findings
    text = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert "VAR=FACTORY_LINUX_POOL" in text
    runner_list = re.search(r"actions/runners\?per_page=100\" --jq '([^']*)'", text)
    assert runner_list, "the loop no longer filters the runner list with --jq"
    assert f'.name=="{config.ci_runner}"' in runner_list.group(1)
    assert ".busy" not in text


FAKE_SYNC_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
case "$*" in
  *actions/runners*)
    n=$(grep -c 'actions/runners' "$GH_LOG")
    line=$(sed -n "${n}p" "$GH_RESPONSES")
    if [[ -z "$line" ]]; then touch "$HOME/.config/factory/runner-pool-sync-hold"; exit 1; fi
    [[ "$line" == FAIL ]] && exit 1
    filter=.
    for ((i = 1; i < $#; i++)); do [[ ${!i} == --jq ]] && { j=$((i + 1)); filter=${!j}; }; done
    jq -r "$filter" <<<"$line" ;;
  *" -X PATCH "* | *" -X POST "*)
    for arg; do [[ $arg == value=* ]] && printf '%s' "${arg#value=}" > "$GH_VALUE"; done ;;
  *actions/variables/FACTORY_LINUX_POOL*)
    if [[ ! -s "$GH_VALUE" ]]; then echo '{"message":"Not Found","status":"404"}'; exit 1; fi
    if [[ "$*" == *"--jq .value" ]]; then cat "$GH_VALUE"; else jq -n --rawfile v "$GH_VALUE" '{name:"FACTORY_LINUX_POOL",value:$v}'; fi ;;
esac
"""

STEADY_SYNC_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
case "$*" in
  *actions/runners*) echo 3 ;;
  *"--jq .value") cat "$GH_VALUE" ;;
esac
"""

START = '{"online":3}'
WRITE = re.compile(
    r"api -X (?:PATCH repos/maxharris1/factory/actions/variables/FACTORY_LINUX_POOL"
    r"|POST repos/maxharris1/factory/actions/variables) -f name=FACTORY_LINUX_POOL -f value=(.*)"
)


def _sync_env(tmp_path: Path, start: str, responses: list[str]) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_SYNC_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    (tmp_path / ".config/factory").mkdir(parents=True)
    (tmp_path / "responses").write_text("".join(f"{line}\n" for line in responses), encoding="utf-8")
    (tmp_path / "value").write_text(start, encoding="utf-8")
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "EVERY": "0",
        "GH_LOG": str(log),
        "GH_RESPONSES": str(tmp_path / "responses"),
        "GH_VALUE": str(tmp_path / "value"),
        "LOG": str(tmp_path / "sync.log"),
    }
    return env


def _steady_env(tmp_path: Path, every: str) -> dict[str, str]:
    env = _sync_env(tmp_path, START, [])
    (tmp_path / "bin/gh").write_text(STEADY_SYNC_GH, encoding="utf-8")
    return env | {"EVERY": every}


def _writes(env: dict[str, str]) -> list[str]:
    return [m.group(1) for call in _gh_calls(env) if (m := WRITE.fullmatch(call))]


def _run_sync(tmp_path: Path, start: str, responses: list[str]) -> dict[str, str]:
    env = _sync_env(tmp_path, start, responses)
    result = subprocess.run(
        ["bash", str(SYNC_SCRIPT), "loop"], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return env


@pytest.mark.parametrize(
    ("start", "responses", "writes"),
    [
        (START, [runners(3, [True, True, True]), runners(3, [True, False, True]), runners(3, [True, True, True])], []),
        (START, [runners(3, [True, True, True]), runners(3, [True, False, True]), runners(3, [False, False, True]),
                 runners(3, [True, True, True]), runners(3, [True, True, True])], []),
        (START, [runners(3), runners(2, offline=1), runners(3)], []),
        (START, [runners(3), runners(2, offline=1), runners(2, [True, False], offline=1)], ['{"online":2}']),
        (START, [runners(3), runners(2), runners(2, [True, True]), runners(2, [False, True])], ['{"online":2}']),
        (START, [runners(0, offline=3), runners(0, offline=3, mac=False)], ['{"online":0}']),
        ("", [runners(3, [True, False, False], offline=1)], [START]),
        (START, ["FAIL", "FAIL"], []),
        ("", ["FAIL"], []),
        (START, [runners(3), runners(2), "FAIL", runners(2)], []),
        (START, [runners(3), runners(2), "FAIL", runners(2), runners(2)], ['{"online":2}']),
    ],
    ids=["busy-idle-busy-stays", "held-busy-and-idle-stay", "one-check-dip-stays", "held-drop-writes-once", "longer-hold-writes-once",
         "none-online", "unset-writes-at-once", "failed-list-writes-nothing", "failed-list-leaves-unset",
         "failed-list-breaks-the-hold", "hold-after-a-failed-list"],
)
def test_the_sync_loop_writes_the_online_count_only_when_it_holds_for_two_checks(
    tmp_path: Path, start: str, responses: list[str], writes: list[str]
) -> None:
    assert _writes(_run_sync(tmp_path, start, responses)) == writes


def test_an_unset_variable_is_created_on_the_first_check(tmp_path: Path) -> None:
    env = _run_sync(tmp_path, "", [runners(3)])
    assert [call for call in _gh_calls(env) if " -X " in call] == [
        f"api -X POST repos/maxharris1/factory/actions/variables -f name=FACTORY_LINUX_POOL -f value={START}"
    ]
    assert Path(env["GH_VALUE"]).read_text(encoding="utf-8") == START


@pytest.mark.parametrize("seed", [1, 7, 42, 1000])
def test_a_random_trace_writes_only_the_sustained_online_counts(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    counts: list[int | None] = []
    responses = []
    for _ in range(100):
        if rng.random() < 0.1:
            counts.append(None)
            responses.append("FAIL")
            continue
        online = rng.randint(0, 3)
        counts.append(online)
        responses.append(runners(online, [rng.random() < 0.5 for _ in range(online)],
                                 offline=rng.randint(0, 2), mac=rng.random() < 0.5))
    held = [b for a, b in pairwise(counts) if b is not None and a == b]
    sustained = [f'{{"online":{v}}}' for before, v in pairwise([3, *held]) if v != before]
    assert sustained, "the seed never changed the sustained count"
    writes = _writes(_run_sync(tmp_path, START, responses))
    assert len(writes) <= len(sustained)
    assert writes[-1:] == sustained[-1:]
    assert writes == sustained


def _gh_calls(env: dict[str, str]) -> list[str]:
    return Path(env["GH_LOG"]).read_text(encoding="utf-8").splitlines()


def _runner_reads(env: dict[str, str]) -> list[str]:
    return [line for line in _gh_calls(env) if "actions/runners" in line]


def _ensure(env: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(SYNC_SCRIPT), "ensure"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_proc
def test_a_second_ensure_leaves_the_first_ensures_loop_as_the_only_one(tmp_path: Path) -> None:
    env = _steady_env(tmp_path, "600")
    try:
        _ensure(env)
        _wait_for(lambda: _runner_reads(env) and len(sync_loops(tmp_path)) == 1, "the loop's first check")
        first = sync_loops(tmp_path)
        _ensure(env)
        assert sync_loops(tmp_path) == first
        assert _locked(tmp_path)
        assert len(_runner_reads(env)) == 1
    finally:
        _kill_loops(tmp_path)


@needs_proc
def test_the_hold_file_keeps_ensure_from_starting_a_loop(tmp_path: Path) -> None:
    env = _steady_env(tmp_path, "600")
    (tmp_path / HOLD).touch()
    try:
        _ensure(env)
        assert sync_loops(tmp_path) == []
        assert not _locked(tmp_path)
        assert _gh_calls(env) == []
    finally:
        _kill_loops(tmp_path)


def test_touching_the_hold_file_stops_a_running_loop_at_its_next_check(tmp_path: Path) -> None:
    env = _steady_env(tmp_path, "0.05")
    loop = subprocess.Popen(
        ["bash", str(SYNC_SCRIPT), "loop"], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(lambda: len(_runner_reads(env)) >= 2, "the loop to check twice")
        (tmp_path / HOLD).touch()
        assert loop.wait(timeout=10) == 0
        assert not _locked(tmp_path)
    finally:
        try:
            os.killpg(loop.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _header(text: str) -> str:
    return " ".join(line.lstrip("#").strip() for line in takewhile(lambda line: line.startswith("#"), text.splitlines()))


def test_the_header_and_usage_say_the_sync_runs_on_the_pc_as_factory_from_its_crontab(
    tmp_path: Path,
) -> None:
    env = _steady_env(tmp_path, "600")
    shown = subprocess.run(["bash", str(SYNC_SCRIPT), "--help"], env=env, capture_output=True, text=True, timeout=30)
    assert shown.returncode == 0, shown.stderr
    for text in (_header(SYNC_SCRIPT.read_text(encoding="utf-8")), " ".join(shown.stdout.split())):
        assert "orch host (the PC)" in text
        assert "the `factory` user" in text
        assert "crontab" in text
        assert "ensure" in text
        assert (
            "publishes the online factory-linux runner count as the repo actions variable factory_linux_pool"
            " ({\"online\":n}), writing only when a changed count holds for two checks in a row"
        ) in text.lower()
    assert "loop" in shown.stdout
    unknown = subprocess.run(["bash", str(SYNC_SCRIPT), "start"], env=env, capture_output=True, text=True, timeout=30)
    assert unknown.returncode != 0
    assert unknown.stderr == shown.stdout
    assert _gh_calls(env) == []


def test_ensure_starts_the_loop_when_an_old_pidfile_names_an_unrelated_live_process(
    tmp_path: Path,
) -> None:
    env = _sync_env(tmp_path, START, [runners(3)])
    state = tmp_path / ".local/state/factory"
    state.mkdir(parents=True)
    hold = tmp_path / ".config/factory/runner-pool-sync-hold"
    with subprocess.Popen(["sleep", "30"]) as reused:
        try:
            (state / "runner-pool-sync.pid").write_text(f"{reused.pid}\n", encoding="utf-8")
            _ensure(env)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                hold.exists()
                and subprocess.run(["flock", "-n", str(state / "runner-pool-sync.lock"), "true"]).returncode == 0
            ):
                time.sleep(0.05)
        finally:
            reused.kill()
    assert _runner_reads(env), "ensure left the overflow loop stopped"


def test_a_running_loop_keeps_ensure_and_a_second_loop_from_starting(tmp_path: Path) -> None:
    env = _sync_env(tmp_path, START, [runners(3)])
    lock = tmp_path / ".local/state/factory/runner-pool-sync.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _ensure(env)
        time.sleep(0.5)
        second = subprocess.run(["bash", str(SYNC_SCRIPT), "loop"], env=env, timeout=30)
    assert second.returncode == 0
    assert _runner_reads(env) == []


@needs_proc_locks
def test_a_loop_outwaits_the_lock_probe_ensure_runs_while_it_starts(tmp_path: Path) -> None:
    env = _sync_env(tmp_path, START, [runners(3)])
    lock = tmp_path / ".local/state/factory/runner-pool-sync.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX)
        loop = subprocess.Popen(["bash", str(SYNC_SCRIPT), "loop"], env=env)
        _wait_for(lambda: _lock_waiters(lock) or loop.poll() is not None, "the loop to wait on the lock or exit")
    assert loop.wait(timeout=30) == 0
    assert _runner_reads(env), "the loop gave up on a lock that ensure held for a moment"


def test_the_checklist_states_how_long_a_second_loop_waits_for_the_lock() -> None:
    wait = re.search(r"^flock -w (\d+) 9 \|\| exit 0$", SYNC_SCRIPT.read_text(encoding="utf-8"), re.M)
    assert wait, "the loop no longer takes its lock with a bounded wait"
    text = " ".join((ROOT / "docs/F2-03-RUNNER-CHECKLIST.md").read_text(encoding="utf-8").split())
    assert f"A second loop waits up to {wait.group(1)} seconds for the lock" in text
    assert "exits at once" not in text


@pytest.mark.parametrize("doc", ["docs/F2-03-RUNNER-CHECKLIST.md", "docs/decisions/711-self-hosted-runners.md"])
def test_the_runner_docs_say_the_loop_publishes_a_held_online_count(doc: str) -> None:
    text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
    assert "`FACTORY_LINUX_POOL`" in text
    assert '`{"online":N}`' in text
    assert (
        "writes `FACTORY_LINUX_POOL` only when the online count changes and the new count holds for two checks in a row"
        in text
    )
    assert "A busy or idle change never writes it" in text
    assert "What flips it" not in text
    assert "flips `FACTORY_LINUX_RUNNER`" not in text


STALLING_SYNC_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
if [[ "$*" == *"$STALL"* && ! -e "$STALLED" ]]; then touch "$STALLED"; exec sleep 30; fi
case "$*" in
  *actions/runners*) touch "$HOME/.config/factory/runner-pool-sync-hold"; echo 3 ;;
  *"--jq .value") echo '{"online":3}' ;;
esac
"""


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
@pytest.mark.parametrize("stall", ["actions/runners", "--jq .value"], ids=["runner-list", "variable-read"])
def test_a_loop_killed_during_a_stalled_request_frees_the_lock_and_ensure_restarts_it(
    tmp_path: Path, stall: str
) -> None:
    env = _sync_env(tmp_path, START, [])
    (tmp_path / "bin/gh").write_text(STALLING_SYNC_GH, encoding="utf-8")
    stalled = tmp_path / "stalled"
    env |= {"STALL": stall, "STALLED": str(stalled)}
    lock = tmp_path / ".local/state/factory/runner-pool-sync.lock"
    loop = subprocess.Popen(
        ["bash", str(SYNC_SCRIPT), "loop"], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not stalled.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stalled.exists(), "the loop never made the stalling request"
        reads_before = len(_runner_reads(env))
        loop.kill()
        loop.wait()
        time.sleep(0.1)
        assert subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0, "a request child kept the lock"
        _ensure(env)
        deadline = time.monotonic() + 5
        while len(_runner_reads(env)) <= reads_before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(_runner_reads(env)) > reads_before, "ensure left the overflow loop stopped"
    finally:
        try:
            os.killpg(loop.pid, 9)
        except ProcessLookupError:
            pass
