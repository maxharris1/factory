"""#122: a separate approval credential. The bots post as a GitHub App; a
capped-ticket go and a hold ok count only from approver_logins once the
switch (github_app.enabled) is on. Every app id, installation id, login and
path here is fake; no key is read.
"""

from __future__ import annotations

import argparse
import builtins
import functools
import io
import os
import sqlite3
from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import Comment, Dispatcher
from lib.harness_env import ApproverProfileExposed, harness_environ
from lib.orch_yml import Approvals, approvals, load_orch_yml
from lib.orchestrate import OrchestrateError, approver_go_post
from lib.waiter import HarnessCommand, Waiter, WaiterError
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions, comment, handled_outcome, job_count
from tests.test_rounds_and_caps import Flow
from tests.test_ship_hold import (
    HEAD,
    NEW_HEAD,
    OK,
    approve,
    hold_factory,
    ready,
    snapshot,
    workflow_state,
)
from tests.test_waiter import MACHINES, FakeContext, FakeSpawn, enqueue, local_pool, make_queue, make_waiter

ROOT = Path(__file__).resolve().parent.parent
APP_BOT = "fake-factory-bots[bot]"
MAX = "maxharris1"
FAKE_APP_ID = 424242
FAKE_INSTALLATION = 9090909
BASE = """\
poll_seconds: 30
projects:
  - repo: owner/repo
trusted_logins:
  {app_bot}: [grok-bot, claude-bot, codex-bot]
  maxharris1: [grok-bot]
  grok-login: [grok-bot]
  claude-login: [claude-bot]
  codex-login: [codex-bot]
  max-login: [grok-bot]
"""


def orch_text(*, enabled: bool | None = True, extra: str = "", profile: str | None = None) -> str:
    text = BASE.format(app_bot=APP_BOT) + "approver_logins:\n  - maxharris1\n"
    if profile is not None:
        text += f"approver_gh_config_dir: {profile}\n"
    if enabled is not None:
        text += (
            "github_app:\n"
            f"  app_id: {FAKE_APP_ID}\n"
            "  installations:\n"
            f"    maxharris1: {FAKE_INSTALLATION}\n"
            "  private_key_file: /nonexistent/fake-app.pem\n"
            f"  bot_login: {APP_BOT}\n"
            f"  enabled: {'true' if enabled else 'false'}\n"
        )
    return text + extra


def load(tmp_path: Path, text: str, *, allow_placeholders: bool = False):
    path = tmp_path / "orch.yml"
    path.write_text(text, encoding="utf-8")
    return load_orch_yml(path, allow_placeholders=allow_placeholders)


def approvals_for(tmp_path: Path, **kwargs) -> Approvals:
    config, findings = load(tmp_path, orch_text(**kwargs))
    assert config is not None, findings
    return approvals(config)


def trusted(config) -> dict:
    return {"github": {login: set(bots) for login, bots in config.trusted_logins.items()}, "linear": {}}


# --- AC 1: the app, the approvers and the profile parse; no key is read ----------


def test_github_app_config_parses_with_fake_ids(tmp_path: Path) -> None:
    config, findings = load(tmp_path, orch_text(enabled=False, profile="/fake/home/gh-approver"))
    assert config is not None, findings
    app = config.github_app
    assert app is not None
    assert (app.app_id, dict(app.installations), app.bot_login, app.enabled) == (
        FAKE_APP_ID,
        {MAX: FAKE_INSTALLATION},
        APP_BOT,
        False,
    )
    assert app.private_key_file == Path("/nonexistent/fake-app.pem")
    assert config.approver_logins == frozenset({MAX})
    assert config.approver_gh_config_dir == Path("/fake/home/gh-approver")
    assert approvals(config).app_trust is False

    config, findings = load(tmp_path, orch_text(enabled=True))
    assert config is not None, findings
    assert approvals(config) == Approvals(app_trust=True, logins=frozenset({MAX}))


