import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import lib.dashboard as dashboard
from lib.dashboard import (
    REFRESH_DEFAULT,
    DashboardError,
    collect_snapshot,
    open_readonly,
    read_log_tail,
    redact,
    render_html,
    write_snapshot,
)
from lib.dispatcher import Dispatcher
from lib.queue import SQLiteJobQueue

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0

# Fake secrets, built at runtime so no literal token shape sits in the repo.
WEBHOOK_SECRET = "Zx9Qp" * 6
WEBHOOK_URL = "https://hooks.example.invalid/services/T000/B000/" + WEBHOOK_SECRET
CRED_PASSWORD = "s3cr3t" + "Pass" + "w0rd"
CRED_URL = "https://grok:" + CRED_PASSWORD + "@webhook.example.invalid/hook"
BEARER_TOKEN = "eyJhbGciOi" + "J9abc123DEF456ghi789"
GH_CLASSIC = "gh" + "p_" + "A1b2C3d4E5" * 4
GH_OAUTH = "gh" + "o_" + "Z9y8X7w6V5" * 4
GH_SERVER = "gh" + "s_" + "Q1w2E3r4T5" * 4
GH_PAT = "github" + "_pat_" + "11ABCDEFG0" + "x7Y" * 18
LINEAR_KEY = "lin" + "_api_" + "Q7w8E9r0T1" * 4
EMAIL = "max.secret" + "@" + "example.com"
KEY_VALUE = "kv-" + "9f8e7d6c5b4a" * 2
ENV_ONLY = "env" + "-only-" + "Tq3Wm8Zr" * 3
DETAIL_ONLY = "gh" + "p_" + "D3t4i1O9n1y" * 4

FREE_TEXT_SECRETS = {
    "webhook url": WEBHOOK_URL,
    "webhook path secret": WEBHOOK_SECRET,
    "url password": CRED_PASSWORD,
    "bearer token": BEARER_TOKEN,
    "gh classic token": GH_CLASSIC,
    "gh oauth token": GH_OAUTH,
    "gh server token": GH_SERVER,
    "gh fine-grained pat": GH_PAT,
    "linear key": LINEAR_KEY,
    "email": EMAIL,
    "key=value secret": KEY_VALUE,
}
# Never selected (env) or never on the allowlist (jobs.detail).
OFF_ALLOWLIST_SECRETS = {
    "env value": ENV_ONLY,
    "job detail": DETAIL_ONLY,
}


class _Unused:
    pass


def make_db(path: Path) -> SQLiteJobQueue:
    queue = SQLiteJobQueue(path, clock=lambda: NOW)
    Dispatcher(
        path,
        queue,
        trusted_logins={"github": {"maxharris1": {"grok-bot"}}},
        review_actions=_Unused(),
        grok_delivery=_Unused(),
    )
    return queue


