import gc
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import lib.dashboard as dashboard
from lib import feeder
from lib.branch_stack import stack_wait
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
from lib.dashboard_pages import tickets as tickets_page
from lib.dashboard_pages.ticket_detail import ticket_view
from lib.dispatcher import Dispatcher
from lib.feeder_gates import GateLines, Holds, PathPolicy, Progress, Ref, RefStatus, blocker_waits
from lib.queue import SQLiteJobQueue

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
TRACKED_AT = NOW - 3600

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
# Review r1 (F1.1) shapes: quoted JSON keys, percent- and JSON-escaped URLs
# and credentials, and private-key blocks. Each value is short or digit-poor
# enough that only the matching rule, not the opaque-string rule, removes it.
JSON_PASSWORD = "hunter" + "2"
JSON_TOKEN = "Tk7" + "-short-v4l"
JSON_API_KEY = "ak" + "-tiny-9q"
JSON_SECRET = "sec" + "-Esc4ped"
JSON_AUTH = "xyzzy" + "-Auth-77"
PCT_WEBHOOK_SECRET = "Pq7Lm2Xc" * 3
PCT_WEBHOOK_URL = (
    "https%3A%2F%2Fhooks.slack.com%2Fservices%2FT0%2FB0%2F" + PCT_WEBHOOK_SECRET
)
PCT2_WEBHOOK_SECRET = "Wd4Rf6Ty" * 3
PCT2_WEBHOOK_URL = (
    "https%253A%252F%252Fhooks.example.invalid%252Fhook%252F" + PCT2_WEBHOOK_SECRET
)
ESC_WEBHOOK_SECRET = "Gh5Jk8Ls" * 3
ESC_WEBHOOK_URL = "https:\\/\\/hooks.example.invalid\\/services\\/" + ESC_WEBHOOK_SECRET
PCT_PASSWORD = "pct" + "-Pw-41"
PCT_USERINFO_PASSWORD = "pct" + "-Ui-73"
KEY_BEGIN = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
KEY_END = "-----END " + "OPENSSH PRIVATE KEY-----"
KEY_BODY = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2"
KEY_LAST = "d3h5" + "eg=="
KEY_TAIL_ONLY = "c2Vj" + "cmV0"
WORKFLOW_STATE_SECRET = "ws" + "-Leak-55"
FEEDER_ITEM_TITLE_TOKEN = "gh" + "p_" + "F33d3rT1tl" * 4
TICKET_ROW_TITLE_KEY = "lin" + "_api_" + "T1ck3tR0wQ" * 4

JSON_SAMPLES = {
    "json password": (JSON_PASSWORD, '{"password":"' + JSON_PASSWORD + '"}'),
    "json token": (JSON_TOKEN, '{ "token" : "' + JSON_TOKEN + '", "n": 1 }'),
    "json api_key": (JSON_API_KEY, "{'api_key': '" + JSON_API_KEY + "'}"),
    "json secret": (JSON_SECRET, '{\\"client_secret\\":\\"' + JSON_SECRET + '\\"}'),
    "json authorization": (JSON_AUTH, '{"Authorization":"' + JSON_AUTH + '"}'),
}
ENCODED_SAMPLES = {
    "percent webhook": (PCT_WEBHOOK_SECRET, "post to " + PCT_WEBHOOK_URL + " failed"),
    "double percent webhook": (PCT2_WEBHOOK_SECRET, "url=" + PCT2_WEBHOOK_URL),
    "json-escaped webhook": (ESC_WEBHOOK_SECRET, '{"url":"' + ESC_WEBHOOK_URL + '"}'),
    "percent password": (PCT_PASSWORD, "body password%3D" + PCT_PASSWORD + "%26x%3D1"),
    "percent userinfo": (
        PCT_USERINFO_PASSWORD,
        "proxy grok%3A" + PCT_USERINFO_PASSWORD + "%40proxy.example.invalid refused",
    ),
}
KEY_BLOCK_LINES = [f"loaded {KEY_BEGIN}", KEY_BODY, KEY_LAST, f"{KEY_END} done"]
# Review r2 (F2.1): an encoded separator (space, semicolon, quote) inside a
# secret value. Decoding first used to cut the value at the decoded
# separator, so the tail after it leaked. Each tail is short and digit-poor,
# so no other rule catches it. Every row leaks at 4c8910e.
TINY = "Tiny" + "Secret7"
SPLIT_SAMPLES = {
    "percent space in a bare value": (TINY + "a", "password=abc%20" + TINY + "a"),
    "percent semicolon in a bare value": (TINY + "b", "token=abc%3B" + TINY + "b"),
    "percent quote in a quoted value": (TINY + "c", 'password="prefix%22' + TINY + 'c"'),
    "escaped quote in a JSON value": (TINY + "d", '{"password":"ab\\"' + TINY + 'd"}'),
    "percent space in a URL query": (TINY + "e", "https://h/x?password=a%20" + TINY + "e"),
    "encoded key, percent space": (TINY + "f", "password%3Dabc%20" + TINY + "f"),
    "double-encoded space": (TINY + "h", "password=abc%2520" + TINY + "h"),
    "escaped JSON inside JSON, escaped quote": (
        TINY + "k",
        '{\\"password\\":\\"ab\\\\\\"' + TINY + 'k\\"}',
    ),
}
# Preservation guards: the same class, but already fully hidden at 4c8910e
# (the decoded value is a whole quoted string, or a header to end of line).
SPLIT_GUARDS = {
    "encoded key, quoted value with a space": (
        TINY + "g",
        "password%3D%22abc " + TINY + "g%22",
    ),
    "encoded auth header": (TINY + "j", "Authorization%3A%20Bearer%20abc%20" + TINY + "j"),
}
# Review r2 (F2.2): a key block whose markers are percent- or
# JSON-escaped, logged across lines, with a short body line.
PCT_KEY_BEGIN = "-----BEGIN%20" + "PRIVATE%20KEY-----"
PCT_KEY_END = "-----END%20" + "PRIVATE%20KEY-----"
ESC_KEY_BEGIN = "-----BEGIN\\u0020" + "PRIVATE\\u0020KEY-----"
ESC_KEY_END = "-----END\\u0020" + "PRIVATE\\u0020KEY-----"
PCT_KEY_BODY = "ZW5j" + "b2Rl"

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
    "feeder item title token": FEEDER_ITEM_TITLE_TOKEN,
    "ticket row title key": TICKET_ROW_TITLE_KEY,
    **{name: secret for name, (secret, _) in JSON_SAMPLES.items()},
    **{name: secret for name, (secret, _) in ENCODED_SAMPLES.items()},
    "private key body": KEY_BODY,
    "private key last line": KEY_LAST,
    **{name: secret for name, (secret, _) in SPLIT_SAMPLES.items()},
    **{name: secret for name, (secret, _) in SPLIT_GUARDS.items()},
    "encoded private key body": PCT_KEY_BODY,
}
# Never selected (env), never on the allowlist (jobs.detail), or an
# enumerated field whose unknown values show as "other" (workflow_state).
OFF_ALLOWLIST_SECRETS = {
    "env value": ENV_ONLY,
    "job detail": DETAIL_ONLY,
    "workflow_state value": WORKFLOW_STATE_SECRET,
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
    last_activity_at: float | None = None,
    timeout_seconds: int = 5400,
    handoff_comment_id: str | None = None,
    detail: str | None = None,
) -> None:
    if last_activity_at is None:
        # As lib/queue.py enqueue writes it: a queued job's queue-wait clock
        # starts when it is queued; anything else last moved a minute ago.
        last_activity_at = created_at if state == "queued" else NOW - 60
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
        (repo, ticket_id, tracker, TRACKED_AT),
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


