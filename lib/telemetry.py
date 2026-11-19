"""Per-job CLI telemetry: the harness's event stream, summarized (issue #93).

`orch job run` (lib/cli.py, lib/job_run.py) runs the harness with its
stdout on a pipe and hands the pipe to a `Session` here:

* `stream_flags` adds the fixed stream flags to the bot's own command:
  Claude Code (`claude -p ...`) gets `--output-format stream-json --verbose`
  and Codex (`codex exec ...`) gets `--json`. So do the bots' wrappers:
  claude-bot's `claude-harness -p ...` (#264) and codex-bot's
  `<wrapper> exec ...` (#151, right after its `exec`). A command that
  already picks an output format, or that is neither, is run as given.
  Nothing from a job row reaches the argv; the flags are constants. Each
  run logs one line, `telemetry: stream flags added|skipped (<reason>)`.
  A remote run gets the same argv, and its stream comes back to this
  process over the follower, so its summary lands in the box's table.
* Every stdout line is appended to `<state dir>/events.jsonl` (mode 600, in
  a mode-700 directory) on the box that runs `orch job run`: the waiter's
  job directory ($ORCH_JOB_STATE_DIR), else `telemetry-<bot>/<job-id>`
  beside orch.db. The file is
  capped at MAX_LOG_BYTES: a write that would pass the cap first renames it
  to `events.jsonl.1` (replacing an older one), so at most two files are
  kept. The raw log never leaves the machine. Lines that are not JSON
  objects are also copied to this process's stdout (the waiter's run.log),
  as before; JSON events are not, so run.log stays small. One exception
  keeps a remote run's handoff (#87): the marker line that decides
  the CLI's final message (Claude Code's `result`, Codex's last
  `agent_message`) by lib/handoff_marker.py's rule is copied there, so the
  waiter on the box reads the same marker text mode would have given it.
  It is copied at the CLI's end event (`result`,
  `turn.completed`), else when the stream ends, and only once.
* Each JSON event updates a `Summary`: turns, tool calls by type, files
  touched, commands run, the last test run's pass and fail counts, tokens
  in (of which cached) and out (`tokens_unknown` 1 while the CLI has
  reported no usage, so the dashboard shows "no data", not 0, #264), cost
  when the CLI reports it, the last
  activity and the CLI's own exit reason, plus the last RECENT_STEPS steps
  (tool calls and commands with their status, and errors). A step is its
  time, kind, status and a fixed label (`command`, the harness's tool name,
  `edit <n> files`, the error kind) and the exit reason is a fixed label too:
  no command line, tool argument, pattern, path, query or error message is
  ever stored (#100 r10, Max: "Hide those fields in the job modal instead").
  File paths are only counted, but for one exception (#505): `skill_reads`,
  the pinned skill files the run read (`SkillReads`), each relative to the
  placed device's skills tree and kept only when skills/skills.lock lists
  it, so the column holds names from that closed set alone, in counters
  mode too. NULL when the run has no known skills tree or skills.lock
  cannot be read.
* The one exception (#147, Max, 2026-11-13): the live run (`LiveRun`), so
  the agent's task-list text, its current and last finished task, and its
  sub-agents' description and type. Never a prompt, a result, a summary or
  assistant text. `ORCH_TELEMETRY=counters` keeps the r10 behaviour: the
  live run is never read and its four columns stay NULL.
* `Session.flush` writes the summary to the `job_telemetry` table in
  orch.db; `orch job run` calls it right after each job heartbeat (every
  ~15 seconds), and `Session.finish` writes it once more when the harness
  exits, with the exit status and reason. So a job's telemetry row is
  rewritten with each heartbeat and `job_telemetry.updated_at` moves with
  `jobs.last_activity_at` (`last_seen` reads it, for the stale and health
  checks of #91 and #92).
* Subscription usage readings (lib/usage_readings.py, #151): the 5-hour
  and 7-day used fraction and reset time only. Claude's come from the
  `get_usage` probe at the job's start (lib/usage_probe.py, `add_usage`)
  and from each `rate_limit_event` in the stream (`parse_rate_limit_event`);
  a box Codex run's come from its own session rollout, found by the
  `thread.started` thread id and read on each flush, before the row is
  built (lib/codex_rollout.py); a remote one's once when the run ends
  (`add_rollout`). They are written to `usage_readings` in the same
  transaction as the telemetry row, per harness, bot and device, with the
  placed device's usage account.
* Tokens are the harness's own session total, cached input and helper
  agents included (#1015); the first source present wins. Claude: the sum
  over every model in the `result` event's `modelUsage`, else that event's
  `usage`, else the sum of each assistant message's usage until a result
  comes. Codex: the same rollout read's last `total_token_usage`, else the
  sum of each `turn.completed` usage; once a rollout total is in, a later
  turn no longer changes the count.

Secrets: the only free text that goes into the table is the harness's tool
names (as tool call counts and step labels) and the live run's texts (each
with an absolute path and the rest of the text after it cut to `[path]`, and capped at TASK_TEXT_LIMIT). They are
scrubbed first (`Scrubber`): the values of the
harness's environment variables whose names look secret, and always its
ORCH_JOB_PROOF and ORCH_WORKER_TAG, are replaced,
whatever their length and however the text quotes or escapes them (each
value is matched against the text as written and as read after up to three
rounds of shell unquoting (bash's `$'...'` and `$"..."` too) or JSON, Python
and C string-escape decoding, with
quote and backslash characters ignored, so a shell-quoted
`'walrus'"'"'harbor'` and the JSON `"walrus\\u00e9harbor"` go like the plain
value; each line of a multi-line value is matched on its own), keeping
only boolean flag values such as `1` or `true` and the XDG_SESSION_* and
DESKTOP_SESSION descriptors; then
the dashboard's `redact` removes token-shaped strings, auth headers, key
assignments, URLs and email addresses, and only then is the text cut to one
short label; a text longer than SCRUB_LIMIT is hidden whole. A remote
run's tool names come from the worker, so its scrubber also learns the
harness's own environment there (after the login profile): the
start prints it as one report line before the harness (lib/remote.py
WORKER_ENV_MARK), which is never logged or copied. The profile's own stdout
goes to stderr, so the report starts the stream; text before the mark on
its line is kept, the report itself never. Until a whole report is read
the scrubber is blind and every tool name and live run text is stored as
`[redacted-env]`. Assistant text and tool output are never stored; tool
output is only read for test counts and a TaskCreate's new task id. The dashboard (lib/dashboard_telemetry.py) shows
only these summaries and step labels.

Counters are clamped to 0..MAX_COUNT and a cost must be finite, so every
value fits a SQLite INTEGER and standard JSON. A telemetry failure (an
unwritable log, a locked database, a malformed or deeply nested event) is
one log line and never stops the job, its heartbeat or its cleanup.
`ORCH_TELEMETRY=off` turns it off: the command then runs exactly as before.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import posixpath
import re
import shlex
import sqlite3
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import IO, Any, NamedTuple

from lib.codex_sandbox import exec_index
from lib.handoff_marker import marker_line
from lib.job_token import PROOF_ENV
from lib.remote import TAG_ENV, WORKER_ENV_END, WORKER_ENV_MARK
from lib.codex_rollout import THREAD_ID
from lib.codex_rollout import latest as rollout_latest
from lib.codex_rollout import default_home as codex_home
from lib import lane_limit
from lib.job_state import USAGE_LIMIT, record_job_event
from lib.machines_yml import DEFAULT_ORCH_HOST, default_account
from lib.usage_readings import Reading, parse_codex, parse_rate_limit_event, store
from lib.pstack_pin import ROOT as FACTORY_CHECKOUT
from lib.pstack_pin import Pin, load_pin

TABLE = "job_telemetry"
STATE_DIR_ENV = "ORCH_JOB_STATE_DIR"
TELEMETRY_ENV = "ORCH_TELEMETRY"
RAW_LOG_NAME = "events.jsonl"
MAX_LOG_BYTES = 20 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
RECENT_STEPS = 20
MAX_TOOL_TYPES = 40
LABEL_LIMIT = 40
TASK_TEXT_LIMIT = 120
PLAN_ITEMS = 12
SUBAGENTS = 8
MAX_TRACKED = 200
SCRUB_LIMIT = 16 * 1024
HIDDEN = "[redacted-env]"
SCAN_BYTES = 64 * 1024
MAX_COUNT = 2**53 - 1

HARNESS_BY_BOT = {"claude-bot": "claude", "codex-bot": "codex", "cursor-bot": "cursor"}
_OFF = frozenset({"0", "off", "no", "false"})
COUNTERS_ONLY = "counters"

Log = Callable[[str], None]
Connect = Callable[[], sqlite3.Connection]
Guard = Callable[[sqlite3.Connection], None]

COLUMNS = (
    "job_id",
    "bot",
    "harness",
    "repo",
    "started_at",
    "updated_at",
    "last_event_at",
    "finished_at",
    "elapsed_seconds",
    "events",
    "turns",
    "tool_calls",
    "files_touched",
    "commands",
    "test_runs",
    "tests_passed",
    "tests_failed",
    "tokens_in",
    "tokens_cached",
    "tokens_out",
    "cost_usd",
    "exit_code",
    "exit_reason",
    "steps",
    "plan",
    "current_task",
    "last_finished",
    "subagents",
    "tokens_unknown",
    "slot_hours_lost",
    "skill_reads",
)
LIVE_RUN_COLUMNS = ("plan", "current_task", "last_finished", "subagents")
ADDED_COLUMNS = {
    **dict.fromkeys(LIVE_RUN_COLUMNS, "TEXT"),
    "tokens_unknown": "INTEGER",
    "slot_hours_lost": "REAL",
    "skill_reads": "TEXT",
}

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    job_id TEXT PRIMARY KEY,
    bot TEXT NOT NULL,
    harness TEXT NOT NULL,
    repo TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_event_at REAL,
    finished_at REAL,
    elapsed_seconds REAL NOT NULL,
    events INTEGER NOT NULL,
    turns INTEGER NOT NULL,
    tool_calls TEXT NOT NULL,
    files_touched INTEGER NOT NULL,
    commands INTEGER NOT NULL,
    test_runs INTEGER NOT NULL,
    tests_passed INTEGER,
    tests_failed INTEGER,
    tokens_in INTEGER NOT NULL,
    tokens_cached INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost_usd REAL,
    exit_code INTEGER,
    exit_reason TEXT,
    steps TEXT NOT NULL,
    plan TEXT,
    current_task TEXT,
    last_finished TEXT,
    subagents TEXT,
    tokens_unknown INTEGER,
    slot_hours_lost REAL,
    skill_reads TEXT
)
"""
INDEX = f"CREATE INDEX IF NOT EXISTS {TABLE}_updated ON {TABLE} (updated_at)"


