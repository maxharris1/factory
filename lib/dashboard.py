"""Read-only, redacted dashboard snapshot (status.json plus four pages).

`orch dashboard snapshot` opens the dispatcher database with a SQLite
`mode=ro` URI and reads the tail of poller.log. It never writes either.
It writes status.json and the self-contained pages (PAGES: index.html,
job.html, activity.html, efficiency.html, health.html; see
lib/dashboard_page.py) to an
output directory, each atomically (temp file in the same directory, fsync,
then rename). Each page's inline script fetches status.json every 1 to 2
seconds and updates in place. Without script (or when status.json cannot
be fetched) the server-rendered page reloads every refresh_seconds (15 to
30). The job page's diagram is model/chart.mmd (lib/dashboard_flow.py).

Secrets: only allowlisted columns are selected (never heads, comment
bodies or environment values; the environment is not read except for the
ORCH_* path variables). jobs.detail is read only to take its `round`
field, kept only when it is digits/digits; nothing else of it is copied.
Every free-text value (stuck reasons, poll_errors, deferrals, log lines)
goes through `redact`. It runs its rules
on the text as written and again on a decoded copy (percent and JSON
escapes undone, so encoded secrets are seen); what the decoded pass finds is
cut out of the original text with the rest of its token, so decoding only
ever widens what is hidden. The log tail also goes through
`redact_key_blocks`, which removes private-key blocks that span lines, with
markers matched in decoded form. The finished snapshot is walked once more so every
string in it has been through `redact` before it is written. Enumerated
fields (including ticket workflow states) outside their known values are
shown as "other".

Health (#92): the component heartbeats in orch.health.db beside the
database (lib/health.py, also opened read-only) become `health`; each
down component (including one machines.yml or orch.yml expects that has
never beaten), each harness with queued jobs but no live waiter, and an
orch.health.db that cannot be read is a needs-attention item linked to the
Health tab, and such a queued job carries `no_waiter`. With no
orch.health.db nothing is flagged.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lib import health as component_health
from lib.dashboard_config import REFRESH_DEFAULT, REFRESH_MAX, REFRESH_MIN
from lib.dashboard_flow import job_flow, load_chart
from lib.dashboard_page import PAGES, render_pages
from lib.dashboard_page import duration as _duration
from lib.feeder import STATES as FEEDER_STATES
from lib.feeder import feeder_counts, feeder_items, last_feeder_poll
from lib.handoff import STATES as WORKFLOW_STATES
from lib.job_state import (
    DEAD,
    JOB_STATES,
    STALE,
    Alive,
    JobStateConfig,
    derive,
    liveness,
    read_placements,
)
from lib.linear_webhook import read_health
from lib.load import load_yaml
from lib.orch_yml import POLL_SECONDS, POLL_SECONDS_ALLOWED, OrchConfig
from lib.pr_title import read_sources
from lib.queue import ACTIONS, RECIPIENTS, STATES
from lib.status import PAUSED_STATES, _hold_approvers
from lib.worktrees import pid_alive

SCHEMA = "factory-dashboard/v1"
LOG_TAIL_LINES = 50
LOG_TAIL_BYTES = 256 * 1024
RAW_LINE_LIMIT = 4096
LINE_LIMIT = 300
RECENT_ERRORS = 20
RECENT_DEFERRALS = 10
RECENT_HANDOFFS = 12
RECENT_VERDICTS = 12
# The Linear share of the hourly API budget the poller allows itself
# (lib/poll_source.py LINEAR_*_PER_HOUR * LINEAR_SHARE).
LINEAR_BUDGET_REQUESTS_PER_HOUR = 1250
LINEAR_BUDGET_COMPLEXITY_PER_HOUR = 1_500_000
# Poll health from the age of the last finished cycle (orch poll), scaled to
# the poller's own poll_seconds (30 or 60): ok up to two cycles plus 30
# seconds, late up to five cycles, stale after that.


def health_thresholds(poll_seconds: int) -> tuple[int, int]:
    """(ok, late) cycle-age limits in seconds for a `poll_seconds` poller."""
    return 2 * poll_seconds + 30, 5 * poll_seconds


HEALTH_OK_SECONDS, HEALTH_LATE_SECONDS = health_thresholds(POLL_SECONDS)
# A handled orch:v1 comment's outcome as the dispatcher writes it.
_OUTCOME = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_ROUND = re.compile(r"[0-9]{1,3}/[0-9]{1,3}")
MACHINES_MAX_BYTES = 256 * 1024
WAIT_RECIPIENTS = ("claude", "codex", "cursor")
HARNESSES = ("claude", "codex", "cursor")
TRACKERS = ("github", "linear")
ATTENTION_REASONS = ("ship-failed", "dedupe-dropped")

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
SCRIPTED_SHARE_DEFINITION = (
    "Scripted share is the part of handoffs a script routed straight to the "
    "next bot, scripted / (scripted + grok wakes), rather than one bot waking another."
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
    last_cycle_at: float | None = None,
    chart_path: Path | None = None,
    job_states: JobStateConfig | None = None,
    alive: Alive | None = pid_alive,
    poll_seconds: int = POLL_SECONDS,
    orch: OrchConfig | None = None,
    publish_seconds: float | None = None,
) -> dict:
    """Read everything once. `last_cycle_at` is when the poller's last cycle
    ended (the live push passes it); without it poll health is "unknown".
    `poll_seconds` is the poller's orch.yml poll_seconds (30 or 60): poll
    health's `cycle_seconds` and its ok and late thresholds follow it.
    `chart_path` is the statechart chart the job page draws (default
    model/chart.mmd); only its file name is copied out. `job_states` is
    orch.yml's job_states (the stale threshold); `alive` checks a recorded
    runner pid on this host (lib/job_state.py), never over SSH. `orch` is
    the loaded orch.yml, which says whether the Linear listener and the
    dashboard writer are expected to heartbeat (lib/health.py).
    `publish_seconds` is how often a pushing writer publishes the snapshot
    (None for a one-off snapshot): the page keeps the component states it
    observed until a newer snapshot is overdue (lib/dashboard_page.py)."""
    if (
        isinstance(refresh_seconds, bool)
        or not isinstance(refresh_seconds, int)
        or not REFRESH_MIN <= refresh_seconds <= REFRESH_MAX
    ):
        raise DashboardError("refresh_seconds must be 15 to 30")
    if last_cycle_at is not None and (
        isinstance(last_cycle_at, bool)
        or not isinstance(last_cycle_at, (int, float))
        or not math.isfinite(last_cycle_at)
    ):
        raise DashboardError("last_cycle_at must be a Unix time")
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, int)
        or poll_seconds not in POLL_SECONDS_ALLOWED
    ):
        raise DashboardError("poll_seconds must be 30 or 60")
    chart = load_chart(chart_path)
    connection = open_readonly(db_path)
    try:
        connection.execute("BEGIN")
        db = _read_db(
            connection,
            now,
            chart,
            job_states if job_states is not None else JobStateConfig(),
            alive,
        )
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
    # One slot count per device (#90): a running harness job with no
    # placement runs on the box, so it counts in the box's held slots, and
    # slots.box.running is every running job on the box, placed or not.
    machines_state, devices, box_max, box_name = _machines(
        machines_path, db["held"], unplaced=db["box_running"]
    )
    box_running = db["box_running"] + db["running_on"].get(box_name, 0)
    report = component_health.load_report(
        db_path,
        now,
        queued=[(item["job"], item["recipient"]) for item in db["queued"]],
        machines=(
            component_health.load_machines(machines_path)
            if machines_path is not None
            else None
        ),
        orch=orch,
    )
    for item in db["queued"]:
        flag = report.no_waiter_for(item["recipient"])
        if flag is not None:
            item["no_waiter"] = flag.harness
            item["no_waiter_link"] = f"health.html#{flag.anchor}"

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
            "box": {"running": box_running, "slot_max": box_max},
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
            # Only when a Linear webhook listener or webhook-mode poller has
            # run on this database.
            **(
                {"linear_webhook": db["linear_webhook"]}
                if db["linear_webhook"] is not None
                else {}
            ),
            "health": _poll_health(
                _age(now, last_cycle_at),
                _age(now, max(activity)) if activity else None,
                poll_seconds,
            ),
            "linear_budget": db["linear_budget"],
            "verdicts": db["verdicts"],
        },
        "handoff_savings": db["handoff_savings"],
        "attention": _attention(
            db["stuck_tickets"], db["overdue_jobs"], db["paused"], db["attention_flags"],
            report,
        ),
        "health": {**component_health.report_json(report, now),
                   "publish_seconds": publish_seconds},
        # The backlog feeder at its last poll (issue #85); None when none has run.
        "feeder": db["feeder"],
        "statechart": chart,
    }
    _add_sources(snapshot, db["sources"])
    return _scrub(snapshot)  # type: ignore[return-value]


def _add_sources(value: object, sources: dict[tuple[str, str], tuple[str, str | None]]) -> None:
    """Issue #115: each ticket row gets its source issue and PR, shown issue first."""
    if isinstance(value, list):
        for item in value:
            _add_sources(item, sources)
    elif isinstance(value, dict):
        found = sources.get((str(value.get("repo")).casefold(), str(value.get("ticket_id"))))
        if found is not None:
            value["source"] = _clean(found[0], 16)
            value["pr"] = _clean(found[1], 16) if found[1] is not None else None
        for item in value.values():
            _add_sources(item, sources)


