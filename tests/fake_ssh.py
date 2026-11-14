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
"""

import json
import os
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


def main(argv: list[str]) -> int:
    options: list[str] = []
    user = None
    index = 0
    while index < len(argv):
        part = argv[index]
        if part == "-o":
            options.append(argv[index + 1])
            index += 2
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
    script = " ".join(rest[1:]) if len(rest) > 1 else None
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {"options": options, "user": user, "alias": alias, "script": script,
                 "words": len(rest) - 1, "pid": os.getpid()}
            )
            + "\n"
        )
    hosts = os.environ.get("FAKE_SSH_HOSTS", "")
    if alias is None or script is None:
        return 255
    known = os.path.join(hosts, alias)
    if not hosts or not os.path.isfile(known):
        print(f"fake ssh: unknown alias {alias}", file=sys.stderr)
        return 255
    with open(known, encoding="utf-8") as handle:
        if handle.read().strip() != user:
            print(f"fake ssh: {alias} refuses user {user}", file=sys.stderr)
            return 255
    if os.path.exists(known + ".offline"):
        return 255
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
