"""deploy/orch-move.sh moves orch.db between hosts with one writer at a time,
and `orch move-db` checks each copy it moves."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import cli
from lib.queue import JobRequest
from lib.state_backup import backup
from tests.pool_helpers import pool_claim
from tests.test_deploy import _orch_poll_launches
from tests.test_dispatcher import setup

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "orch-move.sh"
BOTS = ("claude-bot", "codex-bot", "cursor-bot")

FAKE_SERVICE = """#!/bin/sh
retired=no
[ -e "$HOME/.config/factory/orch-retired" ] && retired=yes
echo "$(basename "$0") $* retired=$retired" >>"$MOVE_LOG"
"""


class Host:
    """A fake HOME and checkout: the real orch-move.sh and run-poller.sh
    beside service scripts that only log each call."""

    def __init__(self, root: Path, name: str) -> None:
        self.name = name
        self.home = root / name / "home"
        self.checkout = root / name / "checkout"
        self.log = root / name / "services.log"
        deploy = self.checkout / "deploy"
        deploy.mkdir(parents=True)
        shutil.copy(SCRIPT, deploy)
        shutil.copy(ROOT / "deploy" / "run-poller.sh", deploy)
        for name_ in ("poller-service.sh", "waiter-service.sh", "self-heal.sh"):
            _executable(deploy / name_, FAKE_SERVICE)
        _executable(self.home / ".local/share/factory/linear-webhook-service.sh", FAKE_SERVICE)
        _executable(
            self.home / ".local/bin/orch",
            f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "bin/orch"}" "$@"\n',
        )
        self.config.mkdir(parents=True)
        (self.config / "webhook.env").write_text("", encoding="utf-8")
        (self.config / "orch-poll.env").write_text("", encoding="utf-8")
        (self.checkout / "machines.yml").write_text(f"# machines of {name}\n", encoding="utf-8")
        self.state.mkdir(parents=True)

    @property
    def config(self) -> Path:
        return self.home / ".config/factory"

    @property
    def state(self) -> Path:
        return self.home / ".local/state/factory"

    @property
    def db(self) -> Path:
        return self.state / "orch.db"

    @property
    def retired(self) -> Path:
        return self.config / "orch-retired"

    def drain(self, bot: str) -> Path:
        return self.state / f"waiter-{bot}.drain"

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "MOVE_LOG": str(self.log),
            "ORCH_MOVE_HOST": self.name,
            "ORCH_MOVE_POLL_SEC": "0.1",
            **extra,
        }

    def move(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.checkout / "deploy/orch-move.sh"), *args],
            env=self.env(**extra),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def start_move(self, *args: str, **extra: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["bash", str(self.checkout / "deploy/orch-move.sh"), *args],
            env=self.env(**extra),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _seed(tmp_path: Path, db: Path, *, running: bool = False) -> None:
    """orch.db with one build job (claimed onto a device slot when running),
    written through the real schema and the real claim."""
    _, queue = setup(tmp_path / f"seed-{len(list(tmp_path.glob('seed-*')))}")
    queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="42",
            action="build",
            recipient="claude",
            source_comment_id="go-1",
            timeout_seconds=3600,
        ),
        dedupe_key="t1:build",
    )
    if running:
        pool_claim(queue, "claude")
    backup(queue.path, db)


def _finish_jobs(db: Path) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET state = 'done' WHERE state = 'running'")
        connection.execute("DELETE FROM device_slots")
    connection.close()


def _rows(db: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
            )
        ]
        return {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                for name in names}
    finally:
        connection.close()


def _until(condition, what: str, seconds: float = 20) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.02)


def _drains_are(host: Host, text: str) -> bool:
    return all(
        host.drain(bot).exists() and host.drain(bot).read_text(encoding="utf-8") == text
        for bot in BOTS
    )


STOPS = [
    "poller-service.sh stop retired=yes",
    *(f"waiter-service.sh {bot} stop retired=yes" for bot in BOTS),
    "self-heal.sh stop retired=yes",
    "linear-webhook-service.sh stop retired=yes",
]


def test_export_waits_for_the_drain_then_retires_before_it_stops_anything(
    tmp_path: Path,
) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db, running=True)
    export = tmp_path / "export"

    move = box.start_move("export", "--to", "pc", "--dir", str(export))
    try:
        _until(lambda: _drains_are(box, "orch-move\n") and (export / "probe.db").exists(),
               "export never drained the waiters")
        time.sleep(0.3)
        assert move.poll() is None, "export went on while a job was running"
        assert _drains_are(box, "orch-move\n")
        assert box.calls() == []
        _finish_jobs(box.db)
        stdout, stderr = move.communicate(timeout=30)
    finally:
        if move.poll() is None:
            move.kill()
            move.communicate()

    assert move.returncode == 0, stderr
    assert stdout.strip() == str(export)
    assert box.calls() == STOPS
    assert box.retired.read_text(encoding="utf-8").startswith("pc ")
    copy = export / "orch.db"
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert manifest["integrity"] == "ok"
    assert manifest["tables"] == _rows(box.db)
    assert manifest["tables"]["jobs"] == 1
    assert manifest["tables"]["device_slots"] == 0
    assert manifest["source_host"] == "box"
    assert manifest["to"] == "pc"
    assert manifest["retired"] == box.retired.read_text(encoding="utf-8").strip()
    assert (export / "machines.box.yml").read_text(encoding="utf-8") == "# machines of box\n"
    assert not (export / "probe.db").exists()


def test_export_that_never_drains_gives_up_and_leaves_the_host_running(
    tmp_path: Path,
) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db, running=True)
    export = tmp_path / "export"

    move = box.start_move("export", "--to", "pc", "--dir", str(export),
                          ORCH_MOVE_DRAIN_TIMEOUT="1")
    try:
        _until(lambda: _drains_are(box, "orch-move\n"), "export never drained the waiters")
        for bot in BOTS:
            box.drain(bot).with_name(f"waiter-{bot}.drain.ack").write_text("orch-move\n")
        _, stderr = move.communicate(timeout=30)
    finally:
        if move.poll() is None:
            move.kill()
            move.communicate()

    assert move.returncode == 75, stderr
    assert sorted(path.name for path in box.state.glob("waiter-*")) == []
    assert not box.retired.exists()
    assert box.calls() == []
    assert not (export / "probe.db").exists()


def test_a_drain_file_self_heal_removes_or_replaces_comes_back(tmp_path: Path) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db, running=True)

    move = box.start_move("export", "--to", "pc", "--dir", str(tmp_path / "export"))
    try:
        _until(lambda: _drains_are(box, "orch-move\n"), "export never drained the waiters")
        box.drain("claude-bot").unlink()
        box.drain("codex-bot").write_text("0123abc\n", encoding="utf-8")
        _until(lambda: _drains_are(box, "orch-move\n"), "a drain file did not come back")
        _finish_jobs(box.db)
        _, stderr = move.communicate(timeout=30)
    finally:
        if move.poll() is None:
            move.kill()
            move.communicate()
    assert move.returncode == 0, stderr


def test_a_stop_that_fails_leaves_the_host_retired_with_no_manifest(tmp_path: Path) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db)
    _executable(box.checkout / "deploy/poller-service.sh", FAKE_SERVICE + "exit 1\n")
    export = tmp_path / "export"

    result = box.move("export", "--to", "pc", "--dir", str(export))

    assert result.returncode == 1
    assert "To undo: rm " in result.stderr
    assert box.retired.exists()
    assert box.calls() == STOPS
    assert not (export / "manifest.json").exists()


def test_export_on_a_retired_host_does_nothing(tmp_path: Path) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db)
    box.retired.write_text("pc 2026-11-17T13:00:00Z\n", encoding="utf-8")
    export = tmp_path / "export"

    result = box.move("export", "--to", "pc", "--dir", str(export))

    assert result.returncode == 79, result.stderr
    assert "already retired" in result.stderr
    assert list(box.state.glob("waiter-*")) == []
    assert box.calls() == []
    assert not export.exists()


@pytest.mark.parametrize(
    "args",
    [(), ("export",), ("export", "--to"), ("export", "--dir", "x"), ("export", "--to", "pc", "-x")],
)
def test_usage_errors_exit_2(tmp_path: Path, args: tuple[str, ...]) -> None:
    box = Host(tmp_path, "box")
    result = box.move(*args)
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert box.calls() == []


def test_orch_move_starts_no_poller_of_its_own() -> None:
    code = [line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]
    assert _orch_poll_launches("\n".join(code)) == []
    assert [line for line in code if "run-poller.sh" in line and "--print-db" not in line] == []
    assert [line for line in code if "restore-db" in line] == []


def test_move_db_drained_counts_running_jobs_and_held_slots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "state" / "orch.db"
    _seed(tmp_path, db, running=True)

    assert cli.main(["move-db", "drained", "--db", str(db)]) == 1
    assert "running jobs 1, held device slots 1" in capsys.readouterr().out

    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM device_slots")
    connection.close()
    assert cli.main(["move-db", "drained", "--db", str(db)]) == 1
    assert "running jobs 1, held device slots 0" in capsys.readouterr().out

    _finish_jobs(db)
    assert cli.main(["move-db", "drained", "--db", str(db)]) == 0
    assert cli.main(["move-db", "drained", "--db", str(tmp_path / "missing.db")]) == 2


def _break_an_index(db: Path) -> None:
    """Leave db readable but failing integrity_check: an index whose recorded
    definition no longer matches its entries."""
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE INDEX jobs_repo ON jobs(repo)")
        connection.commit()
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = 'CREATE INDEX jobs_repo ON jobs(ticket_id)' "
            "WHERE name = 'jobs_repo'"
        )
        connection.commit()
    finally:
        connection.close()


def _manifest(db: Path, out: Path) -> int:
    return cli.main(["move-db", "manifest", "--db", str(db), "--out", str(out),
                     "--source-host", "box", "--to", "pc", "--retired", "pc 2026-11-17T13:00:00Z"])


def test_move_db_manifest_records_the_copy(tmp_path: Path) -> None:
    db = tmp_path / "export" / "orch.db"
    _seed(tmp_path, db)
    out = tmp_path / "export" / "manifest.json"

    assert _manifest(db, out) == 0

    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["sha256"] == hashlib.sha256(db.read_bytes()).hexdigest()
    assert manifest["tables"] == _rows(db)
    assert manifest["integrity"] == "ok"
    assert (manifest["source_host"], manifest["to"]) == ("box", "pc")
    assert manifest["retired"] == "pc 2026-11-17T13:00:00Z"
    assert manifest["taken_at"].endswith("+00:00")


def test_move_db_manifest_refuses_a_copy_that_fails_integrity_check(tmp_path: Path) -> None:
    db = tmp_path / "export" / "orch.db"
    _seed(tmp_path, db)
    _break_an_index(db)
    out = tmp_path / "export" / "manifest.json"

    assert _manifest(db, out) == 1
    assert not out.exists()
