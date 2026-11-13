"""Component heartbeats and the Health tab (issue #92).

Fakes only: heartbeats go to a temp health database beside a temp orch.db,
device probes use a fake transport, and no thread sleeps for real.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from lib import health
from lib.cli import main as cli_main
from lib.dashboard import collect_snapshot
from lib.dashboard_page import PAGES, render_pages
from lib.machines_yml import load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import SSH_UNREACHABLE
from lib.status import main as status_main
from tests.test_wait import restore_signals  # noqa: F401  (fixture)

NOW = 1_794_000_000.0  # Nov 6, 2026, 4:20 PM EST
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
  - name: mac
    enabled: false
    tailscale_name: mac.example.ts.net
    slot_max: 2
    harnesses:
      claude: claude-bot
    workdir_root: /Users/{bot}/factory-work
    ssh_alias: mac-{bot}
"""


def make_db(tmp_path: Path) -> tuple[Path, SQLiteJobQueue]:
    path = tmp_path / "orch.db"
    return path, SQLiteJobQueue(path, clock=lambda: NOW - 30)


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
    return health.HealthStore(db, clock=lambda: clock)


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
    ]
    for record in values:
        component, device, bot, pid, started, seen, version, interval, stopped, checked = record
        assert component in health.COMPONENTS
        assert health.TOKEN.fullmatch(device)
        assert bot in ("", "claude-bot", "codex-bot", "cursor-bot")
        assert isinstance(pid, int)
        assert health.VERSION.fullmatch(version)
        for number in (started, seen, interval, stopped, checked):
            assert number is None or isinstance(number, float)
    raw = health.health_path(db).read_bytes()
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
        ("pid", 0),
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
    writer = health.HealthStore(db, clock=lambda: clock[0])
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

    beater = health.Beater(db, "poller", devices=["box"], store=Broken(db), log=lines.append)
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
    writer.beat("device-probe", "retired", pid=1, started_at=NOW, version="unknown", interval=60.0)
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


def snapshot(tmp_path: Path, db: Path, now: float = NOW) -> dict:
    return collect_snapshot(db, tmp_path / "poller.log", now=now,
                            machines_path=machines_file(tmp_path), alive=None)


def test_health_is_the_tab_after_efficiency(tmp_path: Path) -> None:
    assert PAGES[-2:] == ("efficiency.html", "health.html")
    db, _ = make_db(tmp_path)
    html = render_pages(snapshot(tmp_path, db))["index.html"]
    nav = html[html.index('id="nav"'):html.index("</nav>")]
    assert nav.index("efficiency.html") < nav.index('href="health.html"')
    assert ">Health</a>" in nav


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
    assert data["health"]["down"] == 1
    page = render_pages(data)["health.html"]
    assert 'id="components"' in page
    assert health.et(NOW - 5) in page
    assert ">101<" in page and ">stale<" in page.lower()


def test_a_down_component_shows_on_the_needs_attention_line(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    store(db, NOW - 5).beat("poller", "box", pid=101, started_at=NOW - 900, version="unknown")
    store(db, NOW - 900).beat("waiter", "box", bot="codex-bot", pid=202, started_at=NOW - 990, version="unknown")
    data = snapshot(tmp_path, db)
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


def test_orch_status_has_a_health_section(tmp_path: Path) -> None:
    from tests.test_wait import dispatcher_for

    db, queue = make_db(tmp_path)
    dispatcher_for(db)  # the tracker tables orch status reads
    queue.enqueue(request("codex"), dedupe_key="a")
    store(db, NOW - 5).beat("poller", "box", pid=101, started_at=NOW - 900, version="unknown")
    out = io.StringIO()
    code = status_main(["--db", str(db), "--machines", str(machines_file(tmp_path)),
                        "--orch", str(Path(__file__).resolve().parents[1] / "config" / "orch.example.yml")],
                       stdout=out, clock=lambda: NOW)
    assert code == 0
    text = out.getvalue()
    assert "\nHealth (1 components, 0 down)\n" in text
    assert "  poller @ box up, last seen " in text
    assert "  no waiter for codex: 1 queued job" in text
    assert "build -> codex queued 30s [no waiter for codex]" in text


def test_orch_health_exits_3_when_something_needs_attention(tmp_path: Path, capsys) -> None:
    db, queue = make_db(tmp_path)
    machines = machines_file(tmp_path)
    now = time.time()
    store(db, now).beat("poller", "box", pid=101, started_at=now, version="unknown")
    assert cli_main(["health", "--db", str(db), "--machines", str(machines)]) == 0
    assert "poller @ box up" in capsys.readouterr().out

    queue.enqueue(request("codex"), dedupe_key="a")
    assert cli_main(["health", "--db", str(db), "--machines", str(machines), "--json"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["no_waiter"][0]["harness"] == "codex"
    assert data["components"][0]["component"] == "poller"


def test_orch_health_beat_records_an_external_component(tmp_path: Path) -> None:
    db, _ = make_db(tmp_path)
    assert cli_main(["health", "beat", "tunnel", "--db", str(db), "--pid", str(os.getpid())]) == 0
    rows = health.read_heartbeats(db) or ()
    assert [(r.component, r.pid) for r in rows] == [("tunnel", os.getpid())]
    assert cli_main(["health", "beat", "tunnel", "--db", str(db), "--pid", "999999999"]) == 1
    assert cli_main(["health", "beat", "not a component", "--db", str(db)]) == 2
    assert cli_main(["health", "forget", "tunnel", "--db", str(db)]) == 0
    assert health.read_heartbeats(db) == ()


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

    with health.Beater(db, "waiter", devices=["box"], bot="codex-bot", store=Probe(db),
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
    from tests.test_dashboard_layout import BAR, NODE, _run_page

    if NODE is None:
        pytest.skip("node is not installed")
    db, queue = make_db(tmp_path)
    queue.enqueue(request("codex"), dedupe_key="a")
    store(db, NOW - 5).beat("poller", "box", pid=101, started_at=NOW - 900, version="unknown")
    store(db, NOW - 900).beat("linear-listener", "box", pid=303, started_at=NOW - 990, version="unknown")
    data = snapshot(tmp_path, db)
    (tmp_path / "health").mkdir()
    page = _run_page(tmp_path / "health", data, "health", BAR + ["hud", "components", "no-waiter"])
    text = page["text"]
    assert "poller" in text["components"] and "UP" in text["components"]
    assert "linear-listener" in text["components"] and "DOWN" in text["components"]
    assert health.et(NOW - 5) in text["components"]
    assert "no waiter for codex: 1 queued job" in text["no-waiter"]
    assert text["attention-count"] == "2"  # the listener is down, codex has no waiter
    (tmp_path / "main").mkdir()
    main = _run_page(tmp_path / "main", data, "main", BAR + ["hud", "kpis", "lanes", "queue"])
    assert "no waiter for codex" in main["text"]["queue"]
    assert "a.chip.bad[href=health.html#health-waiter-codex-bot]" in main["dump"]["queue"]
