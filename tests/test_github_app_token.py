"""#171: the token broker. Fake clock, fake mint, fake ssh. Every token
here is a sentinel string, and every test checks it never reaches output.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from lib import github_app_token as broker
from lib.github_actions import GhResult, GitHubReviewActions
from lib.github_app_token import Holder, Remote, Stored
from lib.orch_yml import GitHubApp
from tests.pool_helpers import LOCAL_BOX_MACHINES

ROOT = Path(__file__).resolve().parent.parent
FAKE_SSH = ROOT / "tests" / "fake_ssh.py"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
SENTINEL = "ghs_SENTINEL_NOT_A_TOKEN"


def app(bot: str = "claude-bot", owners: tuple[str, ...] = ("maxharris1",), key: Path | None = None) -> GitHubApp:
    return GitHubApp(
        bot=bot,
        bot_login=f"fake-{bot}[bot]",
        app_id=222222,
        installations=MappingProxyType({owner: 2000 + i for i, owner in enumerate(owners)}),
        private_key_file=key or Path("/nonexistent/apps/claude.pem"),
    )


class FakeMint:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, datetime]] = []

    def __call__(self, app: GitHubApp, installation: int, now: datetime) -> Stored:
        self.calls.append((app.bot, installation, now))
        return Stored(f"{SENTINEL}_{len(self.calls)}", now, now + timedelta(hours=1))


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def issued(minutes_ago: int, token: str = f"{SENTINEL}_OLD") -> Stored:
    at = NOW - timedelta(minutes=minutes_ago)
    return Stored(token, at, at + timedelta(hours=1))


@pytest.fixture
def shared_root():
    """A token root a same-group user could reach: /tmp is world-searchable
    and the root is 0750, unlike pytest's 0700 tmp_path."""
    root = Path(tempfile.mkdtemp(prefix="app-token-", dir="/tmp"))
    root.chmod(0o750)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_a_55_minute_old_token_is_replaced_and_a_1_minute_old_one_reused(tmp_path: Path) -> None:
    mint = FakeMint()
    path = broker.token_path(tmp_path, "maxharris1")

    broker.write_token(path, issued(1), None)
    stored, minted = broker.ensure(app(), "maxharris1", tmp_path, now=NOW, mint=mint, gid=None)
    assert (stored.token, minted, mint.calls) == (f"{SENTINEL}_OLD", False, [])

    broker.write_token(path, issued(55), None)
    stored, minted = broker.ensure(app(), "maxharris1", tmp_path, now=NOW, mint=mint, gid=None)
    assert minted and stored.token == f"{SENTINEL}_1"
    assert mint.calls == [("claude-bot", 2000, NOW)]
    assert broker.read_token(path) == stored
    assert stored.expires_at == NOW + timedelta(hours=1)


def test_the_refresh_margin_leaves_every_job_at_least_ten_minutes_between_timer_passes() -> None:
    timer_minutes = 5
    assert broker.REFRESH_MARGIN - timedelta(minutes=timer_minutes) >= timedelta(minutes=10)
    timer = (ROOT / "deploy/factory-app-token.timer").read_text(encoding="utf-8")
    assert f"OnUnitActiveSec={timer_minutes}min" in timer


def test_a_missing_or_garbled_token_file_is_minted(tmp_path: Path) -> None:
    mint = FakeMint()
    broker.ensure(app(), "maxharris1", tmp_path, now=NOW, mint=mint, gid=None)
    broker.token_path(tmp_path, "maxharris1").write_text("{not json", encoding="utf-8")
    broker.ensure(app(), "maxharris1", tmp_path, now=NOW, mint=mint, gid=None)
    assert len(mint.calls) == 2


def test_a_timer_pass_mints_only_what_is_due_and_never_prints_a_token(tmp_path: Path) -> None:
    mint, clock, out = FakeMint(), Clock(), io.StringIO()
    apps = [app("claude-bot", ("maxharris1", "aut0-code")), app("codex-bot")]
    args = dict(now=clock, mint=mint, holders={}, remotes={}, out=out)

    assert broker.refresh(apps, tmp_path, **args) == 0
    assert len(mint.calls) == 3
    clock.now = NOW + timedelta(minutes=1)
    assert broker.refresh(apps, tmp_path, **args) == 0
    assert len(mint.calls) == 3
    clock.now = NOW + timedelta(minutes=46)
    assert broker.refresh(apps, tmp_path, **args) == 0
    assert len(mint.calls) == 6

    text = out.getvalue()
    assert "claude-bot aut0-code: minted, expires" in text
    assert "codex-bot maxharris1: fresh, expires" in text
    assert SENTINEL not in text
    assert SENTINEL not in repr(broker.read_token(tmp_path / "codex-bot" / "maxharris1.json"))


def test_a_failed_mint_is_reported_without_stopping_other_bots(tmp_path: Path) -> None:
    good = FakeMint()

    def mint(app: GitHubApp, installation: int, now: datetime) -> Stored:
        if app.bot == "claude-bot":
            raise broker.BrokerError("GitHub refused the token for claude-bot (HTTP 401)")
        return good(app, installation, now)

    out = io.StringIO()
    status = broker.refresh(
        [app("claude-bot"), app("codex-bot")], tmp_path,
        now=Clock(), mint=mint, holders={}, remotes={}, out=out,
    )
    assert status == 1
    assert "claude-bot maxharris1: GitHub refused the token for claude-bot (HTTP 401)" in out.getvalue()
    assert (tmp_path / "codex-bot" / "maxharris1.json").exists()


