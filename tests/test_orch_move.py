from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import fcntl
import signal
import threading
import time
from pathlib import Path

import pytest

from lib import cli, health
from lib.device_pool import DevicePool
from lib.machines_yml import load_machines_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.state_backup import backup
from tests.child_reaper import TAG
from tests.pool_helpers import LOCAL_BOX_MACHINES, pool_claim
from tests.procs import alive
from tests.test_deploy import _orch_poll_tokens
from tests.test_dispatcher import setup
from tests.test_poller_service import DRIVER

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "orch-move.sh"
BOTS = ("claude-bot", "codex-bot", "cursor-bot")

FAKE_SERVICE = """#!/bin/sh
retired=no
[ -e "$HOME/.config/factory/orch-retired" ] && retired=yes
echo "$(basename "$0") $* retired=$retired" >>"$MOVE_LOG"
"""


class Host:
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


PRE = "self-heal.sh wait-idle retired=no"

STOPS = [
    PRE,
    "poller-service.sh stop retired=yes",
    *(f"waiter-service.sh {bot} stop retired=yes" for bot in BOTS),
    "self-heal.sh stop retired=yes",
    "self-heal.sh wait-idle retired=yes",
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
        assert box.calls() == [PRE]
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
    assert box.calls() == [PRE]
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


def test_orch_move_starts_the_poller_only_through_poller_service() -> None:
    assert _orch_poll_tokens("orch-move.sh", SCRIPT.read_text(encoding="utf-8")) == []
    code = [line.strip() for line in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]
    assert [line for line in code if "run-poller.sh" in line and "--print-db" not in line] == []
    assert [line for line in code if "restore-db" in line] == []
    assert [line for line in code if "poller-service.sh" in line] == [
        'service "${HERE}/poller-service.sh" stop',
        'service "${HERE}/poller-service.sh" start',
    ]


def _service_poller(host: Host, tmp_path: Path, db: Path) -> int:
    driver = tmp_path / "driver" / "orch"
    driver.parent.mkdir()
    driver.write_text(DRIVER.format(root=str(ROOT)), encoding="utf-8")
    _executable(
        host.home / ".local/bin/orch",
        f'''#!/bin/sh
[ "$1" = poll ] && exec "{sys.executable}" "{driver}" "$@"
exec "{sys.executable}" "{ROOT / "bin/orch"}" "$@"
''',
    )
    (host.config / "orch-poll.env").write_text(f"ORCH_DB={db}\n", encoding="utf-8")
    pidfile = db.parent / "poller.pid"
    subprocess.run(
        ["bash", "-c", '"$0" >/dev/null 2>&1 </dev/null &', str(host.checkout / "deploy/run-poller.sh")],
        env={"HOME": str(host.home), "PATH": "/usr/bin:/bin"},
        timeout=30,
        check=True,
    )
    _until(pidfile.exists, "the service poller never wrote its pidfile")
    return int(pidfile.read_text(encoding="utf-8"))


def _real_poller_service(host: Host) -> None:
    for name in ("poller-service.sh", "host-pidns.sh"):
        shutil.copy(ROOT / "deploy" / name, host.checkout / "deploy" / name)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_export_stops_a_service_poller_whose_pidfile_is_beside_a_custom_orch_db(
    tmp_path: Path,
) -> None:
    box = Host(tmp_path, "box")
    _real_poller_service(box)
    db = tmp_path / "custom" / "orch.db"
    db.parent.mkdir()
    _seed(tmp_path, db)
    poller = _service_poller(box, tmp_path, db)
    export = tmp_path / "export"
    try:
        assert not (box.state / "poller.pid").exists()

        result = box.move("export", "--to", "pc", "--dir", str(export))

        assert result.returncode == 0, result.stderr
        assert not alive(poller), "export left the source poller running"
    finally:
        if alive(poller):
            os.kill(poller, signal.SIGKILL)
    assert not (db.parent / "poller.pid").exists()
    assert (export / "manifest.json").exists()
    assert box.retired.exists()
    assert box.calls() == [PRE, *STOPS[2:]]


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_export_stops_a_service_poller_whose_orch_db_is_a_symlink(tmp_path: Path) -> None:
    box = Host(tmp_path, "box")
    _real_poller_service(box)
    real = tmp_path / "real" / "orch.db"
    real.parent.mkdir()
    _seed(tmp_path, real)
    alias = tmp_path / "alias" / "orch.db"
    alias.parent.mkdir()
    alias.symlink_to(real)
    poller = _service_poller(box, tmp_path, alias)
    export = tmp_path / "export"
    try:
        result = box.move("export", "--to", "pc", "--dir", str(export))

        assert result.returncode == 0, result.stderr
        assert not alive(poller), "export left the source poller running"
    finally:
        if alive(poller):
            os.kill(poller, signal.SIGKILL)
    assert (export / "manifest.json").exists()


def test_move_db_unlocked_sees_the_poll_lock_through_a_symlinked_db(tmp_path: Path) -> None:
    real = tmp_path / "real" / "orch.db"
    real.parent.mkdir()
    _seed(tmp_path, real)
    alias = tmp_path / "alias" / "orch.db"
    alias.parent.mkdir()
    alias.symlink_to(real)
    with (real.parent / "orch.db.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin/orch"), "move-db", "unlocked", "--db", str(alias)],
            capture_output=True, text=True, timeout=60,
        )
    assert result.returncode == 1
    assert f"a poller still holds {real.parent / 'orch.db.lock'}" in result.stderr


