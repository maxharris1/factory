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
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from lib import account_doctor, heal
from lib.account_doctor import SignIn
from lib.account_signin import End, Outcome, first_process_group, judge, kill_script, login_script, parse_output, settle
from lib.machines_yml import MachinesConfig, load_machines_yml
from lib.remote import PROBE_TIMEOUT_SECONDS, ByteTransport, SshTransport
from lib.settings_apply import AuditEntry, SpoolError
from lib.signin_broker import Attempt
from lib.signin_relay import Kind, Relay

EXPIRY_SECONDS = 15 * 60
UNRECOGNIZED_SECONDS = 30
TICK_SECONDS = 0.5
OUTPUT_BYTES = 64 * 1024
CODE = re.compile(rb"[!-~]{1,512}")

Finish = Callable[[Attempt, str, str | None, object], AuditEntry]


def _popen(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class _MemoryTail:
    def __init__(self, stream: IO[bytes]):
        self._data = bytearray()
        self._lock = threading.Lock()
        threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream: IO[bytes]) -> None:
        try:
            for chunk in iter(lambda: stream.read1(4096), b""):
                with self._lock:
                    self._data += chunk
                    del self._data[:-OUTPUT_BYTES]
        except (OSError, ValueError):
            return

    def bytes(self) -> bytes:
        with self._lock:
            return bytes(self._data)


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

    def run(self, attempt: Attempt, finish: Finish) -> None:
        config, _ = load_machines_yml(self.machines)
        if config is None:
            raise ValueError("machines.yml is invalid")
        before = self._others(config, attempt, account_doctor.judge(
            account_doctor.probe(self.transport, config, attempt.device, attempt.harness), self.clock()))
        end = self._session(attempt)
        self._publish(attempt.request.id, {"state": "verifying"})
        outcome, new = self._verdict(config, attempt, before, end)
        finish(attempt, outcome.result, outcome.error, new)

    def _session(self, attempt: Attempt) -> End:
        rid, harness = attempt.request.id, attempt.harness
        process = self.start(account_doctor.ssh_argv(
            attempt.device, harness, login_script(harness, attempt.home), tty=True))
        output = _MemoryTail(process.stdout)
        started = self.monotonic()
        prompt = group = None
        shown = typed = False
        self._publish(rid, {"state": "awaiting-url"})
        try:
            while True:
                if process.poll() is not None:
                    return "exit"
                now = self.monotonic()
                if now - started >= self.expiry_seconds:
                    return "expired"
                if self._take(rid, "cancel") is not None:
                    return "cancelled"
                seen = output.bytes()
                group = group or first_process_group(seen)
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
                time.sleep(self.tick_seconds)
        finally:
            if process.poll() is None:
                self._stop(process, attempt, group)
            for stream in (process.stdin, process.stdout):
                stream.close()

    def _stop(self, process: subprocess.Popen, attempt: Attempt, group: int | None) -> None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if group is None:
            return
        exchanged = self.transport.exchange(
            account_doctor.ssh_argv(attempt.device, attempt.harness, kill_script(group)),
            timeout=PROBE_TIMEOUT_SECONDS)
        if exchanged.timed_out or exchanged.code != 0:
            print(f"signin attempt: kill on {attempt.device.name} exited {exchanged.code}", file=sys.stderr)

    def _verdict(self, config: MachinesConfig, attempt: Attempt, before: Mapping[str, SignIn],
                 end: End) -> tuple[Outcome, str | None]:
        try:
            with closing(sqlite3.connect(self.db, timeout=30, isolation_level=None)) as connection:
                rows = account_doctor.run(connection, config, self.transport, self.clock(), self.repo)
        except sqlite3.Error:
            return settle(end, Outcome("failed", "doctor-db", None)), None
        new = next((row for row in rows if (row.account, row.device) ==
                    (attempt.request.account, attempt.device.name)), None)
        if new is None:
            return settle(end, Outcome("failed", "no-doctor-row", None)), None
        return settle(end, judge(new, before, self._others(config, attempt, rows))), new.status

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
