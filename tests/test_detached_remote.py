"""#128: the detached start's scripts and the box-side follower.

The launch, poll and stop calls go through tests/fake_ssh.py (#112's link
faults) to real processes on this machine, which plays the device. The
follower's timing (backoff, grace) runs on an injected clock.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import remote
from lib.remote_follow import (
    EXITED,
    GONE,
    LOST,
    REFUSED,
    STOPPED,
    CHUNK_BYTES,
    Follower,
    backoff,
)
from tests.test_remote_darwin import Mac
from tests.test_remote_start import Ssh, gone, kill_quietly, use_fake_ssh, wait_file, wait_gone

ALIAS = "fakepc-codex-bot"

# A harness: counts its start, saves its pid and its stdin, prints `lines`
# lines `pause` seconds apart, then its handoff.
HARNESS = (
    "import os, sys, time\n"
    "count, lines, pause = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])\n"
    "with open(count, 'a') as f: f.write(f'{os.getpid()}\\n')\n"
    "brief = sys.stdin.buffer.read()\n"
    "with open(count + '.brief', 'wb') as f: f.write(brief)\n"
    "for i in range(lines):\n"
    "    print(f'line {i} ' + 'x' * 40, flush=True)\n"
    "    time.sleep(pause)\n"
    "print('ORCH-HANDOFF: 123456', flush=True)\n"
)


class Box:
    """One remote target, its fake ssh, and the harness's own files."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str | None = None):
        self.tmp = tmp_path
        self.ssh = Ssh(tmp_path)
        use_fake_ssh(monkeypatch, self.ssh)
        self.transport = remote.SshTransport()
        self.target = self.make_target(tag or remote.new_tag())
        self.count = tmp_path / "count"

    def make_target(self, tag: str) -> remote.RemoteTarget:
        return remote.RemoteTarget(
            device="pc", alias=ALIAS, user="codex-bot",
            workdir_root=str(self.tmp / "work"), job_id="job1", tag=tag,
        )

    def launch(
        self,
        lines: int = 3,
        pause: float = 0.05,
        *,
        target: remote.RemoteTarget | None = None,
        brief: bytes = b"the brief",
        not_after: int | None = None,
        command: list[str] | None = None,
    ) -> str:
        """The launch script for the harness (or command)."""
        t = target or self.target
        argv = command or [sys.executable, "-c", HARNESS, str(self.count), str(lines), str(pause)]
        start = remote.start_script(t, argv, {"ORCH_JOB_ID": t.job_id})
        deadline = not_after if not_after is not None else int(time.time()) + 600
        return remote.launch_script(t, start, size=len(brief), not_after=deadline)

    def send(self, script: str, brief: bytes = b"the brief", timeout: float = 30) -> remote.Exchanged:
        return self.transport.exchange(
            remote.ssh_argv(self.target, script), timeout=timeout, input=brief
        )

    def run_dir(self, target: remote.RemoteTarget | None = None) -> Path:
        t = target or self.target
        return Path(remote.run_dir(t)) / t.require_tag()

    def starts(self) -> list[int]:
        if not self.count.exists():
            return []
        return [int(line) for line in self.count.read_text(encoding="utf-8").split()]

    def poll(self, offset: int = 0, chunk: int = CHUNK_BYTES) -> tuple[list[str], bytes]:
        result = self.transport.exchange(
            remote.ssh_argv(self.target, remote.poll_script(self.target, offset, chunk)),
            timeout=30,
        )
        assert result.code == 0, result.stderr
        line = [x for x in result.stderr.splitlines() if x.startswith(remote.FOLLOW_MARK)][-1]
        return line.removeprefix(remote.FOLLOW_MARK).split(), result.stdout

    def wait_exit(self, target: remote.RemoteTarget | None = None) -> str:
        return wait_file(self.run_dir(target) / "exit")

    def cleanup(self) -> None:
        for pid in self.starts():
            kill_quietly(pid)
        for run in Path(self.tmp / "work" / "jobs").glob("*.run/*/pid"):
            for pid in run.read_text(encoding="utf-8").split():
                kill_quietly(int(pid))


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    made = Box(tmp_path, monkeypatch)
    yield made
    made.cleanup()


