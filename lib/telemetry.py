"""Per-job CLI telemetry: the harness's event stream, summarized (issue #93).

`orch job run` (lib/cli.py, lib/job_run.py) runs the harness with its
stdout on a pipe and hands the pipe to a `Session` here:

* `harness_argv` adds the fixed stream flags to the bot's own command:
  Claude Code (`claude -p ...`) gets `--output-format stream-json --verbose`
  and Codex (`codex exec ...`) gets `--json`. A command that already picks an
  output format, or that is neither, is run as given. Nothing from a job
  row reaches the argv; the flags are constants.
* Every stdout line is appended to `<state dir>/events.jsonl` (mode 600, in
  a mode-700 directory) on the box that runs `orch job run`: the waiter's
  job directory ($ORCH_JOB_STATE_DIR), else `telemetry-<bot>/<job-id>`
  beside orch.db. The file is
  capped at MAX_LOG_BYTES: a write that would pass the cap first renames it
  to `events.jsonl.1` (replacing an older one), so at most two files are
  kept. The raw log never leaves the machine. Lines that are not JSON
  objects are also copied to this process's stdout (the waiter's run.log),
  as before; JSON events are not, so run.log stays small. One exception
  keeps a remote run's handoff (#87): the last line of the CLI's final
  message (Claude Code's `result`, Codex's last
  `agent_message`) is copied there if it is an `ORCH-HANDOFF: ...` line, as
  text mode would have printed it, so the waiter on the box still reads it
  as run.log's last line. It is copied at the CLI's end event (`result`,
  `turn.completed`), else when the stream ends, and only once.
* Each JSON event updates a `Summary`: turns, tool calls by type, files
  touched, commands run, the last test run's pass and fail counts, tokens
  in (of which cached) and out, cost when the CLI reports it, the last
  activity and the CLI's own exit reason, plus the last RECENT_STEPS steps
  (tool calls and commands with their status, and errors). A step is its
  time, kind, status and a fixed label (`command`, the harness's tool name,
  `edit <n> files`, the error kind) and the exit reason is a fixed label too:
  no command line, tool argument, pattern, path, query or error message is
  ever stored (#100 r10, Max: "Hide those fields in the job modal instead").
  File paths are only counted.
* `Session.flush` writes the summary to the `job_telemetry` table in
  orch.db; `orch job run` calls it right after each job heartbeat (every
  ~15 seconds), and `Session.finish` writes it once more when the harness
  exits, with the exit status and reason. So a job's telemetry row is
  rewritten with each heartbeat and `job_telemetry.updated_at` moves with
  `jobs.last_activity_at` (`last_seen` reads it, for the stale and health
  checks of #91 and #92).

Secrets: the only free text that goes into the table is the harness's tool
names (as tool call counts and step labels). Defense in depth, they are
scrubbed first (`Scrubber`): the values of the
harness's environment variables whose names look secret, and always its
ORCH_JOB_PROOF and ORCH_WORKER_TAG, are replaced,
whatever their length and however the text quotes or escapes them (each
value is matched against the text as written and as read after up to three
rounds of shell unquoting or JSON, Python and C string-escape decoding, with
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
the scrubber is blind and every tool name is stored as `[redacted-env]`.
Assistant text and tool output are never stored; tool output is only
read for test counts. The dashboard (lib/dashboard_telemetry.py) shows
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
from typing import IO, Any

from lib.codex_sandbox import exec_index
from lib.job_token import PROOF_ENV
from lib.remote import TAG_ENV, WORKER_ENV_END, WORKER_ENV_MARK

TABLE = "job_telemetry"
STATE_DIR_ENV = "ORCH_JOB_STATE_DIR"
TELEMETRY_ENV = "ORCH_TELEMETRY"
RAW_LOG_NAME = "events.jsonl"
MAX_LOG_BYTES = 20 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
RECENT_STEPS = 20
MAX_TOOL_TYPES = 40
LABEL_LIMIT = 40
SCRUB_LIMIT = 16 * 1024
HIDDEN = "[redacted-env]"
SCAN_BYTES = 64 * 1024
MAX_COUNT = 2**53 - 1

HARNESS_BY_BOT = {"claude-bot": "claude", "codex-bot": "codex", "cursor-bot": "cursor"}
_OFF = frozenset({"0", "off", "no", "false"})

Log = Callable[[str], None]
Connect = Callable[[], sqlite3.Connection]

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
)

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
    steps TEXT NOT NULL
)
"""
INDEX = f"CREATE INDEX IF NOT EXISTS {TABLE}_updated ON {TABLE} (updated_at)"


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(DDL)
    connection.execute(INDEX)


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


def harness_argv(command: Sequence[str]) -> tuple[list[str], str | None]:
    """The command with its stream flags added, and the harness it names.

    Only constants are added. `claude` with `-p`/`--print` and no
    `--output-format` gets `--output-format stream-json` (and `--verbose`,
    which stream-json needs with --print); `codex ... exec` (or `e`) with no
    `--json` gets `--json` right after the subcommand word, found by skipping
    option values (`codex --profile exec exec` names its subcommand second).
    Anything else is returned unchanged.
    """
    argv = list(command)
    if not argv:
        return argv, None
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name == "claude":
        if "-p" not in rest and "--print" not in rest:
            return argv, "claude"
        if any(part == "--output-format" or part.startswith("--output-format=") for part in rest):
            return argv, "claude"
        extra = ["--output-format", "stream-json"]
        if "--verbose" not in rest:
            extra.append("--verbose")
        return [argv[0], *extra, *rest], "claude"
    if name == "codex":
        index = exec_index(argv)
        options = rest[: rest.index("--")] if "--" in rest else rest
        if index is None or "--json" in options or "--experimental-json" in options:
            return argv, "codex"
        return [*argv[: index + 1], "--json", *argv[index + 1 :]], "codex"
    return argv, None


