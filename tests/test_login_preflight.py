"""The waiter preflights the harness login before it launches a job (#1334).

A placed run whose `claude auth status` (or `codex login status`) says not
logged in never starts. The waiter holds that account@device pair for the
doctor (lib/account_doctor.py) and requeues the job with no round, attempt or
free requeue charged, so a bad login never burns a ticket retry or sticks the
ticket job-failed-2.
"""

from __future__ import annotations

import contextlib
import shlex
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from lib import account_doctor
from lib.job_state import MAX_FREE_REQUEUES, free_requeues
from lib.machines_yml import load_machines_yml
from lib.remote import SSH_UNREACHABLE, Exchanged
from tests.test_account_doctor import SIGNED_OUT_CLAUDE, claude_reply, framed
from tests.test_skill_preflight import with_job_events
from tests.test_waiter import FakeSpawn, enqueue, slots, state
from tests.test_waiter_remote import make_waiter, remote_machines


@pytest.fixture
def _login_preflight():
    """The real login check, over the suite-wide no-op in tests/conftest.py."""


class LoginTransport:
    def __init__(self, reply: bytes, code: int = 0) -> None:
        self.reply = reply
        self.code = code
        self.aliases: list[str] = []
        self.scripts: list[str] = []

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        return 0

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        self.aliases.append(argv[-2])
        self.scripts.append(argv[-1])
        return Exchanged(self.code, self.reply, "")


def pc_config(tmp_path: Path):
    config, findings = load_machines_yml(remote_machines(tmp_path, box_enabled=False))
    assert config is not None, findings
    return config


def check(tmp_path: Path, harness: str, transport: LoginTransport) -> None:
    config = pc_config(tmp_path)
    account = config.device("pc").account_for(harness)
    account_doctor.check_login(transport, config, config.device("pc"), harness, account, {})


@pytest.mark.parametrize("harness, reply", [
    ("claude", framed((0, 1, SIGNED_OUT_CLAUDE))),
    ("codex", framed((0, 1, b"Not logged in\n"))),
], ids=["claude", "codex"])
def test_a_signed_out_login_raises_signed_out(tmp_path: Path, harness: str, reply: bytes) -> None:
    transport = LoginTransport(reply)
    with pytest.raises(account_doctor.SignedOut) as raised:
        check(tmp_path, harness, transport)
    assert transport.aliases == [f"fakepc-{harness}-bot"]
    assert account_doctor._STATUS[harness].command in transport.scripts[0]
    assert raised.value.device == "pc"
    assert raised.value.account == pc_config(tmp_path).device("pc").account_for(harness)


@pytest.mark.parametrize("harness, transport", [
    ("claude", LoginTransport(framed((0, 0, claude_reply())))),
    ("codex", LoginTransport(framed((0, 0, b"Logged in using ChatGPT\n")))),
    ("claude", LoginTransport(b"", code=SSH_UNREACHABLE)),
    ("claude", LoginTransport(framed((0, 0, b"not json")))),
    ("claude", LoginTransport(b"no frame at all")),
], ids=["claude-ok", "codex-ok", "unreachable", "not-understood", "no-reply"])
def test_a_login_not_read_as_signed_out_passes(tmp_path: Path, harness: str, transport: LoginTransport) -> None:
    check(tmp_path, harness, transport)
    assert len(transport.aliases) == 1


def test_a_harness_without_a_status_command_is_not_checked(tmp_path: Path) -> None:
    transport = LoginTransport(b"")
    check(tmp_path, "cursor", transport)
    assert transport.aliases == []


def pc_waiter(tmp_path: Path, transport: LoginTransport):
    spawn = FakeSpawn()
    log: list[str] = []
    waiter, queue, db = make_waiter(
        tmp_path, remote_machines(tmp_path, box_enabled=False), spawn, log=log, transport=transport
    )
    with_job_events(db)
    return waiter, queue, db, spawn, log


