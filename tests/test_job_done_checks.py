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
import time
from pathlib import Path

import pytest

from lib.cli import main as cli_main
from lib.queue import MAX_INVALID_HANDOFFS, SUBMITTED_INVALID, JobRequest, SQLiteJobQueue
from tests.handoff_store import HEAD, serve_handoff

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
    head = REVIEW_HEAD if action == "review" else None
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
    claimed = queue.claim(recipient)
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
        ({"round": "2/3"}, "round=2/3 is not 1/3"),
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