def _attention(
    stuck: list[dict],
    overdue: list[dict],
    paused: list[dict],
    flags: Sequence[dict] = (),
    health: component_health.HealthReport | None = None,
) -> dict:
    """Everything that needs Max, each ticket or job once: stuck tickets,
    overdue jobs, tickets flagged needs-attention (a ship that failed and
    will not be retried, a job dedupe dropped: #83, #84), paused tickets
    that are not already listed, then (#92) down components, an unreadable
    orch.health.db and harnesses whose queued jobs have no live waiter, each
    linked to the Health tab. `count` is the badge on every page."""
    items: list[dict] = []
    for row in stuck:
        detail = row["state"] + (f": {row['reason']}" if row["reason"] else "")
        items.append({"kind": "stuck", "repo": row["repo"], "ticket_id": row["ticket_id"],
                      "detail": detail})
    for row in overdue:
        job_state = row.get("job_state")
        if job_state == DEAD:
            kind, what = "dead", "runner gone"
        elif row.get("timed_out", True):
            kind, what = "overdue", row["state"]
        else:
            kind, what = "stale", row["state"]
        items.append({
            "kind": kind,
            "repo": row["repo"],
            "ticket_id": row["ticket_id"],
            "detail": f"{row['recipient']} {row['action']} {what}, "
                      f"no heartbeat for {_duration(row['age_seconds'])}",
        })
    for row in flags:
        detail = row["reason"] + (f": {row['detail']}" if row["detail"] else "")
        items.append({"kind": "needs-attention", "repo": row["repo"],
                      "ticket_id": row["ticket_id"], "detail": detail})
    listed = {(row["repo"], row["ticket_id"]) for row in stuck}
    listed |= {(row["repo"], row["ticket_id"]) for row in flags}
    for row in paused:
        if (row["repo"], row["ticket_id"]) in listed:
            continue
        detail = row["state"] + (f" ({row['paused_action']})" if row["paused_action"] else "")
        if row.get("waiting_on"):
            detail += f" (waiting on {row['waiting_on']})"
        items.append({"kind": "paused", "repo": row["repo"], "ticket_id": row["ticket_id"],
                      "detail": detail})
    for component in health.down if health is not None else ():
        reason = f" ({component.reason})" if component.reason else ""
        items.append({"kind": "down", "repo": "", "ticket_id": "",
                      "component": component.label,
                      "link": f"health.html#{component.anchor}",
                      "detail": f"down{reason}"})
    if health is not None and health.error is not None:
        items.append({"kind": "health", "repo": "", "ticket_id": "",
                      "component": "health storage", "link": "health.html#components",
                      "detail": f"unknown: {health.error}"})
    for flag in health.no_waiter if health is not None else ():
        count = len(flag.jobs)
        items.append({"kind": "no-waiter", "repo": "", "ticket_id": "",
                      "component": f"waiter {flag.bot}",
                      "link": f"health.html#{flag.anchor}",
                      "detail": f"no waiter for {flag.harness} "
                                f"({count} queued job{'' if count == 1 else 's'})"})
    return {"count": len(items), "items": items}


