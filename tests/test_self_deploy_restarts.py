"""#219 end to end: after self-deploy moves the checkout, the heal pass
restarts the poller through its service script and the waiter in the
same pass, once the waiter acknowledges it has stopped claiming;
an up-to-date checkout restarts nothing."""

from __future__ import annotations

from pathlib import Path

from lib import health, heal
from tests.test_heal import Runs, component
from tests.test_self_deploy import Repo, run, write

OWNED = frozenset({"poller", "waiter:codex-bot"})


def heal_pass(tmp_path: Path, checkout: Path, running: str, *, acks: bool = True) -> Runs:
    code = health.factory_version(checkout)
    data = {"known": True, "code_version": code, "components": [
        dict(component("poller", bot="", version=running), stale_code=running != code),
        dict(component(version=running), stale_code=running != code),
    ]}
    runs = Runs()

    def waiter_acknowledged() -> heal.Facts:
        return heal.Facts("box", OWNED, drain_acked={"codex-bot": code} if acks else {})

    heal.heal(data, facts=heal.Facts("box", OWNED),
              state_dir=tmp_path, deploy=checkout / "deploy", webhook=heal.Webhook(),
              environ={"PATH": "/usr/bin"}, now=1_800_000_000.0, run=runs, clock=lambda: 0.0,
              refresh=waiter_acknowledged, sleep=lambda _seconds: None)
    return runs


def test_a_fast_forward_restarts_the_poller_and_the_waiter(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    old = health.factory_version(repo.box)
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    new = repo.commit("code")
    assert run(repo)[0].kind == "deployed"
    assert health.factory_version(repo.box) == new[:12] != old
    (tmp_path / "heal").mkdir()
    runs = heal_pass(tmp_path / "heal", repo.box, old)
    assert [call[0] for call in runs.calls] == [
        [str(repo.box / "deploy/poller-service.sh"), "restart"],
        [str(repo.box / "deploy/waiter-service.sh"), "codex-bot", "restart"],
    ]


def test_a_waiter_that_never_acknowledges_drains_while_the_poller_restarts(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    old = health.factory_version(repo.box)
    write(repo.upstream, "lib/code.py", "VERSION = 2\n")
    repo.commit("code")
    run(repo)
    (tmp_path / "heal").mkdir()
    runs = heal_pass(tmp_path / "heal", repo.box, old, acks=False)
    assert [call[0] for call in runs.calls] == [
        [str(repo.box / "deploy/poller-service.sh"), "restart"]]
    assert (tmp_path / "heal/waiter-codex-bot.drain").read_text().strip() == (
        health.factory_version(repo.box))


def test_an_up_to_date_checkout_restarts_nothing(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    assert run(repo)[0].kind == "up-to-date"
    (tmp_path / "heal").mkdir()
    runs = heal_pass(tmp_path / "heal", repo.box, health.factory_version(repo.box))
    assert runs.calls == []