def test_no_app_block_means_the_switch_is_off(tmp_path: Path) -> None:
    config, findings = load(tmp_path, BASE.format(app_bot=APP_BOT))
    assert config is not None, findings
    assert (config.github_app, config.approver_logins, approvals(config).app_trust) == (None, None, False)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (orch_text().replace(f"app_id: {FAKE_APP_ID}", "app_id: 0"), "github_app.app_id must be a positive integer"),
        (orch_text().replace(f"app_id: {FAKE_APP_ID}", "app_id: '1'"), "github_app.app_id must be a positive integer"),
        (
            orch_text().replace(f"maxharris1: {FAKE_INSTALLATION}", "maxharris1: -3"),
            "github_app.installations must map an account login to a positive installation id",
        ),
        (
            orch_text().replace(f"    maxharris1: {FAKE_INSTALLATION}\n", ""),
            "github_app.installations must map an account login to a positive installation id",
        ),
        (
            orch_text().replace("/nonexistent/fake-app.pem", "keys/fake-app.pem"),
            "github_app.private_key_file must be an absolute path",
        ),
        (
            orch_text().replace("/nonexistent/fake-app.pem", "~/fake-app.pem"),
            "github_app.private_key_file must be an absolute path",
        ),
        (orch_text().replace(f"bot_login: {APP_BOT}", "bot_login: factory-bots"), "github_app.bot_login must be"),
        (orch_text().replace("enabled: true", "enabled: yes please"), "github_app.enabled must be true or false"),
        (orch_text().replace("  enabled: true\n", "  enabled: true\n  token: x\n"), "unknown field 'github_app.token'"),
        (
            orch_text().replace(f"  bot_login: {APP_BOT}\n", ""),
            "github_app.enabled requires github_app.bot_login",
        ),
        (
            orch_text().replace("approver_logins:\n  - maxharris1\n", ""),
            "github_app.enabled requires approver_logins",
        ),
        (
            orch_text().replace(f"  {APP_BOT}: [grok-bot, claude-bot, codex-bot]\n", ""),
            "github_app.bot_login must be a trusted_logins key",
        ),
        (
            orch_text().replace("  - maxharris1\n", f"  - {APP_BOT}\n"),
            "approver_logins must list GitHub user logins",
        ),
        (orch_text().replace("  - maxharris1\n", ""), "approver_logins must list GitHub user logins"),
        (orch_text(profile="gh-approver"), "approver_gh_config_dir must be an absolute path"),
        (orch_text(profile="~/gh-approver"), "approver_gh_config_dir must be an absolute path"),
    ],
)
def test_github_app_config_rejects_bad_fields(tmp_path: Path, text: str, message: str) -> None:
    config, findings = load(tmp_path, text)
    assert config is None
    errors = [item.message for item in findings if item.severity == "error"]
    assert any(error.startswith(message) for error in errors), errors


def test_check_config_never_opens_the_private_key_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "FAKE-KEY-SENTINEL-not-a-real-key"
    key = tmp_path / "fake-app.pem"
    key.write_text(sentinel, encoding="utf-8")
    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text().replace("/nonexistent/fake-app.pem", str(key)), encoding="utf-8")
    opened: list[str] = []
    for module, name in ((builtins, "open"), (io, "open"), (os, "open")):
        real = getattr(module, name)

        def spy(path, *args, _real=real, **kwargs):
            opened.append(os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else repr(path))
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(module, name, spy)

    status = cli._check_config(["--orch", str(orch), "--factory", str(ROOT / "examples/cleffy/factory.md")])

    output = capsys.readouterr()
    assert status == 0, output.err
    assert str(orch) in opened
    assert str(key) not in opened
    assert sentinel not in output.out + output.err


def _uncomment(text: str, keys: tuple[str, ...]) -> str:
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if any(line == f"# {key}:" or line.startswith(f"# {key}: ") for key in keys):
            inside = True
            out.append(line[2:])
        elif inside and line.startswith("#   "):
            out.append(line[2:])
        else:
            inside = False
            out.append(line)
    return "\n".join(out) + "\n"


