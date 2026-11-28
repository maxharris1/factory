"""Poll health follows the poller's own poll_seconds (30 or 60).

status.json's `poller.health.cycle_seconds` and its ok / late thresholds
(`2P + 30`, `5P`) come from orch.yml's poll_seconds, which `orch poll` passes
to each dashboard push as `--poll-seconds`. A 60-second poller reads exactly
as before; a 30-second poller is flagged late on its own, shorter cycle.
"""

import json
import sys
from pathlib import Path

import pytest

from lib import cli, dashboard_cli
from lib.dashboard import DashboardError, collect_snapshot, health_thresholds
from lib.dashboard_config import PushConfig
from lib.dashboard_push import LivePusher, PollPusher, poll_pusher_for
from tests.test_dashboard import NOW, seeded

ROOT = Path(__file__).resolve().parents[1]
DB = Path("/state/orch.db")
SSH_PUSH = PushConfig(
    transport="ssh",
    host="pc-wsl-cursor-bot",
    ssh_config="/home/box/.ssh/config",
    remote_dir="factory-dashboard",
    method="scp",
)
LIVE_PUSH = PushConfig(**{**SSH_PUSH.__dict__, "live": True})


def _health(tmp_path: Path, ago: int | None, **kwargs) -> dict:
    db, log = seeded(tmp_path)
    last = None if ago is None else NOW - ago
    return collect_snapshot(db, log, now=NOW, last_cycle_at=last, **kwargs)["poller"]["health"]


# --- collect_snapshot ----------------------------------------------------------------


def test_thresholds_scale_with_the_poll_interval() -> None:
    assert health_thresholds(30) == (90, 150)
    assert health_thresholds(60) == (150, 300)


@pytest.mark.parametrize(
    ("ago", "state"),
    [(None, "unknown"), (5, "ok"), (90, "ok"), (91, "late"), (150, "late"), (151, "stale")],
)
def test_a_30_second_poller_reports_cycle_seconds_30_and_its_own_thresholds(
    tmp_path: Path, ago, state
) -> None:
    assert _health(tmp_path, ago, poll_seconds=30) == {
        "state": state,
        "cycle_seconds": 30,
        "last_cycle_age_seconds": ago,
        "last_activity_age_seconds": 10,
    }


def test_a_30_second_poller_is_late_where_a_60_second_one_is_still_ok(tmp_path: Path) -> None:
    # 120 s since the last cycle: four 30 s cycles missed, two 60 s ones.
    assert _health(tmp_path / "a", 120, poll_seconds=30)["state"] == "late"
    assert _health(tmp_path / "b", 120, poll_seconds=60)["state"] == "ok"


@pytest.mark.parametrize(
    ("ago", "state"),
    [(None, "unknown"), (5, "ok"), (150, "ok"), (151, "late"), (300, "late"), (301, "stale")],
)
@pytest.mark.parametrize("explicit", [True, False])
def test_a_60_second_poller_is_unchanged(tmp_path: Path, ago, state, explicit) -> None:
    kwargs = {"poll_seconds": 60} if explicit else {}
    assert _health(tmp_path, ago, **kwargs) == {
        "state": state,
        "cycle_seconds": 60,
        "last_cycle_age_seconds": ago,
        "last_activity_age_seconds": 10,
    }


@pytest.mark.parametrize("bad", [0, 15, 45, 90, True, "30", 30.0])
def test_other_poll_intervals_are_refused(tmp_path: Path, bad) -> None:
    db, log = seeded(tmp_path)
    with pytest.raises(DashboardError, match="poll_seconds"):
        collect_snapshot(db, log, now=NOW, poll_seconds=bad)


# --- the push path: orch poll -> PollPusher/LivePusher -> orch dashboard push ---------


def _pusher(config: PushConfig, **kwargs):
    spawned: list[list[str]] = []

    class Child:
        returncode = 0

        def poll(self) -> int:
            return 0

    def spawn(argv, **_):
        spawned.append(list(argv))
        return Child()

    pusher = poll_pusher_for(
        config, db=DB, orch=None, environ={}, spawn=spawn, log=lambda _: None, **kwargs
    )
    return pusher, spawned


@pytest.mark.parametrize("seconds", [30, 60])
def test_the_poll_push_passes_the_poll_interval(seconds: int) -> None:
    pusher, spawned = _pusher(SSH_PUSH, poll_seconds=seconds)
    assert isinstance(pusher, PollPusher) and not isinstance(pusher, LivePusher)
    pusher()
    argv = spawned[0]
    assert argv[argv.index("--poll-seconds") + 1] == str(seconds)