def word(result: remote.Exchanged) -> str:
    lines = [x for x in result.stdout.decode().splitlines() if x.startswith(remote.LAUNCH_MARK)]
    assert lines, (result.code, result.stdout, result.stderr)
    return lines[-1].removeprefix(remote.LAUNCH_MARK).strip()


class Stop:
    """A follower Control that never stops, or stops on request."""

    def __init__(self) -> None:
        self.signum: int | None = None

    def wait(self, seconds: float) -> None:
        time.sleep(min(seconds, 0.2))


# --- the launch -------------------------------------------------------------


def test_the_harness_runs_detached_and_writes_its_files_in_the_run_dir(box: Box) -> None:
    result = box.send(box.launch(lines=20, pause=0.1))
    assert result.code == 0 and word(result) == "started"
    harness = int(wait_file(box.count).split()[0])
    # The launch call has returned; the harness runs on in a session of its own.
    assert not gone(harness)
    wrapper, start = (int(x) for x in (box.run_dir() / "pid").read_text().split())
    assert os.getsid(start) == start and os.getsid(wrapper) == wrapper
    assert os.getsid(harness) == start
    assert box.wait_exit() == "0"
    files = sorted(path.name for path in box.run_dir().iterdir())
    assert files == ["exit", "handoff", "log", "pid", "stdin"]
    assert (box.run_dir() / "handoff").read_text() == "ORCH-HANDOFF: 123456\n"
    log = (box.run_dir() / "log").read_text()
    assert log.startswith("line 0 ") and log.endswith("ORCH-HANDOFF: 123456\n")
    assert (box.run_dir() / "stdin").read_bytes() == b"the brief"
    assert Path(f"{box.count}.brief").read_bytes() == b"the brief"
    assert (box.run_dir() / "stdin").stat().st_mode & 0o077 == 0
    current = Path(remote.run_dir(box.target)) / "current"
    assert current.read_text().strip() == box.target.tag


def test_a_relaunch_of_the_same_tag_starts_nothing(box: Box) -> None:
    script = box.launch(lines=10, pause=0.1)
    assert word(box.send(script)) == "started"
    assert word(box.send(script)) == "already"
    box.wait_exit()
    assert word(box.send(script)) == "already"
    assert len(box.starts()) == 1


def test_a_launch_after_its_deadline_is_refused_and_runs_nothing(box: Box) -> None:
    result = box.send(box.launch(not_after=int(time.time()) - 1))
    assert word(result) == "refused late"
    assert not box.run_dir().exists()
    assert box.starts() == []


def test_a_short_brief_is_not_taken(box: Box) -> None:
    result = box.send(box.launch(brief=b"the whole brief"), brief=b"the who")
    assert word(result) == "short"
    assert not box.run_dir().exists()
    assert box.starts() == []
    assert word(box.send(box.launch(brief=b"the whole brief"), brief=b"the whole brief")) == "started"


def test_a_second_tag_of_the_job_is_busy_while_the_first_runs(box: Box) -> None:
    assert word(box.send(box.launch(lines=50, pause=0.1))) == "started"
    wait_file(box.count)
    other = box.make_target(remote.new_tag())
    result = box.transport.exchange(
        remote.ssh_argv(other, box.launch(target=other)), timeout=30, input=b"the brief"
    )
    assert word(result) == "busy"
    assert len(box.starts()) == 1
    assert (box.run_dir(other) / "exit").read_text().strip() == "125"
    current = Path(remote.run_dir(box.target)) / "current"
    assert current.read_text().strip() == box.target.tag


def test_a_new_tag_may_start_once_the_first_has_exited(box: Box) -> None:
    assert word(box.send(box.launch())) == "started"
    box.wait_exit()
    other = box.make_target(remote.new_tag())
    result = box.transport.exchange(
        remote.ssh_argv(other, box.launch(target=other)), timeout=30, input=b"the brief"
    )
    assert word(result) == "started"
    box.wait_exit(other)
    assert len(box.starts()) == 2