def enabled(environ: Mapping[str, str]) -> bool:
    return environ.get(TELEMETRY_ENV, "").strip().lower() not in _OFF


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
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "a": "\a",
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
    text literal, and backslash escapes read as the character they escape."""
    out: Reading = []
    quote = None
    index = 0
    while index < len(reading):
        char, start, _ = reading[index]
        escaped = index + 1 < len(reading) and (quote is None or reading[index + 1][0] in '"\\$`')
        if quote == "'":
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


class Summary:
    """Everything the dashboard shows of one run, from its events."""

    def __init__(self, harness: str, scrub: Callable[..., str]):
        self.harness = harness
        self.scrub = scrub
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
        self.cost_usd: float | None = None
        self.last_event_at: float | None = None
        self.cli_reason: str | None = None
        self.steps: deque[dict] = deque(maxlen=RECENT_STEPS)
        self._open: dict[str, tuple[dict, str | None]] = {}
        self._seen_items: set[str] = set()
        self._messages: dict[str, tuple[int, int, int]] = {}
        self._totals = False

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
                self._finished(
                    str(block.get("tool_use_id")),
                    bool(block.get("is_error")),
                    _text_of(block.get("content")),
                )

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

    def _claude_result(self, event: dict, now: float) -> None:
        self._totals = True
        if "num_turns" in event:
            self.turns = _int(event.get("num_turns"))
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.tokens_in, self.tokens_cached, self.tokens_out = self._claude_usage(usage)
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
        if kind == "turn.started":
            self.turns += 1
        elif kind == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
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

    def _codex_item(self, item: dict, now: float, *, done: bool) -> None:
        item_type = item.get("type")
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
            "cost_usd": self.cost_usd,
            "last_event_at": self.last_event_at,
            "steps": json.dumps(list(self.steps)),
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


# A remote harness ends its final message with `ORCH-HANDOFF: <id|failed>`, and
# the waiter on the box reads it as the last line of run.log (#87,
# lib/waiter.py handoff_marker). In stream mode that message is inside a JSON
# event, which is not copied to run.log, so a session copies the last line of
# the run's final message there at the end of the run, only when it is such a
# line. No other assistant text reaches run.log.
_HANDOFF_LINE = re.compile(r"ORCH-HANDOFF:[^\r\n]{0,100}")
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


def handoff_line(text: str | None) -> str | None:
    """The last non-blank line of `text` when it is an ORCH-HANDOFF line."""
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not _HANDOFF_LINE.fullmatch(lines[-1]):
        return None
    return lines[-1]


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
        argv: Sequence[str],
        raw_log: EventLog | None,
        scrub: Scrubber,
        log: Log,
        stdout: IO[bytes] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        worker_env: bool = False,
    ):
        self.connect = connect
        self.job_id = job_id
        self.bot = bot
        self.repo = repo
        self.harness = harness
        self.argv = list(argv)
        self.raw_log = raw_log
        self.scrub = scrub
        self.log = log
        self.stdout = stdout
        self.clock = clock
        self.monotonic = monotonic
        self.summary = Summary(harness, scrub)
        self.started_at = clock()
        self._started = monotonic()
        self._lock = threading.Lock()
        self._finished = False
        self._warned: set[str] = set()
        self._final: str | None = None  # the run's last final message (#87 handoff line)
        self._final_sent = True
        self._worker_env = worker_env  # a remote start's environment report is still to come

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
            }
        )
        return data

    def _write(self, code: int | None, *, finished: bool) -> None:
        try:
            data = self.row(code, finished=finished)
            marks = ", ".join("?" for _ in COLUMNS)
            with closing(self.connect()) as connection:
                with connection:
                    ensure_table(connection)
                    connection.execute(
                        f"INSERT OR REPLACE INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({marks})",
                        tuple(data[name] for name in COLUMNS),
                    )
        except Exception as exc:  # telemetry never stops the heartbeat or the job's cleanup
            self._warn("write", f"telemetry: summary not written ({type(exc).__name__}: {exc})")

    def _send_handoff(self) -> None:
        """Copy the final message's ORCH-HANDOFF line to stdout, once (#87)."""
        if self._final_sent:
            return
        self._final_sent = True
        line = handoff_line(self._final)
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
) -> Session | None:
    """A session for one `orch job run`, or None when ORCH_TELEMETRY is off.

    worker_env: the command runs on a remote device, whose start reports
    the harness's environment first (lib/remote.py `start_script`); until
    that report is read, every text is hidden."""
    env = os.environ if environ is None else environ
    if not enabled(env):
        return None
    try:
        argv, harness = harness_argv(command)
        scrub = Scrubber(env, blind=worker_env)
        raw = EventLog(
            state_dir(job_id, bot, database, env) / RAW_LOG_NAME, max_bytes=max_log_bytes, log=log
        )
    except Exception as exc:  # telemetry never stops a job: run the command as given
        log(f"telemetry: off for this run ({type(exc).__name__})")
        return None
    return Session(
        connect=connect,
        job_id=job_id,
        bot=bot,
        repo=repo,
        harness=harness or HARNESS_BY_BOT.get(bot, "other"),
        argv=argv,
        raw_log=raw,
        scrub=scrub,
        log=log,
        stdout=stdout if stdout is not None else getattr(sys.stdout, "buffer", None),
        worker_env=worker_env,
    )
