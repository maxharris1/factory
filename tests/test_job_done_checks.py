"""`orch job done --handoff <id>` checks the handoff against its job (#191, T17a).

The comment is read through `gh` (tests/fake_gh.py here) and must be in the
job's repo, on its PR, by a trusted_logins login for the bot, with the
header's kind, from, role, head and round the brief asked for. One that
does not fit is submitted-invalid: the job keeps running, the ticket is
flagged needs-attention once, and the third fails the job for T6.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from lib.cli import main as cli_main
from lib.handoff_check import expected_for, gh_reader, problems
from lib.queue import (
    MAX_INVALID_HANDOFFS,
    PRE_READY_FAILED,
    PRE_READY_PASSED,
    SUBMITTED_INVALID,
    JobRequest,
    SQLiteJobQueue,
)
from lib.job_token import PROOF_ENV
from lib.handoff import format_header
from tests.handoff_store import HEAD, LOGIN, fitting_fields, serve_handoff
from tests.pool_helpers import CLAIM_HASH, CLAIM_PROOF

REPO = "owner/repo"
OTHER_HEAD = "b" * 40
REVIEW_HEAD = "c" * 40


def detail(**extra: object) -> str:
    value = {"pr_number": "7", "risk": "high", "round": "1/3", "gitBranchName": "factory/issue-42"}
    value.update(extra)
    return json.dumps(value)


def running_job(tmp_path: Path, *, action: str = "build", recipient: str = "claude", **extra):
    db = tmp_path / "orch.db"
    queue = SQLiteJobQueue(db, clock=time.time)
    head = REVIEW_HEAD if action in ("review", "qa") else None
    job = queue.enqueue(
        JobRequest(
            repo=REPO,
            ticket_id="42",
            action=action,
            recipient=recipient,
            source_comment_id=f"src-{action}",
            timeout_seconds=3600,
            generation=1,
            attempt=1,
            head=head,
            detail=detail(head=head, **extra),
        ),
        dedupe_key=f"{action}-{recipient}",
    )
    claimed = queue.claim(recipient, token_hash=CLAIM_HASH)
    assert claimed is not None and claimed.id == job.id
    return db, queue, claimed


def done(db: Path, job, handoff: str, bot: str = "claude-bot") -> int:
    return cli_main(["job", "done", bot, job.id, "--handoff", handoff, "--db", str(db)])


def row(db: Path, job_id: str) -> sqlite3.Row:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def flags(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as connection:
        if not has_table(connection, "needs_attention"):
            return []
        return connection.execute(
            "SELECT repo, ticket_id, reason, detail FROM needs_attention"
        ).fetchall()


def invalid_events(db: Path, job_id: str) -> list[str]:
    with sqlite3.connect(db) as connection:
        if not has_table(connection, "job_events"):
            return []
        return [
            detail
            for (detail,) in connection.execute(
                "SELECT detail FROM job_events WHERE job_id = ? AND event = ? ORDER BY id",
                (job_id, SUBMITTED_INVALID),
            )
        ]


@pytest.fixture(autouse=True)
def claim_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROOF_ENV, CLAIM_PROOF)


@pytest.fixture
def store(_fake_gh_store: Path) -> Path:
    return _fake_gh_store


def test_a_correct_ready_completes(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1001")

    assert done(db, job, "1001") == 0

    assert (row(db, job.id)["state"], row(db, job.id)["handoff_comment_id"]) == ("done", "1001")
    assert capsys.readouterr().out == f"done {job.id}\n"
    assert flags(db) == []


@pytest.mark.parametrize(
    ("action", "recipient", "bot"),
    [("review", "codex", "codex-bot"), ("plan", "claude", "claude-bot"), ("fix", "claude", "claude-bot")],
)
def test_each_job_kind_completes_with_the_header_its_brief_asked_for(
    tmp_path: Path, store: Path, action: str, recipient: str, bot: str
) -> None:
    db, _, job = running_job(tmp_path, action=action, recipient=recipient)
    serve_handoff(store, job, "1002")

    assert done(db, job, "1002", bot) == 0
    assert row(db, job.id)["state"] == "done"


@pytest.mark.parametrize("skills_line", ["", "Skills read: /home/claude-bot/factory/skills/factory/factory-builder/SKILL.md\n"])
@pytest.mark.parametrize(
    ("action", "recipient", "bot"),
    [("plan", "claude", "claude-bot"), ("build", "claude", "claude-bot"), ("review", "codex", "codex-bot")],
)
def test_a_handoff_completes_with_or_without_a_skills_read_line(
    tmp_path: Path, store: Path, action: str, recipient: str, bot: str, skills_line: str
) -> None:
    db, _, job = running_job(tmp_path, action=action, recipient=recipient)
    body = format_header(fitting_fields(job, bot)) + "\nprose\n" + skills_line
    serve_handoff(store, job, "1003", body=body)

    assert done(db, job, "1003", bot) == 0
    assert row(db, job.id)["state"] == "done"
    assert flags(db) == []


def test_a_questions_handoff_completes_any_job(tmp_path: Path, store: Path) -> None:
    db, _, job = running_job(tmp_path, action="review", recipient="codex")
    serve_handoff(
        store, job, "1003", kind="questions", round=None, head=None, verdict=None, ac=None
    )

    assert done(db, job, "1003", "codex-bot") == 0
    assert row(db, job.id)["state"] == "done"


def test_a_nonexistent_comment_id_fails(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)

    assert done(db, job, "999999") == 1

    err = capsys.readouterr().err
    assert "submitted-invalid 999999" in err
    assert "does not exist in owner/repo" in err
    assert row(db, job.id)["state"] == "running"
    assert row(db, job.id)["handoff_comment_id"] is None


def test_a_non_numeric_id_fails_without_a_read(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)

    assert done(db, job, "c-1") == 1

    assert "'c-1' is not a GitHub comment id" in capsys.readouterr().err
    assert row(db, job.id)["state"] == "running"


def test_a_handoff_on_the_wrong_pr_fails(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1004", number=8)

    assert done(db, job, "1004") == 1

    assert "comment 1004 is on #8, not PR #7" in capsys.readouterr().err
    assert row(db, job.id)["state"] == "running"


def test_a_handoff_in_another_repo_fails(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1005", repo="someone/else")

    assert done(db, job, "1005") == 1

    assert "is in someone/else, not owner/repo" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "go", "head": None, "round": None, "risk": None}, "kind=go is not ready"),
        ({"from": "cursor-bot"}, "from=cursor-bot is not claude-bot"),
        ({"role": "reviewer"}, "role=reviewer is not builder"),
    ],
)
def test_a_header_that_does_not_fit_fails(
    tmp_path: Path, store: Path, capsys, changes: dict, message: str
) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1006", **changes)

    assert done(db, job, "1006") == 1

    assert message in capsys.readouterr().err
    assert row(db, job.id)["state"] == "running"


def test_a_ready_at_the_wrong_head_fails(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1007", head=OTHER_HEAD)

    assert done(db, job, "1007") == 1

    assert f"head={OTHER_HEAD} is not the head of #7 ({HEAD})" in capsys.readouterr().err


def serve_closed_pr(store: Path, *, tip: str | None, merged: bool = False) -> None:
    from tests.handoff_store import serve

    serve(
        store,
        "repos/owner/repo/pulls/7",
        {
            "number": 7,
            "state": "closed",
            "merged": merged,
            "head": {"sha": HEAD, "ref": "factory/issue-42"},
        },
    )
    if tip is not None:
        serve(store, "repos/owner/repo/git/ref/heads/factory/issue-42", {"object": {"sha": tip}})


def test_705_a_ready_at_the_rebuilt_branch_of_an_auto_closed_pr_completes(
    tmp_path: Path, store: Path
) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1011", head=OTHER_HEAD)
    serve_closed_pr(store, tip=OTHER_HEAD)

    assert done(db, job, "1011") == 0

    assert row(db, job.id)["state"] == "done"


@pytest.mark.parametrize(
    ("tip", "merged"),
    [(OTHER_HEAD, True), (None, False)],
    ids=["merged-pr-keeps-its-head", "deleted-branch"],
)
def test_705_a_closed_pr_whose_branch_cannot_speak_for_it_keeps_its_frozen_head(
    tmp_path: Path, store: Path, capsys, tip: str | None, merged: bool
) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1012", head=OTHER_HEAD)
    serve_closed_pr(store, tip=tip, merged=merged)

    assert done(db, job, "1012") == 1

    assert f"head={OTHER_HEAD} is not the head of #7 ({HEAD})" in capsys.readouterr().err


def test_a_ready_with_a_short_head_of_the_pr_completes(tmp_path: Path, store: Path) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1008", head=HEAD[:12])

    assert done(db, job, "1008") == 0


def test_a_verdict_on_another_head_fails(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path, action="review", recipient="codex")
    serve_handoff(store, job, "1009", head=OTHER_HEAD)

    assert done(db, job, "1009", "codex-bot") == 1

    assert f"head={OTHER_HEAD} is not the reviewed head {REVIEW_HEAD}" in capsys.readouterr().err


def test_a_verdict_with_no_head_fails_when_the_job_has_one(
    tmp_path: Path, store: Path, capsys
) -> None:
    db, _, job = running_job(tmp_path, action="review", recipient="codex")
    serve_handoff(store, job, "1019", head=None, verdict="REVISE")

    assert done(db, job, "1019", "codex-bot") == 1

    assert f"no head; the reviewed head is {REVIEW_HEAD}" in capsys.readouterr().err


def test_an_untrusted_author_fails(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1010", login="stranger")

    assert done(db, job, "1010") == 1

    assert "author stranger is not a trusted_logins login for claude-bot" in capsys.readouterr().err


def test_a_comment_with_no_header_fails(tmp_path: Path, store: Path, capsys) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1011", body="LGTM\n")

    assert done(db, job, "1011") == 1

    assert "has no valid orch:v1 header" in capsys.readouterr().err


def test_with_no_pr_number_the_comment_must_be_on_a_pr_on_the_jobs_branch(
    tmp_path: Path, store: Path, capsys
) -> None:
    db, _, job = running_job(tmp_path, pr_number=None)
    serve_handoff(store, job, "1012", number=9, branch="someone-elses-branch")

    assert done(db, job, "1012") == 1
    assert "#9 is on branch someone-elses-branch, not factory/issue-42" in capsys.readouterr().err

    serve_handoff(store, job, "1013", number=9, branch="factory/issue-42")
    assert done(db, job, "1013") == 0


QUESTIONS = {"kind": "questions", "round": None, "head": None, "verdict": None, "ac": None}
EVERY_HANDOFF = [
    pytest.param("build", "claude", "claude-bot", {}, id="ready"),
    pytest.param("fix", "claude", "claude-bot", {}, id="fix-ready"),
    pytest.param("plan", "claude", "claude-bot", {}, id="plan"),
    pytest.param("review", "codex", "codex-bot", {}, id="verdict"),
    pytest.param("build", "claude", "claude-bot", QUESTIONS, id="builder-questions"),
    pytest.param("review", "codex", "codex-bot", QUESTIONS, id="reviewer-questions"),
]


@pytest.mark.parametrize(("action", "recipient", "bot", "changes"), EVERY_HANDOFF)
def test_with_no_pr_number_every_handoff_must_be_on_a_pr_on_the_jobs_branch(
    tmp_path: Path, store: Path, capsys, action: str, recipient: str, bot: str, changes: dict
) -> None:
    db, _, job = running_job(tmp_path, action=action, recipient=recipient, pr_number=None)

    serve_handoff(store, job, "2001", bot=bot, number=9, branch="someone-elses-branch", **changes)
    assert done(db, job, "2001", bot) == 1
    assert "#9 is on branch someone-elses-branch, not factory/issue-42" in capsys.readouterr().err

    serve_handoff(store, job, "2002", bot=bot, number=11, is_pr=False, **changes)
    assert done(db, job, "2002", bot) == 1
    assert "#11 is not a PR" in capsys.readouterr().err

    assert row(db, job.id)["state"] == "running"
    assert len(invalid_events(db, job.id)) == 2
    assert len(flags(db)) == 1

    serve_handoff(store, job, "2003", bot=bot, number=9, branch="factory/issue-42", **changes)
    assert done(db, job, "2003", bot) == 0
    assert row(db, job.id)["state"] == "done"


def test_a_github_read_failure_changes_nothing(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    db, _, job = running_job(tmp_path)
    monkeypatch.delenv("FAKE_GH_STORE")

    assert done(db, job, "1014") == 1

    assert "real gh is disabled" in capsys.readouterr().err
    assert row(db, job.id)["state"] == "running"
    assert invalid_events(db, job.id) == []
    assert flags(db) == []


def test_submitted_invalid_surfaces_once_and_the_third_fails_the_job(
    tmp_path: Path, store: Path, capsys
) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1015", number=8)
    serve_handoff(store, job, "1016", head=OTHER_HEAD)

    assert done(db, job, "1015") == 1
    assert done(db, job, "1016") == 1

    first = flags(db)
    assert len(first) == 1
    assert first[0][:3] == ("owner/repo", "42", SUBMITTED_INVALID)
    assert "handoff 1015" in first[0][3]
    assert len(invalid_events(db, job.id)) == 2
    assert row(db, job.id)["state"] == "running"
    err = capsys.readouterr().err
    assert f"invalid handoff 2 of {MAX_INVALID_HANDOFFS}; the job is still running" in err

    assert done(db, job, "999999") == 1

    assert flags(db) == first
    assert len(invalid_events(db, job.id)) == MAX_INVALID_HANDOFFS
    failed = row(db, job.id)
    assert (failed["state"], failed["handoff_comment_id"], failed["failure_handled"]) == (
        "failed",
        None,
        0,
    )
    assert f"{MAX_INVALID_HANDOFFS} invalid handoffs; job {job.id} is failed for T6" in (
        capsys.readouterr().err
    )


def test_a_correct_handoff_after_an_invalid_one_completes_and_clears_the_flag(
    tmp_path: Path, store: Path
) -> None:
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1017", head=OTHER_HEAD)
    assert done(db, job, "1017") == 1
    assert len(flags(db)) == 1

    serve_handoff(store, job, "1018")
    assert done(db, job, "1018") == 0

    assert row(db, job.id)["state"] == "done"
    assert flags(db) == []


def pre_ready_setup(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch, command: str, timeout: int = 30
):
    import base64

    from tests.handoff_store import serve

    work = tmp_path / "work"
    work.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=work, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q")
    (work / "factory.md").write_text(
        "---\nreview:\n  pre_ready:\n    command: touch head-command-ran\n"
        "    timeout_seconds: 5\n---\n",
        encoding="utf-8",
    )
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\ntrusted_logins:\n  maxharris1:\n    - claude-bot\n"
        "projects:\n  - repo: owner/repo\n    factory_ref: main\n"
        "grok:\n  webhook_url_env: FACTORY_WEBHOOK_URL\n  webhook_key_env: FACTORY_WEBHOOK_KEY\n",
        encoding="utf-8",
    )
    factory = (
        "---\nbase_branch: main\ntickets: github\nbuilder: {harness: claude}\n"
        "reviewer: {harness: codex}\nreview:\n  rounds: {low: 1, high: 3}\n  verify: none\n"
        f"  pre_ready: {{command: {json.dumps(command)}, timeout_seconds: {timeout}}}\n"
        "ship: merge\nskills: []\n---\n"
    )
    serve(
        store,
        "repos/owner/repo/contents/factory.md?ref=main",
        {"encoding": "base64", "content": base64.b64encode(factory.encode()).decode()},
    )
    monkeypatch.setenv("ORCH_JOB_WORKTREE", str(work))
    db, _, job = running_job(tmp_path)
    serve_handoff(store, job, "1101", pr_head=head)
    return db, job, orch, work


def done_with(db: Path, job, orch: Path, handoff: str = "1101") -> int:
    return cli_main(
        ["job", "done", "claude-bot", job.id, "--handoff", handoff, "--db", str(db), "--orch", str(orch)]
    )


def pre_ready_events(db: Path, job_id: str, event: str = PRE_READY_FAILED) -> list[str]:
    with sqlite3.connect(db) as connection:
        if not has_table(connection, "job_events"):
            return []
        return [
            detail
            for (detail,) in connection.execute(
                "SELECT detail FROM job_events WHERE job_id = ? AND event = ?",
                (job_id, event),
            )
        ]


def test_a_ready_whose_pre_ready_fails_is_refused_with_its_output(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    db, job, orch, work = pre_ready_setup(
        tmp_path, store, monkeypatch, "echo first; echo the last line; exit 1"
    )

    assert done_with(db, job, orch) == 1

    err = capsys.readouterr().err
    assert "submitted-invalid 1101: pre_ready failed: exit 1" in err
    assert "the last line" in err
    assert (row(db, job.id)["state"], row(db, job.id)["handoff_comment_id"]) == ("running", None)
    assert pre_ready_events(db, job.id) == ["handoff 1101: pre_ready failed: exit 1"]
    assert not (work / "head-command-ran").exists()


def test_a_ready_whose_pre_ready_passes_completes(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, job, orch, work = pre_ready_setup(tmp_path, store, monkeypatch, "touch base-command-ran")

    assert done_with(db, job, orch) == 0

    assert (row(db, job.id)["state"], row(db, job.id)["handoff_comment_id"]) == ("done", "1101")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert pre_ready_events(db, job.id, PRE_READY_PASSED) == [f"handoff 1101: at {head}"]
    assert (work / "base-command-ran").exists()
    assert not (work / "head-command-ran").exists()


def test_a_pre_ready_timeout_is_refused(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    db, job, orch, _ = pre_ready_setup(tmp_path, store, monkeypatch, "sleep 5", timeout=1)

    assert done_with(db, job, orch) == 1

    assert "pre_ready failed: timeout after 1s" in capsys.readouterr().err
    assert row(db, job.id)["state"] == "running"


def test_a_worktree_off_the_ready_head_runs_nothing(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    db, job, orch, work = pre_ready_setup(tmp_path, store, monkeypatch, "touch base-command-ran")
    (work / "factory.md").write_text("dirty\n", encoding="utf-8")

    assert done_with(db, job, orch) == 1

    assert "pre_ready failed: not at head" in capsys.readouterr().err
    assert not (work / "base-command-ran").exists()


def test_an_unreadable_base_factory_md_exits_without_recording(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, job, orch, _ = pre_ready_setup(tmp_path, store, monkeypatch, "true")
    answers = json.loads(store.read_text(encoding="utf-8"))
    del answers["repos/owner/repo/contents/factory.md?ref=main"]
    store.write_text(json.dumps(answers), encoding="utf-8")

    assert done_with(db, job, orch) == 1

    assert row(db, job.id)["state"] == "running"
    assert pre_ready_events(db, job.id) == []


def test_a_questions_handoff_skips_pre_ready(
    tmp_path: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, job, orch, work = pre_ready_setup(tmp_path, store, monkeypatch, "exit 1")
    serve_handoff(store, job, "1102", kind="questions", head=None)

    assert done_with(db, job, orch, "1102") == 0
    assert row(db, job.id)["state"] == "done"


QA_TRUSTED = {LOGIN: frozenset({"claude-bot", "codex-bot", "grok-bot"})}


def qa_header(**changes: str | None) -> str:
    fields = {
        "kind": "qa",
        "from": "grok-bot",
        "role": "qa",
        "head": REVIEW_HEAD,
        "result": "PASS",
        "scope": "full",
        "runner": "claude-cloud",
        "arm": "primary",
    }
    fields.update(changes)
    return format_header({key: value for key, value in fields.items() if value is not None}) + "\nprose\n"


def handoff_problems(job, comment_id: str, bot: str = "grok-bot") -> list[str]:
    return problems(expected_for(job, bot), comment_id, gh_reader(), QA_TRUSTED)


def test_763_a_qa_job_expects_a_qa_handoff_from_grok_bot_at_its_head(tmp_path: Path) -> None:
    _, _, job = running_job(tmp_path, action="qa", recipient="qa")

    expected = expected_for(job, "claude-bot")

    assert (expected.kind, expected.role, expected.bot, expected.head, expected.round) == (
        "qa",
        "qa",
        "grok-bot",
        REVIEW_HEAD,
        None,
    )


@pytest.mark.parametrize("role", ["qa", None])
def test_763_a_qa_handoff_at_the_jobs_head_fits_with_or_without_a_role(
    tmp_path: Path, store: Path, role: str | None
) -> None:
    _, _, job = running_job(tmp_path, action="qa", recipient="qa")
    serve_handoff(store, job, "1201", role=role)

    assert handoff_problems(job, "1201") == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (qa_header(head=OTHER_HEAD), f"head={OTHER_HEAD} is not the job's head {REVIEW_HEAD}"),
        (
            qa_header(
                kind="verdict",
                role="reviewer",
                verdict="APPROVE",
                ac="met",
                round="1/3",
                result=None,
                scope=None,
                runner=None,
                arm=None,
            ),
            "kind=verdict is not qa",
        ),
        (
            qa_header(kind="questions", head=None, result=None, scope=None, runner=None, arm=None),
            "kind=questions is not qa",
        ),
        (qa_header(**{"from": "codex-bot"}), "from=codex-bot is not grok-bot"),
    ],
    ids=["other-head", "verdict", "questions", "other-bot"],
)
def test_763_a_qa_job_refuses_another_head_a_verdict_questions_or_another_bot(
    tmp_path: Path, store: Path, body: str, message: str
) -> None:
    _, _, job = running_job(tmp_path, action="qa", recipient="qa")
    serve_handoff(store, job, "1202", body=body)

    assert message in handoff_problems(job, "1202")


@pytest.mark.parametrize(
    ("action", "recipient", "bot", "message"),
    [
        ("build", "claude", "claude-bot", "kind=qa is not ready (or questions)"),
        ("review", "codex", "codex-bot", "kind=qa is not verdict (or questions)"),
    ],
)
def test_763_a_qa_handoff_does_not_fit_any_other_job(
    tmp_path: Path, store: Path, action: str, recipient: str, bot: str, message: str
) -> None:
    _, _, job = running_job(tmp_path, action=action, recipient=recipient)
    serve_handoff(store, job, "1203", body=qa_header(**{"from": bot}))

    assert message in handoff_problems(job, "1203", bot)