def events_of(db: Path, job_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT event, detail FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()


def counters(db: Path, job_id: str) -> tuple[int, int]:
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT attempt, generation FROM jobs WHERE id = ?", (job_id,)).fetchone()


def holds(db: Path) -> set[tuple[str, str, str]]:
    with contextlib.closing(sqlite3.connect(db)) as connection:
        return {
            (hold.account, hold.device, hold.source)
            for hold in account_doctor._holds(connection).values()
        }


def test_a_signed_out_preflight_does_not_launch_and_holds_the_pair(tmp_path: Path) -> None:
    transport = LoginTransport(framed((0, 1, SIGNED_OUT_CLAUDE)))
    waiter, queue, db, spawn, log = pc_waiter(tmp_path, transport)
    job_id = enqueue(queue, "a")
    before = counters(db, job_id)

    assert waiter.step() is None

    assert spawn.calls == [], "the harness never starts on a signed-out login"
    assert transport.aliases == ["fakepc-claude-bot"]
    assert state(db, job_id) == "queued"
    assert counters(db, job_id) == before, "no attempt or generation is charged"
    assert slots(db) == {}
    assert holds(db) == {("claude-default", "pc", "preflight")}
    [(event, detail)] = events_of(db, job_id)
    assert event == "requeued-signed-out"
    assert detail.endswith("; requeued, no round or attempt charged")
    with sqlite3.connect(db) as connection:
        assert free_requeues(connection, job_id) == 0, "no free requeue is spent"
    assert any("signed out" in line and "requeued" in line for line in log), log


def test_repeated_signed_out_preflights_never_burn_a_retry(tmp_path: Path) -> None:
    transport = LoginTransport(framed((0, 1, SIGNED_OUT_CLAUDE)))
    waiter, queue, db, spawn, _ = pc_waiter(tmp_path, transport)
    job_id = enqueue(queue, "a")
    before = counters(db, job_id)

    for _ in range(MAX_FREE_REQUEUES + 3):
        assert waiter.step() is None

    assert spawn.calls == []
    assert state(db, job_id) == "queued", "a bad login never fails the job, so T6 never retries it"
    assert counters(db, job_id) == before
    assert [event for event, _ in events_of(db, job_id)] == ["requeued-signed-out"], "the hold keeps placement off the pair"
    assert holds(db) == {("claude-default", "pc", "preflight")}, "one hold however many launches fail"


def test_a_signed_in_preflight_launches(tmp_path: Path) -> None:
    transport = LoginTransport(framed((0, 0, claude_reply())))
    waiter, queue, db, spawn, _ = pc_waiter(tmp_path, transport)
    job_id = enqueue(queue, "a")

    assert waiter.step() == job_id

    assert len(spawn.calls) == 1
    assert transport.aliases == ["fakepc-claude-bot"]
    assert holds(db) == set()


class ShellTransport:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = dict(env)

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        return 0

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        done = subprocess.run(["/bin/sh", "-c", argv[-1]], env=self.env, input=input, capture_output=True,
                              timeout=timeout, check=False)
        return Exchanged(done.returncode, done.stdout, done.stderr.decode())


def fake_harnesses(folder: Path, signed_in: str | None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, home, tokens, yes, no in (
        ("claude", "CLAUDE_CONFIG_DIR", "$CLAUDE_CODE_OAUTH_TOKEN$ANTHROPIC_API_KEY$ANTHROPIC_AUTH_TOKEN",
         "printf '%s\\n' " + shlex.quote(claude_reply().decode()),
         "printf '%s\\n' " + shlex.quote(SIGNED_OUT_CLAUDE.decode()) + "; exit 1"),
        ("codex", "CODEX_HOME", "$OPENAI_API_KEY$CODEX_API_KEY",
         "echo 'Logged in using ChatGPT' >&2", "echo 'Not logged in' >&2; exit 1"),
    ):
        fake = folder / name
        fake.write_text(
            "#!/bin/sh\n"
            f'if [ "${{{home}:-}}" = {shlex.quote(signed_in or "")} ] && [ -z "{tokens}" ]; then {yes}; else {no}; fi\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
    return folder


ACCOUNT_MACHINES = """\
accounts:
  - {{id: claude-b, harness: claude, signed_in_on: [box, pc]}}
  - {{id: codex-b, harness: codex, signed_in_on: [box, pc]}}
devices:
  - name: box
    local: true
    slot_max: 1
    harnesses: {{claude: claude-bot, codex: codex-bot}}
    home: {home}
  - name: pc
    tailscale_name: pc.example.ts.net
    slot_max: 1
    harnesses: {{claude: claude-bot, codex: codex-bot}}
    workdir_root: {home}/work
    ssh_alias: pc-{{bot}}
    factory_root: /srv/factory
    home: {home}
"""
PROFILE_HOMES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
PROFILE_TOKENS = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "OPENAI_API_KEY"}


def account_config(tmp_path: Path):
    path = tmp_path / "machines.yml"
    path.write_text(ACCOUNT_MACHINES.format(home=tmp_path / "home"), encoding="utf-8")
    config, findings = load_machines_yml(path)
    assert config is not None, findings
    return config


def login_environment(tmp_path: Path, harness: str, signed_in: str | None) -> dict[str, str]:
    bin_dir = fake_harnesses(tmp_path / "bin", signed_in)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        PROFILE_HOMES[harness]: str(tmp_path / "profile-login"),
        PROFILE_TOKENS[harness]: "sk-planted",
    }


def remote_transport(tmp_path: Path, environment: Mapping[str, str]) -> ShellTransport:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".bash_profile").write_text(
        "".join(f"export {name}={shlex.quote(value)}\n" for name, value in environment.items() if name != "HOME"),
        encoding="utf-8",
    )
    return ShellTransport({"HOME": str(home), "PATH": "/usr/bin:/bin"})