@pytest.mark.parametrize(
    ("relative", "keys", "approver_live"),
    [
        ("config/orch.example.yml", ("approver_logins", "approver_gh_config_dir", "github_app"), False),
        ("orch.yml", ("approver_gh_config_dir", "github_app"), True),
    ],
)
def test_example_configs_carry_the_github_app_block(
    tmp_path: Path,
    relative: str,
    keys: tuple[str, ...],
    approver_live: bool,
) -> None:
    original = (ROOT / relative).read_text(encoding="utf-8")
    config, findings = load(tmp_path, original, allow_placeholders=True)
    assert config is not None, findings
    assert config.github_app is None
    assert (config.approver_logins == frozenset({MAX})) is approver_live

    config, findings = load(tmp_path, _uncomment(original, keys), allow_placeholders=True)
    assert config is not None, findings
    assert config.github_app is not None
    assert config.github_app.enabled is False
    assert config.approver_logins == frozenset({MAX})
    assert config.approver_gh_config_dir is not None
    assert approvals(config).app_trust is False


# --- AC 2, 3a, 3b: a capped go ---------------------------------------------------


class AppFlow(Flow):
    """Flow (test_rounds_and_caps) with orch.yml's trust and switch."""

    def __init__(self, tmp_path: Path, *, enabled: bool | None, text: str | None = None):
        super().__init__(tmp_path)
        config, findings = load(tmp_path, text if text is not None else orch_text(enabled=enabled))
        assert config is not None, findings
        self.dispatcher = Dispatcher(
            self.queue.path,
            self.queue,
            trusted_logins=trusted(config),
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
            approvals=approvals(config),
        )


def grok_wakes(flow: Flow, action: str) -> list[sqlite3.Row]:
    with flow.queue.connect() as connection:
        return connection.execute(
            "SELECT source_comment_id, detail FROM jobs WHERE recipient = 'grok' AND action = ?",
            (action,),
        ).fetchall()


@pytest.mark.parametrize("enabled", [None, False])
@pytest.mark.parametrize("author", [MAX, APP_BOT])
def test_switch_off_capped_go_from_the_single_login_still_resumes(
    tmp_path: Path,
    enabled: bool | None,
    author: str,
) -> None:
    flow = AppFlow(tmp_path, enabled=enabled)
    flow.to_cap()
    assert flow.go("go-2", author=author) == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)


