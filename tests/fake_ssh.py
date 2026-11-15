"""A fake `ssh` for tests. It never contacts a host.

It accepts the argv lib/remote.py builds (`-o OPTION`..., `-l USER`, `--`,
ALIAS, COMMAND...), joins COMMAND with spaces into one string as ssh does,
appends one JSON line per call to $FAKE_SSH_LOG, and then plays the device:

* the alias must be a file in $FAKE_SSH_HOSTS whose content is the user
  that alias logs in as; a missing alias or a different `-l` user exits
  255, as a refused login would;
* `<alias>.offline` in $FAKE_SSH_HOSTS makes the device unreachable (255);
* otherwise SCRIPT runs under `sh -c` in a new session with a fresh
  environment (PATH and HOME only: ssh forwards no variables; the test
  harness's FACTORY_TEST_PROC_TAG rides along so tests/child_reaper.py can
  find a hung test's "remote" workers), as sshd
  runs a command, so killing this process leaves the "remote" worker
  running. $FAKE_SSH_PATH, when set, is the device's non-login PATH.
  HOME is $FAKE_SSH_HOME, else an empty `fake-ssh-home` beside the log,
  never the real HOME: a `bash -lc` on the device reads that HOME's
  ~/.bash_profile, so a test can tell a login shell's PATH from a bare one.
  Standard input is passed through, as ssh forwards it.

Link faults (#106), each a file in $FAKE_SSH_HOSTS holding seconds, read
when the call starts (create or remove one mid-test to toggle it):

* `<alias>.delay`: the client sleeps that long before the script runs;
  killing the client meanwhile means the script never runs;
* `<alias>.delay-detached`: the call has already reached "sshd": the script
  runs that long later in its own session even if the client is killed
  first (a late delivery);
* `<alias>.drop-after`: the client exits 255 after that long, as a dropped
  link does. The script keeps running; its output no longer reaches the
  client, and a write to the closed channel ends it with SIGPIPE.

Connection reuse (#222). `-o ControlMaster=...`, `-o ControlPath=...` and
`-o ControlPersist=...` stay in `options`; `-N`, `-f` and `-O <op>` are
understood. Control calls are logged to $FAKE_SSH_CONTROL_LOG (default
`$FAKE_SSH_LOG.control`), never to $FAKE_SSH_LOG, as one JSON line each with
`op` (`master`, `check`, `stop`, `exit`), `alias`, `user`, `path`, `code`:

* a master start (`ControlMaster=yes` with `-N`) exits 255 as a login would
  (unknown alias, wrong user, `<alias>.offline`); otherwise it binds a real
  unix socket at ControlPath, writes `<path>.fake-master` (the master is
  alive while that marker is there) and exits 0, as `-f` does;
* `-O check` exits 0 while both the socket and its marker exist, else 255
  with `Control socket connect(<path>): Connection refused`;
* `-O stop` / `-O exit` remove the marker, the socket and `<path>.fake-stale`;
* `<path>.fake-stale` makes that live master stale, as after a box VM
  pause: `-O check` still exits 0 (the local master process answers) and
  fresh logins still work, but a session through it hangs until the client
  is killed, as on a TCP connection whose far end is gone;
* a data call whose command is exactly `exit 0 # orch-ssh-mux-probe` is the
  multiplexer's probe: it is logged to the control log as op `probe` (code
  null while it hangs) and never to $FAKE_SSH_LOG, runs no script, ignores
  link faults, and exits 0 through a fresh master or a login, else 255;
* a data call with a ControlPath (not `none`) logs `"via": "master"` when
  that master is alive, else `"via": "login"`: ssh's own fallback to a
  plain login over a missing or stale socket. `<alias>.mux-refuse` makes a
  live master refuse the session: ssh warns and falls back to a login.
  `<alias>.offline` fails the call (255) through a live master too: the
  link under it is down.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time


def _seconds(path: str) -> float | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return float(handle.read().strip() or 0)
    except FileNotFoundError:
        return None


def _test_tag() -> dict[str, str]:
    tag = os.environ.get("FACTORY_TEST_PROC_TAG")
    return {"FACTORY_TEST_PROC_TAG": tag} if tag else {}


def _relay(source, sink) -> None:
    for chunk in iter(lambda: source.read1(65536), b""):
        sink.write(chunk)
        sink.flush()


def _option(options: list[str], name: str) -> str | None:
    for option in options:
        key, _, value = option.partition("=")
        if key == name:
            return value
    return None


def _live(path: str) -> bool:
    return os.path.exists(path) and os.path.exists(path + ".fake-master")


def _refused(hosts: str, alias: str | None, user: str | None) -> str | None:
    """Why a login to alias as user fails, or None when it succeeds."""
    if alias is None:
        return "no alias"
    known = os.path.join(hosts, alias)
    if not hosts or not os.path.isfile(known):
        return f"unknown alias {alias}"
    with open(known, encoding="utf-8") as handle:
        if handle.read().strip() != user:
            return f"{alias} refuses user {user}"
    if os.path.exists(known + ".offline"):
        return f"{alias} is offline"
    return None


def _log_control(op: str, alias, user, path: str, code) -> None:
    log = os.environ.get("FAKE_SSH_CONTROL_LOG") or os.environ["FAKE_SSH_LOG"] + ".control"
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"op": op, "alias": alias, "user": user, "path": path, "code": code}) + "\n"
        )


def _probe(hosts: str, alias: str | None, user: str | None, path: str | None, via: str) -> int:
    if via == "master" and path and os.path.exists(path + ".fake-stale"):
        _log_control("probe", alias, user, path, None)
        while True:
            time.sleep(60)
    code = 255 if _refused(hosts, alias, user) else 0
    _log_control("probe", alias, user, path or "", code)
    return code


def _control(op: str, options: list[str], user: str | None, alias: str | None) -> int:
    path = _option(options, "ControlPath") or ""
    hosts = os.environ.get("FAKE_SSH_HOSTS", "")
    code = 0
    if op == "master":
        reason = _refused(hosts, alias, user)
        if reason:
            print(f"fake ssh: {reason}", file=sys.stderr)
            code = 255
        else:
            if os.path.lexists(path):
                print(f"fake ssh: ControlSocket {path} already exists", file=sys.stderr)
                code = 255
            else:
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(path)
                server.close()
                with open(path + ".fake-master", "w", encoding="utf-8") as marker:
                    marker.write(f"{alias} {user}\n")
    elif op == "check":
        if not _live(path):
            print(f"Control socket connect({path}): Connection refused", file=sys.stderr)
            code = 255
    elif op in ("stop", "exit"):
        if _live(path):
            os.unlink(path + ".fake-master")
            os.unlink(path)
            if os.path.exists(path + ".fake-stale"):
                os.unlink(path + ".fake-stale")
        else:
            print(f"Control socket connect({path}): Connection refused", file=sys.stderr)
            code = 255
    else:
        print(f"fake ssh: unknown -O {op}", file=sys.stderr)
        code = 255
    _log_control(op, alias, user, path, code)
    return code


def main(argv: list[str]) -> int:
    options: list[str] = []
    user = None
    op = None
    background = no_command = False
    index = 0
    while index < len(argv):
        part = argv[index]
        if part == "-o":
            options.append(argv[index + 1])
            index += 2
        elif part == "-O":
            op = argv[index + 1]
            index += 2
        elif part == "-N":
            no_command = True
            index += 1
        elif part == "-f":
            background = True
            index += 1
        elif part == "-l":
            user = argv[index + 1]
            index += 2
        elif part == "--":
            index += 1
            break
        else:
            print(f"fake ssh: unexpected argument {part!r}", file=sys.stderr)
            return 255
    rest = argv[index:]
    alias = rest[0] if rest else None
    if op is not None:
        return _control(op, options, user, alias)
    if no_command and _option(options, "ControlMaster") == "yes":
        return _control("master", options, user, alias) if background else 255
    hosts = os.environ.get("FAKE_SSH_HOSTS", "")
    path = _option(options, "ControlPath")
    via = "login"
    if path and path != "none" and _live(path):
        via = "master"
        if alias and os.path.exists(os.path.join(hosts, alias + ".mux-refuse")):
            print(
                "mux_client_request_session: session request failed: "
                "Session open refused by peer",
                file=sys.stderr,
            )
            via = "login"
    script = " ".join(rest[1:]) if len(rest) > 1 else None
    if script == "exit 0 # orch-ssh-mux-probe":
        return _probe(hosts, alias, user, path, via)
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {"options": options, "user": user, "alias": alias, "script": script,
                 "words": len(rest) - 1, "pid": os.getpid(), "via": via}
            )
            + "\n"
        )
    if alias is None or script is None:
        return 255
    if via == "master" and os.path.exists(f"{path}.fake-stale"):
        while True:
            time.sleep(60)
    reason = _refused(hosts, alias, user)
    if reason:
        if "offline" not in reason:
            print(f"fake ssh: {reason}", file=sys.stderr)
        return 255
    known = os.path.join(hosts, alias)
    home = os.environ.get("FAKE_SSH_HOME")
    if not home:
        # Never the real user's HOME: a login shell on the "device" would
        # read the real ~/.profile. An empty one beside the log instead.
        home = os.path.join(os.path.dirname(os.path.abspath(os.environ["FAKE_SSH_LOG"])), "fake-ssh-home")
        os.makedirs(home, exist_ok=True)
    delay = _seconds(known + ".delay")
    if delay:
        time.sleep(delay)
    detached = _seconds(known + ".delay-detached")
    drop = _seconds(known + ".drop-after")
    argv = ["sh", "-c", script]
    if detached:
        argv = ["sh", "-c", 'sleep "$0" && exec sh -c "$1"', str(detached), script]
    child = subprocess.Popen(
        argv,
        start_new_session=True,
        env={
            "PATH": os.environ.get("FAKE_SSH_PATH") or os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            **_test_tag(),
        },
        stdout=subprocess.PIPE if drop is not None else None,
        stderr=subprocess.PIPE if drop is not None else None,
    )
    if drop is None:
        return child.wait()
    relays = [
        threading.Thread(target=_relay, args=(child.stdout, sys.stdout.buffer), daemon=True),
        threading.Thread(target=_relay, args=(child.stderr, sys.stderr.buffer), daemon=True),
    ]
    for relay in relays:
        relay.start()
    try:
        code = child.wait(timeout=drop)
    except subprocess.TimeoutExpired:
        sys.stderr.flush()
        os._exit(255)  # the pipes close with us; the script runs on
    for relay in relays:
        relay.join()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