def ensure_table(connection: sqlite3.Connection) -> None:
    """Create the table, or add the newer columns (#147, #264) to an older one.

    Two runners can migrate one database at once: the one that loses the
    race gets `duplicate column name`, which means the column is there."""
    connection.execute(DDL)
    connection.execute(INDEX)
    present = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
    for name, kind in ADDED_COLUMNS.items():
        if name in present:
            continue
        try:
            connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {kind}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


def last_seen(connection: sqlite3.Connection, job_id: str) -> float | None:
    """When the job's runner last wrote its telemetry summary, or None.

    None when there is no row or no table (an older database, or a job run
    with telemetry off). For liveness checks (#91, #92): the summary is
    written right after each heartbeat while the harness runs.
    """
    try:
        row = connection.execute(
            f"SELECT updated_at FROM {TABLE} WHERE job_id = ?", (job_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return float(row[0])


# --- the harness argv ---------------------------------------------------------


class StreamFlags(NamedTuple):
    """What `stream_flags` made of a command: the argv to run, the harness it
    names (None when it names none), whether the flags were added, and why
    (one of the fixed STREAM_REASONS)."""

    argv: list[str]
    harness: str | None
    added: bool
    reason: str


STREAM_REASONS = (
    "claude",
    "claude wrapper",
    "codex",
    "codex wrapper",
    "empty command",
    "not print mode",
    "output format already set",
    "json already set",
    "no exec subcommand",
    "wrapper does not pass harness options",
    "not a known harness",
)


def stream_flags(command: Sequence[str], bot_harness: str | None = None) -> StreamFlags:
    """The command with its stream flags added, the harness it names, and why.

    Only constants are added. `claude` with `-p`/`--print` and no
    `--output-format` gets `--output-format stream-json` (and `--verbose`,
    which stream-json needs with --print); `codex ... exec` (or `e`) with no
    `--json` gets `--json` right after the subcommand word, found by skipping
    option values (`codex --profile exec exec` names its subcommand second).
    A bot's wrapper is read the same way when its arguments go on to the CLI
    as they are: for a claude bot, one whose first argument is an option
    (claude-bot's `claude-harness -p ...`, which runs `claude "$@"`, #264);
    for a codex bot, one whose own first word is `exec` (codex-bot's
    `codex-review-harness exec ...`, #151). Anything else is returned
    unchanged.
    """
    argv = list(command)
    if not argv:
        return StreamFlags(argv, None, False, "empty command")
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name == "claude" or bot_harness == "claude":
        wrapper = name != "claude"
        harness = None if wrapper else "claude"
        if wrapper and not (rest and rest[0].startswith("-")):
            return StreamFlags(argv, None, False, "wrapper does not pass harness options")
        if "-p" not in rest and "--print" not in rest:
            return StreamFlags(argv, harness, False, "not print mode")
        if any(part == "--output-format" or part.startswith("--output-format=") for part in rest):
            return StreamFlags(argv, harness, False, "output format already set")
        extra = ["--output-format", "stream-json"]
        if "--verbose" not in rest:
            extra.append("--verbose")
        return StreamFlags(
            [argv[0], *extra, *rest], "claude", True, "claude wrapper" if wrapper else "claude"
        )
    if name == "codex" or bot_harness == "codex":
        harness = "codex" if name == "codex" else None
        index = exec_index(["codex", *rest])
        options = rest[: rest.index("--")] if "--" in rest else rest
        if index is None:
            return StreamFlags(argv, harness, False, "no exec subcommand")
        if "--json" in options or "--experimental-json" in options:
            return StreamFlags(argv, harness, False, "json already set")
        reason = "codex" if name == "codex" else "codex wrapper"
        return StreamFlags([*argv[: index + 1], "--json", *argv[index + 1 :]], "codex", True, reason)
    return StreamFlags(argv, None, False, "not a known harness")


def harness_argv(
    command: Sequence[str], bot_harness: str | None = None
) -> tuple[list[str], str | None]:
    """`stream_flags`' argv and harness."""
    flags = stream_flags(command, bot_harness)
    return flags.argv, flags.harness


def stream_flags_line(flags: StreamFlags) -> str:
    """The one plain line each job logs about its stream flags (#264)."""
    return f"telemetry: stream flags {'added' if flags.added else 'skipped'} ({flags.reason})"


def enabled(environ: Mapping[str, str]) -> bool:
    return environ.get(TELEMETRY_ENV, "").strip().lower() not in _OFF


def text_enabled(environ: Mapping[str, str]) -> bool:
    """False for ORCH_TELEMETRY=counters: counters only, no live run text (#100 r10)."""
    return environ.get(TELEMETRY_ENV, "").strip().lower() != COUNTERS_ONLY


def state_dir(job_id: str, bot: str, database: Path, environ: Mapping[str, str]) -> Path:
    """Where the job's raw log goes: $ORCH_JOB_STATE_DIR (the waiter sets it to
    the job's own directory), else `telemetry-<bot>/<job-id>` beside orch.db
    (both mode 700: one directory per bot user)."""
    explicit = environ.get(STATE_DIR_ENV)
    if explicit:
        return Path(explicit)
    return Path(database).absolute().parent / f"telemetry-{bot}" / job_id


# --- scrubbing ------------------------------------------------------------------

_SECRET_NAME = re.compile(
    r"(?i)(token|secret|passw|pwd|key|auth|credential|cookie|session|webhook|private|signature)"
)
_NOT_SECRET_NAME = re.compile(r"XDG_SESSION_\w+|DESKTOP_SESSION")
# #100 r8: what orch itself hands a run, under names that do not read as
# secret: the run's completion proof (#96) and a remote start's worker tag.
_RUN_SECRETS = frozenset({PROOF_ENV, TAG_ENV})
_FLAG_VALUES = frozenset({"0", "1", "true", "false", "yes", "no", "on", "off"})
_QUOTING = frozenset("'\"\\")
_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "a": "\a", "e": "\x1b", "E": "\x1b",
    "/": "/", "\\": "\\", "'": "'", '"': '"',
}
_HEX_WIDTH = {"x": 2, "u": 4, "U": 8}
_OCTAL = frozenset("01234567")
_HEX = frozenset("0123456789abcdefABCDEF")
_DECODE_ROUNDS = 3

Reading = list[tuple[str, int, int]]


def _escape(reading: Reading, at: int) -> tuple[str, int] | None:
    """The character the escape after a backslash, from reading[at], stands
    for and where it ends; None when it is not a string escape."""
    kind = reading[at][0]
    if kind in _ESCAPES:
        return _ESCAPES[kind], at + 1
    if kind in _OCTAL:
        end = at + 1
        while end < min(at + 3, len(reading)) and reading[end][0] in _OCTAL:
            end += 1
        return chr(int("".join(char for char, _, _ in reading[at:end]), 8)), end
    width = _HEX_WIDTH.get(kind)
    digits = "".join(char for char, _, _ in reading[at + 1 : at + 1 + (width or 0)])
    if width is None or len(digits) < width or not set(digits) <= _HEX:
        return None
    code, end = int(digits, 16), at + 1 + width
    if 0xD800 <= code < 0xDC00 and end + 1 < len(reading) and reading[end][0] == "\\":
        low = _escape(reading, end + 1)
        if low is not None and 0xDC00 <= ord(low[0]) < 0xE000:
            return chr(0x10000 + ((code - 0xD800) << 10) + ord(low[0]) - 0xDC00), low[1]
    return (chr(code), end) if code <= 0x10FFFF else None


def _unescape(reading: Reading) -> Reading:
    """`reading` with JSON, Python and C string escapes (`\\t`, `\\u00e9`,
    surrogate pairs, `\\x41`, octal) decoded."""
    out: Reading = []
    index = 0
    while index < len(reading):
        char, start, _ = reading[index]
        escape = _escape(reading, index + 1) if char == "\\" and index + 1 < len(reading) else None
        if escape is None:
            out.append(reading[index])
            index += 1
        else:
            out.append((escape[0], start, reading[escape[1] - 1][2]))
            index = escape[1]
    return out


def _unshell(reading: Reading) -> Reading:
    """`reading` as the shell reads its words: quotes dropped, single-quoted
    text literal, and backslash escapes read as the character they escape.
    Bash's `$'...'` keeps its escapes for a later `_unescape` round, and
    `$"..."` reads as `"..."`."""
    out: Reading = []
    quote = None
    index = 0
    while index < len(reading):
        char, start, _ = reading[index]
        escaped = index + 1 < len(reading) and (quote is None or reading[index + 1][0] in '"\\$`')
        if quote is None and char == "$" and index + 1 < len(reading) and reading[index + 1][0] in "'\"":
            quote = "$'" if reading[index + 1][0] == "'" else '"'
            index += 2
            continue
        if quote == "$'":
            if char == "\\" and index + 1 < len(reading):
                out += reading[index : index + 2]
                index += 2
                continue
            if char == "'":
                quote = None
            else:
                out.append(reading[index])
        elif quote == "'":
            if char == "'":
                quote = None
            else:
                out.append(reading[index])
        elif char == "\\" and escaped:
            out.append((reading[index + 1][0], start, reading[index + 1][2]))
            index += 1
        elif char == quote:
            quote = None
        elif quote is None and char in "'\"":
            quote = char
        else:
            out.append(reading[index])
        index += 1
    return out


def _unquote(reading: Reading) -> Reading:
    """`reading` without quote characters, so an escape split by quoting (`\\''t`) reads whole."""
    return [item for item in reading if item[0] not in "'\""]


def _readings(text: str) -> list[Reading]:
    """`text` as written, and as read after each mix of up to _DECODE_ROUNDS
    rounds of string-escape decoding, shell unquoting and dropping quotes:
    one (character, start, end) per character read, with the run of `text`
    it came from."""
    written: Reading = [(char, index, index + 1) for index, char in enumerate(text)]
    readings, frontier, seen = [written], [written], {text}
    for _ in range(_DECODE_ROUNDS):
        decoded = []
        for reading in frontier:
            for step in (_unescape, _unshell, _unquote):
                read = step(reading)
                key = "".join(char for char, _, _ in read)
                if key not in seen:
                    seen.add(key)
                    decoded.append(read)
        readings += decoded
        frontier = decoded
    return readings


class Scrubber:
    """One short, secret-free line from any text that may leave the machine.

    A blind scrubber does not yet know the environment the text comes from
    (a remote worker's, until `learn`), so it hides every text whole."""

    def __init__(self, environ: Mapping[str, str], *, blind: bool = False):
        from lib.dashboard import redact  # late: lib.dashboard imports the dashboard side of this

        self._redact = redact
        self._values: list[str] = []
        self.learn(environ)
        self.blind = blind

    def learn(self, environ: Mapping[str, str]) -> None:
        """Also replace the secret values of `environ`, and stop being blind."""
        values = {
            value.strip()
            for name, value in environ.items()
            if value.strip()
            and (
                name in _RUN_SECRETS
                or (
                    value.strip().lower() not in _FLAG_VALUES
                    and _SECRET_NAME.search(name)
                    and not _NOT_SECRET_NAME.fullmatch(name)
                )
            )
        }
        lines = {line.strip() for value in values for line in value.splitlines() if line.strip()}
        bare = {"".join(char for char in value if char not in _QUOTING) for value in values | lines}
        self._values = sorted(bare | set(self._values), key=len, reverse=True)
        self.blind = False

    def __call__(self, value: object, limit: int = LABEL_LIMIT) -> str:
        text = str(value)
        if self.blind or len(text) > SCRUB_LIMIT:
            return HIDDEN
        return _clip(self._redact(" ".join(self._cut(text).split())), limit)

    def _cut(self, text: str) -> str:
        """Replace every run of `text` that reads as a secret, as written or
        decoded (`_readings`) and with quoting ignored, together with the
        quoting around it: `'walrus'"'"'harbor'` and the JSON
        `"walrus\\u00e9harbor"` go as surely as `walrus'harbor`. A secret made
        only of quoting (`\\`) reads as the empty string, so it takes every
        run of quoting with it."""
        if not self._values:
            return text
        hidden = [False] * len(text)
        for reading in _readings(text):
            kept = [(start, end) for char, start, end in reading if char not in _QUOTING]
            bare = "".join(char for char, _, _ in reading if char not in _QUOTING)
            for secret in self._values:
                found = bare.find(secret)
                while found >= 0:
                    after = found + len(secret)
                    first = kept[found - 1][1] if found else 0
                    last = kept[after][0] if after < len(kept) else len(text)
                    hidden[first:last] = [True] * (last - first)
                    found = bare.find(secret, max(after, found + 1))
        return "".join(
            "[redacted-env]" if gone else "".join(char for char, _ in run)
            for gone, run in itertools.groupby(zip(text, hidden), key=lambda pair: pair[1])
        )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def worker_environ(line: bytes) -> dict[str, str] | None:
    """The environment a remote start reported (lib/remote.py WORKER_ENV_MARK),
    or None when `line` is not a whole report."""
    try:
        data = bytes.fromhex(line[len(WORKER_ENV_MARK) :].decode("ascii").strip())
    except ValueError:
        return None
    entries = data.split(b"\0")
    if entries[-2:] != [WORKER_ENV_END.encode(), b""]:
        return None
    pairs = (entry.decode("utf-8", errors="replace").partition("=") for entry in entries[:-2])
    return {name: value for name, _, value in pairs}


# --- the raw log -------------------------------------------------------------------


class EventLog:
    """Append-only raw event log, rotated at max_bytes (one older file kept)."""

    def __init__(self, path: Path, *, max_bytes: int = MAX_LOG_BYTES, log: Log):
        self.path = path
        self.max_bytes = max_bytes
        self.log = log
        self._fd = -1
        self._size = 0
        try:
            path.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            self._fd = self._open()
            self._size = os.fstat(self._fd).st_size
        except OSError as exc:
            self._fail(exc)

    @property
    def rotated(self) -> Path:
        return self.path.with_name(self.path.name + ".1")

    def _open(self) -> int:
        fd = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        os.fchmod(fd, 0o600)
        return fd

    def write(self, data: bytes) -> None:
        if self._fd < 0 or not data:
            return
        try:
            if self._size and self._size + len(data) > self.max_bytes:
                os.close(self._fd)
                self._fd = -1
                os.replace(self.path, self.rotated)
                self._fd = self._open()
                self._size = 0
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]
            self._size += len(data)
        except OSError as exc:
            self._fail(exc)

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1

    def _fail(self, exc: OSError) -> None:
        self.close()
        self.log(f"telemetry: raw event log off ({type(exc).__name__}: {exc.strerror or exc})")


