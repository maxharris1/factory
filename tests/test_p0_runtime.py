"""PR 0.2 regression tests: runtime reliability, SSH mux, SQLite lifecycle, backups, cleanup."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import (
    job_cleanup,
    job_leftovers,
    poll_source,
    queue as queue_mod,
    remote,
    self_deploy,
    settings_apply,
    signin_broker,
    signin_relay,
    ssh_mux,
    state_backup,
    waiter,
)


def test_ssh_mux_uses_tight_keepalive_and_skips_hot_path_probe_and_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_dir = tmp_path / "ctrl"
    control_dir.mkdir(mode=0o700)
    monkeypatch.setenv("ORCH_SSH_CONTROL_DIR", str(control_dir))
    calls: list[list[str]] = []
    sock_alive = False

    def fake_runner(argv, **_kwargs):
        nonlocal sock_alive
        calls.append(list(argv))
        if "-O" in argv and argv[argv.index("-O") + 1] == "check":
            return subprocess.CompletedProcess(argv, 0 if sock_alive else 255)
        if "ControlMaster=yes" in argv:
            sock_path = next(a.split("=", 1)[1] for a in argv if a.startswith("ControlPath="))
            Path(sock_path).write_text("", encoding="utf-8")
            sock_alive = True
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0)

    mux = ssh_mux.Multiplexer(
        binary=remote.SSH_BINARY,
        options=remote.SSH_OPTIONS,
        environ=os.environ,
        runner=fake_runner,
    )
    target = remote.RemoteTarget(
        device="pc", alias="pc-codex", user="codex-bot", workdir_root="/tmp/w", job_id="j1"
    )
    argv = remote.ssh_argv(target, "echo hi")

    first = mux.prepare(argv)
    assert first.socket is not None
    master_call = next(c for c in calls if "ControlMaster=yes" in c)
    # Keepalive options must precede remote.SSH_OPTIONS's 60s defaults so OpenSSH uses 5s/2.
    idx_5 = master_call.index(f"ServerAliveInterval={ssh_mux.SERVER_ALIVE_INTERVAL}")
    idx_60 = master_call.index("ServerAliveInterval=60")
    assert idx_5 < idx_60
    assert f"ServerAliveCountMax={ssh_mux.SERVER_ALIVE_COUNT_MAX}" in master_call

    # Hot-path: make _locked explode if touched; second prepare must not lock or probe.
    def fail_lock(_sock: str):
        raise AssertionError("hot path must not acquire flock")

    monkeypatch.setattr(mux, "_locked", fail_lock)
    calls.clear()
    second = mux.prepare(argv)
    assert second.socket == first.socket
    assert len(calls) == 1
    assert "-O" in calls[0] and "check" in calls[0]
    assert not any(ssh_mux.PROBE_COMMAND in " ".join(c) for c in calls)


def test_signin_broker_idle_backoff_for_remote_relay_and_snap_back(tmp_path: Path) -> None:
    class FakeRemoteRelay:
        def poll(self):
            return signin_relay.SpoolListing(files=(), dir_uid=os.getuid(), dir_mode=0o700)

    class FakeStop:
        def __init__(self, limit: int) -> None:
            self.waits: list[float] = []
            self.limit = limit

        def is_set(self) -> bool:
            return len(self.waits) >= self.limit

        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            return self.is_set()

    db = tmp_path / "orch.db"
    queue_mod.SQLiteJobQueue(db, clock=lambda: 1_000.0)
    broker = signin_broker.Broker(
        relay=FakeRemoteRelay(),  # type: ignore[arg-type]
        machines=tmp_path / "machines.yml",
        db=db,
        state_dir=tmp_path / "state",
        approver="max",
        owner="factory",
        launch=lambda *_args: None,
        claims=contextlib.nullcontext,
    )
    outcomes = [[], [], [], [object()], []]
    broker.tick = lambda: outcomes.pop(0)  # type: ignore[assignment]
    stop = FakeStop(limit=5)
    broker.serve(stop)  # type: ignore[arg-type]

    assert stop.waits[0] == min(signin_broker.POLL_SECONDS * 2, signin_broker.IDLE_POLL_SECONDS)
    assert stop.waits[1] == min(stop.waits[0] * 2, signin_broker.IDLE_POLL_SECONDS)
    assert stop.waits[2] > stop.waits[1]
    assert stop.waits[3] == signin_broker.POLL_SECONDS
    assert stop.waits[4] == min(signin_broker.POLL_SECONDS * 2, signin_broker.IDLE_POLL_SECONDS)

    local_broker = signin_broker.Broker(
        relay=signin_relay.LocalRelay(tmp_path / "spool", tmp_path / "relay"),
        machines=tmp_path / "machines.yml",
        db=db,
        state_dir=tmp_path / "state",
        approver="max",
        owner="factory",
        launch=lambda *_args: None,
        claims=contextlib.nullcontext,
    )
    local_broker.tick = lambda: []  # type: ignore[assignment]
    local_stop = FakeStop(limit=3)
    local_broker.serve(local_stop)  # type: ignore[arg-type]
    assert local_stop.waits == [signin_broker.POLL_SECONDS] * 3


def test_sqlite_job_queue_closes_connections_on_context_exit(tmp_path: Path) -> None:
    q = queue_mod.SQLiteJobQueue(tmp_path / "orch.db", clock=lambda: 1_000.0)
    with q._connect() as conn:
        conn.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_poll_source_close_closes_sqlite_stores(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    cfg = SimpleNamespace(
        trusted_logins={},
        trusted_linear_users={},
        poll_seconds=60,
        secret_env_names=(),
        poller_reads_app=None,
    )
    source = poll_source.LivePollSource(
        cfg,  # type: ignore[arg-type]
        risks=poll_source.IntakeRisks(db),
        fed_pulls=poll_source.FedPulls(db),
        gh_cache=poll_source.GhCacheExpiries(db),
        required_globs=poll_source.RequiredGlobs(db),
    )
    source.close()
    for store in (source.risks, source.fed_pulls, source.gh_cache, source.required_globs):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store._connection.execute("SELECT 1")


def test_adopted_run_does_not_hang_forever_on_non_blocking_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir = tmp_path / "job-1"
    job_dir.mkdir()
    (job_dir / waiter.RUN_LOCK).write_text("", encoding="utf-8")
    run = waiter.AdoptedRun(
        pid=999999,
        start="1",
        job_dir=job_dir,
        gone=lambda _pid, _start: True,
    )

    def boom(*_args, **_kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(os, "open", boom)
    assert run.poll() == 255


def test_adopted_run_persists_and_reads_private_claim_proof(tmp_path: Path) -> None:
    job_dir = tmp_path / "job-1"
    job_dir.mkdir(mode=0o700)
    proof_path = job_dir / waiter.REMOTE_PROOF
    waiter._write_private(proof_path, b"secret-token-xyz\n")
    assert stat.S_IMODE(proof_path.stat().st_mode) == 0o600
    proof = waiter._read_private_proof(proof_path)
    assert proof is not None and proof.token == "secret-token-xyz"
    proof_path.chmod(0o644)
    assert waiter._read_private_proof(proof_path) is None


def test_self_deploy_retries_pending_install_and_prunes_backups(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    backup_root = state / self_deploy.BACKUP_DIR
    backup_root.mkdir()
    for idx in range(self_deploy.KEEP_BACKUPS + 5):
        d = backup_root / f"20261124T0000{idx:02d}Z"
        d.mkdir()
        (d / "diff.patch").write_text("x", encoding="utf-8")
    self_deploy._prune_backups(backup_root)
    remaining = sorted(p.name for p in backup_root.iterdir() if p.is_dir())
    assert len(remaining) == self_deploy.KEEP_BACKUPS
    assert remaining[0] == "20261124T000005Z"

    # End-to-end pending install retry across an up-to-date pass.
    remote_bare = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote_bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote_bare), str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "t"], check=True)
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)

    marker = state / self_deploy.INSTALL_PENDING
    marker.write_text(json.dumps({"commit": "abc", "exit": 3}) + "\n", encoding="utf-8")
    calls: list[list[str]] = []
    codes = [2, 0]

    def fake_install(argv, _env, _log):
        calls.append(list(argv))
        return codes.pop(0)

    home = tmp_path / "home"
    home.mkdir()
    outcome1, _ = self_deploy.run_pass(
        checkout,
        state,
        install=fake_install,
        homes=(),
        environ={"HOME": str(home)},
        webhook=self_deploy.Webhook(),
        now=1_000.0,
    )
    assert outcome1.kind == self_deploy.INSTALL_FAILED
    assert marker.is_file()

    outcome2, _ = self_deploy.run_pass(
        checkout,
        state,
        install=fake_install,
        homes=(),
        environ={"HOME": str(home)},
        webhook=self_deploy.Webhook(),
        now=1_001.0,
    )
    assert outcome2.kind == self_deploy.UP_TO_DATE
    assert not marker.exists()
    assert len(calls) == 2 and all(c[-1] == "--update" for c in calls)


def test_settings_apply_prunes_old_backups(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text("devices: {}\n", encoding="utf-8")
    for idx in range(25):
        (tmp_path / f"machines.yml.bak-settings-{1000 + idx}").write_text("old", encoding="utf-8")
    applier = settings_apply.Applier(
        machines=machines,
        state_dir=tmp_path / "state",
        deploy=tmp_path / "deploy",
        approver="max",
        environ={},
        restart=(),
        pending=(),
        claims=contextlib.nullcontext,
        db=tmp_path / "orch.db",
    )
    applier._prune_backups(keep=20)
    baks = sorted(p.name for p in tmp_path.glob("machines.yml.bak-settings-*"))
    assert len(baks) == 20
    assert baks[0] == "machines.yml.bak-settings-1005"


def test_job_leftovers_exempts_ssh_agent_and_setgid_and_prunes_kept(tmp_path: Path) -> None:
    # Build synthetic /proc/<pid> directories to exercise job_cleanup._hides_from_scan.
    uid = os.getuid()

    def make_proc(name: str, gid_line: str) -> Path:
        p = tmp_path / f"proc-{name}"
        p.mkdir()
        (p / "status").write_text(
            f"Name:\t{name}\nVmSize:\t1000 kB\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
            f"Dumpable:\t0\nGid:\t{gid_line}\n",
            encoding="utf-8",
        )
        return p

    # ssh-agent / setgid processes must NOT trigger blind=True global stall.
    assert not job_cleanup._hides_from_scan(make_proc("ssh-agent", "1000\t115\t115\t115"))
    assert not job_cleanup._hides_from_scan(make_proc("some-helper", "1000\t42\t42\t42"))
    # Regular user non-dumpable process MUST still hide (safety contract preserved).
    assert job_cleanup._hides_from_scan(make_proc("claude", "1000\t1000\t1000\t1000"))

    root = tmp_path / "work"
    kept_root = root / job_leftovers.KEPT_ROOT
    old_dir = kept_root / "job-old"
    fresh_dir = kept_root / "job-fresh"
    pending_dir = kept_root / "job-pending"
    for d in (old_dir, fresh_dir, pending_dir):
        d.mkdir(parents=True)
        (d / "file.txt").write_text("data", encoding="utf-8")
    now = 1_800_000_000.0
    (old_dir / job_leftovers.KEPT_JSON).write_text(
        json.dumps({"at": now - (15 * 86400)}), encoding="utf-8"
    )
    (fresh_dir / job_leftovers.KEPT_JSON).write_text(
        json.dumps({"at": now - (2 * 86400)}), encoding="utf-8"
    )
    (pending_dir / job_leftovers.PENDING).write_text("{}", encoding="utf-8")

    pruned = job_leftovers.prune_kept(root, now=now, retention_days=14.0)
    assert pruned == 1
    assert not old_dir.exists()
    assert fresh_dir.exists()
    assert pending_dir.exists()


def test_state_backup_and_restore_include_job_detail(tmp_path: Path) -> None:
    live_dir = tmp_path / "live"
    backup_dir = tmp_path / "backup"
    live_dir.mkdir()
    live_db = live_dir / "orch.db"
    q = queue_mod.SQLiteJobQueue(live_db, clock=lambda: 1_000.0)
    with q._connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ping (n INTEGER)")
        conn.execute("INSERT INTO ping VALUES (1)")
    detail_dir = live_dir / "job-detail"
    detail_dir.mkdir()
    (detail_dir / "job-42.json").write_text(json.dumps({"id": "job-42"}), encoding="utf-8")

    target_db = backup_dir / "orch.db"
    state_backup.backup(live_db, target_db)
    assert (backup_dir / "job-detail" / "job-42.json").exists()

    (detail_dir / "job-42.json").unlink()
    live_db.unlink()
    state_backup.restore(target_db, live_db)
    assert (live_dir / "job-detail" / "job-42.json").exists()
    assert json.loads((live_dir / "job-detail" / "job-42.json").read_text(encoding="utf-8")) == {
        "id": "job-42"
    }


def test_closing_connection_advances_db_fingerprint_on_rapid_commits(tmp_path: Path) -> None:
    from lib.dashboard_push import db_fingerprint

    db = tmp_path / "orch.db"
    now = [1_800_000_000.0]

    def tick() -> float:
        now[0] += 1.0
        return now[0]

    q = queue_mod.SQLiteJobQueue(db, clock=tick)
    req = queue_mod.JobRequest(
        repo="maxharris1/factory",
        ticket_id="58",
        action="review",
        recipient="codex",
        source_comment_id="c1",
        timeout_seconds=600,
    )
    prev = db_fingerprint(db)
    q.enqueue(req, dedupe_key="rapid-1")
    after = db_fingerprint(db)
    assert after != prev
    job = q.claim("codex")
    assert job is not None
    prev = db_fingerprint(db)
    for _ in range(20):
        q.heartbeat(job.id, owner="codex")
        cur = db_fingerprint(db)
        assert cur != prev
        prev = cur


def test_signin_broker_resets_backoff_when_worker_finishes_within_tick(tmp_path: Path) -> None:
    """Codex PR #1536 f-1: real _intake() + synchronous worker completion resets wait to 2s."""
    from types import SimpleNamespace

    waits: list[float] = []
    polls = [0]
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    req_body = json.dumps(
        {
            "target": "account",
            "op": "signin",
            "id": "req-5",
            "actor": "max",
            "account": "claude-b",
            "device": "box",
            "requested_at": "2026-11-24T22:00:00Z",
        }
    ).encode("utf-8")

    class FakeRelay:
        def poll(self):
            polls[0] += 1
            files = (
                (settings_apply.SpoolFile("req-5.json", "factory", 0o600, True, req_body),)
                if polls[0] == 5
                else ()
            )
            return settings_apply.SpoolListing(directory=True, mode=0o700, files=files)

        def publish(self, request_id: str, status: dict) -> None:
            pass

    class FakeStop:
        def is_set(self) -> bool:
            return len(waits) >= 6

        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            return False

    fake_device = SimpleNamespace(name="box")

    broker = signin_broker.Broker(
        relay=FakeRelay(),  # type: ignore[arg-type]
        machines=tmp_path / "machines.yml",
        db=tmp_path / "orch.db",
        state_dir=state_dir,
        approver="max",
        owner="factory",
        launch=lambda attempt, finish: finish(attempt, "signed-in", None, None),
        claims=contextlib.nullcontext,  # type: ignore[arg-type]
        transport=object(),  # type: ignore[arg-type]
        spawn=lambda fn: fn(),
    )

    broker._refresh_holds = lambda: None  # type: ignore[method-assign]
    broker._previous = lambda req: None  # type: ignore[method-assign]
    broker._home_missing = lambda d, h, home: False  # type: ignore[method-assign]
    broker._release = lambda attempt: None  # type: ignore[method-assign]

    def fake_admit(req, previous):
        attempt = signin_broker.Attempt(req, "claude", fake_device, "/home/claude-b", previous)  # type: ignore[arg-type]
        with broker._lock:
            broker._attempts[(req.account, req.device)] = attempt
        return attempt

    broker._admit = fake_admit  # type: ignore[method-assign]
    broker.serve(FakeStop())  # type: ignore[arg-type]
    assert waits == [4.0, 8.0, 16.0, 30.0, 2.0, 4.0]


