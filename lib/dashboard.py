"""Read-only, redacted dashboard snapshot (status.json plus index.html).

`orch dashboard snapshot` opens the dispatcher database with a SQLite
`mode=ro` URI and reads the tail of poller.log. It never writes either.
It writes two files to an output directory, each atomically (temp file in
the same directory, fsync, then rename): status.json and a static,
self-contained index.html that renders the same data and reloads itself
every refresh_seconds (15 to 30).

Secrets: only allowlisted columns are selected (never jobs.detail, heads,
comment bodies or environment values; the environment is not read except
for the ORCH_* path variables). Every free-text value (stuck reasons,
poll_errors, log lines) goes through `redact`, and the finished snapshot
is walked once more so every string in it has been through `redact`
before it is written. Enumerated fields outside their known values are
shown as "other".
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lib.dashboard_config import REFRESH_DEFAULT, REFRESH_MAX, REFRESH_MIN
from lib.load import load_yaml
from lib.queue import ACTIONS, RECIPIENTS, STATES
from lib.status import PAUSED_STATES

SCHEMA = "factory-dashboard/v1"
LOG_TAIL_LINES = 50
LOG_TAIL_BYTES = 256 * 1024
RAW_LINE_LIMIT = 4096
LINE_LIMIT = 300
RECENT_ERRORS = 20
RECENT_DEFERRALS = 10
MACHINES_MAX_BYTES = 256 * 1024
WAIT_RECIPIENTS = ("claude", "codex", "cursor")
HARNESSES = ("claude", "codex", "cursor")
TRACKERS = ("github", "linear")

HANDOFF_SAVINGS_DEFINITION = (
    "scripted = jobs for claude, codex or cursor that `orch wait` claimed and "
    "the bot finished with an orch:v1 handoff comment (jobs.state = 'done' "
    "with a handoff_comment_id). Each one is a handoff routed from an orch:v1 "
    "comment to a bot by script, with no bot messaging another bot. "
    "scripted_in_flight = such jobs claimed and still running. "
    "bot_to_bot = jobs routed to grok: each one wakes Grok Bot to read the "
    "ticket and hand off by hand (stops, clarify, refusals, ship failures). "
    "scripted_share = scripted / (scripted + bot_to_bot). The database does not "
    "record bot messages sent outside the factory, so grok wakes are the "
    "measurable stand-in for bot-to-bot handoffs."
)


class DashboardError(RuntimeError):
    pass


# --- redaction --------------------------------------------------------------

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+")
_USERINFO = re.compile(r"[^\s:/@'\"]+:[^\s/@'\"]+@[A-Za-z0-9.\-]+")
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key|cookie|set-cookie)"
    r"(\s*[:=]\s*)[^\r\n]*"
)
_SCHEME_TOKEN = re.compile(r"(?i)\b(bearer|basic|token)(\s+)([A-Za-z0-9._~+/=\-]{6,})")
_KNOWN_TOKEN = re.compile(
    r"\b(?:"
    r"gh[pousr]_[A-Za-z0-9_]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|lin_(?:api|oauth)_[A-Za-z0-9_]{16,}"
    r"|glpat-[A-Za-z0-9_\-]{16,}"
    r"|xox[abposr]-[A-Za-z0-9\-]{10,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]+)*"
    r")"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_\-]*"
    r"(?:token|secret|passw(?:or)?d|api[_\-]?key|key|auth|credential|session)"
    r"[A-Za-z0-9_\-]*)"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# 32+ token characters; _opaque keeps ones that read like names, not keys.
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_\-+/=]{32,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _scheme_token(match: re.Match[str]) -> str:
    scheme, space, value = match.groups()
    # "Bearer x" is always a credential; "token"/"basic" need a token-like value.
    if scheme.lower() != "bearer" and not (
        any(ch.isdigit() for ch in value) or "=" in value or len(value) >= 16
    ):
        return match.group(0)
    return f"{scheme}{space}[redacted]"


def _opaque(match: re.Match[str]) -> str:
    value = match.group(0)
    digits = sum(ch.isdigit() for ch in value)
    letters = sum(ch.isalpha() for ch in value)
    # A repo path like owner1/some-long-repo-name has at most one digit.
    return "[redacted]" if digits >= 2 and letters >= 2 else value


def redact(text: str) -> str:
    """Remove anything shaped like a credential, URL or email address."""
    text = _URL.sub("[redacted-url]", text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    text = _SCHEME_TOKEN.sub(_scheme_token, text)
    text = _KNOWN_TOKEN.sub("[redacted-token]", text)
    text = _USERINFO.sub("[redacted-credentials]", text)
    text = _EMAIL.sub("[redacted-email]", text)
    text = _SECRET_ASSIGNMENT.sub(
        lambda m: m.group(0)
        if m.group(3).startswith("[redacted")
        else f"{m.group(1)}{m.group(2)}[redacted]",
        text,
    )
    return _OPAQUE.sub(_opaque, text)


def _strip_control(text: str) -> str:
    return _CONTROL.sub("", text.replace("\t", " "))


def _clean(value: object, limit: int = LINE_LIMIT) -> str:
    """Free text: strip control characters, redact, then cap the length."""
    text = redact(_strip_control(str(value)))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _enum(value: object, allowed: Iterable[str]) -> str:
    return str(value) if str(value) in tuple(allowed) else "other"


def _scrub(value: object) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


# --- reading ------------------------------------------------------------------


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database through a SQLite mode=ro URI."""
    if not path.is_file():
        raise DashboardError(f"database does not exist: {path}")
    wal_path = Path(f"{path}-wal")
    # Without a -wal file no writer has it open; immutable avoids creating -shm.
    query = "?mode=ro" if wal_path.exists() else "?mode=ro&immutable=1"
    uri = path.resolve().as_uri() + query
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise DashboardError(f"cannot read database: {exc}") from exc