def add_feeder_item(
    connection: sqlite3.Connection,
    repo: str,
    issue: str,
    title: str,
    *,
    seen_at: float = NOW - 60,
    state: str = "eligible",
    reason: str | None = None,
) -> None:
    feeder.ensure_tables(connection)
    connection.execute(
        f"INSERT OR REPLACE INTO {feeder.POLL_TABLE} (id, at) VALUES (1, ?)", (NOW - 60,)
    )
    connection.execute(
        f"""
        INSERT INTO {feeder.FEEDER_TABLE} (
            repo, issue, tracker, title, harness, state, waiting_reason, risk,
            issue_created_at, first_seen_at, seen_at
        ) VALUES (?, ?, 'github', ?, 'claude', ?, ?, 'low', ?, ?, ?)
        """,
        (repo, issue, title, state, reason, NOW - 7200, NOW - 7200, seen_at),
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
            detail='{"round":"3/3"}',
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


RETRIES_TABLE = """
CREATE TABLE IF NOT EXISTS ticket_retries (
    repo TEXT NOT NULL, ticket_id TEXT NOT NULL, comment_id TEXT NOT NULL,
    stuck_reason TEXT, resumed_action TEXT, job_id TEXT,
    outcome TEXT NOT NULL, recorded_at REAL NOT NULL,
    PRIMARY KEY (repo, ticket_id, comment_id)
)
"""


def test_retry_outcomes_group_as_routed_or_rejected(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    outcomes = [
        "queued-retry-fix",
        "retried-no-job",
        "retried-merge-queue",
        "rejected-retry-author",
        "rejected-retry-no-reason",
        "rejected-retry-not-stuck",
        "rejected-retry-job-active",
    ]
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO handled_comments (repo, ticket_id, comment_id, outcome) "
            "VALUES ('maxharris1/cleffy', '14', ?, ?)",
            [(f"c{index}", outcome) for index, outcome in enumerate(outcomes)],
        )
    verdicts = collect_snapshot(db, log, now=NOW)["poller"]["verdicts"]
    kinds = {row["outcome"]: row["kind"] for row in verdicts}
    assert kinds == {
        "queued-retry-fix": "routed",
        "retried-no-job": "routed",
        "retried-merge-queue": "routed",
        "rejected-retry-author": "rejected",
        "rejected-retry-no-reason": "rejected",
        "rejected-retry-not-stuck": "rejected",
        "rejected-retry-job-active": "rejected",
    }


def test_active_jobs_count_their_tickets_accepted_retries(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(RETRIES_TABLE)
        connection.executemany(
            "INSERT INTO ticket_retries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("maxharris1/cleffy", "14", "c1", "job-failed-2", "fix", "j1",
                 "queued-retry-fix", NOW - 900),
                ("maxharris1/cleffy", "14", "c2", "ci-red-3", "fix", "j2",
                 "queued-retry-fix", NOW - 300),
                ("maxharris1/cleffy", "13", "c3", None, None, None,
                 "rejected-retry-not-stuck", NOW - 60),
            ],
        )
    snapshot = collect_snapshot(db, log, now=NOW)
    running = {row["ticket_id"]: row for row in snapshot["running"]}
    assert running["14"]["retries"] == 2
    assert "retries" not in running["13"]
    assert all("retries" not in row for row in snapshot["queued"])


def test_the_ticket_page_shows_its_tickets_retries(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(RETRIES_TABLE)
        connection.execute(
            "INSERT INTO ticket_retries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("maxharris1/cleffy", "14", "c1", "job-failed-2", "fix", "j1",
             "queued-retry-fix", NOW - 900),
        )
    snapshot = collect_snapshot(db, log, now=NOW)
    assert "retried 1x" in ticket_view(snapshot, "maxharris1/cleffy#14")
    running = next(row for row in snapshot["running"] if row["ticket_id"] == "14")
    running["retries"] = 999
    assert "retried 999x" in ticket_view(snapshot, "maxharris1/cleffy#14")


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


def _idle_wal_db(tmp_path: Path) -> Path:
    """A WAL database with every connection closed, so no -wal file is left."""
    db, _ = seeded(tmp_path)
    gc.collect()  # the queue and dispatcher hold connections in reference cycles
    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()
    assert not Path(f"{db}-wal").exists()
    return db


def test_open_readonly_never_uses_immutable(tmp_path: Path, monkeypatch) -> None:
    db = _idle_wal_db(tmp_path)
    seen: list[str] = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        seen.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(dashboard.sqlite3, "connect", spy)
    open_readonly(db).close()
    assert seen == [db.resolve().as_uri() + "?mode=ro"]


def test_readonly_reader_sees_commits_from_a_live_wal_writer(tmp_path: Path) -> None:
    db = _idle_wal_db(tmp_path)
    reader = open_readonly(db)
    writer = sqlite3.connect(db)
    try:
        add_poll_error(writer, "written after the reader opened")
        writer.commit()
        assert Path(f"{db}-wal").stat().st_size > 0
        (count,) = reader.execute(
            "SELECT count(*) FROM poll_errors WHERE error = 'written after the reader opened'"
        ).fetchone()
        assert count == 1
    finally:
        reader.close()
        writer.close()


def test_readonly_read_is_a_consistent_snapshot_under_a_checkpointing_writer(
    tmp_path: Path,
) -> None:
    db = _idle_wal_db(tmp_path)
    reader = open_readonly(db)
    writer = sqlite3.connect(db)
    try:
        reader.execute("BEGIN")
        (jobs,) = reader.execute("SELECT count(*) FROM jobs").fetchone()
        for index in range(200):
            add_poll_error(writer, f"concurrent {index} " + "x" * 200)
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # The reader's transaction still sees the database as it was at BEGIN.
        (errors,) = reader.execute("SELECT count(*) FROM poll_errors").fetchone()
        assert (jobs, errors) == (8, 2)
        reader.execute("COMMIT")
        (errors,) = reader.execute("SELECT count(*) FROM poll_errors").fetchone()
        assert errors == 202
    finally:
        reader.close()
        writer.close()


def test_symlinked_database_is_resolved_before_any_check(tmp_path: Path) -> None:
    real_dir = tmp_path / "state"
    real_dir.mkdir()
    db = _idle_wal_db(real_dir)
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link = link_dir / "orch.db"
    link.symlink_to(db)
    writer = sqlite3.connect(db)
    try:
        add_poll_error(writer, "live wal row")
        writer.commit()
        # The -wal sits beside the target, not beside the link.
        assert Path(f"{db}-wal").exists() and not Path(f"{link}-wal").exists()
        reader = open_readonly(link)
        try:
            (count,) = reader.execute(
                "SELECT count(*) FROM poll_errors WHERE error = 'live wal row'"
            ).fetchone()
        finally:
            reader.close()
        assert count == 1
    finally:
        writer.close()
    assert not any(path.name.startswith("orch.db-") for path in link_dir.iterdir())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_wal_database_in_a_read_only_directory_fails_clearly(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    db = _idle_wal_db(state)
    state.chmod(0o555)
    try:
        with pytest.raises(DashboardError, match="-shm") as caught:
            open_readonly(db)
        assert "immutable" in str(caught.value)
    finally:
        state.chmod(0o755)


def test_missing_database_is_refused_and_not_created(tmp_path: Path) -> None:
    db = tmp_path / "absent.db"
    with pytest.raises(DashboardError, match="does not exist"):
        open_readonly(db)
    assert not db.exists()


def test_snapshot_does_not_modify_the_database(tmp_path: Path) -> None:
    # Measure from an idle database: seeded()'s own writer connections sit in
    # reference cycles, and when the garbage collector closes the last one it
    # checkpoints the WAL into orch.db, changing its bytes with no reader
    # involved (this flaked in CI on f3ed897).
    db = _idle_wal_db(tmp_path)
    log = tmp_path / "poller.log"
    before = db.read_bytes()
    collect_snapshot(db, log, now=NOW)
    gc.collect()
    assert db.read_bytes() == before
    assert not Path(f"{db}-wal").exists() or Path(f"{db}-wal").stat().st_size == 0


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
        "settings",  # the settings audit results (#777)
        "stuck",
        "paused",
        "tickets",
        "poller",
        "handoff_savings",
        "relay",
        "ci_runs",
        "ci_pool",
        "ci_attention",
        "qa",  # QA runs, lanes and queued qa jobs (#790)
        "ci_lane",
        "attention",
        "health",  # component heartbeats (#92)
        "feeder",
        "statechart",
        "telemetry",
        "projects",  # per-project ship mode and round caps (#152)
        "rules",
        "usage",  # subscription headroom per bot account (#151)
        "accounts",
        "routing",
        "ticket_records",
    }
    assert snapshot["feeder"] is None  # no backlog feeder has run on this database
    json.dumps(snapshot)