def test_the_harness_user_can_read_the_token_file_but_not_the_key(shared_root: Path) -> None:
    harness_uid = os.getuid() + 54321
    harness_gids = [os.getgid()]
    keys = shared_root / "apps"
    keys.mkdir(mode=0o700)
    key = keys / "claude.pem"
    key.write_text("not a key\n", encoding="utf-8")
    key.chmod(0o600)
    tokens = shared_root / "app-tokens"
    tokens.mkdir()
    tokens.chmod(0o711)

    status = broker.refresh(
        [app(key=key)], tokens, now=Clock(), mint=FakeMint(),
        holders={"claude-bot": Holder("claude-bot", os.getgid())}, remotes={}, out=io.StringIO(),
    )

    token = tokens / "claude-bot" / "maxharris1.json"
    assert status == 0
    assert stat.S_IMODE(token.stat().st_mode) == 0o640
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o750
    assert token.stat().st_gid == os.getgid()
    assert broker.readable_by(token, harness_uid, harness_gids)
    assert not broker.readable_by(key, harness_uid, harness_gids)
    key.chmod(0o640)
    assert not broker.readable_by(key, harness_uid, harness_gids), "the key dir alone must stop it"
    assert not broker.readable_by(token, harness_uid + 1, [harness_uid + 1]), "other users"


def test_a_harness_running_as_the_box_user_gets_a_0600_file(shared_root: Path) -> None:
    broker.refresh(
        [app()], shared_root, now=Clock(), mint=FakeMint(),
        holders={"claude-bot": Holder(None, None)}, remotes={}, out=io.StringIO(),
    )
    token = shared_root / "claude-bot" / "maxharris1.json"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert not broker.readable_by(token, os.getuid() + 54321, [os.getgid()])


@pytest.mark.parametrize(
    ("before", "after", "mode"),
    [
        (Holder(None, None), Holder("claude-bot", os.getgid()), 0o640),
        (Holder("claude-bot", os.getgid()), Holder(None, None), 0o600),
    ],
)
def test_a_fresh_cached_token_follows_the_holders_access_without_a_new_mint(
    shared_root: Path, before: Holder, after: Holder, mode: int
) -> None:
    mint = FakeMint()
    kw = dict(now=Clock(), mint=mint, remotes={}, out=io.StringIO())
    broker.refresh([app()], shared_root, holders={"claude-bot": before}, **kw)
    broker.refresh([app()], shared_root, holders={"claude-bot": after}, **kw)
    token = shared_root / "claude-bot" / "maxharris1.json"
    assert len(mint.calls) == 1
    assert stat.S_IMODE(token.stat().st_mode) == mode
    assert broker.readable_by(token, os.getuid() + 54321, [os.getgid()]) == (mode == 0o640)


class Ssh:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bindir = tmp_path / "fake-bin"
        bindir.mkdir()
        wrapper = bindir / "ssh"
        wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_SSH} "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        self.hosts = tmp_path / "fake-hosts"
        self.hosts.mkdir()
        (self.hosts / "pc-wsl-claude-bot").write_text("claude-bot", encoding="utf-8")
        self.log = tmp_path / "ssh.log"
        self.home = tmp_path / "device-home"
        self.home.mkdir()
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_SSH_LOG", str(self.log))
        monkeypatch.setenv("FAKE_SSH_HOSTS", str(self.hosts))
        monkeypatch.setenv("FAKE_SSH_HOME", str(self.home))

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def test_a_remote_device_receives_each_refreshed_token_over_fake_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = Ssh(tmp_path, monkeypatch)
    remote = Remote("pc", "claude-bot", "pc-wsl-claude-bot")
    mint, clock, out = FakeMint(), Clock(), io.StringIO()
    args = dict(now=clock, mint=mint, holders={}, remotes={"claude-bot": [remote]}, out=out)
    box = tmp_path / "box"
    device_file = ssh.home / ".config/factory/app-tokens/maxharris1.json"

    assert broker.refresh([app()], box, **args) == 0
    assert broker.read_token(device_file).token == f"{SENTINEL}_1"
    assert stat.S_IMODE(device_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(device_file.parent.stat().st_mode) == 0o700

    clock.now = NOW + timedelta(minutes=5)
    broker.refresh([app()], box, **args)
    assert len(ssh.calls()) == 1, "a fresh token already on the device is not sent again"

    clock.now = NOW + timedelta(minutes=50)
    assert broker.refresh([app()], box, **args) == 0
    assert broker.read_token(device_file).token == f"{SENTINEL}_2"
    assert [call["user"] for call in ssh.calls()] == ["claude-bot", "claude-bot"]
    assert all(SENTINEL not in json.dumps(call) for call in ssh.calls()), "never in argv"
    assert "sent to pc" in out.getvalue() and SENTINEL not in out.getvalue()


def test_an_unreachable_device_gets_the_token_on_a_later_pass_without_a_new_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = Ssh(tmp_path, monkeypatch)
    offline = ssh.hosts / "pc-wsl-claude-bot.offline"
    offline.touch()
    remote = Remote("pc", "claude-bot", "pc-wsl-claude-bot")
    mint, clock, out = FakeMint(), Clock(), io.StringIO()
    args = dict(now=clock, mint=mint, holders={}, remotes={"claude-bot": [remote]}, out=out)

    assert broker.refresh([app()], tmp_path / "box", **args) == 1
    assert "pc unreachable; retry next pass" in out.getvalue()
    offline.unlink()
    clock.now = NOW + timedelta(minutes=5)
    assert broker.refresh([app()], tmp_path / "box", **args) == 0
    assert len(mint.calls) == 1
    device_file = ssh.home / ".config/factory/app-tokens/maxharris1.json"
    assert broker.read_token(device_file).token == f"{SENTINEL}_1"


BOTS = ("claude-bot", "codex-bot", "cursor-bot")


def _as_job_of(bot: str | None, home: Path, *argv: str, stdin: str = "") -> str:
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    if bot is not None:
        env["ORCH_JOB_BOT"] = bot
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "lib/github_app_token.py"), *argv],
        input=stdin, capture_output=True, text=True, env=env, check=True,
    )
    return done.stdout