def read_log_tail(path: Path, *, lines: int = LOG_TAIL_LINES) -> list[str]:
    """Return the last `lines` lines of a regular file, control characters removed.

    Only the final LOG_TAIL_BYTES are read. A line longer than RAW_LINE_LIMIT
    is replaced by a marker rather than cut, so a cut can never leave half a
    secret that the redactor no longer recognizes.
    """
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - LOG_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(size - start)
    raw = data.split(b"\n")
    if start > 0 and raw:
        raw = raw[1:]
    if raw and raw[-1] == b"":
        raw = raw[:-1]
    result: list[str] = []
    for item in raw[-lines:]:
        text = item.decode("utf-8", errors="replace").rstrip("\r")
        if len(text) > RAW_LINE_LIMIT:
            result.append("[long line omitted]")
        else:
            result.append(_strip_control(text))
    return result


def _age(now: float, then: float | None) -> int | None:
    if then is None:
        return None
    return int(max(0.0, now - float(then)))


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def collect_snapshot(
    db_path: Path,
    log_path: Path,
    *,
    now: float,
    machines_path: Path | None = None,
    refresh_seconds: int = REFRESH_DEFAULT,
) -> dict:
    if (
        isinstance(refresh_seconds, bool)
        or not isinstance(refresh_seconds, int)
        or not REFRESH_MIN <= refresh_seconds <= REFRESH_MAX
    ):
        raise DashboardError("refresh_seconds must be 15 to 30")
    connection = open_readonly(db_path)
    try:
        connection.execute("BEGIN")
        db = _read_db(connection, now)
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        raise DashboardError(f"cannot read database: {exc}") from exc
    finally:
        connection.close()

    log_present = log_path.is_file()
    log_mtime = log_path.stat().st_mtime if log_present else None
    tail = [_clean(line) for line in read_log_tail(log_path)] if log_present else []
    activity = [
        value
        for value in (log_mtime, db["newest_error_at"], db["newest_deferral_at"])
        if value is not None
    ]
    machines_state, devices, box_max = _machines(machines_path, db["held"])

    snapshot = {
        "schema": SCHEMA,
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "generated_at_epoch": now,
        "refresh_seconds": refresh_seconds,
        "queue": db["queue"],
        "running": db["running"],
        "queued": db["queued"],
        "slots": {
            "machines_yml": machines_state,
            "box": {"running": db["box_running"], "slot_max": box_max},
            "devices": devices,
        },
        "stuck": {
            "tickets": db["stuck_tickets"],
            "overdue_jobs": db["overdue_jobs"],
        },
        "paused": db["paused"],
        "tickets": db["tickets"],
        "poller": {
            "log": {
                "present": log_present,
                "age_seconds": _age(now, log_mtime),
                "tail": tail,
            },
            "last_activity_age_seconds": _age(now, max(activity)) if activity else None,
            "errors": db["errors"],
            "deferrals": db["deferrals"],
        },
        "handoff_savings": db["handoff_savings"],
    }
    return _scrub(snapshot)  # type: ignore[return-value]