def add_job(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    action: str = "build",
    recipient: str = "claude",
    state: str = "queued",
    repo: str = "maxharris1/cleffy",
    ticket_id: str = "12",
    created_at: float = NOW - 600,
    last_activity_at: float = NOW - 60,
    timeout_seconds: int = 5400,
    handoff_comment_id: str | None = None,
    detail: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO jobs (
            id, dedupe_key, repo, ticket_id, action, recipient,
            source_comment_id, timeout_seconds, generation, attempt,
            head, detail, state, created_at, last_activity_at,
            finished_at, handoff_comment_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, NULL, ?, ?, ?, ?, NULL, ?)
        """,
        (
            job_id,
            f"dedupe-{job_id}",
            repo,
            ticket_id,
            action,
            recipient,
            f"comment-{job_id}",
            timeout_seconds,
            detail,
            state,
            created_at,
            last_activity_at,
            handoff_comment_id,
        ),
    )


def add_ticket(
    connection: sqlite3.Connection,
    repo: str,
    ticket_id: str,
    *,
    tracker: str = "github",
    state: str | None = "building",
    review_round: int = 0,
    gate_count: int = 0,
    stuck: int = 0,
    stuck_reason: str | None = None,
    paused_action: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at) "
        "VALUES (?, ?, ?, ?)",
        (repo, ticket_id, tracker, NOW - 3600),
    )
    connection.execute(
        """
        INSERT INTO ticket_state (
            repo, ticket_id, gate_count, stuck, workflow_state,
            review_round, stuck_reason, paused_action
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (repo, ticket_id, gate_count, stuck, state, review_round, stuck_reason, paused_action),
    )


def add_poll_error(
    connection: sqlite3.Connection,
    error: str,
    *,
    repo: str = "maxharris1/cleffy",
    ticket_id: str = "-",
    recorded_at: float = NOW - 30,
) -> None:
    connection.execute(
        "INSERT INTO poll_errors (repo, ticket_id, error, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        (repo, ticket_id, error, recorded_at),
    )


def seeded(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        add_job(connection, "q-claude-build", state="queued", created_at=NOW - 120)
        add_job(
            connection,
            "r-codex-review",
            action="review",
            recipient="codex",
            state="running",
            last_activity_at=NOW - 45,
            ticket_id="13",
        )
        add_job(
            connection,
            "r-cursor-fix-overdue",
            action="fix",
            recipient="cursor",
            state="running",
            last_activity_at=NOW - 7200,
            timeout_seconds=5400,
            ticket_id="14",
        )
        add_job(
            connection,
            "d-claude-build-1",
            state="done",
            handoff_comment_id="9001",
        )
        add_job(
            connection,
            "d-codex-review-1",
            action="review",
            recipient="codex",
            state="done",
            handoff_comment_id="9002",
        )
        add_job(connection, "f-claude-build", state="failed")
        add_job(connection, "g-grok-blocked", action="blocked", recipient="grok", state="done")
        add_job(connection, "g-grok-clarify", action="clarify", recipient="grok", state="queued")
        add_ticket(connection, "maxharris1/cleffy", "12", state="building")
        add_ticket(connection, "maxharris1/cleffy", "13", state="review", review_round=2)
        add_ticket(
            connection,
            "aut0-code/aut0",
            "AUT-7",
            tracker="linear",
            state="stuck",
            stuck=1,
            stuck_reason="job-timeout",
            gate_count=3,
        )
        add_ticket(
            connection,
            "maxharris1/tracy-harris-coaching",
            "5",
            state="waiting-max",
            paused_action="clarify",
        )
        add_poll_error(connection, "poll:PollCycleError", repo="poll")
        add_poll_error(connection, "old:TimeoutExpired", recorded_at=NOW - 7200)
    log = tmp_path / "poller.log"
    log.write_text(
        "".join(f"cycle line {index}\n" for index in range(80)),
        encoding="utf-8",
    )
    os.utime(log, (NOW - 10, NOW - 10))
    return db, log


def snapshot_for(tmp_path: Path, **kwargs) -> dict:
    db, log = seeded(tmp_path)
    return collect_snapshot(db, log, now=NOW, **kwargs)


# --- read-only open -------------------------------------------------------


def test_open_readonly_refuses_writes(tmp_path: Path) -> None:
    db, _ = seeded(tmp_path)
    connection = open_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            connection.execute("DELETE FROM jobs")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE intruder (x)")
    finally:
        connection.close()
    with sqlite3.connect(db) as check:
        assert check.execute("SELECT count(*) FROM jobs").fetchone()[0] == 8


def test_open_readonly_uses_sqlite_ro_uri(tmp_path: Path, monkeypatch) -> None:
    db, _ = seeded(tmp_path)
    seen: list[tuple[str, dict]] = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        seen.append((str(target), kwargs))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(dashboard.sqlite3, "connect", spy)
    open_readonly(db).close()
    assert len(seen) == 1
    target, kwargs = seen[0]
    assert target.startswith("file:") and "mode=ro" in target
    assert kwargs.get("uri") is True


def test_missing_database_is_refused_and_not_created(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    with pytest.raises(DashboardError, match="does not exist"):
        open_readonly(db)
    assert not db.exists()


def test_snapshot_does_not_modify_the_database(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    before = db.read_bytes()
    collect_snapshot(db, log, now=NOW)
    assert db.read_bytes() == before


# --- JSON shape and counters ----------------------------------------------


def test_snapshot_shape(tmp_path: Path) -> None:
    snapshot = snapshot_for(tmp_path)
    assert snapshot["schema"] == "factory-dashboard/v1"
    assert snapshot["generated_at"] == "2027-01-15T08:00:00Z"
    assert snapshot["refresh_seconds"] == REFRESH_DEFAULT
    assert set(snapshot) == {
        "schema",
        "generated_at",
        "generated_at_epoch",
        "refresh_seconds",
        "queue",
        "running",
        "queued",
        "slots",
        "stuck",
        "paused",
        "tickets",
        "poller",
        "handoff_savings",
    }
    json.dumps(snapshot)


def test_queue_counts_by_recipient_and_action(tmp_path: Path) -> None:
    queue = snapshot_for(tmp_path)["queue"]
    assert queue["totals"] == {"queued": 2, "running": 2, "done": 3, "failed": 1}
    rows = {(row["recipient"], row["action"]): row for row in queue["by_recipient_action"]}
    assert rows[("claude", "build")] == {
        "recipient": "claude",
        "action": "build",
        "queued": 1,
        "running": 0,
        "done": 1,
        "failed": 1,
    }
    assert rows[("codex", "review")]["running"] == 1
    assert rows[("codex", "review")]["done"] == 1
    assert rows[("grok", "clarify")]["queued"] == 1
    assert rows[("grok", "blocked")]["done"] == 1


def test_running_jobs_show_heartbeat_age_and_overdue(tmp_path: Path) -> None:
    snapshot = snapshot_for(tmp_path)
    running = {row["ticket_id"]: row for row in snapshot["running"]}
    assert running["13"]["heartbeat_age_seconds"] == 45
    assert running["13"]["overdue"] is False
    assert running["14"]["heartbeat_age_seconds"] == 7200
    assert running["14"]["overdue"] is True
    assert running["13"]["job"] == "r-codex-"
    assert [row["ticket_id"] for row in snapshot["stuck"]["overdue_jobs"]] == ["14"]
    queued = snapshot["queued"]
    assert [(row["recipient"], row["age_seconds"]) for row in queued] == [
        ("grok", 600),
        ("claude", 120),
    ]


def test_tickets_states_rounds_paused_and_stuck(tmp_path: Path) -> None:
    snapshot = snapshot_for(tmp_path)
    tickets = {(row["repo"], row["ticket_id"]): row for row in snapshot["tickets"]}
    assert tickets[("maxharris1/cleffy", "13")]["state"] == "review"
    assert tickets[("maxharris1/cleffy", "13")]["round"] == 2
    assert tickets[("aut0-code/aut0", "AUT-7")]["tracker"] == "linear"
    assert tickets[("aut0-code/aut0", "AUT-7")]["gate_count"] == 3
    assert snapshot["stuck"]["tickets"] == [
        {
            "repo": "aut0-code/aut0",
            "ticket_id": "AUT-7",
            "state": "stuck",
            "reason": "job-timeout",
        }
    ]
    paused = {(row["repo"], row["ticket_id"]): row for row in snapshot["paused"]}
    assert paused[("maxharris1/tracy-harris-coaching", "5")]["paused_action"] == "clarify"
    assert ("aut0-code/aut0", "AUT-7") in paused


def test_poller_section_reports_log_and_recent_errors(tmp_path: Path) -> None:
    poller = snapshot_for(tmp_path)["poller"]
    assert poller["log"]["present"] is True
    assert poller["log"]["age_seconds"] == 10
    assert len(poller["log"]["tail"]) == dashboard.LOG_TAIL_LINES
    assert poller["log"]["tail"][-1] == "cycle line 79"
    assert poller["errors"]["last_hour"] == 1
    assert poller["errors"]["last_day"] == 2
    assert poller["errors"]["recent"][0] == {
        "repo": "poll",
        "ticket_id": "-",
        "error": "poll:PollCycleError",
        "age_seconds": 30,
    }
    assert poller["last_activity_age_seconds"] == 10
    assert poller["deferrals"] is None


def test_poller_reads_deferrals_table_when_present(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE poll_deferrals (id INTEGER PRIMARY KEY, detail TEXT, recorded_at REAL)"
        )
        connection.execute(
            "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
            ("Linear poll budget deferred discovery", NOW - 5),
        )
    poller = collect_snapshot(db, log, now=NOW)["poller"]
    assert poller["deferrals"]["recent"] == [
        {"detail": "Linear poll budget deferred discovery", "age_seconds": 5}
    ]
    assert poller["last_activity_age_seconds"] == 5


def test_missing_log_degrades(tmp_path: Path) -> None:
    db, _ = seeded(tmp_path)
    poller = collect_snapshot(db, tmp_path / "nope.log", now=NOW)["poller"]
    assert poller["log"] == {"present": False, "age_seconds": None, "tail": []}
    assert poller["last_activity_age_seconds"] == 30


def test_handoff_savings_counter(tmp_path: Path) -> None:
    savings = snapshot_for(tmp_path)["handoff_savings"]
    assert savings["scripted"] == 2
    assert savings["scripted_in_flight"] == 2
    assert savings["bot_to_bot"] == 2
    assert savings["scripted_share"] == 0.5
    assert "orch wait" in savings["definition"]


def test_handoff_savings_with_no_jobs(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    make_db(db)
    savings = collect_snapshot(db, tmp_path / "none.log", now=NOW)["handoff_savings"]
    assert savings["scripted"] == 0
    assert savings["bot_to_bot"] == 0
    assert savings["scripted_share"] is None


# --- slots and machines.yml -------------------------------------------------


def test_slots_without_machines_yml(tmp_path: Path) -> None:
    slots = snapshot_for(tmp_path, machines_path=tmp_path / "machines.yml")["slots"]
    assert slots == {
        "machines_yml": "absent",
        "box": {"running": 2, "slot_max": None},
        "devices": [],
    }


def test_slots_with_machines_yml_and_device_slots(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE device_slots (job_id TEXT PRIMARY KEY, device TEXT, "
            "harness TEXT, os_user TEXT, placed_at REAL)"
        )
        connection.execute(
            "INSERT INTO device_slots VALUES ('r-codex-review', 'box', 'codex', 'codex-bot', ?)",
            (NOW,),
        )
    machines = tmp_path / "machines.yml"
    machines.write_text(
        """
devices:
  - name: box
    local: true
    slot_max: 3
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot}
  - name: pc
    enabled: false
    tailscale_name: pc-wsl-bots.tail68624b.ts.net
    slot_max: 3
    harnesses: {claude: claude-bot}
    workdir_root: REPLACE_WITH_PC_WORKDIR_ROOT
""",
        encoding="utf-8",
    )
    slots = collect_snapshot(db, log, now=NOW, machines_path=machines)["slots"]
    assert slots["machines_yml"] == "present"
    assert slots["box"] == {"running": 2, "slot_max": 3}
    assert slots["devices"] == [
        {
            "name": "box",
            "slot_max": 3,
            "held": 1,
            "enabled": True,
            "placeholder": False,
            "harnesses": ["claude", "codex", "cursor"],
        },
        {
            "name": "pc",
            "slot_max": 3,
            "held": 0,
            "enabled": False,
            "placeholder": True,
            "harnesses": ["claude"],
        },
    ]
    assert "tail68624b" not in json.dumps(slots)


def test_invalid_machines_yml_degrades(tmp_path: Path) -> None:
    machines = tmp_path / "machines.yml"
    machines.write_text("devices: [unclosed\n", encoding="utf-8")
    slots = snapshot_for(tmp_path, machines_path=machines)["slots"]
    assert slots["machines_yml"] == "invalid"
    assert slots["devices"] == []


# --- refresh ----------------------------------------------------------------


@pytest.mark.parametrize("seconds", [15, 20, 30])
def test_refresh_within_range(tmp_path: Path, seconds: int) -> None:
    snapshot = snapshot_for(tmp_path, refresh_seconds=seconds)
    assert snapshot["refresh_seconds"] == seconds
    assert f'<meta http-equiv="refresh" content="{seconds}">' in render_html(snapshot)


@pytest.mark.parametrize("seconds", [0, 14, 31, 60])
def test_refresh_out_of_range_refused(tmp_path: Path, seconds: int) -> None:
    with pytest.raises(DashboardError, match="15 to 30"):
        snapshot_for(tmp_path, refresh_seconds=seconds)


# --- log tail ---------------------------------------------------------------


def test_log_tail_reads_only_the_end(tmp_path: Path) -> None:
    log = tmp_path / "poller.log"
    log.write_bytes(b"x" * 1_000_000 + b"\nlast one\n")
    assert read_log_tail(log, lines=5) == ["last one"]


def test_log_tail_strips_control_characters(tmp_path: Path) -> None:
    log = tmp_path / "poller.log"
    log.write_bytes(b"\x1b[31mred\x1b[0m \x00ok\tdone\n")
    assert read_log_tail(log, lines=5) == ["[31mred[0m ok done"]


def test_log_tail_omits_oversized_lines_instead_of_cutting_them(tmp_path: Path) -> None:
    log = tmp_path / "poller.log"
    log.write_bytes(b"z" * (dashboard.RAW_LINE_LIMIT + 1) + b"\nshort\n")
    assert read_log_tail(log, lines=5) == ["[long line omitted]", "short"]


def test_snapshot_caps_lines_after_redaction(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    token = "gh" + "p_" + "A1b2C3d4E5" * 4
    log.write_text("y" * (dashboard.LINE_LIMIT - 10) + " " + token + "\n", encoding="utf-8")
    (line,) = collect_snapshot(db, log, now=NOW)["poller"]["log"]["tail"]
    assert len(line) <= dashboard.LINE_LIMIT
    assert token[:8] not in line


# --- redaction ----------------------------------------------------------------


@pytest.mark.parametrize("name,secret", sorted(FREE_TEXT_SECRETS.items()))
def test_redact_removes_each_secret_shape(name: str, secret: str) -> None:
    samples = [
        f"error posting to {WEBHOOK_URL}",
        f"fetch {CRED_URL} failed",
        f"Authorization: Bearer {BEARER_TOKEN}",
        f"gh failed token={GH_CLASSIC}",
        f"oauth {GH_OAUTH} server {GH_SERVER}",
        f"pat {GH_PAT}",
        f"linear {LINEAR_KEY}",
        f"mail from {EMAIL}",
        f"FACTORY_WEBHOOK_KEY={KEY_VALUE}",
    ]
    for sample in samples:
        assert secret not in redact(sample), name


@pytest.mark.parametrize(
    "text",
    [
        "poll:PollCycleError maxharris1/cleffy#12 build -> claude 45s",
        "maxharris1/tracy-harris-coaching#5 waiting-max",
        "Linear poll budget deferred discovery",
        "basic checks passed; token budget ok",
    ],
)
def test_redact_keeps_ordinary_text(text: str) -> None:
    assert redact(text) == text


def test_redact_names_but_never_values_of_env_style_assignments() -> None:
    assert redact("LINEAR_API_KEY=abc123def456") == "LINEAR_API_KEY=[redacted]"
    assert redact("password: hunter2hunter2") == "password: [redacted]"


# --- the leak test ----------------------------------------------------------


def _plant_secrets(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    for name, value in {
        "FACTORY_WEBHOOK_URL": WEBHOOK_URL,
        "FACTORY_WEBHOOK_KEY": ENV_ONLY,
        "FACTORY_WEBHOOK_AUTH": f"Bearer {ENV_ONLY}",
        "LINEAR_API_KEY": ENV_ONLY,
        "GH_TOKEN": ENV_ONLY,
    }.items():
        monkeypatch.setenv(name, value)
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_poll_error(connection, f"grok-delivery:HTTPError posting {WEBHOOK_URL}")
        add_poll_error(connection, f"grok-delivery:URLError {CRED_URL}")
        add_poll_error(connection, f"gh: Authorization: Bearer {BEARER_TOKEN}")
        add_poll_error(connection, f"gh token {GH_CLASSIC} rejected", ticket_id="12")
        add_poll_error(connection, f"linear 401 for {LINEAR_KEY}")
        add_ticket(
            connection,
            "maxharris1/cleffy",
            "99",
            state="stuck",
            stuck=1,
            stuck_reason=f"stuck: {GH_OAUTH} contact {EMAIL}",
        )
        add_job(
            connection,
            "leaky-detail",
            state="running",
            detail=f"CI log with {DETAIL_ONLY} and {ENV_ONLY}",
            ticket_id="99",
        )
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"orch poll: delivery to {WEBHOOK_URL} failed\n")
        handle.write(f"curl -H 'Authorization: Bearer {BEARER_TOKEN}'\n")
        handle.write(f"gh auth: {GH_SERVER} {GH_PAT}\n")
        handle.write(f"FACTORY_WEBHOOK_KEY={KEY_VALUE}\n")
        handle.write(f"linear key {LINEAR_KEY} from {EMAIL}\n")
        handle.write(f"retrying {CRED_URL}\n")
    os.utime(log, (NOW - 10, NOW - 10))
    return db, log


def _written(tmp_path: Path, db: Path, log: Path) -> str:
    out = tmp_path / "out"
    snapshot = collect_snapshot(db, log, now=NOW)
    write_snapshot(out, snapshot)
    return (out / "status.json").read_text(encoding="utf-8") + (
        out / "index.html"
    ).read_text(encoding="utf-8")


def _leaks(text: str, secrets: dict[str, str]) -> set[str]:
    return {name for name, secret in secrets.items() if secret in text}


def test_no_planted_secret_reaches_status_json_or_index_html(
    tmp_path: Path, monkeypatch
) -> None:
    db, log = _plant_secrets(tmp_path, monkeypatch)
    text = _written(tmp_path, db, log)
    assert _leaks(text, {**FREE_TEXT_SECRETS, **OFF_ALLOWLIST_SECRETS}) == set()
    assert "[redacted" in text


def test_leak_check_goes_red_when_redaction_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    db, log = _plant_secrets(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "redact", lambda text: text)
    text = _written(tmp_path, db, log)
    # With the redactor off, every free-text secret leaks; the allowlist
    # still keeps env values and jobs.detail out.
    assert _leaks(text, FREE_TEXT_SECRETS) == set(FREE_TEXT_SECRETS)
    assert _leaks(text, OFF_ALLOWLIST_SECRETS) == set()


def test_html_escapes_markup_from_the_database(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_poll_error(connection, "<script>alert(1)</script>")
    html = render_html(collect_snapshot(db, log, now=NOW))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_unknown_enum_values_are_not_echoed(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(connection, "weird", action="exfil-action", recipient="mallory", state="queued")
    snapshot = collect_snapshot(db, log, now=NOW)
    text = json.dumps(snapshot)
    assert "exfil-action" not in text and "mallory" not in text
    assert any(row["action"] == "other" for row in snapshot["queued"])


# --- atomic write -----------------------------------------------------------


def test_write_snapshot_is_atomic_and_leaves_no_temp_files(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = snapshot_for(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "status.json").write_text("old", encoding="utf-8")
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(source, target):
        replaced.append((Path(source).name, Path(target).name))
        assert Path(source).parent == Path(target).parent
        return real_replace(source, target)

    monkeypatch.setattr(dashboard.os, "replace", spy)
    paths = write_snapshot(out, snapshot)
    assert [target for _, target in replaced] == ["status.json", "index.html"]
    assert all(source.startswith(".") for source, _ in replaced)
    assert paths == (out / "status.json", out / "index.html")
    assert sorted(path.name for path in out.iterdir()) == ["index.html", "status.json"]
    assert json.loads((out / "status.json").read_text(encoding="utf-8")) == snapshot


def test_failed_write_keeps_the_previous_file(tmp_path: Path, monkeypatch) -> None:
    snapshot = snapshot_for(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "status.json").write_text("previous", encoding="utf-8")

    def boom(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(dashboard.os, "replace", boom)
    with pytest.raises(OSError):
        write_snapshot(out, snapshot)
    assert (out / "status.json").read_text(encoding="utf-8") == "previous"
    assert sorted(path.name for path in out.iterdir()) == ["status.json"]


def test_html_is_self_contained(tmp_path: Path) -> None:
    html = render_html(snapshot_for(tmp_path))
    assert html.startswith("<!doctype html>")
    for needle in ("<link", "src=", "http://", "https://", "fetch("):
        assert needle not in html


# --- CLI --------------------------------------------------------------------


def run_orch(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {
        key: value for key, value in os.environ.items() if not key.startswith("ORCH_")
    }
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), *args],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def test_cli_snapshot_writes_both_files(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    out = tmp_path / "site"
    result = run_orch(
        "dashboard", "snapshot", "--db", str(db), "--log", str(log), "--out", str(out)
    )
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in out.iterdir()) == ["index.html", "status.json"]
    status = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert status["queue"]["totals"]["queued"] == 2
    assert result.stdout.strip() == f"wrote {out / 'status.json'} {out / 'index.html'}"


def test_cli_snapshot_defaults_from_orch_db_env(tmp_path: Path) -> None:
    db, _ = seeded(tmp_path)
    result = run_orch("dashboard", "snapshot", env={"ORCH_DB": str(db)})
    assert result.returncode == 0, result.stderr
    out = tmp_path / "dashboard"
    status = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert status["poller"]["log"]["present"] is True


def test_cli_snapshot_refuses_missing_db(tmp_path: Path) -> None:
    result = run_orch("dashboard", "snapshot", "--db", str(tmp_path / "missing.db"))
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert not (tmp_path / "missing.db").exists()


def test_cli_snapshot_requires_a_db(tmp_path: Path) -> None:
    result = run_orch("dashboard", "snapshot", "--out", str(tmp_path / "o"))
    assert result.returncode == 2
    assert "ORCH_DB" in result.stderr


def test_cli_snapshot_refuses_bad_refresh(tmp_path: Path) -> None:
    db, _ = seeded(tmp_path)
    result = run_orch("dashboard", "snapshot", "--db", str(db), "--refresh", "5")
    assert result.returncode == 2
    assert "15 to 30" in result.stderr


def test_cli_has_no_live_server(tmp_path: Path) -> None:
    result = run_orch("dashboard", "--port", "8080")
    assert result.returncode == 2
    help_text = run_orch("--help").stdout
    assert "dashboard snapshot" in help_text
    assert "--port" not in help_text