@pytest.mark.parametrize("where", ["local", "remote"])
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_own_login_is_read_through_the_profile_config_folder(tmp_path: Path, where: str, harness: str) -> None:
    config = account_config(tmp_path)
    environment = login_environment(tmp_path, harness, signed_in=None)
    device = config.device("box" if where == "local" else "pc")
    transport = remote_transport(tmp_path, environment) if where == "remote" else LoginTransport(b"")

    with pytest.raises(account_doctor.SignedOut) as raised:
        account_doctor.check_login(transport, config, device, harness, f"{harness}-default", environment)

    assert (raised.value.account, raised.value.device) == (f"{harness}-default", device.name)


@pytest.mark.parametrize("where", ["local", "remote"])
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_account_folder_is_read_without_the_profile_folder_or_tokens(
    tmp_path: Path, where: str, harness: str
) -> None:
    config = account_config(tmp_path)
    device = config.device("box" if where == "local" else "pc")
    account = f"{harness}-b"
    folder = config.account_home(device.name, harness, account)
    environment = login_environment(tmp_path, harness, signed_in=folder)
    transport = remote_transport(tmp_path, environment) if where == "remote" else LoginTransport(b"")

    account_doctor.check_login(transport, config, device, harness, account, environment)

    signed_out = login_environment(tmp_path, harness, signed_in=str(tmp_path / "elsewhere"))
    transport = remote_transport(tmp_path, signed_out) if where == "remote" else transport
    with pytest.raises(account_doctor.SignedOut):
        account_doctor.check_login(transport, config, device, harness, account, signed_out)


def box_waiter(tmp_path: Path, signed_in: str | None):
    spawn = FakeSpawn()
    bin_dir = fake_harnesses(tmp_path / "bin", signed_in)
    waiter, queue, db = make_waiter(
        tmp_path, remote_machines(tmp_path, pc=False), spawn,
        environ={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}, transport=LoginTransport(b""),
    )
    with_job_events(db)
    return waiter, queue, db, spawn


def test_a_signed_out_local_login_does_not_launch_or_charge_a_retry(tmp_path: Path) -> None:
    waiter, queue, db, spawn = box_waiter(tmp_path, signed_in=str(tmp_path / "elsewhere"))
    job_id = enqueue(queue, "a")
    before = counters(db, job_id)

    for _ in range(MAX_FREE_REQUEUES + 1):
        assert waiter.step() is None

    assert spawn.calls == [], "the harness never starts on a signed-out local login"
    assert state(db, job_id) == "queued"
    assert counters(db, job_id) == before
    assert holds(db) == {("claude-default", "box", "preflight")}
    with sqlite3.connect(db) as connection:
        assert free_requeues(connection, job_id) == 0


def test_a_signed_in_local_login_launches(tmp_path: Path) -> None:
    waiter, queue, db, spawn = box_waiter(tmp_path, signed_in=None)
    job_id = enqueue(queue, "a")

    assert waiter.step() == job_id

    assert len(spawn.calls) == 1
    assert holds(db) == set()