def test_export_fails_while_any_process_still_holds_the_poll_lock(tmp_path: Path) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db)
    export = tmp_path / "export"
    with (box.db.parent / "orch.db.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = box.move("export", "--to", "pc", "--dir", str(export))

    assert result.returncode == 1
    assert "a poller still holds" in result.stderr
    assert box.retired.exists()
    assert box.calls() == STOPS
    assert not (export / "orch.db").exists()
    assert not (export / "manifest.json").exists()


def test_move_db_unlocked_reads_the_poll_lock(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    assert cli.main(["move-db", "unlocked", "--db", str(db)]) == 0
    with (tmp_path / "orch.db.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = subprocess.run(
            [sys.executable, str(ROOT / "bin/orch"), "move-db", "unlocked", "--db", str(db)],
            text=True, capture_output=True, timeout=30, check=False,
        )
    assert held.returncode == 1
    assert f"a poller still holds {db}.lock" in held.stderr
    assert cli.main(["move-db", "unlocked", "--db", str(db)]) == 0


@pytest.mark.parametrize("share", [True, False], ids=["plain-wait", "autostart"])
def test_move_db_unlocked_reads_every_waiter_lock(tmp_path: Path, share: bool) -> None:
    db = tmp_path / "orch.db"
    take = "share_waiter_lock" if share else "hold_waiter_lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import sys, time\nsys.path.insert(0, {str(ROOT)!r})\nfrom lib import waiter\n"
            f"waiter.{take}({str(db)!r}, 'codex-bot')\nprint('held', flush=True)\ntime.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == "held\n"
        held = subprocess.run(
            [sys.executable, str(ROOT / "bin/orch"), "move-db", "unlocked", "--db", str(db)],
            text=True, capture_output=True, timeout=30, check=False,
        )
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert held.returncode == 1
    assert f"a waiter still holds {tmp_path / 'waiter-codex-bot.lock'}" in held.stderr
    assert cli.main(["move-db", "unlocked", "--db", str(db)]) == 0


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



def test_move_db_check_names_each_table_of_a_matching_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "export" / "orch.db"
    _seed(tmp_path, db)
    manifest = tmp_path / "export" / "manifest.json"
    assert _manifest(db, manifest) == 0
    capsys.readouterr()

    assert cli.main(["move-db", "check", "--db", str(db), "--manifest", str(manifest)]) == 0
    assert "  jobs 1\n" in capsys.readouterr().out

    missing = tmp_path / "missing.json"
    assert cli.main(["move-db", "check", "--db", str(db), "--manifest", str(missing)]) == 1


def test_readme_documents_the_move() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Move the orch host\n", 1)[1].split("\n## ", 1)[0]
    for text in ("orch-move.sh export", "orch-move.sh import", "machines.<source>.yml",
                 "machines.<dest>.yml", "Rollback", "ORCH_MOVE_POLL_SEC",
                 "ORCH_MOVE_DRAIN_TIMEOUT", "ORCH_DB_BACKUP_EVERY", "orch-retired"):
        assert text in section, text
    assert "same tool" in section
    assert "does nothing while any `waiter-<bot>.drain` reads `orch-move`" in section
    assert "self-heal resumes on its next pass" in section


FAKE_SCP = """#!/bin/sh
[ "$1" = -r ] || exit 2
exec cp -R "${2#*:}" "$3"
"""

FAKE_SSH = """#!/bin/sh
shift
cd "$FAKE_SSH_HOME" && exec "$@"
"""


def _remote(tmp_path: Path, remote_home: Path) -> dict[str, str]:
    scp, ssh = tmp_path / "fake-scp", tmp_path / "fake-ssh"
    _executable(scp, FAKE_SCP)
    _executable(ssh, FAKE_SSH)
    return {"ORCH_MOVE_SCP": str(scp), "ORCH_MOVE_SSH": str(ssh),
            "FAKE_SSH_HOME": str(remote_home)}


def _export(tmp_path: Path, host: Host, to: str) -> Path:
    result = host.move("export", "--to", to)
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip())


def _job_states(db: Path) -> list[str]:
    connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return [row[0] for row in connection.execute("SELECT state FROM jobs")]
    finally:
        connection.close()


def _integrity(db: Path) -> str:
    connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()


def test_import_places_the_export_and_starts_the_poller_first(tmp_path: Path) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    export = _export(tmp_path, box, "pc")
    pc.retired.write_text("box 2026-11-01T00:00:00Z\n", encoding="utf-8")
    for bot in ("claude-bot", "cursor-bot"):
        (pc.config / f"waiter-autostart-{bot}").touch()

    result = pc.move("import", str(export))

    assert result.returncode == 0, result.stderr
    assert pc.db.read_bytes() == (export / "orch.db").read_bytes()
    assert _job_states(pc.db) == ["queued"]
    assert not pc.retired.exists()
    assert pc.calls() == [
        "poller-service.sh start retired=no",
        "waiter-service.sh claude-bot start retired=no",
        "waiter-service.sh cursor-bot start retired=no",
        "self-heal.sh start retired=no",
        "linear-webhook-service.sh start retired=no",
    ]
    [stage] = (pc.home / ".local/share/factory/orch-move").glob("import-*")
    assert (stage / "machines.box.yml").read_text(encoding="utf-8") == "# machines of box\n"
    assert (stage / "machines.pc.yml").read_text(encoding="utf-8") == "# machines of pc\n"


def _flip_a_byte(export: Path) -> None:
    db = export / "orch.db"
    data = bytearray(db.read_bytes())
    data[data.index(b"go-1") + 3] ^= 0x01
    db.write_bytes(bytes(data))
    assert _integrity(db) == "ok"


def _break_the_copy(export: Path) -> None:
    db = export / "orch.db"
    _break_an_index(db)
    assert _integrity(db) != "ok"
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
    (export / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _unretired_manifest(export: Path) -> None:
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    manifest["retired"] = ""
    (export / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    "spoil",
    [_flip_a_byte, _break_the_copy, _unretired_manifest],
    ids=["sha256-mismatch", "integrity", "no-retired-line"],
)
def test_import_refuses_a_bad_export_and_places_nothing(tmp_path: Path, spoil) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    export = _export(tmp_path, box, "pc")
    spoil(export)
    pc.retired.write_text("box 2026-11-01T00:00:00Z\n", encoding="utf-8")
    (pc.config / "waiter-autostart-claude-bot").touch()

    result = pc.move("import", str(export))

    assert result.returncode == 1, result.stderr
    assert "placed nothing" in result.stderr
    assert not pc.db.exists()
    assert pc.retired.exists()
    assert pc.calls() == []


def test_import_refuses_to_replace_an_existing_database(tmp_path: Path) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    export = _export(tmp_path, box, "pc")
    _seed(tmp_path, pc.db, running=True)
    before = pc.db.read_bytes()

    result = pc.move("import", str(export))

    assert result.returncode == 1
    assert "refusing to replace" in result.stderr
    assert pc.db.read_bytes() == before
    assert pc.calls() == []


def test_import_from_a_host_that_is_not_retired_places_nothing(tmp_path: Path) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    export = _export(tmp_path, box, "pc")
    box.retired.unlink()

    result = pc.move("import", f"box:{export}", **_remote(tmp_path, box.home))

    assert result.returncode == 1
    assert "box is not retired" in result.stderr
    assert not pc.db.exists()
    assert pc.calls() == []


def test_a_cutover_and_its_rollback_round_trip_the_database(tmp_path: Path) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)

    cutover = _export(tmp_path, box, "pc")
    first = json.loads((cutover / "manifest.json").read_text(encoding="utf-8"))
    placed = pc.move("import", f"box:{cutover}", **_remote(tmp_path, box.home))
    assert placed.returncode == 0, placed.stderr

    rollback = _export(tmp_path, pc, "box")
    assert pc.retired.exists()
    box.db.rename(box.db.with_name("orch.db.pre-rollback"))
    back = box.move("import", str(rollback))
    assert back.returncode == 0, back.stderr

    assert not box.retired.exists()
    assert _rows(box.db) == first["tables"]
    assert _integrity(box.db) == "ok"


PAYLOAD = {
    "round": 2,
    "brief_text": "Fix the flaky export.\n" * 40,
    "plan_text": "1. copy job-detail\n2. check it",
    "evidence": {"checks": [{"name": "pytest", "ok": False}], "head": "abc123"},
    "instruction": "address the review",
}


def _queue_spilled_job(db: Path) -> str:
    job = SQLiteJobQueue(db, clock=time.time).enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="43",
            action="fix",
            recipient="claude",
            source_comment_id="go-2",
            timeout_seconds=3600,
            detail=json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")),
        ),
        dedupe_key="t43:fix",
    )
    assert (db.parent / "job-detail" / f"{job.id}.json").is_file()
    return job.id


