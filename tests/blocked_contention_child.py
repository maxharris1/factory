from __future__ import annotations

import json
import sys
from pathlib import Path

from lib.dispatch.types import RetryableStateError
from tests.test_dispatcher import handled_outcome
from tests.test_job_bound_accept import _claim, _reviewing, _verdict_ticket


def main(root: Path, pause_at: str) -> None:
    dispatcher, queue, _ = _reviewing(root)
    review = _claim(queue, "codex")
    queue.finish(review.id, owner="codex", handoff_comment_id="blocked-1")
    snapshot = _verdict_ticket("blocked-1", "BLOCKED")
    armed = True

    def pause(frame, event, arg):
        nonlocal armed
        if armed and event == "call" and frame.f_code.co_name == pause_at:
            armed = False
            print(json.dumps({"db": str(queue.path)}), flush=True)
            sys.stdin.readline()

    sys.settrace(pause)
    try:
        dispatcher.process(snapshot)
        first = "handled"
    except RetryableStateError:
        first = "retryable"
    finally:
        sys.settrace(None)
    dispatcher.process(snapshot)
    dispatcher.process(snapshot)
    with queue.connect() as connection:
        state = connection.execute("SELECT workflow_state FROM ticket_state").fetchone()[0]
        wakes = [
            [row["action"], row["source_comment_id"], row["detail"]]
            for row in connection.execute(
                "SELECT action, source_comment_id, detail FROM jobs WHERE recipient = 'grok'"
            )
        ]
    print(
        json.dumps(
            {
                "first": first,
                "outcome": handled_outcome(queue, "blocked-1"),
                "state": state,
                "wakes": wakes,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