def _poll_health(
    cycle_age: int | None, activity_age: int | None, poll_seconds: int
) -> dict:
    ok_seconds, late_seconds = health_thresholds(poll_seconds)
    if cycle_age is None:
        state = "unknown"
    elif cycle_age <= ok_seconds:
        state = "ok"
    elif cycle_age <= late_seconds:
        state = "late"
    else:
        state = "stale"
    return {
        "state": state,
        "cycle_seconds": poll_seconds,
        "last_cycle_age_seconds": cycle_age,
        "last_activity_age_seconds": activity_age,
    }


def _verdict_kind(outcome: str) -> str:
    """Group a handled comment's outcome for the feed."""
    if outcome.startswith("queued-"):
        return "routed"
    if outcome.startswith("stale-"):
        return "stale"
    if outcome.startswith(("rejected-", "refused-")):
        return "rejected"
    if outcome.startswith(("stuck-", "capped-")):
        return "stuck"
    if outcome in ("merged", "merge-confirmed", "handed-off"):
        return "shipped"
    if outcome.startswith("ship-") or outcome == "approve-ci-red":
        return "blocked"
    if outcome.startswith(("waiting-", "already-waiting")) or outcome == "no-questions":
        return "waiting"
    return "other"


def _handoff_kind(recipient: str, state: str, has_handoff: bool) -> str:
    if state == "failed":
        return "failed"
    if recipient == "grok":
        return "bot_to_bot"
    if has_handoff and recipient in WAIT_RECIPIENTS:
        return "scripted"
    return "done"


