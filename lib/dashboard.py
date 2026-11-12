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
poll_errors, deferrals, log lines) goes through `redact`. It runs its rules
on the text as written and again on a decoded copy (percent and JSON
escapes undone, so encoded secrets are seen); what the decoded pass finds is
cut out of the original text with the rest of its token, so decoding only
ever widens what is hidden. The log tail also goes through
`redact_key_blocks`, which removes private-key blocks that span lines, with
markers matched in decoded form. The finished snapshot is walked once more so every
string in it has been through `redact` before it is written. Enumerated
fields (including ticket workflow states) outside their known values are
shown as "other".
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lib.dashboard_config import REFRESH_DEFAULT, REFRESH_MAX, REFRESH_MIN
from lib.handoff import STATES as WORKFLOW_STATES
from lib.linear_webhook import read_health
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
# The optional quote after a name catches JSON and repr keys ("token": ...).
_AUTH_HEADER = re.compile(
    r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key|cookie|set-cookie)"
    r"([\"']?\s*[:=]\s*)[^\r\n]*"
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
    r"(?:token|secret|passw(?:or)?d|passphrase|pwd|api[_\-]?key|key|auth"
    r"|credential|session|cookie|signature)"
    r"[A-Za-z0-9_\-]*)"
    r"([\"']?\s*[:=]\s*)"
    # A JSON string (escapes allowed, closing quote optional when cut short),
    # a single-quoted string, or a bare value.
    r"(\"(?:[^\"\\\n]|\\.)*\"?|'[^'\n]*'?|[^\s,;}\]]+)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# 32+ token characters; _opaque keeps ones that read like names, not keys.
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_\-+/=]{32,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# JSON string escapes that can hide a URL or a quoted key: \/ \" \' and
# \u00XX (ASCII only, so no lone surrogate can reach the UTF-8 writer).
_JSON_ESCAPE = re.compile(r"\\(?:u00([0-7][0-9A-Fa-f])|([/\"']))")
DECODE_ROUNDS = 8
REDACT_PASSES = 4
ENCODED_MARK = "[redacted-encoded]"
# PEM/OpenSSH/PGP armor. A block without its END runs to the end of the text.
_KEY_MARKER = r"-----\s*{0} [A-Z0-9 ]{{1,60}}-----"
_KEY_BEGIN = re.compile(_KEY_MARKER.format("BEGIN"))
_KEY_END = re.compile(_KEY_MARKER.format("END"))
_KEY_BLOCK = re.compile(
    _KEY_MARKER.format("BEGIN") + r"[\s\S]*?(?:" + _KEY_MARKER.format("END") + r"|\Z)"
)
_KEY_ORPHAN_END = re.compile(r"\A[\s\S]*?" + _KEY_MARKER.format("END"))
KEY_BLOCK_MARK = "[redacted-key-block]"


# Each rule returns None to leave a match alone, or (keep, marker): keep the
# first `keep` characters of the match (a key name, a scheme) and replace the
# rest with the marker.
Rule = Callable[[re.Match[str]], "tuple[int, str] | None"]


def _whole(marker: str) -> Rule:
    return lambda match: (0, marker)


def _scheme_token(match: re.Match[str]) -> tuple[int, str] | None:
    scheme, space, value = match.groups()
    # "Bearer x" is always a credential; "token"/"basic" need a token-like value.
    if scheme.lower() != "bearer" and not (
        any(ch.isdigit() for ch in value) or "=" in value or len(value) >= 16
    ):
        return None
    return len(scheme) + len(space), "[redacted]"


def _auth_header(match: re.Match[str]) -> tuple[int, str]:
    return len(match.group(1)) + len(match.group(2)), "[redacted]"


def _assignment(match: re.Match[str]) -> tuple[int, str] | None:
    if match.group(3).startswith("[redacted"):
        return None
    return len(match.group(1)) + len(match.group(2)), "[redacted]"


def _opaque(match: re.Match[str]) -> tuple[int, str] | None:
    value = match.group(0)
    digits = sum(ch.isdigit() for ch in value)
    letters = sum(ch.isalpha() for ch in value)
    # A repo path like owner1/some-long-repo-name has at most one digit.
    return (0, "[redacted]") if digits >= 2 and letters >= 2 else None


_RULES = (
    (_KEY_BLOCK, _whole(KEY_BLOCK_MARK)),
    (_KEY_ORPHAN_END, _whole(KEY_BLOCK_MARK)),
    (_URL, _whole("[redacted-url]")),
    (_AUTH_HEADER, _auth_header),
    (_SCHEME_TOKEN, _scheme_token),
    (_KNOWN_TOKEN, _whole("[redacted-token]")),
    (_USERINFO, _whole("[redacted-credentials]")),
    (_EMAIL, _whole("[redacted-email]")),
    (_SECRET_ASSIGNMENT, _assignment),
    (_OPAQUE, _opaque),
)
MARKER = -1  # origin of a character a rule inserted


def _apply_rules(text: str) -> tuple[str, list[int], set[int]]:
    """Run every rule in order over text.

    Returns the redacted text, the origin of each of its characters (an
    index into `text`, or MARKER for inserted marker text) and the set of
    indices into `text` that some rule removed.
    """
    origin = list(range(len(text)))
    removed: set[int] = set()
    for pattern, rule in _RULES:
        pieces: list[str] = []
        origins: list[int] = []
        last = 0
        for match in pattern.finditer(text):
            decision = rule(match)
            if decision is None:
                continue
            keep, marker = decision
            cut = match.start() + keep
            pieces.append(text[last:cut])
            origins.extend(origin[last:cut])
            pieces.append(marker)
            origins.extend([MARKER] * len(marker))
            removed.update(index for index in origin[cut : match.end()] if index >= 0)
            last = match.end()
        if pieces:
            pieces.append(text[last:])
            origins.extend(origin[last:])
            text, origin = "".join(pieces), origins
    return text, origin, removed


def _hex(text: str) -> bool:
    return len(text) == 2 and all(ch in "0123456789abcdefABCDEF" for ch in text)


def _decode_once(
    text: str, spans: list[tuple[int, int]]
) -> tuple[str, list[tuple[int, int]]]:
    """One round of percent and JSON-escape decoding, keeping for every output
    character the span of original characters it came from."""
    out: list[str] = []
    out_spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "%" and _hex(text[index + 1 : index + 3]):
            # A run of %XX bytes decodes as UTF-8 together, like unquote.
            end = index
            data = bytearray()
            while text[end : end + 1] == "%" and _hex(text[end + 1 : end + 3]):
                data.append(int(text[end + 1 : end + 3], 16))
                end += 3
            span = (spans[index][0], spans[end - 1][1])
            for decoded in data.decode("utf-8", errors="replace"):
                out.append(decoded)
                out_spans.append(span)
            index = end
            continue
        if char == "\\":
            match = _JSON_ESCAPE.match(text, index)
            if match is not None:
                out.append(
                    chr(int(match.group(1), 16)) if match.group(1) else match.group(2)
                )
                out_spans.append((spans[index][0], spans[match.end() - 1][1]))
                index = match.end()
                continue
        out.append(char)
        out_spans.append(spans[index])
        index += 1
    return "".join(out), out_spans


def _decode_map(text: str) -> tuple[str, list[tuple[int, int]], bool]:
    """Undo percent-encoding and JSON string escapes until nothing changes.

    Repeated, so double-encoded text (%253A) is seen too. Returns the
    decoded text, for each decoded character the [start, end) span of
    `text` it came from, and whether decoding settled within DECODE_ROUNDS.
    Control characters an escape produced (%0A is kept as a line break,
    \u0000 is dropped) are removed, as in `_strip_control`.
    """
    spans = [(index, index + 1) for index in range(len(text))]
    current = text
    settled = False
    for _ in range(DECODE_ROUNDS):
        decoded, decoded_spans = _decode_once(current, spans)
        if decoded == current:
            settled = True
            break
        current, spans = decoded, decoded_spans
    kept = [
        (char, span)
        for char, span in zip(current, spans)
        if char == "\n" or not _CONTROL.match(char)
    ]
    current = "".join(" " if char == "\t" else char for char, _ in kept)
    return current, [span for _, span in kept], settled


def _token_end(text: str, index: int) -> int:
    while index < len(text) and not text[index].isspace():
        index += 1
    return index


def _widen(text: str, decoded: str, spans: list[tuple[int, int]]) -> set[int]:
    """Indices of `text` to hide because its decoded form holds a secret.

    The rules run again over the decoded text. Every character they remove
    maps back to the original characters it came from, and the hidden part
    runs on from there to the end of that original whitespace-separated
    token. A separator that exists only once decoded (%20, %3B, %22, \")
    can therefore end a match early but never shows what follows it.
    """
    if decoded == text:
        return set()
    _, _, removed = _apply_rules(decoded)
    hidden: set[int] = set()
    run_start = previous = None
    for index in sorted(removed) + [None]:
        if index is not None and previous is not None and index == previous + 1:
            previous = index
            continue
        if run_start is not None and previous is not None:
            start = spans[run_start][0]
            end = _token_end(text, spans[previous][1])
            hidden.update(range(start, end))
        run_start = previous = index
    return hidden


def _drop(text: str, origin: list[int], hidden: set[int]) -> str:
    """Replace each run of characters whose origin is hidden with a marker
    (none when the run touches a marker already there)."""
    pieces: list[str] = []
    index = 0
    while index < len(text):
        if origin[index] not in hidden or origin[index] == MARKER:
            pieces.append(text[index])
            index += 1
            continue
        end = index
        while end < len(text) and origin[end] != MARKER and origin[end] in hidden:
            end += 1
        touches = (index > 0 and origin[index - 1] == MARKER) or (
            end < len(text) and origin[end] == MARKER
        )
        if not touches:
            pieces.append("[redacted]")
        index = end
    return "".join(pieces)


def _redact_pass(text: str) -> str:
    decoded, spans, settled = _decode_map(text)
    if not settled:
        # Still changing after DECODE_ROUNDS rounds: too deep to inspect.
        return ENCODED_MARK
    redacted, origin, _ = _apply_rules(text)
    hidden = _widen(text, decoded, spans)
    return _drop(redacted, origin, hidden) if hidden else redacted


def redact(text: str) -> str:
    """Remove anything shaped like a credential, key, URL or email address.

    The rules run on the text as written, then on its decoded form (percent
    and JSON escapes undone); anything the decoded pass finds is cut out of
    the original text together with the rest of its token (see `_widen`).
    Decoding can only widen what is hidden. The result is the original text
    with secrets replaced by markers, never a decoded form, and is checked
    again until a further pass changes nothing.
    """
    text = _strip_control(text)
    for _ in range(REDACT_PASSES):
        redacted = _redact_pass(text)
        if redacted == text:
            return redacted
        text = redacted
    return ENCODED_MARK if _redact_pass(text) != text else text


def _starts_inside_key_block(decoded_lines: Sequence[str]) -> bool:
    for line in decoded_lines:
        begin = _KEY_BEGIN.search(line)
        end = _KEY_END.search(line)
        if end is not None and (begin is None or end.start() < begin.start()):
            return True
        if begin is not None:
            return False
    return False


def redact_key_blocks(lines: Sequence[str]) -> list[str]:
    """Replace every log line inside a private-key block with a marker.

    The tail is read line by line, so a block spans entries: the BEGIN line,
    the base64 body (whose short last line no other rule would catch) and
    the END line. The markers are looked for in each line's decoded form,
    so percent- or JSON-escaped markers count too; the text kept around a
    marker is the original. When the tail starts inside a block (an END
    comes before any BEGIN, because the BEGIN line scrolled out), the lines
    up to that END are treated as the block's body. A block with no END
    hides the rest of the tail.
    """
    decoded_lines = [_decode_map(line) for line in lines]
    inside = _starts_inside_key_block([decoded for decoded, _, _ in decoded_lines])
    result: list[str] = []
    for line, (decoded, spans, _) in zip(lines, decoded_lines):
        pieces: list[str] = []
        raw_at = 0
        decoded_at = 0
        if inside:
            end = _KEY_END.search(decoded)
            if end is None:
                result.append(KEY_BLOCK_MARK)
                continue
            pieces.append(KEY_BLOCK_MARK)
            raw_at = spans[end.end() - 1][1]
            decoded_at = end.end()
            inside = False
        while True:
            block = _KEY_BLOCK.search(decoded, decoded_at)
            if block is None:
                break
            pieces.append(line[raw_at : max(raw_at, spans[block.start()][0])])
            pieces.append(KEY_BLOCK_MARK)
            if _KEY_END.search(block.group(0)) is None:
                inside = True
                raw_at = len(line)
                break
            raw_at = spans[block.end() - 1][1]
            decoded_at = block.end()
        pieces.append(line[raw_at:])
        result.append("".join(pieces))
    return result


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
    """Open an existing database with a SQLite mode=ro URI and query_only.

    Symlinks are resolved first, so the existence check, the URI and the
    -wal/-shm files SQLite uses all belong to the real file. It never uses
    immutable=1: the poller's writers come and go (the -wal is removed when
    the last one closes), so SQLite's normal shared locks and WAL reads are
    what keep a read consistent while a writer commits or checkpoints. Like
    any reader of a WAL database, SQLite may create the -wal and -shm files
    beside it. When it cannot (a read-only directory), the open fails with
    that explanation rather than falling back to an unlocked read.
    """
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DashboardError(f"database does not exist: {path}") from None
    if not real.is_file():
        raise DashboardError(f"database does not exist: {path}")
    uri = real.as_uri() + "?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        # Opening is lazy; read the schema now so a failure surfaces here.
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        message = f"cannot read database {real}: {exc}"
        if not os.access(real.parent, os.W_OK):
            message += (
                f" ({real.parent} is not writable, so SQLite cannot create the"
                " -wal/-shm files a WAL database is read through; the dashboard"
                " does not fall back to an unlocked immutable=1 read)"
            )
        raise DashboardError(message) from exc


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
    tail = (
        [_clean(line) for line in redact_key_blocks(read_log_tail(log_path))]
        if log_present
        else []
    )
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
            # Only when a Linear webhook listener has run on this database.
            **(
                {"linear_webhook": db["linear_webhook"]}
                if db["linear_webhook"] is not None
                else {}
            ),
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

    held: dict[str, int] = {}
    placed: set[str] = set()
    if "device_slots" in tables:
        for row in connection.execute("SELECT job_id, device FROM device_slots"):
            placed.add(str(row["job_id"]))
            held[str(row["device"])] = held.get(str(row["device"]), 0) + 1

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
                # Like `orch status` (lib/status.py box_slots), a job holding a
                # device-pool slot counts on its device, not on the box.
                if row["recipient"] != "grok" and str(row["id"]) not in placed:
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
            state = _enum(row["workflow_state"], WORKFLOW_STATES)
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

    linear_webhook = None
    health = read_health(connection)
    if health is not None:
        linear_webhook = {
            "last_received_age_seconds": _age(now, health.last_received_at),
            "last_event": (
                _clean(health.last_event, 64) if health.last_event is not None else None
            ),
            "listener_age_seconds": _age(now, health.started_at),
            "dirty": health.dirty,
            "warning": health.silence_warning(now),
        }

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
        "linear_webhook": linear_webhook,
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
    """Write status.json and index.html from one snapshot, as a pair.

    Both are written to temp files in the same directory and fsynced, then
    renamed into place. If the second rename fails, status.json is put back
    as it was (or removed if there was none), so the directory never holds
    one file from this snapshot and one from an earlier one.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    status_path = out_dir / "status.json"
    index_path = out_dir / "index.html"
    temps: list[str] = []
    try:
        status_temp = _write_temp(
            status_path, json.dumps(snapshot, indent=2, sort_keys=True) + "\n", temps
        )
        index_temp = _write_temp(index_path, render_html(snapshot), temps)
        previous = None
        if status_path.is_file():
            previous = _temp_name(status_path, temps, suffix=".prev")
            shutil.copyfile(status_path, previous)
        os.replace(status_temp, status_path)
        try:
            os.replace(index_temp, index_path)
        except BaseException:
            if previous is not None:
                os.replace(previous, status_path)
            else:
                status_path.unlink(missing_ok=True)
            raise
    finally:
        for temp in temps:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
    return status_path, index_path


def _temp_name(path: Path, temps: list[str], *, suffix: str = ".tmp") -> str:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=suffix)
    os.close(fd)
    temps.append(temp)
    return temp


def _write_temp(path: Path, text: str, temps: list[str]) -> str:
    temp = _temp_name(path, temps)
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o644)
    return temp


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
    webhook = poller.get("linear_webhook")
    if webhook is not None:
        if webhook["last_received_age_seconds"] is not None:
            received = (
                f'{_duration(webhook["last_received_age_seconds"])} ago'
                + (f' ({webhook["last_event"]})' if webhook["last_event"] else "")
            )
        else:
            received = "never"
        parts.append("<h2>Linear webhook</h2>")
        parts.append(
            f"<p>last received {_e(received)} · dirty queue {_e(webhook['dirty'])}</p>"
        )
        if webhook["warning"] is not None:
            parts.append(f'<p class="bad">warning: {_e(webhook["warning"])}</p>')
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
