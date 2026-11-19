import builtins
import io
import os
import stat
from pathlib import Path

import pytest

from lib import cli
from lib.account_home import CLAUDE_LINKS, Refused, prepare
from lib.machines_yml import load_machines_yml

MACHINES = """\
orch_host: pc
accounts:
  - {{id: claude-b, harness: claude, signed_in_on: [pc]}}
  - {{id: claude-c, harness: claude, signed_in_on: [pc]}}
  - {{id: codex-b, harness: codex, signed_in_on: [pc]}}
devices:
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 2
    harnesses: {{claude: claude-bot, codex: codex-bot}}
    workdir_root: /home/{{bot}}/work
    ssh_alias: pc-{{bot}}
    factory_root: /srv/factory
    home: {root}/{{bot}}
"""


@pytest.fixture
def machines(tmp_path: Path) -> Path:
    path = tmp_path / "machines.yml"
    path.write_text(MACHINES.format(root=tmp_path), encoding="utf-8")
    return path


@pytest.fixture
def config(machines: Path):
    loaded, findings = load_machines_yml(machines, overlay=False)
    assert loaded is not None, findings
    return loaded


def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude-bot"
    (home / ".claude" / "skills").mkdir(parents=True)
    return home


def snapshot(folder: Path) -> dict[str, tuple[int, int, int]]:
    seen = {}
    for path in [folder, *sorted(folder.iterdir())]:
        info = os.lstat(path)
        seen[path.name] = (info.st_ino, info.st_mtime_ns, info.st_mode)
    return seen


def test_fresh_home_makes_a_700_folder_linked_into_claude(tmp_path: Path, config) -> None:
    home = claude_home(tmp_path)

    changes = prepare(config, "pc", "claude-bot", home)

    for account in ("claude-b", "claude-c"):
        folder = home / ".factory-accounts" / account
        assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
        for name in ("skills", "agents", "docs", "rules", "settings.json"):
            link = folder / name
            assert link.is_symlink(), name
            assert os.readlink(link) == str(home / ".claude" / name)
    assert set(CLAUDE_LINKS) == {"skills", "agents", "docs", "rules", "settings.json"}
    assert stat.S_IMODE(os.lstat(home / ".factory-accounts").st_mode) == 0o700
    assert {change.action for change in changes} == {"made", "linked"}
    assert not (tmp_path / "codex-bot").exists()


def test_second_run_changes_nothing(tmp_path: Path, config) -> None:
    home = claude_home(tmp_path)
    prepare(config, "pc", "claude-bot", home)
    folder = home / ".factory-accounts" / "claude-b"
    before = snapshot(folder)

    assert prepare(config, "pc", "claude-bot", home) == []
    assert snapshot(folder) == before


def test_prepare_never_opens_or_lists_inside_the_account_folder(
    tmp_path: Path, config, monkeypatch
) -> None:
    home = claude_home(tmp_path)
    folder = home / ".factory-accounts" / "claude-b"
    folder.mkdir(parents=True, mode=0o700)
    secret = folder / ".credentials.json"
    secret.write_bytes(b"do-not-read")
    touched: list[str] = []

    def spy(name, real):
        def call(target, *args, **kwargs):
            touched.append(f"{name} {os.fspath(target)}")
            return real(target, *args, **kwargs)

        return call

    monkeypatch.setattr(builtins, "open", spy("open", builtins.open))
    monkeypatch.setattr(io, "open", spy("io.open", io.open))
    monkeypatch.setattr(os, "open", spy("os.open", os.open))
    monkeypatch.setattr(os, "scandir", spy("scandir", os.scandir))
    monkeypatch.setattr(os, "listdir", spy("listdir", os.listdir))

    prepare(config, "pc", "claude-bot", home)
    prepare(config, "pc", "claude-bot", home)

    monkeypatch.undo()
    accounts = str(home / ".factory-accounts")
    assert [entry for entry in touched if accounts in entry] == []
    assert secret.read_bytes() == b"do-not-read"


def test_another_user_is_refused_and_nothing_is_made(tmp_path: Path, config) -> None:
    home = claude_home(tmp_path)

    with pytest.raises(Refused, match="mallory"):
        prepare(config, "pc", "mallory", home)

    assert not (home / ".factory-accounts").exists()


def test_a_home_other_than_the_configured_one_is_refused(tmp_path: Path, config) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()

    with pytest.raises(Refused, match="home"):
        prepare(config, "pc", "claude-bot", other)

    assert not (other / ".factory-accounts").exists()
    assert not (tmp_path / "claude-bot").exists()


