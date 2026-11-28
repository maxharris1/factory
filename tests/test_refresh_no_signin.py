from __future__ import annotations

import ast
import contextlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from lib import account_doctor, account_signin, signin_wake, usage_probe, usage_readings, usage_refresh
from lib.queue import SQLiteJobQueue
from lib.remote import Exchanged
from tests.test_account_doctor import CURSOR_SIGNED_IN, SIGNED_OUT_CLAUDE, claude_reply, framed
from tests.test_remote_start import config_of
from tests.test_usage_refresh import NOW, Transport, agy_usage, get_usage, rollout

ROOT = Path(__file__).resolve().parents[1]
REPO = "Owner/Factory"
CANARY = "CANARY-Reply-7f3e9b-DoNotStore"
SIGN_IN_MODULES = {"account_signin", "signin_attempt", "signin_broker"}

MACHINES = """\
orch_host: far
accounts:
  - {id: claude-b, harness: claude, signed_in_on: [far]}
devices:
  - name: far
    tailscale_name: far.example.ts.net
    slot_max: 4
    harnesses: {claude: claude-bot, codex: codex-bot, cursor: cursor-bot, gemini: gemini-bot}
    workdir_root: /home/{bot}/work
    ssh_alias: far-{bot}
    factory_root: /srv/factory
    home: /home/{bot}
"""


def with_canary(reply: bytes) -> bytes:
    data = json.loads(reply)
    return json.dumps(data | {"canary": CANARY}).encode()


def planted(reply: bytes) -> bytes:
    *before, last = reply.rstrip(b"\n").split(b"\n")
    return b"\n".join([CANARY.encode(), *before, with_canary(last)]) + b"\n"


class Recording(Transport):
    def __init__(self) -> None:
        super().__init__()
        canary = CANARY.encode()
        self.status = {
            "far-claude-bot": framed(
                (0, 0, claude_reply(subscription=CANARY, canary=CANARY)), (1, 1, with_canary(SIGNED_OUT_CLAUDE))),
            "far-codex-bot": framed((0, 0, b"Logged in using ChatGPT " + canary + b"\n" + canary)),
            "far-cursor-bot": framed((0, 0, with_canary(CURSOR_SIGNED_IN))),
            "far-gemini-bot": framed((0, 0, b"gemini-3.1-pro  " + canary + b"\nORCH-ACCOUNT-STDERR\n" + canary)),
        }
        self.usage = {
            ("far-claude-bot", None): planted(get_usage(20)),
            ("far-claude-bot", "claude-b"): planted(get_usage(40)),
            ("far-codex-bot", None): planted(rollout(30)),
            ("far-gemini-bot", None): planted(agy_usage(0.9)),
        }
        self.exchanges: list[tuple[tuple[str, ...], bytes]] = []
        self.runs: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], *, timeout: float) -> int:
        self.runs.append(tuple(argv))
        return 0

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        self.exchanges.append((tuple(argv), input))
        return super().exchange(argv, timeout=timeout, input=input)


def canary_cells(connection: sqlite3.Connection) -> list[str]:
    hits = []
    tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        cursor = connection.execute(f'SELECT * FROM "{table}"')
        columns = [column[0] for column in cursor.description]
        for row in cursor:
            for column, cell in zip(columns, row):
                text = cell.decode("utf-8", errors="replace") if isinstance(cell, bytes) else str(cell)
                if CANARY in text:
                    hits.append(f"{table}.{column}")
    return sorted(set(hits))