def test_queue_counts_by_recipient_and_action(tmp_path: Path) -> None:
    queue = snapshot_for(tmp_path)["queue"]
    assert queue["totals"] == {"queued": 2, "running": 2, "done": 3, "failed": 1, "cancelled": 0}
    rows = {(row["recipient"], row["action"]): row for row in queue["by_recipient_action"]}
    assert rows[("claude", "build")] == {
        "recipient": "claude",
        "action": "build",
        "queued": 1,
        "running": 0,
        "done": 1,
        "failed": 1,
        "cancelled": 0,
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


def test_feeder_items_carry_their_title_redacted_and_cut(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_feeder_item(connection, "maxharris1/cleffy", "107", "Plain title")
        add_feeder_item(connection, "maxharris1/cleffy", "108", f"Rotate {GH_CLASSIC} out of CI")
        add_feeder_item(connection, "maxharris1/cleffy", "109", "x" * 200)
    items = collect_snapshot(db, log, now=NOW)["feeder"]["items"]
    titles = {item["issue"]: item["title"] for item in items}
    assert titles["107"] == "Plain title"
    assert titles["108"] == "Rotate [redacted-token] out of CI"
    assert titles["109"] == "x" * 119 + "…"


def test_ticket_rows_carry_their_feeder_title(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "MaxHarris1/Cleffy", "111")
        add_ticket(connection, "MaxHarris1/Cleffy", "112")
        connection.executemany(
            "INSERT INTO ticket_sources (repo, ticket_id, source, pr_number) VALUES (?, ?, ?, ?)",
            [("MaxHarris1/Cleffy", "111", "#107", "111"), ("MaxHarris1/Cleffy", "112", "#108", "112")],
        )
        add_feeder_item(connection, "maxharris1/cleffy", "107", f"Rotate {GH_CLASSIC} out of CI")
        add_feeder_item(connection, "maxharris1/cleffy", "108", "y" * 200, seen_at=NOW - 86400)
        add_feeder_item(connection, "aut0-code/aut0", "AUT-7", "Linear ticket title", seen_at=NOW - 86400)
    snapshot = collect_snapshot(db, log, now=NOW)
    titles = {(row["repo"], row["ticket_id"]): row["title"] for row in snapshot["tickets"]}
    assert titles[("MaxHarris1/Cleffy", "111")] == "Rotate [redacted-token] out of CI"
    assert titles[("MaxHarris1/Cleffy", "112")] == "y" * 119 + "…"
    assert titles[("aut0-code/aut0", "AUT-7")] == "Linear ticket title"
    assert titles[("maxharris1/cleffy", "12")] is None
    assert [item["issue"] for item in snapshot["feeder"]["items"]] == ["107"]


def _tickets(*refs: str) -> list[dict]:
    return [{"kind": "ticket", "ref": ref, "path": None} for ref in refs]


def test_blocked_by_reads_what_the_feeder_writes() -> None:
    def ref(number: int | str, repo: str = "o/r", tracker: str = "github") -> Ref:
        return Ref(tracker, repo, str(number))

    def blocked(*refs: Ref) -> GateLines:
        return GateLines(blocked_by=frozenset(refs))

    waits = blocker_waits(
        {
            ref(10): blocked(ref(2), ref(3), ref("AUT-7", tracker="linear"), ref(9, "acme/widgets")),
            ref(1): blocked(ref(2)),
            ref(2): blocked(ref(3)),
            ref(3): blocked(ref(1)),
            ref(4): blocked(ref(4)),
        },
        {
            ref(2): RefStatus(Progress.OPEN),
            ref(3): RefStatus(Progress.UNFINISHED, detail="closed as not planned"),
            ref("AUT-7", tracker="linear"): RefStatus(Progress.MISSING),
        },
    )
    holds = Holds(policies={"o/r": PathPolicy(max_holders=2)})
    holds.take(ref(2), (), GateLines(touches=frozenset({"lib/dashboard.py"})))
    holds.take(ref(3), (), GateLines(touches=frozenset({"lib/dashboard.py"})))
    file_wait = holds.collision(ref(11), GateLines(touches=frozenset({"lib/dashboard.py"})))
    conflicts = Holds()
    conflicts.take(ref(4), (), GateLines())
    conflict_wait = conflicts.collision(ref(12), GateLines(conflicts_with=frozenset({ref(4)})))
    assert file_wait is not None and conflict_wait is not None
    held = [{"kind": "file", "ref": f"o/r#{n}", "path": "lib/dashboard.py"} for n in (2, 3)]
    cases = [
        (waits[ref(10)], _tickets("o/r#2", "o/r#3", "AUT-7", "acme/widgets#9")),
        (waits[ref(1)], _tickets("o/r#2", "o/r#3")),
        (waits[ref(4)], []),
        (file_wait, held),
        (conflict_wait, _tickets("o/r#4")),
        (stack_wait("o/r", "branch factory/issue-7", [5, 6]), _tickets("o/r#5", "o/r#6")),
        (stack_wait("o/r", "head abc1234", [8]), _tickets("o/r#8")),
    ]
    for wait, expected in cases:
        assert dashboard._blocked_by(wait.reason, "o/r") == expected, wait.reason


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("waiting on #2", _tickets("o/r#2")),
        (
            "waiting on #2, #3 (closed as not planned), AUT-7 (not found), acme/widgets#9 (status unreadable)",
            _tickets("o/r#2", "o/r#3", "AUT-7", "acme/widgets#9"),
        ),
        ("waiting on #5 (branch factory/issue-7 carries its commits)", _tickets("o/r#5")),
        (
            "waiting on file lib/dashboard.py held by #2, acme/widgets#3",
            [
                {"kind": "file", "ref": "o/r#2", "path": "lib/dashboard.py"},
                {"kind": "file", "ref": "acme/widgets#3", "path": "lib/dashboard.py"},
            ],
        ),
        ("waiting on a blocker cycle: #1 -> #3 -> #2 -> #1", _tickets("o/r#3", "o/r#2")),
        ("waiting on a blocker cycle: #1 -> #1", []),
    ],
)
def test_blocked_by_reads_the_three_waiting_reasons(reason: str, expected: list[dict]) -> None:
    assert dashboard._blocked_by(reason, "o/r") == expected


@pytest.mark.parametrize(
    "reason",
    [
        None,
        12,
        b"waiting on #2",
        "",
        "main CI unreadable for main",
        "#12 was closed because its base is green",
        "waiting on #2, the release",
        "waiting on #2 and more",
        "waiting on #2\n",
        "waiting on #2 (see (#3))",
        "waiting on file lib/dashboard.py held by #2 (conflicts with it)",
        "waiting on file lib/my file.py held by #2",
        "waiting on a blocker cycle: #1 -> #2 -> #3",
    ],
)
def test_blocked_by_is_empty_for_any_other_reason(reason: object) -> None:
    assert dashboard._blocked_by(reason, "o/r") == []


def test_feeder_waiting_states_are_feeder_states() -> None:
    assert set(dashboard.FEEDER_WAITING) <= set(feeder.STATES)