def test_switch_on_capped_go_from_the_app_bot_is_refused(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    fixes = job_count(flow.queue, "fix")

    assert flow.go("go-2", author=APP_BOT) == "go-not-from-approver"
    flow.dispatcher.process(flow.snapshot())

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("capped", 0)
    assert job_count(flow.queue, "fix") == fixes
    wakes = grok_wakes(flow, "go-not-from-approver")
    assert [row["source_comment_id"] for row in wakes] == ["go-2"]


def test_switch_on_capped_go_from_the_approver_raises_cap_extra(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    flow.to_cap()
    fixes = job_count(flow.queue, "fix")

    assert flow.go("go-2", author=MAX) == "queued-fix"

    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)
    assert job_count(flow.queue, "fix") == fixes + 1
    assert grok_wakes(flow, "go-not-from-approver") == []


def test_switch_on_go_on_an_uncapped_ticket_keeps_todays_trust(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True)
    assert flow.go("go-1", author=APP_BOT) == "queued-build"
    assert flow.go("go-forged", author="claude-login") == "rejected-go-author"


WITHOUT_MAX_GROK_TRUST = orch_text(enabled=True).replace("  maxharris1: [grok-bot]\n", "")


def test_switch_on_an_approver_without_grok_trust_lifts_a_cap(tmp_path: Path) -> None:
    flow = AppFlow(tmp_path, enabled=True, text=WITHOUT_MAX_GROK_TRUST)
    assert cli._check_config(["--orch", str(tmp_path / "orch.yml")]) == 0
    flow.to_cap()

    assert flow.go("go-2", author=MAX) == "queued-fix"
    assert (flow.state()["workflow_state"], flow.state()["cap_extra"]) == ("fixing", 1)


def test_switch_on_go_from_an_approver_on_an_uncapped_ticket_needs_grok_trust(
    tmp_path: Path,
) -> None:
    flow = AppFlow(tmp_path, enabled=True, text=WITHOUT_MAX_GROK_TRUST)
    assert flow.go("go-1", author=MAX) == "rejected-go-author"
    assert job_count(flow.queue, "build") == 0


# --- AC 2, 3c, 3d: a hold ok ------------------------------------------------------


def hold_dispatcher(tmp_path: Path, enabled: bool | None):
    config, findings = load(tmp_path, orch_text(enabled=enabled))
    assert config is not None, findings
    actions = FakeReviewActions()
    queue_path = tmp_path / "d.db"
    from lib.queue import SQLiteJobQueue

    queue = SQLiteJobQueue(queue_path, clock=lambda: 100.0)
    dispatcher = Dispatcher(
        queue_path,
        queue,
        trusted_logins=trusted(config),
        review_actions=actions,
        grok_delivery=FakeGrokDelivery(),
        approvals=approvals(config),
    )
    return dispatcher, queue, actions


def ok_from(author: str, head: str = HEAD, comment_id: str = "ok-1") -> Comment:
    return comment(comment_id, OK.format(head=head), author=author, created_at=20)


@pytest.mark.parametrize("enabled", [None, False])
@pytest.mark.parametrize("approver", [MAX, "max-login"])
def test_switch_off_hold_ok_from_the_single_login_still_clears(
    tmp_path: Path,
    enabled: bool | None,
    approver: str,
) -> None:
    dispatcher, queue, actions = hold_dispatcher(tmp_path, enabled)
    ready(dispatcher, queue)
    factory = hold_factory(approver=approver)

    dispatcher.process(snapshot(approve(), ok_from(approver), factory=factory, labels=("factory:hold",)))

    assert actions.merges == [(True, HEAD)]
    # #108: the APPROVE merges first in the pass; merged is absorbing.
    assert handled_outcome(queue, "ok-1") == "after-merge"


@pytest.mark.parametrize("approver", [MAX, APP_BOT])
def test_switch_on_hold_ok_from_the_app_bot_keeps_the_hold(tmp_path: Path, approver: str) -> None:
    dispatcher, queue, actions = hold_dispatcher(tmp_path, True)
    ready(dispatcher, queue)
    factory = hold_factory(approver=approver)

    dispatcher.process(snapshot(approve(), ok_from(APP_BOT), factory=factory, labels=("factory:hold",)))

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"
    assert handled_outcome(queue, "ok-1") == "rejected-hold-ok-author"


def test_switch_on_hold_ok_from_a_non_approver_keeps_the_hold(tmp_path: Path) -> None:
    """max-login is a trusted_logins key (today it could clear) but not an approver."""
    from lib.hold import LabelEvent

    dispatcher, queue, actions = hold_dispatcher(tmp_path, True)
    ready(dispatcher, queue)
    factory = hold_factory(approver="max-login")
    removed = (LabelEvent("1", "unlabeled", "factory:hold", "max-login", 15.0),)

    dispatcher.process(snapshot(approve(), ok_from("max-login"), factory=factory, labels=("factory:hold",)))
    dispatcher.process(snapshot(approve(), ok_from("max-login"), factory=factory, labels=(), events=removed))

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"
    assert handled_outcome(queue, "ok-1") == "rejected-hold-ok-author"


def test_switch_on_hold_ok_from_the_approver_clears_at_the_head(tmp_path: Path) -> None:
    dispatcher, queue, actions = hold_dispatcher(tmp_path, True)
    ready(dispatcher, queue)
    factory = hold_factory(approver=MAX)

    dispatcher.process(snapshot(approve(), factory=factory, files=("src/view/Board.tsx",)))
    assert actions.merges == []
    dispatcher.process(snapshot(approve(), ok_from(MAX), factory=factory, files=("src/view/Board.tsx",)))

    assert actions.merges == [(True, HEAD)]
    assert workflow_state(queue) == "merged"
    # #108: the APPROVE merges first in the pass; merged is absorbing.
    assert handled_outcome(queue, "ok-1") == "after-merge"


def test_switch_on_hold_ok_at_an_old_head_does_not_clear(tmp_path: Path) -> None:
    dispatcher, queue, actions = hold_dispatcher(tmp_path, True)
    ready(dispatcher, queue, NEW_HEAD)
    factory = hold_factory(approver=MAX)

    dispatcher.process(
        snapshot(approve(NEW_HEAD), ok_from(MAX, HEAD), head=NEW_HEAD, factory=factory, files=("src/view/Board.tsx",))
    )

    assert actions.merges == []
    assert workflow_state(queue) == "awaiting-ok"


# --- AC 4: check-config ties hold.approver to the approvers ------------------------


def held_factory_md(tmp_path: Path, approver: str) -> Path:
    text = (ROOT / "examples/cleffy/factory.md").read_text(encoding="utf-8")
    text = text.replace("skills: []\n", f"skills: []\nhold:\n  approver: {approver}\n", 1)
    path = tmp_path / approver.replace("[", "").replace("]", "") / "factory.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("approver", ["max", "factory-bots", "max-login"])
def test_check_config_fails_when_the_hold_approver_is_not_an_approver(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    approver: str,
) -> None:
    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=False), encoding="utf-8")
    factory = held_factory_md(tmp_path, approver)

    status = cli._check_config(["--orch", str(orch), "--factory", str(factory)])

    assert status == 1
    assert f"hold.approver {approver} is not in approver_logins" in capsys.readouterr().err


