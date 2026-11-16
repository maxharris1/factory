"""Every claim carries a per-claim token (#127).

`orch wait` prints the token as `proof` and stores only its SHA-256 on the
job. While the job runs, `orch job heartbeat`, `orch job done` and a plain
`orch job run` are refused (exit 2, nothing written) unless ORCH_JOB_PROOF
holds that claim's token.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lib.job_token import PROOF_ENV
from tests.handoff_store import env_store, serve_handoff
from tests.test_wait import make_db, request, state_of

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
WATCHED = ("state", "last_activity_at", "finished_at", "handoff_comment_id")
COMMANDS = {
    "heartbeat": ["job", "heartbeat", "claude-bot", "{id}"],
    "done --handoff": ["job", "done", "claude-bot", "{id}", "--handoff", "1009"],
    "done --failed": ["job", "done", "claude-bot", "{id}", "--failed"],
}


def orch(args: list[str], db: Path, proof: str | None = None, **env: str):
    environ = {key: value for key, value in os.environ.items() if key != PROOF_ENV}
    if proof is not None:
        environ[PROOF_ENV] = proof
    return subprocess.run(
        [sys.executable, str(ORCH), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env={**environ, "ORCH_DB": str(db), **env},
    )


def claimed(tmp_path: Path):
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request(), dedupe_key="a")
    serve_handoff(env_store(), job, "1009")
    waited = orch(["wait", "claude-bot"], db)
    assert waited.returncode == 0, waited.stderr
    line = json.loads(waited.stdout)
    assert line["id"] == job.id
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET last_activity_at = last_activity_at - 100")
    connection.close()
    return db, queue, job, line["proof"]


def watched(db: Path, job_id: str) -> tuple:
    row = state_of(db, job_id)
    return tuple(row[name] for name in WATCHED)


def test_wait_prints_a_proof_and_the_database_holds_only_its_hash(tmp_path: Path) -> None:
    db, _, job, proof = claimed(tmp_path)

    assert isinstance(proof, str) and len(proof) >= 32
    row = state_of(db, job.id)
    assert row["claim_token_hash"] == hashlib.sha256(proof.encode()).hexdigest()
    with sqlite3.connect(db) as connection:
        dump = "\n".join(connection.iterdump())
    connection.close()
    assert proof not in dump


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("presented", [None, "not-the-proof"], ids=["no-proof", "wrong-proof"])
def test_a_write_without_the_claims_proof_is_refused_and_changes_nothing(
    tmp_path: Path, command: str, presented: str | None
) -> None:
    db, _, job, _ = claimed(tmp_path)
    before = watched(db, job.id)

    result = orch([part.format(id=job.id) for part in COMMANDS[command]], db, presented)

    assert result.returncode == 2, result.stderr
    assert "ORCH_JOB_PROOF" in result.stderr
    assert watched(db, job.id) == before


@pytest.mark.parametrize("command", COMMANDS)
def test_the_claims_proof_is_accepted(tmp_path: Path, command: str) -> None:
    db, _, job, proof = claimed(tmp_path)
    before = watched(db, job.id)

    result = orch([part.format(id=job.id) for part in COMMANDS[command]], db, proof)

    assert result.returncode == 0, result.stderr
    assert watched(db, job.id) != before


def test_a_requeue_and_reclaim_retires_the_old_proof(tmp_path: Path) -> None:
    db, queue, job, old = claimed(tmp_path)
    queue.release(job.id, owner="claude")
    waited = orch(["wait", "claude-bot"], db)
    assert waited.returncode == 0, waited.stderr
    new = json.loads(waited.stdout)["proof"]
    assert new != old

    stale = orch(["job", "heartbeat", "claude-bot", job.id], db, old)
    fresh = orch(["job", "heartbeat", "claude-bot", job.id], db, new)

    assert stale.returncode == 2, stale.stderr
    assert fresh.returncode == 0, fresh.stderr


def test_a_plain_job_run_without_a_proof_starts_nothing(tmp_path: Path) -> None:
    db, _, job, _ = claimed(tmp_path)
    before = watched(db, job.id)
    marker = tmp_path / "started"

    result = orch(
        ["job", "run", "claude-bot", job.id, "--", "touch", str(marker)],
        db,
    )

    assert result.returncode == 2, result.stderr
    assert "ORCH_JOB_PROOF" in result.stderr
    assert not marker.exists()
    assert watched(db, job.id) == before


@pytest.mark.parametrize("command", ["heartbeat", "done --failed", "done --handoff", "run"])
def test_a_reclaim_after_the_proof_check_still_refuses_the_old_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from lib import cli, job_token
    from tests.test_waiter import MACHINES, enqueue, local_pool, make_queue

    db, queue = make_queue(tmp_path)
    pool = local_pool(queue)
    job_id = enqueue(queue, "reclaim")
    old, old_hash = job_token.mint()
    _, new_hash = job_token.mint()
    assert pool.claim("claude", token_hash=old_hash)[0].id == job_id
    monkeypatch.setenv(PROOF_ENV, old)
    checked = cli._proof_refusal
    reclaimed: list[tuple] = []

    def check_then_reclaim(q, ident):
        refusal = checked(q, ident)
        assert refusal is None
        if not reclaimed:
            queue.release(job_id, owner="claude")
            assert pool.claim("claude", token_hash=new_hash)[0].id == job_id
            reclaimed.append(watched(db, job_id))
        return refusal

    monkeypatch.setattr(cli, "_proof_refusal", check_then_reclaim)
    monkeypatch.setattr(cli, "_check_handoff", lambda *args: None)
    marker = tmp_path / "old-claim-command-ran"
    args = {
        "heartbeat": ["job", "heartbeat", "claude-bot", job_id],
        "done --failed": ["job", "done", "claude-bot", job_id, "--failed"],
        "done --handoff": ["job", "done", "claude-bot", job_id, "--handoff", "1009"],
        "run": ["job", "run", "claude-bot", job_id, "--machines", str(MACHINES), "--",
                sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
    }[command]

    code = cli.main([*args[:4], "--db", str(db), *args[4:]])

    assert code == 2
    assert [watched(db, job_id)] == reclaimed
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT claim_token_hash FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0] == new_hash
    assert not marker.exists()
