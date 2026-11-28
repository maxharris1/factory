from __future__ import annotations

import ast
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import codex_app_server

SOURCE = Path(codex_app_server.__file__).read_text(encoding="utf-8")
FIXTURES = Path(__file__).parent / "fixtures" / "usage"
ANSWER = FIXTURES / "codex-0.172.0-app-server-rate-limits.jsonl"
SIGNED_OUT = FIXTURES / "codex-0.172.0-app-server-signed-out.jsonl"
MARKER = "MARKER-1873-must-not-reach-stdout"
STDLIB = {"__future__", "json", "math", "os", "select", "signal", "subprocess", "sys", "time"}

FAKE_CODEX = r'''
import json
import os
import subprocess
import sys

log = open(os.environ["FAKE_LOG"], "a")
mode = os.environ["FAKE_MODE"]
marker = os.environ["FAKE_MARKER"]
fixture = os.environ.get("FAKE_FIXTURE")
lines = [json.loads(line) for line in open(fixture)] if fixture else []


def note(entry):
    log.write(json.dumps(entry) + "\n")
    log.flush()


def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def answer(line, ident):
    return {**line, "id": ident} if "id" in line else line


note({"argv": sys.argv[1:]})
sys.stderr.write(marker + "\n")
sys.stderr.flush()
if mode == "silent":
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stderr=subprocess.DEVNULL)
    note({"pids": [os.getpid(), sleeper.pid]})

for raw in sys.stdin:
    message = json.loads(raw)
    note(message)
    method = message.get("method")
    if mode == "silent":
        continue
    if method == "initialize":
        if mode == "exit":
            sys.exit(0)
        send(answer(lines[0], message["id"]))
        if mode == "flood":
            for _ in range(300):
                send({"method": "flood", "params": {"pad": "a" * 1024}})
    elif method == "account/rateLimits/read":
        rest = lines[1:]
        if mode == "answer":
            rest[0]["params"]["note"] = marker
            rest[1]["result"]["note"] = marker
            rest[1]["result"]["rateLimits"]["primary"]["note"] = marker
            send({"id": message["id"], "method": "account/chatgptAuthTokens/refresh", "params": {"note": marker}})
        if mode == "empty":
            rest[-1]["result"]["rateLimits"]["primary"] = None
            rest[-1]["result"]["rateLimits"]["secondary"] = None
        if mode == "huge":
            sys.stdout.write('{"id": %d, "result": %s}\n' % (message["id"], "[" * 50000 + "]" * 50000))
            rest[-1]["result"]["rateLimits"]["primary"]["usedPercent"] = 10 ** 400
        for line in rest:
            send(answer(line, message["id"]))
'''


def fake_bin(tmp_path: Path, *, install: bool = True) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    if install:
        codex = folder / "codex"
        codex.write_text(f"#!{sys.executable}\n{FAKE_CODEX}", encoding="utf-8")
        codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    return folder


def drive(tmp_path: Path, mode: str, *args: str, fixture: Path | None = ANSWER,
          install: bool = True) -> subprocess.CompletedProcess:
    env = {
        "PATH": str(fake_bin(tmp_path, install=install)),
        "FAKE_LOG": str(tmp_path / "log.jsonl"),
        "FAKE_MODE": mode,
        "FAKE_MARKER": MARKER,
    }
    if fixture is not None:
        env["FAKE_FIXTURE"] = str(fixture)
    return subprocess.run([sys.executable, "-I", "-S", "-c", SOURCE, *args], capture_output=True,
                          env=env, cwd=tmp_path, timeout=30)


def logged(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()]


def printed(run: subprocess.CompletedProcess) -> dict:
    lines = run.stdout.decode().splitlines()
    assert len(lines) == 1 and run.stdout.endswith(b"\n"), run.stdout
    return json.loads(lines[0])


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return True