def test_the_scheduled_pass_runs_no_login_command_and_moves_no_reply(tmp_path: Path) -> None:
    assert not account_doctor.SUBSCRIPTION.fullmatch(CANARY), "the canary must not parse as a subscription"
    path = tmp_path / "machines.yml"
    path.write_text(MACHINES, encoding="utf-8")
    config = config_of(path)
    (tmp_path / "state").mkdir()
    queue = SQLiteJobQueue(tmp_path / "state" / "d.db", clock=lambda: NOW)
    usage_readings.migrate(queue.path, queue.connect, directory=tmp_path / "backups")

    allowed: set[tuple[tuple[str, ...], bytes]] = set()
    banned: set[str] = set()
    for device in config.devices:
        for harness in account_doctor._STATUS:
            accounts = config.accounts_on(device.name, harness)
            homes = [config.account_home(device.name, harness, account) for account in accounts]
            if accounts:
                script = account_doctor.status_script(harness, homes)
                allowed.add((tuple(account_doctor.ssh_argv(device, harness, script)), b""))
            if harness in usage_refresh.READERS:
                reader = usage_refresh.READERS[harness]
                allowed |= {(tuple(account_doctor.ssh_argv(device, harness, reader.script(home))), reader.stdin)
                            for home in homes}
            if harness in account_signin._LOGIN:
                for home in {*homes, device.home_for(harness)} - {None}:
                    banned |= {account_signin.login_script(harness, home), account_signin.logout_script(harness, home)}
    assert banned, "the configured pairs give login and logout scripts to ban"

    transport = Recording()
    with contextlib.closing(queue.connect()) as connection:
        usage_refresh.run(connection, config, transport, NOW, REPO)
        jobs = signin_wake.announce(queue, config, REPO)

        assert set(transport.exchanges) <= allowed, "every exchange is a status probe or a usage read"
        assert transport.runs == [], "the pass runs no ssh command outside exchange"
        for argv, stdin in transport.exchanges:
            assert not any("-tt" in word for word in argv), f"no tty is asked for: {argv}"
            assert not any(word.endswith(("scp", "rsync", "sftp")) for word in argv), f"no file copy runs: {argv}"
            assert argv[-1] not in banned, f"no login or logout script runs: {argv}"
            assert stdin in {b"", usage_probe.REQUEST}, f"stdin is empty or the usage request: {stdin!r}"
            assert not any(CANARY in word for word in argv), f"no reply byte reaches an argv: {argv}"
            assert CANARY.encode() not in stdin, "no reply byte reaches stdin"

        assert {str(row.harness) for row in usage_readings.stored(connection) or []} == set(usage_refresh.READERS), \
            "every harness with a reader has a stored reading"
        hits = canary_cells(connection)
        assert not hits, f"reply bytes are stored in table.column {hits}"
        assert tuple(connection.execute(
            f"SELECT status FROM {account_doctor.TABLE} WHERE account = 'claude-b' AND device = 'far'"
        ).fetchone()) == ("signed-out",), "the doctor recorded claude-b signed out"
        queued = [tuple(row) for row in connection.execute(
            "SELECT action, ticket_id FROM jobs WHERE action = 'account-signin'")]

    assert queued == [("account-signin", "claude-b@far")], "one sign-in job was queued, for claude-b@far"
    assert [(job.request.action, job.request.ticket_id) for job in jobs] == [("account-signin", "claude-b@far")]
    assert CANARY not in jobs[0].request.detail, "no reply byte reaches the wake detail"


def test_the_pass_and_the_wake_import_no_sign_in_module() -> None:
    imported: set[str] = set()
    for module in ("usage_refresh", "signin_wake"):
        tree = ast.parse((ROOT / "lib" / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {node.module or "", *(alias.name for alias in node.names)}
    parts = {part for name in imported for part in name.split(".")}
    assert parts, "the modules import something"
    assert not parts & SIGN_IN_MODULES, f"imported names: {sorted(imported)}"

    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, lib.usage_refresh, lib.signin_wake; "
         "print('\\n'.join(sorted(name for name in sys.modules if name.startswith('lib.'))))"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert {"lib.usage_refresh", "lib.signin_wake"} <= set(loaded)
    assert not {f"lib.{module}" for module in SIGN_IN_MODULES} & set(loaded), loaded
