"""`orch wait <bot> --autostart`: the waiter starts claimed jobs itself (#80).

On claim the waiter runs `orch job run <bot> <job-id> --worktree -- <the
bot's own harness command>` with a rendered standard brief on stdin, then
goes back to waiting. No model turn and no bot-to-bot message sits between
the claim and the start. The harness command comes only from the bot's own
config file; nothing from a job row or a comment reaches an argv.
"""

from __future__ import annotations

import ast
import base64
import re
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib import waiter as waiter_module
from lib.device_pool import DevicePool
from lib.dispatcher import Comment
from lib.job_state import RUNNING, derive, read_placements
from lib.machines_yml import load_machines_yml
from lib.orch_yml import load_orch_yml
from lib.queue import JobRequest, SQLiteJobQueue
from lib.waiter import (
    ContextResult,
    GhContextSource,
    HarnessCommand,
    Waiter,
    WaiterError,
    job_run_argv,
    load_harness_command,
    local_only,
)
from tests.test_brief import FACTORY_MD, detail
from tests.test_wait import GO, dispatcher_for, tracked

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
HARNESS_BINARIES = ("claude", "codex", "cursor-agent")
MACHINES = ROOT / "machines.yml"


# --- the harness command comes from the bot's own fixed config -------------


def write_config(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_harness_command_is_an_argv_list_for_this_bot(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "waiter-claude-bot.yml",
        "bot: claude-bot\ncommand:\n  - my-harness\n  - -p\n  - --flag=value with spaces\n",
    )
    loaded = load_harness_command(path, "claude-bot")
    assert loaded == HarnessCommand(
        bot="claude-bot", command=("my-harness", "-p", "--flag=value with spaces")
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("bot: codex-bot\ncommand: [x]\n", "codex-bot"),
        ("bot: claude-bot\ncommand: my-harness -p\n", "list"),
        ("bot: claude-bot\ncommand: []\n", "empty"),
        ("bot: claude-bot\ncommand: [x, 3]\n", "string"),
        ("bot: claude-bot\ncommand: ['', x]\n", "empty"),
        ("bot: claude-bot\ncommand: ['-x']\n", "option"),
        ("bot: claude-bot\ncommand: [\"a\\u0000b\"]\n", "control"),
        ("bot: claude-bot\ncommand: [\"a\\nb\"]\n", "control"),
        ("bot: claude-bot\ncommand: [x]\nshell: true\n", "unknown"),
        ("command: [x]\n", "bot"),
        ("- x\n", "mapping"),
    ],
)
def test_a_bad_harness_config_is_refused(tmp_path: Path, text: str, message: str) -> None:
    path = write_config(tmp_path / "waiter.yml", text)
    with pytest.raises(WaiterError, match=message):
        load_harness_command(path, "claude-bot")


@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_harness_config_others_can_write_is_refused(tmp_path: Path, mode: int) -> None:
    path = write_config(tmp_path / "waiter.yml", "bot: claude-bot\ncommand: [x]\n", mode)
    with pytest.raises(WaiterError, match="writable"):
        load_harness_command(path, "claude-bot")


def test_a_missing_harness_config_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WaiterError, match="harness config"):
        load_harness_command(tmp_path / "nope.yml", "claude-bot")


def test_a_symlinked_harness_config_is_refused(tmp_path: Path) -> None:
    real = write_config(tmp_path / "real.yml", "bot: claude-bot\ncommand: [x]\n")
    link = tmp_path / "waiter.yml"
    link.symlink_to(real)
    with pytest.raises(WaiterError, match="regular file"):
        load_harness_command(link, "claude-bot")