def test_bots_sharing_one_device_login_each_keep_their_own_app_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = Ssh(tmp_path, monkeypatch)
    for bot in BOTS:
        (ssh.hosts / f"box-{bot}").write_text("box", encoding="utf-8")
    remotes = {bot: [Remote("box", "box", f"box-{bot}", bot)] for bot in BOTS}
    mint = FakeMint()
    status = broker.refresh(
        [app(bot) for bot in BOTS], tmp_path / "box-state", now=Clock(datetime.now(timezone.utc)),
        mint=mint, holders={}, remotes=remotes, out=io.StringIO(),
    )
    assert status == 0
    assert [call["user"] for call in ssh.calls()] == ["box"] * 3
    minted = {bot: f"{SENTINEL}_{index + 1}" for index, (bot, _, _) in enumerate(mint.calls)}
    assert sorted(minted) == sorted(BOTS)

    tokens = ssh.home / ".config/factory/app-tokens"
    fake_gh = tmp_path / "gh"
    seen = tmp_path / "seen"
    fake_gh.write_text(f'#!/bin/sh\nprintf %s "$GH_TOKEN" >{seen}\n', encoding="utf-8")
    fake_gh.chmod(0o755)
    request = "protocol=https\nhost=github.com\npath=maxharris1/demo\n\n"
    for bot in BOTS:
        answer = _as_job_of(bot, ssh.home, "credential", "--dir", str(tokens), "get", stdin=request)
        assert answer == f"username=x-access-token\npassword={minted[bot]}\n", bot
        _as_job_of(bot, ssh.home, "gh", "--dir", str(tokens), "--gh", str(fake_gh), "--",
                   "api", "repos/maxharris1/demo")
        assert seen.read_text() == minted[bot], bot
    assert _as_job_of(None, ssh.home, "credential", "--dir", str(tokens), "get", stdin=request) == "", (
        "a job that names no bot gets no bot's token"
    )


def test_a_device_whose_login_changes_gets_the_current_token_without_a_new_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = Ssh(tmp_path, monkeypatch)
    host = ssh.hosts / "box-claude-bot"
    host.write_text("claude-bot", encoding="utf-8")
    mint, clock = FakeMint(), Clock()
    args = dict(now=clock, mint=mint, holders={}, out=io.StringIO())
    box = tmp_path / "box-state"
    unshared = Remote("box", "claude-bot", "box-claude-bot")
    assert broker.refresh([app()], box, remotes={"claude-bot": [unshared]}, **args) == 0

    host.write_text("box", encoding="utf-8")
    clock.now = NOW + timedelta(minutes=5)
    shared = Remote("box", "box", "box-claude-bot", "claude-bot")
    assert broker.refresh([app()], box, remotes={"claude-bot": [shared]}, **args) == 0
    assert len(mint.calls) == 1
    assert [call["user"] for call in ssh.calls()] == ["claude-bot", "box"]
    device_file = ssh.home / ".config/factory/app-tokens/claude-bot/maxharris1.json"
    assert broker.read_token(device_file).token == f"{SENTINEL}_1"
    assert stat.S_IMODE(device_file.parent.stat().st_mode) == 0o700


def test_the_git_credential_helper_answers_with_the_owners_token(tmp_path: Path) -> None:
    broker.write_token(broker.token_path(tmp_path, "maxharris1"), issued(10, f"{SENTINEL}_MAX"), None)
    broker.write_token(broker.token_path(tmp_path, "aut0-code"), issued(10, f"{SENTINEL}_AUT0"), None)

    def get(request: str) -> str:
        out = io.StringIO()
        broker.credential(tmp_path, "get", io.StringIO(request), out, NOW)
        return out.getvalue()

    answer = get("protocol=https\nhost=github.com\npath=aut0-code/app.git\n\n")
    assert answer == f"username=x-access-token\npassword={SENTINEL}_AUT0\n"
    assert get("protocol=https\nhost=github.com\n\n") == "", "two owners and no path: no guess"
    assert get("protocol=https\nhost=example.com\npath=maxharris1/x.git\n\n") == ""
    assert get("protocol=https\nhost=github.com\npath=stranger/x.git\n\n") == ""
    expired = NOW + timedelta(minutes=51)
    out = io.StringIO()
    broker.credential(tmp_path, "get", io.StringIO("protocol=https\nhost=github.com\npath=maxharris1/x\n"), out, expired)
    assert out.getvalue() == ""


