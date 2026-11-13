"""#103: a sandboxed `codex exec` harness gets the orch state dir as a writable root.

`codex exec --sandbox workspace-write` lets model commands write only the
job worktree (and its temp dirs), so the harness's own `orch job done`
failed with "attempt to write a readonly database" and every Codex job
ended failed. `orch job run` now adds `--add-dir <dir of ORCH_DB>` to a
sandboxed codex exec command on the box, once, where that directory exists,
and leaves every other command unchanged.

A remote (placed) run gets no box path (Factory, Nov 13): a codex command
runs by its bare name from the device's login PATH, and each `--add-dir` it
names is given only where `test -d` holds on the device.

Fakes only: no real codex, sandbox, host or GitHub is used; remote runs go
through the fake ssh transport (tests/fake_ssh.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import codex_sandbox
from lib.codex_sandbox import DeviceCommand, for_device, state_dirs, with_state_dir
from lib.queue import SQLiteJobQueue
from tests import test_remote_start as remote_start
from tests.test_brief import FACTORY_MD
from tests.test_waiter import (
    FAKE_GH,
    MACHINES,
    ORCH,
    _git_remote,
    _wait_for,
    _write_exe,
    enqueue,
    write_config,
)

DB = Path("/srv/factory-state/orch.db")
STATE = str(DB.parent)


@pytest.fixture(autouse=True)
def _state_dir_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    # The box adds the state dir only where it exists; DB's fixed directory
    # does not, so this file's pure argv tests treat it as present. Real
    # directories (tmp_path) are checked for real.
    real = codex_sandbox._is_dir
    monkeypatch.setattr(codex_sandbox, "_is_dir", lambda path: path == STATE or real(path))


def _add_dirs(argv: list[str]) -> list[str]:
    found = []
    for index, part in enumerate(argv):
        if part == "--add-dir":
            found.append(argv[index + 1])
        elif part.startswith("--add-dir="):
            found.append(part.partition("=")[2])
    return found


# --- (a) a sandboxed codex exec gets the state dir exactly once ---------------


@pytest.mark.parametrize(
    "command",
    [
        ["codex", "exec", "--sandbox", "workspace-write", "-"],
        ["codex", "exec", "-s", "workspace-write", "-"],
        ["codex", "exec", "--sandbox=workspace-write", "-"],
        ["codex", "exec", "-sworkspace-write", "-"],
        ["/home/codex-bot/.local/bin/codex", "exec", "--sandbox", "workspace-write", "-"],
        # global options before the subcommand, and the `e` alias
        ["codex", "-m", "gpt-5", "-s", "workspace-write", "exec", "-"],
        ["codex", "e", "--sandbox", "workspace-write", "-"],
        # a sandbox the config marks: -c sandbox_mode, or a workspace-write shorthand
        ["codex", "exec", "-c", 'sandbox_mode="workspace-write"', "-"],
        ["codex", "exec", "--config=sandbox_mode=workspace-write", "-"],
        ["codex", "exec", "--full-auto", "-"],
        # the last explicit --sandbox wins, as in codex
        ["codex", "exec", "-s", "read-only", "--sandbox", "workspace-write", "-"],
        # an --add-dir for some other directory is kept, and ours is added
        ["codex", "exec", "--add-dir", "/data/cache", "--sandbox", "workspace-write", "-"],
        # a relative or ~ path is not provably the state dir: ours is added
        ["codex", "exec", "--add-dir", "factory-state", "--sandbox", "workspace-write", "-"],
        ["codex", "exec", "--add-dir", "~/.local/state/factory", "-s", "workspace-write", "-"],
    ],
)
def test_a_sandboxed_codex_exec_gets_the_state_dir_once_right_after_exec(
    command: list[str],
) -> None:
    out = with_state_dir(command, DB)

    exec_at = next(i for i, part in enumerate(command) if part in ("exec", "e"))
    assert out[: exec_at + 1] == command[: exec_at + 1]
    assert out[exec_at + 1 : exec_at + 3] == ["--add-dir", STATE]
    assert out[exec_at + 3 :] == command[exec_at + 1 :]  # nothing else moves
    assert _add_dirs(out).count(STATE) == 1
    assert command == out[: exec_at + 1] + out[exec_at + 3 :]  # the input is not mutated


@pytest.mark.parametrize(
    "command",
    [
        # the box-local stopgap: the state dir is already there, by hand
        ["codex", "exec", "--add-dir", STATE, "--sandbox", "workspace-write", "-"],
        ["codex", "exec", f"--add-dir={STATE}", "--sandbox", "workspace-write", "-"],
        ["codex", "exec", "--add-dir", STATE + "/", "-s", "workspace-write", "-"],
        ["codex", "exec", "--add-dir", "/srv/factory-state/../factory-state", "-s",
         "workspace-write", "-"],
        ["codex", "--add-dir", STATE, "exec", "--sandbox", "workspace-write", "-"],
    ],
)
def test_a_state_dir_already_added_by_hand_is_not_added_again(command: list[str]) -> None:
    assert with_state_dir(command, DB) == command


def test_applying_it_twice_changes_nothing_the_second_time() -> None:
    once = with_state_dir(["codex", "exec", "--sandbox", "workspace-write", "-"], DB)
    assert with_state_dir(once, DB) == once


def test_the_state_dir_comes_from_the_resolved_db_path(tmp_path: Path) -> None:
    # A symlinked ORCH_DB: SQLite writes its -wal/-shm beside the real file,
    # and the waiter lock sits beside the path given. Both dirs, once each.
    real = tmp_path / "real"
    real.mkdir()
    (real / "orch.db").touch()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "orch.db").symlink_to(real / "orch.db")

    assert state_dirs(linked / "orch.db") == (linked, real)
    assert state_dirs(real / "orch.db") == (real,)
    out = with_state_dir(["codex", "exec", "-s", "workspace-write", "-"], linked / "orch.db")
    assert out == ["codex", "exec", "--add-dir", str(linked), "--add-dir", str(real),
                   "-s", "workspace-write", "-"]
    # A relative --db resolves against the working directory.
    assert state_dirs(Path("orch.db")) == (Path.cwd(),)


# --- (b) claude, cursor and unsandboxed codex commands are unchanged -----------


@pytest.mark.parametrize(
    "command",
    [
        ["claude", "-p", "--permission-mode", "acceptEdits"],
        ["/usr/local/bin/claude", "-p", "--sandbox", "workspace-write"],
        ["cursor-agent", "-p", "--force"],
        ["cursor-agent", "exec", "--sandbox", "workspace-write"],
        ["codex", "exec", "-"],
        ["codex", "exec", "--sandbox", "read-only", "-"],
        ["codex", "exec", "--sandbox", "danger-full-access", "-"],
        ["codex", "exec", "--sandbox", "workspace-write", "--sandbox", "read-only", "-"],
        ["codex", "exec", "--full-auto", "-s", "danger-full-access", "-"],
        ["codex", "exec", "-c", "sandbox_mode=read-only", "-"],
        ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "-s",
         "workspace-write", "-"],
        # not `codex exec`: the interactive TUI, or another subcommand
        ["codex", "--sandbox", "workspace-write"],
        ["codex", "review", "--sandbox", "workspace-write"],
        # an option's value is not the subcommand
        ["codex", "-m", "exec", "--sandbox", "workspace-write"],
        # words after `--` are the prompt, not options
        ["codex", "exec", "--", "--sandbox", "workspace-write"],
        # a wrapper is not codex
        ["codex-wrapper", "exec", "--sandbox", "workspace-write", "-"],
        ["env", "codex", "exec", "--sandbox", "workspace-write", "-"],
    ],
)
def test_other_commands_come_out_unchanged(command: list[str]) -> None:
    out = with_state_dir(command, DB)
    assert out == command
    assert out is not command


# --- (c) end to end: the waiter, orch job run, and a fake sandboxed codex ------

# Plays `codex exec` and its sandbox: a model command may write only inside
# its writable roots (the working directory, which is the job worktree, plus
# each --add-dir). With --sandbox workspace-write and the ORCH_DB's directory
# outside them, `orch job done` cannot write orch.db (or its -wal/-shm), so
# it is never run and the harness prints what the real one printed (#103).
# Anything else runs `orch job done` as the brief says. It records what it
# was given either way.
FAKE_CODEX = f"#!{sys.executable}\n" + """import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
roots, sandbox, index = [os.getcwd()], None, 0
while index < len(args):
    part = args[index]
    if part in ("--add-dir", "--sandbox", "-s") and index + 1 < len(args):
        if part == "--add-dir":
            roots.append(args[index + 1])
        else:
            sandbox = args[index + 1]
        index += 2
        continue
    index += 1