def test_without_setsid_the_launch_detaches_with_perl(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "no-setsid"
    bare.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            link = bare / name
            if name != "setsid" and not link.exists():
                link.symlink_to(os.path.join(directory, name))
    assert not (bare / "setsid").exists() and (bare / "perl").exists()
    monkeypatch.setenv("FAKE_SSH_PATH", str(bare))
    result = box.send(box.launch(lines=10, pause=0.1))
    assert word(result) == "started"
    wrapper, start = (int(x) for x in (box.run_dir() / "pid").read_text().split())
    assert os.getsid(start) == start and os.getsid(wrapper) == wrapper
    assert box.wait_exit() == "0"
    assert len(box.starts()) == 1


def test_darwin_launch_records_the_start_and_polls_it(
    box: Box, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = Mac(tmp_path)
    monkeypatch.setenv("PATH", mac.path(os.environ["PATH"]))
    assert word(box.send(box.launch(lines=30, pause=0.1))) == "started"
    wrapper, start = (int(x) for x in (box.run_dir() / "pid").read_text().split())
    record = Path(box.target.job_dir) / remote.darwin_record(box.target.require_tag())
    fields = wait_file(record).split()
    assert fields[0] == str(start) and fields[2] == box.target.tag
    assert box.poll()[0][1] == "running"
    assert box.wait_exit() == "0"
    head, data = box.poll()
    assert head[1:] == ["exited", "0"] and data.endswith(b"ORCH-HANDOFF: 123456\n")


# --- the poll ---------------------------------------------------------------


def test_poll_reports_none_for_a_tag_never_launched(box: Box) -> None:
    assert box.poll() == (["0", "none"], b"")


def test_poll_reads_exact_slices_of_the_log_and_the_exit(box: Box) -> None:
    assert word(box.send(box.launch(lines=2, pause=0))) == "started"
    box.wait_exit()
    whole = (box.run_dir() / "log").read_bytes()
    head, data = box.poll(0, 10)
    assert head == [str(len(whole)), "exited", "0"] and data == whole[:10]
    head, data = box.poll(10, 10_000)
    assert data == whole[10:]
    assert box.poll(len(whole))[1] == b""


def test_poll_reports_running_then_exited(box: Box) -> None:
    assert word(box.send(box.launch(lines=40, pause=0.1))) == "started"
    assert box.poll()[0][1] == "running"
    box.wait_exit()
    assert box.poll()[0][1:] == ["exited", "0"]


def test_poll_reports_gone_for_a_killed_worker_with_no_exit(box: Box) -> None:
    assert word(box.send(box.launch(lines=100, pause=0.1))) == "started"
    wrapper, start = (int(x) for x in (box.run_dir() / "pid").read_text().split())
    os.kill(wrapper, signal.SIGKILL)
    os.killpg(start, signal.SIGKILL)
    assert wait_gone(wrapper) and wait_gone(start)
    deadline = time.monotonic() + 10
    while box.poll()[0][1] != "gone":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert not (box.run_dir() / "exit").exists()


def test_poll_is_unsure_while_the_start_has_not_recorded_itself(box: Box) -> None:
    box.run_dir().mkdir(parents=True)
    assert box.poll()[0][1] == "unsure"


def test_stop_reaches_the_detached_worker_and_the_wrapper_records_it(box: Box) -> None:
    assert word(box.send(box.launch(lines=100, pause=0.1))) == "started"
    harness = int(wait_file(box.count).split()[0])
    assert remote.stop_worker(box.transport, box.target)
    assert wait_gone(harness)
    assert box.wait_exit() == str(128 + signal.SIGTERM)
    assert remote.check_worker(box.transport, box.target) == remote.GONE


# --- the follower: backoff, grace, partial transfers ------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Script:
    """A transport that answers each call from a list (then repeats the last)."""

    def __init__(self, answers: list[remote.Exchanged]):
        self.answers = answers
        self.calls: list[str] = []

    def exchange(self, argv, *, timeout, input=b""):
        self.calls.append(argv[-1])
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def run(self, argv, *, timeout):
        self.calls.append(argv[-1])
        return 0


def launched(word_: str = "started") -> remote.Exchanged:
    return remote.Exchanged(0, f"{remote.LAUNCH_MARK} {word_}\n".encode(), "")


def polled(size: int, state: str, data: bytes = b"") -> remote.Exchanged:
    return remote.Exchanged(0, data, f"{remote.FOLLOW_MARK} {size} {state}\n")


DROP = remote.Exchanged(255, b"", "")


def follower(transport, clock: Clock, sink, grace: float = 600.0) -> Follower:
    target = remote.RemoteTarget(
        device="pc", alias=ALIAS, user="codex-bot", workdir_root="/w", job_id="job1",
        tag="0" * 32,
    )
    return Follower(transport, target, sink, log=lambda _: None, grace=grace,
                    clock=clock, sleep=clock.sleep)


def test_backoff_doubles_from_two_seconds_to_a_minute() -> None:
    assert [backoff(n) for n in range(8)] == [2, 4, 8, 16, 32, 60, 60, 60]


def test_launch_retries_a_dropped_call_with_backoff_then_follows(tmp_path: Path) -> None:
    clock = Clock()
    sink = (tmp_path / "out").open("wb")
    transport = Script([DROP, DROP, DROP, launched("already"), polled(3, "exited 0", b"ok\n")])
    end = follower(transport, clock, sink).run("LAUNCH", b"", Stop())
    sink.close()
    assert (end.kind, end.code) == (EXITED, 0)
    assert clock.slept == [2, 4, 8]
    assert transport.calls[:4] == ["LAUNCH"] * 4
    assert (tmp_path / "out").read_bytes() == b"ok\n"


def test_a_refused_launch_ends_without_a_follow(tmp_path: Path) -> None:
    clock = Clock()
    transport = Script([launched("refused late")])
    end = follower(transport, clock, (tmp_path / "out").open("wb")).run("LAUNCH", b"", Stop())
    assert (end.kind, end.code) == (REFUSED, remote.START_REFUSED)
    assert transport.calls == ["LAUNCH"]


def test_a_partial_transfer_is_discarded_and_fetched_again(tmp_path: Path) -> None:
    clock = Clock()
    sink_path = tmp_path / "out"
    sink = sink_path.open("wb")
    transport = Script([
        launched(),
        polled(10, "running", b"0123"),          # cut short: 10 bytes promised
        remote.Exchanged(255, b"0123456789", f"{remote.FOLLOW_MARK} 10 running\n"),
        polled(10, "running", b"0123456789"),
        polled(12, "exited 0", b"ab"),
    ])
    end = follower(transport, clock, sink).run("LAUNCH", b"", Stop())
    sink.close()
    assert end.kind == EXITED
    assert sink_path.read_bytes() == b"0123456789ab"


def test_drops_shorter_than_the_grace_window_never_end_the_follow(tmp_path: Path) -> None:
    clock = Clock()
    transport = Script([launched(), polled(0, "running"), *[DROP] * 9, polled(0, "exited 3")])
    end = follower(transport, clock, (tmp_path / "out").open("wb"), grace=600).run(
        "LAUNCH", b"", Stop()
    )
    assert (end.kind, end.code) == (EXITED, 3)
    assert sum(clock.slept) < 600


def test_no_answer_for_the_grace_window_is_lost(tmp_path: Path) -> None:
    clock = Clock()
    transport = Script([launched(), polled(0, "running"), DROP])
    end = follower(transport, clock, (tmp_path / "out").open("wb"), grace=300).run(
        "LAUNCH", b"", Stop()
    )
    assert end.kind == LOST
    assert 300 <= clock.now - 1000 < 300 + 61


def test_an_unsure_device_does_not_hold_the_follow_forever(tmp_path: Path) -> None:
    clock = Clock()
    transport = Script([launched(), polled(0, "unsure")])
    end = follower(transport, clock, (tmp_path / "out").open("wb"), grace=60).run(
        "LAUNCH", b"", Stop()
    )
    assert end.kind == LOST


def test_a_worker_gone_without_an_exit_status_ends_as_gone(tmp_path: Path) -> None:
    clock = Clock()
    transport = Script([launched(), polled(0, "running"), polled(0, "gone")])
    end = follower(transport, clock, (tmp_path / "out").open("wb")).run("LAUNCH", b"", Stop())
    assert end.kind == GONE


def test_a_cancel_stops_the_worker_and_follows_it_until_it_exits(tmp_path: Path) -> None:
    clock = Clock()
    control = Stop()
    control.signum = signal.SIGTERM
    transport = Script([launched(), polled(0, "running"), polled(0, "exited 143")])
    end = follower(transport, clock, (tmp_path / "out").open("wb")).run("LAUNCH", b"", control)
    assert (end.kind, end.code) == (STOPPED, 143)
    assert any("kill -TERM" in call for call in transport.calls)


# --- the follower over the fake ssh: drops and late delivery ----------------


def test_follow_through_dropped_polls_relays_the_log_byte_for_byte(
    box: Box, tmp_path: Path
) -> None:
    sink_path = tmp_path / "relayed"
    script = box.launch(lines=40, pause=0.05)
    drop = None

    class DropMidway(Stop):
        def wait(self, seconds: float) -> None:
            nonlocal drop
            if drop is None and box.count.exists():
                drop = box.ssh.fault(ALIAS, "drop-after", 0)
            elif drop is not None and drop.exists() and len(box.ssh.calls()) > 8:
                drop.unlink()
            time.sleep(min(seconds, 0.2))

    with sink_path.open("wb") as sink:
        end = Follower(box.transport, box.target, sink, log=lambda _: None, grace=120,
                       sleep=lambda s: time.sleep(min(s, 0.2))).run(
            script, b"the brief", DropMidway()
        )
    assert (end.kind, end.code) == (EXITED, 0)
    assert len(box.starts()) == 1
    assert sink_path.read_bytes() == (box.run_dir() / "log").read_bytes()
    assert sink_path.read_bytes().endswith(b"ORCH-HANDOFF: 123456\n")


def test_a_late_launch_delivery_never_starts_a_second_harness(box: Box, tmp_path: Path) -> None:
    """Done-when: the first launch reaches the device after the box gave up on it."""
    late = box.ssh.fault(ALIAS, "delay-detached", 3)
    script = box.launch(lines=20, pause=0.1)
    calls: list[int] = []

    class Transport(remote.SshTransport):
        def exchange(self, argv, *, timeout, input=b""):
            calls.append(1)
            if len(calls) == 2:
                late.unlink()  # the retry goes through at once
            return super().exchange(argv, timeout=timeout, input=input)

    with (tmp_path / "relayed").open("wb") as sink:
        end = Follower(Transport(), box.target, sink, log=lambda _: None, grace=120,
                       call_timeout=1, sleep=lambda s: time.sleep(min(s, 0.2))).run(
            script, b"the brief", Stop()
        )
    assert (end.kind, end.code) == (EXITED, 0)
    time.sleep(3)  # the late copy of the first launch has arrived by now
    assert len(box.starts()) == 1
    assert (tmp_path / "relayed").read_bytes().endswith(b"ORCH-HANDOFF: 123456\n")


def test_a_late_launch_past_its_deadline_starts_nothing(box: Box) -> None:
    box.ssh.fault(ALIAS, "delay-detached", 2)
    script = box.launch(not_after=int(time.time()) + 1)
    result = box.send(script, timeout=0.5)
    assert result.code == 255  # the box gave up on the call
    time.sleep(3)
    assert box.starts() == []
    assert not box.run_dir().exists()
