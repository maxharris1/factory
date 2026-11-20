"""A ready is checked for a Proof entry per acceptance criterion (#1032)."""

from __future__ import annotations

import json

import pytest

from lib.handoff_check import expected_for, problems
from lib.queue import Job, JobRequest

HEAD = "a" * 40
LOGIN = "builder-login"
TRUSTED = {LOGIN: frozenset({"claude-bot"})}
PROOF = [
    {"id": "ac-1", "kind": "review", "journeys": []},
    {"id": "ac-2", "kind": "review", "journeys": []},
    {"id": "ac-3", "kind": "qa", "journeys": ["search"]},
]
HEADER = f"<!-- orch:v1 kind=ready from=claude-bot role=builder head={HEAD} -->"
AC_1 = "- ac-1 (review): `pytest -q tests/test_a.py`\n  ```\n  2 passed\n  ```\n"
REST = (
    "- ac-2 (review): `pytest -q tests/test_b.py`\n  ```\n  1 passed\n  ```\n"
    "- ac-3 (qa): `verify run search`\n  ```\n  search PASS\n  ```\n"
)


def _job(action: str = "build", **extra: object) -> Job:
    detail = {"pr_number": "7", "gitBranchName": "factory/issue-42", **extra}
    request = JobRequest(
        repo="owner/repo", ticket_id="42", action=action, recipient="claude",
        source_comment_id="src", timeout_seconds=3600, detail=json.dumps(detail),
    )
    return Job("job1", "key", request, "running", 0.0, 0.0, None, None)


def _check(job: Job, body: str, created_at: str | None = None) -> list[str]:
    answers = {
        "repos/owner/repo/issues/comments/1001": {
            "issue_url": "https://api.github.com/repos/owner/repo/issues/7",
            "user": {"login": LOGIN},
            "body": body,
            **({"created_at": created_at} if created_at is not None else {}),
        },
        "repos/owner/repo/pulls/7": {"head": {"sha": HEAD, "ref": "factory/issue-42"}, "state": "open"},
    }
    return problems(expected_for(job, "claude-bot"), "1001", answers.__getitem__, TRUSTED)


@pytest.mark.parametrize("action", ["build", "fix"])
def test_a_ready_missing_proof_entries_names_them(action: str) -> None:
    body = f"{HEADER}\nWhat changed.\n\n## Proof\n{AC_1}"
    assert "ready has no Proof entry for ac-2, ac-3" in _check(_job(action, proof=PROOF), body)


def test_a_ready_with_every_proof_entry_passes() -> None:
    body = f"{HEADER}\nWhat changed.\n\n## Proof\n{AC_1}{REST}"
    assert _check(_job(proof=PROOF), body) == []


def test_a_job_without_proof_in_its_detail_asks_for_none() -> None:
    assert _check(_job(), f"{HEADER}\nWhat changed.\n") == []


LIVE = "## Verify, live\n- qa-1 (ac-1): Open the case. Pass: it opens.\n"
LIVE_JOBS = {
    "plan": ("plan", f"<!-- orch:v1 kind=plan from=claude-bot role=builder ac=met round=1/2 -->\nEstimate: 40\n\n{LIVE}"),
    "ready": ("build", f"{HEADER}\nWhat changed.\n\n{LIVE}"),
}


@pytest.mark.parametrize("kind", ["plan", "ready"])
def test_a_live_section_is_accepted_before_the_cutoff(kind: str) -> None:
    action, body = LIVE_JOBS[kind]
    assert _check(_job(action), body, "2026-12-14T23:59:59Z") == []


@pytest.mark.parametrize("kind", ["plan", "ready"])
def test_a_live_section_is_refused_from_the_cutoff_naming_features(kind: str) -> None:
    action, body = LIVE_JOBS[kind]
    found = _check(_job(action), body, "2026-12-15T00:00:00Z")
    assert len(found) == 1
    assert "Features:" in found[0]


@pytest.mark.parametrize("kind", ["plan", "ready"])
def test_an_inline_triple_backtick_span_before_the_live_section_is_still_refused(kind: str) -> None:
    action, body = LIVE_JOBS[kind]
    body = body.replace("## Verify, live", "```npm test```\n\n## Verify, live")
    found = _check(_job(action), body, "2026-12-15T00:00:00Z")
    assert len(found) == 1
    assert "Features:" in found[0]
