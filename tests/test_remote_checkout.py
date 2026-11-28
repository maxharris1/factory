"""#94: a remote placement gets its job checkout on the placed device.

`orch job run --worktree` (and so the #82 waiter, which always passes it)
makes the job's checkout on whatever device the pool placed it: a git
worktree on the box, as before, or a clone at the job's head in
`<workdir_root>/jobs/<job-id>` on a remote device, made through the
device's ssh alias. The harness then runs there under `bash -lc`, so the
bot's login PATH (gh, claude, codex) is loaded, with the brief on stdin and
ORCH_JOB_BRIEF / ORCH_JOB_WORKTREE naming the remote paths.

Before anything starts, one ssh call checks under `bash -lc` that git, gh
and the harness binary exist. A missing one, an unreachable device or a
failed clone fails fast with a one-line reason, releases the slot and
requeues the job: no round, no attempt, never `running` with no slot.

Every test uses tests/fake_ssh.py first on PATH: no real ssh, no network
(the "remote" origin is a file:// repository in tmp_path).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import remote
from lib.device_pool import DevicePool
from lib.queue import JobRequest, SQLiteJobQueue
from tests.test_remote_start import Ssh, config_of, machines_file

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "bin" / "orch"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
BRIEF = "# Factory job brief: review\n\nReview PR #7 at its head.\n"

# The harness the bot's config names. It is found only through the bot's
# login PATH (~/.bash_profile), never the bare ssh PATH, and it records what
# it saw: where it ran, the checkout's HEAD and branch, its stdin, and the
# job environment.
HARNESS = """#!{python}
import json, os, subprocess, sys
if sys.argv[1:] == ["--version"]:
    print("fake-harness 1.0.0")
    sys.exit(0)
def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()
keys = ("ORCH_JOB_ID", "ORCH_JOB_BOT", "ORCH_JOB_DEVICE", "ORCH_JOB_WORKDIR",
        "ORCH_JOB_WORKTREE", "ORCH_JOB_BRIEF", "ORCH_DB", "PATH")
brief = os.environ.get("ORCH_JOB_BRIEF")
json.dump({{
    "cwd": os.getcwd(),
    "head": git("rev-parse", "HEAD"),
    "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
    "status": git("status", "--porcelain", "--untracked-files=all"),
    "stdin": sys.stdin.read(),
    "brief_file": open(brief).read() if brief else None,
    "argv": sys.argv[1:],
    **{{key: os.environ.get(key) for key in keys}},
}}, open({report!r}, "w"))
sys.exit(int(os.environ.get("HARNESS_EXIT", "0")))
"""


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV},
        text=True, capture_output=True, check=True,
    ).stdout.strip()


class Device:
    """The fake remote device: its origin, its bot's HOME and login PATH."""

    def __init__(self, tmp_path: Path, *, login_tools: tuple[str, ...] = ("git", "gh", "harness")):
        self.tmp = tmp_path
        self.ssh = Ssh(tmp_path)
        self.report = tmp_path / "harness.json"
        # origin: owner/repo with main and a PR branch "feature" one commit ahead
        remotes = tmp_path / "origin"
        bare = remotes / "owner" / "repo.git"
        bare.parent.mkdir(parents=True)
        git("init", "--bare", "-q", "-b", "main", str(bare))
        seed = tmp_path / "seed"
        git("clone", "-q", str(bare), str(seed))
        (seed / "README").write_text("one\n", encoding="utf-8")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "one", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.main = git("rev-parse", "HEAD", cwd=seed)
        git("checkout", "-q", "-b", "feature", cwd=seed)
        (seed / "change").write_text("two\n", encoding="utf-8")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "two", cwd=seed)
        git("push", "-q", "origin", "feature", cwd=seed)
        self.feature = git("rev-parse", "HEAD", cwd=seed)
        self.template = f"file://{remotes}/{{repo}}.git"
        # the bot's login PATH holds exactly login_tools (plus the basics)
        self.bin = tmp_path / "remote-bin"
        self.bin.mkdir()
        for name in ("sh", "cat", "mkdir", "printf", "env", "id", "uname", "grep", "touch", "mv"):
            found = _which(name)
            if found:
                (self.bin / name).symlink_to(found)
        if "git" in login_tools:
            (self.bin / "git").symlink_to(_which("git"))
        if "gh" in login_tools:
            stub = self.bin / "gh"
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
        if "harness" in login_tools:
            harness = self.bin / "fakeharness"
            harness.write_text(
                HARNESS.format(python=sys.executable, report=str(self.report)), encoding="utf-8"
            )
            harness.chmod(0o755)
        self.home = tmp_path / "remote-home"
        self.home.mkdir()
        # /etc/profile may reset PATH; the bot's profile then sets its own.
        (self.home / ".bash_profile").write_text(f'PATH="{self.bin}"\nexport PATH\n', encoding="utf-8")

    def env(self) -> dict[str, str]:
        return {
            **self.ssh.env(),
            **GIT_ENV,
            # a bare (non-login) ssh PATH: sh and bash, but neither the
            # harness nor this bot's gh
            "FAKE_SSH_PATH": "/usr/bin:/bin",
            "FAKE_SSH_HOME": str(self.home),
        }

    def job_dir(self, bot: str, job_id: str) -> Path:
        return self.tmp / "remote" / bot / "work" / "jobs" / job_id

    def seen(self) -> dict:
        return json.loads(self.report.read_text(encoding="utf-8"))