# --- parsing: one summary from either CLI's events ------------------------------------

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_CLAUDE_REASONS = {
    "success": "success",
    "error_max_turns": "max turns reached",
    "error_during_execution": "error during execution",
}
_CODEX_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)
_TEST_COMMAND = re.compile(
    r"(?:^|[\s/;&|('\"])(?:pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit"
    r"|go test|cargo test|(?:npm|yarn|pnpm|bun)(?: run)? test|make (?:test|check))\b"
)
_COUNTS_LINE = re.compile(r"\b\d+ (?:passed|failed)\b")
_PASSED = re.compile(r"\b(\d+) passed\b")
_FAILED = re.compile(r"\b(\d+) (?:failed|errors?)\b")
_CARGO = re.compile(r"test result: \w+\. (\d+) passed; (\d+) failed")


def count_tests(command: str, output: str) -> tuple[int, int] | None:
    """(passed, failed) from a test command's output, or None when not a test
    run or no counts were printed. pytest, jest/vitest and cargo forms."""
    if not _TEST_COMMAND.search(command):
        return None
    text = output[-SCAN_BYTES:]
    cargo = _CARGO.findall(text)
    if cargo:
        return sum(int(p) for p, _ in cargo), sum(int(f) for _, f in cargo)
    for line in reversed(text.splitlines()):
        if _COUNTS_LINE.search(line):
            passed = sum(int(n) for n in _PASSED.findall(line))
            failed = sum(int(n) for n in _FAILED.findall(line))
            return passed, failed
    return None


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return min(MAX_COUNT, max(0, int(value)))