@pytest.mark.parametrize("seconds", [30, 60])
def test_live_pushes_pass_the_poll_interval_on_every_status_push(seconds: int) -> None:
    now = [1000.0]
    commits = [0]
    pusher, spawned = _pusher(
        LIVE_PUSH,
        poll_seconds=seconds,
        clock=lambda: now[0],
        wall=lambda: float(NOW),
        fingerprint=lambda _db: commits[0],
    )
    assert isinstance(pusher, LivePusher)
    pusher()  # a cycle end: the first push is the full set, with --last-cycle
    assert len(spawned) == 1 and "--status-only" not in spawned[0]
    commits[0] += 1  # a database change seen by the watcher
    pusher.tick()
    assert len(spawned) == 1
    now[0] += pusher.min_interval + 0.5  # past the throttle
    pusher.tick()
    assert len(spawned) == 2
    status = spawned[1]
    assert "--status-only" in status
    assert status[status.index("--poll-seconds") + 1] == str(seconds)
    for argv in spawned:
        assert argv[argv.index("--poll-seconds") + 1] == str(seconds)


def test_without_a_poll_interval_the_child_reads_orch_yml() -> None:
    pusher, spawned = _pusher(SSH_PUSH)
    pusher()
    assert "--poll-seconds" not in spawned[0]


def _orch(tmp_path: Path, seconds: int) -> Path:
    orch = tmp_path / "orch.yml"
    orch.write_text(
        f"poll_seconds: {seconds}\nprojects:\n  - repo: maxharris1/factory\n"
        "dashboard:\n  push:\n    transport: ssh\n    host: pc-wsl-cursor-bot\n"
        "    remote_dir: factory-dashboard\n    method: scp\n    live: true\n",
        encoding="utf-8",
    )
    return orch


@pytest.mark.parametrize("seconds", [30, 60])
def test_orch_poll_hands_its_orch_yml_poll_seconds_to_the_pusher(
    tmp_path: Path, monkeypatch, seconds: int
) -> None:
    orch = _orch(tmp_path, seconds)
    database = tmp_path / "orch.db"
    database.write_bytes(b"")
    seen: dict = {}

    class Loop:
        def __init__(self, dispatcher, source, **_kwargs) -> None:
            pass

        def run(self, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.delenv("ORCH_DB", raising=False)
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(cli, "_runtime", lambda path, orch, create, **_: (object(), object()))
    monkeypatch.setattr(cli, "PollLoop", Loop)
    code = cli.main(["poll", "--db", str(database), "--orch", str(orch), "--once",
                     "--pidfile", str(tmp_path / "poller.pid")])
    assert code == 0
    argv = seen["after_poll"].pusher.argv
    assert argv[:5] == [sys.executable, str(ROOT / "bin" / "orch"), "dashboard", "push", "--once"]
    assert argv[argv.index("--poll-seconds") + 1] == str(seconds)


class _Capture:
    """Stands in for the ssh transport: keeps the status.json it was sent."""

    def __init__(self) -> None:
        self.status: list[dict] = []

    def send(self, paths) -> None:
        self.status.append(json.loads(Path(paths[0]).read_text(encoding="utf-8")))


def _push(tmp_path: Path, monkeypatch, orch_seconds: int, *flags: str) -> dict:
    db, log = seeded(tmp_path)
    capture = _Capture()
    monkeypatch.setattr(dashboard_cli, "transport_for", lambda config: capture)
    monkeypatch.setattr(dashboard_cli.time, "time", lambda: NOW)
    code = dashboard_cli.main(
        ["push", "--once", "--status-only", "--db", str(db), "--log", str(log),
         "--out", str(tmp_path / "site"), "--orch", str(_orch(tmp_path, orch_seconds)),
         "--machines", str(tmp_path / "no-machines.yml"), *flags]
    )
    assert code == 0
    assert len(capture.status) == 1
    return capture.status[0]["poller"]["health"]


def test_a_30_second_poller_push_reports_30_and_is_late_at_120_seconds(
    tmp_path: Path, monkeypatch
) -> None:
    health = _push(
        tmp_path, monkeypatch, 30, "--last-cycle", str(NOW - 120), "--poll-seconds", "30"
    )
    assert health["cycle_seconds"] == 30
    assert health["state"] == "late"


def test_a_60_second_poller_push_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    health = _push(
        tmp_path, monkeypatch, 60, "--last-cycle", str(NOW - 120), "--poll-seconds", "60"
    )
    assert health["cycle_seconds"] == 60
    assert health["state"] == "ok"


def test_the_push_falls_back_to_orch_yml_poll_seconds(tmp_path: Path, monkeypatch) -> None:
    health = _push(tmp_path, monkeypatch, 30, "--last-cycle", str(NOW - 120))
    assert health["cycle_seconds"] == 30
    assert health["state"] == "late"


def test_the_running_pollers_interval_wins_over_an_edited_orch_yml(
    tmp_path: Path, monkeypatch
) -> None:
    # orch.yml now says 60, but the running poller started at 30 and says so.
    health = _push(
        tmp_path, monkeypatch, 60, "--last-cycle", str(NOW - 120), "--poll-seconds", "30"
    )
    assert health["cycle_seconds"] == 30
    assert health["state"] == "late"


def test_push_refuses_another_poll_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        dashboard_cli.main(["push", "--once", "--poll-seconds", "45",
                            "--db", str(tmp_path / "x.db")])
