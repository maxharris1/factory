"""What a job ran under, recorded at its start (F-01a)."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from lib import exposure, telemetry
from lib.brief import render_brief
from lib.brief.skills import Router, pinned_paths, resolve, section
from lib.queue import JobRequest, SQLiteJobQueue
from lib.remote import Exchanged, RemoteTarget, ssh_argv
from tests.test_brief import job

FACTORY_ROOT = "/home/claude-bot/factory"
SHA = "0123456789abcdef0123456789abcdef01234567"
EXPECTED_COLUMNS = [
    "job_id", "model", "cli_version", "brief_sha256", "skill_paths", "host",
    "factory_revision", "recorded_at",
]


def _router(role: str, bot: str) -> Router:
    router = resolve(role, bot, FACTORY_ROOT)
    assert isinstance(router, Router), router
    return router


def _shell(replies: dict[str, str | None]) -> exposure.Shell:
    def shell(script: str, args: Sequence[str]) -> str | None:
        return next((reply for needle, reply in replies.items() if needle in script), None)

    return shell


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _exposure(**changes: object) -> exposure.Exposure:
    fields: dict[str, object] = {
        "model": "gpt-5.5",
        "cli_version": "codex-cli 1.2.3",
        "brief_sha256": hashlib.sha256(b"brief").hexdigest(),
        "skill_paths": ("/f/a/SKILL.md", "/f/b/SKILL.md"),
        "host": "box",
        "factory_revision": SHA,
    }
    fields.update(changes)
    return exposure.Exposure(**fields)  # type: ignore[arg-type]


# --- pinned_paths is section's inverse -----------------------------------------


@pytest.mark.parametrize(
    ("role", "bot"),
    [("builder", "claude-bot"), ("builder", "codex-bot"), ("reviewer", "claude-bot"), ("reviewer", "codex-bot")],
)
def test_pinned_paths_reads_back_the_paths_section_wrote(role: str, bot: str) -> None:
    router = _router(role, bot)
    assert pinned_paths(section(router)) == router.paths


@pytest.mark.parametrize(
    ("action", "recipient", "role", "extra"),
    [
        ("plan", "claude", "builder", {}),
        ("build", "codex", "builder", {}),
        ("fix", "claude", "builder", {"instruction": "https://github.com/owner/repo/pull/42#issuecomment-7"}),
        ("review", "codex", "reviewer", {"round": "2/3"}),
    ],
)
def test_pinned_paths_reads_the_router_paths_from_a_full_rendered_brief(
    action: str, recipient: str, role: str, extra: dict
) -> None:
    brief = render_brief(
        job(action, recipient, **extra), bot=f"{recipient}-bot", context=None, factory_root=FACTORY_ROOT
    )
    assert pinned_paths(brief) == _router(role, f"{recipient}-bot").paths


def test_pinned_paths_is_none_without_a_skills_section_or_any_path() -> None:
    assert pinned_paths("# Brief\n\n## Task\n- `/not/a/skill.md`\n") is None
    assert pinned_paths("## Skills\nno paths here\n## Task\n") is None


def test_pinned_paths_stops_at_the_next_section() -> None:
    brief = "## Skills\n- `/a/SKILL.md`\n\n## Task\n- `/b/SKILL.md`\n"
    assert pinned_paths(brief) == ("/a/SKILL.md",)


# --- the model -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "harness", "expected"),
    [
        (["claude", "-p", "--model", "claude-opus-5-5", "--verbose"], "claude", "claude-opus-5-5"),
        (["claude", "-p", "--model=opus"], "claude", "opus"),
        (["codex", "exec", "-m", "gpt-5.5", "--json"], "codex", "gpt-5.5"),
        (["codex", "exec", "-c", 'model="gpt-5.5"'], "codex", "gpt-5.5"),
        (["codex", "exec", "--config", "model=gpt-5.4"], "codex", "gpt-5.4"),
        (["codex", "exec", "--config=model='o3'"], "codex", "o3"),
        (["codex", "exec", "-c", 'model="a"', "-m", "b"], "codex", "b"),
    ],
)
def test_the_model_comes_from_the_command_line(argv: list[str], harness: str, expected: str) -> None:
    shell = _shell({"config.toml": 'model = "from-config"\n'})
    assert exposure.model(argv, harness, shell) == expected


def test_options_after_a_double_dash_are_not_the_harnesses() -> None:
    shell = _shell({})
    assert exposure.model(["claude", "-p", "--", "--model", "x"], "claude", shell) == exposure.UNKNOWN


def test_a_codex_job_without_a_model_flag_reads_its_config_toml(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.5-codex"\n[profiles.x]\nmodel = "other"\n', encoding="utf-8")
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home)})
    assert exposure.model(["codex", "exec", "--json"], "codex", shell) == "gpt-5.5-codex"


def test_no_model_anywhere_is_unknown(tmp_path: Path) -> None:
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path / "none")})
    assert exposure.model(["codex", "exec"], "codex", shell) == exposure.UNKNOWN
    assert exposure.model(["claude", "-p"], "claude", _shell({"config.toml": 'model = "x"'})) == exposure.UNKNOWN
    assert exposure.model(["codex", "exec"], "codex", _shell({"config.toml": "model = 3"})) == exposure.UNKNOWN
    assert exposure.model(["codex", "exec"], "codex", _shell({"config.toml": "not toml ["})) == exposure.UNKNOWN


# --- the CLI version -------------------------------------------------------------


def test_the_cli_version_is_the_first_line_of_version(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", '[ "$1" = --version ] && printf "\\n  fake-cli 9.9.9  \\nmore\\n"')
    assert exposure.cli_version(str(binary), exposure.local_shell({"PATH": "/usr/bin:/bin"})) == "fake-cli 9.9.9"


def test_a_long_version_line_is_cut(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", "printf 'v1.%0300d\\n' 0")
    assert exposure.cli_version(str(binary), exposure.local_shell({})) == "v1." + "0" * 197


def test_a_binary_that_ignores_version_is_unknown_not_its_output(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", """echo '{"command": "deploy --pin s3cret 1.0"}'""")
    assert exposure.cli_version(str(binary), exposure.local_shell({})) == exposure.UNKNOWN


def test_a_version_that_hangs_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "TIMEOUT_SECONDS", 0.5)
    binary = _script(tmp_path, "fake", "echo started; exec sleep 30")
    started = time.monotonic()
    assert exposure.cli_version(str(binary), exposure.local_shell({"PATH": "/usr/bin:/bin"})) == exposure.UNKNOWN
    assert time.monotonic() - started < 5


def test_a_version_that_exits_non_zero_or_is_missing_is_unknown(tmp_path: Path) -> None:
    binary = _script(tmp_path, "fake", "echo fake-cli 1.0; exit 3")
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin"})
    assert exposure.cli_version(str(binary), shell) == exposure.UNKNOWN
    assert exposure.cli_version(str(tmp_path / "missing"), shell) == exposure.UNKNOWN


def test_the_local_shell_drops_the_proof_and_worker_tag(tmp_path: Path) -> None:
    shell = exposure.local_shell(
        {"PATH": "/usr/bin:/bin", "ORCH_JOB_PROOF": "secret", "ORCH_WORKER_TAG": "tag", "KEPT": "yes"}
    )
    seen = shell('printf "%s|%s|%s|%s" "${ORCH_JOB_PROOF-}" "${ORCH_WORKER_TAG-}" "$KEPT" "$1"', ["a b"])
    assert seen == "||yes|a b"


# --- the factory revision --------------------------------------------------------


def test_the_factory_revision_is_the_checkouts_head(tmp_path: Path) -> None:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], env=env, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x"], env=env, check=True)
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()
    assert exposure.factory_revision(str(tmp_path), exposure.local_shell(env)) == head


def test_a_factory_root_that_is_not_git_or_missing_is_unknown(tmp_path: Path) -> None:
    shell = exposure.local_shell({"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                  "GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert exposure.factory_revision(str(tmp_path), shell) == exposure.UNKNOWN
    assert exposure.factory_revision(None, shell) == exposure.UNKNOWN
    assert exposure.factory_revision("/x", _shell({"rev-parse": "not-a-sha"})) == exposure.UNKNOWN


# --- the brief -------------------------------------------------------------------


def test_the_brief_hash_and_its_unknowns(tmp_path: Path) -> None:
    path = tmp_path / "brief.md"
    path.write_bytes(b"the brief\n")
    assert exposure.brief_sha256(exposure.brief_file(str(path))) == hashlib.sha256(b"the brief\n").hexdigest()
    assert exposure.brief_file(str(tmp_path / "missing")) is None
    assert exposure.brief_file(None) is None
    assert exposure.brief_sha256(None) == exposure.UNKNOWN
    assert exposure.brief_sha256(b"") == exposure.UNKNOWN


def test_gather_fills_every_field_and_says_unknown_where_it_cannot() -> None:
    router = _router("builder", "codex-bot")
    brief = f"# Brief\n\n{section(router)}\n\n## Task\n".encode()
    shell = _shell({"--version": "codex-cli 1.2.3", "rev-parse": SHA})
    found = exposure.gather(
        argv=["codex", "exec", "-m", "gpt-5.5"], binary="codex", harness="codex",
        brief=brief, host="box", factory_root="/f", shell=shell,
    )
    assert found == exposure.Exposure(
        "gpt-5.5", "codex-cli 1.2.3", hashlib.sha256(brief).hexdigest(), router.paths, "box", SHA
    )
    nothing = exposure.gather(
        argv=["claude", "-p"], binary="claude", harness="claude",
        brief=b"# Brief\n## Task\n", host="", factory_root=None, shell=_shell({}),
    )
    assert nothing == exposure.Exposure(
        exposure.UNKNOWN, exposure.UNKNOWN, hashlib.sha256(b"# Brief\n## Task\n").hexdigest(),
        None, exposure.UNKNOWN, exposure.UNKNOWN,
    )


# --- the remote shell ------------------------------------------------------------


class _Transport:
    def __init__(self, reply: Exchanged | None = None):
        self.reply = reply
        self.argvs: list[list[str]] = []

    def exchange(self, argv: Sequence[str], *, timeout: float, input: bytes = b"") -> Exchanged:
        self.argvs.append(list(argv))
        assert timeout == exposure.TIMEOUT_SECONDS
        if self.reply is not None:
            return self.reply
        done = subprocess.run(["/bin/sh", "-c", argv[-1]], capture_output=True, timeout=30)
        return Exchanged(done.returncode, done.stdout, done.stderr.decode())

    def run(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("not a byte call")


TARGET = RemoteTarget("pc", "pc-codex-bot", "codex-bot", "/home/codex-bot/work", "job1")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_remote_shell_runs_the_script_with_its_args_in_a_login_shell() -> None:
    transport = _Transport()
    shell = exposure.remote_shell(transport, TARGET)
    assert shell('printf "%s|%s" "$1" "$2"', ["a b", "it's"]) == "a b|it's"
    assert transport.argvs == [ssh_argv(TARGET, transport.argvs[0][-1])]


@pytest.mark.parametrize(
    "reply", [Exchanged(0, b"x", "", timed_out=True), Exchanged(255, b"x", ""), Exchanged(0, b"  \n", "")]
)
def test_the_remote_shell_is_none_on_a_timeout_a_failure_or_no_output(reply: Exchanged) -> None:
    assert exposure.remote_shell(_Transport(reply), TARGET)("exec true", []) is None


def test_the_remote_shell_stops_asking_a_link_that_timed_out() -> None:
    transport = _Transport(Exchanged(255, b"", "", timed_out=True))
    shell = exposure.remote_shell(transport, TARGET)
    assert shell("exec true", []) is None and shell("exec true", []) is None
    assert len(transport.argvs) == 1


def test_the_remote_shell_needs_a_byte_transport() -> None:
    class Plain:
        def run(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("never run")

    assert exposure.remote_shell(Plain(), TARGET)("exec true", []) is None  # type: ignore[arg-type]


# --- the table and record --------------------------------------------------------


def test_read_returns_what_write_stored_and_a_retry_overwrites(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "x.db")
    assert exposure.read(connection, "job1") is None
    exposure.ensure_table(connection)
    assert exposure.read(connection, "job1") is None
    exposure.write(connection, "job1", _exposure(), 1.0)
    assert exposure.read(connection, "job1") == _exposure()
    exposure.write(connection, "job1", _exposure(skill_paths=None, model=exposure.UNKNOWN), 2.0)
    assert exposure.read(connection, "job1") == _exposure(skill_paths=None, model=exposure.UNKNOWN)
    assert connection.execute("SELECT skill_paths, recorded_at FROM job_exposure").fetchall() == [("unknown", 2.0)]


def test_record_writes_a_row_and_logs_one_line(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    lines: list[str] = []
    got = exposure.record(queue.connect, "job1", _exposure, lines.append, clock=lambda: 5.0)
    assert got == _exposure()
    assert lines == [f"exposure: recorded (model gpt-5.5, cli codex-cli 1.2.3, host box, revision {SHA[:12]})"]
    with queue.connect() as connection:
        assert exposure.read(connection, "job1") == _exposure()
    exposure.record(queue.connect, "job1", lambda: _exposure(host="pc"), lines.append)
    with queue.connect() as connection:
        assert exposure.read(connection, "job1") == _exposure(host="pc")
        assert connection.execute("SELECT COUNT(*) FROM job_exposure").fetchone()[0] == 1


def test_record_never_raises_and_writes_nothing_when_gather_fails(tmp_path: Path) -> None:
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    lines: list[str] = []

    def broken() -> exposure.Exposure:
        raise RuntimeError("boom")

    assert exposure.record(queue.connect, "job1", broken, lines.append) is None
    assert len(lines) == 1 and lines[0].startswith("exposure: not recorded (")
    with queue.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_exposure").fetchone()[0] == 0

    def unreachable() -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    assert exposure.record(unreachable, "job1", _exposure, lines.append) is None
    assert lines[1] == "exposure: not recorded (unable to open database file)"


# --- ac-4: the migration on a copy of an existing orch.db ------------------------


def _dump(path: Path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(path)
    try:
        return {
            "jobs": connection.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
            "job_telemetry": connection.execute("SELECT * FROM job_telemetry ORDER BY job_id").fetchall(),
        }
    finally:
        connection.close()


def _schema(path: Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        connection.close()


def _files(path: Path) -> dict[str, str]:
    return {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest()
        for file in (path, Path(f"{path}-wal"))
        if file.exists()
    }


def _old_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(exposure, "ensure_table", lambda connection: None)
        queue = SQLiteJobQueue(path, clock=time.time)
    for number in range(3):
        job = queue.enqueue(
            JobRequest(repo="acme/widgets", ticket_id=str(number), action="build", recipient="codex",
                       source_comment_id=str(number), timeout_seconds=60),
            dedupe_key=f"k{number}",
        )
        row = {name: 0 for name in telemetry.COLUMNS} | {
            "job_id": job.id, "bot": "codex-bot", "harness": "codex", "repo": "acme/widgets",
            "tool_calls": "{}", "steps": "[]", "started_at": 1.5, "updated_at": 2.5,
        }
        with closing(queue.connect()) as connection, connection:
            telemetry.ensure_table(connection)
            connection.execute(
                f"INSERT INTO job_telemetry ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0, "checkpointed"
    assert "job_exposure" not in {row[1] for row in _schema(path)}


def test_opening_an_existing_orch_db_adds_job_exposure_and_changes_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "orch.db"
    _old_database(original, monkeypatch)
    before = _files(original)
    rows = _dump(original)
    copy = tmp_path / "copy" / "orch.db"
    copy.parent.mkdir()
    shutil.copy(original, copy)

    SQLiteJobQueue(copy, clock=time.time)

    connection = sqlite3.connect(copy)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(job_exposure)")]
    connection.close()
    assert columns == EXPECTED_COLUMNS
    assert _dump(copy) == rows and len(rows["jobs"]) == 3 and len(rows["job_telemetry"]) == 3
    assert _files(original) == before

    migrated = _schema(copy)
    SQLiteJobQueue(copy, clock=time.time)
    assert _schema(copy) == migrated
    assert _dump(copy) == rows


def test_two_openers_migrating_at_once_both_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "orch.db"
    _old_database(path, monkeypatch)
    rows = _dump(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            barrier.wait()
            SQLiteJobQueue(path, clock=time.time)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    connection = sqlite3.connect(path)
    assert [row[1] for row in connection.execute("PRAGMA table_info(job_exposure)")] == EXPECTED_COLUMNS
    connection.close()
    assert _dump(path) == rows


def test_importing_the_queue_and_the_cli_first_has_no_cycle() -> None:
    for first in ("lib.queue", "lib.exposure", "lib.brief.skills", "lib.cli"):
        done = subprocess.run([sys.executable, "-c", f"import {first}; import lib.cli, lib.queue"],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