def test_waiting_feeder_items_carry_blocked_by(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    many = "waiting on " + ", ".join(f"#{n} (status unreadable)" for n in range(100, 120))
    token_path = f"lib/{GH_CLASSIC}.py"
    rows = [
        ("201", "waiting-blocked", many),
        ("202", "waiting-blocked", f"waiting on file {token_path} held by #7"),
        ("203", "waiting-capacity", "waiting on a free lane"),
        ("204", "waiting-blocked", "main CI unreadable for main"),
        ("205", "fed", "waiting on #9"),
    ]
    with sqlite3.connect(db) as connection:
        for issue, state, reason in rows:
            add_feeder_item(connection, "maxharris1/cleffy", issue, "t", state=state, reason=reason)
    items = {item["issue"]: item for item in collect_snapshot(db, log, now=NOW)["feeder"]["items"]}
    assert items["201"]["reason"].endswith("…")
    assert items["201"]["blocked_by"] == _tickets(*(f"maxharris1/cleffy#{n}" for n in range(100, 120)))
    assert items["202"]["blocked_by"] == [
        {"kind": "file", "ref": "maxharris1/cleffy#7", "path": "lib/[redacted-token].py"}
    ]
    assert items["203"]["blocked_by"] == []
    assert items["204"]["blocked_by"] == []
    assert items["205"]["reason"] == "waiting on #9"
    assert "blocked_by" not in items["205"]


def test_ticket_rows_have_a_null_title_without_a_feeder_table(tmp_path: Path) -> None:
    tickets = snapshot_for(tmp_path)["tickets"]
    assert tickets
    assert all(row["title"] is None for row in tickets)


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
    # The dispatcher's schema creates poll_deferrals; empty means none yet.
    assert poller["deferrals"] == {"recent": []}


def test_poller_deferrals_absent_on_a_db_without_the_table(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE poll_deferrals")
    poller = collect_snapshot(db, log, now=NOW)["poller"]
    assert poller["deferrals"] is None
    assert poller["last_activity_age_seconds"] == 10


def test_poller_reads_deferrals_table_when_present(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
            ("Linear poll budget deferred discovery", NOW - 5),
        )
    poller = collect_snapshot(db, log, now=NOW)["poller"]
    assert poller["deferrals"]["recent"] == [
        {"detail": "Linear poll budget deferred discovery", "age_seconds": 5}
    ]
    assert poller["last_activity_age_seconds"] == 5


def test_a_ready_held_for_ci_marks_its_ticket(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO ci_waits (repo, ticket_id, comment_id, head, reason, checks, since) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("maxharris1/cleffy", "13", "c-ready", "a" * 40, "ci-pending",
                 json.dumps(["build", GH_CLASSIC]), NOW - 125),
                ("aut0-code/aut0", "AUT-7", "c-ready-2", "b" * 40, "approved-ci-pending",
                 "not json", NOW - 5),
            ],
        )
    tickets = {
        (row["repo"], row["ticket_id"]): row for row in collect_snapshot(db, log, now=NOW)["tickets"]
    }
    assert tickets[("maxharris1/cleffy", "13")]["ci_wait"] == {
        "reason": "ci-pending",
        "checks": ["build", "[redacted-token]"],
        "waiting_seconds": 125,
    }
    assert tickets[("aut0-code/aut0", "AUT-7")]["ci_wait"] == {
        "reason": "approved-ci-pending", "checks": [], "waiting_seconds": 5,
    }
    assert [key for key, row in tickets.items() if "ci_wait" in row] == [
        ("aut0-code/aut0", "AUT-7"), ("maxharris1/cleffy", "13"),
    ]


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
        "box": {"name": "box", "running": 2, "slot_max": None},
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
    ssh_alias: pc-wsl-{bot}
    factory_root: /home/{bot}/factory
""",
        encoding="utf-8",
    )
    slots = collect_snapshot(db, log, now=NOW, machines_path=machines)["slots"]
    assert slots["machines_yml"] == "present"
    # r-codex-review holds a box slot; r-cursor-fix-overdue runs with no
    # placement, so it runs on the box too. One count for the box (#90):
    # both are box jobs, and both are in its held slots.
    assert slots["box"] == {"name": "box", "running": 2, "slot_max": 3}
    assert slots["devices"] == [
        {
            "name": "box",
            "slot_max": 3,
            "held": 2,
            "enabled": True,
            "drain": False,
            "local": True,
            "placeholder": False,
            "harnesses": ["claude", "codex", "cursor"],
        },
        {
            "name": "pc",
            "slot_max": 3,
            "held": 0,
            "enabled": False,
            "drain": False,
            "local": False,
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


def _place(db: Path, job_id: str, device: str) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS device_slots (job_id TEXT PRIMARY KEY, "
            "device TEXT, harness TEXT, os_user TEXT, placed_at REAL)"
        )
        connection.execute(
            "INSERT INTO device_slots VALUES (?, ?, 'codex', 'codex-bot', ?)",
            (job_id, device, NOW),
        )


def test_box_running_counts_only_jobs_not_placed_on_a_device(tmp_path: Path) -> None:
    # Like `orch status`: a running job that holds a device-pool slot on
    # another device is counted on that device, not on the box.
    db, log = seeded(tmp_path)
    _place(db, "r-codex-review", "pc")
    slots = collect_snapshot(db, log, now=NOW)["slots"]
    assert slots["box"]["running"] == 1


def test_a_slot_left_on_a_finished_job_does_not_change_the_box_count(tmp_path: Path) -> None:
    # Preservation guard: only running jobs are counted, placed or not.
    db, log = seeded(tmp_path)
    _place(db, "d-claude-build-1", "pc")
    slots = collect_snapshot(db, log, now=NOW)["slots"]
    assert slots["box"]["running"] == 2


def test_collect_snapshot_reads_every_table_in_one_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    """A writer that commits between two of the snapshot's queries is not seen.

    The writer commits right after the first query (the table list), so
    without collect_snapshot's BEGIN the job, queue, error and savings
    queries would each see the new rows.
    """
    db = _idle_wal_db(tmp_path)
    writer = sqlite3.connect(db)
    real_tables = dashboard._tables
    commits: list[str] = []

    def tables_then_a_concurrent_commit(connection: sqlite3.Connection) -> set[str]:
        names = real_tables(connection)
        add_job(writer, "late-running", state="running", recipient="claude", ticket_id="77")
        add_poll_error(writer, "late error", recorded_at=NOW - 1)
        writer.commit()
        commits.append("late")
        return names

    monkeypatch.setattr(dashboard, "_tables", tables_then_a_concurrent_commit)
    try:
        snapshot = collect_snapshot(db, tmp_path / "poller.log", now=NOW)
    finally:
        writer.close()
    assert commits == ["late"]
    with sqlite3.connect(db) as check:
        (running,) = check.execute(
            "SELECT count(*) FROM jobs WHERE state = 'running'"
        ).fetchone()
    assert running == 3
    assert snapshot["queue"]["totals"]["running"] == 2
    assert len(snapshot["running"]) == 2
    assert snapshot["handoff_savings"]["scripted_in_flight"] == 2
    assert snapshot["poller"]["errors"]["last_hour"] == 1
    assert "late error" not in [item["error"] for item in snapshot["poller"]["errors"]["recent"]]


TEXT_CAP = 64 * 1024
LARGE_DETAIL = json.dumps({"round": "2/3", "pad": "x" * 300_000})


class SqliteSpy:
    def __init__(self) -> None:
        self.longest_text = 0
        self.statements: list[tuple[str, frozenset[tuple[str, str]]]] = []

    def reading(self, table: str, column: str) -> list[str]:
        return [sql for sql, reads in self.statements if (table, column) in reads]


def spy_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> SqliteSpy:
    spy = SqliteSpy()
    real_connect = sqlite3.connect

    def seen(row):
        if row is not None:
            for value in row:
                if isinstance(value, str):
                    spy.longest_text = max(spy.longest_text, len(value.encode()))
        return row

    class Cursor(sqlite3.Cursor):
        def __next__(self):
            return seen(super().__next__())

        def fetchone(self):
            return seen(super().fetchone())

        def fetchmany(self, *args, **kwargs):
            return [seen(row) for row in super().fetchmany(*args, **kwargs)]

        def fetchall(self):
            return [seen(row) for row in super().fetchall()]

    class Connection(sqlite3.Connection):
        def cursor(self, factory=Cursor):
            return super().cursor(factory)

        def execute(self, *args):
            return self.cursor().execute(*args)

        def executemany(self, *args):
            return self.cursor().executemany(*args)

    def connect(*args, **kwargs):
        connection = real_connect(*args, **{**kwargs, "factory": Connection, "cached_statements": 0})
        pending: set[tuple[str, str]] = set()

        def authorize(action, table, column, _db, _source):
            if action == sqlite3.SQLITE_READ:
                pending.add((table, column))
            return sqlite3.SQLITE_OK

        def trace(sql: str) -> None:
            spy.statements.append((sql, frozenset(pending)))
            pending.clear()

        connection.set_authorizer(authorize)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return spy


def large_detail_db(tmp_path: Path) -> Path:
    db = tmp_path / "orch.db"
    make_db(db)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/factory", "1", state="merged")
        add_ticket(connection, "maxharris1/factory", "2", state="review", review_round=2)
        add_job(connection, "merged01", state="done", repo="maxharris1/factory", ticket_id="1",
                detail=LARGE_DETAIL, handoff_comment_id="h1")
        add_job(connection, "review02", action="review", recipient="codex", state="done",
                repo="maxharris1/factory", ticket_id="2", detail=LARGE_DETAIL, handoff_comment_id="h2")
        add_job(connection, "running3", action="review", recipient="codex", state="running",
                repo="maxharris1/factory", ticket_id="2", detail='{"round": "3/3"}')
    return db


def test_collect_snapshot_reads_job_rounds_inside_sqlite(tmp_path: Path, monkeypatch) -> None:
    db = large_detail_db(tmp_path)
    spy = spy_on_sqlite(monkeypatch)
    snapshot = collect_snapshot(db, tmp_path / "poller.log", now=NOW)

    assert spy.longest_text <= TEXT_CAP
    bounded, live = sorted(spy.reading("jobs", "detail"), key=lambda sql: "json_each" not in sql)
    assert "json_each(" in bounded
    assert "state IN ('queued', 'running')" in live
    rounds = {
        key: {job["job"]: job["exposure"]["round"] for visit in record["visits"] for job in visit["jobs"]}
        for key, record in snapshot["ticket_records"].items()
    }
    assert rounds == {
        "maxharris1/factory#1": {"merged01": "2"},
        "maxharris1/factory#2": {"review02": "2", "running3": "3"},
    }
    assert [(job["job"], job["round"]) for job in snapshot["running"]] == [("running3", "3/3")]


def _replaced_detail_round(detail: object) -> str | None:
    if not isinstance(detail, str):
        return None
    try:
        value = json.loads(detail)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    found = value.get("round")
    return found if isinstance(found, str) and re.fullmatch(r"[0-9]{1,3}/[0-9]{1,3}", found) else None


@pytest.mark.parametrize(
    "detail, expected",
    [
        (None, None),
        ("", None),
        ("plain text", None),
        ('["2/3"]', None),
        ('"2/3"', None),
        ('{"action": "review"}', None),
        ('{"round": "2/3"}', "2/3"),
        ('{"round": "two"}', None),
        ('{"round": 2}', None),
        ('{"round": null}', None),
        ('{"round": true}', None),
        ('{"round": ["2/3"]}', None),
        ('{"round": {"round": "2/3"}}', None),
        ('{"round": "1000/3"}', None),
        ('{"round": "2/3 "}', None),
        ('{"ROUND": "2/3"}', None),
        ('{"pr": {"round": "1/3"}, "round": "2/3"}', "2/3"),
        ('{"pr": {"round": "1/3"}}', None),
        ('{"round": "2/3"', None),
        (' \n{"round": "2/3"}\t ', "2/3"),
        (LARGE_DETAIL, "2/3"),
    ],
    ids=lambda value: value[:40] if isinstance(value, str) else repr(value),
)
def test_the_sql_round_matches_the_python_reader_it_replaces(detail: str | None, expected: str | None) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        (value,) = connection.execute(
            f"SELECT {dashboard._DETAIL_ROUND} FROM (SELECT ? AS detail)", (detail,)
        ).fetchone()
    finally:
        connection.close()
    assert dashboard._round(value) == _replaced_detail_round(detail) == expected


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


# One sample per shape, so no row passes because its secret was never in
# the input (the r2 weak-test note). The JSON, encoded, split and key-block
# shapes have their own tests below.
SHAPE_SAMPLES = {
    "webhook url": (WEBHOOK_URL, f"error posting to {WEBHOOK_URL}"),
    "webhook path secret": (WEBHOOK_SECRET, f"error posting to {WEBHOOK_URL}"),
    "url password": (CRED_PASSWORD, f"fetch {CRED_URL} failed"),
    "bearer token": (BEARER_TOKEN, f"Authorization: Bearer {BEARER_TOKEN}"),
    "bearer token, no header": (BEARER_TOKEN, f"sent Bearer {BEARER_TOKEN} upstream"),
    "gh classic token": (GH_CLASSIC, f"gh failed token={GH_CLASSIC}"),
    "gh classic token, bare": (GH_CLASSIC, f"gh rejected {GH_CLASSIC}"),
    "gh oauth token": (GH_OAUTH, f"oauth {GH_OAUTH} server {GH_SERVER}"),
    "gh server token": (GH_SERVER, f"oauth {GH_OAUTH} server {GH_SERVER}"),
    "gh fine-grained pat": (GH_PAT, f"pat {GH_PAT}"),
    "linear key": (LINEAR_KEY, f"linear {LINEAR_KEY}"),
    "email": (EMAIL, f"mail from {EMAIL}"),
    "key=value secret": (KEY_VALUE, f"FACTORY_WEBHOOK_KEY={KEY_VALUE}"),
    "feeder item title token": (FEEDER_ITEM_TITLE_TOKEN, f"Rotate {FEEDER_ITEM_TITLE_TOKEN} out of CI"),
    "ticket row title key": (TICKET_ROW_TITLE_KEY, f"Revoke {TICKET_ROW_TITLE_KEY}"),
}


def test_every_base_secret_shape_has_a_sample() -> None:
    base = {
        name
        for name in FREE_TEXT_SECRETS
        if name not in JSON_SAMPLES
        and name not in ENCODED_SAMPLES
        and name not in SPLIT_SAMPLES
        and name not in SPLIT_GUARDS
        and "private key" not in name
    }
    assert base <= set(SHAPE_SAMPLES)


@pytest.mark.parametrize("name", sorted(SHAPE_SAMPLES))
def test_redact_removes_each_secret_shape(name: str) -> None:
    secret, sample = SHAPE_SAMPLES[name]
    assert secret in sample, name
    assert secret not in redact(sample), name


@pytest.mark.parametrize(
    "text",
    [
        "poll:PollCycleError maxharris1/cleffy#12 build -> claude 45s",
        "maxharris1/tracy-harris-coaching#5 waiting-max",
        "Linear poll budget deferred discovery",
        "basic checks passed; token budget ok",
        "poll 100% done in 3s",
        '{"repo": "maxharris1/cleffy", "state": "review"}',
    ],
)
def test_redact_keeps_ordinary_text(text: str) -> None:
    assert redact(text) == text


def test_redact_names_but_never_values_of_env_style_assignments() -> None:
    assert redact("LINEAR_API_KEY=abc123def456") == "LINEAR_API_KEY=[redacted]"
    assert redact("password: hunter2hunter2") == "password: [redacted]"


@pytest.mark.parametrize("name", sorted(JSON_SAMPLES))
def test_redact_removes_quoted_json_secret_values(name: str) -> None:
    secret, sample = JSON_SAMPLES[name]
    assert secret not in redact(sample), name


def test_redact_keeps_json_key_names() -> None:
    assert redact('{"password":"' + JSON_PASSWORD + '"}') == '{"password":[redacted]}'


@pytest.mark.parametrize("name", sorted(ENCODED_SAMPLES))
def test_redact_removes_encoded_urls_and_credentials(name: str) -> None:
    secret, sample = ENCODED_SAMPLES[name]
    assert secret not in redact(sample), name


@pytest.mark.parametrize("name", sorted(SPLIT_SAMPLES))
def test_redact_never_cuts_a_secret_value_at_an_encoded_separator(name: str) -> None:
    secret, sample = SPLIT_SAMPLES[name]
    assert secret in sample, name
    text = redact(sample)
    assert TINY not in text, (name, text)
    assert "Secret7" not in text, (name, text)


@pytest.mark.parametrize("name", sorted(SPLIT_GUARDS))
def test_redact_keeps_hiding_values_split_by_a_decoded_separator(name: str) -> None:
    # Preservation guard (passes at 4c8910e too).
    secret, sample = SPLIT_GUARDS[name]
    assert secret in sample, name
    text = redact(sample)
    assert "Tiny" not in text and "Secret7" not in text, (name, text)


def test_redact_hides_the_whole_value_and_keeps_the_key_name() -> None:
    assert redact(SPLIT_SAMPLES["percent space in a bare value"][1]) == "password=[redacted]"
    assert redact(SPLIT_SAMPLES["percent semicolon in a bare value"][1]) == "token=[redacted]"
    # The decoded pass sees the value end at the unescaped quote, so it hides
    # the rest of that token too (here the closing "}): wider, never narrower.
    assert redact(SPLIT_SAMPLES["escaped quote in a JSON value"][1]) == '{"password":[redacted]'
    # The key is only visible once decoded: the encoded key stays, the
    # whole raw value after it goes.
    assert redact(SPLIT_SAMPLES["encoded key, percent space"][1]) == "password%3D[redacted]"


SPLIT_PREFIXES = [
    "password=abc",
    "token=abc",
    "api_key: abc",
    '{"password":"abc',
    "{'secret': 'abc",
    "https://h/x?password=a",
    "password%3Dabc",
    "client_secret%3D%22abc",
    '{\\"token\\":\\"abc',
]
SPLIT_SEPARATORS = [
    "%20",
    "%3B",
    "%3b",
    "%22",
    "%27",
    "%2C",
    "%7D",
    "%5D",
    "%0A",
    "%09",
    "%2520",
    "%25253B",
    "+%20",
    "\\u0020",
    "\\u003b",
    "\\u0022",
    '\\"',
    "\\'",
    "\\/%20",
]


def test_no_encoded_separator_cuts_any_secret_value_short() -> None:
    """Decoding may widen what is redacted, never narrow it (F2.1 as a class)."""
    leaks = []
    for prefix in SPLIT_PREFIXES:
        for separator in SPLIT_SEPARATORS:
            sample = f"poll failed: {prefix}{separator}{TINY} (retry 3)"
            text = redact(sample)
            if "Tiny" in text or "Secret7" in text:
                leaks.append((sample, text))
    assert leaks == []


def test_text_still_decoding_after_the_last_round_is_hidden_whole() -> None:
    # Ten levels of %25 outlast DECODE_ROUNDS; rather than show a half-decoded
    # value, the whole string is replaced.
    sample = "token=abc%" + "25" * 10 + "20" + TINY
    assert redact(sample) == dashboard.ENCODED_MARK


def test_redact_keeps_encoded_ordinary_text_as_written() -> None:
    # The decoded form is only used to find secrets; what is shown is the
    # original text with the secrets cut out.
    assert redact("fetch path%2Fto%2Ffile ok") == "fetch path%2Fto%2Ffile ok"
    assert redact("password%3Dabc%20" + TINY + " then ok") == "password%3D[redacted] then ok"


def test_redact_is_stable_when_run_twice() -> None:
    samples = [sample for _, sample in [*SPLIT_SAMPLES.values(), *SPLIT_GUARDS.values()]]
    samples += [sample for _, sample in ENCODED_SAMPLES.values()]
    samples += [sample for _, sample in JSON_SAMPLES.values()]
    for sample in samples:
        once = redact(sample)
        assert redact(once) == once, sample


def test_redact_removes_a_private_key_block_in_one_string() -> None:
    text = redact("\n".join(KEY_BLOCK_LINES))
    for part in (KEY_BODY, KEY_LAST):
        assert part not in text
    assert text.startswith("loaded [redacted-key-block]")
    assert text.endswith(" done")


def test_redact_removes_a_json_escaped_private_key_block() -> None:
    text = redact('{"key":"' + "\\n".join(KEY_BLOCK_LINES[0:1] + [KEY_LAST]) + '"}')
    assert KEY_LAST not in text


def test_key_block_spanning_log_lines_is_removed_line_by_line() -> None:
    lines = ["before", *KEY_BLOCK_LINES, "after"]
    assert dashboard.redact_key_blocks(lines) == [
        "before",
        "loaded [redacted-key-block]",
        "[redacted-key-block]",
        "[redacted-key-block]",
        "[redacted-key-block] done",
        "after",
    ]


def test_key_block_whose_begin_scrolled_out_of_the_tail_is_removed() -> None:
    lines = [KEY_LAST, KEY_TAIL_ONLY, KEY_END, "after"]
    assert dashboard.redact_key_blocks(lines) == [
        "[redacted-key-block]",
        "[redacted-key-block]",
        "[redacted-key-block]",
        "after",
    ]


def test_unterminated_key_block_hides_the_rest_of_the_tail() -> None:
    lines = ["before", KEY_BEGIN, KEY_BODY, KEY_LAST]
    assert dashboard.redact_key_blocks(lines) == ["before"] + ["[redacted-key-block]"] * 3


def test_snapshot_log_tail_redacts_a_key_block_across_lines(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    log.write_text("\n".join([KEY_LAST, KEY_TAIL_ONLY, KEY_END, "ok"]) + "\n", encoding="utf-8")
    tail = collect_snapshot(db, log, now=NOW)["poller"]["log"]["tail"]
    assert tail == ["[redacted-key-block]"] * 3 + ["ok"]


def test_percent_encoded_key_block_spanning_log_lines_is_removed() -> None:
    lines = ["before", f"loaded {PCT_KEY_BEGIN}", PCT_KEY_BODY, f"{PCT_KEY_END} done", "after"]
    assert dashboard.redact_key_blocks(lines) == [
        "before",
        "loaded [redacted-key-block]",
        "[redacted-key-block]",
        "[redacted-key-block] done",
        "after",
    ]


def test_json_escaped_key_block_spanning_log_lines_is_removed() -> None:
    lines = [ESC_KEY_BEGIN, PCT_KEY_BODY, ESC_KEY_END, "after"]
    assert dashboard.redact_key_blocks(lines) == ["[redacted-key-block]"] * 3 + ["after"]


def test_encoded_key_block_whose_begin_scrolled_out_is_removed() -> None:
    lines = [PCT_KEY_BODY, f"{PCT_KEY_END} tail", "after"]
    assert dashboard.redact_key_blocks(lines) == [
        "[redacted-key-block]",
        "[redacted-key-block] tail",
        "after",
    ]


def test_snapshot_log_tail_redacts_an_encoded_key_block_across_lines(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    log.write_text(
        "\n".join([PCT_KEY_BEGIN, PCT_KEY_BODY, PCT_KEY_END, "ok"]) + "\n", encoding="utf-8"
    )
    tail = collect_snapshot(db, log, now=NOW)["poller"]["log"]["tail"]
    assert PCT_KEY_BODY not in json.dumps(tail)
    assert tail == ["[redacted-key-block]"] * 3 + ["ok"]


# --- the leak test ----------------------------------------------------------


def _plant_secrets(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    # Show every planted row, so none drops out of the recent-N windows.
    monkeypatch.setattr(dashboard, "RECENT_ERRORS", 200)
    monkeypatch.setattr(dashboard, "RECENT_DEFERRALS", 200)
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
        # F1.1: quoted JSON, encoded URLs and credentials, key blocks.
        samples = [
            sample
            for _, sample in [
                *JSON_SAMPLES.values(),
                *ENCODED_SAMPLES.values(),
                *SPLIT_SAMPLES.values(),
                *SPLIT_GUARDS.values(),
            ]
        ]
        for index, sample in enumerate(samples):
            add_poll_error(connection, f"linear:HTTPError body {sample}", recorded_at=NOW - index)
            connection.execute(
                "INSERT INTO poll_deferrals (detail, recorded_at) VALUES (?, ?)",
                (f"deferred: {sample}", NOW - index),
            )
        add_poll_error(connection, "ssh: " + "\n".join(KEY_BLOCK_LINES))
        add_ticket(
            connection,
            "maxharris1/cleffy",
            "98",
            state='{"password":"' + WORKFLOW_STATE_SECRET + '"}',
            stuck=1,
            stuck_reason=f"stuck: {JSON_SAMPLES['json token'][1]} {PCT_WEBHOOK_URL}",
        )
        for index, (_, sample) in enumerate([*SPLIT_SAMPLES.values(), *SPLIT_GUARDS.values()]):
            add_ticket(
                connection,
                "maxharris1/cleffy",
                str(200 + index),
                state="stuck",
                stuck=1,
                stuck_reason=f"stuck: {sample}",
            )
        add_feeder_item(connection, "maxharris1/cleffy", "4242", f"Rotate {FEEDER_ITEM_TITLE_TOKEN} out of CI")
        add_feeder_item(
            connection, "maxharris1/cleffy", "99", f"Revoke {TICKET_ROW_TITLE_KEY}", seen_at=NOW - 86400
        )
    with log.open("a", encoding="utf-8") as handle:
        for line in KEY_BLOCK_LINES:
            handle.write(line + "\n")
        for _, sample in [
            *JSON_SAMPLES.values(),
            *ENCODED_SAMPLES.values(),
            *SPLIT_SAMPLES.values(),
            *SPLIT_GUARDS.values(),
        ]:
            handle.write(f"orch poll: {sample}\n")
        for line in [PCT_KEY_BEGIN, PCT_KEY_BODY, PCT_KEY_END]:
            handle.write(line + "\n")
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
    assert "Tiny" not in text and "Secret7" not in text
    assert "[redacted" in text


def test_leak_check_goes_red_when_redaction_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    db, log = _plant_secrets(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "redact", lambda text: text)
    monkeypatch.setattr(dashboard, "redact_key_blocks", lambda lines: list(lines))
    text = _written(tmp_path, db, log)
    # With the redactor off, every free-text secret leaks; the allowlist
    # still keeps env values and jobs.detail out.
    assert _leaks(text, FREE_TEXT_SECRETS) == set(FREE_TEXT_SECRETS)
    assert _leaks(text, OFF_ALLOWLIST_SECRETS) == set()


def test_html_escapes_markup_from_the_database(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_poll_error(connection, "<script>alert(1)</script>")
    pages = dashboard.render_pages(collect_snapshot(db, log, now=NOW))
    for html in pages.values():
        assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages["health.html"]


def test_unknown_enum_values_are_not_echoed(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_job(connection, "weird", action="exfil-action", recipient="mallory", state="queued")
    snapshot = collect_snapshot(db, log, now=NOW)
    text = json.dumps(snapshot)
    assert "exfil-action" not in text and "mallory" not in text
    assert any(row["action"] == "other" for row in snapshot["queued"])


def test_unknown_workflow_state_shows_as_other(tmp_path: Path) -> None:
    db, log = seeded(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/cleffy", "77", state="exfil-state")
        add_ticket(connection, "maxharris1/cleffy", "78", state="plan-check")
    snapshot = collect_snapshot(db, log, now=NOW)
    states = {row["ticket_id"]: row["state"] for row in snapshot["tickets"]}
    assert states["77"] == "other"
    assert states["78"] == "plan-check"
    assert states["5"] == "waiting-max"
    assert "exfil-state" not in json.dumps(snapshot)


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
    paths = dashboard.write_snapshot_pages(out, snapshot)
    names = ["status.json", *dashboard.STATIC_ASSETS, "index.html", "job.html", "activity.html", "efficiency.html",
             "health.html", "evals.html", "settings.html", "ticket.html", "tickets.json", "evals.json"]
    assert [target for _, target in replaced] == names
    assert all(source.startswith(".") for source, _ in replaced)
    assert paths == tuple(out / name for name in names)
    assert sorted(path.name for path in out.iterdir()) == sorted(names)
    status = {key: value for key, value in snapshot.items() if key not in ("ticket_records", "evals")}
    assert json.loads((out / "status.json").read_text(encoding="utf-8")) == status


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


def _fail_replacing(monkeypatch, name: str) -> None:
    real_replace = os.replace

    def replace(source, target):
        if Path(target).name == name:
            raise OSError("disk full")
        return real_replace(source, target)

    monkeypatch.setattr(dashboard.os, "replace", replace)


def test_failed_second_write_keeps_the_previous_pair(tmp_path: Path, monkeypatch) -> None:
    snapshot = snapshot_for(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "status.json").write_text("old status", encoding="utf-8")
    (out / "index.html").write_text("old index", encoding="utf-8")
    _fail_replacing(monkeypatch, "index.html")
    with pytest.raises(OSError, match="disk full"):
        write_snapshot(out, snapshot)
    assert (out / "status.json").read_text(encoding="utf-8") == "old status"
    assert (out / "index.html").read_text(encoding="utf-8") == "old index"
    assert sorted(path.name for path in out.iterdir()) == ["index.html", "status.json"]


def test_failed_last_page_write_keeps_every_previous_file(tmp_path: Path, monkeypatch) -> None:
    snapshot = snapshot_for(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    old = {"status.json": "old status", "index.html": "old index", "job.html": "old job"}
    for name, text in old.items():
        (out / name).write_text(text, encoding="utf-8")
    _fail_replacing(monkeypatch, "job.html")
    with pytest.raises(OSError, match="disk full"):
        write_snapshot(out, snapshot)
    # Pages published before the failure are put back, or removed when they
    # had no previous version.
    for name, text in old.items():
        assert (out / name).read_text(encoding="utf-8") == text
    assert sorted(path.name for path in out.iterdir()) == sorted(old)


def test_failed_first_snapshot_write_leaves_no_half_pair(tmp_path: Path, monkeypatch) -> None:
    snapshot = snapshot_for(tmp_path)
    out = tmp_path / "out"
    _fail_replacing(monkeypatch, "index.html")
    with pytest.raises(OSError, match="disk full"):
        write_snapshot(out, snapshot)
    assert sorted(path.name for path in out.iterdir()) == []


def test_html_is_self_contained(tmp_path: Path) -> None:
    snapshot = snapshot_for(tmp_path)
    html = render_html(snapshot)
    script = dashboard.render_static_assets(snapshot)["app.js"]
    assert html.startswith("<!doctype html>")
    assert '<link rel="stylesheet" href="/app.css">' in html
    assert '<script src="/app.js" defer></script>' in html
    assert "<script>" not in html and "<style>" not in html
    assert "http://" not in html and "http://" not in script
    # #563: the only https:// text is a link to github.com, never an asset.
    assert set(re.findall(r"https://[^/'\"]*/", html)) <= {"https://github.com/"}
    assert set(re.findall(r"https://[^/'\"]*/", script)) <= {"https://github.com/"}
    assert re.findall(r"fetch\(([^,)]+)", script) == [
        "'evals.json'", "path", "'/api/session'", "'tickets.json'", "STATUS_URL"]
    assert all(path.startswith("/api/") for path in re.findall(r"ghPost\('([^']*)'", script))
    assert "var STATUS_URL = 'status.json';" in script


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
    names = ["status.json", *dashboard.STATIC_ASSETS, "index.html", "job.html", "activity.html", "efficiency.html",
             "health.html", "evals.html", "settings.html", "ticket.html", "tickets.json", "evals.json"]
    assert sorted(path.name for path in out.iterdir()) == sorted(names)
    status = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert status["queue"]["totals"]["queued"] == 2
    assert result.stdout.strip() == "wrote " + " ".join(str(out / name) for name in names)


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


AUT0 = "maxharris1/aut0"


def _linear_db(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "orch.db"
    make_db(db)
    log = tmp_path / "poller.log"
    log.write_text("", encoding="utf-8")
    return db, log


def _hand_off_in_linear(connection: sqlite3.Connection, ticket_id: str, linear_state: str) -> None:
    connection.execute(
        """
        UPDATE ticket_state SET external_from_state = workflow_state, external_from_stuck = stuck,
               workflow_state = 'handed-off-externally', stuck = 0, external_at = ?
        WHERE repo = ? AND ticket_id = ?
        """,
        (NOW - 7200, AUT0, ticket_id),
    )
    connection.execute(
        "DELETE FROM needs_attention WHERE repo = ? AND ticket_id = ?", (AUT0, ticket_id)
    )
    connection.execute(
        "INSERT INTO linear_states (repo, ticket_id, state_name, state_type, read_at) "
        "VALUES (?, ?, ?, 'started', ?)",
        (AUT0, ticket_id, linear_state, NOW - 60),
    )


def _attention_tickets(snapshot: dict) -> list[str]:
    return [item["ticket_id"] for item in snapshot["attention"]["items"] if item["ticket_id"]]


def test_handed_off_externally_leaves_needs_attention(tmp_path: Path) -> None:
    db, log = _linear_db(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, AUT0, "AUT-2282", tracker="linear", state="capped", review_round=6)
        add_ticket(connection, AUT0, "AUT-2283", tracker="linear", state="building",
                   stuck=1, stuck_reason="job died twice")
        connection.execute(
            "INSERT INTO needs_attention (repo, ticket_id, reason, detail, raised_at) "
            "VALUES (?, 'AUT-2282', 'ship-failed', 'round cap reached', ?)",
            (AUT0, NOW - 600),
        )
    before = collect_snapshot(db, log, now=NOW)
    assert {"AUT-2282", "AUT-2283"} <= set(_attention_tickets(before))

    with sqlite3.connect(db) as connection:
        _hand_off_in_linear(connection, "AUT-2282", "Ready for Review")
        _hand_off_in_linear(connection, "AUT-2283", "In Review")
    after = collect_snapshot(db, log, now=NOW)

    assert after["attention"]["count"] == before["attention"]["count"] - 2
    assert not {"AUT-2282", "AUT-2283"} & set(_attention_tickets(after))
    assert after["stuck"]["tickets"] == []


def test_card_says_handed_off_in_linear_with_time(tmp_path: Path) -> None:
    db, log = _linear_db(tmp_path)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, AUT0, "AUT-2282", tracker="linear", state="capped", review_round=6)
        _hand_off_in_linear(connection, "AUT-2282", "In Review")
    snapshot = collect_snapshot(db, log, now=NOW)

    [row] = snapshot["tickets"]
    assert (row["external_at"], row["linear_state"]) == (NOW - 7200, "In Review")
    found = tickets_page.board(snapshot)
    assert [tile["ticket"]["ticket_id"] for tile in found["shelf"]["tiles"]] == ["AUT-2282"]
    assert (found["shelf"]["merged"], found["shelf"]["handed"]) == (0, 1)
    assert all(not col["cards"] for col in found["columns"])
    page = dashboard.render_pages(snapshot)["job.html"]
    tile = re.search(r'<a class="tk-tile"[^>]*data-ticket="[^"]*AUT-2282"[^>]*>.*?</a>', page).group(0)
    assert '<span class="tk-ago">handed off in Linear 2h 00m ago</span>' in tile
    assert "handed off in Linear 2h 00m ago · In Review · 6 revise rounds" in tile


def test_a_database_without_close_times_ages_abandoned_from_the_transition(tmp_path: Path) -> None:
    db, log = _linear_db(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("ALTER TABLE ticket_state DROP COLUMN closed_at")
        add_ticket(connection, "maxharris1/cleffy", "98", state="abandoned")
        connection.execute(
            "INSERT INTO ticket_transitions (repo, ticket_id, from_state, to_state, at, source) "
            "VALUES ('maxharris1/cleffy', '98', 'review', 'abandoned', ?, 'closed-unmerged')", (NOW - 600,))
    [row] = collect_snapshot(db, log, now=NOW)["tickets"]
    assert (row["state"], row["state_age_seconds"]) == ("abandoned", 600)


def test_linear_settled_ticket_shows_merged_done_abandoned(tmp_path: Path) -> None:
    db, log = _linear_db(tmp_path)
    with sqlite3.connect(db) as connection:
        for ticket_id, state in (("AUT-1", "merged"), ("AUT-2", "done"), ("AUT-3", "abandoned")):
            add_ticket(connection, AUT0, ticket_id, tracker="linear", state=state, review_round=1)
    snapshot = collect_snapshot(db, log, now=NOW)

    assert {row["ticket_id"]: row["state"] for row in snapshot["tickets"]} == {
        "AUT-1": "merged", "AUT-2": "done", "AUT-3": "abandoned",
    }
    assert _attention_tickets(snapshot) == []
    found = tickets_page.board(snapshot)
    assert {tile["ticket"]["ticket_id"] for tile in found["shelf"]["tiles"]} == {"AUT-1", "AUT-2"}
    [stopped] = [col for col in found["columns"] if col["phase"] == "Stop"]
    assert [card["ticket"]["ticket_id"] for card in stopped["cards"]] == ["AUT-3"]


def _wait_rows(tmp_path: Path) -> dict[str, dict]:
    db, log = seeded(tmp_path)
    repo = "maxharris1/factory"
    with sqlite3.connect(db) as connection:
        for ticket_id, state in (
            ("1", "awaiting-ok"), ("2", "waiting-max"), ("3", "waiting-max"),
            ("4", "waiting-max"), ("5", "waiting-max"), ("6", "capped"), ("7", "blocked"),
            ("8", "waiting-external"), ("9", "building"),
        ):
            add_ticket(connection, repo, ticket_id, state=state)
        add_ticket(connection, repo, "10", state="building", stuck=1, stuck_reason="job-failed-2")
        connection.executemany(
            "UPDATE ticket_state SET paused_question_id = ?, paused_action = ? WHERE repo = ? AND ticket_id = ?",
            [(f"60{n}", action, repo, str(n))
             for n, action in ((2, "build"), (3, "fix"), (4, "review"), (5, "build"))],
        )
        connection.execute(
            "UPDATE ticket_state SET stuck_source_comment_id = '6010' "
            "WHERE repo = ? AND ticket_id = '10'",
            (repo,),
        )
        connection.execute(
            "INSERT INTO plan_events (repo, ticket_id, comment_id, outcome) VALUES (?, '2', '602', ?)",
            (repo, "waiting-max-size"),
        )
        connection.executemany(
            "INSERT INTO review_events (repo, ticket_id, comment_id, outcome) VALUES (?, ?, ?, ?)",
            [(repo, "3", "603", "waiting-max-repeat"), (repo, "4", "604", "waiting-max-unsat"),
             (repo, "5", "605", "queued-fix"), (repo, "6", "6061", "capped-review"),
             (repo, "6", "606", "capped-review"), (repo, "7", "607", "blocked-review")],
        )
        connection.execute(
            "INSERT INTO ship_holds (repo, ticket_id, head, approver, reasons, approve_comment_id, "
            "held_at) VALUES (?, '1', ?, 'maxharris1', 'label', '601', ?)",
            (repo, "a" * 40, NOW - 60),
        )
    snapshot = collect_snapshot(db, log, now=NOW)
    return {
        "tickets": {row["ticket_id"]: row for row in snapshot["tickets"] if row["repo"] == repo},
        "attention": {
            row["ticket_id"]: row for row in snapshot["attention"]["items"] if row.get("repo") == repo
        },
    }


def test_each_waiting_ticket_says_why_it_waits_and_which_actions_apply(tmp_path: Path) -> None:
    rows = _wait_rows(tmp_path)
    found = {
        ticket_id: (row["wait"] and (row["wait"]["why"], row["wait"]["comment_id"]), row["actions"])
        for ticket_id, row in rows["tickets"].items()
    }
    assert found == {
        "1": (("awaiting-ok", "601"), ["ok", "close", "hand-off"]),
        "2": (("plan-size", "602"), ["approve-plan", "split", "close", "hand-off"]),
        "3": (("repeat-stop", "603"), ["another-round", "close", "hand-off"]),
        "4": (("unsat", "604"), ["another-round", "close", "hand-off"]),
        "5": (("question", "605"), ["answer", "close", "hand-off"]),
        "6": (("capped", "606"), ["another-round", "close", "hand-off"]),
        "7": (("blocked", "607"), ["resume", "close", "hand-off"]),
        "8": (("waiting-external", None), ["close", "hand-off"]),
        "9": (None, []),
        "10": (("stuck", "6010"), ["retry", "close", "hand-off"]),
    }
    assert rows["tickets"]["10"]["wait"]["detail"] == "job-failed-2"


def test_attention_items_carry_the_ticket_wait_and_actions(tmp_path: Path) -> None:
    rows = _wait_rows(tmp_path)
    attention = rows["attention"]
    assert set(attention) == {"1", "2", "3", "4", "5", "6", "7", "8", "10"}
    for ticket_id, item in attention.items():
        ticket = rows["tickets"][ticket_id]
        assert (item["state"], item["wait"], item["actions"]) == (
            ticket["state"], ticket["wait"], ticket["actions"]
        )


def test_tickets_json_carries_a_finished_job_s_exposure(tmp_path: Path) -> None:
    from lib import exposure
    from lib.queue import JobRequest

    db = tmp_path / "orch.db"
    queue = make_db(db)
    with sqlite3.connect(db) as connection:
        add_ticket(connection, "maxharris1/factory", "1014", state="review")
    queue.enqueue(JobRequest(repo="maxharris1/factory", ticket_id="1014", action="build", recipient="claude",
                             source_comment_id="c1", timeout_seconds=600), dedupe_key="k1")
    job = queue.claim("claude")
    assert job is not None
    brief, revision = "c" * 64, "d" * 40
    with sqlite3.connect(db) as connection:
        exposure.write(connection, job.id, exposure.Exposure(
            "opus", "claude 2.1.0", brief, ("/skills/a/SKILL.md",), "pc", revision, "high", "claude"), NOW)
    queue.finish(job.id, owner="claude", handoff_comment_id="h1")

    snapshot = collect_snapshot(db, tmp_path / "missing.log", now=NOW)
    assert snapshot["running"] == []
    path = tmp_path / "out" / "tickets.json"
    dashboard.write_snapshot_pages(tmp_path / "out", snapshot)
    (record,) = json.loads(path.read_text(encoding="utf-8"))["tickets"].values()
    assert [visit["jobs"] for visit in record["visits"] if visit["state"] == "building"] == [[{
        "job": job.id[:8],
        "action": "build",
        "recipient": "claude",
        "exposure": {
            "round": "unknown",
            "model": "opus",
            "effort": "high",
            "harness": "claude",
            "cli_version": "claude 2.1.0",
            "brief_sha256": ["c" * 16] * 4,
            "skill_paths": ["/skills/a/SKILL.md"],
            "host": "pc",
            "factory_revision": ["d" * 16, "d" * 16, "d" * 8],
        },
        "suite_wait_seconds": None,
    }]]