def _maybe_int(value: int | None) -> int | None:
    return None if value is None else _int(value)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _command_text(command: object) -> str:
    if isinstance(command, list):
        return shlex.join(str(part) for part in command)
    return str(command or "")


# --- the live run: what the agent says it is doing (#147) ---------------------------

# Claude Code's sub-agent tool; it was `Task` before 2.1, when it ran in the foreground.
_SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
_TASK_EVENTS = frozenset({"task_started", "task_progress", "task_notification"})
_TASK_CREATED = re.compile(r"\bTask #([\w-]{1,40}) created successfully")
_PLAN_STATUS = {"pending": "pending", "in_progress": "in_progress", "completed": "done"}
_SUBAGENT_END = {"completed": "finished", "failed": "failed", "stopped": "failed"}
_RELATIVE_PATH = re.compile(r"""[(\['"`]*(?!-)[\w.-]+(?:[/\\][\w.-]+)*[/\\]?[.,;:!?)\]'"`]*""")


def _is_path(token: str) -> bool:
    return bool(re.search(r"[/\\]", token)) and bool(token.strip("/\\")) and not _RELATIVE_PATH.fullmatch(token)


def _cut_paths(text: str) -> str:
    """Every word with a slash is a path unless it reads as a plain relative one
    (`lib/x.py`, `../y`, `and/or`) or is only slashes (`a / b`). A shell word
    joined by escaped spaces (`/\\ my\\ dir/x`) is judged as it unescapes. Where
    a path ends cannot be told from text (quotes, spaces, escapes), so
    everything from the first such word to the end goes."""
    for shell_word in re.finditer(r"(?:\\[\s\S]?|[^\s\\])+", text):
        if _is_path(re.sub(r"\\([\s\S])", r"\1", shell_word.group())):
            return text[: shell_word.start()] + "[path]"
        for word in re.finditer(r"\S+", shell_word.group()):
            if _is_path(word.group()):
                return text[: shell_word.start() + word.start()] + "[path]"
    return text