def test_job_run_argv_is_fixed_except_the_validated_job_id() -> None:
    argv = job_run_argv(
        ("python3", "/f/bin/orch"),
        bot="claude-bot",
        job_id="0123456789abcdef0123456789abcdef",
        db=Path("/s/orch.db"),
        machines=Path("/f/machines.yml"),
        command=("my-harness", "-p"),
    )
    assert argv == [
        "python3", "/f/bin/orch", "job", "run", "claude-bot",
        "0123456789abcdef0123456789abcdef",
        "--db", "/s/orch.db", "--machines", "/f/machines.yml", "--worktree",
        "--", "my-harness", "-p",
    ]


@pytest.mark.parametrize(
    "job_id", ["", "--worktree", "-x", "a b", "a;b", "$(id)", "a/b", "../x", "x\n", "é"]
)
def test_a_job_id_that_is_not_a_plain_token_never_reaches_an_argv(job_id: str) -> None:
    with pytest.raises(WaiterError):
        job_run_argv(
            ("orch",), bot="claude-bot", job_id=job_id, db=Path("/d"),
            machines=Path("/m"), command=("x",),
        )


def test_job_run_argv_refuses_a_bot_that_is_not_a_harness_bot() -> None:
    with pytest.raises(WaiterError):
        job_run_argv(
            ("orch",), bot="grok-bot", job_id="abc", db=Path("/d"),
            machines=Path("/m"), command=("x",),
        )


# --- the waiter loop, with a fake spawn -------------------------------------


