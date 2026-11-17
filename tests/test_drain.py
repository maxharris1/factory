from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.dispatcher import Dispatcher
from lib.queue import SQLiteJobQueue
from lib.settings_apply import Applier, LocalSpool, run_pass, running_from_db
from lib.worktrees import pid_alive, process_start
from tests.test_dispatcher import TRUSTED_LOGINS, FakeGrokDelivery, FakeReviewActions
from tests.test_remote_start import (
    FakeTransport,
    config_of,
    enqueue,
    job_events,
    machines_file,
    poll_at,
    stamp,
    state,
)

HARNESSES = ("claude", "codex", "cursor")
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


class Settings:
    """Mission Control requests applied through `orch settings apply`'s applier."""

    def __init__(self, tmp_path: Path, machines: Path, db: Path):
        self.machines = machines
        self.spool = tmp_path / "spool"
        self.spool.mkdir(mode=0o755)
        self.restarts: list[list[str]] = []
        state = tmp_path / "state"
        state.mkdir()
        self.applier = Applier(
            machines=machines, state_dir=state, deploy=tmp_path / "deploy",
            approver="maxharris1", environ={}, restart=("poller", "waiter:claude-bot"),
            pending=(), running=running_from_db(db),
            run=lambda argv, env, log: self.restarts.append(list(argv)) or 0,
        )
        self.count = 0

    def apply(self, device: str, field: str, expect: object, value: object, ns: int):
        self.count += 1
        rid = f"drain-test-request-{self.count:04d}"
        path = self.spool / f"{rid}.json"
        path.write_text(json.dumps({
            "id": rid, "actor": "maxharris1", "requested_at": "2026-11-17T10:00:00Z",
            "device": device, "field": field, "expect": expect, "value": value,
        }), encoding="utf-8")
        path.chmod(0o644)
        (entry,) = run_pass(self.applier, LocalSpool(self.spool),
                            pwd.getpwuid(os.getuid()).pw_name)
        stamp(self.machines, ns)
        return entry


@pytest.mark.parametrize(
    ("field", "expect", "value"),
    [("drain", False, True), ("slot_max", 6, 1)],
    ids=["drain", "slot_max-below-running"],
)
def test_running_box_jobs_finish_and_new_jobs_wait_or_go_elsewhere(
    tmp_path: Path, field: str, expect: object, value: object
) -> None:
    now = [0.0]
    queue = SQLiteJobQueue(tmp_path / "d.db", clock=lambda: now[0])
    machines = machines_file(tmp_path, box_slots=6, pc=False)
    config = config_of(machines)
    pool = DevicePool(queue, config)
    kills: list[tuple[int, int]] = []
    dispatcher = Dispatcher(
        queue.path,
        queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        devices=DeviceMonitor(
            pool,
            config,
            FakeTransport(),
            alive=pid_alive,
            kill=lambda pid, sig: kills.append((pid, sig)),
            machines_path=machines,
        ),
    )
    sleepers: list[subprocess.Popen] = []
    try:
        jobs = []
        for harness in HARNESSES:
            enqueue(queue, harness, harness, timeout=5400)
            claimed = pool.claim(harness)
            assert claimed is not None and claimed[1].device == "box", "placed on the box"
            job = claimed[0]
            runner = subprocess.Popen(SLEEPER)
            worker = subprocess.Popen(SLEEPER)
            sleepers += [runner, worker]
            pool.attach_runner(
                job.id, owner=harness, pid=runner.pid, start=process_start(runner.pid),
                alive=pid_alive,
            )
            pool.attach_worker(job.id, owner=harness, pid=worker.pid, start=process_start(worker.pid))
            jobs.append(job.id)

        def assert_box_jobs_untouched(step: str) -> None:
            assert [state(queue, job_id) for job_id in jobs] == ["running"] * 3, step
            assert pool.used().get("box") == 3, step
            for job_id in jobs:
                assert "device-lost" not in [e for e, _ in job_events(queue, job_id)], step
            assert pool.orphans() == [], step
            assert kills == [], step
            assert [proc.poll() for proc in sleepers] == [None] * 6, step

        poll_at(now, dispatcher, 0.0)
        assert_box_jobs_untouched("before the change")

        settings = Settings(tmp_path, machines, queue.path)
        refused = settings.apply("box", "enabled", True, False, 10**18 - 1)
        assert (refused.result, refused.error) == (
            "refused", "box holds 3 running job(s); drain it first"), "never disable a busy box"
        assert settings.restarts == []

        applied = settings.apply("box", field, expect, value, 10**18)
        assert applied.result == "applied", applied
        assert settings.restarts == [
            [str(tmp_path / "deploy" / "poller-service.sh"), "restart"],
            [str(tmp_path / "deploy" / "waiter-service.sh"), "claude-bot", "restart"],
        ]
        text = machines.read_text(encoding="utf-8")
        assert "slot_max: 0" not in text and "enabled: false" in text.split("- name: pc")[1]
        assert text.split("- name: pc")[0].count("enabled: false") == 0, "the box stays enabled"
        for at in (60.0, 120.0, 180.0):
            poll_at(now, dispatcher, at)
            assert_box_jobs_untouched(f"poll at {at} after the change")
        assert pool.used() == {"box": 3}, "no slot moved or freed"
        assert dispatcher.devices.config != config, "the edit was reloaded"
        box = dispatcher.devices.config.device("box")
        assert box.enabled is True, "drain is not implemented as disable"
        assert pool.config is dispatcher.devices.config, "the pool places with the reloaded file"

        fourth = enqueue(queue, "claude", "fourth", timeout=5400)
        assert pool.claim("claude") is None, "no new placement on the box"
        assert state(queue, fourth) == "queued", "the fourth job stays queued"

        assert settings.apply("pc", "enabled", False, True, 10**18 + 1).result == "applied"
        poll_at(now, dispatcher, 240.0)
        claimed = pool.claim("claude")
        assert claimed is not None and claimed[1].device == "pc", "the fourth job lands on pc"
        assert pool.used() == {"box": 3, "pc": 1}
        assert_box_jobs_untouched("after pc is enabled")
    finally:
        for proc in sleepers:
            proc.kill()
            proc.wait()
