from __future__ import annotations

import signal
from dataclasses import replace
from pathlib import Path

import pytest

from lib import device_monitor, device_pool, process_tree
from lib.device_monitor import DeviceMonitor
from tests.test_remote_start import TAG, FakeTransport, monitor_setup

SCOPE = process_tree.scope_unit(TAG)
CGROUP = f"/user.slice/user-1000.slice/user@1000.service/app.slice/{SCOPE}"
RUNNER = 11
WORKER = 12
CHILD = 30
STRANGER = 40


class Box:
    def __init__(self, tmp_path: Path, *, finished: bool, stubborn: bool = False):
        self.queue, self.pool, config, self.job_id = monitor_setup(tmp_path, "claude", box=True)
        self.pool.attach_runner(
            self.job_id, owner="claude", pid=RUNNER, start="r", alive=lambda *_: False
        )
        self.pool.attach_worker(
            self.job_id, owner="claude", pid=WORKER, start="w", run_tag=TAG, run_scope=SCOPE
        )
        if finished:
            self.queue.finish(self.job_id, owner="claude", handoff_comment_id="c1")
        self.root = tmp_path / "cgroup"
        self.kill_file = self.root / CGROUP.lstrip("/") / "cgroup.kill"
        self.kill_file.parent.mkdir(parents=True)
        self.kill_file.write_text("0")
        self.listing = {
            CHILD: process_tree.Process(
                CHILD, ppid=1, start="c", tagged=True, group=CHILD, scope=SCOPE, cgroup=CGROUP
            ),
            STRANGER: process_tree.Process(
                STRANGER, ppid=1, start="x", tagged=False, group=STRANGER
            ),
        }
        self.stubborn = stubborn
        self.scans = 0
        self.signals: list[tuple[int, int, str]] = []
        self.monitor = DeviceMonitor(
            self.pool,
            config,
            FakeTransport(),
            alive=lambda *_: False,
            kill=self.kill,
            scan=self.scan,
            cgroup_root=self.root,
        )

    def scan(self, tag: str | None) -> dict[int, process_tree.Process]:
        self.scans += 1
        return {
            pid: process
            for pid, process in self.listing.items()
            if not process.tagged or tag == TAG
        }

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig, self.kill_file.read_text()))
        if sig == signal.SIGKILL and not self.stubborn:
            self.listing.pop(pid, None)

    def reconcile(self):
        return self.monitor.reconcile(1000.0, 600.0, lambda *_: None)


def test_a_finished_box_job_with_a_dead_runner_has_its_tree_killed_then_its_slot_released(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path, finished=True)

    result = box.reconcile()

    assert box.signals == [(CHILD, signal.SIGSTOP, "1"), (CHILD, signal.SIGKILL, "1")]
    assert result.stopped == (box.job_id,)
    assert result.released == (box.job_id,)
    assert box.pool.placement(box.job_id) is None


def test_a_running_box_jobs_tree_is_never_touched(tmp_path: Path) -> None:
    box = Box(tmp_path, finished=False)

    result = box.reconcile()

    assert box.signals == []
    assert box.scans == 0
    assert box.kill_file.read_text() == "0"
    assert result.released == () and result.stopped == ()
    assert box.pool.placement(box.job_id) is not None


def test_a_box_slot_is_kept_while_a_killed_member_survives_and_released_once_it_dies(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path, finished=True, stubborn=True)

    first = box.reconcile()
    assert first.stopped == (box.job_id,) and first.released == ()
    assert box.pool.placement(box.job_id) is not None

    box.stubborn = False
    box.signals.clear()
    second = box.reconcile()
    assert box.signals == [(CHILD, signal.SIGSTOP, "1"), (CHILD, signal.SIGKILL, "1")]
    assert second.released == (box.job_id,)
    assert box.pool.placement(box.job_id) is None