class FakeChild:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class FakeSpawn:
    def __init__(self, error: BaseException | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.stdin: list[bytes] = []
        self.children: list[FakeChild] = []
        self.error = error

    def __call__(self, argv, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append((list(argv), kwargs))
        self.stdin.append(kwargs["stdin"].read())
        child = FakeChild(10_000 + len(self.children))
        self.children.append(child)
        return child


class FakeContext:
    def __init__(self, result: ContextResult | None = None):
        self.repos: list[str] = []
        self.result = result or ContextResult(None, None, "test: no factory.md")

    def load(self, repo: str) -> ContextResult:
        self.repos.append(repo)
        return self.result


def make_queue(tmp_path: Path) -> tuple[Path, SQLiteJobQueue]:
    db = tmp_path / "orch.db"
    return db, SQLiteJobQueue(db, clock=time.time)


def local_pool(queue: SQLiteJobQueue, machines: Path = MACHINES) -> DevicePool:
    config, findings = load_machines_yml(machines, allow_placeholders=True)
    assert config is not None, findings
    return DevicePool(queue, local_only(config))


def enqueue(queue: SQLiteJobQueue, key: str, **fields) -> str:
    action = fields.pop("action", "build")
    request = JobRequest(
        repo=fields.pop("repo", "owner/repo"),
        ticket_id=fields.pop("ticket_id", "42"),
        action=action,
        recipient=fields.pop("recipient", "claude"),
        source_comment_id=fields.pop("source_comment_id", "c-1"),
        timeout_seconds=fields.pop("timeout_seconds", 3600),
        head=fields.pop("head", "3f9c2ab"),
        detail=fields.pop("detail", detail(action=action)),
    )
    assert not fields
    return queue.enqueue(request, dedupe_key=key).id


def make_waiter(
    tmp_path: Path,
    queue: SQLiteJobQueue,
    db: Path,
    spawn: FakeSpawn,
    *,
    context: FakeContext | None = None,
    command: tuple[str, ...] = ("fake-harness", "-p"),
    log: list[str] | None = None,
    machines: Path = MACHINES,
) -> Waiter:
    return Waiter(
        bot="claude-bot",
        queue=queue,
        pool=local_pool(queue, machines),
        harness=HarnessCommand(bot="claude-bot", command=command),
        context=context or FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=machines,
        orch=("python3", "/factory/bin/orch"),
        spawn=spawn,
        log=(log if log is not None else []).append,
        environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def state(db: Path, job_id: str) -> str:
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def slots(db: Path) -> dict[str, str]:
    with sqlite3.connect(db) as connection:
        return dict(connection.execute("SELECT job_id, device FROM device_slots").fetchall())


def test_on_claim_the_waiter_starts_orch_job_run_with_the_brief_on_stdin(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    context = FakeContext()
    waiter = make_waiter(tmp_path, queue, db, spawn, context=context)

    assert waiter.step() == job_id

    assert len(spawn.calls) == 1
    argv, kwargs = spawn.calls[0]
    assert argv == [
        "python3", "/factory/bin/orch", "job", "run", "claude-bot", job_id,
        "--db", str(db), "--machines", str(MACHINES), "--worktree",
        "--", "fake-harness", "-p",
    ]
    assert kwargs["start_new_session"] is True
    assert kwargs.get("shell", False) is False
    brief = tmp_path / "state" / "jobs" / job_id / "brief.md"
    assert kwargs["env"]["ORCH_JOB_BRIEF"] == str(brief)
    assert spawn.stdin[0] == brief.read_bytes()
    text = brief.read_text(encoding="utf-8")
    assert "kind: implement" in text and f"job id: {job_id}" in text
    assert stat.S_IMODE(brief.stat().st_mode) == 0o600
    assert stat.S_IMODE(brief.parent.stat().st_mode) == 0o700
    assert context.repos == ["owner/repo"]
    # The claim holds the box slot; `orch job run` takes it from here.
    assert state(db, job_id) == "running"
    assert slots(db) == {job_id: "box"}


def test_after_a_start_the_waiter_goes_back_to_waiting(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    first = enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() == first
    assert waiter.step() is None  # nothing queued: keeps waiting, starts nothing
    assert len(spawn.calls) == 1
    second = enqueue(queue, "b")
    assert waiter.step() == second
    assert [call[0][5] for call in spawn.calls] == [first, second]


def test_the_waiter_only_takes_its_own_bots_jobs(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    codex_job = enqueue(queue, "a", recipient="codex", action="review",
                        detail=detail(action="review", round="1/3"))
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() is None
    assert spawn.calls == []
    assert state(db, codex_job) == "queued"


def test_a_finished_run_is_reaped_and_logged(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    waiter.step()
    assert waiter.running() == {job_id}
    spawn.children[0].returncode = 3
    waiter.step()
    assert waiter.running() == set()
    assert any(job_id in line and "exited 3" in line for line in log)


def test_a_job_with_no_standard_brief_is_failed_for_t6_and_its_slot_freed(
    tmp_path: Path,
) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a", action="clarify", detail=None)
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    assert waiter.step() is None
    assert spawn.calls == []
    assert state(db, job_id) == "failed"
    assert slots(db) == {}
    assert any("clarify" in line for line in log)


def test_a_spawn_that_fails_fails_the_job_for_t6_and_frees_its_slot(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn(error=OSError("no such file"))
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() is None
    assert state(db, job_id) == "failed"
    assert slots(db) == {}


def test_factory_md_context_reaches_the_brief(tmp_path: Path) -> None:
    from lib.factory_md import parse_factory_context

    parsed, _ = parse_factory_context(FACTORY_MD)
    db, queue = make_queue(tmp_path)
    enqueue(queue, "a")
    spawn = FakeSpawn()
    waiter = make_waiter(
        tmp_path, queue, db, spawn, context=FakeContext(ContextResult(parsed, "dev", None))
    )
    waiter.step()
    text = spawn.stdin[0].decode("utf-8")
    assert "owner/repo factory.md at dev" in text
    assert "Never apply migrations" in text


def test_the_autostart_waiter_claims_remote_slots_too(tmp_path: Path) -> None:
    # Issue #85: a remote job's harness ends with an ORCH-HANDOFF line and the
    # waiter runs `orch job done` on the box, so remote devices are claimed
    # too (tests/test_waiter_remote.py covers the remote run itself).
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n"
        "  - name: box\n    local: true\n    slot_max: 1\n"
        "    harnesses: {claude: claude-bot}\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 3\n"
        "    harnesses: {claude: claude-bot}\n"
        "    workdir_root: /home/{bot}/factory-work\n    ssh_alias: pc-{bot}\n",
        encoding="utf-8",
    )
    db, queue = make_queue(tmp_path)
    first = enqueue(queue, "a")
    second = enqueue(queue, "b")
    spawn = FakeSpawn()
    config, findings = load_machines_yml(machines, allow_placeholders=True)
    assert config is not None, findings
    waiter = make_waiter(tmp_path, queue, db, spawn, machines=machines)
    waiter.pool = DevicePool(queue, config)
    assert waiter.step() == first
    assert waiter.step() == second
    assert slots(db) == {first: "pc", second: "pc"}  # least loaded: pc has 3 free


def test_serve_runs_until_stopped_and_leaves_running_jobs_alone(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    spawn = FakeSpawn()
    log: list[str] = []
    waiter = make_waiter(tmp_path, queue, db, spawn, log=log)
    stop = threading.Event()
    thread = threading.Thread(target=waiter.serve, args=(stop,), kwargs={"poll": 0.01})
    thread.start()
    deadline = time.monotonic() + 5
    while not spawn.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    later = enqueue(queue, "b")
    while len(spawn.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert [call[0][5] for call in spawn.calls] == [job_id, later]
    assert any("leaving 2 running" in line for line in log)


# --- no command injection from job fields -----------------------------------

HOSTILE = [
    "$(touch /tmp/factory-pwned)",
    "`touch /tmp/factory-pwned`",
    "; touch /tmp/factory-pwned #",
    "| sh",
    "--worktree",
    "--help",
    "&& id",
    "a\nb",
    "' \" \\",
    "${IFS}",
]


def test_no_job_field_reaches_the_argv_or_the_environment(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    for index, value in enumerate(HOSTILE):
        enqueue(
            queue,
            f"k{index}",
            repo=f"owner/repo{index}",
            ticket_id=value,
            source_comment_id=value,
            head=value,
            detail=json.dumps({"brief": value, "instruction": value, "pr_number": value,
                               "risk": value, "round": value, "base_branch": value}),
        )
        started = waiter.step()
        assert started is not None
        # The harness runs `orch job done`, then `orch job run` frees the box
        # slot once its command ends. (A running job with no slot would still
        # count on the box: issue #90.)
        queue.finish(started, owner="claude", handoff_comment_id="1")
        waiter.pool.release(started, owner="claude")
    assert len(spawn.calls) == len(HOSTILE)
    for (argv, kwargs), value in zip(spawn.calls, HOSTILE):
        assert argv[:5] == ["python3", "/factory/bin/orch", "job", "run", "claude-bot"]
        assert argv[6:] == ["--db", str(db), "--machines", str(MACHINES), "--worktree",
                            "--", "fake-harness", "-p"]
        assert re.fullmatch(r"[0-9a-f]{32}", argv[5])  # the job id, a plain token
        assert all(value not in item for item in kwargs["env"].values())
        assert kwargs.get("shell", False) is False
    # The text is in the brief, as data.
    assert any(HOSTILE[0].encode() in data for data in spawn.stdin)
    assert not Path("/tmp/factory-pwned").exists()


def test_a_job_row_with_a_hostile_id_is_failed_and_never_started(tmp_path: Path) -> None:
    db, queue = make_queue(tmp_path)
    job_id = enqueue(queue, "a")
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE jobs SET id = '--worktree' WHERE id = ?", (job_id,))
    spawn = FakeSpawn()
    waiter = make_waiter(tmp_path, queue, db, spawn)
    assert waiter.step() is None
    assert spawn.calls == []
    assert state(db, "--worktree") == "failed"


def test_waiter_and_brief_name_no_harness_and_use_no_shell() -> None:
    for name in ("waiter.py", "brief.py"):
        tree = ast.parse((ROOT / "lib" / name).read_text(encoding="utf-8"))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not literals & set(HARNESS_BINARIES), name
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert isinstance(node.value, ast.Constant) and node.value.value is False
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert f"{node.value.id}.{node.attr}" not in ("os.system", "os.popen"), name
    assert "subprocess" not in _imports(ROOT / "lib" / "brief.py")


# --- factory.md is read from the trusted base branch, without tokens --------


class FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0, error: Exception | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def orch_config(tmp_path: Path):
    path = tmp_path / "orch.yml"
    path.write_text(
        "poll_seconds: 60\n"
        "trusted_logins:\n  maxharris1: [grok-bot, claude-bot, codex-bot, cursor-bot]\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: dev\n",
        encoding="utf-8",
    )
    config, findings = load_orch_yml(path)
    assert config is not None, findings
    return config


def contents(text: str) -> str:
    return json.dumps(
        {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
    )


def test_factory_md_is_read_from_factory_ref_with_gh_tokens_unset(tmp_path: Path) -> None:
    run = FakeRun(stdout=contents(FACTORY_MD))
    environ = {"PATH": "/usr/bin", "GH_TOKEN": "x", "GITHUB_TOKEN": "y",
               "GH_ENTERPRISE_TOKEN": "z", "GITHUB_ENTERPRISE_TOKEN": "w", "HOME": "/h"}
    source = GhContextSource(orch_config(tmp_path), run=run, environ=environ)
    result = source.load("owner/repo")
    assert result.context is not None and result.ref == "dev" and result.note is None
    assert "Never apply migrations" in result.context.body
    argv, kwargs = run.calls[0]
    assert argv == ["gh", "api", "-X", "GET", "repos/owner/repo/contents/factory.md",
                    "-f", "ref=dev"]
    assert kwargs.get("shell", False) is False
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN"} & set(kwargs["env"])
    assert kwargs["timeout"] > 0


def test_factory_md_lookup_is_case_insensitive_on_the_repo(tmp_path: Path) -> None:
    run = FakeRun(stdout=contents(FACTORY_MD))
    result = GhContextSource(orch_config(tmp_path), run=run, environ={}).load("Owner/Repo")
    assert result.context is not None


@pytest.mark.parametrize(
    ("run", "repo", "note"),
    [
        (FakeRun(stdout=contents(FACTORY_MD)), "other/repo", "not in orch.yml"),
        (FakeRun(returncode=1), "owner/repo", "gh api exited 1"),
        (FakeRun(error=subprocess.TimeoutExpired(["gh"], 30)), "owner/repo", "TimeoutExpired"),
        (FakeRun(error=FileNotFoundError("gh")), "owner/repo", "FileNotFoundError"),
        (FakeRun(stdout="not json"), "owner/repo", "not base64"),
        (FakeRun(stdout=contents("no front matter")), "owner/repo", "front matter"),
        (FakeRun(stdout=contents(FACTORY_MD.replace("base_branch: dev", "base_branch: main"))),
         "owner/repo", "base_branch"),
    ],
)
def test_a_factory_md_that_cannot_be_read_gives_a_note_not_a_crash(
    tmp_path: Path, run: FakeRun, repo: str, note: str
) -> None:
    result = GhContextSource(orch_config(tmp_path), run=run, environ={}).load(repo)
    assert result.context is None
    assert note in (result.note or "")


@pytest.mark.parametrize("repo", ["owner", "owner/repo/x", "-x/y", "o/$(id)", "../x", "o/r?ref=x"])
def test_an_odd_repo_name_is_never_sent_to_gh(tmp_path: Path, repo: str) -> None:
    run = FakeRun(stdout=contents(FACTORY_MD))
    result = GhContextSource(orch_config(tmp_path), run=run, environ={}).load(repo)
    assert result.context is None
    assert run.calls == []


# --- the dispatcher and `orch poll` still never exec a harness --------------


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_dispatcher_and_poller_modules_do_not_import_the_waiter_or_job_run() -> None:
    for name in ("dispatcher.py", "poll_source.py", "device_monitor.py", "job_state.py",
                 "grok_delivery.py", "linear_webhook.py"):
        imports = _imports(ROOT / "lib" / name)
        assert not {"lib.waiter", "lib.job_run", "lib.brief", "lib.placed_run"} & imports, name
        assert "lib.waiter" not in imports
    dispatcher_imports = _imports(ROOT / "lib" / "dispatcher.py")
    assert "subprocess" not in dispatcher_imports


def test_orch_poll_never_reaches_the_waiter() -> None:
    tree = ast.parse((ROOT / "lib" / "cli.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for name in ("_poll", "_runtime", "_init_db"):
        names = {
            n.id for n in ast.walk(functions[name]) if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(functions[name]) if isinstance(n, ast.Attribute)
        }
        assert not {"Waiter", "waiter", "job_run", "serve_autostart"} & names, name


def test_the_dispatcher_queues_a_harness_job_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "harness-ran"
    for name in (*HARNESS_BINARIES, "orch"):
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\necho {name} >> {marker}\n", encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    started: list[list[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(argv, *args, **kwargs):
        started.append(list(argv) if not isinstance(argv, str) else [argv])
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(os, "execvp", lambda *a: started.append(list(a)))
    monkeypatch.setattr(os, "execv", lambda *a: started.append(list(a)))
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    dispatcher = dispatcher_for(db)
    dispatcher.process(tracked(Comment(id="go-1", author="grok-login", body=GO, created_at=1)))
    dispatcher.reconcile_jobs()
    with sqlite3.connect(db) as connection:
        rows = connection.execute("SELECT recipient, action, state FROM jobs").fetchall()
    assert ("claude", "build", "queued") in rows
    assert started == []
    assert not marker.exists()


# --- end to end: `orch wait <bot> --autostart`, a fake harness, no messages --

FAKE_HARNESS = f"#!{sys.executable}\n" + """# Stands in for the bot's harness: records what it was given, then waits for
# the test to let it finish the job the way the brief says.
import json, os, subprocess, sys, time
from pathlib import Path

out = Path(os.environ["FAKE_HARNESS_DIR"])
job = os.environ["ORCH_JOB_ID"]
(out / f"{job}.json").write_text(json.dumps({
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "brief_env": os.environ.get("ORCH_JOB_BRIEF"),
    "cwd": os.getcwd(),
    "job_bot": os.environ.get("ORCH_JOB_BOT"),
    "db": os.environ.get("ORCH_DB"),
}))
go = out / f"{job}.go"
deadline = time.time() + 60
while not go.exists() and time.time() < deadline:
    time.sleep(0.05)
subprocess.run([sys.executable, os.environ["FAKE_ORCH"], "job", "done",
                os.environ["ORCH_JOB_BOT"], job, "--handoff", "c-" + job[:6]], check=True)
"""

FAKE_GH = f"#!{sys.executable}\n" + """import base64, json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_GH_LOG"])
with log.open("a") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:],
                             "token": bool(os.environ.get("GH_TOKEN"))}) + "\\n")
text = Path(os.environ["FAKE_GH_FACTORY"]).read_text()
print(json.dumps({"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}))
"""


def _write_exe(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _git_remote(tmp_path: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    work = tmp_path / "src"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=env)
    (work / "README").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True, env=env)
    bare = tmp_path / "remotes" / "owner" / "repo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, env=env)
    return f"file://{tmp_path}/remotes/{{repo}}.git"


def _wait_for(predicate, seconds: float = 20.0, proc: subprocess.Popen | None = None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"waiter exited {proc.returncode}: {proc.stderr.read()}")
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def autostart(tmp_path: Path):
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    harness = _write_exe(bin_dir / "fake-harness", FAKE_HARNESS)
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
        tmp_path / "waiter-claude-bot.yml",
        f"bot: claude-bot\ncommand:\n  - {harness}\n  - --from-config\n",
    )
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONUNBUFFERED": "1",
        "FAKE_HARNESS_DIR": str(harness_dir),
        "FAKE_ORCH": str(ORCH),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_FACTORY": str(factory_md),
        "GH_TOKEN": "must-not-reach-gh",
        "ORCH_WORK_ROOT": str(tmp_path / "work"),
        "ORCH_GIT_URL_TEMPLATE": _git_remote(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env.pop("ORCH_DB", None)
    proc = subprocess.Popen(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--autostart",
         "--db", str(db), "--machines", str(MACHINES), "--orch", str(orch_yml),
         "--harness-config", str(config), "--state-dir", str(tmp_path / "state")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield {"proc": proc, "db": db, "queue": queue, "harness_dir": harness_dir,
               "gh_log": tmp_path / "gh.log", "harness": harness}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for go in harness_dir.glob("*.json"):
            (harness_dir / (go.stem + ".go")).touch()
        time.sleep(0.3)


def _running(db: Path, job_id: str) -> bool:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        placements = read_placements(connection)
    placement = placements.get(job_id)
    if row is None or row["state"] != "running" or placement is None:
        return False
    if placement.runner_pid is None:
        return False
    return derive(
        last_activity_at=float(row["last_activity_at"]),
        now=time.time(),
        placement=placement,
        stale_seconds=120,
    ) == RUNNING and float(row["last_activity_at"]) > placement.placed_at


def test_a_queued_job_goes_from_claimed_to_running_with_a_heartbeat_and_no_message(
    autostart: dict,
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    started_at = time.monotonic()
    job_id = enqueue(queue, "a", head=None)

    _wait_for(lambda: _running(db, job_id), 20, proc)
    assert time.monotonic() - started_at < 20

    record = json.loads(
        _wait_for(lambda: (autostart["harness_dir"] / f"{job_id}.json").exists()
                  and (autostart["harness_dir"] / f"{job_id}.json").read_text())
    )
    # Exactly the configured command, the brief on stdin and as a file.
    assert record["argv"] == ["--from-config"]
    assert "kind: implement" in record["stdin"]
    assert "owner/repo factory.md at main" in record["stdin"]
    assert "Never apply migrations" in record["stdin"]
    assert Path(record["brief_env"]).read_text(encoding="utf-8") == record["stdin"]
    assert record["job_bot"] == "claude-bot"
    assert record["db"] == str(db.absolute())
    assert "/work/" in record["cwd"]  # --worktree: a job worktree on the box

    # No bot was messaged: the only job is this one, and gh was only asked
    # for factory.md (never to post), with no token.
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    calls = [json.loads(line) for line in autostart["gh_log"].read_text().splitlines()]
    assert calls == [{"argv": ["api", "-X", "GET", "repos/owner/repo/contents/factory.md",
                               "-f", "ref=main"], "token": False}]

    # The waiter is back to waiting while the job runs: a second job starts too.
    second = enqueue(queue, "b", head=None)
    _wait_for(lambda: _running(db, second), 20, proc)
    assert proc.poll() is None

    # The harness finishes its job the way the brief says.
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20, proc)
    _wait_for(lambda: job_id not in slots(db), 20, proc)


def test_the_autostart_waiter_exits_cleanly_on_sigterm_and_leaves_its_job_running(
    autostart: dict,
) -> None:
    proc, db, queue = autostart["proc"], autostart["db"], autostart["queue"]
    job_id = enqueue(queue, "a", head=None)
    _wait_for(lambda: _running(db, job_id), 20, proc)
    proc.terminate()
    assert proc.wait(10) == 0
    # `orch job run` is in its own session: the job keeps its heartbeat.
    time.sleep(0.5)
    assert state(db, job_id) == "running"
    (autostart["harness_dir"] / f"{job_id}.go").touch()
    _wait_for(lambda: state(db, job_id) == "done", 20)


def test_autostart_refuses_to_start_without_a_valid_harness_config(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    result = subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--autostart", "--db", str(db),
         "--harness-config", str(tmp_path / "missing.yml")],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 2
    assert "harness config" in result.stderr
    assert result.stdout == ""


def test_plain_wait_still_prints_and_exits_without_starting_anything(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    job_id = enqueue(queue, "a")
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    result = subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--db", str(db)],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == job_id
    with sqlite3.connect(db) as connection:
        runner = connection.execute(
            "SELECT runner_pid FROM device_slots WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert runner is None


def test_waiter_module_spawns_only_orch_job_run(tmp_path: Path) -> None:
    # Every process the waiter module starts is `orch job run` (and the gh
    # read of factory.md): the waiter never execs a harness directly.
    tree = ast.parse((ROOT / "lib" / "waiter.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id in ("subprocess", "os")
        and (node.attr.startswith(("exec", "spawn", "Popen", "run", "call", "check_"))
             or node.attr in ("system", "popen", "fork"))
    ]
    assert sorted(f"{c.value.id}.{c.attr}" for c in calls) == ["subprocess.Popen", "subprocess.run"]
    assert waiter_module.GH_ARGV_PREFIX == ("gh", "api", "-X", "GET")


def test_spec_states_the_new_harness_rule_and_keeps_the_poller_rule() -> None:
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    row = next(line for line in spec.splitlines()
               if line.startswith("| Only the owning harness bot starts that harness |"))
    assert "The dispatcher and `orch poll` exec no harness" in row
    assert "running as the owning bot's own user and login, may start that bot's harness" in row
    assert "under `orch job run` from the standard brief plus `factory.md`" in row
    assert "no job, PR or comment text reaches a command line" in row
    security = spec[spec.index("## 10. Security"):spec.index("## 11.")]
    assert "The dispatcher and `orch poll` never exec `claude`, `codex`, or `cursor-agent`" in security
    assert "`orch wait <bot> --autostart`" in security
    assert "The dispatcher, `orch poll` and `orch wait` never exec" not in spec
    assert "Let the waiter start the harness itself".lower() in spec.lower()
    plan = (ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
    assert "The dispatcher and `orch poll` exec no harness" in plan


def test_autostart_runs_with_only_remote_devices(tmp_path: Path) -> None:
    # Issue #85: the box is no longer required; a remote-only pool is served.
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    config = write_config(tmp_path / "waiter.yml", "bot: claude-bot\ncommand: [x]\n")
    machines = tmp_path / "machines.yml"
    machines.write_text(
        "devices:\n"
        "  - name: pc\n    tailscale_name: pc.example.ts.net\n    slot_max: 3\n"
        "    harnesses: {claude: claude-bot}\n"
        "    workdir_root: /home/{bot}/factory-work\n    ssh_alias: pc-{bot}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    proc = subprocess.Popen(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--autostart", "--db", str(db),
         "--machines", str(machines), "--harness-config", str(config),
         "--state-dir", str(tmp_path / "state"), "--orch", str(tmp_path / "missing.yml")],
        env=env, text=True, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
    )
    try:
        seen = ""
        deadline = time.monotonic() + 20
        while "waiting for claude-bot jobs" not in seen and time.monotonic() < deadline:
            line = proc.stderr.readline()
            if not line:
                break
            seen += line
        assert "waiting for claude-bot jobs" in seen, seen
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    assert proc.returncode == 0


def test_autostart_only_flags_need_autostart(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    SQLiteJobQueue(db, clock=time.time)
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    result = subprocess.run(
        [sys.executable, str(ORCH), "wait", "claude-bot", "--db", str(db),
         "--harness-config", str(tmp_path / "x.yml")],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 2
    assert "need --autostart" in result.stderr