def _detail(db: Path, job_id: str) -> dict[str, object]:
    [job] = [job for job in SQLiteJobQueue(db, clock=time.time).outstanding() if job.id == job_id]
    return json.loads(job.request.detail)


def test_a_cutover_and_its_rollback_carry_a_queued_jobs_spilled_detail(
    tmp_path: Path,
) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    job_id = _queue_spilled_job(box.db)

    cutover = _export(tmp_path, box, "pc")
    first = json.loads((cutover / "manifest.json").read_text(encoding="utf-8"))
    placed = pc.move("import", f"box:{cutover}", **_remote(tmp_path, box.home))
    assert placed.returncode == 0, placed.stderr
    assert _detail(pc.db, job_id) == PAYLOAD
    assert _rows(pc.db) == first["tables"]
    assert _integrity(pc.db) == "ok"

    rollback = _export(tmp_path, pc, "box")
    for path in box.state.glob("orch.db*"):
        path.rename(path.with_name(path.name.replace("orch.db", "pre-rollback.db")))
    back = box.move("import", str(rollback))
    assert back.returncode == 0, back.stderr

    assert _detail(box.db, job_id) == PAYLOAD
    assert _rows(box.db) == first["tables"]
    assert _integrity(box.db) == "ok"


def _change_a_detail_file(export: Path) -> None:
    [path] = (export / "job-detail").glob("*.json")
    path.write_text(json.dumps({"brief_text": "tampered"}), encoding="utf-8")


