#!/usr/bin/env python3
"""A fake harness "posts" its handoff (#191): serves a comment that fits the
job in $ORCH_DB to the fake gh's $FAKE_GH_STORE and prints its id."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.handoff_store import harness_comment_id, serve_job  # noqa: E402


def main(db: str, job_id: str) -> int:
    comment_id = harness_comment_id(job_id)
    serve_job(Path(db), job_id, comment_id)
    print(comment_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
