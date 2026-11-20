"""Whether this host's systemd user manager starts scopes, and if not, why (#1140)."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys


@functools.cache
def scope_refusal(stripped: bool = False) -> str | None:
    """None when `systemd-run --user --scope` works here, else a skip reason.

    stripped: probe from the PATH/HOME-only environment a remote start runs
    in (tests/fake_ssh.py), where the start script derives XDG_RUNTIME_DIR.
    """
    if not sys.platform.startswith("linux"):
        return "systemd scopes are Linux only"
    if shutil.which("systemd-run") is None:
        return "no systemd-run on PATH"
    env = None
    where = "user manager unreachable"
    if stripped:
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": os.environ.get("HOME", "/"),
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        }
        where += " from a PATH/HOME-only environment"
    try:
        done = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{where}: {exc}"
    if done.returncode == 0:
        return None
    return f"{where}: {' '.join(done.stderr.split()) or f'systemd-run exited {done.returncode}'}"