def test_check_config_without_approver_logins_names_trusted_logins(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    orch = tmp_path / "orch.yml"
    orch.write_text(BASE.format(app_bot=APP_BOT), encoding="utf-8")

    assert cli._check_config(["--orch", str(orch), "--factory", str(held_factory_md(tmp_path, "max"))]) == 1
    assert "hold.approver max is not a trusted_logins key" in capsys.readouterr().err
    assert cli._check_config(["--orch", str(orch), "--factory", str(held_factory_md(tmp_path, MAX))]) == 0


@pytest.mark.parametrize("enabled", [None, False, True])
def test_check_config_passes_when_the_hold_approver_is_an_approver(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enabled: bool | None,
) -> None:
    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=enabled), encoding="utf-8")

    status = cli._check_config(["--orch", str(orch), "--factory", str(held_factory_md(tmp_path, "MaxHarris1"))])

    assert status == 0, capsys.readouterr().err


# --- AC 5: harness runs never carry the approver profile ---------------------------


def test_waiter_env_drops_the_approver_gh_config_dir(tmp_path: Path) -> None:
    profile = tmp_path / "grok" / "gh-approver"
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(profile))
    home = str(tmp_path / "claude-bot")
    for gh_config_dir in (profile, profile / "nested"):
        environ = {"PATH": "/usr/bin", "HOME": home, "GH_CONFIG_DIR": str(gh_config_dir)}
        assert harness_environ(environ, switch_on) == {"PATH": "/usr/bin", "HOME": home}
    xdg = {"HOME": home, "XDG_CONFIG_HOME": str(tmp_path / "grok")}
    xdg_profile = approvals_for(tmp_path, enabled=True, profile=str(tmp_path / "grok" / "gh"))
    assert harness_environ(xdg, xdg_profile) == {"HOME": home}


def test_waiter_env_keeps_a_bot_gh_config_dir(tmp_path: Path) -> None:
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(tmp_path / "grok" / "gh-approver"))
    environ = {"HOME": str(tmp_path / "claude-bot"), "GH_CONFIG_DIR": str(tmp_path / "claude-bot" / "gh")}
    assert harness_environ(environ, switch_on) == environ


def test_waiter_refuses_when_the_approver_profile_is_in_the_run_home(tmp_path: Path) -> None:
    home = tmp_path / "claude-bot"
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(home / ".config" / "gh"))
    with pytest.raises(ApproverProfileExposed):
        harness_environ({"HOME": str(home)}, switch_on)
    db, queue = make_queue(tmp_path)
    with pytest.raises(WaiterError, match="approver"):
        waiter_with(tmp_path, queue, db, switch_on, {"HOME": str(home), "PATH": "/usr/bin"})


