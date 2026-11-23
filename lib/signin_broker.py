"""The sign-in broker's intake for dashboard `op: signin` requests (#1383).

Every POLL_SECONDS it lists the settings spool through the relay
(lib/signin_relay.py) and takes each signin request the audit log has not
seen. It re-checks the request against machines.yml, account_signins and
running jobs, allows one attempt per pair and MAX_CONCURRENT in all,
prepares the account's home on the device when it is missing, and hands the
attempt to `launch`, which ends it with `finish`. Each request ends in
exactly one audit line, a refusal or failure before launch included.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from lib import account_doctor, heal, health, self_deploy, settings_apply
from lib.account_settings import PairRequest
from lib.machines_yml import Device, orch_host
from lib.orch_yml import load_orch_yml
from lib.remote import _UNTRACE, PROBE_TIMEOUT_SECONDS, SSH_UNREACHABLE, ByteTransport, SshTransport, _login
from lib.settings_apply import (
    AUDIT_NAME,
    FAILED,
    REFUSED,
    ROOT,
    AuditEntry,
    BadRequest,
    ClaimLock,
    Refused,
    SpoolError,
    SpoolFile,
    SpoolListing,
    _clip,
    _signin,
    append_audit,
    audited_ids,
    is_broker_request,
    parse_request,
    prepare_pair,
    resolve_pair,
    untrusted,
)
from lib.signin_relay import Relay, relay_for

POLL_SECONDS = 2.0
MAX_CONCURRENT = 2


@dataclass(frozen=True)
class Attempt:
    request: PairRequest
    harness: str
    device: Device
    home: str
    previous: str | None


def _pair(request: PairRequest) -> tuple[str, str]:
    return request.account, request.device


def _thread(work: Callable[[], None]) -> object:
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


@dataclass
class Broker:
    relay: Relay
    machines: Path
    db: Path
    state_dir: Path
    approver: str
    owner: str
    launch: Callable[[Attempt, Callable[[Attempt, str, str | None, object], AuditEntry]], None]
    claims: ClaimLock
    transport: ByteTransport = field(default_factory=SshTransport)
    clock: Callable[[], float] = time.time
    spawn: Callable[[Callable[[], None]], object] = _thread
    _attempts: dict[tuple[str, str], Attempt] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _running: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._idle = threading.Condition(self._lock)

    @property
    def audit_path(self) -> Path:
        return self.state_dir / AUDIT_NAME

    def tick(self) -> list[AuditEntry]:
        self._refresh_holds()
        listing = self.relay.poll()
        with self._lock:
            done = audited_ids(self.audit_path) | {a.request.id for a in self._attempts.values()}
        entries = []
        for item in listing.files:
            stem = item.name[: -len(".json")]
            if stem in done or not is_broker_request(item.body):
                continue
            done.add(stem)
            entry = self._intake(listing, item, stem)
            if entry is not None:
                entries.append(entry)
        return entries

    def finish(self, attempt: Attempt, result: str, error: str | None, new: object) -> AuditEntry:
        """End a live attempt with its one line; ValueError when it is not live."""
        return self._close(self._entry(attempt.request, result, attempt.previous, error, new), attempt)

    def serve(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except (SpoolError, OSError, sqlite3.Error) as exc:
                detail = str(exc) if isinstance(exc, SpoolError) else type(exc).__name__
                print(f"signin broker: {detail}", file=sys.stderr)
            stop.wait(POLL_SECONDS)

    def drain(self) -> None:
        while True:
            with self._idle:
                if self._idle.wait_for(lambda: self._running == 0, POLL_SECONDS):
                    return
            try:
                self._refresh_holds()
            except sqlite3.Error as exc:
                print(f"signin broker: {type(exc).__name__}", file=sys.stderr)

    def _intake(self, listing: SpoolListing, item: SpoolFile, stem: str) -> AuditEntry | None:
        reason = untrusted(listing, item, self.owner)
        if reason is None:
            try:
                request = parse_request(item.body, stem)
            except BadRequest as exc:
                reason = str(exc)
        if reason is not None:
            return self._close(AuditEntry(stem, None, None, heal.stamp(self.clock()), None, None,
                                          None, None, REFUSED, reason))
        assert isinstance(request, PairRequest)
        previous = self._previous(request)
        try:
            attempt = self._admit(request, previous)
        except Refused as refusal:
            return self._close(self._entry(request, REFUSED, previous, refusal.error))
        with self._lock:
            self._running += 1
        self.spawn(lambda: self._run(attempt))
        return None

    def _run(self, attempt: Attempt) -> None:
        try:
            self._attempt(attempt)
        finally:
            with self._idle:
                self._running -= 1
                self._idle.notify_all()

    def _attempt(self, attempt: Attempt) -> None:
        try:
            error = self._prepare(attempt)
        except Refused as refusal:
            self.finish(attempt, REFUSED, refusal.error, None)
            return
        except (SpoolError, OSError) as exc:
            print(f"signin broker: {exc if isinstance(exc, SpoolError) else type(exc).__name__}",
                  file=sys.stderr)
            self._release(attempt)
            return
        except Exception as exc:
            error = type(exc).__name__
        if error is None:
            try:
                self.launch(attempt, self.finish)
                return
            except Exception as exc:
                error = f"launch: {type(exc).__name__}"
        self.finish(attempt, FAILED, error, None)

    def _prepare(self, attempt: Attempt) -> str | None:
        request = attempt.request
        if self._home_missing(attempt.device, attempt.harness, attempt.home):
            self.relay.publish(request.id, {"state": "preparing"})
            error = prepare_pair(self.transport, self.machines, attempt.device, attempt.harness,
                                 request.account)
            if error is not None:
                return error
        self.relay.publish(request.id, {"state": "launching"})
        return None

    def _previous(self, request: PairRequest) -> str | None:
        if not self.db.is_file():
            return None
        with closing(sqlite3.connect(self.db, timeout=30, isolation_level=None)) as connection:
            account_doctor.ensure_table(connection)
            return _signin(connection, request)

    def _admit(self, request: PairRequest, previous: str | None) -> Attempt:
        config, device, harness = resolve_pair(request, self.machines, self.approver, self.db)
        if request.account == config.accounts_on(device.name, harness)[0]:
            raise Refused("default-account")
        blocked = account_doctor.no_ssh_path(device, harness)
        if blocked is not None:
            raise Refused(blocked)
        try:
            home = config.account_home(device.name, harness, request.account)
        except ValueError:
            raise Refused("no-home") from None
        with self._lock:
            if _pair(request) in self._attempts:
                raise Refused("attempt-live")
            if len(self._attempts) >= MAX_CONCURRENT:
                raise Refused("too-many")
        attempt = Attempt(request, harness, device, home, previous)
        try:
            with self.claims() as held:
                busy = _pair(request) in {(pair.account, pair.device) for pair in held.pairs(config)}
                taken = not busy and held.hold(*_pair(request), request.id, self.clock())
                if taken:
                    with self._lock:
                        self._attempts[_pair(request)] = attempt
        except sqlite3.Error:
            raise Refused("cannot read running jobs") from None
        if busy:
            raise Refused("running-job")
        if not taken:
            raise Refused("signing-out")
        return attempt

    def _refresh_holds(self) -> None:
        if not self._attempts or not self.db.is_file():
            return
        with self._lock:
            attempts = list(self._attempts.values())
        with closing(sqlite3.connect(self.db, timeout=30, isolation_level=None)) as connection:
            for attempt in attempts:
                account_doctor.renew_pair(connection, *_pair(attempt.request), attempt.request.id,
                                             self.clock())

    def _release(self, attempt: Attempt) -> None:
        with self._lock:
            if self._attempts.get(_pair(attempt.request)) is not attempt:
                return
            del self._attempts[_pair(attempt.request)]
        self._unhold(attempt)

    def _unhold(self, attempt: Attempt) -> None:
        if not self.db.is_file():
            return
        try:
            with closing(sqlite3.connect(self.db, timeout=30, isolation_level=None)) as connection:
                account_doctor.release_pair(connection, *_pair(attempt.request), attempt.request.id)
        except sqlite3.Error as exc:
            print(f"signin broker: {type(exc).__name__}", file=sys.stderr)

    def _home_missing(self, device: Device, harness: str, home: str) -> bool:
        script = _login(f"{_UNTRACE}test -d {shlex.quote(home)}", "orch-signin-home", ())
        exchanged = self.transport.exchange(account_doctor.ssh_argv(device, harness, script),
                                            timeout=PROBE_TIMEOUT_SECONDS)
        if exchanged.timed_out or exchanged.code == SSH_UNREACHABLE:
            raise Refused("unreachable")
        return exchanged.code != 0

    def _entry(self, request: PairRequest, result: str, previous: object, error: str | None,
               new: object = None) -> AuditEntry:
        return AuditEntry(
            id=request.id, actor=_clip(request.actor), requested_at=request.requested_at,
            applied_at=heal.stamp(self.clock()), device=_clip(request.device), field="signin",
            previous=_clip(previous), new=_clip(new), result=result, error=error,
            target="account", account=_clip(request.account),
        )

    def _close(self, entry: AuditEntry, attempt: Attempt | None = None) -> AuditEntry:
        with self._lock:
            if attempt is not None and self._attempts.get(_pair(attempt.request)) is not attempt:
                raise ValueError(f"no live attempt for {attempt.request.id}")
            append_audit(self.audit_path, entry)
            if attempt is not None:
                del self._attempts[_pair(attempt.request)]
        if attempt is not None:
            self._unhold(attempt)
        try:
            self.relay.publish(entry.id, {"result": entry.result, "error": entry.error})
        except (SpoolError, OSError) as exc:
            print(f"signin broker: publish: {type(exc).__name__}", file=sys.stderr)
        return entry


def main(argv: list[str]) -> int:
    from lib import signin_attempt

    parser = argparse.ArgumentParser(prog="orch accounts signin-broker")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--machines", type=Path, default=ROOT / "machines.yml")
    parser.add_argument("--orch", type=Path, default=ROOT / "orch.yml")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    orch_config, findings = load_orch_yml(args.orch, allow_placeholders=True)
    if orch_config is None:
        error = next((f.message for f in findings if f.severity == "error"), "invalid")
        print(f"signin broker: {args.orch.name}: {error}", file=sys.stderr)
        return 1
    settings = orch_config.dashboard.settings
    try:
        relay = relay_for(settings, orch_config.dashboard.push) if settings is not None else None
    except SpoolError as exc:
        print(f"signin broker: {exc}", file=sys.stderr)
        return 1
    if relay is None:
        print("dashboard sign-in is off")
        return 0
    state_dir = args.state or Path(os.environ.get("ORCH_STATE_DIR")
                                   or Path.home() / ".local/state/factory")
    state_dir.mkdir(parents=True, exist_ok=True)
    db = args.db or Path(os.environ.get("ORCH_DB") or state_dir / "orch.db")
    stop = threading.Event()
    runner = signin_attempt.Runner(relay=relay, machines=args.machines, db=db,
                                   repo=self_deploy.own_repo(ROOT), stop=stop)
    broker = Broker(relay, args.machines, db, state_dir, approver=settings.approver,
                    owner=settings.spool_owner, launch=runner.run,
                    claims=settings_apply.claim_lock(db))
    machines_config = health.load_machines(args.machines)
    beater = health.Beater(db, health.SIGNIN_BROKER, devices=[orch_host(machines_config)],
                           store=health.HealthStore(db, devices=health.known_devices(machines_config)))
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
    code = 0
    with beater:
        if args.once:
            beater.beat_now()
            try:
                broker.tick()
            except (SpoolError, OSError) as exc:
                print(f"signin broker: {exc if isinstance(exc, SpoolError) else type(exc).__name__}",
                      file=sys.stderr)
                code = 1
        else:
            broker.serve(stop)
        broker.drain()
    return code
