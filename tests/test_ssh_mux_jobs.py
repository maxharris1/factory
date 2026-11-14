"""#222: a stale ControlMaster is never a job failure.

`orch job run` on a remote device, end to end over tests/fake_ssh.py: the
master is stale before the launch and killed again while the job runs. Each
next call rebuilds it inside the transport; the job ends on its first
attempt with no `run-died`, no lost link and one harness.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from lib import ssh_mux
from tests.test_detached_remote_cli import (
    CODEX,
    events,
    harness,
    run,
    run_dirs,
    setup,
    started_harness,
)

USER = "codex-bot"


def masters(ssh) -> list[dict]:
    path = Path(f"{ssh.log}.control")
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [entry for entry in entries if entry["op"] == "master"]


def test_a_job_rides_out_stale_masters_without_a_failure(tmp_path: Path) -> None:
    poller, db, job_id, ssh = setup(tmp_path)
    sock = Path(ssh_mux.socket_path(os.environ["ORCH_SSH_CONTROL_DIR"], CODEX, USER))
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock))  # left by a master that died with the box's last boot
    stale.close()
    count = tmp_path / "count"
    proc = run(poller, job_id, ssh, "--interval", "0.3", "--", *harness(count, 40, 0.1))
    try:
        started_harness(count, proc)
        for _ in range(2):  # a VM pause kills the master mid-run, twice
            deadline = time.monotonic() + 20
            marker = Path(f"{sock}.fake-master")
            while not marker.exists():
                assert time.monotonic() < deadline
                time.sleep(0.05)
            marker.unlink()
            time.sleep(0.8)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert "lost the link" not in err
    assert len(count.read_text().split()) == 1
    [rundir] = run_dirs(tmp_path, "codex-bot", job_id)
    assert out.encode() == (rundir / "log").read_bytes()
    assert [event for event, _ in events(db, job_id) if "died" in event or "fail" in event] == []
    row = poller.row(job_id)
    assert row["attempt"] == 1 and row["state"] != "failed"
    assert len(masters(ssh)) >= 3  # the stale one replaced, then each kill rebuilt
    assert all(entry["code"] == 0 for entry in masters(ssh))
    assert {call["via"] for call in ssh.calls()} == {"master"}