def waiter_with(tmp_path, queue, db, approvals_value, environ):
    from lib.waiter import HarnessCommand, Waiter
    from tests.test_waiter import MACHINES, FakeContext, local_pool

    return Waiter(
        bot="claude-bot",
        queue=queue,
        pool=local_pool(queue, MACHINES),
        harness=HarnessCommand(bot="claude-bot", command=("fake-harness",)),
        context=FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=MACHINES,
        orch=("python3", "/factory/bin/orch"),
        spawn=FakeSpawn(),
        log=[].append,
        environ=environ,
        approvals=approvals_value,
    )


def test_waiter_env_unchanged_with_the_switch_off(tmp_path: Path) -> None:
    home = tmp_path / "claude-bot"
    profile = home / ".config" / "gh"
    environ = {"PATH": "/usr/bin", "HOME": str(home), "GH_CONFIG_DIR": str(profile)}
    for value in (None, approvals_for(tmp_path, enabled=False, profile=str(profile)), approvals_for(tmp_path, enabled=None, profile=str(profile))):
        assert harness_environ(environ, value) == environ
    db, queue = make_queue(tmp_path)
    off = approvals_for(tmp_path, enabled=False, profile=str(profile))
    assert waiter_with(tmp_path, queue, db, off, environ).environ == environ


def test_waiter_built_with_the_switch_on_spawns_without_the_profile(tmp_path: Path) -> None:
    profile = tmp_path / "grok" / "gh-approver"
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(profile))
    db, queue = make_queue(tmp_path)
    enqueue(queue, "a")
    environ = {"PATH": "/usr/bin", "HOME": str(tmp_path / "claude-bot"), "GH_CONFIG_DIR": str(profile)}
    waiter = waiter_with(tmp_path, queue, db, switch_on, environ)
    waiter.step()
    env = waiter.spawn.calls[0][1]["env"]
    assert "GH_CONFIG_DIR" not in env


# --- scope item: Grok posts a capped go only through Max's profile (#172: never an ok) ---------

FAKE_TOKENS = {
    "GH_TOKEN": "fake-app-token",
    "GITHUB_TOKEN": "fake-app-token",
    "GH_ENTERPRISE_TOKEN": "fake",
    "GITHUB_ENTERPRISE_TOKEN": "fake",
}


def post_env(approvals_value: Approvals, environ: dict[str, str]) -> dict[str, str]:
    return approver_go_post(
        "go",
        "One more round.",
        environ,
        app_trust=approvals_value.app_trust,
        gh_config_dir=approvals_value.gh_config_dir,
    ).env