def test_the_driver_asks_only_for_rate_limits_and_prints_numbers_only(tmp_path: Path) -> None:
    run = drive(tmp_path, "answer")

    entries = logged(tmp_path)
    assert entries[0] == {"argv": ["app-server"]}
    requests = entries[1:]
    assert [entry["method"] for entry in requests] == ["initialize", "initialized", "account/rateLimits/read"]
    assert requests[2]["params"] == {"excludeResetCreditDetails": True}
    expected = {"rateLimits": {
        "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1795850000},
        "secondary": {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 1796400000},
    }}
    assert run.returncode == 0
    assert run.stdout == (json.dumps(expected) + "\n").encode()
    reply = json.loads(ANSWER.read_text(encoding="utf-8").splitlines()[2])["result"]["rateLimits"]
    for name, window in printed(run)["rateLimits"].items():
        assert window.items() <= reply[name].items()
    assert MARKER.encode() not in run.stdout
    assert MARKER.encode() in run.stderr


def test_a_server_that_gives_no_answer_is_killed_at_the_deadline(tmp_path: Path) -> None:
    started = time.monotonic()
    run = drive(tmp_path, "silent", "1", fixture=None)
    elapsed = time.monotonic() - started

    assert run.stdout == b'{"rateLimits": null, "why": "timed out"}\n'
    assert elapsed < 5
    [pids] = [entry["pids"] for entry in logged(tmp_path) if "pids" in entry]
    give_up = time.monotonic() + 3
    while not all(map(gone, pids)) and time.monotonic() < give_up:
        time.sleep(0.05)
    assert all(map(gone, pids)), pids


def test_an_oversized_number_or_nesting_is_dropped_not_fatal(tmp_path: Path) -> None:
    run = drive(tmp_path, "huge")

    assert run.returncode == 0
    assert printed(run) == {"rateLimits": {
        "primary": {"windowDurationMins": 300, "resetsAt": 1795850000},
        "secondary": {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 1796400000},
    }}


def test_a_refused_read_prints_the_fixed_word_and_the_code(tmp_path: Path) -> None:
    run = drive(tmp_path, "refused", fixture=SIGNED_OUT)

    assert run.returncode == 0
    assert printed(run) == {"rateLimits": None, "why": "refused", "code": -32600}


@pytest.mark.parametrize(("mode", "why"), [
    ("missing", "no codex"),
    ("exit", "exited"),
    ("flood", "too much output"),
    ("empty", "no rate limits"),
])
def test_every_other_failure_prints_its_fixed_word(tmp_path: Path, mode: str, why: str) -> None:
    run = drive(tmp_path, mode, install=mode != "missing")

    assert run.returncode == 0
    assert printed(run) == {"rateLimits": None, "why": why}
    assert why in codex_app_server.WHY


def test_the_driver_is_stdlib_only_and_opens_no_file() -> None:
    tree = ast.parse(SOURCE)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= STDLIB, imported - STDLIB
    assert "threading" not in imported
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            assert name not in ("open", "fdopen"), ast.unparse(node)


def _live() -> bool:
    import conftest

    flag = conftest._live_state_env.get("ORCH_LIVE_USAGE", os.environ.get("ORCH_LIVE_USAGE"))
    return flag == "1" and shutil.which("codex") is not None


@pytest.mark.skipif(not _live(), reason="set ORCH_LIVE_USAGE=1 with codex on PATH to ask the real server")
def test_the_live_server_answers_one_line_the_contract_allows(tmp_path: Path) -> None:
    run = subprocess.run([sys.executable, "-I", "-S", "-c", SOURCE], capture_output=True,
                         env=dict(os.environ), cwd=tmp_path, timeout=30)

    assert run.returncode == 0
    outcome = printed(run)
    limits = outcome["rateLimits"]
    if limits is None:
        assert outcome["why"] in codex_app_server.WHY
        assert set(outcome) == ({"rateLimits", "why", "code"} if "code" in outcome else {"rateLimits", "why"})
        assert "code" not in outcome or (outcome["why"] == "refused" and type(outcome["code"]) is int)
    else:
        assert set(outcome) == {"rateLimits"} and limits and set(limits) <= {"primary", "secondary"}
        for window in limits.values():
            assert window and set(window) <= set(codex_app_server.WINDOW_KEYS)
            assert all(type(value) in (int, float) and math.isfinite(value) for value in window.values())