def _delete_a_detail_file(export: Path) -> None:
    [path] = (export / "job-detail").glob("*.json")
    path.unlink()


@pytest.mark.parametrize(
    "spoil", [_change_a_detail_file, _delete_a_detail_file], ids=["changed", "missing"]
)
def test_import_refuses_an_export_whose_job_detail_is_not_the_manifests(
    tmp_path: Path, spoil
) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    _queue_spilled_job(box.db)
    export = _export(tmp_path, box, "pc")
    spoil(export)
    pc.retired.write_text("box 2026-11-01T00:00:00Z\n", encoding="utf-8")

    result = pc.move("import", str(export))

    assert result.returncode == 1, result.stderr
    assert "placed nothing" in result.stderr
    assert not pc.db.exists()
    assert not (pc.state / "job-detail").exists()
    assert pc.calls() == []


def test_import_refuses_to_overwrite_a_different_job_detail_file(tmp_path: Path) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    job_id = _queue_spilled_job(box.db)
    export = _export(tmp_path, box, "pc")
    pc.retired.write_text("box 2026-11-01T00:00:00Z\n", encoding="utf-8")
    existing = pc.state / "job-detail" / f"{job_id}.json"
    existing.parent.mkdir()
    existing.write_text('{"brief_text":"other"}', encoding="utf-8")

    result = pc.move("import", str(export))

    assert result.returncode == 1, result.stderr
    assert "placed nothing" in result.stderr
    assert existing.read_text(encoding="utf-8") == '{"brief_text":"other"}'
    assert not pc.db.exists()
    assert pc.calls() == []