def test_gh_gets_the_token_for_the_repos_owner_and_no_inherited_token(tmp_path: Path) -> None:
    broker.write_token(broker.token_path(tmp_path, "maxharris1"), issued(10, f"{SENTINEL}_MAX"), None)
    environ = {"PATH": "/usr/bin", "GITHUB_TOKEN": "inherited", "GH_ENTERPRISE_TOKEN": "x"}

    env = broker.gh_env(tmp_path, ["api", "repos/maxharris1/factory/issues/1/comments"], environ, NOW)
    assert env == {"PATH": "/usr/bin", "GH_TOKEN": f"{SENTINEL}_MAX"}
    assert broker.gh_env(tmp_path, ["pr", "view", "-R", "MaxHarris1/factory"], environ, NOW)["GH_TOKEN"]
    assert "GH_TOKEN" not in broker.gh_env(tmp_path, ["pr", "view", "--repo=other/x"], environ, NOW)


def test_gh_finds_the_owner_from_the_checkouts_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/maxharris1/factory.git"],
        check=True,
    )
    assert broker.owner_from_origin(str(repo)) == "maxharris1"
    assert broker.owner_from_args(["pr", "view", "284"]) is None


@pytest.mark.parametrize(
    "args",
    [
        ["repo", "clone", "aut0-code/project"],
        ["repo", "clone", "aut0-code/project", "work/dir"],
        ["repo", "view", "https://github.com/aut0-code/project"],
        ["pr", "view", "https://github.com/aut0-code/project/pull/7"],
        ["pr", "checkout", "github.com/aut0-code/project/pull/7"],
        ["issue", "view", "https://github.com/aut0-code/project/issues/3", "--json", "state"],
        ["api", "graphql", "-f", "query=q", "-f", "owner=aut0-code", "-f", "name=project"],
        ["api", "graphql", "-F", "owner=aut0-code", "-F", "name=project"],
        ["api", "graphql", "--raw-field", "owner=aut0-code"],
        ["api", "graphql", "--field=owner=aut0-code"],
        ["api", "graphql", "-fowner=aut0-code"],
        ["api", "/repos/aut0-code/project/pulls"],
        ["api", "orgs/aut0-code/repos"],
        ["api", "https://api.github.com/repos/aut0-code/project"],
        ["pr", "list", "-R", "github.com/aut0-code/project"],
        ["pr", "list", "--repo", "https://github.com/aut0-code/project"],
        ["pr", "list", "-Raut0-code/project"],
        ["pr", "list", "-Rgithub.com/aut0-code/project"],
        ["pr", "list", "-R=aut0-code/project"],
    ],
)
def test_gh_uses_the_installation_of_the_commands_target_over_the_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    broker.write_token(broker.token_path(tokens, "maxharris1"), issued(10, f"{SENTINEL}_MAX"), None)
    broker.write_token(broker.token_path(tokens, "aut0-code"), issued(10, f"{SENTINEL}_AUT0"), None)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert broker.gh_env(tokens, args, {}, NOW).get("GH_TOKEN") == f"{SENTINEL}_AUT0"
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/maxharris1/factory"], check=True)
    assert broker.gh_env(tokens, args, {}, NOW).get("GH_TOKEN") == f"{SENTINEL}_AUT0"
    assert broker.gh_env(tokens, ["pr", "view", "284"], {}, NOW).get("GH_TOKEN") == f"{SENTINEL}_MAX"


def _two_installations_in_a_maxharris1_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    broker.write_token(broker.token_path(tokens, "maxharris1"), issued(10, f"{SENTINEL}_MAX"), None)
    broker.write_token(broker.token_path(tokens, "aut0-code"), issued(10, f"{SENTINEL}_AUT0"), None)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/maxharris1/factory"],
        check=True,
    )
    monkeypatch.chdir(checkout)
    return tokens


@pytest.mark.parametrize("value", ["aut0-code/project", "github.com/aut0-code/project"])
def test_gh_repo_in_the_environment_names_the_installation_when_the_args_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    tokens = _two_installations_in_a_maxharris1_checkout(tmp_path, monkeypatch)
    environ = {"GH_REPO": value}
    assert broker.gh_env(tokens, ["pr", "view", "7"], environ, NOW)["GH_TOKEN"] == f"{SENTINEL}_AUT0"
    assert broker.gh_env(tokens, ["api", "graphql", "-f", "id=PR_x"], environ, NOW)["GH_TOKEN"] == f"{SENTINEL}_AUT0"
    explicit = ["pr", "view", "7", "-R", "maxharris1/factory"]
    assert broker.gh_env(tokens, explicit, environ, NOW)["GH_TOKEN"] == f"{SENTINEL}_MAX"