def test_a_regular_settings_file_is_reported_and_kept(tmp_path: Path, config) -> None:
    home = claude_home(tmp_path)
    folder = home / ".factory-accounts" / "claude-b"
    folder.mkdir(parents=True, mode=0o700)
    settings = folder / "settings.json"
    settings.write_bytes(b'{"mine": true}')

    changes = prepare(config, "pc", "claude-bot", home, account="claude-b")

    conflicts = [change for change in changes if change.action == "conflict"]
    assert [change.path for change in conflicts] == [str(settings)]
    assert not settings.is_symlink()
    assert settings.read_bytes() == b'{"mine": true}'
    assert (folder / "skills").is_symlink()


def test_a_wrong_link_is_reported_and_kept(tmp_path: Path, config) -> None:
    home = claude_home(tmp_path)
    folder = home / ".factory-accounts" / "claude-b"
    folder.mkdir(parents=True, mode=0o700)
    os.symlink("/somewhere/else", folder / "skills")

    changes = prepare(config, "pc", "claude-bot", home, account="claude-b")

    assert [(c.action, c.path) for c in changes if c.action == "conflict"] == [
        ("conflict", str(folder / "skills"))
    ]
    assert os.readlink(folder / "skills") == "/somewhere/else"


def test_a_loose_folder_mode_is_tightened(tmp_path: Path, config) -> None:
    home = claude_home(tmp_path)
    folder = home / ".factory-accounts" / "claude-b"
    folder.mkdir(parents=True)
    os.chmod(folder, 0o755)

    changes = prepare(config, "pc", "claude-bot", home, account="claude-b")

    assert ("mode", str(folder)) in [(c.action, c.path) for c in changes]
    assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700


def test_codex_gets_only_the_folder(tmp_path: Path, config) -> None:
    home = tmp_path / "codex-bot"
    home.mkdir()

    prepare(config, "pc", "codex-bot", home)

    folder = home / ".factory-accounts" / "codex-b"
    assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
    assert not any(os.path.lexists(folder / name) for name in CLAUDE_LINKS)
    assert not (home / ".factory-accounts" / "claude-b").exists()


def test_account_option_limits_the_run(tmp_path: Path, config) -> None:
    home = claude_home(tmp_path)

    prepare(config, "pc", "claude-bot", home, account="claude-c")

    assert (home / ".factory-accounts" / "claude-c").is_dir()
    assert not (home / ".factory-accounts" / "claude-b").exists()


@pytest.mark.parametrize(
    "account", ["codex-b", "claude-default", "claude-z"], ids=["other-login", "default", "unknown"]
)
def test_an_account_this_login_does_not_prepare_is_refused(
    tmp_path: Path, config, account: str
) -> None:
    home = claude_home(tmp_path)

    with pytest.raises(Refused):
        prepare(config, "pc", "claude-bot", home, account=account)

    assert not (home / ".factory-accounts").exists()


def test_unknown_device_is_refused(tmp_path: Path, config) -> None:
    with pytest.raises(Refused, match="box"):
        prepare(config, "box", "claude-bot", claude_home(tmp_path))


def run_cli(tmp_path: Path, machines: Path, monkeypatch, user: str, *extra: str) -> int:
    monkeypatch.setattr("getpass.getuser", lambda: user)
    monkeypatch.setenv("HOME", str(tmp_path / user))
    return cli.main(["accounts", "prepare", "--device", "pc", "--machines", str(machines), *extra])


def test_cli_prepares_and_then_reports_nothing_to_change(
    tmp_path: Path, machines: Path, monkeypatch, capsys
) -> None:
    claude_home(tmp_path)

    assert run_cli(tmp_path, machines, monkeypatch, "claude-bot") == 0
    first = capsys.readouterr().out
    assert run_cli(tmp_path, machines, monkeypatch, "claude-bot") == 0
    second = capsys.readouterr().out

    assert "linked" in first and "claude-b" in first
    assert second == "ok: nothing to change\n"


def test_cli_exits_2_for_another_user(tmp_path: Path, machines: Path, monkeypatch, capsys) -> None:
    (tmp_path / "mallory").mkdir()

    assert run_cli(tmp_path, machines, monkeypatch, "mallory") == 2

    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "mallory" / ".factory-accounts").exists()


def test_cli_exits_1_on_a_conflict(tmp_path: Path, machines: Path, monkeypatch, capsys) -> None:
    home = claude_home(tmp_path)
    folder = home / ".factory-accounts" / "claude-b"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "settings.json").write_bytes(b"{}")

    assert run_cli(tmp_path, machines, monkeypatch, "claude-bot", "--account", "claude-b") == 1

    assert "conflict" in capsys.readouterr().out


def test_help_lists_accounts_prepare(capsys) -> None:
    assert cli.main(["--help"]) == 0

    out = capsys.readouterr().out
    assert "\n  accounts " in out
    assert "accounts prepare --device NAME [--account ID] [--machines PATH]" in out