def test_move_db_manifest_refuses_a_copy_whose_spilled_job_has_no_file(
    tmp_path: Path,
) -> None:
    live = tmp_path / "live" / "orch.db"
    _seed(tmp_path, live)
    _queue_spilled_job(live)
    db = tmp_path / "export" / "orch.db"
    backup(live, db)
    out = tmp_path / "export" / "manifest.json"

    assert _manifest(db, out) == 1
    assert not out.exists()


def _custom_db(host: Host) -> Path:
    db = host.home / "custom-state" / "orch.db"
    (host.config / "orch-poll.env").write_text(f"ORCH_DB={db}\n", encoding="utf-8")
    return db


def test_a_rollback_places_the_database_at_the_retired_hosts_configured_path(
    tmp_path: Path,
) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    box_db, pc_db = _custom_db(box), _custom_db(pc)
    _seed(tmp_path, box_db)

    cutover = _export(tmp_path, box, "pc")
    first = json.loads((cutover / "manifest.json").read_text(encoding="utf-8"))
    placed = pc.move("import", f"box:{cutover}", **_remote(tmp_path, box.home))
    assert placed.returncode == 0, placed.stderr
    assert _rows(pc_db) == first["tables"]
    assert not pc.db.exists()

    rollback = _export(tmp_path, pc, "box")
    assert box.retired.exists()
    box_db.rename(box_db.with_name("orch.db.pre-rollback"))
    back = box.move("import", f"pc:{rollback}", **_remote(tmp_path, pc.home))
    assert back.returncode == 0, back.stderr

    assert not box.retired.exists()
    assert _rows(box_db) == first["tables"]
    assert not box.db.exists()
    assert list(box.state.glob("orch.db*")) == []


def test_print_db_answers_on_a_retired_host_while_a_launch_still_refuses(
    tmp_path: Path,
) -> None:
    box = Host(tmp_path, "box")
    db = _custom_db(box)
    box.retired.write_text("pc 2026-11-17T13:00:00Z\n", encoding="utf-8")
    run_poller = box.checkout / "deploy/run-poller.sh"

    printed = subprocess.run(["bash", str(run_poller), "--print-db"], env=box.env(),
                             text=True, capture_output=True, timeout=30, check=False)
    launched = subprocess.run(["bash", str(run_poller)], env=box.env(),
                              text=True, capture_output=True, timeout=30, check=False)

    assert (printed.returncode, printed.stdout) == (0, f"{db}\n"), printed.stderr
    assert launched.returncode == 79
    assert "not starting orch poll" in launched.stderr


def test_import_refuses_when_the_poller_database_path_cannot_be_read(tmp_path: Path) -> None:
    box, pc = Host(tmp_path, "box"), Host(tmp_path, "pc")
    _seed(tmp_path, box.db)
    export = _export(tmp_path, box, "pc")
    pc.retired.write_text("box 2026-11-01T00:00:00Z\n", encoding="utf-8")
    (pc.config / "orch-poll.env").unlink()
    (pc.config / "waiter-autostart-claude-bot").touch()

    result = pc.move("import", str(export))

    assert result.returncode == 1, result.stderr
    assert "cannot read the poller's database path" in result.stderr
    assert "missing ~/.config/factory/orch-poll.env" in result.stderr
    assert list(pc.home.rglob("orch.db*")) == []
    assert not (pc.home / ".local/share/factory/orch-move").exists()
    assert pc.retired.exists()
    assert pc.calls() == []