def test_every_gh_call_of_a_cross_owner_handoff_gets_the_targets_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = _two_installations_in_a_maxharris1_checkout(tmp_path, monkeypatch)
    head = "a" * 40
    calls: list[str] = []

    def runner(argv: list[str], timeout: float) -> GhResult:
        args = argv[argv.index("gh") + 1 :]
        graphql = "graphql" in args
        calls.append("graphql" if graphql else "rest")
        if broker.gh_env(tokens, args, {}, NOW).get("GH_TOKEN") != f"{SENTINEL}_AUT0":
            return GhResult(1, "", "installation token cannot reach aut0-code/project")
        ready = {"isDraft": False, "headRefOid": head}
        data = (
            {"data": {"markPullRequestReadyForReview": {"pullRequest": ready}}}
            if graphql
            else {"head": {"sha": head}, "state": "open", "draft": True, "node_id": "PR_kwDOKfac0s6CiTKq"}
        )
        return GhResult(0, json.dumps(data), "")

    ticket = SimpleNamespace(repo="aut0-code/project", pr_number="42")
    GitHubReviewActions(runner=runner).handoff(ticket, head)
    assert calls == ["rest", "graphql"]


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "create", "--title", "t", "--body", "https://github.com/maxharris1/factory", "-R", "aut0-code/project"],
        ["pr", "create", "--title", "https://github.com/maxharris1/factory", "--repo", "aut0-code/project"],
        ["pr", "create", "-t", "https://github.com/maxharris1/x", "-b", "b", "--repo=aut0-code/project"],
        ["pr", "create", "--body-file", "orgs/maxharris1/pr.md", "-R", "aut0-code/project"],
        ["pr", "create", "-F", "repos/maxharris1/x/pr.md", "-R", "aut0-code/project"],
        ["issue", "comment", "7", "--body", "github.com/maxharris1/factory/pull/1", "-R", "aut0-code/project"],
        ["release", "create", "v1", "--notes", "https://github.com/maxharris1/x", "-R", "aut0-code/project"],
        ["pr", "merge", "7", "--subject", "https://github.com/maxharris1/x", "-R", "aut0-code/project"],
        ["pr", "view", "https://github.com/maxharris1/x/pull/1", "-R", "aut0-code/project"],
        ["api", "repos/aut0-code/project/issues/1/comments", "-f", "body=https://github.com/maxharris1/x"],
        ["api", "--input", "orgs/maxharris1/body.json", "repos/aut0-code/project/issues/1/comments"],
        ["api", "-H", "https://github.com/maxharris1/x", "repos/aut0-code/project"],
        ["api", "--jq", "https://github.com/maxharris1/x", "repos/aut0-code/project"],
        ["api", "graphql", "-f", "owner=aut0-code", "-f", "query=https://github.com/maxharris1/x"],
        ["issue", "create", "--body", "https://github.com/maxharris1/x", "--title", "x", "-R", "aut0-code/project"],
    ],
)
def test_no_option_value_but_the_target_names_the_installation(args: list[str]) -> None:
    assert broker.owner_from_args(args) == "aut0-code"


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "create", "--title", "t", "--body", "https://github.com/maxharris1/factory"],
        ["pr", "create", "--body-file", "orgs/maxharris1/pr.md"],
        ["pr", "create", "-F", "repos/maxharris1/x/pr.md"],
        ["pr", "comment", "7", "--body", "https://github.com/maxharris1/factory/pull/1"],
        ["pr", "view", "7", "--json", "url", "--jq", "https://github.com/maxharris1/x"],
        ["pr", "create", "--head", "orgs/maxharris1/x"],
    ],
)
def test_option_values_without_a_target_leave_the_owner_to_the_checkout(args: list[str]) -> None:
    assert broker.owner_from_args(args) is None


def test_gh_reads_no_owner_from_values_that_name_no_repo() -> None:
    assert broker.owner_from_args(["pr", "create", "--body-file", "notes/x.md"]) is None
    assert broker.owner_from_args(["api", "graphql", "-f", "query=a/b"]) is None
    assert broker.owner_from_args(["pr", "view", "284", "--json", "url"]) is None


def test_the_gh_wrapper_runs_gh_with_the_token_in_its_environment_only(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    now = datetime.now(timezone.utc)
    live = Stored(f"{SENTINEL}_MAX", now, now + timedelta(hours=1))
    broker.write_token(broker.token_path(tokens, "maxharris1"), live, None)
    fake_gh = tmp_path / "gh"
    seen = tmp_path / "seen"
    fake_gh.write_text(
        f'#!/bin/sh\n[ "$GH_TOKEN" = "{SENTINEL}_MAX" ] && echo ok >{seen}\necho "args:$*"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in broker.TOKEN_VARIABLES}
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "lib/github_app_token.py"), "gh", "--dir", str(tokens),
         "--gh", str(fake_gh), "--", "api", "repos/maxharris1/factory"],
        capture_output=True, text=True, env=env, cwd=tmp_path, check=True,
    )
    assert done.stdout == "args:api repos/maxharris1/factory\n"
    assert seen.read_text() == "ok\n"
    assert SENTINEL not in done.stdout + done.stderr


def test_the_app_jwt_is_rs256_signed_by_the_key_and_verifies(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    public = tmp_path / "app.pub"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
        check=True, capture_output=True,
    )
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True)

    token = broker.app_jwt(222222, key, NOW)
    header, body, signature = token.split(".")
    pad = lambda part: part + "=" * (-len(part) % 4)  # noqa: E731
    import base64

    claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {"alg": "RS256", "typ": "JWT"}
    assert claims == {
        "iat": int(NOW.timestamp()) - 60,
        "exp": int(NOW.timestamp()) + 540,
        "iss": "222222",
    }
    sig = tmp_path / "sig"
    sig.write_bytes(base64.urlsafe_b64decode(pad(signature)))
    verify = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
        input=f"{header}.{body}".encode(), capture_output=True,
    )
    assert verify.returncode == 0


def test_github_mint_posts_as_the_app_and_reports_refusals_without_the_body() -> None:
    import urllib.error

    seen: dict = {}

    class Response:
        def __init__(self, body: dict) -> None:
            self.body = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    def ok(request, timeout):
        seen["url"], seen["method"] = request.full_url, request.get_method()
        seen["auth"] = request.get_header("Authorization")
        return Response({"token": SENTINEL, "expires_at": "2026-11-14T13:00:00Z"})

    stored = broker.github_mint(app(), 2000, NOW, urlopen=ok, sign=lambda key, data: b"sig")
    assert seen["url"] == "https://api.github.com/app/installations/2000/access_tokens"
    assert seen["method"] == "POST" and seen["auth"].startswith("Bearer ")
    assert stored == Stored(SENTINEL, NOW, NOW + timedelta(hours=1))

    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"secret body"))

    with pytest.raises(broker.BrokerError) as caught:
        broker.github_mint(app(), 2000, NOW, urlopen=refused, sign=lambda key, data: b"sig")
    assert str(caught.value) == "GitHub refused the token for claude-bot (HTTP 401)"