def test_a_box_slot_is_kept_and_nothing_signalled_when_processes_cannot_be_listed(
    tmp_path: Path,
) -> None:
    box = Box(tmp_path, finished=True)

    def unknown(_tag: str | None) -> dict[int, process_tree.Process]:
        raise process_tree.ProcessesUnknown("cannot list /proc")

    box.monitor.scan = unknown
    result = box.reconcile()

    assert box.signals == []
    assert result.released == ()
    assert box.pool.placement(box.job_id) is not None


@pytest.mark.parametrize("stubborn", [False, True])
def test_a_box_runs_kill_never_signals_a_process_outside_its_tag_scope_and_session(
    tmp_path: Path, stubborn: bool
) -> None:
    box = Box(tmp_path, finished=True, stubborn=stubborn)

    box.reconcile()
    box.reconcile()

    assert box.signals and all(pid != STRANGER for pid, _, _ in box.signals)


OTHER_RUN = "b" * 32
REUSED = 50
OTHER_CHILD = 51


class StartlessWorkerPidReused:
    def __init__(self, tmp_path: Path, *, orphaned: bool, run_tag: str | None = None):
        self.queue, self.pool, config, self.job_id = monitor_setup(tmp_path, "claude", box=True)
        self.pool.attach_runner(
            self.job_id, owner="claude", pid=RUNNER, start="r", alive=lambda *_: False
        )
        self.pool.attach_worker(
            self.job_id, owner="claude", pid=REUSED, start=None, run_tag=run_tag
        )
        if orphaned:
            with self.queue.connect() as connection:
                device_pool.orphan_slot(connection, self.job_id, 100.0)
        else:
            self.queue.finish(self.job_id, owner="claude", handoff_comment_id="c1")
        self.listing = {
            REUSED: process_tree.Process(REUSED, ppid=1, start="other", tagged=False, group=REUSED),
            OTHER_CHILD: process_tree.Process(
                OTHER_CHILD, ppid=REUSED, start="o", tagged=False, group=REUSED
            ),
        }
        self.signals: list[tuple[int, int]] = []
        self.monitor = DeviceMonitor(
            self.pool,
            config,
            FakeTransport(),
            alive=lambda pid, _start: pid in self.listing,
            kill=lambda pid, sig: self.signals.append((pid, sig)),
            scan=self.scan,
            cgroup_root=tmp_path / "cgroup",
        )


    def scan(self, tag: str | None) -> dict[int, process_tree.Process]:
        return {
            pid: replace(process, tagged=tag == OTHER_RUN and pid == OTHER_CHILD)
            for pid, process in self.listing.items()
        }


@pytest.mark.parametrize("orphaned", [False, True], ids=["finished-slot", "orphan"])
def test_a_recorded_worker_pid_without_its_start_never_names_a_kill_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphaned: bool
) -> None:
    monkeypatch.setattr(device_monitor, "run_tag", lambda pid: OTHER_RUN if pid == REUSED else None)
    box = StartlessWorkerPidReused(tmp_path, orphaned=orphaned)

    result = box.monitor.reconcile(1000.0, 600.0, lambda *_: None)

    assert box.signals == []
    assert result.released == ()
    if not orphaned:
        assert box.pool.placement(box.job_id) is not None


@pytest.mark.parametrize("orphaned", [False, True], ids=["finished-slot", "orphan"])
def test_a_recorded_worker_pid_without_its_start_adopts_no_process_it_is_responsible_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphaned: bool
) -> None:
    disowned = process_tree.Process(52, ppid=1, start="d", tagged=False, group=52)
    monkeypatch.setattr(device_monitor.sys, "platform", "darwin")
    monkeypatch.setattr(device_monitor, "_responsible_pid", lambda pid: REUSED)
    box = StartlessWorkerPidReused(tmp_path, orphaned=orphaned, run_tag=TAG)
    box.listing[disowned.pid] = disowned

    box.monitor.reconcile(1000.0, 600.0, lambda *_: None)

    assert box.signals == []
