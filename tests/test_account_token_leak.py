"""#1184 ac-3: a token value planted for a non-default account never reaches
the run log, the telemetry or the job detail of its `orch job run`.

Out of scope: the default account, other processes' logs and the harness's
own files.
"""

from __future__ import annotations

import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.device_pool import DevicePool
from lib.harness_env import ACCOUNT_ISOLATION
from lib.queue import SQLiteJobQueue
from tests.test_job_run import _orch, _queue, _running_job
from tests.test_remote_start import Ssh, config_of, enqueue, machines_file, orch

HARNESS = """
import json, os, sys
env = dict(os.environ)
print("env: " + " ".join(f"{k}={v}" for k, v in sorted(env.items())), flush=True)
print(json.dumps({"type": "system", "subtype": "env", "env": env}), flush=True)
print("env: " + repr(env), file=sys.stderr, flush=True)
"""


def _plant(harness: str) -> tuple[dict[str, str], str]:
    tokens = {name: f"leak-{secrets.token_hex(16)}" for name in ACCOUNT_ISOLATION[harness].tokens}
    return tokens, f"control-{secrets.token_hex(16)}"


def _outputs(stdout: str, stderr: str, state: Path, db: Path) -> dict[str, str]:
    seen = {"run log stdout": stdout, "run log stderr": stderr}
    for name in ("events.jsonl", "events.jsonl.1"):
        path = state / name
        if path.exists():
            seen[str(path)] = path.read_text(encoding="utf-8", errors="replace")
    with sqlite3.connect(db) as connection:
        seen["orch.db"] = "\n".join(connection.iterdump())
    for path in sorted((db.parent / "job-detail").rglob("*")):
        if path.is_file():
            seen[str(path)] = path.read_text(encoding="utf-8", errors="replace")
    return seen


def _assert_no_leak(seen: dict[str, str], tokens: dict[str, str], control: str) -> None:
    assert control in seen["run log stdout"], "the harness printed its environment to the run log"
    assert any(control in text for name, text in seen.items() if "events.jsonl" in name), (
        "the harness's environment reached the telemetry raw log"
    )
    leaks = [(name, where) for name, value in tokens.items() for where, text in seen.items() if value in text]
    assert leaks == []


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_local_non_default_account_run_never_records_a_token_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    placed = f"{harness}-max-2"
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "accounts:\n"
        f"  - {{id: {placed}, harness: {harness}, signed_in_on: [box]}}\n"
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 3\n"
        f"    home: {tmp_path}/homes/{{bot}}\n"
        "    harnesses: {claude: claude-bot, codex: codex-bot}\n",
        encoding="utf-8",
    )
    queue = _queue(tmp_path)
    job = _running_job(queue, recipient=harness, machines=machines)
    with queue.connect() as connection:
        connection.execute("UPDATE device_slots SET account = ? WHERE job_id = ?", (placed, job.id))
    for isolation in ACCOUNT_ISOLATION.values():
        monkeypatch.delenv(isolation.home, raising=False)
    tokens, control = _plant(harness)
    state = tmp_path / "state"

    proc = _orch(
        "job", "run", f"{harness}-bot", job.id, "--machines", str(machines), "--db", str(tmp_path / "orch.db"),
        "--interval", "0.2", "--", sys.executable, "-c", HARNESS,
        env={"HOME": str(tmp_path / "login"), "ORCH_JOB_STATE_DIR": str(state), "ORCH_TELEMETRY": "on",
             "LEAK_CONTROL": control, **tokens},
        stdin=subprocess.DEVNULL,
    )
    stdout, stderr = proc.communicate(timeout=60)

    assert proc.returncode == 0, stderr
    _assert_no_leak(_outputs(stdout, stderr, state, tmp_path / "orch.db"), tokens, control)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_remote_non_default_account_run_never_records_a_token_value(tmp_path: Path, harness: str) -> None:
    bot = f"{harness}-bot"
    placed = f"{harness}-max-2"
    machines = machines_file(tmp_path, box_enabled=False)
    machines.write_text(
        "accounts:\n"
        f"  - {{id: {placed}, harness: {harness}, signed_in_on: [pc]}}\n"
        + machines.read_text(encoding="utf-8").replace(
            "    ssh_alias: fakepc-{bot}\n", f"    ssh_alias: fakepc-{{bot}}\n    home: {tmp_path}/homes/{{bot}}\n"
        ),
        encoding="utf-8",
    )
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    job_id = enqueue(queue, harness, "1")
    assert DevicePool(queue, config_of(machines)).claim(harness) is not None
    with queue.connect() as connection:
        connection.execute("UPDATE device_slots SET account = ? WHERE job_id = ?", (placed, job_id))
    ssh = Ssh(tmp_path)
    home = tmp_path / "homes" / bot
    home.mkdir(parents=True)
    tokens, control = _plant(harness)
    (home / ".bash_profile").write_text(
        f"export LEAK_CONTROL={control}\n" + "".join(f"export {name}={value}\n" for name, value in tokens.items()),
        encoding="utf-8",
    )
    state = tmp_path / "state"

    proc = orch(
        "job", "run", bot, job_id, "--db", str(tmp_path / "orch.db"), "--machines", str(machines),
        "--", sys.executable, "-c", HARNESS,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home), "ORCH_JOB_STATE_DIR": str(state), "ORCH_TELEMETRY": "on",
             **tokens},
    )
    stdout, stderr = proc.communicate(timeout=60)

    assert proc.returncode == 0, stderr
    _assert_no_leak(_outputs(stdout, stderr, state, tmp_path / "orch.db"), tokens, control)

