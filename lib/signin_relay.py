"""The sign-in relay directory, orch.yml `dashboard.settings.relay` (#1383).

It sits beside the settings spool, on `dashboard.push.host` when the push is
ssh, else on this host, and is read and written over the push login through
one multiplexed connection. `<id>.relay` is an attempt's view, published by
the sign-in broker. `<id>.code` and `<id>.cancel` are the dashboard's to
write; the broker takes each one (claims, reads and removes it). Files are
written 0640. The sign-in requests themselves stay in the settings spool.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

from lib import dashboard_push, ssh_mux
from lib.dashboard_config import PushConfig, SettingsConfig
from lib.remote import ByteTransport, Exchanged, SshTransport
from lib.settings_apply import (
    MAX_REQUEST_BYTES,
    REMOTE_LIST,
    SSH_TIMEOUT_SECONDS,
    LocalSpool,
    SpoolError,
    SpoolListing,
    parse_listing,
)

MODE = 0o640
RID = re.compile(r"[A-Za-z0-9_-]{16,64}")
KINDS = ("code", "cancel")
_CLAIMED = b"+"

Kind = Literal["code", "cancel"]


class Relay(Protocol):
    def poll(self) -> SpoolListing: ...

    def publish(self, rid: str, view: Mapping[str, object]) -> None: ...

    def take(self, rid: str, kind: Kind) -> bytes | None: ...


def _name(rid: str, kind: str) -> str:
    if not isinstance(rid, str) or RID.fullmatch(rid) is None:
        raise ValueError("bad request id")
    if kind not in ("relay", *KINDS):
        raise ValueError(f"bad relay file kind {kind!r}")
    return f"{rid}.{kind}"


def _view(view: Mapping[str, object]) -> bytes:
    return json.dumps(dict(view), separators=(",", ":")).encode("utf-8")


class LocalRelay:
    def __init__(self, spool: Path, relay: Path):
        self.spool = Path(spool)
        self.relay = Path(relay)

    def poll(self) -> SpoolListing:
        return LocalSpool(self.spool).read()

    def publish(self, rid: str, view: Mapping[str, object]) -> None:
        name = _name(rid, "relay")
        temp = self.relay / f".{name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, MODE)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(_view(view))
                out.flush()
                os.fchmod(out.fileno(), MODE)
                os.fsync(out.fileno())
            os.replace(temp, self.relay / name)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def take(self, rid: str, kind: Kind) -> bytes | None:
        name = _name(rid, kind)
        temp = self.relay / f".{name}.{secrets.token_hex(6)}.take"
        try:
            os.rename(self.relay / name, temp)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(os.open(temp, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                return handle.read(MAX_REQUEST_BYTES)
        finally:
            temp.unlink(missing_ok=True)


_PUBLISH = """\
umask 027
cd -- {relay} || exit 3
t=.{name}.$$.tmp
if cat > "$t" && chmod 0640 "$t" && mv -f -- "$t" {name}; then exit 0; fi
rm -f -- "$t"
exit 4
"""

_TAKE = """\
cd -- {relay} || exit 3
t=.{name}.$$.take
if ! mv -- {name} "$t" 2>/dev/null; then
  if [ -e {name} ] || [ -L {name} ]; then exit 4; fi
  exit 0
fi
if [ -L "$t" ] || [ ! -f "$t" ]; then rm -f -- "$t"; exit 4; fi
printf '+'
head -c {limit} -- "$t"
s=$?
rm -f -- "$t"
exit $s
"""


def _options(ssh_config: str | None) -> list[str]:
    return [*(["-F", ssh_config] if ssh_config else []), *dashboard_push.SSH_OPTIONS]


class SshRelay:
    def __init__(self, host: str, user: str, spool: str, relay: str, *,
                 ssh_config: str | None = None, transport: ByteTransport | None = None):
        self.host = host
        self.user = user
        self.spool = spool
        self.relay = relay
        self.options = _options(ssh_config)
        self.transport = transport or SshTransport(
            mux=ssh_mux.Multiplexer(binary="ssh", options=self.options))

    def _run(self, script: str, data: bytes = b"") -> Exchanged:
        exchanged = self.transport.exchange(
            ["ssh", *self.options, "-l", self.user, "--", self.host, script],
            timeout=SSH_TIMEOUT_SECONDS, input=data)
        if exchanged.timed_out:
            raise SpoolError(f"ssh to {self.host} timed out")
        if exchanged.code != 0:
            detail = next((line.strip() for line in exchanged.stderr.splitlines() if line.strip()), "")
            raise SpoolError(f"ssh to {self.host} failed (exit {exchanged.code})"
                             + (f": {dashboard_push.redact(detail)[:200]}" if detail else ""))
        return exchanged

    def poll(self) -> SpoolListing:
        script = REMOTE_LIST.format(path=shlex.quote(self.spool), limit=MAX_REQUEST_BYTES + 1)
        return parse_listing(self._run(script).stdout.decode("utf-8", "replace"))

    def publish(self, rid: str, view: Mapping[str, object]) -> None:
        name = shlex.quote(_name(rid, "relay"))
        self._run(_PUBLISH.format(relay=shlex.quote(self.relay), name=name), _view(view))

    def take(self, rid: str, kind: Kind) -> bytes | None:
        name = shlex.quote(_name(rid, kind))
        out = self._run(_TAKE.format(relay=shlex.quote(self.relay), name=name,
                                     limit=MAX_REQUEST_BYTES)).stdout
        return out[len(_CLAIMED):] if out.startswith(_CLAIMED) else None


def relay_for(settings: SettingsConfig, push: PushConfig | None, *,
              runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Relay | None:
    """The relay orch.yml names, or None when dashboard sign-in is off."""
    if settings.relay is None:
        return None
    if push is None or push.transport != "ssh":
        return LocalRelay(Path(settings.spool), Path(settings.relay))
    argv = ["ssh", *_options(push.ssh_config), "-G", "--", push.host]
    try:
        result = runner(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SpoolError(f"ssh -G {push.host}: {type(exc).__name__}") from None
    user = None
    if result.returncode == 0:
        user = next((line.split(" ", 1)[1].strip() for line in (result.stdout or "").splitlines()
                     if line.startswith("user ")), None)
    if not user:
        raise SpoolError(f"ssh -G {push.host} names no user (exit {result.returncode})")
    return SshRelay(push.host, user, settings.spool, settings.relay, ssh_config=push.ssh_config)