sys.stdin.read()
db = Path(os.environ["ORCH_DB"]).resolve()
resolved = [Path(root).resolve() for root in roots]
writable = sandbox != "workspace-write" or any(
    root == db.parent or root in db.parents for root in resolved
)
job = os.environ["ORCH_JOB_ID"]
Path(os.environ["FAKE_CODEX_DIR"], job + ".json").write_text(json.dumps(
    {"argv": args, "roots": roots, "writable": writable, "cwd": os.getcwd()}))
if not writable:
    print("orch job done: attempt to write a readonly database", flush=True)
    sys.exit(0)  # codex itself exits 0 after its turn
subprocess.run([sys.executable, os.environ["FAKE_ORCH"], "job", "done",
                os.environ["ORCH_JOB_BOT"], job, "--handoff", "c-" + job[:6]], check=True)
"""


def _job_row(db: Path, job_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT state, handoff_comment_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()


def _codex_waiter(tmp_path: Path, command: list[str]):
    """`orch wait codex-bot --autostart` with `command` as its harness config."""
    record_dir = tmp_path / "codex-records"
    record_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_exe(bin_dir / "gh", FAKE_GH)
    factory_md = tmp_path / "factory.md"
    factory_md.write_text(FACTORY_MD.replace("base_branch: dev", "base_branch: main"),
                          encoding="utf-8")
    orch_yml = tmp_path / "orch.yml"
    orch_yml.write_text(
        "poll_seconds: 60\n"
        "trusted_logins:\n  maxharris1: [grok-bot, claude-bot, codex-bot, cursor-bot]\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: main\n",
        encoding="utf-8",
    )
    config = write_config(
        tmp_path / "waiter-codex-bot.yml",
        "bot: codex-bot\ncommand:\n" + "".join(f"  - {json.dumps(part)}\n" for part in command),
    )
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONUNBUFFERED": "1",
        "FAKE_CODEX_DIR": str(record_dir),
        "FAKE_ORCH": str(ORCH),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_FACTORY": str(factory_md),
        "ORCH_WORK_ROOT": str(tmp_path / "work"),
        "ORCH_GIT_URL_TEMPLATE": _git_remote(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for name in ("ORCH_DB", "ORCH_JOB_PROOF", "GH_TOKEN"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [sys.executable, str(ORCH), "wait", "codex-bot", "--autostart",
         "--db", str(db), "--machines", str(MACHINES), "--orch", str(orch_yml),
         "--harness-config", str(config), "--state-dir", str(tmp_path / "state")],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc, db, queue, record_dir


def _finished(db: Path, job_id: str) -> bool:
    return _job_row(db, job_id)[0] in ("done", "failed")


def _stop(proc: subprocess.Popen) -> str:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return proc.stderr.read() if proc.stderr else ""


def _run_codex_job(tmp_path: Path, command: list[str]) -> tuple[Path, str, dict, str]:
    proc, db, queue, record_dir = _codex_waiter(tmp_path, command)
    try:
        job_id = enqueue(queue, "a", recipient="codex", head=None)
        _wait_for(lambda: _finished(db, job_id), 30, proc)
        record = json.loads((record_dir / f"{job_id}.json").read_text(encoding="utf-8"))
    finally:
        stderr = _stop(proc)
    return db, job_id, record, stderr


def test_a_sandboxed_codex_job_finishes_done_because_the_state_dir_was_added(
    tmp_path: Path,
) -> None:
    (tmp_path / "bin-codex").mkdir()
    codex = _write_exe(tmp_path / "bin-codex" / "codex", FAKE_CODEX)
    db, job_id, record, stderr = _run_codex_job(
        tmp_path, [str(codex), "exec", "--sandbox", "workspace-write", "-"]
    )

    assert record["argv"] == ["exec", "--add-dir", str(tmp_path), "--json", "--sandbox",
                              "workspace-write", "-"], stderr
    assert record["writable"] is True
    assert Path(record["cwd"]).is_relative_to(tmp_path / "work")  # the job worktree
    assert _job_row(db, job_id) == ("done", f"c-{job_id[:6]}")


def test_a_box_local_manual_add_dir_stopgap_still_works_and_is_not_doubled(
    tmp_path: Path,
) -> None:
    (tmp_path / "bin-codex").mkdir()
    codex = _write_exe(tmp_path / "bin-codex" / "codex", FAKE_CODEX)
    command = [str(codex), "exec", "--add-dir", str(tmp_path), "--sandbox", "workspace-write", "-"]
    db, job_id, record, stderr = _run_codex_job(tmp_path, command)

    assert record["argv"] == ["exec", "--json", *command[2:]], stderr
    assert _add_dirs(record["argv"]) == [str(tmp_path)]
    assert _job_row(db, job_id) == ("done", f"c-{job_id[:6]}")


def test_without_the_state_dir_the_fake_sandbox_fails_the_job_as_before(
    tmp_path: Path,
) -> None:
    # The control: the same fake under a name that is not `codex` gets no
    # --add-dir, its `orch job done` cannot write, and the job ends failed,
    # exactly as every Codex job did before #103.
    (tmp_path / "bin-codex").mkdir()
    wrapped = _write_exe(tmp_path / "bin-codex" / "codex-wrapper", FAKE_CODEX)
    db, job_id, record, stderr = _run_codex_job(
        tmp_path, [str(wrapped), "exec", "--sandbox", "workspace-write", "-"]
    )

    assert record["argv"] == ["exec", "--sandbox", "workspace-write", "-"], stderr
    assert record["writable"] is False
    assert _job_row(db, job_id) == ("failed", None)


# --- the box: the state dir only where it exists ------------------------------


def test_the_box_adds_no_state_dir_that_does_not_exist(tmp_path: Path) -> None:
    command = ["codex", "exec", "--sandbox", "workspace-write", "-"]
    missing = tmp_path / "gone" / "orch.db"
    assert with_state_dir(command, missing) == command
    present = tmp_path / "orch.db"
    assert with_state_dir(command, present) == [
        "codex", "exec", "--add-dir", str(tmp_path), "--sandbox", "workspace-write", "-"
    ]


# --- remote (placed) runs: codex by name, --add-dir only where it exists -------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # the box's own config, as the PC got it: absolute binary, box stopgap
        (["/home/box/.local/bin/codex", "exec", "--add-dir", "/home/box/.local/state/factory",
          "--sandbox", "workspace-write", "-"],
         DeviceCommand(("codex", "exec", "--sandbox", "workspace-write", "-"),
                       ("/home/box/.local/state/factory",), 2)),
        (["/home/box/.local/bin/codex", "exec", "--sandbox", "workspace-write", "-"],
         DeviceCommand(("codex", "exec", "--sandbox", "workspace-write", "-"))),
        (["codex", "-m", "gpt-5", "exec", "--add-dir=/a", "-s", "workspace-write",
          "--add-dir", "/b", "--add-dir", "/a", "-"],
         DeviceCommand(("codex", "-m", "gpt-5", "exec", "-s", "workspace-write", "-"),
                       ("/a", "/b"), 4)),
        # an option's value, or a word after --, is not an --add-dir
        (["codex", "exec", "-m", "--add-dir", "-"],
         DeviceCommand(("codex", "exec", "-m", "--add-dir", "-"))),
        (["codex", "exec", "--", "--add-dir", "/x"],
         DeviceCommand(("codex", "exec", "--", "--add-dir", "/x"))),
        # claude, cursor and wrappers are run exactly as configured
        (["/home/box/.local/bin/claude", "-p", "--add-dir", "/x"],
         DeviceCommand(("/home/box/.local/bin/claude", "-p", "--add-dir", "/x"))),
        (["cursor-agent", "-p", "--force"], DeviceCommand(("cursor-agent", "-p", "--force"))),
        (["env", "codex", "exec", "--add-dir", "/x"],
         DeviceCommand(("env", "codex", "exec", "--add-dir", "/x"))),
        # codex resolves a relative --add-dir against its -C/--cd directory
        (["codex", "exec", "-C", "/p", "--add-dir", "../s", "-"],
         DeviceCommand(("codex", "exec", "-C", "/p", "-"), ("../s",), 4, "/p")),
        (["codex", "exec", "--cd", "p", "--add-dir", "s", "-"],
         DeviceCommand(("codex", "exec", "--cd", "p", "-"), ("s",), 4, "p")),
        (["codex", "exec", "--cd=/p", "--add-dir", "s", "-"],
         DeviceCommand(("codex", "exec", "--cd=/p", "-"), ("s",), 3, "/p")),
        (["codex", "exec", "--add-dir", "s", "-C/p", "-"],
         DeviceCommand(("codex", "exec", "-C/p", "-"), ("s",), 2, "/p")),
        (["codex", "-C", "/a", "exec", "-C", "/b", "--add-dir", "s", "-"],
         DeviceCommand(("codex", "-C", "/a", "exec", "-C", "/b", "-"), ("s",), 6, "/b")),
        (["codex", "exec", "--add-dir", "s", "--", "-C", "/p"],
         DeviceCommand(("codex", "exec", "--", "-C", "/p"), ("s",), 2)),
    ],
)
def test_a_device_command_names_codex_bare_and_guards_its_add_dirs(
    command: list[str], expected: DeviceCommand
) -> None:
    assert for_device(command) == expected


BOX_CODEX = "/home/box/.local/bin/codex"


def _placed_codex_run(tmp_path: Path, command: list[str], *, home_dirs: tuple[str, ...] = ()):
    """`orch job run` of a codex job the pool placed on the pc, over fake ssh.

    The device's login shell (its ~/.bash_profile) puts device-bin, which
    holds a fake `codex` that records its argv, first on PATH.
    """
    machines, queue, pool, job_id, ssh = remote_start.remote_setup(tmp_path, "codex")
    report = tmp_path / "remote-argv.json"
    device_bin = tmp_path / "device-bin"
    device_bin.mkdir()
    _write_exe(
        device_bin / "codex",
        f"#!{sys.executable}\nimport json, sys\n"
        f"json.dump(sys.argv[1:], open({str(report)!r}, 'w'))\n",
    )
    home = tmp_path / "device-home"
    home.mkdir()
    (home / ".bash_profile").write_text(
        f'PATH="{device_bin}:$PATH"\nexport PATH\n', encoding="utf-8"
    )
    for name in home_dirs:
        (tmp_path / name).mkdir()
    proc = remote_start.orch(
        "job", "run", "codex-bot", job_id, "--db", str(tmp_path / "orch.db"),
        "--machines", str(machines), "--", *command,
        env={**ssh.env(), "FAKE_SSH_HOME": str(home)},
    )
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    argv = json.loads(report.read_text(encoding="utf-8"))
    return argv, ssh.calls(), err


def test_a_placed_codex_review_resolves_codex_by_name_with_no_box_path(
    tmp_path: Path,
) -> None:
    # The PC failure (exit 127): the box's absolute codex path, and its
    # state dir stopgap, in the shared waiter config.
    # The stopgap dir as the box names it; this device has no such directory
    # (under tmp_path, so the host running the tests cannot have it either).
    box_state = str(tmp_path / "box" / ".local" / "state" / "factory")
    argv, calls, err = _placed_codex_run(
        tmp_path,
        [BOX_CODEX, "exec", "--add-dir", box_state, "--sandbox", "workspace-write", "-"],
    )

    assert argv == ["exec", "--json", "--sandbox", "workspace-write", "-"], err
    script = calls[0]["script"]
    assert BOX_CODEX not in script
    assert "/home/box/.local/bin" not in script
    assert " codex exec " in script  # the bare name, found on the device's PATH
    assert "test -d" in script  # the stopgap dir is only tried, not assumed
    assert str(tmp_path) not in argv  # no box ORCH_DB dir is sent to the device
    assert "runs as 'codex' from its login PATH" in err


def test_a_placed_codex_gets_an_add_dir_only_where_the_device_has_it(tmp_path: Path) -> None:
    present = str(tmp_path / "device-state")
    absent = str(tmp_path / "no-such-dir")
    argv, calls, err = _placed_codex_run(
        tmp_path,
        [BOX_CODEX, "exec", "--add-dir", absent, "--add-dir", present,
         "--sandbox", "workspace-write", "-"],
        home_dirs=("device-state",),
    )

    assert argv == ["exec", "--json", "--add-dir", present, "--sandbox", "workspace-write", "-"], err
    assert argv.count("--add-dir") == 1
    assert absent not in argv


def test_a_placed_codex_with_no_add_dir_gets_none(tmp_path: Path) -> None:
    argv, calls, err = _placed_codex_run(
        tmp_path, ["codex", "exec", "--sandbox", "workspace-write", "-"]
    )

    assert argv == ["exec", "--json", "--sandbox", "workspace-write", "-"], err
    assert "--add-dir" not in calls[0]["script"]
    assert "test -d" not in calls[0]["script"]
    assert "added --add-dir" not in err


def _device_jobs_dir(tmp_path: Path) -> Path:
    """The parent of the fake device's job directory (machines_file's workdir_root)."""
    return tmp_path / "remote" / "codex-bot" / "work" / "jobs"


@pytest.mark.parametrize(
    "cd", [["-C", "{project}"], ["--cd", "{project}"], ["--cd={project}"], ["-C{project}"]]
)
def test_a_relative_add_dir_is_checked_where_codex_resolves_it(
    tmp_path: Path, cd: list[str]
) -> None:
    # Codex resolves a relative --add-dir against its -C/--cd directory, not
    # the directory it was started in (the job directory).
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "shared").mkdir()
    flags = [part.format(project=project) for part in cd]
    argv, _, err = _placed_codex_run(
        tmp_path,
        [BOX_CODEX, "exec", *flags, "--add-dir", "../shared", "-s", "workspace-write", "-"],
    )

    assert argv == ["exec", "--json", *flags, "--add-dir", "../shared", "-s", "workspace-write", "-"], err


def test_a_relative_add_dir_beside_only_the_job_directory_is_not_given(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (_device_jobs_dir(tmp_path) / "shared").mkdir(parents=True)
    argv, _, err = _placed_codex_run(
        tmp_path,
        [BOX_CODEX, "exec", "-C", str(project), "--add-dir", "../shared",
         "-s", "workspace-write", "-"],
    )

    assert argv == ["exec", "--json", "-C", str(project), "-s", "workspace-write", "-"], err


def test_a_relative_cd_and_add_dir_resolve_from_the_job_directory(tmp_path: Path) -> None:
    # -C itself is relative to the job directory; the --add-dir to that.
    (_device_jobs_dir(tmp_path) / "shared").mkdir(parents=True)
    argv, _, err = _placed_codex_run(
        tmp_path,
        ["codex", "exec", "--cd", "..", "--add-dir", "shared", "--add-dir", "../shared",
         "-s", "workspace-write", "-"],
    )

    assert argv == ["exec", "--json", "--cd", "..", "--add-dir", "shared",
                    "-s", "workspace-write", "-"], err