def _str(value: object) -> str | None:
    """A non-blank string as given, kept just long enough for the scrubber to
    hide an overlong one whole; None for anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value[: SCRUB_LIMIT + 1]


def _drop_oldest(mapping: dict, limit: int) -> None:
    while len(mapping) > limit:
        del mapping[next(iter(mapping))]


class LiveRun:
    """The agent's plan, current and last finished task, and sub-agents.

    Only these texts are ever read from the stream: a task-list item's text
    and active form, and a sub-agent's description and type. Never a prompt,
    a result, a summary or a TaskCreate description. They stay in memory as
    given; `row` scrubs each one, then cuts every path and what follows it to `[path]`."""

    def __init__(self) -> None:
        self.plan: list[dict] = []
        self.last_done: str | None = None
        self.subagents: dict[str, dict] = {}  # by the tool_use id that started each
        self._tasks: dict[str, str] = {}  # a stream task id -> the sub-agent's tool_use id
        self._creating: dict[str, dict] = {}  # a TaskCreate call -> its item, until its id comes back

    # -- Claude Code --

    def tool_use(self, name: str, call_id: str, data: dict, now: float) -> None:
        if name == "TodoWrite":
            self._todo_write(data.get("todos"))
        elif name == "TaskCreate":
            self._task_create(call_id, data)
        elif name == "TaskUpdate":
            self._task_update(data)
        elif name in _SUBAGENT_TOOLS:
            self._subagent(call_id, name, data, now)

    def tool_result(self, call_id: str, error: bool, output: str, now: float) -> None:
        item = self._creating.pop(call_id, None)
        if item is not None:
            found = None if error else _TASK_CREATED.search(output)
            if found is not None:
                item["id"] = found.group(1)
            elif error:
                self._drop(item)
            return
        agent = self.subagents.get(call_id)
        if agent is None:
            return
        if error:
            self._end(agent, "failed", now)
        elif not agent["background"]:  # a background call's result only says it launched
            self._end(agent, "finished", now)

    def task_event(self, subtype: str, event: dict, now: float) -> None:
        """A `system` task event, for a sub-agent this run started. Tasks that
        are not sub-agents (background shells, monitors) are not in the map."""
        call_id = event.get("tool_use_id")
        task_id = event.get("task_id")
        task_id = task_id if isinstance(task_id, str) else None
        if not isinstance(call_id, str) or call_id not in self.subagents:
            call_id = self._tasks.get(task_id) if task_id else None
        agent = self.subagents.get(call_id) if call_id else None
        if agent is None:
            return
        if subtype == "task_started":
            if task_id:
                self._tasks[task_id] = call_id
                _drop_oldest(self._tasks, MAX_TRACKED)
            backgrounded = event.get("is_backgrounded")
            if isinstance(backgrounded, bool):
                agent["background"] = backgrounded
            self._resume(agent)
        elif subtype == "task_progress":
            if agent["background"]:
                self._resume(agent)
        else:
            status = event.get("status")
            end = _SUBAGENT_END.get(status) if isinstance(status, str) else None
            if end is not None:
                self._end(agent, end, now)

    def _todo_write(self, todos: object) -> None:
        if not isinstance(todos, list):
            return
        items = []
        for todo in todos[:MAX_TRACKED]:
            if not isinstance(todo, dict):
                continue
            text = _str(todo.get("content"))
            status = todo.get("status")
            status = _PLAN_STATUS.get(status) if isinstance(status, str) else None
            if text is not None and status is not None:
                items.append({"text": text, "active": _str(todo.get("activeForm")), "status": status})
        self._replace(items)

    def _task_create(self, call_id: str, data: dict) -> None:
        subject = _str(data.get("subject"))
        if subject is None or len(self.plan) >= MAX_TRACKED:
            return
        item = {"text": subject, "active": _str(data.get("activeForm")), "status": "pending", "id": None}
        self.plan.append(item)
        self._creating[call_id] = item
        _drop_oldest(self._creating, MAX_TRACKED)

    def _task_update(self, data: dict) -> None:
        ident = data.get("taskId")
        if isinstance(ident, bool) or not isinstance(ident, (str, int)):
            return
        item = next((item for item in self.plan if item.get("id") == str(ident)), None)
        if item is None:
            return
        status = data.get("status")
        if status == "deleted":
            self._drop(item)
            return
        item["text"] = _str(data.get("subject")) or item["text"]
        item["active"] = _str(data.get("activeForm")) or item["active"]
        mapped = _PLAN_STATUS.get(status) if isinstance(status, str) else None
        if mapped is not None:
            if mapped == "done" and item["status"] != "done":
                self.last_done = item["text"]
            item["status"] = mapped

    def _drop(self, item: dict) -> None:
        self.plan = [kept for kept in self.plan if kept is not item]

    def _subagent(self, call_id: str, name: str, data: dict, now: float) -> None:
        background = data.get("run_in_background")
        if not isinstance(background, bool):
            background = name == "Agent"  # Claude Code 2.1 runs sub-agents in the background by default
        self.subagents[call_id] = {
            "kind": _str(data.get("subagent_type")) or "general-purpose",
            "description": _str(data.get("description")) or "",
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "background": background,
        }
        if len(self.subagents) > MAX_TRACKED:
            done = next((key for key, agent in self.subagents.items() if agent["status"] != "running"), None)
            del self.subagents[done if done is not None else next(iter(self.subagents))]

    @staticmethod
    def _end(agent: dict, status: str, now: float) -> None:
        agent["status"] = status
        agent["finished_at"] = now

    @staticmethod
    def _resume(agent: dict) -> None:
        agent["status"] = "running"
        agent["finished_at"] = None

    # -- Codex --

    def codex_todo(self, entries: object) -> None:
        """A `todo_list` item's whole list. Codex has no in-progress state:
        the first item not completed is the one in progress."""
        if not isinstance(entries, list):
            return
        items, started = [], False
        for entry in entries[:MAX_TRACKED]:
            text = _str(entry.get("text")) if isinstance(entry, dict) else None
            if text is None:
                continue
            done = entry.get("completed") is True
            status = "done" if done else "pending" if started else "in_progress"
            started = started or not done
            items.append({"text": text, "active": None, "status": status})
        self._replace(items)

    def _replace(self, items: list[dict]) -> None:
        """A new whole list; its last item newly done is the last finished task.
        Items with the same text are told apart by how many of them are done."""
        before = Counter(item["text"] for item in self.plan if item["status"] == "done")
        seen: Counter[str] = Counter()
        for item in items:
            if item["status"] == "done":
                seen[item["text"]] += 1
                if seen[item["text"]] > before[item["text"]]:
                    self.last_done = item["text"]
        self.plan = items

    # -- out --

    def row(self, scrub: Callable[..., str]) -> dict[str, Any]:
        def text(value: str | None, limit: int = TASK_TEXT_LIMIT) -> str:
            return _cut_paths(scrub(value, limit)) if value else ""

        current = next((item for item in self.plan if item["status"] == "in_progress"), None)
        running = [agent for agent in self.subagents.values() if agent["status"] == "running"]
        if current is not None:
            current_task = current["active"] or current["text"]
        else:
            current_task = running[-1]["description"] if running else None
        return {
            "plan": json.dumps([{"text": text(item["text"]), "status": item["status"]} for item in self._window()]),
            "current_task": text(current_task),
            "last_finished": text(self.last_done),
            "subagents": json.dumps(
                [
                    {
                        "kind": text(agent["kind"], LABEL_LIMIT),
                        "description": text(agent["description"]),
                        "status": agent["status"],
                        "started_at": agent["started_at"],
                        "finished_at": agent["finished_at"],
                    }
                    for agent in self._kept_subagents()
                ]
            ),
        }

    def _window(self) -> list[dict]:
        """PLAN_ITEMS items: from up to two before the one in progress, else the first."""
        at = next((index for index, item in enumerate(self.plan) if item["status"] == "in_progress"), 0)
        start = max(0, min(at - 2, len(self.plan) - PLAN_ITEMS))
        return self.plan[start : start + PLAN_ITEMS]

    def _kept_subagents(self) -> list[dict]:
        """Up to SUBAGENTS, in the order they started: the newest running ones,
        then the most recently finished."""
        agents = list(self.subagents.values())
        kept = [agent for agent in agents if agent["status"] == "running"][-SUBAGENTS:]
        ended = sorted((agent for agent in agents if agent["status"] != "running"), key=lambda agent: agent["finished_at"])
        room = SUBAGENTS - len(kept)
        chosen = {id(agent) for agent in kept + (ended[len(ended) - room :] if room else [])}
        return [agent for agent in agents if id(agent) in chosen]


_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_VALUED_OPTIONS = {
    "cat": frozenset(),
    "more": frozenset(),
    "head": frozenset({"-n", "-c", "--lines", "--bytes"}),
    "tail": frozenset({"-n", "-c", "-s", "--lines", "--bytes", "--pid", "--sleep-interval", "--max-unchanged-stats"}),
    "nl": frozenset({"-b", "-d", "-f", "-h", "-i", "-l", "-n", "-s", "-v", "-w"}),
    "less": frozenset({"-b", "-h", "-j", "-k", "-o", "-O", "-p", "-P", "-t", "-T", "-x", "-y", "-z"}),
    "bat": frozenset({"-l", "-r", "-H", "-m", "--language", "--line-range", "--highlight-line", "--style", "--theme", "--map-syntax"}),
    "sed": frozenset({"-e", "-f", "-l", "--expression", "--file", "--line-length"}),
    "awk": frozenset({"-f", "-v", "-F", "--file", "--assign", "--field-separator"}),
}
_PROGRAM_OPTIONS = {
    "sed": frozenset({"-e", "-f", "--expression", "--file"}),
    "awk": frozenset({"-f", "--file"}),
}
_NUMBER_OPTIONS = {"sed": frozenset({"-l"})}
_SHELL_WORDS = frozenset({"if", "then", "elif", "else", "while", "until", "do", "{", "}", "!"})
_OPERATORS = ";&|()<>\n"
_NOT_A_PATH = re.compile(r"[$*?\[`~]")
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")


def _simple_commands(script: str) -> list[tuple[str, list[str]]]:
    lexer = shlex.shlex(script, posix=True, punctuation_chars=_OPERATORS)
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, ""
    commands: list[tuple[str, list[str]]] = [("always", [])]
    redirect = False
    for token in lexer:
        if redirect:
            redirect = False
        elif token and not token.strip(_OPERATORS):
            redirect = "<" in token or ">" in token
            if not redirect:
                operator = token.strip("\n")
                commands.append((operator if operator in ("&&", "||") else "always", []))
        else:
            commands[-1][1].append(token)
    return [
        (joined, list(itertools.takewhile(lambda word: not word.startswith("#"), words)))
        for joined, words in commands
    ]


def _read_operands(verb: str, args: list[str]) -> list[str]:
    valued, program = _VALUED_OPTIONS[verb], _PROGRAM_OPTIONS.get(verb, frozenset())
    numbered = _NUMBER_OPTIONS.get(verb, frozenset())
    operands: list[str] = []
    programmed = False
    at_word = 0
    while at_word < len(args):
        arg, at_word = args[at_word], at_word + 1
        if arg == "--":
            operands.extend(args[at_word:])
            break
        elif not arg.startswith("-"):
            operands.append(arg)
        else:
            if arg.startswith("--"):
                name, attached, _ = arg.partition("=")
                options, takes_next = [name], not attached and name in valued
            else:
                options = [f"-{letter}" for letter in arg[1:]]
                at = next((index for index, option in enumerate(options) if option in valued), len(options))
                options, takes_next = options[: at + 1], at == len(options) - 1
            programmed |= any(option in program for option in options)
            value = "".join(args[at_word : at_word + 1])
            if takes_next and (options[-1] not in numbered or value.isdigit()):
                at_word += 1
    return operands[1:] if program and not programmed else operands


def _error_lines(output: str) -> list[tuple[str, str]]:
    """Each output line as a program name and its message, for lines shaped
    like `cat: <path>: No such file` or `[bat error]: '<path>' ...`."""
    lines = []
    for line in output.splitlines():
        name, colon, message = line.partition(":")
        if colon:
            lines.append((posixpath.basename(name.strip().strip("[]").removesuffix(" error")), message))
    return lines


class _Step(NamedTuple):
    joined: str
    reads: list[tuple[str, str]]
    found: dict[str, str | None]
    inner: list[_Step]


def _found(steps: list[_Step]) -> set[str]:
    return {rel for step in steps for rel in [*step.found, *_found(step.inner)]}


class SkillReads:
    """The skills-lock files under one skills tree that a run read, by their path in the tree.

    Fed the harness's own tool calls: Claude's Read and Skill, and the file
    operands of read commands (cat, sed, head ...) in Claude's Bash and
    Codex's shell. A `test -f`, `ls` or `grep` is not a read. A call's files
    count only once its result arrives. A failed Read or refused Skill loaded
    nothing. A failed shell call loses the operands its output names, as
    `cat: <path>: No such file` does, and all of them when it printed
    nothing. In a failed call, a read failed when an output line from its
    program names one of its file operands and succeeded otherwise, a
    `bash -c` ends as its own chain ended, and any other command's outcome is
    unknown. A read behind a failed `&&`, or behind a `||` whose left side
    succeeded, did not run."""

    def __init__(self, root: str, locked: frozenset[str]):
        self.root = posixpath.normpath(root)
        self.locked = locked
        self.skills = {
            posixpath.basename(posixpath.dirname(rel)): rel
            for rel in sorted(locked)
            if posixpath.basename(rel) == "SKILL.md"
        }
        self.read: set[str] = set()
        self.pending: dict[str, list[_Step]] = {}

    @classmethod
    def pinned(cls, root: str, pin: Pin) -> SkillReads:
        trees = (("pstack", pin.files), ("factory", pin.factory), ("cursor-team-kit", pin.kit))
        return cls(root, frozenset(f"{tree}/{rel}" for tree, rows in trees for rel, _ in rows))

    def claude_tool(self, call_id: str, name: str, data: dict) -> None:
        found: dict[str, str | None] = {}
        if name == "Read":
            self._path(found, _str(data.get("file_path")) or "", None)
            found = dict.fromkeys(found)
        elif name == "Skill":
            rel = self.skills.get((_str(data.get("skill")) or "").rpartition(":")[2])
            if rel is not None:
                found[rel] = None
        elif name == "Bash":
            self.command(call_id, _command_text(data.get("command")))
            return
        self._started(call_id, [_Step("always", [], found, [])])

    def command(self, call_id: str, text: str) -> None:
        try:
            steps = self._script(text, None)
        except ValueError:
            return
        self._started(call_id, steps)

    def finished(self, call_id: str, error: bool, output: str) -> None:
        steps = self.pending.pop(call_id, [])
        if not error:
            self.read |= _found(steps)
        elif output:
            self._chain(steps, _error_lines(output), output)

    def row(self) -> str:
        return json.dumps(sorted(self.read))

    def _chain(self, steps: list[_Step], errors: list[tuple[str, str]], output: str) -> bool | None:
        ok: bool | None = None
        for step in steps:
            if (step.joined == "&&" and ok is False) or (step.joined == "||" and ok is True):
                continue
            if step.inner:
                ok = self._chain(step.inner, errors, output)
            elif step.reads:
                ok = not any(
                    program == verb and operand in message
                    for verb, operand in step.reads
                    for program, message in errors
                )
            else:
                ok = None
            self.read |= {rel for rel, operand in step.found.items() if operand is not None and operand not in output}
        return ok

    def _started(self, call_id: str, steps: list[_Step]) -> None:
        if _found(steps):
            self.pending[call_id] = steps

    def _path(self, found: dict[str, str | None], operand: str, base: str | None) -> None:
        if _NOT_A_PATH.search(operand) or not (base or posixpath.isabs(operand)):
            return
        path = posixpath.normpath(posixpath.join(base or "", operand))
        rel = path[len(self.root) + 1 :]
        if path.startswith(self.root + "/") and rel in self.locked:
            found[rel] = operand

    def _script(self, script: str, base: str | None) -> list[_Step]:
        steps = []
        for joined, words in _simple_commands(script):
            words = list(itertools.dropwhile(lambda word: word in _SHELL_WORDS, words))
            if words:
                steps.append(_Step(joined, [], {}, []))
                base = self._cwd_after(steps[-1], words, base)
        return steps

    def _cwd_after(self, step: _Step, words: list[str], base: str | None) -> str | None:
        words = list(itertools.dropwhile(lambda word: word in _SHELL_WORDS or _ASSIGNMENT.match(word), words))
        if not words:
            return base
        verb, args = posixpath.basename(words[0]), words[1:]
        if verb == "cd":
            target = args[0] if args else "~"
            if _NOT_A_PATH.search(target) or not (base or posixpath.isabs(target)):
                return None
            return posixpath.normpath(posixpath.join(base or "", target))
        if verb in _SHELLS and len(args) > 1 and args[0].startswith("-") and "c" in args[0]:
            step.inner.extend(self._script(args[1], base))
        elif verb in _VALUED_OPTIONS:
            for operand in _read_operands(verb, args):
                step.reads.append((verb, operand))
                self._path(step.found, operand, base)
        return base


def skill_reads(root: str | None, log: Log) -> SkillReads | None:
    if root is None:
        return None
    try:
        return SkillReads.pinned(root, load_pin(FACTORY_CHECKOUT))
    except (OSError, ValueError, UnicodeError) as exc:
        log(f"telemetry: skill reads not recorded (skills.lock: {type(exc).__name__})")
        return None


class Summary:
    """Everything the dashboard shows of one run, from its events.

    text=False (ORCH_TELEMETRY=counters) keeps the counters only: no live
    run text is read into memory or stored, and its columns stay NULL."""

    def __init__(
        self, harness: str, scrub: Callable[..., str], *, text: bool = True, skills: SkillReads | None = None
    ):
        self.harness = harness
        self.scrub = scrub
        self.live = LiveRun() if text else None
        self.skills = skills
        self.events = 0
        self.turns = 0
        self.tool_calls: Counter[str] = Counter()
        self._files: set[str] = set()  # for the count only; never stored
        self.files_touched = 0
        self.commands = 0
        self.test_runs = 0
        self.tests_passed: int | None = None
        self.tests_failed: int | None = None
        self.tokens_in = 0
        self.tokens_cached = 0
        self.tokens_out = 0
        self.usage_seen = False
        self.cost_usd: float | None = None
        self.last_event_at: float | None = None
        self.cli_reason: str | None = None
        self.steps: deque[dict] = deque(maxlen=RECENT_STEPS)
        self._open: dict[str, tuple[dict, str | None]] = {}
        self._seen_items: set[str] = set()
        self._messages: dict[str, tuple[int, int, int]] = {}
        self._totals = False
        self.thread_id: str | None = None

    # -- shared --

    def feed(self, event: dict, now: float) -> None:
        self.events += 1
        self.last_event_at = now
        kind = event.get("type")
        if kind in ("system", "assistant", "user", "result"):
            self._claude(kind, event, now)
        elif kind in _CODEX_EVENTS:
            self._codex(kind, event, now)

    def _tool(self, name: str) -> str:
        """Count a call of the harness's tool `name`; its label for the step."""
        key = self.scrub(name) or "unknown"
        if key not in self.tool_calls and len(self.tool_calls) >= MAX_TOOL_TYPES:
            key = "other"
        self.tool_calls[key] += 1
        return key

    def _step(self, now: float, kind: str, label: str, status: str) -> dict:
        """A step is its kind, status and a fixed label: never text from its arguments."""
        step = {"at": now, "kind": kind, "status": status, "label": label}
        self.steps.append(step)
        return step

    def _touch(self, path: object) -> bool:
        if not isinstance(path, str) or not path:
            return False
        if path not in self._files:
            self._files.add(path)
            self.files_touched += 1
        return True

    def _finished(self, call_id: str, error: bool, output: str) -> None:
        if self.skills is not None:
            self.skills.finished(call_id, error, output)
        entry = self._open.pop(call_id, None)
        if entry is None:
            return
        step, command = entry
        step["status"] = "error" if error else "ok"
        if command is not None:
            self._tests(command, output)

    def _tests(self, command: str, output: str) -> None:
        if not _TEST_COMMAND.search(command):
            return
        self.test_runs += 1
        counts = count_tests(command, output)
        if counts is not None:
            self.tests_passed, self.tests_failed = counts

    def _error(self, now: float, reason: str) -> None:
        self._step(now, "error", reason, "error")
        self.cli_reason = reason

    # -- Claude Code: --output-format stream-json --

    def _claude(self, kind: str, event: dict, now: float) -> None:
        message = event.get("message")
        if kind == "result":
            self._claude_result(event, now)
            return
        if kind == "system":
            subtype = event.get("subtype")
            if self.live is not None and isinstance(subtype, str) and subtype in _TASK_EVENTS:
                self.live.task_event(subtype, event, now)
            return
        if not isinstance(message, dict):
            return
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        if kind == "assistant":
            ident = str(message.get("id") or f"anon-{self.events}")
            if ident not in self._messages and not self._totals:
                self.turns += 1
            usage = message.get("usage")
            if isinstance(usage, dict) and not self._totals:
                self.usage_seen = True
                self._messages[ident] = self._claude_usage(usage)
                self.tokens_in = sum(item[0] for item in self._messages.values())
                self.tokens_cached = sum(item[1] for item in self._messages.values())
                self.tokens_out = sum(item[2] for item in self._messages.values())
            else:
                self._messages.setdefault(ident, (0, 0, 0))
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._claude_tool(block, now)
            return
        for block in blocks:  # user: tool results
            if isinstance(block, dict) and block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id"))
                error = bool(block.get("is_error"))
                output = _text_of(block.get("content"))
                self._finished(call_id, error, output)
                if self.live is not None:
                    self.live.tool_result(call_id, error, output, now)

    @staticmethod
    def _claude_usage(usage: dict) -> tuple[int, int, int]:
        cached = _int(usage.get("cache_read_input_tokens"))
        total_in = (
            _int(usage.get("input_tokens"))
            + _int(usage.get("cache_creation_input_tokens"))
            + cached
        )
        return total_in, cached, _int(usage.get("output_tokens"))

    def _claude_tool(self, block: dict, now: float) -> None:
        name = str(block.get("name") or "unknown")
        call_id = str(block.get("id") or f"call-{self.events}")
        data = block.get("input") if isinstance(block.get("input"), dict) else {}
        label = self._tool(name)
        command: str | None = None
        if name == "Bash":
            command = _command_text(data.get("command"))
            self.commands += 1
            step = self._step(now, "command", "command", "running")
        else:
            if name in _EDIT_TOOLS:
                self._touch(data.get("file_path") or data.get("notebook_path"))
            step = self._step(now, "tool", label, "running")
        self._open[call_id] = (step, command)
        if self.skills is not None:
            self.skills.claude_tool(call_id, name, data)
        if self.live is not None:
            self.live.tool_use(name, call_id, data, now)

    @staticmethod
    def _claude_models(models: object) -> tuple[int, int, int] | None:
        """The session total in a result's `modelUsage`: every model, helper agents included."""
        if not isinstance(models, dict):
            return None
        totals = [usage for usage in models.values() if isinstance(usage, dict)]
        if not totals:
            return None
        cached = sum(_int(usage.get("cacheReadInputTokens")) for usage in totals)
        total_in = cached + sum(
            _int(usage.get("inputTokens")) + _int(usage.get("cacheCreationInputTokens")) for usage in totals
        )
        return total_in, cached, sum(_int(usage.get("outputTokens")) for usage in totals)

    def _claude_result(self, event: dict, now: float) -> None:
        self._totals = True
        if "num_turns" in event:
            self.turns = _int(event.get("num_turns"))
        usage = event.get("usage")
        totals = self._claude_models(event.get("modelUsage"))
        if totals is None and isinstance(usage, dict):
            totals = self._claude_usage(usage)
        if totals is not None:
            self.usage_seen = True
            self.tokens_in, self.tokens_cached, self.tokens_out = totals
        cost = event.get("total_cost_usd", event.get("cost_usd"))
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost = float(cost)
            if math.isfinite(cost) and cost >= 0:
                self.cost_usd = cost
        subtype = event.get("subtype")
        reason = _CLAUDE_REASONS.get(subtype) if isinstance(subtype, str) else None
        if reason is None:
            reason = "error" if isinstance(subtype, str) and subtype.startswith("error") else "finished"
        if event.get("is_error") and reason == "success":
            reason = "error"
        self.cli_reason = reason
        if reason != "success":
            self._step(now, "error", reason, "error")

    # -- Codex: codex exec --json --

    def _codex(self, kind: str, event: dict, now: float) -> None:
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and THREAD_ID.fullmatch(thread_id):
                self.thread_id = thread_id
        elif kind == "turn.started":
            self.turns += 1
        elif kind == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict) and not self._totals:
                self.usage_seen = True
                self.tokens_in += _int(usage.get("input_tokens"))
                self.tokens_cached += _int(usage.get("cached_input_tokens"))
                self.tokens_out += _int(usage.get("output_tokens"))
            self.cli_reason = "completed"
        elif kind == "turn.failed":
            self._error(now, "turn failed")
        elif kind == "error":
            self._error(now, "error")
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if isinstance(item, dict):
                self._codex_item(item, now, done=kind == "item.completed")

    def codex_total(self, tokens: object) -> None:
        """Codex's own session total from its rollout (#1015); later turn sums no longer change it."""
        if not isinstance(tokens, dict):
            return
        self._totals = True
        self.usage_seen = True
        self.tokens_in = _int(tokens.get("input_tokens"))
        self.tokens_cached = _int(tokens.get("cached_input_tokens"))
        self.tokens_out = _int(tokens.get("output_tokens"))

    def _codex_item(self, item: dict, now: float, *, done: bool) -> None:
        item_type = item.get("type")
        if item_type == "todo_list" and self.live is not None:
            self.live.codex_todo(item.get("items"))
        ident = str(item.get("id") or f"item-{self.events}")
        if ident not in self._seen_items:
            self._seen_items.add(ident)
            self._codex_start(ident, item_type, item, now)
        if not done or ident not in self._open:
            return
        status = item.get("status")
        if item_type == "command_execution":
            code = item.get("exit_code")
            error = status == "failed" or (isinstance(code, int) and code != 0)
            self._finished(ident, error, str(item.get("aggregated_output") or ""))
        else:
            self._finished(ident, status == "failed" or bool(item.get("error")), "")

    def _codex_start(self, ident: str, item_type: object, item: dict, now: float) -> None:
        command: str | None = None
        if item_type == "command_execution":
            self._tool("shell")
            command = _command_text(item.get("command"))
            self.commands += 1
            if self.skills is not None:
                self.skills.command(ident, command)
            step = self._step(now, "command", "command", "running")
        elif item_type == "file_change":
            self._tool("file_change")
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            count = sum(self._touch(change.get("path")) for change in changes if isinstance(change, dict))
            step = self._step(now, "tool", f"edit {count} file{'' if count == 1 else 's'}", "running")
        elif item_type == "mcp_tool_call":
            label = self._tool(f"mcp:{item.get('server')}.{item.get('tool')}")
            step = self._step(now, "tool", label, "running")
        elif item_type == "web_search":
            step = self._step(now, "tool", self._tool("web_search"), "running")
        elif item_type == "error":
            self._step(now, "error", "error", "error")
            return
        else:
            return  # agent_message, reasoning, todo_list: not steps
        self._open[ident] = (step, command)

    # -- out --

    def row(self) -> dict[str, Any]:
        return {
            "events": _int(self.events),
            "turns": _int(self.turns),
            "tool_calls": json.dumps(dict(self.tool_calls.most_common()), sort_keys=False),
            "files_touched": _int(self.files_touched),
            "commands": _int(self.commands),
            "test_runs": _int(self.test_runs),
            "tests_passed": _maybe_int(self.tests_passed),
            "tests_failed": _maybe_int(self.tests_failed),
            "tokens_in": _int(self.tokens_in),
            "tokens_cached": _int(self.tokens_cached),
            "tokens_out": _int(self.tokens_out),
            "tokens_unknown": 0 if self.usage_seen else 1,
            "cost_usd": self.cost_usd,
            "last_event_at": self.last_event_at,
            "steps": json.dumps(list(self.steps)),
            "skill_reads": None if self.skills is None else self.skills.row(),
            **(dict.fromkeys(LIVE_RUN_COLUMNS) if self.live is None else self.live.row(self.scrub)),
        }


