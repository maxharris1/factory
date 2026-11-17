"""Component heartbeats and the Health tab (issue #92).

Fakes only: heartbeats go to a temp health database beside a temp orch.db,
device probes use a fake transport, the tunnel probe a fake fetch, and no
thread sleeps for real.
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import health
from lib.cli import main as cli_main
from lib.dashboard import collect_snapshot
from lib.dashboard_page import NAV_PAGES, render_pages
from lib.machines_yml import load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE, Exchanged
from lib.status import main as status_main
from tests.test_wait import restore_signals  # noqa: F401  (fixture)

NOW = 1_794_000_000.0  # Nov 6, 2026, 4:20 PM EST
PAGE_NOW = 1_800_000_000.0  # the page harness's clock is 3 s past this
DEVICES = frozenset({"box", "pc", "mac"})
ORCH_EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "orch.example.yml"
MACHINES = """\
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses:
      claude: claude-bot
      codex: codex-bot
      cursor: cursor-bot
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses:
      codex: codex-bot
    workdir_root: /home/{bot}/factory-work
    ssh_alias: pc-wsl-{bot}
    factory_root: /home/{bot}/factory
  - name: mac
    enabled: false
    tailscale_name: mac.example.ts.net
    slot_max: 2
    harnesses:
      claude: claude-bot
    workdir_root: /Users/{bot}/factory-work
    ssh_alias: mac-{bot}
    factory_root: /Users/{bot}/factory
"""


def make_db(tmp_path: Path, now: float = NOW) -> tuple[Path, SQLiteJobQueue]:
    path = tmp_path / "orch.db"
    return path, SQLiteJobQueue(path, clock=lambda: now - 30)


def machines_file(tmp_path: Path, text: str = MACHINES) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(text, encoding="utf-8")
    return path


def request(recipient: str, ticket: str = "42") -> JobRequest:
    return JobRequest(
        repo="owner/repo",
        ticket_id=ticket,
        action="build",
        recipient=recipient,
        source_comment_id=f"go-{recipient}-{ticket}",
        timeout_seconds=3600,
        head="3f9c2ab",
    )


def store(db: Path, clock: float = NOW) -> health.HealthStore:
    return health.HealthStore(db, devices=DEVICES, clock=lambda: clock)


TUNNEL_URL = "https://box.example.ts.net/linear-webhook"


def orch(*, listener: bool = False, push: bool = False, public_url: str | None = TUNNEL_URL,
         refused: tuple[str, ...] = (), alert_minutes: int = 30) -> SimpleNamespace:
    """What lib/health.py, and the snapshot's projects (#152), read of an OrchConfig."""
    return SimpleNamespace(
        projects=(),
        linear_webhook=SimpleNamespace(public_url=public_url, tunnel_alert_minutes=alert_minutes)
        if listener else None,
        dashboard=SimpleNamespace(push=SimpleNamespace(transport="ssh" if push else "none")),
        refused_harnesses=refused,
    )


def machines_config(tmp_path: Path, text: str = MACHINES):
    config, _ = load_machines_yml(machines_file(tmp_path, text), allow_placeholders=True)
    return config


def row(**overrides) -> health.Heartbeat:
    values = dict(
        component="waiter",
        device="box",
        bot="codex-bot",
        pid=4242,
        started_at=NOW - 600,
        last_seen=NOW,
        version="0123456789ab",
        interval_seconds=15.0,
        stopped_at=None,
        checked_at=None,
    )
    values.update(overrides)
    return health.Heartbeat(**values)


# --- thresholds -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age, expected",
    [
        (0, "up"),
        (44, "up"),
        (45, "up"),  # three 15 s beats
        (46, "stale"),
        (119, "stale"),
        (120, "stale"),  # eight beats
        (121, "down"),
        (86_400, "down"),
    ],
)
def test_stale_and_down_thresholds_follow_the_beat_interval(age: int, expected: str) -> None:
    assert health.STALE_BEATS == 3
    assert health.DOWN_BEATS == 8
    assert health.BEAT_SECONDS == 15.0
    state, _ = health.classify(row(last_seen=NOW - age), NOW)
    assert state == expected


def test_thresholds_scale_with_a_slower_component() -> None:
    probe = row(component="device-probe", bot="", interval_seconds=60.0)
    assert health.classify(probe._replace(last_seen=NOW - 180), NOW)[0] == "up"
    assert health.classify(probe._replace(last_seen=NOW - 181), NOW)[0] == "stale"
    assert health.classify(probe._replace(last_seen=NOW - 480), NOW)[0] == "stale"
    assert health.classify(probe._replace(last_seen=NOW - 481), NOW)[0] == "down"


def test_a_cleanly_stopped_component_is_down_at_once() -> None:
    state, reason = health.classify(row(last_seen=NOW - 2, stopped_at=NOW - 1), NOW)
    assert (state, reason) == ("down", "stopped")


def test_a_component_never_seen_is_down() -> None:
    assert health.classify(row(last_seen=None), NOW) == ("down", "never seen")


def test_a_failed_latest_probe_is_at_least_stale() -> None:
    probe = row(component="device-probe", bot="", interval_seconds=60.0,
                last_seen=NOW - 30, checked_at=NOW - 5)
    state, reason = health.classify(probe, NOW)
    assert state == "stale"
    assert "unreachable" in reason


# --- writing heartbeats -------------------------------------------------------------------


