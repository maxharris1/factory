"""Fake GitHub answers for `orch job done --handoff` (#191): a handoff
comment that fits a job, read by tests/fake_gh.py from $FAKE_GH_STORE."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from lib.handoff import format_header
from lib.handoff_check import expected_for
from lib.queue import Job, job_from_row

HEAD = "a" * 40
LOGIN = "maxharris1"


def harness_comment_id(job_id: str) -> str:
    """The comment id a fake harness posts for job_id (tests/post_handoff.py)."""
    return str(int(job_id[:6], 16) + 1)


def env_store() -> Path:
    """The store $FAKE_GH_STORE names (set by store_path)."""
    return Path(os.environ["FAKE_GH_STORE"])


def serve(store: Path, path: str, answer: dict) -> None:
    answers = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    answers[path] = answer
    store.write_text(json.dumps(answers), encoding="utf-8")


def fitting_fields(job: Job, bot: str, head: str = HEAD) -> dict[str, str]:
    expected = expected_for(job, bot)
    fields = {"kind": expected.kind, "from": expected.bot, "role": expected.role}
    if expected.round is not None:
        fields["round"] = expected.round
    if expected.kind == "plan":
        fields["ac"] = "met"
    if expected.kind == "ready":
        fields["head"] = head
    if expected.kind == "verdict":
        fields["head"] = expected.head or head
        fields["verdict"] = "APPROVE"
        fields["ac"] = "met"
    if expected.kind == "qa":
        fields.update(
            head=expected.head or head, result="PASS", scope="full", runner="claude-cloud", arm="primary"
        )
    return fields


def fitting_body(kind: str | None) -> str:
    return "Estimate: 120\nprose\n" if kind == "plan" else "prose\n"


def serve_handoff(
    store: Path,
    job: Job,
    comment_id: str,
    *,
    bot: str | None = None,
    login: str = LOGIN,
    number: int | None = None,
    repo: str | None = None,
    pr_head: str = HEAD,
    branch: str | None = None,
    is_pr: bool = True,
    body: str | None = None,
    **changes: str | None,
) -> None:
    """Serve comment_id as a handoff that fits job, then apply changes:
    a header key set to None is dropped, any other value replaces it."""
    bot = bot or f"{job.request.recipient}-bot"
    expected = expected_for(job, bot)
    repo = repo or job.request.repo
    number = number or expected.pr or 1
    fields = fitting_fields(job, bot, pr_head)
    for key, value in changes.items():
        if value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    text = body if body is not None else format_header(fields) + "\n" + fitting_body(fields.get("kind"))
    serve(
        store,
        f"repos/{job.request.repo}/issues/comments/{comment_id}",
        {
            "id": int(comment_id),
            "user": {"login": login},
            "body": text,
            "issue_url": f"https://api.github.com/repos/{repo}/issues/{number}",
        },
    )
    if not is_pr:
        return
    serve(
        store,
        f"repos/{repo}/pulls/{number}",
        {"number": number, "head": {"sha": pr_head, "ref": branch or expected.branch or "topic"}},
    )


def serve_job(db: Path, job_id: str, *comment_ids: str) -> None:
    """Serve each of comment_ids as a handoff that fits the job in db."""
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        job = job_from_row(
            connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone(), connection
        )
    for comment_id in comment_ids:
        serve_handoff(env_store(), job, comment_id)
