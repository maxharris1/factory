from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import test_deploy as deploy
from tests import test_waiter_service as waiter
from tests.conftest import reap_home
from tests.procs import alive

FAILURES = (AssertionError, subprocess.TimeoutExpired)


def _running(home: Path) -> dict[str, int]:
    state = home / ".local/state/factory"
    pids = {}
    for path in (*state.glob("*.pid"), *state.glob("*.lock")):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text.isdigit() and alive(int(text)):
            pids[path.name] = int(text)
    return pids


def _fail_after_start_and_time_out_stop(
    monkeypatch: pytest.MonkeyPatch, up: Path, autostart: Path
) -> None:
    run = subprocess.run

    def failing(args, **kwargs):
        if "stop" in args:
            raise subprocess.TimeoutExpired(args, 60)
        result = run(args, **kwargs)
        if args[:2] == ["bash", "-c"] and autostart.exists():
            waiter._wait_for(up.exists)
            raise AssertionError("injected failure after the service started")
        return result

    monkeypatch.setattr(subprocess, "run", failing)


def test_the_poller_profile_test_stops_its_poller_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_after_start_and_time_out_stop(monkeypatch,
                                        tmp_path / ".local/state/factory/poller.pid",
                                        tmp_path / ".config/factory/poller-autostart")
    try:
        with pytest.raises(FAILURES):
            deploy.test_profile_hook_runs_ensure_only_when_autostart_is_on(tmp_path)
        assert _running(tmp_path) == {}
    finally:
        reap_home(tmp_path)


def test_the_linear_profile_test_stops_its_listener_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_after_start_and_time_out_stop(monkeypatch,
                                        tmp_path / ".local/state/factory/linear-webhook-listener.pid",
                                        tmp_path / ".config/factory/linear-webhook-autostart")
    try:
        with pytest.raises(FAILURES):
            deploy.test_install_copies_the_linear_service_and_the_profile_runs_its_ensure(tmp_path)
        assert _running(tmp_path) == {}
    finally:
        reap_home(tmp_path)


def test_the_waiter_fixture_kills_its_waiters_when_stop_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = waiter.home.__wrapped__(tmp_path)
    home = next(fixture)
    try:
        assert waiter._service(home, "claude-bot", "start").returncode == 0
        child = waiter._wait_for(lambda: waiter._starts(home))[0]
        supervisor = waiter._supervisor(home)

        def timing_out(home, *args, **extra):
            raise subprocess.TimeoutExpired(["waiter-service.sh", *args], 60)

        monkeypatch.setattr(waiter, "_service", timing_out)
        with pytest.raises(subprocess.TimeoutExpired):
            fixture.close()
        assert not alive(supervisor) and not alive(child)
    finally:
        reap_home(home)