def _detail_round(detail: object) -> str | None:
    """A job's review round ("2/3") from its detail JSON, or None."""
    if not isinstance(detail, str):
        return None
    try:
        value = json.loads(detail)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    found = value.get("round")
    return found if isinstance(found, str) and _ROUND.fullmatch(found) else None


def _read_db(
    connection: sqlite3.Connection,
    now: float,
    chart: dict | None = None,
    job_states: JobStateConfig = JobStateConfig(),
    alive: Alive | None = pid_alive,
) -> dict:
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
    placements: dict[str, tuple[str, float | None]] = {}
    if "device_slots" in tables:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(device_slots)")
        }
        placed_at = "placed_at" if "placed_at" in columns else "NULL"
        for row in connection.execute(
            f"SELECT job_id, device, {placed_at} AS placed_at FROM device_slots"
        ):
            placed.add(str(row["job_id"]))
            held[str(row["device"])] = held.get(str(row["device"]), 0) + 1
            placements[str(row["job_id"])] = (
                str(row["device"]),
                float(row["placed_at"]) if row["placed_at"] is not None else None,
            )

    slots = read_placements(connection) if "device_slots" in tables else {}
    running: list[dict] = []
    queued: list[dict] = []
    overdue: list[dict] = []
    # Each running or queued job's ticket as stored, to find its history.
    active: list[tuple[dict, str, str]] = []
    box_running = 0  # running harness jobs with no placement: on the box (#90)
    running_on: dict[str, int] = {}  # running jobs per device they are placed on
    if jobs:
        for row in connection.execute(
            """
            SELECT id, repo, ticket_id, action, recipient, state,
                   timeout_seconds, created_at, last_activity_at, detail
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
            job_state: str | None = None
            if row["state"] == "running":
                age = _age(now, row["last_activity_at"]) or 0
                # The lane: a device-pool placement, else a box slot (grok
                # wakes hold none). jobs has no claim time, so an unplaced
                # job's elapsed time counts from when it was queued.
                placement = placements.get(str(row["id"]))
                if placement is not None:
                    device: str | None = _clean(placement[0], 40)
                    started = placement[1]
                    elapsed_from = "placed"
                else:
                    device = None if row["recipient"] == "grok" else "box"
                    started = None
                    elapsed_from = "queued"
                if started is None:
                    started = row["created_at"]
                    elapsed_from = "queued"
                slot = slots.get(str(row["id"]))
                job_state = _enum(
                    derive(
                        last_activity_at=float(row["last_activity_at"]),
                        now=now,
                        placement=slot,
                        stale_seconds=job_states.stale_seconds,
                        liveness=liveness(slot, alive),
                    ),
                    JOB_STATES,
                )
                item = {
                    **base,
                    "job_state": job_state,
                    "round": _detail_round(row["detail"]),
                    "heartbeat_age_seconds": age,
                    "timeout_seconds": timeout,
                    "overdue": age > timeout,
                    "device": device,
                    "elapsed_seconds": _age(now, started) or 0,
                    "elapsed_from": elapsed_from,
                }
                running.append(item)
                active.append((item, str(row["repo"]), str(row["ticket_id"])))
                # Like `orch status`, a job holding a device-pool slot counts
                # on its device; a harness job with no placement runs on the
                # box and counts there (#90; added in collect_snapshot).
                if row["recipient"] != "grok" and str(row["id"]) not in placed:
                    box_running += 1
                elif placement is not None:
                    running_on[placement[0]] = running_on.get(placement[0], 0) + 1
            else:
                # The poller's queue-wait clock: since it was queued, or
                # since a claim that never started was released (#77 F1.2).
                age = _age(now, row["last_activity_at"]) or 0
                item = {**base, "round": _detail_round(row["detail"]), "age_seconds": age}
                queued.append(item)
                active.append((item, str(row["repo"]), str(row["ticket_id"])))
            # Stuck / overdue: past the timeout, or (#75) a running job whose
            # heartbeat went stale or whose runner is dead.
            if age > timeout or job_state in (STALE, DEAD):
                overdue.append({
                    **base,
                    "state": _enum(row["state"], STATES),
                    "job_state": job_state,
                    "timed_out": age > timeout,
                    "age_seconds": age,
                })
        running.sort(key=lambda item: -item["heartbeat_age_seconds"])

    tickets: list[dict] = []
    stuck_tickets: list[dict] = []
    paused: list[dict] = []
    # (repo casefolded, ticket id) as stored -> tracker, round, state.
    ticket_info: dict[tuple[str, str], tuple[str, int, str]] = {}
    if "tracked_tickets" in tables and "ticket_state" in tables:
        approvers = _hold_approvers(connection)
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
            # Issue #86: who a held PR waits on, only while awaiting-ok.
            approver = (
                approvers.get((str(row["repo"]), str(row["ticket_id"])))
                if str(row["workflow_state"]) == "awaiting-ok"
                else None
            )
            waiting = {"waiting_on": _clean(approver, 39)} if approver else {}
            ticket_info[(str(row["repo"]).casefold(), str(row["ticket_id"]))] = (
                _enum(row["tracker"], TRACKERS),
                int(row["review_round"]),
                state,
            )
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
                    **waiting,
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
                        **waiting,
                    }
                )
    _add_job_tickets(connection, active, ticket_info, chart, now)

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

    feeder = None
    if last_feeder_poll(connection) is not None:
        items = feeder_items(connection)
        feeder = {
            **feeder_counts(connection),
            "items": [
                {
                    "repo": _clean(item["repo"], 120),
                    "issue": _clean(item["issue"], 64),
                    "state": _enum(item["state"], FEEDER_STATES),
                    "pr": _clean(item["pr_number"], 16) if item["pr_number"] is not None else None,
                }
                for item in items
            ],
        }
    attention_flags: list[dict] = []
    if "needs_attention" in tables:
        finished = (
            """
            LEFT JOIN ticket_state AS state
              ON state.repo = flag.repo AND state.ticket_id = flag.ticket_id
            WHERE COALESCE(state.workflow_state, '') NOT IN ('merged', 'handed-off', 'done')
            """
            if "ticket_state" in tables
            else ""
        )
        for row in connection.execute(
            f"""
            SELECT flag.repo, flag.ticket_id, flag.reason, flag.detail
            FROM needs_attention AS flag
            {finished}
            ORDER BY flag.raised_at, flag.repo, flag.ticket_id, flag.reason
            """
        ):
            attention_flags.append(
                {
                    "repo": _clean(row["repo"], 120),
                    "ticket_id": _clean(row["ticket_id"], 64),
                    "reason": _enum(row["reason"], ATTENTION_REASONS),
                    "detail": _clean(row["detail"]) if row["detail"] is not None else "",
                }
            )

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

    recent_handoffs: list[dict] = []
    if jobs:
        for row in connection.execute(
            """
            SELECT id, repo, ticket_id, action, recipient, state, finished_at,
                   handoff_comment_id IS NOT NULL AS has_handoff
            FROM jobs
            WHERE state IN ('done', 'failed') AND finished_at IS NOT NULL
            ORDER BY finished_at DESC, id DESC
            LIMIT ?
            """,
            (RECENT_HANDOFFS,),
        ):
            recipient = _enum(row["recipient"], RECIPIENTS)
            state = _enum(row["state"], STATES)
            recent_handoffs.append(
                {
                    "job": _clean(str(row["id"])[:8], 8),
                    "repo": _clean(row["repo"], 100),
                    "ticket_id": _clean(row["ticket_id"], 64),
                    "action": _enum(row["action"], ACTIONS),
                    "recipient": recipient,
                    "state": state,
                    "kind": _handoff_kind(recipient, state, bool(row["has_handoff"])),
                    "age_seconds": _age(now, row["finished_at"]),
                }
            )

    verdicts = None
    if "handled_comments" in tables:
        verdicts = []
        for row in connection.execute(
            "SELECT repo, ticket_id, outcome FROM handled_comments "
            "ORDER BY rowid DESC LIMIT ?",
            (RECENT_VERDICTS,),
        ):
            outcome = str(row["outcome"])
            if _OUTCOME.fullmatch(outcome) is None:
                outcome = "other"
            verdicts.append(
                {
                    "repo": _clean(row["repo"], 100),
                    "ticket_id": _clean(row["ticket_id"], 64),
                    "outcome": _clean(outcome, 48),
                    "kind": _verdict_kind(outcome),
                }
            )

    budget = {"deferrals": 0, "exhausted": 0, "rate_limited": 0}
    if "poll_deferrals" in tables:
        row = connection.execute(
            "SELECT count(*) AS n FROM poll_deferrals "
            "WHERE recorded_at >= ? AND detail LIKE '%Linear poll budget%'",
            (now - 3600,),
        ).fetchone()
        budget["deferrals"] = int(row["n"])
    if "poll_errors" in tables:
        row = connection.execute(
            """
            SELECT
              COALESCE(SUM(error LIKE '%Linear poll budget is exhausted%'), 0)
                AS exhausted,
              COALESCE(SUM(error LIKE '%Linear returned HTTP 429%'
                           OR error LIKE '%Linear returned RATELIMITED%'
                           OR error LIKE '%Linear rate-limit backoff%'), 0)
                AS rate_limited
            FROM poll_errors WHERE recorded_at >= ?
            """,
            (now - 3600,),
        ).fetchone()
        budget["exhausted"] = int(row["exhausted"])
        budget["rate_limited"] = int(row["rate_limited"])
    if budget["rate_limited"]:
        budget_state = "rate_limited"
    elif budget["exhausted"]:
        budget_state = "exhausted"
    elif budget["deferrals"]:
        budget_state = "deferring"
    else:
        budget_state = "ok"

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
        "running_on": running_on,
        "overdue_jobs": overdue,
        "tickets": tickets,
        "stuck_tickets": stuck_tickets,
        "paused": paused,
        "errors": errors,
        "newest_error_at": newest_error_at,
        "deferrals": deferrals,
        "newest_deferral_at": newest_deferral_at,
        "linear_webhook": linear_webhook,
        "feeder": feeder,
        "attention_flags": attention_flags,
        "sources": read_sources(connection),
        "held": held,
        "verdicts": verdicts,
        "linear_budget": {
            "state": budget_state,
            "deferrals_last_hour": budget["deferrals"],
            "exhausted_last_hour": budget["exhausted"],
            "rate_limited_last_hour": budget["rate_limited"],
            "requests_per_hour": LINEAR_BUDGET_REQUESTS_PER_HOUR,
            "complexity_per_hour": LINEAR_BUDGET_COMPLEXITY_PER_HOUR,
        },
        "handoff_savings": {
            "scripted": scripted,
            "scripted_in_flight": in_flight,
            "bot_to_bot": bot_to_bot,
            "scripted_share": (
                round(scripted / denominator, 3) if denominator else None
            ),
            "definition": HANDOFF_SAVINGS_DEFINITION,
            "scripted_share_definition": SCRIPTED_SHARE_DEFINITION,
            "recent": recent_handoffs,
        },
    }


def _add_job_tickets(
    connection: sqlite3.Connection,
    active: list[tuple[dict, str, str]],
    ticket_info: dict[tuple[str, str], tuple[str, int, str]],
    chart: dict | None,
    now: float,
) -> None:
    """Give each running or queued job its ticket's tracker and flow (current
    statechart state, the path its jobs took, time in state). The job's
    round comes from its own detail (#75), not the ticket's completed
    review count."""
    if not active:
        return
    # One read for every ticket with a running or queued job, oldest first.
    wanted = {(repo.casefold(), ticket_id) for _, repo, ticket_id in active}
    ids = sorted({ticket_id for _, ticket_id in wanted})
    histories: dict[tuple[str, str], list[tuple[str, float]]] = {key: [] for key in wanted}
    for row in connection.execute(
        "SELECT repo, ticket_id, action, created_at FROM jobs "
        f"WHERE ticket_id IN ({','.join('?' for _ in ids)}) "
        "ORDER BY created_at, rowid",
        ids,
    ):
        key = (str(row["repo"]).casefold(), str(row["ticket_id"]))
        if key in histories:
            histories[key].append((str(row["action"]), float(row["created_at"])))
    for item, repo, ticket_id in active:
        key = (repo.casefold(), ticket_id)
        tracker, _review_round, state = ticket_info.get(key, (None, None, None))
        item["tracker"] = tracker
        item["flow"] = job_flow(chart, histories[key], state, now)


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
    *,
    unplaced: int = 0,
) -> tuple[str, list[dict], int | None, str]:
    """Optional F2-01 inventory. Absent or invalid degrades to no device rows.

    Also returns the box's name (the `local: true` device, else "box"); the
    `unplaced` running jobs are counted in its held slots (#90).
    """
    if path is None or not path.is_file():
        return "absent", [], None, "box"
    try:
        if path.stat().st_size > MACHINES_MAX_BYTES:
            return "invalid", [], None, "box"
        data = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return "invalid", [], None, "box"
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return "invalid", [], None, "box"
    items = data["devices"]
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in items):
        return "invalid", [], None, "box"
    box_name = next(
        (item["name"] for item in items if item.get("local") is True),
        "box",
    )
    devices: list[dict] = []
    box_max = None
    for item in items:
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
                "held": held.get(item["name"], 0)
                + (unplaced if item["name"] == box_name else 0),
                "enabled": enabled is True,
                "placeholder": _has_placeholder(item),
                "harnesses": sorted(
                    {str(name) for name in names if str(name) in HARNESSES}
                ),
            }
        )
        if item["name"] == box_name:
            box_max = slot_max if box_max is None else box_max
    return "present", devices, box_max, box_name


# --- writing --------------------------------------------------------------------


def write_snapshot(out_dir: Path, snapshot: dict) -> tuple[Path, Path]:
    """Write status.json and every page (PAGES) from one snapshot, as a set.

    The same write as `write_snapshot_pages`; returns just
    (status.json, index.html). Callers that need every written path (the
    push, the CLI) use `write_snapshot_pages`.
    """
    paths = write_snapshot_pages(out_dir, snapshot)
    return paths[0], out_dir / "index.html"


def write_snapshot_pages(out_dir: Path, snapshot: dict) -> tuple[Path, ...]:
    """Write status.json and every page (PAGES) from one snapshot, as a set.

    Each is written to a temp file in the same directory and fsynced, then
    renamed into place, status.json first. If a later rename fails, every
    file already renamed is put back as it was (or removed if there was
    none), so the directory never mixes files from this snapshot with files
    from an earlier one. Returns every path, status.json first, then the
    pages in PAGES order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = {"status.json": json.dumps(snapshot, indent=2, sort_keys=True) + "\n"}
    texts.update(render_pages(snapshot))
    targets = [out_dir / name for name in texts]
    temps: list[str] = []
    try:
        staged = [_write_temp(path, texts[path.name], temps) for path in targets]
        published: list[tuple[Path, str | None]] = []
        try:
            for path, temp in zip(targets, staged):
                previous = None
                if path.is_file():
                    previous = _temp_name(path, temps, suffix=".prev")
                    shutil.copyfile(path, previous)
                os.replace(temp, path)
                published.append((path, previous))
        except BaseException:
            for path, previous in reversed(published):
                if previous is not None:
                    os.replace(previous, path)
                else:
                    path.unlink(missing_ok=True)
            raise
    finally:
        for temp in temps:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
    return tuple(targets)


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


def render_html(snapshot: dict) -> str:
    """The main page (index.html). Every value is escaped and already redacted."""
    return render_pages(snapshot)["index.html"]


__all__ = [
    "PAGES",
    "DashboardError",
    "collect_snapshot",
    "redact",
    "render_html",
    "render_pages",
    "write_snapshot",
    "write_snapshot_pages",
]
