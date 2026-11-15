"""#219: self-heal.sh deploys before it heals, `kick` starts a pass after a
merge, and the poller kicks only after merging a PR of its own repo.

The shell tests reuse tests/test_self_heal_sh.py's temporary HOME with a
fake `orch` that records `heal deploy`, `health --json` and `heal`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from lib import self_deploy
from tests.test_dispatcher import FakeSource, setup
from tests.test_self_heal_sh import FAKE_SYSTEMCTL, STATE, _calls, _executable, _run, home

assert home  # the fixture, imported for these tests


def _order(home: Path) -> list[str]:
    path = home / "orch-order"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _service_log(home: Path) -> str:
    path = home / STATE / "self-heal-service.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_once_deploys_before_health_and_heal(home: Path) -> None:
    result = _run(home, "once")
    assert result.returncode == 0, result.stderr
    order = _order(home)
    assert order[0] == "heal deploy"
    assert sorted(order[1:]) == ["heal --health-json", "health --json"]
    (deploy,) = [json.loads(line) for line in (home / "deploy-calls").read_text().splitlines()]
    assert deploy["argv"] == ["heal", "deploy", "--state", str(home / STATE)]
    assert deploy["db"] == str(home / STATE / "orch.db")
    assert "deploy output" in _service_log(home)
    assert result.stdout == ""


def test_a_failing_deploy_step_still_heals(home: Path) -> None:
    result = _run(home, "once", FAKE_DEPLOY_EXIT="1")
    assert result.returncode == 0, result.stderr
    assert len(_calls(home)) == 1
    assert (home / "service-calls").read_text() == "waiter-service.sh codex-bot restart\n"
    assert "orch heal deploy exited 1; healing anyway" in _service_log(home)


def test_kick_starts_the_systemd_service_without_waiting(home: Path) -> None:
    systemctl = _executable(home / "fake-bin" / "systemctl", FAKE_SYSTEMCTL)
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "factory-self-heal.service").write_text("[Service]\n", encoding="utf-8")
    result = _run(home, "kick", ORCH_SYSTEMCTL=str(systemctl))
    assert result.returncode == 0, result.stderr
    assert (home / "systemctl-calls").read_text().splitlines() == [
        "--user show-environment",
        "--user start --no-block factory-self-heal.service",
    ]
    assert _order(home) == []
    assert "kick: started factory-self-heal.service" in _service_log(home)


def test_kick_without_the_timer_runs_a_detached_pass(home: Path) -> None:
    started = time.monotonic()
    result = _run(home, "kick")
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 30
    while len(_order(home)) < 4 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _order(home)[0] == "heal deploy"
    assert sorted(_order(home)[1:]) == ["heal --health-json", "heal devices", "health --json"]
    assert time.monotonic() - started < 30
    assert "kick: started a detached pass" in _service_log(home)


# --- the poller's kick -----------------------------------------------------------------------


class Spawns:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[list[str], dict, Path]] = []

    def __call__(self, argv, env, log_path):
        self.calls.append((list(argv), dict(env), log_path))
        if self.error is not None:
            raise self.error


def _kick(merged: set[str], spawns: Spawns, errors: list, repo: str | None = "Max/Factory"):
    return self_deploy.MergeKick(
        lambda: merged, repo=repo, on_error=errors.append, root=Path("/srv/factory"),
        environ={"PATH": "/bin", "ORCH_STATE_DIR": "/srv/state",
                 "FACTORY_WEBHOOK_URL": "https://hooks.example.test/secret"},
        secret_names=("FACTORY_WEBHOOK_URL",), spawn=spawns)


def test_a_merge_of_the_factory_repo_kicks_a_pass_once() -> None:
    spawns, errors = Spawns(), []
    _kick({"max/factory", "other/repo"}, spawns, errors)()
    assert spawns.calls == [(["/srv/factory/deploy/self-heal.sh", "kick"],
                             {"PATH": "/bin", "ORCH_STATE_DIR": "/srv/state"},
                             Path("/srv/state/self-heal-service.log"))]
    assert errors == []


@pytest.mark.parametrize("merged, repo", [
    (set(), "max/factory"),
    ({"other/repo"}, "max/factory"),
    ({"max/factory"}, None),
])
def test_no_kick_without_a_merge_of_the_factory_repo(merged, repo) -> None:
    spawns = Spawns()
    _kick(merged, spawns, [], repo=repo)()
    assert spawns.calls == []


def test_a_failed_kick_is_reported_and_does_not_raise() -> None:
    errors: list = []
    _kick({"max/factory"}, Spawns(OSError("no such file")), errors)()
    assert [type(e).__name__ for e in errors] == ["OSError"]


@pytest.mark.parametrize("url, repo", [
    ("https://github.com/maxharris1/factory.git", "maxharris1/factory"),
    ("git@github.com:MaxHarris1/Factory.git", "maxharris1/factory"),
    ("https://github.com/maxharris1/factory", "maxharris1/factory"),
    ("/srv/origin.git", None),
])
def test_own_repo_reads_the_origin_url(tmp_path: Path, url: str, repo: str | None) -> None:
    import subprocess

    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url], check=True)
    assert self_deploy.own_repo(tmp_path) == repo


def test_the_dispatcher_records_repos_that_entered_merged_this_poll(tmp_path: Path) -> None:
    dispatcher, _ = setup(tmp_path)
    dispatcher.poll_once(FakeSource())
    assert dispatcher.merged_this_poll == set()
    dispatcher.record_terminal("Owner/Repo", "42", "merged")
    assert dispatcher.merged_this_poll == {"owner/repo"}
    dispatcher.poll_once(FakeSource())
    assert dispatcher.merged_this_poll == set()
    dispatcher.record_terminal("owner/repo", "42", "merged")
    assert dispatcher.merged_this_poll == set()


def test_the_poll_loop_runs_the_kick_after_each_cycle() -> None:
    from lib.cli import _AfterPoll

    order: list[str] = []
    _AfterPoll(lambda: order.append("backup"), lambda: order.append("push"),
               lambda: order.append("kick"))()
    assert order == ["backup", "push", "kick"]
