"""A fake `ssh` for tests. It never contacts a host.

It accepts the argv lib/remote.py builds (`-o OPTION`..., `-l USER`, `--`,
ALIAS, SCRIPT), appends one JSON line per call to $FAKE_SSH_LOG, and then
plays the device:

* the alias must be a file in $FAKE_SSH_HOSTS whose content is the user
  that alias logs in as; a missing alias or a different `-l` user exits
  255, as a refused login would;
* `<alias>.offline` in $FAKE_SSH_HOSTS makes the device unreachable (255);
* otherwise SCRIPT runs under `sh -c` in a new session with a fresh
  environment (PATH and HOME only: ssh forwards no variables), as sshd
  runs a command, so killing this process leaves the "remote" worker
  running.
"""

import json
import os
import subprocess
import sys


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
    script = rest[1] if len(rest) > 1 else None
    with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {"options": options, "user": user, "alias": alias, "script": script,
                 "extra": rest[2:], "pid": os.getpid()}
            )
            + "\n"
        )
    hosts = os.environ.get("FAKE_SSH_HOSTS", "")
    if alias is None or script is None or rest[2:]:
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
    child = subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/")},
    )
    return child.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