def exit_reason(code: int | None, cli_reason: str | None) -> str:
    """The run's end: what the CLI reported, then how the process ended."""
    if code is None:
        status = "runner error"
    elif code == 0:
        status = "exit 0"
    elif code == 127:
        status = "could not start (exit 127)"
    elif 128 < code < 160:
        status = f"killed by signal {code - 128}"
    else:
        status = f"exit {code}"
    return f"{cli_reason}; {status}" if cli_reason else status


# The CLI's own end of a run: the line goes out then (and at the end of the
# stream otherwise), so a process left holding the pipe cannot keep it back.
_END_EVENTS = frozenset({"result", "turn.completed"})


def final_message(event: dict) -> str | None:
    """The CLI's final answer text in this event, or None.

    Claude Code's `result` event carries what `claude -p` prints in text
    mode; Codex's completed `agent_message` items are its messages, the last
    of which is what `codex exec` prints in text mode.
    """
    kind = event.get("type")
    if kind == "result":
        text = event.get("result")
        return text if isinstance(text, str) else None
    if kind == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            return text if isinstance(text, str) else None
    return None


# --- the session: one run's tee, summary and table row ------------------------------


class Session:
    def __init__(
        self,
        *,
        connect: Connect,
        job_id: str,
        bot: str,
        repo: str,
        harness: str,
        flags: StreamFlags,
        raw_log: EventLog | None,
        scrub: Scrubber,
        log: Log,
        stdout: IO[bytes] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        worker_env: bool = False,
        device: str = DEFAULT_ORCH_HOST,
        account: str | None = None,
        rollout_home: str | None = None,
        text: bool = True,
        guard: Guard | None = None,
        skills: SkillReads | None = None,
    ):
        self.connect = connect
        self.guard = guard
        self.device = device
        self.account = account
        self.rollout_home = rollout_home  # a box Codex run's CODEX_HOME (#151)
        self.usage: dict[str, Reading] = {}
        self.job_id = job_id
        self.bot = bot
        self.repo = repo
        self.harness = harness
        self.flags = flags
        self.raw_log = raw_log
        self.scrub = scrub
        self.log = log
        self.stdout = stdout
        self.clock = clock
        self.monotonic = monotonic
        self.summary = Summary(harness, scrub, text=text, skills=skills)
        self.started_at = clock()
        self._started = monotonic()
        self._lock = threading.Lock()
        self._finished = False
        self._slot_hours_lost: float | None = None
        self._warned: set[str] = set()
        self._final: str | None = None  # the run's last final message (#87 handoff line)
        self._final_sent = True
        self._worker_env = worker_env  # a remote start's environment report is still to come
        self._limit: lane_limit.UsageLimit | None = None
        self._limit_recorded = False

    # Runs on the reader thread until the command's stdout closes.
    def consume(self, stream: IO[bytes]) -> None:
        try:
            self._consume(stream)
        finally:
            self._send_handoff()  # a stream that ended without the CLI's end event

    def _consume(self, stream: IO[bytes]) -> None:
        skipping = False
        event_line = False
        dropping = False
        while True:
            try:
                chunk = stream.readline(MAX_LINE_BYTES)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            complete = chunk.endswith(b"\n")
            if dropping:  # the rest of an oversized environment report: never kept
                dropping = not complete
                continue
            at = chunk.find(WORKER_ENV_MARK.encode() + b" ") if self._worker_env else -1
            if at >= 0:  # text before the mark on its line is kept; the report never is
                if self.raw_log is not None:
                    self.raw_log.write(chunk[:at])
                self._passthrough(chunk[:at])
                self._worker_env = False
                self._learn(chunk[at:] if complete else b"")
                dropping = not complete
                continue
            if self.raw_log is not None:
                self.raw_log.write(chunk)
            if skipping:
                if not event_line:
                    self._passthrough(chunk)
                skipping = not complete
                continue
            if not complete and len(chunk) >= MAX_LINE_BYTES:
                event_line = chunk.lstrip().startswith(b"{")
                if not event_line:
                    self._passthrough(chunk)
                skipping = True
                continue
            try:
                event = json.loads(chunk)
            except (ValueError, RecursionError):
                event = None
            if not isinstance(event, dict):
                self._passthrough(chunk)
                continue
            with self._lock:
                try:
                    self.summary.feed(event, self.clock())
                except Exception as exc:  # a malformed event never stops the tee
                    self._warn("parse", f"telemetry: skipped an event ({type(exc).__name__})")
            if self._limit is None:
                self._limit = lane_limit.detect(self.harness, event, self.clock())
                if self._limit is not None:
                    self.flush()
            if self.harness == "claude":
                self.add_usage(parse_rate_limit_event(event, self.clock()))
            try:
                message = final_message(event)
            except Exception:
                message = None
            if message is not None:
                self._final = message
                self._final_sent = False
            kind = event.get("type")
            if isinstance(kind, str) and kind in _END_EVENTS:
                self._send_handoff()

    def _learn(self, line: bytes) -> None:
        """Scrub with the remote worker's own environment (#100 r5); a report
        that cannot be read leaves the scrubber blind."""
        environ = worker_environ(line)
        if environ is None:
            self._warn("worker-env", "telemetry: unreadable worker environment report; tool names hidden")
            return
        with self._lock:
            self.scrub.learn(environ)

    def flush(self) -> None:
        """Write the summary so far (orch job run calls this after each heartbeat)."""
        if self._finished:
            return
        self._write(None, finished=False)

    def wall_clock(self, seconds: float) -> None:
        """#199: the run was stopped past its budget after `seconds` holding its slot."""
        self._slot_hours_lost = max(0.0, seconds) / 3600

    def finish(self, code: int | None) -> None:
        """Write the final summary with the exit status. Never raises."""
        if self._finished:
            return
        self._finished = True
        try:
            self._write(code, finished=True)
        finally:
            if self.raw_log is not None:
                self.raw_log.close()

    def row(self, code: int | None, *, finished: bool) -> dict[str, Any]:
        with self._lock:
            data = self.summary.row()
            cli_reason = self.summary.cli_reason
        lost = self._slot_hours_lost
        if lost is not None:
            cli_reason = f"wall-clock ({lost:.2f} slot-hours lost)"
        elif self._limit is not None:
            cli_reason = lane_limit.USAGE_LIMIT_REASON
        now = self.clock()
        data.update(
            {
                "job_id": self.job_id,
                "bot": self.bot,
                "harness": self.harness,
                "repo": self.repo,
                "started_at": self.started_at,
                "updated_at": now,
                "finished_at": now if finished else None,
                "elapsed_seconds": max(0.0, self.monotonic() - self._started),
                "exit_code": code if finished else None,
                "exit_reason": exit_reason(code, cli_reason) if finished else None,
                "slot_hours_lost": lost,
            }
        )
        return data

    def _write(self, code: int | None, *, finished: bool) -> None:
        try:
            self._read_rollout()
            data = self.row(code, finished=finished)
            with self._lock:
                readings = list(self.usage.values())
            marks = ", ".join("?" for _ in COLUMNS)
            with closing(self.connect()) as connection:
                with connection:
                    if self.guard is not None:
                        connection.execute("BEGIN IMMEDIATE")
                        self.guard(connection)
                    ensure_table(connection)
                    connection.execute(
                        f"INSERT OR REPLACE INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({marks})",
                        tuple(data[name] for name in COLUMNS),
                    )
                    store(
                        connection,
                        harness=self.harness,
                        bot=self.bot,
                        device=self.device,
                        job_id=self.job_id,
                        readings=readings,
                        account=self.account,
                    )
                    self._record_limit(connection)
        except Exception as exc:  # telemetry never stops the heartbeat or the job's cleanup
            self._warn("write", f"telemetry: summary not written ({type(exc).__name__}: {exc})")

    def _record_limit(self, connection: sqlite3.Connection) -> None:
        if self._limit is None or self._limit_recorded:
            return
        now = self.clock()
        held = lane_limit.record(
            connection,
            job_id=self.job_id,
            harness=self.harness,
            bot=self.bot,
            account=self.account or default_account(self.harness),
            limit=self._limit,
            now=now,
        )
        if held is not None:
            record_job_event(connection, self.job_id, USAGE_LIMIT, lane_limit.label(held), now)
        self._limit_recorded = True

    def add_usage(self, readings: list[Reading]) -> None:
        """Keep each window's newest reading; the next flush stores them (#151)."""
        with self._lock:
            for reading in readings:
                kept = self.usage.get(reading.window)
                if kept is None or reading.observed_at >= kept.observed_at:
                    self.usage[reading.window] = reading

    def add_rollout(self, found: object) -> None:
        """One Codex rollout read (lib/codex_rollout.py `latest`): its rate
        limits as readings and its session token total; the next flush stores both."""
        try:
            self.add_usage(parse_codex(found, self.clock()))
            with self._lock:
                self.summary.codex_total(found.get("tokens") if isinstance(found, dict) else None)
        except Exception as exc:  # orch job run calls this right before the job's final write
            self._warn("rollout", f"telemetry: rollout not read ({type(exc).__name__})")

    def _read_rollout(self) -> None:
        if self.harness != "codex" or self.rollout_home is None:
            return
        with self._lock:
            thread_id = self.summary.thread_id
        if thread_id is not None:
            self.add_rollout(rollout_latest(self.rollout_home, thread_id))

    def _send_handoff(self) -> None:
        """Copy the final message's ORCH-HANDOFF line to stdout, once (#87)."""
        if self._final_sent:
            return
        self._final_sent = True
        line = marker_line(self._final)
        if line is not None:
            self._passthrough(line.encode("utf-8", errors="replace") + b"\n")

    def _passthrough(self, chunk: bytes) -> None:
        if self.stdout is None:
            return
        try:
            self.stdout.write(chunk)
            self.stdout.flush()
        except (OSError, ValueError):
            pass

    def _warn(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.log(" ".join(message.split()))


def start_session(
    *,
    connect: Connect,
    database: Path,
    job_id: str,
    bot: str,
    repo: str,
    command: Sequence[str],
    log: Log,
    environ: Mapping[str, str] | None = None,
    stdout: IO[bytes] | None = None,
    max_log_bytes: int = MAX_LOG_BYTES,
    worker_env: bool = False,
    device: str = DEFAULT_ORCH_HOST,
    account: str | None = None,
    guard: Guard | None = None,
    skills_root: str | None = None,
) -> Session | None:
    """A session for one `orch job run`, or None when ORCH_TELEMETRY is off.

    worker_env: the command runs on a remote device, whose start reports
    the harness's environment first (lib/remote.py `start_script`); until
    that report is read, every text is hidden.

    account: the usage account the placed device's login uses;
    None is the harness's default account.

    guard: runs in each summary write's transaction and refuses it by
    raising.

    skills_root: the placed device's skills tree, for `skill_reads` (#505)."""
    env = os.environ if environ is None else environ
    if not enabled(env):
        return None
    try:
        flags = stream_flags(command, HARNESS_BY_BOT.get(bot))
        harness = flags.harness or HARNESS_BY_BOT.get(bot, "other")
        scrub = Scrubber(env, blind=worker_env)
        raw = EventLog(
            state_dir(job_id, bot, database, env) / RAW_LOG_NAME, max_bytes=max_log_bytes, log=log
        )
    except Exception as exc:  # telemetry never stops a job: run the command as given
        log(f"telemetry: off for this run ({type(exc).__name__})")
        return None
    log(stream_flags_line(flags))
    return Session(
        connect=connect,
        job_id=job_id,
        bot=bot,
        repo=repo,
        harness=harness,
        flags=flags,
        raw_log=raw,
        scrub=scrub,
        log=log,
        stdout=stdout if stdout is not None else getattr(sys.stdout, "buffer", None),
        worker_env=worker_env,
        device=device,
        account=account,
        rollout_home=codex_home(dict(env)) if harness == "codex" and not worker_env else None,
        text=text_enabled(env),
        guard=guard,
        skills=skill_reads(skills_root, log),
    )
