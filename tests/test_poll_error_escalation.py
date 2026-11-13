"""#32 carried note, with #107: a ticket whose poll keeps failing flags attention."""

from pathlib import Path

import pytest

from lib.dispatcher import Dispatcher, TrackedTicket
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import FakeSource, setup, ticket


def failing(dispatcher: Dispatcher, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    broken: set[str] = set()
    process = dispatcher.process

    def flaky(polled: TrackedTicket) -> None:
        if polled.ticket_id in broken:
            raise RuntimeError("tracker read failed")
        process(polled)

    monkeypatch.setattr(dispatcher, "process", flaky)
    return broken


def track(queue: SQLiteJobQueue, *ids: str) -> None:
    with queue.connect() as connection:
        connection.executemany(
            "INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)"
            " VALUES ('owner/repo', ?, 'github', 0)",
            [(ticket_id,) for ticket_id in ids],
        )


def attention(queue: SQLiteJobQueue) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        return [
            (row["ticket_id"], row["reason"], row["detail"])
            for row in connection.execute(
                "SELECT ticket_id, reason, detail, raised_at FROM needs_attention"
                " ORDER BY ticket_id"
            )
        ]


def test_repeated_poll_errors_flag_attention_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    broken = failing(dispatcher, monkeypatch)
    track(queue, "42", "43")
    source = FakeSource(ticket(ticket_id="42"), ticket(ticket_id="43"))
    broken.add("42")
    flagged = []

    for _ in range(5):
        dispatcher.poll_once(source)
        flagged.append(attention(queue))
        now[0] += 30

    assert flagged[:2] == [[], []]
    assert flagged[2] == [
        ("42", "poll-errors", "3 polls in a row failed: RuntimeError"),
    ]
    with queue.connect() as connection:
        raised = connection.execute(
            "SELECT raised_at FROM needs_attention WHERE reason = 'poll-errors'"
        ).fetchone()[0]
    assert raised == 160.0


def test_a_clean_poll_resets_the_error_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, queue = setup(tmp_path)
    broken = failing(dispatcher, monkeypatch)
    track(queue, "42")
    source = FakeSource(ticket(ticket_id="42"))

    broken.add("42")
    dispatcher.poll_once(source)
    dispatcher.poll_once(source)
    broken.clear()
    dispatcher.poll_once(source)
    broken.add("42")
    dispatcher.poll_once(source)
    dispatcher.poll_once(source)
    assert attention(queue) == []

    dispatcher.poll_once(source)
    assert [reason for _, reason, _ in attention(queue)] == ["poll-errors"]

    broken.clear()
    dispatcher.poll_once(source)
    assert attention(queue) == []


def test_the_error_count_survives_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher, queue = setup(tmp_path)
    track(queue, "42")
    source = FakeSource(ticket(ticket_id="42"))
    failing(dispatcher, monkeypatch).add("42")
    dispatcher.poll_once(source)
    dispatcher.poll_once(source)

    restarted, _ = setup(tmp_path)
    failing(restarted, monkeypatch).add("42")
    restarted.poll_once(source)

    assert [reason for _, reason, _ in attention(queue)] == ["poll-errors"]