FAKE_HEAL_ORCH = f"""#!{sys.executable}
import os, sqlite3, sys, time
from pathlib import Path

home = Path(os.environ["HOME"])
with (home / "heal-orch.log").open("a") as log:
    log.write(" ".join(sys.argv[1:3]) + "\\n")
if sys.argv[1:3] == ["health", "--json"]:
    print('{{"known": true, "components": []}}')
elif sys.argv[1:3] == ["heal", "--health-json"]:
    sys.stdin.read()
elif sys.argv[1:3] == ["heal", "devices"]:
    (home / "devices-running").touch()
    while not (home / "devices-go").exists():
        time.sleep(0.02)
    connection = sqlite3.connect(os.environ["ORCH_DB"])
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS device_sync (device TEXT NOT NULL, bot TEXT NOT NULL, "
            "harness TEXT NOT NULL, checkout TEXT NOT NULL, target TEXT, sha TEXT, "
            "state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', since REAL NOT NULL, "
            "checked_at REAL NOT NULL, PRIMARY KEY (device, bot))")
        connection.execute(
            "INSERT INTO device_sync (device, bot, harness, checkout, state, since, checked_at) "
            "VALUES ('pc', 'claude-bot', 'claude', '/factory', 'deployed', 1, 1)")
    connection.close()
"""


def test_export_waits_for_a_self_heal_pass_and_its_device_deploy_before_its_drain(
    tmp_path: Path,
) -> None:
    box = Host(tmp_path, "box")
    for name in ("self-heal.sh", "host-pidns.sh"):
        shutil.copy(ROOT / "deploy" / name, box.checkout / "deploy" / name)
    heal_orch = tmp_path / "heal-orch"
    _executable(heal_orch, FAKE_HEAL_ORCH)
    heal_log = box.home / "heal-orch.log"
    _seed(tmp_path, box.db)
    before = _rows(box.db)
    export = tmp_path / "export"

    def self_heal_once() -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(box.checkout / "deploy/self-heal.sh"), "once"],
                              env=box.env(ORCH_BIN=str(heal_orch)), text=True,
                              capture_output=True, timeout=60, check=False)

    move = None
    try:
        heal = self_heal_once()
        assert heal.returncode == 0, heal.stderr
        _until(lambda: (box.home / "devices-running").exists(), "heal devices never started")

        move = box.start_move("export", "--to", "pc", "--dir", str(export),
                              ORCH_BIN=str(heal_orch))
        _until(lambda: _drains_are(box, "orch-move\n"), "export never drained the waiters")
        time.sleep(0.5)
        assert move.poll() is None, "export went on while heal devices was still writing"
        assert not (export / "probe.db").exists()
        assert not box.retired.exists()
        assert box.calls() == []

        (box.home / "devices-go").touch()
        _, stderr = move.communicate(timeout=30)
        assert move.returncode == 0, stderr
    finally:
        (box.home / "devices-go").touch()
        if move is not None and move.poll() is None:
            move.kill()
            move.communicate()

    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tables"]["device_sync"] == before.get("device_sync", 0) + 1
    assert manifest["tables"] == _rows(box.db)
    assert _rows(export / "orch.db") == _rows(box.db)
    assert box.calls()[-1] == "linear-webhook-service.sh stop retired=yes"

    invoked = heal_log.read_text(encoding="utf-8")
    refused = self_heal_once()
    assert refused.returncode == 79, refused.stderr
    assert "retired as the orch host" in refused.stderr
    assert heal_log.read_text(encoding="utf-8") == invoked


def test_export_that_cannot_wait_out_a_self_heal_pass_drains_nothing(tmp_path: Path) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db)
    _executable(box.checkout / "deploy/self-heal.sh", FAKE_SERVICE + "exit 1\n")
    export = tmp_path / "export"

    result = box.move("export", "--to", "pc", "--dir", str(export))

    assert result.returncode == 1, result.stderr
    assert not box.retired.exists()
    assert list(box.state.glob("waiter-*")) == []
    assert box.calls() == [PRE]
    assert not export.exists()


FAKE_CLEANUP_ORCH = f"""#!{sys.executable}
import os, sys
from pathlib import Path

if sys.argv[1:3] == ["health", "--json"]:
    print('{{"known": true, "components": []}}')
elif sys.argv[1:3] == ["heal", "--health-json"]:
    sys.stdin.read()
    for drain in Path(sys.argv[5]).glob("waiter-*.drain*"):
        drain.unlink(missing_ok=True)
"""