def test_an_owner_that_is_not_a_login_never_becomes_a_path_or_script() -> None:
    for owner in ("../etc", "a b", "x';rm -rf ~;'", ""):
        with pytest.raises(broker.BrokerError):
            broker.push_script(owner)
        with pytest.raises(broker.BrokerError):
            broker.token_path(Path("/tmp"), owner)


def test_a_bot_name_that_is_not_plain_never_becomes_a_path_or_script(tmp_path: Path) -> None:
    for bot in ("../etc", "a b", "x';rm -rf ~;'", "", "Claude-bot"):
        with pytest.raises(broker.BrokerError):
            broker.push_script("maxharris1", bot)
    tokens = tmp_path / "tokens"
    (tokens / "claude-bot").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    assert broker.bot_directory(tokens, {"ORCH_JOB_BOT": "../elsewhere"}) == tokens
    assert broker.bot_directory(tokens, {"ORCH_JOB_BOT": "codex-bot"}) == tokens, "no such bot directory"
    assert broker.bot_directory(tokens, {"ORCH_JOB_BOT": "claude-bot"}) == tokens / "claude-bot"


# --- the refresh command, the timer installer and the harness installer ------

SERVICE = ROOT / "deploy" / "app-token-service.sh"


def _no_such_user(name: str):
    raise KeyError(name)


def test_refresh_reads_orch_and_machines_and_sends_to_enabled_remote_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from tests.test_github_apps_config import orch_text

    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=False), encoding="utf-8")
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 3\n"
        "    harnesses: {claude: claude-bot, codex: codex-bot}\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 2\n"
        "    harnesses: {claude: claude-bot}\n    workdir_root: /home/{bot}/factory-work\n"
        "    ssh_alias: pc-{bot}\n    factory_root: /home/{bot}/factory\n"
        "  - name: spare\n    enabled: false\n    tailscale_name: spare.example.ts.net\n    slot_max: 1\n"
        "    harnesses: {claude: claude-bot}\n    workdir_root: /home/{bot}/w\n"
        "    ssh_alias: spare-{bot}\n    factory_root: /home/{bot}/factory\n",
        encoding="utf-8",
    )
    mint, pushes = FakeMint(), []
    monkeypatch.setattr(broker, "github_mint", mint)
    monkeypatch.setattr(broker, "ssh_push", lambda remote, owner, payload: pushes.append((remote, owner)) or True)
    monkeypatch.setattr("pwd.getpwnam", _no_such_user)
    root = tmp_path / "tokens"

    status = broker.main(["refresh", "--orch", str(orch), "--machines", str(machines), "--token-root", str(root)])

    assert status == 0
    assert len(mint.calls) == 8, "four Apps, two installations each"
    assert sorted(p.name for p in (root / "claude-bot").glob("*.json")) == ["maxharris1.json", "owner.json"]
    assert stat.S_IMODE(root.stat().st_mode) == 0o711
    assert sorted(pushes, key=lambda p: p[1]) == [
        (Remote("pc", "claude-bot", "pc-claude-bot"), "maxharris1"),
        (Remote("pc", "claude-bot", "pc-claude-bot"), "owner"),
    ]
    printed = capsys.readouterr().out
    assert "claude-bot maxharris1: minted" in printed
    assert SENTINEL not in printed


def test_refresh_sends_to_a_shared_device_login_with_each_bots_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.pool_helpers import write_target_machines
    from tests.test_github_apps_config import orch_text

    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=False), encoding="utf-8")
    machines = write_target_machines(tmp_path)
    pushes: list[Remote] = []
    monkeypatch.setattr(broker, "github_mint", FakeMint())
    monkeypatch.setattr(broker, "ssh_push", lambda remote, owner, payload: pushes.append(remote) or True)

    status = broker.main(["refresh", "--orch", str(orch), "--machines", str(machines),
                          "--token-root", str(tmp_path / "tokens")])

    assert status == 0
    assert set(pushes) == {
        *(Remote("box", "box", f"box-{bot}", bot) for bot in BOTS),
        *(Remote("pc", bot, f"pc-wsl-{bot}") for bot in BOTS),
    }