def _which(name: str) -> str | None:
    import shutil

    for directory in ("/usr/bin", "/bin", "/usr/local/bin"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def remote_job(
    tmp_path: Path,
    recipient: str = "codex",
    *,
    head: str | None,
    detail: dict | None = None,
    box: bool = False,
) -> tuple[Path, SQLiteJobQueue, DevicePool, str]:
    machines = machines_file(tmp_path, box_enabled=box, pc=not box)
    queue = SQLiteJobQueue(tmp_path / "orch.db", clock=time.time)
    job_id = queue.enqueue(
        JobRequest(
            repo="owner/repo",
            ticket_id="7",
            action="review",
            recipient=recipient,
            source_comment_id="go-7",
            timeout_seconds=3600,
            head=head,
            detail=json.dumps(detail or {"round": "1/3", "risk": "high"}),
        ),
        dedupe_key="7",
    ).id
    pool = DevicePool(queue, config_of(machines))
    claimed = pool.claim(recipient)
    assert claimed is not None and claimed[1].device == ("box" if box else "pc")
    return machines, queue, pool, job_id


def run_job(
    tmp_path: Path,
    device: Device,
    machines: Path,
    job_id: str,
    *command: str,
    bot: str = "codex-bot",
    brief: bool = True,
    template: str | None = None,
    extra_env: dict[str, str] | None = None,
    extra_args: tuple[str, ...] = (),
) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("ORCH_DB", "ORCH_JOB_BRIEF")}
    env.update(device.env())
    env.update(extra_env or {})
    box_brief = tmp_path / "state" / "brief.md"
    box_brief.parent.mkdir(exist_ok=True)
    box_brief.write_text(BRIEF, encoding="utf-8")
    if brief:
        env["ORCH_JOB_BRIEF"] = str(box_brief)
    with open(box_brief, "rb") as stdin:
        proc = subprocess.run(
            [
                sys.executable, str(ORCH), "job", "run", bot, job_id,
                "--db", str(tmp_path / "orch.db"), "--machines", str(machines),
                "--worktree", "--git-url-template", template or device.template,
                *extra_args, "--", *(command or ("fakeharness",)),
            ],
            stdin=stdin,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    return proc.returncode, proc.stderr


def row(queue: SQLiteJobQueue, job_id: str) -> dict:
    with queue.connect() as connection:
        return dict(connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def scripts(device: Device) -> list[str]:
    return [call["script"] or "" for call in device.ssh.calls()]


def starts(device: Device) -> list[str]:
    return [item for item in scripts(device) if item.startswith(f"exec env {remote.TAG_ENV}=")]


# --- a remote placement gets a checkout and runs there ----------------------


def test_remote_worktree_checks_out_the_job_head_and_runs_the_harness_there(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(
        tmp_path, head=device.feature, detail={"gitBranchName": "feature", "base_branch": "main"}
    )
    code, err = run_job(tmp_path, device, machines, job_id)

    assert code == 0, err
    workdir = device.job_dir("codex-bot", job_id)
    seen = device.seen()
    assert seen["cwd"] == str(workdir)
    assert seen["head"] == device.feature  # the job's head ...
    assert seen["branch"] == "feature"  # ... on the job's branch
    assert seen["status"] == ""  # orch's own files never show as changes
    assert seen["ORCH_JOB_WORKTREE"] == seen["ORCH_JOB_WORKDIR"] == str(workdir)
    assert (seen["ORCH_JOB_ID"], seen["ORCH_JOB_BOT"], seen["ORCH_JOB_DEVICE"]) == (
        job_id, "codex-bot", "pc",
    )
    assert seen["ORCH_DB"] is None  # the box database never reaches the device
    # the brief reaches the remote harness on stdin, as on the box, and
    # ORCH_JOB_BRIEF names a copy on the device
    assert seen["stdin"] == BRIEF
    assert seen["ORCH_JOB_BRIEF"] == remote.brief_path(
        remote.RemoteTarget("pc", "a", "codex-bot", str(tmp_path / "remote/codex-bot/work"), job_id)
    )
    assert seen["brief_file"] == BRIEF
    assert not seen["ORCH_JOB_BRIEF"].startswith(str(workdir) + "/")  # beside, not in, the checkout
    # found only through the bot's login PATH: the harness ran under bash -lc
    assert seen["PATH"] == str(device.bin)
    calls = device.ssh.calls()
    assert all(
        (call["user"], call["alias"]) == ("codex-bot", "fakepc-codex-bot") for call in calls
    )
    kinds = scripts(device)
    assert "bash -lc" in kinds[0] and "command -v" in kinds[0]  # preflight first
    assert "bash -lc" in kinds[1] and "git" in kinds[1]  # then the checkout
    assert len(starts(device)) == 1 and "bash -lc" in starts(device)[0]
    assert pool.placement(job_id) is None  # released once the worker was gone
    assert row(queue, job_id)["state"] == "running"  # job run never finishes a job


def test_remote_worktree_with_no_head_checks_out_the_base_branch(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=None, detail={"base_branch": "main"})
    code, err = run_job(tmp_path, device, machines, job_id, brief=False)

    assert code == 0, err
    seen = device.seen()
    assert seen["head"] == device.main
    assert seen["ORCH_JOB_BRIEF"] is None  # no brief on the box: none named remotely
    assert seen["stdin"] == BRIEF  # stdin is still forwarded as is


def test_remote_head_not_on_the_named_branch_is_checked_out_detached(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(
        tmp_path, head=device.main, detail={"gitBranchName": "feature"}
    )
    code, err = run_job(tmp_path, device, machines, job_id)

    assert code == 0, err
    seen = device.seen()
    assert seen["head"] == device.main
    assert seen["branch"] == "HEAD"  # the branch has moved on: never reset it to head


def test_remote_rerun_reuses_the_existing_checkout(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    workdir = device.job_dir("codex-bot", job_id)
    code, err = run_job(tmp_path, device, machines, job_id)
    assert code == 0, err
    (workdir / "work-in-progress").write_text("kept\n", encoding="utf-8")
    assert pool.claim("codex") is None  # still running; take its slot again by hand
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at) "
            "VALUES (?, 'pc', 'codex', 'codex-bot', ?)",
            (job_id, time.time()),
        )
        connection.commit()

    code, err = run_job(tmp_path, device, machines, job_id)

    assert code == 0, err
    assert "reused" in err
    assert (workdir / "work-in-progress").read_text(encoding="utf-8") == "kept\n"


def test_harness_exit_status_is_passed_through(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    code, err = run_job(tmp_path, device, machines, job_id, extra_env={})
    assert code == 0, err
    # the harness's own failure is the harness's: no requeue, slot released
    device.report.unlink()
    with queue.connect() as connection:
        connection.execute(
            "INSERT INTO device_slots (job_id, device, harness, os_user, placed_at) "
            "VALUES (?, 'pc', 'codex', 'codex-bot', ?)",
            (job_id, time.time()),
        )
        connection.commit()
    code, err = run_job(
        tmp_path, device, machines, job_id, "env", "HARNESS_EXIT=3", "fakeharness"
    )
    assert code == 3, err
    assert row(queue, job_id)["state"] == "running"
    assert pool.placement(job_id) is None


# --- preflight and clone failures requeue, never charging a round ------------


def assert_requeued(
    queue: SQLiteJobQueue, pool: DevicePool, job_id: str, before: dict, code: int, err: str
) -> None:
    assert code == 75, err  # EX_TEMPFAIL: requeued, try again
    after = row(queue, job_id)
    assert after["state"] == "queued"  # never left running with no slot
    assert pool.placement(job_id) is None  # the slot is free
    assert after["attempt"] == before["attempt"] == 1  # no attempt charged
    assert after["detail"] == before["detail"]  # no round charged
    assert after["generation"] == before["generation"]
    assert after["finished_at"] is None and after["handoff_comment_id"] is None
    assert after["failure_handled"] == 0
    assert after["last_activity_at"] >= before["last_activity_at"]  # queue clock restarted
    reason = [line for line in err.splitlines() if "requeued" in line]
    assert len(reason) == 1, err  # one clear line


@pytest.mark.parametrize(
    ("tools", "missing"),
    [
        (("git", "harness"), "gh"),
        (("git", "gh"), "fakeharness"),
        (("gh", "harness"), "git"),
        ((), "git gh fakeharness"),
    ],
    ids=["gh", "harness", "git", "all"],
)
def test_a_missing_tool_fails_fast_and_requeues_without_a_round(
    tmp_path: Path, tools: tuple[str, ...], missing: str
) -> None:
    device = Device(tmp_path, login_tools=tools)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    before = row(queue, job_id)

    code, err = run_job(tmp_path, device, machines, job_id)

    assert_requeued(queue, pool, job_id, before, code, err)
    assert f"missing under bash -lc on pc: {missing}" in err
    assert len(device.ssh.calls()) == 1  # the preflight only: no clone, no start
    assert starts(device) == []
    assert not device.report.exists()
    assert not device.job_dir("codex-bot", job_id).exists()


def test_a_failed_clone_fails_fast_and_requeues_without_a_round(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    before = row(queue, job_id)

    code, err = run_job(
        tmp_path, device, machines, job_id, template=f"file://{tmp_path}/missing/{{repo}}.git"
    )

    assert_requeued(queue, pool, job_id, before, code, err)
    assert "checkout failed on pc: git fetch failed" in err
    assert starts(device) == []
    assert not device.report.exists()


def test_a_head_the_origin_does_not_have_fails_fast_and_requeues(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head="f" * 40)
    before = row(queue, job_id)

    code, err = run_job(tmp_path, device, machines, job_id)

    assert_requeued(queue, pool, job_id, before, code, err)
    assert f"cannot fetch {'f' * 40}" in err
    assert starts(device) == []


def test_an_unreachable_device_fails_fast_and_requeues(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    device.ssh.offline("fakepc-codex-bot").touch()
    before = row(queue, job_id)

    code, err = run_job(tmp_path, device, machines, job_id)

    assert_requeued(queue, pool, job_id, before, code, err)
    assert "pc is unreachable over ssh" in err
    assert starts(device) == []


def test_requeued_job_can_be_claimed_again(tmp_path: Path) -> None:
    device = Device(tmp_path, login_tools=("git", "harness"))
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    code, err = run_job(tmp_path, device, machines, job_id)
    assert code == 75, err
    claimed = pool.claim("codex")
    assert claimed is not None and claimed[0].id == job_id


@pytest.mark.parametrize(
    ("tools", "template", "reason"),
    [
        (("git", "harness"), None, "missing under bash -lc on pc: gh"),
        (("git", "gh", "harness"), "file://{tmp}/missing/{{repo}}.git", "git fetch failed"),
        (("git", "gh", "harness"), "https://user:secret@example.invalid/{{repo}}.git", "credentials"),
    ],
    ids=["preflight", "clone", "url"],
)
def test_a_failed_start_after_taking_over_a_dead_runners_slot_still_requeues(
    tmp_path: Path, tools: tuple[str, ...], template: str | None, reason: str
) -> None:
    device = Device(tmp_path, login_tools=tools)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    # a runner that died after recording its remote start, before spawning ssh
    pool.attach_runner(job_id, owner="codex", pid=99999999, start=None, alive=lambda *_: False)
    pool.attach_worker(job_id, owner="codex", remote=True)
    before = row(queue, job_id)

    code, err = run_job(
        tmp_path, device, machines, job_id,
        template=template.format(tmp=tmp_path) if template else None,
    )

    assert_requeued(queue, pool, job_id, before, code, err)
    assert reason in err
    assert starts(device) == []
    assert not device.report.exists()


def test_a_credential_carrying_url_template_is_refused_and_requeued(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    before = row(queue, job_id)
    code, err = run_job(
        tmp_path, device, machines, job_id, template="https://user:secret@example.invalid/{repo}.git"
    )
    assert_requeued(queue, pool, job_id, before, code, err)
    assert "secret" not in err
    assert device.ssh.calls() == []  # refused on the box: the URL never reached ssh


# --- the remote command is orch's, never the harness's ------------------------


def test_scripts_quote_every_job_value_and_name_no_key() -> None:
    target = remote.RemoteTarget("pc", "pc-wsl-codex-bot", "codex-bot", "/home/codex-bot/w", "job1")
    preflight = remote.preflight_script("codex; touch /tmp/pwned")
    assert preflight.startswith("exec bash -lc ")
    assert "'codex; touch /tmp/pwned'" in preflight
    script = remote.checkout_script(
        target,
        url="https://github.com/owner/repo.git",
        head="a" * 40,
        base_branch="main",
        branch="feature/x",
        brief=True,
    )
    assert script.startswith("exec bash -lc ")
    assert "/home/codex-bot/w/jobs/job1" in script
    for bad in ("main; rm -rf /", "-x", "a..b", "x.lock", "feature/"):
        with pytest.raises(remote.RemoteError):
            remote.checkout_script(
                target, url="https://github.com/o/r.git", head=None, base_branch=bad,
                branch=None, brief=False,
            )
    with pytest.raises(remote.RemoteError):
        remote.checkout_script(
            target, url="https://github.com/o/r.git", head="not-a-sha", base_branch=None,
            branch=None, brief=False,
        )
    for item in (preflight, script):
        assert ".ssh" not in item and "IdentityFile" not in item


def test_without_worktree_a_remote_job_still_runs_under_a_login_shell(tmp_path: Path) -> None:
    """No --worktree: no preflight and no checkout, but the login PATH loads."""
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature)
    env = {k: v for k, v in os.environ.items() if k != "ORCH_DB"}
    env.update(device.env())
    proc = subprocess.run(
        [
            sys.executable, str(ORCH), "job", "run", "codex-bot", job_id,
            "--db", str(tmp_path / "orch.db"), "--machines", str(machines),
            "--", "fakeharness",
        ],
        stdin=subprocess.DEVNULL, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert device.seen()["PATH"] == str(device.bin)
    assert device.seen()["ORCH_JOB_WORKTREE"] is None
    # the launch, its polls (#128) and the gone check: no preflight or checkout
    assert len(starts(device)) == 1
    assert not [
        item for item in scripts(device)
        if item.startswith("exec bash -lc") and not item.endswith(" orch-containers")
    ]


# --- the box is unchanged ----------------------------------------------------


def test_box_worktree_is_unchanged_and_makes_no_ssh_call(tmp_path: Path) -> None:
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature, box=True)
    report = tmp_path / "box.json"
    script = (
        "import json, os, subprocess, sys; "
        "json.dump({'cwd': os.getcwd(), 'wt': os.environ.get('ORCH_JOB_WORKTREE'), "
        "'brief': os.environ.get('ORCH_JOB_BRIEF'), 'stdin': sys.stdin.read(), "
        "'head': subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()}, "
        f"open({str(report)!r}, 'w'))"
    )
    work = tmp_path / "box-work"
    code, err = run_job(
        tmp_path, device, machines, job_id, sys.executable, "-c", script,
        extra_args=("--work-root", str(work)),
    )

    assert code == 0, err
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert seen["cwd"] == seen["wt"] == str(work / "jobs" / job_id)
    assert seen["head"] == device.feature
    assert seen["brief"] == str(tmp_path / "state" / "brief.md")  # the box path, as before
    assert seen["stdin"] == BRIEF
    assert device.ssh.calls() == []
    assert pool.placement(job_id) is None
    assert f"removed job worktree {work / 'jobs' / job_id}" in err


def test_box_worktree_failure_is_still_exit_1_and_requeued_as_unstarted(tmp_path: Path) -> None:
    """#91: a box setup failure starts no harness, so it is an unstarted requeue."""
    device = Device(tmp_path)
    machines, queue, pool, job_id = remote_job(tmp_path, head=device.feature, box=True)
    code, err = run_job(
        tmp_path, device, machines, job_id, "true",
        template=f"file://{tmp_path}/missing/{{repo}}.git",
        extra_args=("--work-root", str(tmp_path / "box-work")),
    )
    assert code == 1, err
    assert "git fetch failed" in err
    assert row(queue, job_id)["state"] == "queued"
    assert pool.placement(job_id) is None
    assert device.ssh.calls() == []


def _checkout(tmp_path: Path, origin: Path, job_dir: Path) -> str:
    target = remote.RemoteTarget("pc", "a", "codex-bot", str(job_dir.parent.parent), job_dir.name)
    script = remote.checkout_script(
        target, url=f"file://{origin}", head=None, base_branch="main", branch=None, brief=False
    )
    env = {**os.environ, **GIT_ENV, "HOME": str(tmp_path)}
    done = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("made_by", ["new", "older orch"])
def test_run_records_survive_git_clean_in_new_and_reused_checkouts(tmp_path: Path, made_by: str) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "README").write_text("one\n", encoding="utf-8")
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "one", cwd=origin)
    job_dir = tmp_path / "w" / "jobs" / "job1"
    assert "new" in _checkout(tmp_path, origin, job_dir)
    if made_by == "older orch":
        (job_dir / ".git/info/exclude").write_text(
            "/.orch-worker.pid\n/.orch-worker-*.pid\n", encoding="utf-8"
        )
        assert "reused" in _checkout(tmp_path, origin, job_dir)
        assert "reused" in _checkout(tmp_path, origin, job_dir)
    tag = "0" * 32
    records = [
        remote.PID_FILE, remote.start_record(tag), remote.tree_record(tag), f"{remote.tree_record(tag)}.lock",
        remote.scope_record(tag), f".orch-worker-{tag}.keeper.pid", f".orch-worker-{tag}.members.pid",
    ]
    for name in records:
        (job_dir / name).write_text("x\n", encoding="utf-8")

    assert git("status", "--porcelain", "--untracked-files=all", cwd=job_dir) == ""
    git("clean", "-fd", cwd=job_dir)
    assert [name for name in records if not (job_dir / name).exists()] == []
    excludes = (job_dir / ".git/info/exclude").read_text(encoding="utf-8").splitlines()
    assert len(excludes) == len(set(excludes))
