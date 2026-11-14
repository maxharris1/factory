"""#128 with telemetry (#93) on a detached remote run.

The start's worker-environment report never lands in a device file other
than its own mode-600 `env`, which the follower relays once and removes;
and the device's `handoff` artifact holds the parsed final handoff whether
the harness wrote text or a JSON event stream.
"""

from __future__ import annotations

import json
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lib import remote
from lib.remote_follow import EXITED
from tests.test_detached_remote import Clock, Script, Stop, follower, launched, polled
from tests.test_detached_remote_cli import run_dirs
from tests.test_remote_start import remote_setup
from tests.test_telemetry import _PROFILES, WORKER_SECRET, _orch, _remote_run, _row


@pytest.mark.parametrize("profile", sorted(_PROFILES))
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_worker_environment_report_never_stays_in_a_device_file(
    tmp_path: Path, harness: str, profile: str
) -> None:
    run, db, job_id = _remote_run(tmp_path, harness, profile=profile)
    assert run.returncode == 0, run.stderr
    [rundir] = run_dirs(tmp_path, f"{harness}-bot", job_id)
    assert stat.S_IMODE(rundir.stat().st_mode) == 0o700
    assert sorted(path.name for path in rundir.iterdir()) == ["exit", "handoff", "log", "pid", "stdin"]
    for path in rundir.iterdir():
        data = path.read_bytes()
        assert b"ORCH-WORKER-ENV" not in data, path.name
        assert WORKER_SECRET.encode().hex().encode() not in data, path.name
    assert [step["label"] for step in json.loads(_row(db, job_id)["steps"])] == ["command"]
    assert "ORCH-WORKER-ENV" not in run.stdout and run.stdout.endswith("plain worker line\n")


_FINAL = "Done: said \"ok\".\nORCH-HANDOFF: 123456"
_STREAMS = {
    "codex": ("--json", ["exec", "-"], [
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": _FINAL}},
        {"type": "turn.completed", "usage": {}},
    ]),
    "claude": ("--output-format", ["-p"], [
        {"type": "result", "subtype": "success", "result": _FINAL, "session_id": "s"},
    ]),
}


@pytest.mark.parametrize("telemetry", ["off", "on"])
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_device_records_the_final_handoff_in_text_and_stream_mode(
    tmp_path: Path, harness: str, telemetry: str
) -> None:
    machines, queue, _, job_id, ssh = remote_setup(tmp_path, harness)
    home = tmp_path / "worker-home"
    home.mkdir()
    worker_bin = tmp_path / "worker-bin"
    worker_bin.mkdir()
    (home / ".bash_profile").write_text(f"export PATH={shlex.quote(str(worker_bin))}:$PATH\n")
    flag, args, stream = _STREAMS[harness]
    cli = worker_bin / harness
    cli.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"if {flag!r} in sys.argv:\n"
        + "".join(f"    print({json.dumps(event)!r})\n" for event in stream)
        + f"else:\n    print({_FINAL!r})\n"
    )
    cli.chmod(0o755)
    result = _orch(
        "job", "run", f"{harness}-bot", job_id, "--db", str(queue.path),
        "--machines", str(machines), "--", str(cli), *args,
        env={"HOME": str(tmp_path), **ssh.env(), "FAKE_SSH_HOME": str(home),
             "ORCH_TELEMETRY": telemetry},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("ORCH-HANDOFF: 123456\n")
    [rundir] = run_dirs(tmp_path, f"{harness}-bot", job_id)
    assert (rundir / "handoff").read_text() == "ORCH-HANDOFF: 123456\n"


def _event(**fields) -> str:
    return json.dumps(fields)


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["working", "ORCH-HANDOFF: 7", ""], "ORCH-HANDOFF: 7"),
        (["  ORCH-HANDOFF: 7  "], "  ORCH-HANDOFF: 7  "),
        (["ORCH-HANDOFF: 7", "more text"], ""),
        ([], ""),
        ([_event(type="item.completed", item={"type": "agent_message", "text": "a\nORCH-HANDOFF: 9 \n"}),
          _event(type="turn.completed")], "ORCH-HANDOFF: 9"),
        ([_event(type="item.completed", item={"type": "agent_message", "text": "ORCH-HANDOFF: 9\nbye"}),
          _event(type="turn.completed")], ""),
        ([_event(type="item.completed", item={"type": "agent_message", "text": "ORCH-HANDOFF: 1"}),
          _event(type="item.completed", item={"type": "agent_message", "text": "ORCH-HANDOFF: 2"}),
          _event(type="turn.completed")], "ORCH-HANDOFF: 2"),
        ([_event(type="item.completed", item={"type": "agent_message", "text": "ORCH-HANDOFF: 1"}),
          _event(type="item.completed", item={"type": "command_execution", "text": "ORCH-HANDOFF: 3"})],
         "ORCH-HANDOFF: 1"),
        ([_event(type="result", result="ORCH-HANDOFF: 5"), "plain text after"], ""),
        (["plain", _event(type="result", is_error=False, result="q \"x\\\" \\u00e9\r\nORCH-HANDOFF: failed",
                                session_id="s")], "ORCH-HANDOFF: failed"),
        ([_event(type="result", result="ORCH-HANDOFF: 5"), _event(type="assistant", message={})],
         "ORCH-HANDOFF: 5"),
        ([_event(type="assistant", message={"content": [{"type": "text", "text": "ORCH-HANDOFF: 4"}]})], ""),
        ([_event(type="result", result="ORCH-HANDOFF: " + "x" * 101)], ""),
    ],
)
def test_the_handoff_rule_matches_the_box_for_text_and_events(
    tmp_path: Path, lines: list[str], expected: str
) -> None:
    log = tmp_path / "log"
    log.write_text("".join(line + "\n" for line in lines))
    done = subprocess.run(["awk", remote.HANDOFF_AWK, str(log)], capture_output=True, text=True, check=True)
    assert done.stdout == (expected + "\n" if expected else "")


def test_the_follower_relays_the_environment_report_once_before_the_log_and_removes_it(
    tmp_path: Path,
) -> None:
    report = "ORCH-WORKER-ENV 41423d310000\n"
    with_env = remote.Exchanged(0, b"line\n", f"{report}{remote.FOLLOW_MARK} 5 running\n")
    again = remote.Exchanged(0, b"end\n", f"{report}{remote.FOLLOW_MARK} 9 exited 0\n")
    transport = Script([launched(), polled(0, "running"), with_env, again])
    sink_path = tmp_path / "out"
    with sink_path.open("wb") as sink:
        end = follower(transport, Clock(), sink).run("LAUNCH", b"", Stop())
    assert end.kind == EXITED
    assert sink_path.read_bytes() == report.encode() + b"line\nend\n"
    target = follower(transport, Clock(), None).target
    assert transport.calls.count(remote.env_clear_script(target)) == 2