def test_refresh_reads_an_inventory_whose_disabled_devices_hold_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_github_apps_config import orch_text

    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=False), encoding="utf-8")
    inventory = LOCAL_BOX_MACHINES.read_text(encoding="utf-8")
    assert "REPLACE_WITH_" in inventory
    machines = tmp_path / "machines.yml"
    machines.write_text(
        inventory.replace("name: pc\n    enabled: false", "name: pc\n    enabled: true")
        + "  - name: half\n    tailscale_name: REPLACE_WITH_HALF_TAILSCALE\n    slot_max: 1\n"
        "    harnesses: {claude: claude-bot}\n    workdir_root: /home/{bot}/w\n"
        "    ssh_alias: half-{bot}\n    factory_root: /home/{bot}/factory\n",
        encoding="utf-8",
    )
    readers: list[dict[str, str]] = []
    pushes: list[tuple[Remote, str]] = []
    monkeypatch.setattr(broker, "github_mint", FakeMint())
    monkeypatch.setattr(broker, "box_holders", lambda bots, users: readers.append(dict(users)) or {})
    monkeypatch.setattr(broker, "ssh_push", lambda remote, owner, payload: pushes.append((remote, owner)) or True)

    status = broker.main(["refresh", "--orch", str(orch), "--machines", str(machines),
                          "--token-root", str(tmp_path / "tokens")])

    assert status == 0
    assert readers == [{"claude-bot": "claude-bot", "codex-bot": "codex-bot", "cursor-bot": "cursor-bot"}]
    assert {remote.device for remote, _ in pushes} == {"pc"}
    assert sorted((remote.alias, owner) for remote, owner in pushes) == sorted(
        (f"pc-wsl-{bot}", owner) for bot in ("claude-bot", "codex-bot", "cursor-bot") for owner in ("maxharris1", "owner")
    )


def test_refresh_still_mints_on_the_box_when_machines_yml_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from tests.test_github_apps_config import orch_text

    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=False), encoding="utf-8")
    monkeypatch.setattr(broker, "github_mint", FakeMint())
    root = tmp_path / "tokens"
    status = broker.main(["refresh", "--orch", str(orch), "--machines", str(tmp_path / "none.yml"),
                          "--token-root", str(root)])
    assert status == 1
    assert (root / "grok-bot" / "owner.json").exists()
    assert "no remote device gets a token" in capsys.readouterr().out


def test_refresh_does_nothing_without_github_app(tmp_path: Path) -> None:
    from tests.test_github_apps_config import orch_text

    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=None), encoding="utf-8")
    root = tmp_path / "tokens"
    assert broker.main(["refresh", "--orch", str(orch), "--machines", str(tmp_path / "none.yml"),
                        "--token-root", str(root)]) == 0
    assert not root.exists()


def _service_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_CONFIG_HOME", *broker.TOKEN_VARIABLES)}
    bindir = tmp_path / "fake-bin"
    bindir.mkdir(exist_ok=True)
    env.update(
        HOME=str(tmp_path / "home"),
        PATH=f"{bindir}:/usr/bin:/bin",
        ORCH_SYSTEMCTL=str(bindir / "no-such-systemctl"),
        GIT_CONFIG_NOSYSTEM="1",
    )
    env.update(extra)
    Path(env["HOME"]).mkdir(exist_ok=True)
    return env