def _read_db(connection: sqlite3.Connection, now: float) -> dict:
    tables = _tables(connection)
    jobs = "jobs" in tables

    totals = {state: 0 for state in STATES}
    groups: dict[tuple[str, str], dict] = {}
    if jobs:
        for row in connection.execute(
            "SELECT recipient, action, state, count(*) AS n FROM jobs "
            "GROUP BY recipient, action, state"
        ):
            recipient = _enum(row["recipient"], RECIPIENTS)
            action = _enum(row["action"], ACTIONS)
            state = _enum(row["state"], STATES)
            if state == "other":
                continue
            totals[state] += int(row["n"])
            entry = groups.setdefault(
                (recipient, action),
                {"recipient": recipient, "action": action, **{s: 0 for s in STATES}},
            )
            entry[state] += int(row["n"])

    running: list[dict] = []
    queued: list[dict] = []
    overdue: list[dict] = []
    box_running = 0
    if jobs:
        for row in connection.execute(
            """
            SELECT id, repo, ticket_id, action, recipient, state,
                   timeout_seconds, created_at, last_activity_at
            FROM jobs WHERE state IN ('queued', 'running')
            ORDER BY created_at, id
            """
        ):
            base = {
                "job": _clean(str(row["id"])[:8], 8),
                "repo": _clean(row["repo"], 100),
                "ticket_id": _clean(row["ticket_id"], 64),
                "action": _enum(row["action"], ACTIONS),
                "recipient": _enum(row["recipient"], RECIPIENTS),
            }
            timeout = int(row["timeout_seconds"])
            if row["state"] == "running":
                age = _age(now, row["last_activity_at"]) or 0
                item = {
                    **base,
                    "heartbeat_age_seconds": age,
                    "timeout_seconds": timeout,
                    "overdue": age > timeout,
                }
                running.append(item)
                if row["recipient"] != "grok":
                    box_running += 1
            else:
                age = _age(now, row["created_at"]) or 0
                item = {**base, "age_seconds": age}
                queued.append(item)
            if age > timeout:
                overdue.append({**base, "state": str(row["state"]), "age_seconds": age})
        running.sort(key=lambda item: -item["heartbeat_age_seconds"])

    tickets: list[dict] = []
    stuck_tickets: list[dict] = []
    paused: list[dict] = []
    if "tracked_tickets" in tables and "ticket_state" in tables:
        for row in connection.execute(
            """
            SELECT tracked.repo, tracked.ticket_id, tracked.tracker,
                   COALESCE(state.workflow_state, 'intake') AS workflow_state,
                   COALESCE(state.review_round, 0) AS review_round,
                   COALESCE(state.gate_count, 0) AS gate_count,
                   COALESCE(state.stuck, 0) AS stuck,
                   state.stuck_reason, state.paused_action
            FROM tracked_tickets AS tracked
            LEFT JOIN ticket_state AS state
              ON state.repo = tracked.repo AND state.ticket_id = tracked.ticket_id
            """
        ):
            repo = _clean(row["repo"], 100)
            ticket_id = _clean(row["ticket_id"], 64)
            state = _clean(row["workflow_state"], 40)
            paused_action = (
                _enum(row["paused_action"], ACTIONS)
                if row["paused_action"] is not None
                else None
            )
            stuck = bool(row["stuck"])
            tickets.append(
                {
                    "repo": repo,
                    "ticket_id": ticket_id,
                    "tracker": _enum(row["tracker"], TRACKERS),
                    "state": state,
                    "round": int(row["review_round"]),
                    "gate_count": int(row["gate_count"]),
                    "stuck": stuck,
                    "paused_action": paused_action,
                }
            )
            if stuck:
                stuck_tickets.append(
                    {
                        "repo": repo,
                        "ticket_id": ticket_id,
                        "state": state,
                        "reason": (
                            _clean(row["stuck_reason"], 200)
                            if row["stuck_reason"] is not None
                            else None
                        ),
                    }
                )
            if stuck or str(row["workflow_state"]) in PAUSED_STATES:
                paused.append(
                    {
                        "repo": repo,
                        "ticket_id": ticket_id,
                        "state": state,
                        "paused_action": paused_action,
                    }
                )
    order = lambda item: (item["repo"].casefold(), item["ticket_id"])  # noqa: E731
    tickets.sort(key=order)
    stuck_tickets.sort(key=order)
    paused.sort(key=order)

    errors = {"last_hour": 0, "last_day": 0, "recent": []}
    newest_error_at = None
    if "poll_errors" in tables:
        row = connection.execute(
            """
            SELECT
              COALESCE(SUM(recorded_at >= ?), 0) AS last_hour,
              COALESCE(SUM(recorded_at >= ?), 0) AS last_day,
              MAX(recorded_at) AS newest
            FROM poll_errors
            """,
            (now - 3600, now - 86400),
        ).fetchone()
        errors["last_hour"] = int(row["last_hour"])
        errors["last_day"] = int(row["last_day"])
        newest_error_at = row["newest"]
        errors["recent"] = [
            {
                "repo": _clean(item["repo"], 100),
                "ticket_id": _clean(item["ticket_id"], 64),
                "error": _clean(item["error"]),
                "age_seconds": _age(now, item["recorded_at"]),
            }
            for item in connection.execute(
                "SELECT repo, ticket_id, error, recorded_at FROM poll_errors "
                "ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (RECENT_ERRORS,),
            )
        ]

    deferrals = None
    newest_deferral_at = None
    if "poll_deferrals" in tables:
        rows = connection.execute(
            "SELECT detail, recorded_at FROM poll_deferrals "
            "ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (RECENT_DEFERRALS,),
        ).fetchall()
        deferrals = {
            "recent": [
                {
                    "detail": _clean(item["detail"]),
                    "age_seconds": _age(now, item["recorded_at"]),
                }
                for item in rows
            ]
        }
        newest_deferral_at = rows[0]["recorded_at"] if rows else None

    held: dict[str, int] = {}
    if "device_slots" in tables:
        for row in connection.execute(
            "SELECT device, count(*) AS n FROM device_slots GROUP BY device"
        ):
            held[str(row["device"])] = int(row["n"])

    scripted = in_flight = bot_to_bot = 0
    if jobs:
        marks = ",".join("?" for _ in WAIT_RECIPIENTS)
        row = connection.execute(
            f"""
            SELECT
              COALESCE(SUM(recipient IN ({marks}) AND state = 'done'
                           AND handoff_comment_id IS NOT NULL), 0) AS scripted,
              COALESCE(SUM(recipient IN ({marks}) AND state = 'running'), 0)
                AS in_flight,
              COALESCE(SUM(recipient = 'grok'), 0) AS bot_to_bot
            FROM jobs
            """,
            (*WAIT_RECIPIENTS, *WAIT_RECIPIENTS),
        ).fetchone()
        scripted = int(row["scripted"])
        in_flight = int(row["in_flight"])
        bot_to_bot = int(row["bot_to_bot"])
    denominator = scripted + bot_to_bot

    return {
        "queue": {
            "totals": totals,
            "by_recipient_action": [groups[key] for key in sorted(groups)],
        },
        "running": running,
        "queued": queued,
        "box_running": box_running,
        "overdue_jobs": overdue,
        "tickets": tickets,
        "stuck_tickets": stuck_tickets,
        "paused": paused,
        "errors": errors,
        "newest_error_at": newest_error_at,
        "deferrals": deferrals,
        "newest_deferral_at": newest_deferral_at,
        "held": held,
        "handoff_savings": {
            "scripted": scripted,
            "scripted_in_flight": in_flight,
            "bot_to_bot": bot_to_bot,
            "scripted_share": (
                round(scripted / denominator, 3) if denominator else None
            ),
            "definition": HANDOFF_SAVINGS_DEFINITION,
        },
    }