def test_pending_install_retry_marks_device_deploying(tmp_path: Path) -> None:
    """Codex PR #1536 f-2: pending install retries must hold DEPLOYING to block claims."""
    import subprocess
    from lib import device_sync

    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    state = tmp_path / "state"
    home = tmp_path / "home"
    state.mkdir()
    home.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "main"], check=True, capture_output=True)
    (checkout / "deploy").mkdir()
    (checkout / "deploy" / "install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)

    # Plant INSTALL_PENDING so run_pass hits the up-to-date retry branch.
    (state / self_deploy.INSTALL_PENDING).write_text("pending\n", encoding="utf-8")
    db = tmp_path / "orch.db"
    q = queue_mod.SQLiteJobQueue(db, clock=lambda: 1_000.0)
    seen_deploying: list[bool] = []
    codes = iter([1, 0])

    def fake_install(argv, env, log):
        with q.connect() as conn:
            rows = conn.execute(
                "SELECT state FROM device_sync WHERE device = ? AND state = ?",
                ("box", device_sync.DEPLOYING),
            ).fetchall()
            seen_deploying.append(bool(rows))
        return next(codes)

    o1, _ = self_deploy.run_pass(
        checkout, state, install=fake_install, homes=(),
        environ={"HOME": str(home)}, webhook=self_deploy.Webhook(),
        now=1_000.0, queue=q, device="box",
    )
    assert o1.kind == self_deploy.INSTALL_FAILED
    o2, _ = self_deploy.run_pass(
        checkout, state, install=fake_install, homes=(),
        environ={"HOME": str(home)}, webhook=self_deploy.Webhook(),
        now=1_001.0, queue=q, device="box",
    )
    assert o2.kind == self_deploy.UP_TO_DATE
    assert seen_deploying == [True, True]
    with q.connect() as conn:
        leftover = conn.execute(
            "SELECT state FROM device_sync WHERE device = ? AND state = ?",
            ("box", device_sync.DEPLOYING),
        ).fetchall()
    assert leftover == []


def test_backup_pruning_preserves_current_backup_on_clock_rollback(tmp_path: Path) -> None:
    """Codex PR #1536 f-3: pruning must never delete the newly created active backup."""
    # Self-deploy backups: 10 future timestamps + 1 current backup at earlier timestamp.
    sd_root = tmp_path / "sd-backups"
    sd_root.mkdir()
    for i in range(10):
        (sd_root / f"20990101T000{i:02d}00Z-old{i}").mkdir()
    current_sd = sd_root / "20200101T000000Z-current"
    current_sd.mkdir()
    self_deploy._prune_backups(sd_root, keep=10, preserve=current_sd)
    assert current_sd.is_dir()
    assert len(list(sd_root.iterdir())) == 10

    # Settings backups: 20 future timestamps + 1 current backup at earlier timestamp.
    machines = tmp_path / "machines.yml"
    machines.write_text("devices: {}\n", encoding="utf-8")
    for i in range(20):
        (tmp_path / f"machines.yml.bak-settings-20990101T000{i:02d}00Z").write_text("old", encoding="utf-8")
    applier = settings_apply.Applier(
        machines=machines,
        state_dir=tmp_path / "state",
        deploy=tmp_path / "deploy",
        approver="max",
        environ={},
        restart=(),
        pending=(),
        claims=contextlib.nullcontext,
        db=tmp_path / "orch.db",
        clock=lambda: 1_577_836_800.0,  # 2020-01-01T00:00:00Z (earlier than 2099...)
    )
    created = applier._backup(b"previous-config\n", 0o600)
    assert created.is_file()
    assert created.read_bytes() == b"previous-config\n"
    remaining = list(tmp_path.glob("machines.yml.bak-settings-*"))
    assert len(remaining) == 20


def test_state_backup_raises_on_job_detail_write_failure(tmp_path: Path) -> None:
    """Codex PR #1536 f-4: backup() must fail when required job-detail files cannot be written."""
    live_dir = tmp_path / "live"
    backup_dir = tmp_path / "backup"
    live_dir.mkdir()
    live_db = live_dir / "orch.db"
    queue_mod.SQLiteJobQueue(live_db, clock=lambda: 1_000.0)
    detail_dir = live_dir / "job-detail"
    detail_dir.mkdir()
    (detail_dir / "job-1.json").write_text('{"brief_text": "original brief"}', encoding="utf-8")

    target_db = backup_dir / "orch.db"
    state_backup.backup(live_db, target_db)

    # Update live detail and make backup's job-detail directory read-only.
    (detail_dir / "job-1.json").write_text('{"brief_text": "new brief"}', encoding="utf-8")
    backup_detail = backup_dir / "job-detail"
    backup_detail.chmod(0o500)
    try:
        with pytest.raises(state_backup.BackupError, match="job-detail"):
            state_backup.backup(live_db, target_db)
    finally:
        backup_detail.chmod(0o700)
