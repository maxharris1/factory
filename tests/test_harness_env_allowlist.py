from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib import github_app_token as broker
from lib.brief.rules import SECRET_RULE, rules
from lib.harness_env import harness_environ
from lib.queue import SQLiteJobQueue
from lib.waiter import HarnessCommand, Waiter
from tests.test_approver_credential import approvals_for
from tests.test_waiter import MACHINES, FakeContext, FakeSpawn, enqueue, local_pool

ROOT = Path(__file__).resolve().parent.parent
GROK_SKILL = ROOT / "grok" / "orchestrate-project" / "SKILL.md"
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
SENTINEL = "SENTINEL_NOT_A_TOKEN"


def with_sentinels(base: dict[str, str]) -> dict[str, str]:
    return {**base, **{name: f"{SENTINEL}_{name}" for name in TOKEN_VARIABLES}}


@pytest.mark.parametrize("switch", [None, False, True])
def test_harness_environ_drops_every_token_variable(tmp_path: Path, switch: bool | None) -> None:
    home = str(tmp_path / "claude-bot")
    kept = {"PATH": "/usr/bin", "HOME": home, "GH_CONFIG_DIR": f"{home}/gh"}
    approvals = None if switch is None else approvals_for(
        tmp_path, enabled=switch, profile=str(tmp_path / "grok" / "gh-approver")
    )

    assert harness_environ(with_sentinels(kept), approvals) == kept


def test_the_waiter_starts_orch_job_run_without_a_token_variable(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    spawn = FakeSpawn()
    waiter = Waiter(
        bot="claude-bot",
        queue=queue,
        pool=local_pool(queue, MACHINES),
        harness=HarnessCommand(bot="claude-bot", command=("fake-harness",)),
        context=FakeContext(),
        state_dir=tmp_path / "state",
        db=db,
        machines=MACHINES,
        orch=("python3", "/factory/bin/orch"),
        spawn=spawn,
        log=[].append,
        environ=with_sentinels({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}),
    )
    job_id = enqueue(queue, "a")

    assert waiter.step() == job_id
    (_, kwargs), = spawn.calls
    assert not set(TOKEN_VARIABLES) & set(kwargs["env"])
    assert SENTINEL not in "".join(kwargs["env"].values())
    assert kwargs["env"]["HOME"] == str(tmp_path)


def test_the_harness_still_reads_the_broker_token_file(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    now = datetime.now(timezone.utc)
    stored = f"ghs_{SENTINEL}_FROM_FILE"
    broker.write_token(broker.token_path(tokens, "maxharris1"), broker.Stored(stored, now, now + timedelta(hours=1)), None)
    env = harness_environ(with_sentinels({"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}), None)

    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "lib/github_app_token.py"), "credential", "--dir", str(tokens), "get"],
        input="protocol=https\nhost=github.com\npath=maxharris1/factory.git\n\n",
        capture_output=True, text=True, env=env, cwd=tmp_path, check=True,
    )

    assert done.stdout == f"username=x-access-token\npassword={stored}\n"


def test_the_brief_and_the_grok_skill_state_one_secret_rule() -> None:
    assert f"- {SECRET_RULE}\n" in rules()
    lines = GROK_SKILL.read_text(encoding="utf-8").splitlines()
    assert f"- {SECRET_RULE}" in lines
    assert [line for line in lines if "credentials" in line and "Never read" in line] == [f"- {SECRET_RULE}"]
