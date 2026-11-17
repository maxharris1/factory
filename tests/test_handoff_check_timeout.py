from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

import pytest

import lib.handoff_check
import tests.test_pre_ready_remote as remote
from lib.queue import PRE_READY_PASSED, SUBMITTED_INVALID
from tests.test_pre_ready_remote import COMMENT, _events, _remote_ready, _row, origin

__all__ = ["origin"]

FAKE_GH = Path(__file__).with_name("fake_gh.py")


@pytest.fixture
def flaky_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def install(how: Literal["stall", "502"], count: int) -> None:
        directory = tmp_path / "flaky-gh"
        directory.mkdir()
        left = directory / "left"
        left.write_text(str(count), encoding="utf-8")
        failure = "exec sleep 5" if how == "stall" else 'echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1'
        script = directory / "gh"
        script.write_text(
            "#!/bin/sh\n"
            f'n=$(cat "{left}")\n'
            'if [ "$n" -gt 0 ]; then\n'
            f'  echo $((n - 1)) > "{left}"\n'
            f"  {failure}\n"
            "fi\n"
            f'exec "{sys.executable}" "{FAKE_GH}" "$@"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")

    monkeypatch.setattr(lib.handoff_check, "GH_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(lib.handoff_check, "RETRY_DELAYS", (0.0, 0.0))
    return install


def _retries(log: list[str]) -> list[str]:
    return [line for line in log if "retrying" in line]


def test_a_check_that_times_out_once_finishes_the_job_done(
    tmp_path: Path, origin: str, flaky_gh
) -> None:
    flaky_gh("stall", 1)
    db, job_id, log = _remote_ready(tmp_path, origin, "sh ok.sh")
    assert _row(db, job_id) == ("done", COMMENT)
    assert _events(db, job_id) == [(PRE_READY_PASSED, f"handoff {COMMENT}: at {origin}")]
    retries = _retries(log)
    assert len(retries) == 1
    assert job_id in retries[0] and "TimeoutExpired" in retries[0]
    assert "attempt 1 of 3" in retries[0]


def test_a_502_is_retried_too(tmp_path: Path, origin: str, flaky_gh) -> None:
    flaky_gh("502", 2)
    db, job_id, log = _remote_ready(tmp_path, origin, "sh ok.sh")
    assert _row(db, job_id) == ("done", COMMENT)
    retries = _retries(log)
    assert [("HTTP 502" in line, f"attempt {n} of 3" in line) for n, line in enumerate(retries, 1)] == [
        (True, True),
        (True, True),
    ]


def test_a_check_that_never_reads_github_fails_the_job_after_three_attempts(
    tmp_path: Path, origin: str, flaky_gh
) -> None:
    flaky_gh("stall", 3)
    db, job_id, log = _remote_ready(tmp_path, origin, "sh ok.sh")
    assert _row(db, job_id) == ("failed", None)
    assert len(_retries(log)) == 2
    assert any(
        f"could not check handoff {COMMENT}" in line and "attempt 3 of 3" in line for line in log
    )
    assert _events(db, job_id) == []


def test_an_invalid_handoff_still_fails_the_job_with_no_retry(
    tmp_path: Path, origin: str, flaky_gh, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky_gh("stall", 0)
    real = remote.serve_handoff
    monkeypatch.setattr(
        remote, "serve_handoff", lambda *args, **kwargs: real(*args, **kwargs, role="reviewer")
    )
    db, job_id, log = _remote_ready(tmp_path, origin, "sh ok.sh")
    assert _row(db, job_id) == ("failed", None)
    assert [event for event, _detail in _events(db, job_id)] == [SUBMITTED_INVALID]
    assert _retries(log) == []
    assert any(f"submitted-invalid {COMMENT}" in line and "role=reviewer" in line for line in log)