def test_approver_go_post_uses_only_the_approver_profile(tmp_path: Path) -> None:
    profile = tmp_path / "grok" / "gh-approver"
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(profile))
    environ = {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/home/grok/.config/gh-app", **FAKE_TOKENS}

    env = post_env(switch_on, environ)

    assert env == {"PATH": "/usr/bin", "GH_CONFIG_DIR": str(profile)}


def test_approver_go_post_refuses_with_no_profile_when_the_switch_is_on(tmp_path: Path) -> None:
    switch_on = approvals_for(tmp_path, enabled=True)
    with pytest.raises(OrchestrateError, match="by hand"):
        post_env(switch_on, {"PATH": "/usr/bin", **FAKE_TOKENS})


@pytest.mark.parametrize("enabled", [None, False])
def test_approver_go_post_with_the_switch_off_only_drops_tokens(tmp_path: Path, enabled: bool | None) -> None:
    switch_off = approvals_for(tmp_path, enabled=enabled, profile=str(tmp_path / "gh-approver"))
    environ = {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/home/grok/.config/gh", **FAKE_TOKENS}

    assert post_env(switch_off, environ) == {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/home/grok/.config/gh"}


# --- AC 5: a symlink to the approver profile is the profile ------------------------


def linked(link: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    return link


def test_waiter_env_drops_a_symlink_to_the_approver_profile(tmp_path: Path) -> None:
    profile = tmp_path / "grok" / "gh-approver"
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(profile))
    home = str(tmp_path / "claude-bot")
    alias = linked(tmp_path / "elsewhere" / "gh", profile)
    environ = {"HOME": home, "GH_CONFIG_DIR": str(alias)}
    assert harness_environ(environ, switch_on) == {"HOME": home}
    xdg = linked(tmp_path / "xdg-alias", tmp_path / "grok")
    xdg_profile = approvals_for(tmp_path, enabled=True, profile=str(tmp_path / "grok" / "gh"))
    assert harness_environ({"HOME": home, "XDG_CONFIG_HOME": str(xdg)}, xdg_profile) == {"HOME": home}


def test_waiter_env_drops_the_real_profile_named_through_a_configured_symlink(tmp_path: Path) -> None:
    real = tmp_path / "grok" / "gh-approver"
    configured = linked(tmp_path / "profile-link", real)
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(configured))
    home = str(tmp_path / "claude-bot")
    assert harness_environ({"HOME": home, "GH_CONFIG_DIR": str(real / "hosts")}, switch_on) == {"HOME": home}


def test_waiter_refuses_when_the_default_gh_directory_links_to_the_profile(tmp_path: Path) -> None:
    profile = tmp_path / "approver" / "gh"
    home = tmp_path / "bot"
    alias = linked(home / ".config" / "gh", profile)
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(profile))
    for environ in (
        {"HOME": str(home)},
        {"HOME": str(home), "GH_CONFIG_DIR": str(alias)},
        {"HOME": str(home), "GH_CONFIG_DIR": str(tmp_path / "bot-gh")},
    ):
        with pytest.raises(ApproverProfileExposed):
            harness_environ(environ, switch_on)
    db, queue = make_queue(tmp_path)
    with pytest.raises(WaiterError, match="approver"):
        waiter_with(tmp_path, queue, db, switch_on, {"HOME": str(home), "GH_CONFIG_DIR": str(alias)})


def test_waiter_refuses_when_the_run_home_links_to_a_home_holding_the_profile(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    profile = real_home / "approver" / "gh"
    profile.mkdir(parents=True)
    home = linked(tmp_path / "bot-home", real_home)
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(profile))
    with pytest.raises(ApproverProfileExposed):
        harness_environ({"HOME": str(home), "GH_CONFIG_DIR": str(tmp_path / "bot-gh")}, switch_on)


# --- AC 5: a file linked to one of the profile's files is the profile --------------


def file_alias(kind: str, alias: Path, source: Path) -> None:
    alias.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        alias.symlink_to(source)
    else:
        alias.hardlink_to(source)


@pytest.fixture
def approver_profile(tmp_path: Path) -> Path:
    profile = tmp_path / "approver" / "gh"
    profile.mkdir(parents=True)
    for name in ("hosts.yml", "config.yml"):
        (profile / name).write_text("fake\n")
    return profile


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("name", ["hosts.yml", "config.yml"])
def test_waiter_refuses_when_the_default_gh_directory_links_a_profile_file(
    tmp_path: Path, approver_profile: Path, kind: str, name: str
) -> None:
    home = tmp_path / "bot"
    file_alias(kind, home / ".config" / "gh" / name, approver_profile / name)
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(approver_profile))
    for environ in ({"HOME": str(home)}, {"HOME": str(home), "GH_CONFIG_DIR": str(tmp_path / "bot-gh")}):
        with pytest.raises(ApproverProfileExposed):
            harness_environ(environ, switch_on)
    db, queue = make_queue(tmp_path)
    with pytest.raises(WaiterError, match="approver"):
        waiter_with(tmp_path, queue, db, switch_on, {"HOME": str(home), "PATH": "/usr/bin:/bin"})


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_waiter_env_drops_a_gh_config_dir_that_links_a_profile_file(
    tmp_path: Path, approver_profile: Path, kind: str
) -> None:
    home = str(tmp_path / "bot")
    gh_dir = tmp_path / "bot-gh"
    file_alias(kind, gh_dir / "hosts.yml", approver_profile / "hosts.yml")
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(approver_profile))
    assert harness_environ({"HOME": home, "GH_CONFIG_DIR": str(gh_dir)}, switch_on) == {"HOME": home}
    xdg = tmp_path / "xdg"
    file_alias(kind, xdg / "gh" / "hosts.yml", approver_profile / "hosts.yml")
    assert harness_environ({"HOME": home, "XDG_CONFIG_HOME": str(xdg)}, switch_on) == {"HOME": home}