def test_beat_writes_one_row_per_component_device_and_bot(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    writer = store(db)
    writer.beat("waiter", "box", bot="codex-bot", pid=11, started_at=NOW - 60, version="abcdef012345")
    writer.beat("waiter", "box", bot="codex-bot", pid=11, started_at=NOW - 60, version="abcdef012345")
    writer.beat("poller", "box", pid=12, started_at=NOW - 5, version="unknown")
    rows = health.read_heartbeats(db)
    assert rows is not None
    assert sorted((r.component, r.device, r.bot, r.pid) for r in rows) == [
        ("poller", "box", "", 12),
        ("waiter", "box", "codex-bot", 11),
    ]
    waiter = next(r for r in rows if r.component == "waiter")
    assert waiter.last_seen == NOW
    assert waiter.started_at == NOW - 60
    assert waiter.interval_seconds == health.BEAT_SECONDS


def test_heartbeats_live_beside_orch_db_not_in_it(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db).beat("poller", "box", pid=1, started_at=NOW, version="unknown")
    assert health.health_path(db) == tmp_path / "orch.health.db"
    assert health.health_path(db).is_file()
    with sqlite3.connect(db) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert health.TABLE not in tables


def test_the_health_db_copies_orch_db_permissions(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    db.chmod(0o660)
    store(db).beat("poller", "box", pid=1, started_at=NOW, version="unknown")
    mode = stat.S_IMODE(health.health_path(db).stat().st_mode)
    assert mode == 0o660


def test_no_health_db_reads_as_unknown(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    assert health.read_heartbeats(db) is None
    report = health.build_report(None, NOW, queued=[("j1", "codex")])
    assert report.known is False
    assert report.components == ()
    assert report.no_waiter == ()


SECRETS = {
    "FACTORY_WEBHOOK_URL": "https://hooks.example.test/s3cr3t-webhook-path",
    "FACTORY_WEBHOOK_KEY": "whk_0123456789abcdefSECRET",
    "GH_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "LINEAR_API_KEY": "lin_api_SECRETSECRETSECRET",
}


def test_no_secrets_in_heartbeat_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    db, _ = make_db(tmp_path)
    beater = health.Beater(db, "waiter", devices=["box"], bot="codex-bot",
                           store=store(db), log=lambda line: None)
    beater.beat_now()
    beater.close()
    store(db).beat("poller", "box", pid=os.getpid(), started_at=NOW, version=health.factory_version())

    with sqlite3.connect(health.health_path(db)) as connection:
        columns = [r[1] for r in connection.execute(f"PRAGMA table_info({health.TABLE})")]
        values = connection.execute(f"SELECT * FROM {health.TABLE}").fetchall()
    assert columns == [
        "component", "device", "bot", "pid", "started_at", "last_seen",
        "version", "interval_seconds", "stopped_at", "checked_at",
        "cycle_started_at", "last_cycle_seconds", "seen_boot", "seen_monotonic",
        "checked_boot", "down_since",
    ]
    for record in values:
        (component, device, bot, pid, started, seen, version, interval, stopped, checked,
         cycle_started, last_cycle, seen_boot, seen_monotonic, checked_boot, down_since) = record
        assert seen_boot is None or health._BOOT.fullmatch(seen_boot)
        assert checked_boot is None or health._BOOT.fullmatch(checked_boot)
        assert seen_monotonic is None or isinstance(seen_monotonic, float)
        assert component in health.COMPONENTS
        assert health.TOKEN.fullmatch(device)
        assert bot in ("", "claude-bot", "codex-bot", "cursor-bot")
        assert isinstance(pid, int)
        assert health.VERSION.fullmatch(version)
        for number in (started, seen, interval, stopped, checked, cycle_started, last_cycle,
                       down_since):
            assert number is None or isinstance(number, float)
    path = health.health_path(db)
    wal = Path(f"{path}-wal")
    raw = path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    for value in SECRETS.values():
        assert value.encode() not in raw


@pytest.mark.parametrize(
    "field, value",
    [
        ("component", "poller; DROP TABLE x"),
        ("component", "token=abc"),
        ("device", "user@host"),
        ("device", "-oProxyCommand=x"),
        ("device", "a" * 65),
        ("bot", "grok-bot"),
        ("version", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("version", "0123456789abcdef0123456789abcdef01234567"),
        ("device", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("device", "retired"),
        ("pid", 0),
        ("pid", None),
        ("interval", 0.5),
    ],
)
def test_beat_refuses_anything_outside_the_allowlist(tmp_path: Path, field: str, value: object) -> None:
    db, _ = make_db(tmp_path)
    args: dict = dict(component="waiter", device="box", bot="codex-bot", pid=1,
                      started_at=NOW, version="unknown", interval=15.0)
    args[field] = value
    component = args.pop("component")
    device = args.pop("device")
    with pytest.raises(health.HealthError):
        store(db).beat(component, device, **args)
    assert health.read_heartbeats(db) in (None, ())


def test_factory_version_is_a_short_sha_or_unknown(tmp_path: Path) -> None:
    assert health.VERSION.fullmatch(health.factory_version())
    assert health.factory_version(tmp_path) == "unknown"
    git = tmp_path / "repo" / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert health.factory_version(tmp_path / "repo") == "0123456789ab"


# --- the beat thread ------------------------------------------------------------------------


def test_beater_beats_every_device_and_marks_stopped_on_exit(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    clock = [NOW - 100]
    writer = health.HealthStore(db, devices=DEVICES, clock=lambda: clock[0])
    beater = health.Beater(db, "waiter", devices=["box", "pc"], bot="codex-bot",
                           store=writer, pid=77, log=lambda line: None)
    beater.beat_now()
    clock[0] = NOW
    beater.beat_now()
    rows = {r.device: r for r in health.read_heartbeats(db) or ()}
    assert set(rows) == {"box", "pc"}
    assert all(r.last_seen == NOW and r.started_at == NOW - 100 and r.pid == 77 for r in rows.values())
    beater.close()
    rows = {r.device: r for r in health.read_heartbeats(db) or ()}
    assert all(r.stopped_at == NOW for r in rows.values())
    beater.beat_now()  # after close nothing more is written
    assert {r.device: r.stopped_at for r in health.read_heartbeats(db) or ()} == {"box": NOW, "pc": NOW}


def test_a_plain_waiter_removes_its_row_on_exit(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    beater = health.Beater(db, "waiter", devices=["box"], bot="claude-bot",
                           store=store(db), on_exit="delete", log=lambda line: None)
    beater.beat_now()
    assert len(health.read_heartbeats(db) or ()) == 1
    beater.close()
    assert health.read_heartbeats(db) == ()


def test_a_failed_beat_logs_once_and_never_raises(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    lines: list[str] = []

    class Broken(health.HealthStore):
        def beat(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    beater = health.Beater(db, "poller", devices=["box"], store=Broken(db, devices=DEVICES), log=lines.append)
    beater.beat_now()
    beater.beat_now()
    assert lines == ["health: poller heartbeat failed: OperationalError"]


def test_the_beat_thread_runs_and_stops(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    with health.Beater(db, "linear-listener", devices=["box"], store=store(db),
                       interval=60.0, log=lambda line: None):
        deadline = time.monotonic() + 5
        while not health.read_heartbeats(db) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [r.component for r in health.read_heartbeats(db) or ()] == ["linear-listener"]
    assert all(r.stopped_at is not None for r in health.read_heartbeats(db) or ())


# --- the device SSH probe ------------------------------------------------------------------


class FakeTransport:
    def __init__(self, codes: dict[str, int]):
        self.codes = codes
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout: float) -> int:
        argv = list(argv)
        self.calls.append(argv)
        alias = argv[argv.index("--") + 1]
        return self.codes.get(alias, 0)


def test_probe_targets_are_enabled_remote_devices_only(tmp_path: Path) -> None:
    machines, _ = load_machines_yml(machines_file(tmp_path), allow_placeholders=True)
    targets = health.probe_targets(machines)
    assert [(t.device, t.target.alias, t.target.user) for t in targets] == [
        ("pc", "pc-wsl-codex-bot", "codex-bot"),
    ]


def test_device_probe_marks_reachable_devices_up_and_unreachable_ones_unseen(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    machines, _ = load_machines_yml(machines_file(tmp_path), allow_placeholders=True)
    writer = store(db)
    health.HealthStore(db, devices={"retired"}, clock=lambda: NOW).beat(
        "device-probe", "retired", pid=1, started_at=NOW, version="unknown", interval=60.0)
    transport = FakeTransport({"pc-wsl-codex-bot": 0})
    prober = health.DeviceProber(writer, health.probe_targets(machines), transport, log=lambda line: None)
    prober.forget_others()
    prober.probe_once()
    rows = {r.device: r for r in health.read_heartbeats(db) or ()}
    assert set(rows) == {"pc"}  # the retired device's row is forgotten
    assert rows["pc"].last_seen == NOW
    assert rows["pc"].interval_seconds == health.PROBE_SECONDS
    argv = transport.calls[0]
    assert argv[0] == "ssh" and "BatchMode=yes" in argv and argv[-1] == "exit 0"

    later = store(db, NOW + 300)
    transport.codes["pc-wsl-codex-bot"] = SSH_UNREACHABLE
    health.DeviceProber(later, health.probe_targets(machines), transport, log=lambda line: None).probe_once()
    pc = next(r for r in health.read_heartbeats(db) or ())
    assert pc.last_seen == NOW  # an unreachable probe never counts as seen
    assert pc.checked_at == NOW + 300
    assert health.classify(pc, NOW + 300)[0] == "stale"
    assert health.classify(pc, NOW + 500)[0] == "down"


def test_poller_beats_write_the_poller_and_probe_rows(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    transport = FakeTransport({})
    with health.poller_beats(db, machines_file(tmp_path), transport=transport,
                             interval=3600.0, log=lambda line: None):
        deadline = time.monotonic() + 5
        while len(health.read_heartbeats(db) or ()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        rows = {(r.component, r.device) for r in health.read_heartbeats(db) or ()}
    assert rows == {("poller", "box"), ("device-probe", "pc")}
    poller = next(r for r in health.read_heartbeats(db) or () if r.component == "poller")
    assert poller.stopped_at is not None


def test_waiter_devices_are_the_eligible_devices_for_its_harness(tmp_path: Path) -> None:
    machines, _ = load_machines_yml(machines_file(tmp_path), allow_placeholders=True)
    assert health.waiter_devices(machines, "codex-bot") == ["box", "pc"]
    assert health.waiter_devices(machines, "claude-bot") == ["box"]  # mac is disabled
    assert health.waiter_devices(None, "claude-bot") == ["box"]


# --- the report: no waiter for a queued job ---------------------------------------------


def eligible(tmp_path: Path) -> dict:
    machines, _ = load_machines_yml(machines_file(tmp_path), allow_placeholders=True)
    return health.eligible_devices(machines)


def test_a_queued_job_with_no_live_waiter_is_flagged(tmp_path: Path) -> None:
    rows = (row(component="poller", bot=""),)
    report = health.build_report(rows, NOW, queued=[("j1", "codex"), ("j2", "codex"), ("j3", "grok")],
                                 eligible=eligible(tmp_path))
    assert [(n.harness, n.bot, n.jobs) for n in report.no_waiter] == [("codex", "codex-bot", ("j1", "j2"))]
    assert report.no_waiter[0].anchor == "health-waiter-codex-bot"


def test_a_live_waiter_on_an_eligible_device_clears_the_flag(tmp_path: Path) -> None:
    rows = (row(component="poller", bot=""), row(device="pc", last_seen=NOW - 50))  # stale still counts
    report = health.build_report(rows, NOW, queued=[("j1", "codex")], eligible=eligible(tmp_path))
    assert report.no_waiter == ()


@pytest.mark.parametrize(
    "waiter",
    [
        row(last_seen=NOW - 500),  # down
        row(stopped_at=NOW),  # stopped
        row(device="mac"),  # mac does not list codex
        row(bot="claude-bot"),  # another bot's waiter
    ],
)
def test_a_dead_or_ineligible_waiter_still_leaves_the_job_flagged(tmp_path: Path, waiter) -> None:
    report = health.build_report((waiter,), NOW, queued=[("j1", "codex")], eligible=eligible(tmp_path))
    assert [n.harness for n in report.no_waiter] == ["codex"]


def test_a_known_health_db_with_no_poller_row_shows_the_poller_down() -> None:
    report = health.build_report((row(),), NOW, queued=[])
    poller = report.components[0]
    assert (poller.component, poller.state, poller.reason) == ("poller", "down", "never seen")
    assert [c.component for c in report.down] == ["poller"]


def test_report_lines_for_orch_status() -> None:
    rows = (
        row(component="poller", bot="", pid=10, last_seen=NOW - 5),
        row(last_seen=NOW - 600, pid=4242),
    )
    report = health.build_report(rows, NOW, queued=[("j1", "codex")])
    lines = health.render_lines(report, NOW)
    assert lines[0] == "Health (2 components, 1 down)"
    assert lines[1].startswith("  poller @ box up, last seen ")
    assert lines[1].endswith(" ET (5s ago), pid 10")
    assert "waiter codex-bot @ box down (no heartbeat for 10m)" in lines[2]
    assert lines[3] == "  no waiter for codex: 1 queued job"


@pytest.mark.parametrize(
    "when, text",
    [
        (1_794_000_000.0, "Nov 6 4:20:00 PM ET"),  # EST (UTC-5)
        (1_780_000_000.0, "May 28 4:26:40 PM ET"),  # EDT (UTC-4)
        (1_772_953_199.0, "Mar 8 1:59:59 AM ET"),  # the last EST second, 2026
        (1_772_953_200.0, "Mar 8 3:00:00 AM ET"),  # the first EDT second
        (1_793_512_799.0, "Nov 1 1:59:59 AM ET"),  # the last EDT second, 2026
        (1_793_512_800.0, "Nov 1 1:00:00 AM ET"),  # back to EST
    ],
)
def test_last_seen_is_shown_in_eastern_time(when: float, text: str) -> None:
    assert health.et(when) == text


# --- the dashboard ------------------------------------------------------------------------


ONLY_CODEX = """\
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses:
      codex: codex-bot
"""


def snapshot(tmp_path: Path, db: Path, now: float = NOW, *, machines: str = MACHINES,
             config=None) -> dict:
    return collect_snapshot(db, tmp_path / "poller.log", now=now,
                            machines_path=machines_file(tmp_path, machines), alive=None,
                            orch=config)


def test_health_is_the_tab_after_efficiency(tmp_path: Path) -> None:
    assert NAV_PAGES[-2:] == ("efficiency.html", "health.html")
    db, _ = make_db(tmp_path)
    html = render_pages(snapshot(tmp_path, db))["index.html"]
    nav = html[html.index('id="nav"'):html.index("</nav>")]
    assert nav.index("efficiency.html") < nav.index('href="health.html"')
    assert ">System</a>" in nav  # #139


def test_the_health_page_lists_components_with_state_et_pid_and_device(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 5).beat("poller", "box", pid=101, started_at=NOW - 900, version="0123456789ab")
    store(db, NOW - 60).beat("waiter", "box", bot="codex-bot", pid=202, started_at=NOW - 900, version="unknown")
    store(db, NOW - 900).beat("linear-listener", "box", pid=303, started_at=NOW - 990, version="unknown")
    data = snapshot(tmp_path, db)
    components = {(c["component"], c["bot"]): c for c in data["health"]["components"]}
    assert components[("poller", "")]["state"] == "up"
    assert components[("poller", "")]["pid"] == 101
    assert components[("poller", "")]["device"] == "box"
    assert components[("poller", "")]["last_seen_et"] == health.et(NOW - 5)
    assert components[("waiter", "codex-bot")]["state"] == "stale"
    assert components[("linear-listener", "")]["state"] == "down"
    # Also down: the claude and cursor waiters and pc's probe, which
    # machines.yml expects and which never beat.
    assert data["health"]["down"] == 4
    page = render_pages(data)["health.html"]
    assert 'id="components"' in page
    assert health.et(NOW - 5).removesuffix(" ET") in page
    assert ">101<" in page and ">stale<" in page.lower()


def test_a_down_component_shows_on_the_needs_attention_line(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 5).beat("poller", "box", pid=101, started_at=NOW - 900, version="unknown")
    store(db, NOW - 900).beat("waiter", "box", bot="codex-bot", pid=202, started_at=NOW - 990, version="unknown")
    data = snapshot(tmp_path, db, machines=ONLY_CODEX)
    items = [i for i in data["attention"]["items"] if i["kind"] == "down"]
    assert len(items) == 1
    assert items[0]["component"] == "waiter codex-bot @ box"
    assert items[0]["link"] == "health.html#health-waiter-codex-bot-box"
    assert data["attention"]["count"] >= 1
    index = render_pages(data)["index.html"]
    assert 'id="attention-count">1<' in index
    activity = render_pages(data)["activity.html"]
    assert "waiter codex-bot @ box" in activity


def test_a_queued_job_with_no_waiter_is_flagged_on_the_dashboard(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    job = queue.enqueue(request("codex"), dedupe_key="a")
    queue.enqueue(request("grok", "43"), dedupe_key="b")
    store(db, NOW - 5).beat("poller", "box", pid=101, started_at=NOW - 900, version="unknown")
    data = snapshot(tmp_path, db)
    flagged = [q for q in data["queued"] if q.get("no_waiter")]
    assert [(q["job"], q["no_waiter"], q["no_waiter_link"]) for q in flagged] == [
        (job.id[:8], "codex", "health.html#health-waiter-codex-bot"),
    ]
    assert data["health"]["no_waiter"] == [
        {"harness": "codex", "bot": "codex-bot", "jobs": [job.id[:8]], "count": 1,
         "anchor": "health-waiter-codex-bot"},
    ]
    attention = [i for i in data["attention"]["items"] if i["kind"] == "no-waiter"]
    assert attention[0]["detail"] == "no waiter for codex (1 queued job)"
    pages = render_pages(data)
    assert "no waiter for codex" in pages["index.html"]
    assert 'href="health.html#health-waiter-codex-bot"' in pages["index.html"]
    assert 'id="health-waiter-codex-bot"' in pages["health.html"]

    store(db, NOW - 5).beat("waiter", "box", bot="codex-bot", pid=7, started_at=NOW - 60, version="unknown")
    data = snapshot(tmp_path, db)
    assert not any(q.get("no_waiter") for q in data["queued"])
    assert data["health"]["no_waiter"] == []


def test_without_heartbeats_the_dashboard_attention_is_unchanged(tmp_path: Path) -> None:
    db, queue = make_db(tmp_path)
    queue.enqueue(request("codex"), dedupe_key="a")
    data = snapshot(tmp_path, db)
    assert data["health"]["known"] is False
    assert data["attention"]["count"] == 0
    assert not any("no_waiter" in q for q in data["queued"])


# --- orch status and orch health -----------------------------------------------------------


def test_orch_status_has_a_health_section(tmp_path: Path, monkeypatch) -> None:
    from tests.test_wait import dispatcher_for

    monkeypatch.setattr(health, "ROOT", tmp_path / "no-checkout")
    db, queue = make_db(tmp_path)
    dispatcher_for(db)  # the tracker tables orch status reads
    queue.enqueue(request("codex"), dedupe_key="a")
    store(db, NOW - 5).beat("poller", "box", pid=101, started_at=NOW - 900, version="unknown")
    out = io.StringIO()
    code = status_main(["--db", str(db), "--machines", str(machines_file(tmp_path, ONLY_CODEX)),
                        "--orch", str(ORCH_EXAMPLE)],
                       stdout=out, clock=lambda: NOW)
    assert code == 0
    text = out.getvalue()
    # orch.example.yml pushes the dashboard; machines.yml runs a codex waiter.
    assert "\nHealth (3 components, 2 down)\n" in text
    assert "  poller @ box up, last seen " in text
    assert "  waiter codex-bot @ box down (never seen), never seen" in text
    assert "  dashboard @ box down (never seen), never seen" in text
    assert "  no waiter for codex: 1 queued job" in text
    assert "build -> codex queued 30s [no waiter for codex]" in text


def test_orch_health_exits_3_when_something_needs_attention(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(health, "box_storage", functools.partial(
        health.box_storage, statvfs=lambda path: statvfs(inodes_used=10)))
    db, queue = make_db(tmp_path)
    machines = machines_file(tmp_path, ONLY_CODEX)
    now = time.time()
    store(db, now).beat("poller", "box", pid=101, started_at=now, version="unknown")
    store(db, now).beat("waiter", "box", bot="codex-bot", pid=102, started_at=now, version="unknown")
    store(db, now).beat("dashboard", "box", pid=103, started_at=now, version="unknown")
    args = ["health", "--db", str(db), "--machines", str(machines), "--orch", str(ORCH_EXAMPLE)]
    assert cli_main(args) == 0
    assert "poller @ box up" in capsys.readouterr().out

    queue.enqueue(request("cursor"), dedupe_key="a")
    assert cli_main([*args, "--json"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["no_waiter"][0]["harness"] == "cursor"
    assert data["components"][0]["component"] == "poller"


def test_orch_health_forget_removes_a_retired_components_rows(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db).beat("waiter", "pc", bot="cursor-bot", pid=5, version="unknown")
    store(db).beat("poller", "box", pid=6, version="unknown")
    assert cli_main(["health", "forget", "waiter", "--db", str(db), "--bot", "cursor-bot"]) == 0
    assert [r.component for r in health.read_heartbeats(db) or ()] == ["poller"]


# --- storage (#395) ------------------------------------------------------------------------

GNU_DF = """\
Filesystem     1024-blocks     Used Available Capacity Mounted on
/dev/vdb         133587600 63189528  70381688      48% /
Filesystem      Inodes   IUsed  IFree IUse% Mounted on
/dev/vdb       2048000 1740800 307200   85% /
"""
GNU_DF_OVERFULL = """\
Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/vdb 4000 3960 -40 102% /work
Filesystem Inodes IUsed IFree IUse% Mounted on
/dev/vdb 1000 900 100 90% /work
"""
GNU_DF_WRAPPED = """\
Filesystem     1024-blocks     Used Available Capacity Mounted on
/dev/mapper/a-very-long-volume-name 1000 900 100 90% /srv
Filesystem      Inodes   IUsed  IFree IUse% Mounted on
/dev/mapper/a-very-long-volume-name
                  1000     500    500   50% /srv
"""
BSD_DF = """\
Filesystem 1024-blocks      Used Available Capacity Mounted on
/dev/disk3s5 971350180 400000000 571350180    42% /System/Volumes/Data
Filesystem   512-blocks      Used  Available Capacity  iused     ifree %iused  Mounted on
/dev/disk3s5 1942700360 800000000 1142700360    42% 1000000 9000000   10%   /System/Volumes/Data
"""
BSD_DF_OVERFULL = """\
Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/disk4s1 4000 3960 -40 102% /Volumes/Work
Filesystem 512-blocks Used Available Capacity iused ifree %iused Mounted on
/dev/disk4s1 8000 7920 -80 102% 900 100 90% /Volumes/Work
"""

BSD_DF_SPACED_MOUNT = """\
Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/disk4s1 1000 300 700 30% /Volumes/Work Disk
Filesystem 512-blocks Used Available Capacity iused ifree %iused Mounted on
/dev/disk4s1 2000 600 1400 30% 900 100 90% /Volumes/Work Disk
"""
GNU_DF_SPACED_NAMES = """\
Filesystem     1024-blocks Used Available Capacity Mounted on
//nas/team share 1000 300 700 30% /mnt/team share 2
Filesystem      Inodes IUsed IFree IUse% Mounted on
//nas/team share 1000 860 140 86% /mnt/team share 2
"""


@pytest.mark.parametrize("text, expected", [
    (GNU_DF, (48, 85)),
    (GNU_DF_OVERFULL, (102, 90)),
    (GNU_DF_WRAPPED, (90, 50)),
    (BSD_DF, (42, 10)),
    (BSD_DF_OVERFULL, (102, 90)),
    (BSD_DF_SPACED_MOUNT, (30, 90)),
    (GNU_DF_SPACED_NAMES, (30, 86)),
    ("df: /nowhere: No such file or directory\n", (None, None)),
])
def test_parse_df_reads_disk_and_inode_use_from_gnu_and_bsd_df(text: str, expected) -> None:
    assert health.parse_df(text) == expected


def statvfs(*, inodes_used: int, files: int = 1000, used_blocks: int = 300) -> SimpleNamespace:
    return SimpleNamespace(f_blocks=1000, f_bfree=1000 - used_blocks, f_bavail=1000 - used_blocks,
                           f_files=files, f_ffree=files - inodes_used)


@pytest.mark.parametrize("used, percent, alert", [(840, 84, False), (841, 85, True), (850, 85, True),
                                                  (1000, 100, True)])
def test_box_storage_alerts_at_85_percent_inode_use(tmp_path: Path, used: int, percent: int,
                                                    alert: bool) -> None:
    [entry] = health.box_storage("box", [tmp_path, tmp_path / ".."],
                                 statvfs=lambda path: statvfs(inodes_used=used))
    assert (entry.device, entry.disk, entry.inodes, entry.alert) == ("box", 30, percent, alert)
    assert Path(entry.path) == health._mount(tmp_path)


def test_a_filesystem_without_inode_counts_reports_unknown_not_an_alert(tmp_path: Path) -> None:
    [entry] = health.box_storage("box", [tmp_path], statvfs=lambda path: statvfs(inodes_used=0, files=0))
    assert entry.inodes is None and not entry.alert


class DfTransport:
    def __init__(self, outputs: dict[str, Exchanged]):
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def exchange(self, argv, *, timeout: float, input: bytes = b"") -> Exchanged:
        self.calls.append(list(argv))
        return self.outputs[argv[argv.index("--") + 1]]


def test_remote_storage_runs_df_over_ssh_in_the_devices_workdir(tmp_path: Path) -> None:
    transport = DfTransport({"pc-wsl-codex-bot": Exchanged(0, GNU_DF.encode(), "")})
    [target] = health.probe_targets(machines_config(tmp_path))
    entry = health.remote_storage(transport, target)
    assert entry == health.Storage("pc", "/home/codex-bot/factory-work", 48, 85)
    assert entry.alert
    argv = transport.calls[0]
    assert argv[0] == "ssh" and "BatchMode=yes" in argv
    assert argv[-1] == health.df_script("/home/codex-bot/factory-work")


def test_a_device_workdir_on_a_mount_with_spaces_reports_and_alerts(tmp_path: Path) -> None:
    transport = DfTransport({"pc-wsl-codex-bot": Exchanged(0, BSD_DF_SPACED_MOUNT.encode(), "")})
    [target] = health.probe_targets(machines_config(tmp_path))
    entry = health.remote_storage(transport, target)
    assert (entry.disk, entry.inodes, entry.alert, entry.error) == (30, 90, True, None)


def test_an_unreachable_device_reports_unknown_storage_without_an_alert(tmp_path: Path) -> None:
    transport = DfTransport({"pc-wsl-codex-bot": Exchanged(SSH_UNREACHABLE, b"", "timed out")})
    [target] = health.probe_targets(machines_config(tmp_path))
    entry = health.remote_storage(transport, target)
    assert (entry.disk, entry.inodes, entry.alert, entry.error) == (None, None, False, "df exited 255")


def test_orch_health_reports_box_and_device_inode_use_and_alerts_at_85(tmp_path: Path,
                                                                       monkeypatch) -> None:
    monkeypatch.setattr(health, "box_storage", functools.partial(
        health.box_storage, statvfs=lambda path: statvfs(inodes_used=120)))
    db, _ = make_db(tmp_path)
    now = time.time()
    for component, device in (("poller", "box"), ("device-probe", "pc")):
        store(db, now).beat(component, device, pid=101, started_at=now, version="unknown")
    store(db, now).beat("waiter", "box", bot="codex-bot", pid=102, started_at=now, version="unknown")
    store(db, now).beat("waiter", "box", bot="claude-bot", pid=103, started_at=now, version="unknown")
    store(db, now).beat("waiter", "box", bot="cursor-bot", pid=104, started_at=now, version="unknown")
    store(db, now).beat("dashboard", "box", pid=105, started_at=now, version="unknown")
    df = GNU_DF.replace("1740800 307200   85%", "1000000 1048000   49%")
    transport = DfTransport({"pc-wsl-codex-bot": Exchanged(0, df.encode(), "")})
    args = ["--db", str(db), "--machines", str(machines_file(tmp_path)), "--orch", str(ORCH_EXAMPLE)]

    out = io.StringIO()
    assert health.main(args, stdout=out, transport=transport) == 0
    text = out.getvalue()
    box_mount = health._mount(health.ROOT)
    assert f"\n  storage box {box_mount}: disk 30%, inodes 12%\n" in text
    assert "\n  storage pc /home/codex-bot/factory-work: disk 48%, inodes 49%" in text
    assert "inode alert" not in text

    transport.outputs["pc-wsl-codex-bot"] = Exchanged(0, GNU_DF.encode(), "")
    out = io.StringIO()
    assert health.main(args, stdout=out, transport=transport) == 3
    text = out.getvalue()
    assert ", 1 inode alert)" in text.splitlines()[0]
    assert ("  storage pc /home/codex-bot/factory-work: disk 48%, inodes 85% "
            "(inode alert: at or over 85%)") in text

    out = io.StringIO()
    assert health.main([*args, "--json"], stdout=out, transport=transport) == 3
    data = json.loads(out.getvalue())
    assert data["storage_alerts"] == 1
    assert data["thresholds"]["inode_alert_percent"] == 85
    assert {"device": "pc", "path": "/home/codex-bot/factory-work", "disk_percent": 48,
            "inode_percent": 85, "alert": True, "error": None} in data["storage"]
    assert data["storage"][0]["device"] == "box" and data["storage"][0]["alert"] is False


def test_orch_health_alerts_when_the_box_reaches_85_percent_inodes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(health, "box_storage", functools.partial(
        health.box_storage, statvfs=lambda path: statvfs(inodes_used=900)))
    db, _ = make_db(tmp_path)
    now = time.time()
    store(db, now).beat("poller", "box", pid=101, started_at=now, version="unknown")
    store(db, now).beat("waiter", "box", bot="codex-bot", pid=102, started_at=now, version="unknown")
    out = io.StringIO()
    code = health.main(["--db", str(db), "--machines", str(machines_file(tmp_path, ONLY_CODEX))],
                       stdout=out, transport=DfTransport({}))
    assert code == 3
    assert f"  storage box {health._mount(health.ROOT)}: disk 30%, inodes 90% (inode alert" in out.getvalue()


def test_orch_health_shows_storage_alerts_before_any_heartbeat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(health, "box_storage", functools.partial(
        health.box_storage, statvfs=lambda path: statvfs(inodes_used=900)))
    db, _ = make_db(tmp_path)
    transport = DfTransport({"pc-wsl-codex-bot": Exchanged(0, GNU_DF_OVERFULL.encode(), "")})
    out = io.StringIO()
    code = health.main(["--db", str(db), "--machines", str(machines_file(tmp_path))],
                       stdout=out, transport=transport)
    text = out.getvalue()
    assert code == 3
    assert "no heartbeats recorded yet" in text
    assert f"  storage box {health._mount(health.ROOT)}: disk 30%, inodes 90% (inode alert" in text
    assert "  storage pc /home/codex-bot/factory-work: disk 102%, inodes 90% (inode alert" in text


@pytest.mark.parametrize("report", [
    health.HealthReport(known=False),
    health.HealthReport(known=False, error="health table unreadable"),
])
def test_every_health_text_lists_storage(report) -> None:
    storage = (health.Storage("pc", "/work", 40, 90),)
    lines = health.render_lines(replace(report, storage=storage), NOW)
    assert "  storage pc /work: disk 40%, inodes 90% (inode alert: at or over 85%)" in lines


GERMAN_DF = """\
Dateisystem 1024-Blöcke Benutzt Verfügbar Kapazität Eingehängt auf
/dev/sdf 1000 120 880 12% /
Dateisystem Inodes IBenutzt IFrei IUse% Eingehängt auf
/dev/sdf 1000 70 930 7% /
"""


def test_df_reads_english_headers_whatever_the_devices_locale(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_df = bin_dir / "df"
    fake_df.write_text(
        "#!/bin/sh\n"
        f"[ \"$LC_ALL\" = C ] && printf '%s' {shlex.quote(GNU_DF)} && exit 0\n"
        f"printf '%s' {shlex.quote(GERMAN_DF)}\n")
    fake_df.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}
    run = subprocess.run(["sh", "-c", health.df_script("/work")], env=env, capture_output=True,
                         text=True, check=True)
    assert health.parse_df(run.stdout) == (48, 85)



@pytest.mark.parametrize("variable", ["DF_BLOCK_SIZE", "BLOCK_SIZE", "BLOCKSIZE"])
def test_df_reports_inode_use_when_the_device_asks_for_human_readable_sizes(
        tmp_path: Path, variable: str) -> None:
    st = os.statvfs(tmp_path)
    expected = health._df_percent(st.f_files - st.f_ffree, st.f_ffree)
    if expected is None:
        pytest.skip("this filesystem reports no inode counts")
    env = {**os.environ, variable: "human-readable"}
    run = subprocess.run(["sh", "-c", health.df_script(str(tmp_path))], env=env,
                         capture_output=True, text=True, check=True)
    assert health.parse_df(run.stdout)[1] == expected

# --- the components wire their heartbeats in ------------------------------------------------


class Recorder:
    entered: list[dict] = []

    def __init__(self, db, component, **kwargs):
        Recorder.entered.append({"db": db, "component": component, **kwargs})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_beat_thread_blocks_the_signals_orch_wait_guards(tmp_path: Path) -> None:
    import signal
    import threading

    db, _ = make_db(tmp_path)
    seen: list[set] = []
    done = threading.Event()

    class Probe(health.HealthStore):
        def beat(self, *args, **kwargs):
            seen.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
            done.set()

    with health.Beater(db, "waiter", devices=["box"], bot="codex-bot", store=Probe(db, devices=DEVICES),
                       interval=60.0, log=lambda line: None):
        assert done.wait(5)
    assert {signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= set(seen[0])
    assert signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, [])  # main thread untouched


def test_plain_orch_wait_beats_as_a_waiter_while_it_blocks(
    tmp_path: Path, monkeypatch, capsys, restore_signals
) -> None:
    db, queue = make_db(tmp_path)
    queue.enqueue(request("codex"), dedupe_key="a")
    Recorder.entered = []
    monkeypatch.setattr(health, "Beater", Recorder)
    assert cli_main(["wait", "codex-bot", "--db", str(db), "--machines", str(machines_file(tmp_path))]) == 0
    assert [(e["component"], e["bot"], e["devices"], e["on_exit"]) for e in Recorder.entered] == [
        ("waiter", "codex-bot", ["box", "pc"], "delete"),
    ]


def test_the_dashboard_push_records_the_dashboard_writer(tmp_path: Path, monkeypatch) -> None:
    from lib import dashboard_cli

    db, _ = make_db(tmp_path)
    beats: list[tuple] = []
    monkeypatch.setattr(health, "beat_once", lambda db, component, **kw: beats.append((db, component, kw)) or True)

    class Sent:
        def send(self, paths):
            return None

    monkeypatch.setattr(dashboard_cli, "transport_for", lambda config: Sent())
    code = dashboard_cli.main(["push", "--once", "--db", str(db), "--out", str(tmp_path / "out"),
                               "--machines", str(machines_file(tmp_path)), "--log", str(tmp_path / "p.log"),
                               "--orch", str(Path(__file__).resolve().parents[1] / "config" / "orch.example.yml")])
    assert code == 0
    assert [(b[0], b[1]) for b in beats] == [(db, "dashboard")]
    assert (tmp_path / "out" / "health.html").is_file()


def test_the_page_script_renders_the_health_tab_and_the_queue_flag(tmp_path: Path) -> None:
    from tests.test_dashboard_layout import BAR, NODE, _run_page, attention_count

    if NODE is None:
        pytest.skip("node is not installed")
    db, queue = make_db(tmp_path, PAGE_NOW)
    queue.enqueue(request("codex"), dedupe_key="a")
    store(db, PAGE_NOW - 5).beat("poller", "box", pid=101, started_at=PAGE_NOW - 900, version="unknown")
    store(db, PAGE_NOW - 900).beat("linear-listener", "box", pid=303, started_at=PAGE_NOW - 990,
                                   version="unknown")
    data = snapshot(tmp_path, db, PAGE_NOW, machines=ONLY_CODEX)
    (tmp_path / "health").mkdir()
    page = _run_page(tmp_path / "health", data, "health", BAR + ["hud", "broken", "components", "waiters"])
    text = page["text"]
    assert "poller" in text["components"] and "UP" in text["components"]
    assert "linear-listener" in text["components"] and "DOWN" in text["components"]
    assert health.et(PAGE_NOW - 5).removesuffix(" ET") in text["components"]
    assert "no waiter for codex: 1 queued job" in text["waiters"]
    assert "no waiter for codex: 1 queued job" in text["broken"]
    # Down: the listener and the codex waiter; codex has no waiter.
    assert attention_count(page) == "3"
    (tmp_path / "main").mkdir()
    main = _run_page(tmp_path / "main", data, "main", BAR + ["hud", "lanes", "queue"])
    assert "no waiter for codex" in main["text"]["queue"]
    assert "a.chip.bad[href=health.html#health-waiter-codex-bot]" in main["dump"]["queue"]


# --- review round 1 (PR #98): each class the review named ------------------------------------


def test_expected_components_that_never_beat_are_down(tmp_path: Path) -> None:
    """A1: every configured component is listed; one that never beat is down."""
    rows = (row(component="poller", bot="", pid=1),)
    machines = machines_config(tmp_path)
    report = health.build_report(
        rows, NOW, expected=health.expected_components(machines, orch(listener=True, push=True)))
    listed = {(c.component, c.bot, c.device): (c.state, c.reason) for c in report.components}
    assert listed == {
        ("poller", "", "box"): ("up", ""),
        ("waiter", "claude-bot", "box"): ("down", "never seen"),
        ("waiter", "codex-bot", "box"): ("down", "never seen"),
        ("waiter", "cursor-bot", "box"): ("down", "never seen"),
        ("linear-listener", "", "box"): ("down", "never seen"),
        ("tunnel", "", "box"): ("down", "never reached"),
        ("dashboard", "", "box"): ("down", "never seen"),
        ("device-probe", "", "pc"): ("down", "never reached"),
    }
    assert len(report.down) == 7


def test_a_waiter_on_any_eligible_device_meets_its_bots_slot(tmp_path: Path) -> None:
    rows = (row(component="poller", bot="", pid=1), row(device="pc", pid=2))
    report = health.build_report(
        rows, NOW, expected=health.expected_components(machines_config(tmp_path), orch()))
    codex = [c for c in report.components if c.bot == "codex-bot"]
    assert [(c.device, c.state) for c in codex] == [("pc", "up")]


def test_unconfigured_components_are_listed_but_need_no_attention(tmp_path: Path) -> None:
    """A1: the registry separates configured components from retired ones."""
    rows = (
        row(component="poller", bot="", pid=1),
        row(device="mac", bot="claude-bot", last_seen=NOW - 900),  # mac is disabled
        row(component="linear-listener", bot="", last_seen=NOW - 900),  # no linear_webhook
    )
    report = health.build_report(
        rows, NOW, expected=health.expected_components(machines_config(tmp_path, ONLY_CODEX), orch()))
    retired = {c.component: c for c in report.components if not c.configured}
    assert set(retired) == {"waiter", "linear-listener"}
    assert all(c.state == "down" for c in retired.values())
    assert [c.label for c in report.down] == ["waiter codex-bot @ box"]
    assert "(not configured)" in "\n".join(health.render_lines(report, NOW))


def test_the_dashboard_expects_what_orch_yml_configures(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 5).beat("poller", "box", pid=101, version="unknown")
    store(db, NOW - 5).beat("waiter", "box", bot="codex-bot", pid=102, version="unknown")
    data = snapshot(tmp_path, db, machines=ONLY_CODEX, config=orch(listener=True, push=True))
    down = sorted(i["component"] for i in data["attention"]["items"] if i["kind"] == "down")
    assert down == ["dashboard @ box", "linear-listener @ box", "tunnel @ box"]


@pytest.mark.parametrize("damage", ["corrupt", "unreadable"])
def test_unreadable_health_storage_needs_attention(tmp_path: Path, capsys, damage: str) -> None:
    """A2: a health file that cannot be read is flagged, never reported as fine."""
    db, _ = make_db(tmp_path)
    path = health.health_path(db)
    if damage == "corrupt":
        path.write_bytes(b"not a database, " * 64)
    else:
        store(db).beat("poller", "box", pid=1, version="unknown")
        path.chmod(0)
        if os.access(path, os.R_OK):
            pytest.skip("running as root: chmod 0 does not stop reads")
    try:
        args = ["health", "--db", str(db), "--machines", str(machines_file(tmp_path, ONLY_CODEX)),
                "--orch", str(ORCH_EXAMPLE)]
        assert cli_main(args) == 1
        assert "unknown: cannot read orch.health.db" in capsys.readouterr().out
        assert cli_main([*args, "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["error"].startswith("cannot read orch.health.db")

        data = snapshot(tmp_path, db)
        items = [i for i in data["attention"]["items"] if i["kind"] == "health"]
        assert [(i["component"], i["link"]) for i in items] == [("health storage", "health.html#components")]
        assert items[0]["detail"].startswith("unknown: cannot read orch.health.db")
        pages = render_pages(data)
        assert 'id="attention-count">1<' in pages["index.html"]
        assert "health storage" in pages["activity.html"]
    finally:
        path.chmod(0o600)


def _page_with_fresh_rows(tmp_path: Path) -> dict:
    db, _ = make_db(tmp_path, PAGE_NOW)
    store(db, PAGE_NOW).beat("poller", "box", pid=101, version="unknown")
    store(db, PAGE_NOW).beat("waiter", "box", bot="codex-bot", pid=102, version="unknown")
    return snapshot(tmp_path, db, PAGE_NOW, machines=ONLY_CODEX)


def test_the_page_ages_component_states_with_an_unchanged_snapshot(tmp_path: Path) -> None:
    """A3: a snapshot that stops changing never keeps showing UP. A one-off
    snapshot (no writer, no publication interval) vouches for nothing: a
    component past its thresholds is unknown, and the old snapshot is one
    needs-attention item."""
    from tests.test_dashboard_layout import BAR, NODE, _run_page, attention_count

    if NODE is None:
        pytest.skip("node is not installed")
    data = _page_with_fresh_rows(tmp_path)
    assert data["health"]["publish_seconds"] is None
    ids = BAR + ["hud", "broken", "components", "waiters"]
    (tmp_path / "now").mkdir()
    fresh_out = _run_page(tmp_path / "now", data, "health", ids)
    fresh = fresh_out["text"]
    assert _states(fresh["components"]) == ["UP", "UP"]
    assert attention_count(fresh_out) == "0"

    for advance in (50, 121):
        (tmp_path / str(advance)).mkdir()
        later_out = _run_page(tmp_path / str(advance), data, "health", ids, actions=[["advance", advance]])
        later = later_out["text"]
        assert _states(later["components"]) == ["UNKNOWN", "UNKNOWN"]
        assert "no snapshot for " in later["components"]
        assert "2 configured · 0 up · " in later["components"]
        assert attention_count(later_out) == "1"


def test_the_activity_page_lists_an_old_snapshot_once(tmp_path: Path) -> None:
    from tests.test_dashboard_layout import BAR, NODE, _run_page

    if NODE is None:
        pytest.skip("node is not installed")
    data = _page_with_fresh_rows(tmp_path)
    page = _run_page(tmp_path, data, "activity", BAR + ["hud", "attention"], actions=[["advance", 121]])
    assert "health snapshot" in page["text"]["attention"]
    assert "unknown: no snapshot for 2m" in page["text"]["attention"]
    assert 'a.at-go[href=health.html#components] "open in System"' in page["dump"]["attention"]
    assert "poller @ box" not in page["text"]["attention"]


@pytest.mark.parametrize(
    "device",
    [SECRETS["GH_TOKEN"], SECRETS["LINEAR_API_KEY"], "whk_0123456789abcdefSECRET", "retired"],
)
def test_every_writer_refuses_a_device_outside_machines_yml(tmp_path: Path, device: str) -> None:
    """A4: the device column is checked against the machines.yml inventory."""
    db, _ = make_db(tmp_path)
    machines = machines_file(tmp_path)
    with pytest.raises(health.HealthError):
        health.HealthStore(db, devices=health.known_devices(health.load_machines(machines))).beat(
            "tunnel", device, pid=1, version="unknown")
    lines: list[str] = []
    beater = health.Beater(db, "waiter", devices=["box"], bot="codex-bot",
                           store=health.HealthStore(db, devices={"box"}), log=lines.append)
    beater.devices = (device,)
    beater.beat_now()
    assert lines == ["health: waiter heartbeat failed: HealthError"]
    assert cli_main(["health", "beat", "tunnel", "--db", str(db), "--device", device,
                     "--pid", str(os.getpid())]) == 2
    assert health.read_heartbeats(db) in (None, ())
    raw = health.health_path(db).read_bytes() if health.health_path(db).exists() else b""
    assert device.encode() not in raw


def test_the_inventory_is_machines_yml_plus_the_local_device(tmp_path: Path) -> None:
    assert health.known_devices(machines_config(tmp_path)) == {"box", "pc", "mac"}
    assert health.known_devices(None) == {"box"}


def test_a_locked_health_db_never_stalls_the_dashboard_push(tmp_path: Path, monkeypatch, capsys) -> None:
    """A5: heartbeat I/O is bounded and a dropped beat is logged; under WAL
    (#440) the writer's lock does not stop the snapshot reading the rows."""
    from lib import dashboard_cli

    db, _ = make_db(tmp_path)
    store(db).beat("poller", "box", pid=1, version="unknown")
    holder = sqlite3.connect(health.health_path(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    sent: list = []

    class Sent:
        def send(self, paths):
            sent.append(time.monotonic())

    monkeypatch.setattr(dashboard_cli, "transport_for", lambda config: Sent())
    started = time.monotonic()
    try:
        code = dashboard_cli.main(["push", "--once", "--db", str(db), "--out", str(tmp_path / "out"),
                                   "--machines", str(machines_file(tmp_path)),
                                   "--log", str(tmp_path / "p.log"), "--orch", str(ORCH_EXAMPLE)])
    finally:
        holder.rollback()
        holder.close()
    assert code == 0
    assert sent and sent[0] - started < 4 * health.BUSY_SECONDS + 1
    assert "health: dashboard heartbeat failed: OperationalError" in capsys.readouterr().err
    status = json.loads((tmp_path / "out" / "status.json").read_text())
    assert [i for i in status["attention"]["items"] if i["kind"] == "health"] == []
    assert "poller" in [c["component"] for c in status["health"]["components"]]


def test_a_locked_health_db_bounds_a_waiters_exit(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    beater = health.Beater(db, "waiter", devices=["box", "pc"], bot="codex-bot",
                           store=store(db), log=lambda line: None)
    beater.beat_now()
    holder = sqlite3.connect(health.health_path(db), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        beater.close()
    finally:
        holder.rollback()
        holder.close()
    assert time.monotonic() - started < 2 * health.BUSY_SECONDS + 1


def test_one_waiter_exiting_keeps_another_live_waiters_row(tmp_path: Path) -> None:
    """A6: rows are per process; B's clean exit never hides A."""
    db, queue = make_db(tmp_path)
    queue.enqueue(request("codex"), dedupe_key="a")
    a = health.Beater(db, "waiter", devices=["box"], bot="codex-bot", store=store(db),
                      on_exit="delete", pid=1001, log=lambda line: None)
    b = health.Beater(db, "waiter", devices=["box"], bot="codex-bot", store=store(db),
                      on_exit="delete", pid=1002, log=lambda line: None)
    a.beat_now()
    b.beat_now()
    b.close()
    rows = health.read_heartbeats(db) or ()
    assert [(r.component, r.bot, r.pid) for r in rows] == [("waiter", "codex-bot", 1001)]
    report = health.build_report(rows, NOW, queued=[("j1", "codex")], eligible=eligible(tmp_path))
    assert report.no_waiter == ()


def test_one_entry_per_component_shows_its_best_process(tmp_path: Path) -> None:
    rows = (
        row(component="poller", bot="", pid=1),
        row(pid=10, last_seen=NOW - 100),  # an older waiter, stale
        row(pid=11, last_seen=NOW - 2),  # a live one
    )
    report = health.build_report(rows, NOW)
    waiters = [c for c in report.components if c.component == "waiter"]
    assert [(c.pid, c.state) for c in waiters] == [(11, "up")]


def test_a_beat_removes_rows_other_processes_left_down(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 900).beat("waiter", "pc", bot="codex-bot", pid=1, version="unknown")  # died
    store(db, NOW - 20).beat("waiter", "box", bot="codex-bot", pid=2, version="unknown")  # alive
    store(db, NOW - 20).beat("waiter", "box", bot="codex-bot", pid=3, version="unknown")
    store(db, NOW - 10).stop("waiter", "box", bot="codex-bot", pid=3)  # stopped
    store(db, NOW - 900).beat("waiter", "box", bot="claude-bot", pid=4, version="unknown")  # other bot
    store(db).beat("waiter", "box", bot="codex-bot", pid=5, version="unknown")
    left = sorted((r.bot, r.pid) for r in health.read_heartbeats(db) or ())
    assert left == [("claude-bot", 4), ("codex-bot", 2), ("codex-bot", 5)]


MANY_DEVICES = MACHINES + "".join(
    f"""\
  - name: {name}
    tailscale_name: {name}.example.ts.net
    slot_max: 1
    harnesses:
      codex: codex-bot
    workdir_root: /home/{{bot}}/factory-work
    ssh_alias: {name}-{{bot}}
    factory_root: /home/{{bot}}/factory
"""
    for name in ("hang1", "hang2", "hang3")
)


def test_a_hanging_device_never_delays_another_devices_probe(tmp_path: Path) -> None:
    """A7: each device keeps its own probe cadence."""
    db, _ = make_db(tmp_path)
    release = threading.Event()
    good: list[float] = []

    class Hanging:
        def run(self, argv, *, timeout: float) -> int:
            alias = list(argv)[list(argv).index("--") + 1]
            if alias.startswith("hang"):
                release.wait(10)
                return SSH_UNREACHABLE
            good.append(time.monotonic())
            return 0

    machines = machines_config(tmp_path, MANY_DEVICES)
    targets = health.probe_targets(machines)
    assert [t.device for t in targets][0] == "pc" and len(targets) == 4
    writer = health.HealthStore(db, devices=health.known_devices(machines))
    with health.DeviceProber(writer, targets, Hanging(), interval=0.05, log=lambda line: None):
        deadline = time.monotonic() + 5
        while len(good) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        release.set()
    assert len(good) >= 4  # pc kept probing while three devices hung


def test_the_beat_loop_keeps_its_cadence_when_a_step_is_slow() -> None:
    starts: list[float] = []

    def step() -> None:
        starts.append(time.monotonic())
        time.sleep(0.06)

    loop = health._Loop("test", 0.1, step, block_signals=False)
    loop.start()
    time.sleep(0.55)
    loop.stop()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert gaps and max(gaps) < 0.15


def test_a_tunnel_counts_as_seen_only_when_the_listener_answers(tmp_path: Path) -> None:
    """A8: the tunnel's beat is an end-to-end GET, not a pid anyone can name."""
    db, _ = make_db(tmp_path)
    answers: list[object] = [405]
    urls: list[str] = []

    def fetch(url: str, timeout: float) -> int | None:
        urls.append(url)
        answer = answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    lines: list[str] = []
    probe = health.TunnelProber(store(db), "box", "https://box.example.ts.net/linear-webhook",
                                fetch=fetch, pid=7, log=lines.append)
    probe.probe_once()
    (tunnel,) = health.read_heartbeats(db) or ()
    assert (tunnel.component, tunnel.last_seen) == ("tunnel", NOW)
    assert urls == ["https://box.example.ts.net/linear-webhook"]

    for answer in (502, 200, 404, None):
        answers[0] = answer
        health.TunnelProber(store(db, NOW + 60), "box", "https://x.example/linear-webhook",
                            fetch=fetch, pid=7, log=lines.append).probe_once()
        (tunnel,) = health.read_heartbeats(db) or ()
        assert tunnel.last_seen == NOW and tunnel.checked_at == NOW + 60
        assert health.classify(tunnel, NOW + 60) == ("stale", "unreachable, last reached 1m ago")
    answers[0] = OSError("boom")
    probe.probe_once()
    probe.probe_once()
    assert lines == ["health: tunnel probe failed: OSError"]  # logged once, never raised


def test_orch_health_has_no_pid_beat_for_anyone_to_renew(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    assert cli_main(["health", "beat", "tunnel", "--db", str(db), "--pid", str(os.getpid())]) == 2
    assert health.read_heartbeats(db) is None


def test_the_listener_beats_and_probes_its_tunnel(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    with health.listener_beats(db, "https://box.example.ts.net/linear-webhook",
                               machines_path=machines_file(tmp_path), fetch=lambda url, t: 405,
                               interval=60.0, log=lambda line: None):
        deadline = time.monotonic() + 5
        while len(health.read_heartbeats(db) or ()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        live = {r.component: r for r in health.read_heartbeats(db) or ()}
        assert set(live) == {"linear-listener", "tunnel"}
        assert all(r.last_seen is not None for r in live.values())
    assert all(r.stopped_at is not None for r in health.read_heartbeats(db) or ())

    plain = health.listener_beats(db, None, machines_path=machines_file(tmp_path))
    assert plain.tunnel is None


def test_the_linear_listener_passes_its_public_url(tmp_path: Path, monkeypatch) -> None:
    from lib import cli as cli_module

    seen: list = []

    class Beats:
        def __init__(self, db, url, **kwargs):
            seen.append(url)

        def __enter__(self):
            raise KeyboardInterrupt

        def __exit__(self, *exc):
            return False

    import socket

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    db, _ = make_db(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("s" * 40)
    secret.chmod(0o600)
    config = tmp_path / "orch.yml"
    config.write_text(
        "poll_seconds: 60\nlinear:\n  api_key_env: LINEAR_KEY\n"
        "grok:\n  webhook_url_env: U\n  webhook_key_env: K\n"
        "trusted_linear_users:\n  user-1: [grok-bot]\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: main\n"
        "    linear_project_id: p1\n"
        f"linear_webhook:\n  bind: 127.0.0.1:{port}\n"
        f"  secret_file: {secret}\n"
        "  public_url: https://box.example.ts.net/linear-webhook\n"
    )
    monkeypatch.setattr(health, "listener_beats", Beats)
    code = cli_module.main(["linear-webhook", "serve", "--db", str(db), "--orch", str(config)])
    assert (code, seen) == (130, ["https://box.example.ts.net/linear-webhook"])


@pytest.mark.parametrize("every", [60, 720, 1440])
def test_a_scheduled_dashboard_push_stays_up_between_pushes(tmp_path: Path, monkeypatch, every: int) -> None:
    """A9: the dashboard writer's interval follows every supported push schedule."""
    from lib import dashboard_cli

    db, _ = make_db(tmp_path)
    config = tmp_path / "orch.yml"
    config.write_text(ORCH_EXAMPLE.read_text().replace(
        "    method: scp\n", f"    method: scp\n    every_minutes: {every}\n"))

    class Sent:
        def send(self, paths):
            return None

    monkeypatch.setattr(dashboard_cli, "transport_for", lambda c: Sent())
    for _ in range(2):
        assert dashboard_cli.main(["push", "--once", "--db", str(db), "--out", str(tmp_path / "out"),
                                   "--machines", str(machines_file(tmp_path)),
                                   "--log", str(tmp_path / "p.log"), "--orch", str(config)]) == 0
    (dashboard,) = [r for r in health.read_heartbeats(db) or () if r.component == "dashboard"]
    assert dashboard.interval_seconds == every * 60.0
    assert health.classify(dashboard, dashboard.last_seen + every * 60)[0] == "up"


# --- review round 2 (PR #98): each class the review named ------------------------------------


def _published(tmp_path: Path, monkeypatch, *, every: int | None = None, live: bool = False) -> dict:
    """status.json as one real `orch dashboard push --once` sends it, with a
    queued codex job, a live poller and a live codex waiter; dated PAGE_NOW
    so the page harness's clock is 3 s after it."""
    from lib import dashboard_cli

    tmp_path.mkdir(parents=True, exist_ok=True)
    db, queue = make_db(tmp_path, time.time())
    queue.enqueue(request("codex"), dedupe_key="queued")
    health.HealthStore(db, devices={"box"}).beat("poller", "box", pid=101)
    health.HealthStore(db, devices={"box"}).beat("waiter", "box", bot="codex-bot", pid=102)
    config = tmp_path / "orch.yml"
    extra = (f"    every_minutes: {every}\n" if every else "") + ("    live: true\n" if live else "")
    config.write_text(ORCH_EXAMPLE.read_text().replace("    method: scp\n", "    method: scp\n" + extra))
    sent: list[dict] = []

    class Sent:
        def send(self, paths):
            sent.append(json.loads(paths[0].read_text()))

    monkeypatch.setattr(dashboard_cli, "transport_for", lambda c: Sent())
    assert dashboard_cli.main(["push", "--once", "--db", str(db), "--out", str(tmp_path / "out"),
                               "--machines", str(machines_file(tmp_path, ONLY_CODEX)),
                               "--log", str(tmp_path / "p.log"), "--orch", str(config)]) == 0
    (data,) = sent
    data["generated_at_epoch"] = PAGE_NOW
    return data


def _states(text: str) -> list[str]:
    return re.findall(r"UNKNOWN|STALE|DOWN|UP", text.partition("process details")[2])


@pytest.mark.parametrize("every, live, seconds", [(5, False, 300), (1440, False, 86400),
                                                  (None, False, 30), (5, True, 30)])
def test_a_push_publishes_its_own_cadence_and_its_own_heartbeat(
    tmp_path: Path, monkeypatch, every, live, seconds,
) -> None:
    """R2-1/R2-4: the snapshot says how often it is published (a live push
    publishes every poll cycle), and the first push already shows its writer up."""
    data = _published(tmp_path, monkeypatch, every=every, live=live)
    assert data["health"]["publish_seconds"] == seconds
    rows = {c["component"]: (c["state"], c["reason"], c["interval_seconds"])
            for c in data["health"]["components"]}
    assert rows["dashboard"] == ("up", "", seconds)
    assert rows["poller"][0] == rows["waiter"][0] == "up"
    assert data["attention"]["count"] == 0


@pytest.mark.parametrize("advance", [0, 121, 299, 600, 897])  # the clock starts 3 s in
def test_healthy_components_stay_up_between_scheduled_pushes(tmp_path: Path, monkeypatch, advance) -> None:
    """R2-1/R2-2: within its publication window a snapshot keeps every
    state it observed, and nothing on the page is broken: no Broken now, no
    no-waiter panel, and the no-waiter anchor lands on Components."""
    from tests.test_dashboard_layout import BAR, NODE, _run_page, attention_count

    if NODE is None:
        pytest.skip("node is not installed")
    data = _published(tmp_path / "push", monkeypatch, every=5)
    (tmp_path / "page").mkdir()
    out = _run_page(tmp_path / "page", data, "health", BAR + ["hud", "broken", "components", "waiters"],
                    actions=[["advance", advance]])
    page = out["text"]
    assert _states(page["components"]) == ["UP", "UP", "UP"]
    assert attention_count(out) == "0"
    assert out["hidden"]["broken"] and out["hidden"]["waiters"]
    assert page["no-waiter"].startswith("Components")


def test_an_overdue_snapshot_shows_unknown_and_only_its_writer_alerts(tmp_path: Path, monkeypatch) -> None:
    """R2-1/R2-2: once the next push is overdue the page cannot see the other
    components, so they are unknown, not down; the writer itself ages to
    stale and then down, which is the one needs-attention item, and no
    panel vouches for the waiters the snapshot can no longer see."""
    from tests.test_dashboard_layout import BAR, NODE, _run_page, attention_count

    if NODE is None:
        pytest.skip("node is not installed")
    data = _published(tmp_path / "push", monkeypatch, every=5)
    ids = BAR + ["hud", "broken", "components", "waiters"]
    (tmp_path / "stale").mkdir()
    stale_out = _run_page(tmp_path / "stale", data, "health", ids, actions=[["advance", 901]])
    stale = stale_out["text"]
    assert _states(stale["components"]) == ["STALE", "UNKNOWN", "UNKNOWN"]
    assert "DOWN" not in stale["components"]
    assert attention_count(stale_out) == "0"
    assert stale_out["hidden"]["waiters"]
    assert "no snapshot for " in stale["components"]
    assert "dashboard" in stale["broken"] and "waiter" not in stale["broken"]

    (tmp_path / "down").mkdir()
    down = _run_page(tmp_path / "down", data, "activity", BAR + ["hud", "attention"],
                     actions=[["advance", 8 * 300 + 1]])
    assert attention_count(down) == "1"
    assert "dashboard @ box" in down["text"]["attention"]
    assert "poller" not in down["text"]["attention"] and "waiter" not in down["text"]["attention"]


def _overdue_page(tmp_path: Path, *, waiter_seen: float, writer_interval: int,
                  publish_seconds: int | None, push: bool, advance: int) -> dict:
    from tests.test_dashboard_layout import BAR, _run_page

    db, queue = make_db(tmp_path, PAGE_NOW)
    queue.enqueue(request("codex"), dedupe_key="queued")
    store(db, PAGE_NOW).beat("poller", "box", pid=101)
    store(db, PAGE_NOW + waiter_seen).beat("waiter", "box", bot="codex-bot", pid=102)
    store(db, PAGE_NOW).beat("dashboard", "box", pid=103, interval=writer_interval)
    data = collect_snapshot(db, tmp_path / "poller.log", now=PAGE_NOW,
                            machines_path=machines_file(tmp_path, ONLY_CODEX), alive=None,
                            orch=orch(push=push), publish_seconds=publish_seconds)
    (tmp_path / "page").mkdir()
    return _run_page(tmp_path / "page", data, "health", BAR + ["hud", "broken", "components", "waiters"],
                     actions=[["advance", advance]])


@pytest.mark.parametrize("waiter_seen, expected", [
    (0, ["STALE", "UNKNOWN", "UNKNOWN"]),
    (-60, ["STALE", "UNKNOWN", "UNKNOWN"]),
    (-100, ["STALE", "UNKNOWN", "UNKNOWN"]),
])
def test_an_overdue_snapshot_vouches_for_no_state_past_its_up_window(
    tmp_path: Path, waiter_seen, expected,
) -> None:
    """R3-2: once the next push is overdue, a component the snapshot already
    showed stale is as unseen as one it showed up: it is unknown, says how
    long the snapshot is overdue, and no panel vouches for its waiter."""
    from tests.test_dashboard_layout import NODE

    if NODE is None:
        pytest.skip("node is not installed")
    out = _overdue_page(tmp_path, waiter_seen=waiter_seen, writer_interval=15,
                        publish_seconds=15, push=True, advance=46)
    page = out["text"]
    assert _states(page["components"]) == expected
    assert out["hidden"]["waiters"]
    assert "no snapshot for " in page["components"]
    assert "waiter" not in page["broken"]


def test_a_retired_writer_row_does_not_hide_an_old_one_off_snapshot(tmp_path: Path) -> None:
    """R3-3: only a dashboard row that can alert stands in for the old
    snapshot; a retired (not configured) one still leaves the one
    needs-attention item."""
    from tests.test_dashboard_layout import NODE, attention_count

    if NODE is None:
        pytest.skip("node is not installed")
    out = _overdue_page(tmp_path, waiter_seen=0, writer_interval=300,
                        publish_seconds=None, push=False, advance=3000)
    assert _states(out["text"]["components"]) == ["DOWN", "UNKNOWN", "UNKNOWN"]
    assert attention_count(out) == "1"


@pytest.mark.parametrize(
    "component, device, interval",
    [("device-probe", "pc", health.PROBE_SECONDS), ("tunnel", "box", health.BEAT_SECONDS)],
)
@pytest.mark.parametrize("latest_failed", [True, False])
def test_a_check_shows_its_latest_result_across_restarts(
    tmp_path: Path, component, device, interval, latest_failed,
) -> None:
    """R2-3: after a poller or listener restart, the newest check decides,
    whichever process made it; a failure is stale with the last success."""
    db, _ = make_db(tmp_path)
    if latest_failed:
        store(db, NOW - 10).beat(component, device, pid=10, interval=interval)
        store(db, NOW).checked(component, device, pid=11, interval=interval)
        expected = [(11, "stale", "unreachable, last reached 10s ago")]
    else:
        store(db, NOW - 10).checked(component, device, pid=10, interval=interval)
        store(db, NOW).beat(component, device, pid=11, interval=interval)
        expected = [(11, "up", "")]
    report = health.build_report(health.read_heartbeats(db), NOW,
                                 expected=health.Expected((), frozenset()))
    assert [(c.pid, c.state, c.reason) for c in report.components] == expected


# --- #107: expectations and stale code ---------------------------------------------------------


def test_a_refused_harness_has_no_waiter_slot(tmp_path: Path) -> None:
    """#107 c: a refused harness's leftover waiter row is not configured, not down."""
    rows = (row(component="poller", bot="", pid=1),
            row(bot="cursor-bot", pid=2, last_seen=NOW - 900))
    report = health.build_report(rows, NOW, expected=health.expected_components(
        machines_config(tmp_path), orch(refused=("cursor",))))
    cursor = [c for c in report.components if c.bot == "cursor-bot"]
    assert [(c.state, c.configured) for c in cursor] == [("down", False)]
    assert sorted(c.label for c in report.down if c.component == "waiter") == [
        "waiter claude-bot @ box", "waiter codex-bot @ box"]


def test_the_tunnel_is_expected_only_with_a_public_url(tmp_path: Path) -> None:
    """#107 d: a listener without linear_webhook.public_url has no tunnel row."""
    machines = machines_config(tmp_path)
    without = health.expected_components(machines, orch(listener=True, public_url=None))
    with_url = health.expected_components(machines, orch(listener=True))
    assert {s.component for s in without.slots} >= {"linear-listener"}
    assert "tunnel" not in {s.component for s in without.slots}
    assert "tunnel" in {s.component for s in with_url.slots}


def test_heartbeats_record_the_commit_the_process_loaded(tmp_path: Path) -> None:
    """#107 b: a beat names the HEAD loaded at import, not the HEAD at beat time."""
    db, _ = make_db(tmp_path)
    beater = health.Beater(db, "poller", devices=["box"], store=store(db), log=lambda line: None)
    assert beater.version == health.LOADED_VERSION
    beater.beat_now()
    (beat,) = health.read_heartbeats(db) or ()
    assert beat.version == health.LOADED_VERSION


@pytest.mark.parametrize(
    "component, version, checkout, stale",
    [
        ("poller", "0123456789ab", "ba9876543210", True),
        ("waiter", "0123456789ab", "ba9876543210", True),
        ("linear-listener", "0123456789ab", "ba9876543210", True),
        ("poller", "0123456789ab", "0123456789ab", False),
        ("poller", "unknown", "ba9876543210", False),
        ("poller", "0123456789ab", "unknown", False),
        ("device-probe", "0123456789ab", "ba9876543210", False),
        ("tunnel", "0123456789ab", "ba9876543210", False),
        ("dashboard", "0123456789ab", "ba9876543210", False),
    ],
)
def test_stale_code_is_a_running_process_on_another_commit(
    component: str, version: str, checkout: str, stale: bool
) -> None:
    rows = (row(component=component, bot="codex-bot" if component == "waiter" else "",
                device="pc" if component == "device-probe" else "box", version=version),)
    report = health.build_report(rows, NOW, checkout_version=checkout)
    (item,) = [c for c in report.components if c.component == component]
    assert item.stale_code is stale
    assert item.state == "up"
    assert report.down == () or component != "poller"


def test_stale_code_is_shown_in_text_and_json_but_is_not_down() -> None:
    rows = (row(component="poller", bot="", pid=7, version="0123456789ab"),)
    report = health.build_report(rows, NOW, checkout_version="ba9876543210")
    lines = health.render_lines(report, NOW)
    assert lines[0] == "Health (1 components, 0 down, 1 stale code)"
    assert "poller @ box up, stale code (running 0123456789ab, checkout ba9876543210)" in lines[1]
    data = health.report_json(report, NOW)
    assert data["code_version"] == "ba9876543210"
    assert data["stale_code"] == 1
    (poller,) = data["components"]
    assert poller["stale_code"] is True
    assert poller["started_at"] == NOW - 600
    assert data["down"] == 0


def test_orch_status_shows_stale_code(tmp_path: Path, monkeypatch) -> None:
    from tests.test_wait import dispatcher_for

    db, _ = make_db(tmp_path)
    dispatcher_for(db)
    store(db, NOW - 5).beat("poller", "box", pid=101, version="0123456789ab")
    monkeypatch.setattr(health, "factory_version", lambda root=health.ROOT: "ba9876543210")
    out = io.StringIO()
    assert status_main(["--db", str(db), "--machines", str(machines_file(tmp_path, ONLY_CODEX)),
                        "--orch", str(ORCH_EXAMPLE)], stdout=out, clock=lambda: NOW) == 0
    assert "stale code (running 0123456789ab, checkout ba9876543210)" in out.getvalue()


def test_the_health_tab_marks_stale_code_without_raising_attention(tmp_path: Path, monkeypatch) -> None:
    from tests.test_dashboard_layout import BAR, NODE, _run_page, attention_count

    monkeypatch.setattr(health, "factory_version", lambda root=health.ROOT: "ba9876543210")
    db, _ = make_db(tmp_path, PAGE_NOW)
    store(db, PAGE_NOW - 5).beat("poller", "box", pid=101, started_at=PAGE_NOW - 900,
                                 version="0123456789ab")
    store(db, PAGE_NOW - 5).beat("waiter", "box", bot="codex-bot", pid=102,
                                 started_at=PAGE_NOW - 900, version="ba9876543210")
    data = snapshot(tmp_path, db, PAGE_NOW, machines=ONLY_CODEX)
    components = {c["component"]: c for c in data["health"]["components"]}
    assert components["poller"]["stale_code"] is True
    assert components["waiter"]["stale_code"] is False
    assert data["attention"]["count"] == 0
    page = render_pages(data)["health.html"]
    assert "0123456789ab<span class=\"warn\"> stale code</span>" in page
    if NODE is None:
        return
    (tmp_path / "health").mkdir()
    rendered = _run_page(tmp_path / "health", data, "health", BAR + ["hud", "components"])
    assert "0123456789ab stale code" in rendered["text"]["components"]
    assert attention_count(rendered) == "0"
# --- #106: dashboard push failures -------------------------------------------------------


def push_row(**overrides) -> health.Heartbeat:
    return row(component="dashboard-push", bot="", interval_seconds=30.0, **overrides)


def pushes(report: health.HealthReport) -> list[health.Component]:
    return [c for c in report.components if c.component == "dashboard-push"]


POLLER_UP = row(component="poller", bot="", pid=1)


def test_one_failed_push_makes_dashboard_push_stale_with_how_long() -> None:
    report = health.build_report((POLLER_UP, push_row(last_seen=NOW - 40, checked_at=NOW - 5)), NOW)
    (item,) = pushes(report)
    assert (item.state, item.reason) == ("stale", "push failing, last pushed 40s ago")
    assert report.down == ()


def test_pushes_failing_for_eight_intervals_are_down_and_need_attention() -> None:
    report = health.build_report((POLLER_UP, push_row(last_seen=NOW - 300, checked_at=NOW - 5)), NOW)
    (item,) = pushes(report)
    assert (item.state, item.reason) == ("down", "push failing, last pushed 5m ago")
    assert [c.label for c in report.down] == ["dashboard-push @ box"]
    assert "  dashboard-push @ box down (push failing, last pushed 5m ago)" in "\n".join(
        health.render_lines(report, NOW))


def test_a_push_that_never_succeeded_is_down() -> None:
    report = health.build_report((POLLER_UP, push_row(last_seen=None, checked_at=NOW - 5)), NOW)
    assert [(c.state, c.reason) for c in pushes(report)] == [("down", "never pushed")]


def test_a_success_clears_dashboard_push(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 300).beat("dashboard-push", "box", pid=7, interval=30)
    store(db, NOW - 10).checked("dashboard-push", "box", pid=7, interval=30)
    assert [c.state for c in pushes(health.load_report(db, NOW))] == ["down"]
    store(db, NOW - 1).beat("dashboard-push", "box", pid=7, interval=30)
    assert [(c.state, c.reason) for c in pushes(health.load_report(db, NOW))] == [("up", "")]


def test_a_restarted_pollers_failure_outranks_the_old_pollers_success(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 20).beat("dashboard-push", "box", pid=7, interval=30)  # the old poller
    store(db, NOW - 5).checked("dashboard-push", "box", pid=8, interval=30)  # after a restart
    (item,) = pushes(health.load_report(db, NOW))
    assert (item.state, item.reason) == ("stale", "push failing, last pushed 20s ago")



HEAD_SHA = "1a2b3c4d5e6f" + "0" * 28
MAIN_SHA = "9f8e7d6c5b4a" + "1" * 28


def write_sync(db: Path, *, device: str = "mac", bot: str = "claude-bot", state: str = "lagging",
               sha: str | None = HEAD_SHA, detail: str = "", since: float = NOW) -> None:
    from lib import device_sync

    connection = sqlite3.connect(db)
    try:
        with connection:
            device_sync.upsert(connection, device=device, bot=bot, harness="claude",
                               checkout=f"/Users/{bot}/factory", target=MAIN_SHA, sha=sha,
                               state=state, detail=detail, now=since)
    finally:
        connection.close()


def fake_checkout(root: Path, head: str, origin_main: str | None) -> Path:
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git / "refs" / "remotes" / "origin").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(head + "\n", encoding="utf-8")
    if origin_main is not None:
        (git / "refs" / "remotes" / "origin" / "main").write_text(origin_main + "\n", encoding="utf-8")
    return root


def device_report(tmp_path: Path, now: float = NOW, *, head: str = MAIN_SHA,
                  origin_main: str | None = MAIN_SHA) -> health.HealthReport:
    db = tmp_path / "orch.db"
    root = fake_checkout(tmp_path / "factory", head, origin_main)
    return health.load_report(db, now, machines=machines_config(tmp_path), root=root)


def device_json(tmp_path: Path, now: float = NOW, **checkout) -> dict:
    return health.report_json(device_report(tmp_path, now, **checkout), now)


def remote(data: dict) -> list[dict]:
    return [d for d in data["devices"] if d["device"] != "box"]


def test_the_report_lists_each_checkouts_sync_state(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    write_sync(db, state="lagging", detail="waiting for 1 job", since=NOW - 60)
    data = device_json(tmp_path)
    box, mac = data["devices"]
    assert box == {"device": "box", "bot": "", "checkout": str(tmp_path / "factory"),
                   "sha": MAIN_SHA, "target": MAIN_SHA, "state": "synced", "since": None,
                   "detail": "", "needs_attention": False}
    assert mac == {"device": "mac", "bot": "claude-bot", "checkout": "/Users/claude-bot/factory",
                   "sha": HEAD_SHA, "target": MAIN_SHA, "state": "lagging", "since": NOW - 60,
                   "detail": "waiting for 1 job", "needs_attention": False}
    assert data["devices_out_of_sync"] == 0


@pytest.mark.parametrize("state, sha", [("missing", None), ("refused", HEAD_SHA)])
def test_a_missing_or_refused_checkout_needs_attention_at_once(
    tmp_path: Path, state: str, sha: str | None
) -> None:
    db, _ = make_db(tmp_path)
    write_sync(db, state=state, sha=sha, since=NOW)
    data = device_json(tmp_path)
    (mac,) = remote(data)
    assert mac["needs_attention"] is True
    assert data["devices_out_of_sync"] == 1


@pytest.mark.parametrize("state", ["lagging", "deploying"])
def test_a_lagging_checkout_needs_attention_only_after_the_grace(tmp_path: Path, state: str) -> None:
    db, _ = make_db(tmp_path)
    write_sync(db, state=state, since=NOW)
    grace = 15 * 60
    early = device_json(tmp_path, NOW + grace - 1)
    late = device_json(tmp_path, NOW + grace)
    (shown,) = remote(early)
    assert shown["state"] == state
    assert early["devices_out_of_sync"] == 0
    assert late["devices_out_of_sync"] == 1


def test_a_box_behind_origin_main_is_lagging_and_needs_attention(tmp_path: Path) -> None:
    make_db(tmp_path)
    report = device_report(tmp_path, head=HEAD_SHA, origin_main=MAIN_SHA)
    (box,) = health.report_json(report, NOW)["devices"]
    assert (box["device"], box["state"], box["needs_attention"]) == ("box", "lagging", True)
    assert report.devices_out_of_sync == report.devices
    assert "  device box lagging at 1a2b3c4d5e6f, target 9f8e7d6c5b4a" in health.render_lines(report, NOW)


def test_a_box_with_no_origin_main_is_not_listed(tmp_path: Path) -> None:
    make_db(tmp_path)
    assert device_json(tmp_path, origin_main=None)["devices"] == []


def test_an_out_of_sync_device_is_one_status_line(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db).beat("poller", "box", pid=7)
    write_sync(db, state="refused", detail="local commits")
    write_sync(db, device="pc", bot="codex-bot", state="synced", sha=MAIN_SHA)
    lines = health.render_lines(device_report(tmp_path), NOW)
    assert lines[0].endswith(", 1 devices out of sync)")
    device_lines = [line for line in lines if line.startswith("  device ")]
    assert device_lines == [
        "  device mac claude-bot refused at 1a2b3c4d5e6f, target 9f8e7d6c5b4a (local commits)"
    ]


def test_no_device_sync_table_lists_only_the_box(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db).beat("poller", "box", pid=7)
    data = device_json(tmp_path)
    assert [d["device"] for d in data["devices"]] == ["box"]
    assert data["devices_out_of_sync"] == 0
    assert data["known"] is True


def test_an_unreadable_orch_db_lists_no_remote_devices(tmp_path: Path) -> None:
    (tmp_path / "orch.db").write_bytes(b"not a database")
    assert remote(device_json(tmp_path)) == []


def test_devices_out_of_sync_leave_orch_health_exit_alone(tmp_path: Path, capsys) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 5).beat("poller", "box", pid=7)
    argv = ["--db", str(db), "--machines", str(machines_file(tmp_path)), "--json"]
    before = health.main(argv, clock=lambda: NOW)
    write_sync(db, state="missing", sha=None)
    capsys.readouterr()
    after = health.main(argv, clock=lambda: NOW)
    assert json.loads(capsys.readouterr().out)["devices_out_of_sync"] >= 1
    assert after == before


def _tunnel(report: health.HealthReport) -> list[health.Component]:
    return [c for c in report.components if c.component == "tunnel"]


def test_a_tunnel_whose_port_another_owner_holds_says_so(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 600).beat("tunnel", "box", pid=7)
    store(db, NOW - 5).checked("tunnel", "box", pid=7)
    assert [c.reason for c in _tunnel(health.load_report(db, NOW))] == [
        "unreachable, last reached 10m ago"]
    (db.parent / "linear-webhook-tunnel.held").write_text("8788 pc-wsl-cursor-bot\n")
    report = health.load_report(db, NOW)
    assert [(c.state, c.reason) for c in _tunnel(report)] == [
        ("down", "port held by another owner: 8788 on pc-wsl-cursor-bot")]
    store(db, NOW - 1).beat("tunnel", "box", pid=7)
    assert [(c.state, c.reason) for c in _tunnel(health.load_report(db, NOW))] == [
        ("down", "port held by another owner: 8788 on pc-wsl-cursor-bot")], (
        "a 405 from whoever holds the port is not this tunnel")
    (db.parent / "linear-webhook-tunnel.held").unlink()
    assert [(c.state, c.reason) for c in _tunnel(health.load_report(db, NOW))] == [("up", "")]


def test_a_held_port_shows_on_a_tunnel_that_never_reported() -> None:
    registry = health.Expected((health.Slot("tunnel", ("box",)),), frozenset({"tunnel"}))
    report = health.build_report([], NOW, expected=registry, tunnel_held="8787 on pc-wsl-cursor-bot")
    assert [(c.state, c.reason) for c in _tunnel(report)] == [
        ("down", "port held by another owner: 8787 on pc-wsl-cursor-bot")]


def test_a_held_record_names_any_host_the_service_accepts(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    host = "user@" + "a" * 30 + "." + "b" * 30 + ".com"
    (db.parent / "linear-webhook-tunnel.held").write_text(f"8787 {host}\n")
    assert health.read_tunnel_held(db) == f"8787 on {host}"


def test_a_malformed_held_record_is_ignored(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    (db.parent / "linear-webhook-tunnel.held").write_text("8787 $(evil)\n")
    assert health.read_tunnel_held(db) is None


def test_a_held_port_shows_before_any_component_has_reported(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    assert not health.health_path(db).exists()
    (db.parent / "linear-webhook-tunnel.held").write_text("8787 pc-wsl-cursor-bot\n")
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert report.known
    assert [(c.state, c.reason) for c in _tunnel(report)] == [
        ("down", "port held by another owner: 8787 on pc-wsl-cursor-bot")]


HELD_REASON = "port held by another owner: 8787 on pc-wsl-cursor-bot"
TUNNEL_SLOT = health.Expected((health.Slot("tunnel", ("box",)),), frozenset({"tunnel"}))


def hold_port(db: Path, since: float) -> None:
    held = db.parent / "linear-webhook-tunnel.held"
    held.write_text("8787 pc-wsl-cursor-bot\n")
    os.utime(held, (since, since))


@pytest.mark.parametrize("minutes, attention", [
    (29, []),
    (31, [health.Attention("tunnel-down", "tunnel @ box", "unreachable, last reached 31m ago",
                           NOW - 31 * 60, "health-tunnel-box")]),
])
def test_a_tunnel_down_longer_than_the_alert_minutes_needs_attention(minutes: int, attention) -> None:
    rows = (row(component="tunnel", bot="", last_seen=NOW - minutes * 60, checked_at=NOW - 5),)
    report = health.build_report(rows, NOW, expected=TUNNEL_SLOT, tunnel_alert_seconds=30 * 60)
    assert [c.state for c in _tunnel(report)] == ["down"]
    assert list(report.attention) == attention


def test_a_tunnel_needs_attention_on_every_read_until_it_is_up(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 3600).beat("tunnel", "box", pid=7)
    store(db, NOW - 5).checked("tunnel", "box", pid=7)
    for now in (NOW, NOW + 600, NOW + 7200):
        report = health.load_report(db, now, orch=orch(listener=True))
        assert [(a.kind, a.label, a.since) for a in report.attention] == [
            ("tunnel-down", "tunnel @ box", NOW - 3600)], now
    store(db, NOW + 7201).beat("tunnel", "box", pid=7)
    report = health.load_report(db, NOW + 7205, orch=orch(listener=True))
    assert [c.state for c in _tunnel(report)] == ["up"]
    assert report.attention == ()


def test_a_held_tunnel_never_seen_is_down_since_its_held_record(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    hold_port(db, NOW - 31 * 60)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [(a.kind, a.reason, a.since) for a in report.attention] == [
        ("tunnel-down", HELD_REASON, NOW - 31 * 60)]
    hold_port(db, NOW - 29 * 60)
    assert health.load_report(db, NOW, orch=orch(listener=True)).attention == ()


def test_a_held_tunnel_down_past_the_alert_keeps_its_reason_on_the_item(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 7200).beat("tunnel", "box", pid=7)
    store(db, NOW - 5).checked("tunnel", "box", pid=7)
    hold_port(db, NOW - 7000)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [(c.state, c.reason) for c in _tunnel(report)] == [("down", HELD_REASON)]
    assert [(a.reason, a.since) for a in report.attention] == [(HELD_REASON, NOW - 7200)]


def test_a_held_tunnel_that_still_answers_is_down_since_the_held_record(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 1).beat("tunnel", "box", pid=7)
    hold_port(db, NOW - 3600)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [(a.reason, a.since) for a in report.attention] == [(HELD_REASON, NOW - 3600)]


def test_a_tunnel_never_reached_keeps_its_item_across_a_listener_restart(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 31 * 60).checked("tunnel", "box", pid=101, started_at=NOW - 31 * 60)
    store(db, NOW - 60).checked("tunnel", "box", pid=101, started_at=NOW - 31 * 60)
    store(db, NOW - 30).stop("tunnel", "box", pid=101)
    store(db, NOW).checked("tunnel", "box", pid=102, started_at=NOW - 29)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [(c.state, c.pid) for c in _tunnel(report)] == [("down", 102)]
    assert [(a.kind, a.since) for a in report.attention] == [("tunnel-down", NOW - 31 * 60)]


def test_a_tunnel_never_reached_keeps_its_item_when_the_restarted_listener_reuses_the_pid(
        tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 31 * 60).checked("tunnel", "box", pid=101, started_at=NOW - 31 * 60)
    store(db, NOW - 60).checked("tunnel", "box", pid=101, started_at=NOW - 31 * 60)
    store(db, NOW - 30).stop("tunnel", "box", pid=101)
    store(db, NOW).checked("tunnel", "box", pid=101, started_at=NOW - 29)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [(c.state, c.pid) for c in _tunnel(report)] == [("down", 101)]
    assert [(a.kind, a.since) for a in report.attention] == [("tunnel-down", NOW - 31 * 60)]
    store(db, NOW + 60).beat("tunnel", "box", pid=101, started_at=NOW - 29)
    assert health.load_report(db, NOW + 61, orch=orch(listener=True)).attention == ()


def test_a_tunnel_down_in_a_database_from_before_down_since_keeps_its_item_through_the_upgrade(
        tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 31 * 60).checked("tunnel", "box", pid=101, started_at=NOW - 31 * 60)
    store(db, NOW - 60).checked("tunnel", "box", pid=101, started_at=NOW - 31 * 60)
    with sqlite3.connect(health.health_path(db)) as connection:
        connection.execute(f"ALTER TABLE {health.TABLE} DROP COLUMN down_since")
    assert [(a.kind, a.since) for a in health.load_report(
        db, NOW - 30, orch=orch(listener=True)).attention] == [("tunnel-down", NOW - 31 * 60)]
    store(db, NOW - 30).stop("tunnel", "box", pid=101)
    store(db, NOW).checked("tunnel", "box", pid=101, started_at=NOW - 29)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [(c.state, c.pid) for c in _tunnel(report)] == [("down", 101)]
    assert [(a.kind, a.since) for a in report.attention] == [("tunnel-down", NOW - 31 * 60)]


def test_a_tunnel_last_reached_before_a_reboot_keeps_its_item_when_the_pid_is_reused(
        tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    before, after = "1" * 8 + "-1111-1111-1111-" + "1" * 12, "2" * 8 + "-2222-2222-2222-" + "2" * 12
    health.HealthStore(db, devices=DEVICES, clock=lambda: NOW - 3600,
                       uptime=lambda: health.Uptime(before, 50_000.0)).beat(
        "tunnel", "box", pid=101, started_at=NOW - 4000)
    health.HealthStore(db, devices=DEVICES, clock=lambda: NOW,
                       uptime=lambda: health.Uptime(after, 40.0)).checked(
        "tunnel", "box", pid=101, started_at=NOW - 40)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [c.state for c in _tunnel(report)] == ["down"]
    assert [(a.kind, a.since) for a in report.attention] == [("tunnel-down", NOW - 3600)]


def test_a_held_record_written_again_does_not_restart_a_never_reached_tunnels_clock(
        tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW).checked("tunnel", "box", pid=7, started_at=NOW - 2 * 3600)
    hold_port(db, NOW - 5 * 60)
    report = health.load_report(db, NOW, orch=orch(listener=True))
    assert [(a.reason, a.since) for a in report.attention] == [(HELD_REASON, NOW - 2 * 3600)]


def test_a_tunnel_never_checked_and_never_held_has_no_item() -> None:
    report = health.build_report([], NOW, expected=TUNNEL_SLOT, tunnel_alert_seconds=60)
    assert [c.state for c in _tunnel(report)] == ["down"]
    assert report.attention == ()


def test_orch_health_json_lists_a_long_tunnel_outage_as_needs_attention(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(health, "box_storage", functools.partial(
        health.box_storage, statvfs=lambda path: statvfs(inodes_used=120)))
    db, _ = make_db(tmp_path)
    store(db, NOW).beat("poller", "box", pid=101, version="unknown")
    hold_port(db, NOW - 22 * 3600)
    config = tmp_path / "orch.yml"
    config.write_text(
        "poll_seconds: 60\nlinear:\n  api_key_env: LINEAR_KEY\n"
        "grok:\n  webhook_url_env: U\n  webhook_key_env: K\n"
        "trusted_linear_users:\n  user-1: [grok-bot]\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: main\n"
        "    linear_project_id: p1\n"
        "linear_webhook:\n  bind: 127.0.0.1:8787\n  secret_file: /s\n"
        f"  public_url: {TUNNEL_URL}\n  tunnel_alert_minutes: 30\n"
    )
    args = ["--db", str(db), "--orch", str(config)]

    out = io.StringIO()
    assert health.main([*args, "--json"], stdout=out, clock=lambda: NOW,
                       transport=DfTransport({})) == 3
    data = json.loads(out.getvalue())
    assert data["attention"] == 1
    assert data["needs_attention"] == [{
        "kind": "tunnel-down", "label": "tunnel @ box", "reason": HELD_REASON,
        "down_since_et": "Nov 5 6:20:00 PM ET", "down_seconds": 22 * 3600,
        "anchor": "health-tunnel-box",
    }]

    out = io.StringIO()
    assert health.main(args, stdout=out, clock=lambda: NOW, transport=DfTransport({})) == 3
    assert f"  needs attention: tunnel @ box down 22h ({HELD_REASON})" in out.getvalue().splitlines()