def _has_placeholder(value: object) -> bool:
    if isinstance(value, str):
        return "REPLACE_WITH_" in value
    if isinstance(value, dict):
        return any(_has_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    return False


def _machines(
    path: Path | None,
    held: dict[str, int],
) -> tuple[str, list[dict], int | None]:
    """Optional F2-01 inventory. Absent or invalid degrades to no device rows."""
    if path is None or not path.is_file():
        return "absent", [], None
    try:
        if path.stat().st_size > MACHINES_MAX_BYTES:
            return "invalid", [], None
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return "invalid", [], None
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return "invalid", [], None
    devices: list[dict] = []
    box_max = None
    for item in data["devices"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return "invalid", [], None
        slot_max = item.get("slot_max")
        slot_max = (
            slot_max
            if isinstance(slot_max, int) and not isinstance(slot_max, bool)
            else None
        )
        harnesses = item.get("harnesses")
        names = harnesses.keys() if isinstance(harnesses, dict) else (
            harnesses if isinstance(harnesses, list) else ()
        )
        enabled = item.get("enabled", True)
        devices.append(
            {
                "name": _clean(item["name"], 40),
                "slot_max": slot_max,
                "held": held.get(item["name"], 0),
                "enabled": enabled is True,
                "placeholder": _has_placeholder(item),
                "harnesses": sorted(
                    {str(name) for name in names if str(name) in HARNESSES}
                ),
            }
        )
        if item.get("local") is True or item["name"] == "box":
            box_max = slot_max if box_max is None else box_max
    return "present", devices, box_max


# --- writing --------------------------------------------------------------------


def write_snapshot(out_dir: Path, snapshot: dict) -> tuple[Path, Path]:
    """Write status.json, then index.html, each by temp file and rename."""
    out_dir.mkdir(parents=True, exist_ok=True)
    status_text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    html_text = render_html(snapshot)
    status_path = out_dir / "status.json"
    index_path = out_dir / "index.html"
    _atomic_write(status_path, status_text)
    _atomic_write(index_path, html_text)
    return status_path, index_path


def _atomic_write(path: Path, text: str) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


# --- rendering ----------------------------------------------------------------

_CSS = """
body{font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;
color:#1f2328;background:#fff}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}
.meta{color:#59636e}.stale{color:#b42318;font-weight:600}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.tile{border:1px solid #d1d9e0;border-radius:6px;padding:8px 12px;min-width:120px}
.tile b{display:block;font-size:20px}
table{border-collapse:collapse;margin:0 0 8px}
th,td{border-bottom:1px solid #e6eaef;padding:3px 10px 3px 0;text-align:left;
vertical-align:top}th{color:#59636e;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.bad{color:#b42318;font-weight:600}.empty{color:#59636e}
pre{background:#f6f8fa;padding:8px;overflow-x:auto;font-size:12px}
@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}
.tile,th,td{border-color:#30363d}pre{background:#161b22}.meta,th,.empty{color:#9198a1}}
"""

_SCRIPT = (
    "(function(){var g=Number(document.body.getAttribute('data-generated'));"
    "var e=document.getElementById('age');function t(){var s=Math.max(0,"
    "Math.round(Date.now()/1000-g));e.textContent=s+'s ago';"
    "e.className=s>120?'stale':'';}t();setInterval(t,1000);})();"
)


def _e(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return html.escape(str(value), quote=True)


def _duration(seconds: object) -> str:
    if not isinstance(seconds, int):
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _table(headers: list[str], rows: list[list[str]], empty: str = "none") -> str:
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = "".join("<tr>" + "".join(rows_item) + "</tr>" for rows_item in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _td(value: object, *, num: bool = False, bad: bool = False) -> str:
    classes = " ".join(name for name, on in (("num", num), ("bad", bad)) if on)
    attr = f' class="{classes}"' if classes else ""
    return f"<td{attr}>{_e(value)}</td>"


def render_html(snapshot: dict) -> str:
    """A static page; every value is escaped and already redacted."""
    totals = snapshot["queue"]["totals"]
    savings = snapshot["handoff_savings"]
    poller = snapshot["poller"]
    slots = snapshot["slots"]
    stuck_count = len(snapshot["stuck"]["tickets"]) + len(snapshot["stuck"]["overdue_jobs"])
    share = savings["scripted_share"]
    tiles = [
        ("queued", totals["queued"]),
        ("running", totals["running"]),
        ("done", totals["done"]),
        ("failed", totals["failed"]),
        ("stuck / overdue", stuck_count),
        ("scripted handoffs", savings["scripted"]),
        ("bot-to-bot (grok wakes)", savings["bot_to_bot"]),
        ("scripted share", "—" if share is None else f"{round(share * 100)}%"),
        ("poller last activity", _duration(poller["last_activity_age_seconds"])),
    ]
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f'<meta http-equiv="refresh" content="{int(snapshot["refresh_seconds"])}">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="robots" content="noindex">',
        "<title>factory status</title>",
        f"<style>{_CSS}</style></head>",
        f'<body data-generated="{float(snapshot["generated_at_epoch"]):.3f}">',
        "<h1>factory status</h1>",
        f'<p class="meta">snapshot {_e(snapshot["generated_at"])} '
        f'(<span id="age"></span>), reloads every '
        f'{int(snapshot["refresh_seconds"])}s. Read-only and redacted.</p>',
        '<div class="tiles">',
        *(f'<div class="tile"><b>{_e(value)}</b>{_e(label)}</div>' for label, value in tiles),
        "</div>",
        "<h2>Queue by recipient and action</h2>",
        _table(
            ["recipient", "action", "queued", "running", "done", "failed"],
            [
                [
                    _td(row["recipient"]),
                    _td(row["action"]),
                    *(_td(row[state], num=True) for state in ("queued", "running", "done", "failed")),
                ]
                for row in snapshot["queue"]["by_recipient_action"]
            ],
            "no jobs",
        ),
        "<h2>Running jobs</h2>",
        _table(
            ["ticket", "action", "recipient", "last heartbeat", "timeout", "job"],
            [
                [
                    _td(f"{row['repo']}#{row['ticket_id']}"),
                    _td(row["action"]),
                    _td(row["recipient"]),
                    _td(
                        _duration(row["heartbeat_age_seconds"])
                        + (" overdue" if row["overdue"] else ""),
                        num=True,
                        bad=row["overdue"],
                    ),
                    _td(_duration(row["timeout_seconds"]), num=True),
                    _td(row["job"]),
                ]
                for row in snapshot["running"]
            ],
            "nothing running",
        ),
        "<h2>Queued jobs</h2>",
        _table(
            ["ticket", "action", "recipient", "waiting", "job"],
            [
                [
                    _td(f"{row['repo']}#{row['ticket_id']}"),
                    _td(row["action"]),
                    _td(row["recipient"]),
                    _td(_duration(row["age_seconds"]), num=True),
                    _td(row["job"]),
                ]
                for row in snapshot["queued"]
            ],
            "queue empty",
        ),
        "<h2>Slots</h2>",
        f'<p>box: {_e(slots["box"]["running"])} running'
        + (
            f' of {_e(slots["box"]["slot_max"])} slots'
            if slots["box"]["slot_max"] is not None
            else ""
        )
        + f' · machines.yml {_e(slots["machines_yml"])}</p>',
        _table(
            ["device", "held", "slot max", "harnesses", "enabled", "placeholder"],
            [
                [
                    _td(row["name"]),
                    _td(row["held"], num=True),
                    _td(row["slot_max"], num=True),
                    _td(", ".join(row["harnesses"]) or "—"),
                    _td(row["enabled"]),
                    _td(row["placeholder"], bad=row["placeholder"]),
                ]
                for row in slots["devices"]
            ],
            "no device inventory",
        ),
        "<h2>Stuck</h2>",
        _table(
            ["ticket", "state", "reason"],
            [
                [
                    _td(f"{row['repo']}#{row['ticket_id']}"),
                    _td(row["state"], bad=True),
                    _td(row["reason"]),
                ]
                for row in snapshot["stuck"]["tickets"]
            ],
            "no stuck tickets",
        ),
        _table(
            ["overdue job", "action", "recipient", "state", "age"],
            [
                [
                    _td(f"{row['repo']}#{row['ticket_id']}"),
                    _td(row["action"]),
                    _td(row["recipient"]),
                    _td(row["state"]),
                    _td(_duration(row["age_seconds"]), num=True, bad=True),
                ]
                for row in snapshot["stuck"]["overdue_jobs"]
            ],
            "no overdue jobs",
        ),
        "<h2>Paused</h2>",
        _table(
            ["ticket", "state", "paused action"],
            [
                [
                    _td(f"{row['repo']}#{row['ticket_id']}"),
                    _td(row["state"]),
                    _td(row["paused_action"]),
                ]
                for row in snapshot["paused"]
            ],
            "nothing paused",
        ),
        "<h2>Tickets</h2>",
        _table(
            ["ticket", "tracker", "state", "round", "gate count", "stuck"],
            [
                [
                    _td(f"{row['repo']}#{row['ticket_id']}"),
                    _td(row["tracker"]),
                    _td(row["state"]),
                    _td(row["round"], num=True),
                    _td(row["gate_count"], num=True),
                    _td(row["stuck"], bad=row["stuck"]),
                ]
                for row in snapshot["tickets"]
            ],
            "no tracked tickets",
        ),
        "<h2>Poller</h2>",
        f'<p>poller.log: {"present" if poller["log"]["present"] else "absent"}'
        + (
            f', written {_duration(poller["log"]["age_seconds"])} before the snapshot'
            if poller["log"]["present"]
            else ""
        )
        + f' · poll errors: {_e(poller["errors"]["last_hour"])} in the last hour, '
        f'{_e(poller["errors"]["last_day"])} in the last day</p>',
        _table(
            ["age", "repo", "ticket", "error"],
            [
                [
                    _td(_duration(row["age_seconds"]), num=True),
                    _td(row["repo"]),
                    _td(row["ticket_id"]),
                    _td(row["error"]),
                ]
                for row in poller["errors"]["recent"]
            ],
            "no poll errors",
        ),
    ]
    if poller["deferrals"] is not None:
        parts.append(
            _table(
                ["age", "deferral"],
                [
                    [_td(_duration(row["age_seconds"]), num=True), _td(row["detail"])]
                    for row in poller["deferrals"]["recent"]
                ],
                "no deferrals",
            )
        )
    parts.extend(
        [
            f"<h2>poller.log (last {LOG_TAIL_LINES} lines, redacted)</h2>",
            "<pre>" + "\n".join(_e(line) for line in poller["log"]["tail"]) + "</pre>",
            "<h2>Handoff savings</h2>",
            f"<p>{_e(savings['definition'])}</p>",
            f"<script>{_SCRIPT}</script>",
            "</body></html>",
        ]
    )
    return "\n".join(parts) + "\n"