def test_waiter_env_keeps_a_bot_gh_config_dir_with_its_own_files(tmp_path: Path, approver_profile: Path) -> None:
    gh_dir = tmp_path / "bot-gh"
    gh_dir.mkdir()
    (gh_dir / "hosts.yml").write_text("fake\n")
    (gh_dir / "profile").symlink_to(approver_profile, target_is_directory=True)
    switch_on = approvals_for(tmp_path, enabled=True, profile=str(approver_profile))
    environ = {"HOME": str(tmp_path / "bot"), "GH_CONFIG_DIR": str(gh_dir)}
    assert harness_environ(environ, switch_on) == environ


# --- AC 5: an orch.yml error elsewhere never turns the guard off --------------------


def autostart_once(tmp_path: Path, monkeypatch, text: str | None, environ: dict[str, str]):
    """orch wait --autostart for one step, with a job already queued."""
    orch = tmp_path / "orch.yml"
    if text is not None:
        orch.write_text(text, encoding="utf-8")
    db, queue = make_queue(tmp_path)
    enqueue(queue, "already-queued")
    spawn = FakeSpawn()
    monkeypatch.setattr(cli.waiter, "GhContextSource", lambda *args, **kwargs: FakeContext())
    monkeypatch.setattr(cli.waiter, "Waiter", functools.partial(Waiter, spawn=spawn, environ=environ))
    monkeypatch.setattr(Waiter, "serve", lambda self, stop: (self.step(), 0)[1])
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    args = argparse.Namespace(
        orch=orch, bot="claude-bot", state_dir=tmp_path / "state", db=db, machines=MACHINES
    )
    harness = HarnessCommand(bot="claude-bot", command=("fake-harness",))
    return cli._wait_autostart(args, queue, local_pool(queue), harness), spawn


def test_autostart_keeps_the_guard_when_orch_yml_has_another_error(tmp_path: Path, monkeypatch) -> None:
    profile = tmp_path / "grok" / "gh-approver"
    text = orch_text(enabled=True, profile=str(profile)).replace("poll_seconds: 30", "poll_seconds: 45")
    environ = {"PATH": "/usr/bin", "HOME": str(tmp_path / "bot"), "GH_CONFIG_DIR": str(profile)}
    code, spawn = autostart_once(tmp_path, monkeypatch, text, environ)
    assert code == 0
    assert len(spawn.calls) == 1
    assert "GH_CONFIG_DIR" not in spawn.calls[0][1]["env"]


def test_autostart_refuses_a_profile_in_home_when_orch_yml_has_another_error(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "bot"
    profile = home / ".config" / "gh"
    text = orch_text(enabled=True, profile=str(profile)).replace("poll_seconds: 30", "poll_seconds: 45")
    code, spawn = autostart_once(tmp_path, monkeypatch, text, {"HOME": str(home), "GH_CONFIG_DIR": str(profile)})
    assert code == 2
    assert spawn.calls == []


@pytest.mark.parametrize(
    "text",
    [
        None,
        "poll_seconds: [30\n",
        orch_text(enabled=True).replace("  enabled: true", "  enabled: yes-please"),
        orch_text(enabled=True, profile="relative/gh"),
        orch_text(enabled=True).replace("approver_logins:\n  - maxharris1", "approver_logins: []"),
    ],
    ids=["missing", "bad-yaml", "bad-switch", "relative-profile", "empty-approvers"],
)
def test_autostart_refuses_when_the_approval_settings_cannot_load(
    tmp_path: Path, monkeypatch, text: str | None
) -> None:
    home = tmp_path / "bot"
    code, spawn = autostart_once(tmp_path, monkeypatch, text, {"HOME": str(home), "GH_CONFIG_DIR": str(home / "gh")})
    assert code == 2
    assert spawn.calls == []
