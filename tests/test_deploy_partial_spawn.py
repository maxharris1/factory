import errno
import subprocess
import time

import pytest

from tests import test_deploy as deploy
from tests.conftest import _homed, reap_home
from tests.procs import alive


def test_legacy_spawn_failure_leaves_children(tmp_path, monkeypatch):
    popen = subprocess.Popen
    launched = []

    def resource_failure(args, **kwargs):
        if args[0] == "/bin/sh" and len(args) == 2 and str(args[1]).endswith("/run-poller.sh"):
            if len(launched) == 19:
                time.sleep(0.3)
                raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
            process = popen(args, **kwargs)
            launched.append(process.pid)
            return process
        return popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", resource_failure)
    try:
        with pytest.raises(OSError, match="Resource temporarily unavailable"):
            deploy.test_install_retires_the_legacy_launchers(tmp_path)
        leaked = [pid for pid in _homed(tmp_path) if alive(pid)]
        print(f"SPAWN_FAILED; LAUNCHED_POLLERS={len(launched)}; LEFT_RUNNING={leaked}")
        assert all(not alive(pid) for pid in launched)
        assert leaked == [], "cleanup left background processes running"
    finally:
        reap_home(tmp_path)
        assert [pid for pid in _homed(tmp_path) if alive(pid)] == []