def _script(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_install_autostart_puts_the_broker_on_a_systemd_timer(tmp_path: Path) -> None:
    log = tmp_path / "systemctl.log"
    systemctl = _script(tmp_path / "systemctl", f'#!/bin/sh\necho "$*" >>{log}\n')
    env = _service_env(tmp_path, ORCH_SYSTEMCTL=str(systemctl))

    done = subprocess.run([str(SERVICE), "install-autostart"], env=env, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    units = tmp_path / "home/.config/systemd/user"
    assert f"ExecStart={ROOT}/deploy/app-token-service.sh once" in (units / "factory-app-token.service").read_text()
    assert "OnUnitActiveSec=5min" in (units / "factory-app-token.timer").read_text()
    assert "--user enable --now factory-app-token.timer" in log.read_text().splitlines()
    assert not (tmp_path / "home/.config/factory/app-token-autostart").exists()


def test_without_systemd_each_self_heal_pass_runs_the_broker(tmp_path: Path) -> None:
    calls = tmp_path / "python.log"
    python = _script(tmp_path / "python", f'#!/bin/sh\necho "$*" >>{calls}\n')
    orch = _script(tmp_path / "orch", "#!/bin/sh\nexit 0\n")
    env = _service_env(tmp_path, ORCH_PYTHON=str(python), ORCH_BIN=str(orch))

    done = subprocess.run([str(SERVICE), "install-autostart"], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "home/.config/factory/app-token-autostart").exists()

    heal = subprocess.run([str(ROOT / "deploy/self-heal.sh"), "once"], env=env, capture_output=True, text=True)

    assert heal.returncode == 0, heal.stderr
    state = tmp_path / "home/.local/state/factory"
    assert calls.read_text().split() == [
        "-m", "lib.github_app_token", "refresh", "--orch", f"{ROOT}/orch.yml",
        "--machines", f"{ROOT}/machines.yml", "--token-root", f"{state}/app-tokens",
    ]
    subprocess.run([str(SERVICE), "remove-autostart"], env=env, check=True, capture_output=True)
    assert not (tmp_path / "home/.config/factory/app-token-autostart").exists()


def test_install_harness_routes_gh_and_git_through_the_token_dir(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    now = datetime.now(timezone.utc)
    broker.write_token(
        broker.token_path(tokens, "maxharris1"), Stored(f"{SENTINEL}_MAX", now, now + timedelta(hours=1)), None
    )
    seen = tmp_path / "seen"
    real_gh = _script(
        tmp_path / "real-gh", f'#!/bin/sh\n[ "$GH_TOKEN" = "{SENTINEL}_MAX" ] && echo "$*" >{seen}\nexit 0\n'
    )
    env = _service_env(tmp_path, ORCH_REAL_GH=str(real_gh))

    done = subprocess.run([str(SERVICE), "install-harness", str(tokens)], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    wrapper = tmp_path / "home/.local/bin/gh"
    subprocess.run(["env", "-u", "GH_TOKEN", str(wrapper), "api", "repos/maxharris1/factory"],
                   env=env, check=True, cwd=tmp_path)
    assert seen.read_text() == "api repos/maxharris1/factory\n"

    fill = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=maxharris1/factory.git\n\n",
        env={**env, "GIT_TERMINAL_PROMPT": "0"}, capture_output=True, text=True, cwd=tmp_path,
    )
    assert fill.returncode == 0
    assert f"password={SENTINEL}_MAX" in fill.stdout.splitlines()
    assert SENTINEL not in done.stdout + done.stderr

    _script(wrapper, "#!/bin/sh\necho someone else's\n")
    again = subprocess.run([str(SERVICE), "install-harness", str(tokens)], env=env, capture_output=True, text=True)
    assert again.returncode == 1 and "refusing to overwrite" in again.stderr


def test_with_one_owner_the_helpers_need_no_path_and_skip_broker_files(tmp_path: Path) -> None:
    broker.write_token(broker.token_path(tmp_path, "maxharris1"), issued(10, f"{SENTINEL}_MAX"), None)
    (tmp_path / broker.PUSHED_FILE).write_text("{}", encoding="utf-8")
    (tmp_path / ".maxharris1.json.tmp.json").write_text("{}", encoding="utf-8")
    assert broker.select_token(tmp_path, None, NOW) == f"{SENTINEL}_MAX"


def test_only_the_configured_worker_apps_get_tokens_and_a_bot_with_no_app_gets_none(
    tmp_path: Path,
) -> None:
    from lib.orch_yml import load_orch_yml
    from tests.test_github_apps_config import two_worker_apps

    orch = tmp_path / "orch.yml"
    orch.write_text(two_worker_apps(), encoding="utf-8")
    config, findings = load_orch_yml(orch)
    assert config is not None and config.github_app is not None, findings
    bots = ("grok-bot", "claude-bot", "codex-bot", "cursor-bot")
    mint, clock, pushes = FakeMint(), Clock(), []
    args = dict(
        now=clock,
        mint=mint,
        holders={bot: Holder(None, None) for bot in bots},
        remotes={bot: [Remote("pc", bot, f"pc-{bot}")] for bot in bots},
        push=lambda remote, owner, payload: pushes.append(remote.user) or True,
        out=io.StringIO(),
    )
    root = tmp_path / "tokens"

    assert broker.refresh(config.github_app.apps.values(), root, **args) == 0
    assert sorted(bot for bot, _, _ in mint.calls) == ["claude-bot", "claude-bot", "codex-bot", "codex-bot"]
    assert sorted(path.name for path in root.iterdir()) == ["claude-bot", "codex-bot"]
    assert sorted(set(pushes)) == ["claude-bot", "codex-bot"]

    clock.now = NOW + timedelta(hours=1) - broker.REFRESH_MARGIN
    assert broker.refresh(config.github_app.apps.values(), root, **args) == 0
    assert sorted(bot for bot, _, _ in mint.calls[4:]) == ["claude-bot", "claude-bot", "codex-bot", "codex-bot"]

    no_app = root / "cursor-bot"
    environ = {"PATH": "/usr/bin", "GH_TOKEN": "inherited"}
    assert broker.gh_env(no_app, ["api", "repos/maxharris1/factory"], environ, clock.now) == {"PATH": "/usr/bin"}
    answer = io.StringIO()
    request = io.StringIO("protocol=https\nhost=github.com\npath=maxharris1/factory.git\n\n")
    assert broker.credential(no_app, "get", request, answer, clock.now) == 0
    assert answer.getvalue() == ""


def _broker_token_root(env: dict[str, str]) -> Path:
    assignments = "\n".join(
        line for line in SERVICE.read_text(encoding="utf-8").splitlines()
        if line.startswith(("STATE=", "TOKEN_ROOT="))
    )
    done = subprocess.run(
        ["/bin/bash", "-c", f'{assignments}\nprintf "%s" "$TOKEN_ROOT"'],
        env=env, capture_output=True, text=True, check=True,
    )
    return Path(done.stdout)


@pytest.mark.parametrize(
    "extra",
    [
        {"ORCH_STATE_DIR": "/tmp/review-state"},
        {"ORCH_APP_TOKEN_ROOT": "/tmp/review-tokens", "ORCH_STATE_DIR": "/tmp/review-state"},
        {},
        {"ORCH_STATE_DIR": ""},
        {"ORCH_STATE_DIR": "/tmp/review-state", "ORCH_APP_TOKEN_ROOT": ""},
    ],
)
def test_the_poller_read_helper_uses_the_brokers_token_root(
    extra: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"HOME": "/tmp/review-home", "PATH": "/usr/bin:/bin", **extra}
    monkeypatch.setattr(broker.shutil, "which", lambda *args, **kwargs: "/fake/gh")
    prefix = broker.reads_prefix((), "claude-bot", env)
    assert prefix[prefix.index("--dir") + 1] == str(_broker_token_root(env) / "claude-bot")


def test_refresh_writes_under_the_state_dir_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from tests.test_github_apps_config import orch_text

    orch = tmp_path / "orch.yml"
    orch.write_text(orch_text(enabled=False), encoding="utf-8")
    monkeypatch.setattr(broker, "github_mint", FakeMint())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ORCH_APP_TOKEN_ROOT", raising=False)
    broker.main(["refresh", "--orch", str(orch), "--machines", str(tmp_path / "none.yml")])
    assert (tmp_path / "state" / "app-tokens" / "grok-bot" / "owner.json").exists()