def test_export_drains_while_the_self_heal_loop_runs(tmp_path: Path) -> None:
    box = Host(tmp_path, "box")
    for name in ("self-heal.sh", "host-pidns.sh"):
        shutil.copy(ROOT / "deploy" / name, box.checkout / "deploy" / name)
    cleanup_orch = tmp_path / "cleanup-orch"
    _executable(cleanup_orch, FAKE_CLEANUP_ORCH)
    _, seed = setup(tmp_path / "seed")
    for recipient in ("claude", "codex"):
        seed.enqueue(
            JobRequest(repo="owner/repo", ticket_id="42", action="build", recipient=recipient,
                       source_comment_id=f"go-{recipient}", timeout_seconds=3600),
            dedupe_key=f"t1:{recipient}",
        )
    pool_claim(seed, "claude")
    backup(seed.path, box.db)
    export = tmp_path / "export"
    service_log = box.state / "self-heal-service.log"
    heal_env = box.env(ORCH_BIN=str(cleanup_orch), ORCH_HEAL_SECONDS="0.2",
                       **{TAG: os.environ.get(TAG, "")})

    def self_heal(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(box.checkout / "deploy/self-heal.sh"), command],
                              env=heal_env, text=True, capture_output=True, timeout=60,
                              check=False)

    def skips() -> int:
        text = service_log.read_text(encoding="utf-8") if service_log.exists() else ""
        return text.count("skipping the pass: orch-move export is draining")

    gaps: list[str] = []
    claimed: list[int] = []
    stop = threading.Event()

    def waiter() -> None:
        config, _ = load_machines_yml(LOCAL_BOX_MACHINES)
        queue = SQLiteJobQueue(box.db, clock=time.time)
        seen = False
        while not stop.is_set():
            if _drains_are(box, "orch-move\n"):
                seen = True
            elif seen:
                gaps.append(time.strftime("%H:%M:%S"))
                if not claimed and DevicePool(queue, config).claim("codex") is not None:
                    claimed.append(1)
            time.sleep(0.02)

    started = self_heal("start")
    assert started.returncode == 0, started.stderr
    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    move = None
    try:
        move = box.start_move("export", "--to", "pc", "--dir", str(export),
                              ORCH_BIN=str(cleanup_orch))
        _until(lambda: skips() >= 3 or gaps, "self-heal never ran a pass during the drain")
        assert gaps == [], "a self-heal pass removed the drain files"
        assert move.poll() is None, "export went on while a job was running"
        _finish_jobs(box.db)
        _, stderr = move.communicate(timeout=30)
        loop_after_export = self_heal("status").returncode
    finally:
        stop.set()
        thread.join(timeout=10)
        if move is not None and move.poll() is None:
            move.kill()
            move.communicate()
        self_heal("stop")

    assert move.returncode == 0, stderr
    assert box.retired.exists()
    assert gaps == [] and claimed == []
    assert sorted(_job_states(export / "orch.db")) == ["done", "queued"]
    assert loop_after_export == 3


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_a_poller_launch_already_past_the_retired_guard_exits_once_export_finishes(
    tmp_path: Path,
) -> None:
    box = Host(tmp_path, "box")
    _real_poller_service(box)
    db = tmp_path / "custom" / "orch.db"
    db.parent.mkdir()
    _seed(tmp_path, db)
    gate, ready, launched = tmp_path / "launch-gate", tmp_path / "launch-ready", tmp_path / "launcher.pid"
    pidfile = db.parent / "poller.pid"
    driver = tmp_path / "driver" / "orch"
    driver.parent.mkdir()
    driver.write_text(DRIVER.format(root=str(ROOT)), encoding="utf-8")
    (box.config / "orch-poll.env").write_text(
        f'''ORCH_DB={db}
if [ "${{1:-}}" != --print-db ]; then
    touch "{ready}"
    while [ ! -e "{gate}" ]; do sleep 0.01; done
fi
''',
        encoding="utf-8",
    )
    _executable(
        box.home / ".local/bin/orch",
        f'''#!{sys.executable}
import os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from tests.procs import alive
if sys.argv[1:2] == ["poll"]:
    os.execv({sys.executable!r}, [{sys.executable!r}, {str(driver)!r}, *sys.argv[1:]])
result = subprocess.run([{sys.executable!r}, {str(ROOT / "bin/orch")!r}, *sys.argv[1:]])
if sys.argv[1:3] == ["move-db", "unlocked"] and result.returncode == 0:
    Path({str(gate)!r}).touch()
    launcher = int(Path({str(launched)!r}).read_text())
    deadline = time.monotonic() + 10
    while not Path({str(pidfile)!r}).exists() and alive(launcher):
        assert time.monotonic() < deadline, "the pending launch neither started nor exited"
        time.sleep(0.01)
raise SystemExit(result.returncode)
''',
    )
    launcher = subprocess.Popen(
        [str(box.checkout / "deploy/run-poller.sh")],
        env={"HOME": str(box.home), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    launched.write_text(f"{launcher.pid}\n", encoding="utf-8")
    try:
        _until(ready.exists, "the launch never passed run-poller's retired guard")
        assert not box.retired.exists()

        result = box.move("export", "--to", "pc", "--dir", str(tmp_path / "export"))

        assert result.returncode == 0, result.stderr
        assert launcher.wait(timeout=10) == 79
        assert "retired" in launcher.stderr.read()
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
    assert not pidfile.exists()
    assert cli.main(["move-db", "unlocked", "--db", str(db)]) == 0
    assert (tmp_path / "export/manifest.json").exists()


def _retire(home: Path) -> None:
    (home / ".config/factory").mkdir(parents=True)
    (home / ".config/factory/orch-retired").write_text("box 2026-01-01T00:00:00Z\n", encoding="utf-8")


def test_orch_poll_on_a_retired_host_exits_79_without_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "state" / "orch.db"
    db.parent.mkdir()
    _seed(tmp_path, db)
    pidfile = tmp_path / "poller.pid"
    _retire(tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert cli.main(["poll", "--once", "--db", str(db), "--pidfile", str(pidfile)]) == 79

    assert "retired as the orch host" in capsys.readouterr().err
    assert not pidfile.exists()
    assert cli.main(["move-db", "unlocked", "--db", str(db)]) == 0


def test_orch_wait_on_a_retired_host_exits_79_without_claiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "state" / "orch.db"
    db.parent.mkdir()
    _seed(tmp_path, db)
    _retire(tmp_path / "home")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert cli.main(["wait", "claude-bot", "--db", str(db)]) == 79

    assert "retired as the orch host" in capsys.readouterr().err
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT state FROM jobs").fetchall() == [("queued",)]
    assert health.read_heartbeats(db) is None


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_export_refuses_while_a_plain_wait_past_the_retired_guard_can_still_claim(
    tmp_path: Path,
) -> None:
    box = Host(tmp_path, "box")
    _seed(tmp_path, box.db)
    _real_poller_service(box)
    shutil.copy(ROOT / "deploy/waiter-service.sh", box.checkout / "deploy/waiter-service.sh")
    ready, gate = tmp_path / "ready", tmp_path / "gate"
    driver = tmp_path / "driver" / "orch"
    _executable(
        driver,
        f'''#!{sys.executable}
import sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from lib import cli
checked = cli._retired
def paused():
    retired = checked()
    Path({str(ready)!r}).touch()
    while not Path({str(gate)!r}).exists():
        time.sleep(0.01)
    return retired
cli._retired = paused
raise SystemExit(cli.main(sys.argv[1:]))
''',
    )
    waiter = subprocess.Popen(
        [sys.executable, str(driver), "wait", "claude-bot", "--db", str(box.db),
         "--machines", str(ROOT / "machines.yml")],
        env=box.env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _until(lambda: ready.exists() or waiter.poll() is not None, "the wait never reached its retired guard")
        assert waiter.poll() is None, waiter.communicate()
        assert not box.retired.exists()

        result = box.move("export", "--to", "pc", "--dir", str(tmp_path / "export"), ORCH_WAITER_STOP_WAIT="1")

        assert result.returncode == 1, result.stderr
        assert f"a waiter still holds {box.state / 'waiter-claude-bot.lock'}" in result.stderr
        assert not (tmp_path / "export/manifest.json").exists()
        assert box.retired.exists()
        assert waiter.poll() is None
    finally:
        gate.touch()
        waiter.kill()
        waiter.wait(timeout=10)
