"""One dashboard sign-in attempt, from the CLI's start to its audit line (#1384).

The pty echoes the pasted code and the CLI prints whatever it likes, so its
output stays in memory and only the parsed URL and code reach `<id>.relay`.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO

from lib import account_doctor, heal
from lib.account_doctor import SignIn
from lib.account_signin import (
    GROUP_LINE_BYTES,
    GROUP_MARKER,
    End,
    Outcome,
    disturbed,
    first_process_group,
    judge,
    kill_script,
    login_script,
    parse_output,
    settle,
)
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.remote import PROBE_TIMEOUT_SECONDS, ByteTransport, SshTransport
from lib.settings_apply import AuditEntry, SpoolError
from lib.signin_broker import Attempt
from lib.signin_relay import Kind, Relay

EXPIRY_SECONDS = 15 * 60
UNRECOGNIZED_SECONDS = 30
TICK_SECONDS = 0.5
OUTPUT_BYTES = 64 * 1024
STARTUP_GRACE_SECONDS = 5
CODE = re.compile(rb"[!-~]{1,512}")

Finish = Callable[[Attempt, str, str | None, object], AuditEntry]


def _popen(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class _Output:
    def __init__(self, stream: IO[bytes]):
        self._tail = bytearray()
        self._marker = b""
        self._lock = threading.Lock()
        self.group: int | None = None
        self.grouped = threading.Event()
        threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream: IO[bytes]) -> None:
        try:
            for chunk in iter(lambda: stream.read1(4096), b""):
                with self._lock:
                    self._tail += chunk
                    del self._tail[:-OUTPUT_BYTES]
                if not self.grouped.is_set():
                    self._find_group(chunk)
        except (OSError, ValueError):
            pass
        self.grouped.set()

    def _find_group(self, chunk: bytes) -> None:
        window = self._marker + chunk
        at = window.find(GROUP_MARKER)
        if at < 0:
            self._marker = window[1 - len(GROUP_MARKER):]
            return
        self._marker = window[at:at + GROUP_LINE_BYTES]
        self.group = first_process_group(self._marker)
        if self.group is not None or len(self._marker) >= GROUP_LINE_BYTES:
            self.grouped.set()

    def bytes(self) -> bytes:
        with self._lock:
            return bytes(self._tail)


@dataclass
class Runner:
    relay: Relay
    machines: Path
    db: Path
    transport: ByteTransport = field(default_factory=SshTransport)
    repo: str | None = None
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    start: Callable[[list[str]], subprocess.Popen] = _popen
    expiry_seconds: float = EXPIRY_SECONDS
    unrecognized_seconds: float = UNRECOGNIZED_SECONDS
    tick_seconds: float = TICK_SECONDS
    startup_grace_seconds: float = STARTUP_GRACE_SECONDS
    stop: threading.Event = field(default_factory=threading.Event)

    def run(self, attempt: Attempt, finish: Finish) -> None:
        config, _ = load_machines_yml(self.machines)
        if config is None:
            raise ValueError("machines.yml is invalid")
        before = self._others(config, attempt, account_doctor.observe(config, self.transport, self.clock()))
        end = self._session(attempt)
        self._publish(attempt.request.id, {"state": "verifying"})
        outcome, new = self._verdict(config, attempt, before, end)
        finish(attempt, outcome.result, outcome.error, new)

    def _session(self, attempt: Attempt) -> End:
        rid, harness = attempt.request.id, attempt.harness
        process = self.start(account_doctor.ssh_argv(
            attempt.device, harness, login_script(harness, attempt.home), tty=True))
        output = _Output(process.stdout)
        started = self.monotonic()
        prompt = None
        shown = typed = False
        self._publish(rid, {"state": "awaiting-url"})
        try:
            while True:
                if process.poll() is not None:
                    return "exit"
                now = self.monotonic()
                if now - started >= self.expiry_seconds:
                    return "expired"
                if self.stop.is_set() or self._take(rid, "cancel") is not None:
                    return "cancelled"
                seen = output.bytes()
                if prompt is None:
                    prompt = parse_output(harness, seen)
                    if prompt is None and now - started >= self.unrecognized_seconds:
                        return "unrecognized-output"
                if prompt is not None and not shown:
                    left = self.expiry_seconds - (now - started)
                    shown = self._publish(rid, {
                        "state": "awaiting-code" if prompt.code is None else "awaiting-approval",
                        "url": prompt.url, **({} if prompt.code is None else {"code": prompt.code}),
                        "expires_at": heal.stamp(self.clock() + left)})
                if prompt is not None and prompt.code is None:
                    code = self._take(rid, "code")
                    if code is not None and not typed:
                        typed = _type(process, code)
                self.stop.wait(self.tick_seconds)
        finally:
            if process.poll() is None:
                self._stop(process, attempt, output)
            for stream in (process.stdin, process.stdout):
                stream.close()

    def _stop(self, process: subprocess.Popen, attempt: Attempt, output: _Output) -> None:
        output.grouped.wait(self.startup_grace_seconds)
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        group = output.group
        if group is None:
            return
        exchanged = self.transport.exchange(
            account_doctor.ssh_argv(attempt.device, attempt.harness, kill_script(group)),
            timeout=PROBE_TIMEOUT_SECONDS)
        if exchanged.timed_out or exchanged.code != 0:
            print(f"signin attempt: kill on {attempt.device.name} exited {exchanged.code}", file=sys.stderr)

    def _verdict(self, config: MachinesConfig, attempt: Attempt, before: Mapping[str, SignIn],
                 end: End) -> tuple[Outcome, str | None]:
        now = self.clock()
        rows = account_doctor.observe(config, self.transport, now)
        after = self._others(config, attempt, rows)
        at = next((index for index, row in enumerate(rows) if (row.account, row.device) ==
                   (attempt.request.account, attempt.device.name)), None)
        outcome = None if at is None else judge(rows[at], before, after)
        changed = disturbed(before, after)
        if outcome is not None and changed and rows[at].status != "mismatch":
            rows[at] = replace(rows[at], status="mismatch",
                               detail=account_doctor.DISTURBED_ACCOUNT.format(", ".join(changed)))
        try:
            with closing(sqlite3.connect(self.db, timeout=30, isolation_level=None)) as connection:
                account_doctor.write(connection, rows, config, self.repo, now)
        except sqlite3.Error:
            return settle(end, Outcome("failed", "doctor-db", None)), None
        if outcome is None:
            return settle(end, Outcome("failed", "no-doctor-row", None)), None
        return settle(end, outcome), rows[at].status

    @staticmethod
    def _others(config: MachinesConfig, attempt: Attempt, rows: list[SignIn]) -> dict[str, SignIn]:
        accounts = set(config.accounts_on(attempt.device.name, attempt.harness)) - {attempt.request.account}
        return {row.account: row for row in rows if row.device == attempt.device.name and row.account in accounts}

    def _publish(self, rid: str, view: Mapping[str, object]) -> bool:
        try:
            self.relay.publish(rid, view)
        except (SpoolError, OSError) as exc:
            print(f"signin attempt: publish: {type(exc).__name__}", file=sys.stderr)
            return False
        return True

    def _take(self, rid: str, kind: Kind) -> bytes | None:
        try:
            return self.relay.take(rid, kind)
        except (SpoolError, OSError) as exc:
            print(f"signin attempt: take {kind}: {type(exc).__name__}", file=sys.stderr)
            return None


def _type(process: subprocess.Popen, code: bytes) -> bool:
    code = code.strip()
    if CODE.fullmatch(code) is None:
        return False
    try:
        process.stdin.write(code + b"\r")
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        return False
    return True
