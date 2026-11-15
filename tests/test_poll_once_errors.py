from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import PollCycleFailed, PollLoop
from lib.poll_source import PollCycleError
from tests.test_dispatcher import (
    GO,
    FakeSource,
    comment,
    seed_builder_job,
    setup,
    ticket,
)

class BrokenSource(FakeSource):
    def __init__(self, *tickets, broken: str = "", **kwargs):
        super().__init__(*tickets, **kwargs)
        self.broken = broken
        self.tracked_calls = 0

    def github_candidates(self, label_prefix, *, exclude=frozenset()):
        if self.broken == "github":
            raise RuntimeError("github is down")
        return super().github_candidates(label_prefix, exclude=exclude)

    def linear_candidates(self, query, *, exclude, reserve):
        if self.broken == "linear":
            raise RuntimeError("linear is down")
        return super().linear_candidates(query, exclude=exclude, reserve=reserve)

    def tracked_tickets(self, refs):
        self.tracked_calls += 1
        if self.broken == "tracked" or (self.broken == "verify" and self.tracked_calls > 1):
            raise RuntimeError("tracked read is down")
        return super().tracked_tickets(refs)


def poll_error_rows(queue) -> list[tuple[str, str, str]]:
    with queue.connect() as connection:
        return [
            (row["repo"], row["ticket_id"], row["error"])
            for row in connection.execute(
                "SELECT repo, ticket_id, error FROM poll_errors ORDER BY id"
            )
        ]


def run_once(monkeypatch, dispatcher, source, database: Path) -> int:
    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda db, orch, create, **_: (dispatcher, source),
    )
    return cli.main(["poll", "--db", str(database), "--once"])


@pytest.mark.parametrize("broken", ["github", "linear", "tracked"])
def test_poll_once_exits_one_naming_the_failed_source(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], broken: str
) -> None:
    dispatcher, queue = setup(tmp_path)

    result = run_once(monkeypatch, dispatcher, BrokenSource(broken=broken), tmp_path / "d.db")

    assert result == 1
    assert f"orch poll: poll cycle failed: {broken}: RuntimeError" in capsys.readouterr().err
    assert (broken, "-", "RuntimeError") in poll_error_rows(queue)


def test_a_clean_poll_once_exits_zero(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dispatcher, queue = setup(tmp_path)

    assert run_once(monkeypatch, dispatcher, BrokenSource(), tmp_path / "d.db") == 0
    assert "poll cycle failed" not in capsys.readouterr().err
    assert poll_error_rows(queue) == []


def test_a_failed_source_still_lets_the_rest_of_the_cycle_run(tmp_path: Path) -> None:
    dispatcher, queue = setup(tmp_path)
    go = ticket(comment("go-1", GO.format(state="building"), author="grok-login"))
    source = BrokenSource(go, github=(go,), broken="linear")

    with pytest.raises(PollCycleFailed, match="linear: RuntimeError"):
        dispatcher.poll_once(source)

    with queue.connect() as connection:
        jobs = [
            (row["ticket_id"], row["action"])
            for row in connection.execute("SELECT ticket_id, action FROM jobs")
        ]
    assert jobs == [("42", "build")]


def test_a_failed_verify_read_fails_the_cycle(tmp_path: Path) -> None:
    now = [100.0]
    dispatcher, queue = setup(tmp_path, now)
    seed_builder_job(queue)
    with queue.connect() as connection:
        connection.execute(
            """
            INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)
            VALUES ('owner/repo', '42', 'github', 0)
            """
        )
    now[0] = 200.0

    with pytest.raises(PollCycleFailed, match="verify: RuntimeError"):
        dispatcher.poll_once(BrokenSource(broken="verify"))

    assert ("verify", "-", "RuntimeError") in poll_error_rows(queue)


def test_a_failed_source_and_a_failed_end_cycle_keep_both_messages(tmp_path: Path) -> None:
    dispatcher, _ = setup(tmp_path)

    class EndFails(BrokenSource):
        def end_cycle(self):
            raise PollCycleError("poll cycle recorded 1 source error(s): acme/widgets x1")

    with pytest.raises(PollCycleFailed) as raised:
        dispatcher.poll_once(EndFails(broken="github"))

    assert "github: RuntimeError" in str(raised.value)
    assert "acme/widgets x1" in str(raised.value)


def test_the_poll_loop_keeps_polling_after_a_failed_source(tmp_path: Path) -> None:
    dispatcher, _ = setup(tmp_path)
    errors: list[Exception] = []

    PollLoop(dispatcher, BrokenSource(broken="github"), sleep=lambda _: None).run(
        max_polls=2, on_error=errors.append
    )

    assert [type(error) for error in errors] == [PollCycleFailed, PollCycleFailed]


def test_poll_errors_older_than_seven_days_are_pruned_each_cycle(tmp_path: Path) -> None:
    day = 86400.0
    now = [30 * day]
    dispatcher, queue = setup(tmp_path, now)
    with queue.connect() as connection:
        connection.executemany(
            """
            INSERT INTO poll_errors (repo, ticket_id, error, recorded_at)
            VALUES ('owner/repo', '42', ?, ?)
            """,
            [
                ("old", now[0] - 8 * day),
                ("edge", now[0] - 7 * day + 1),
                ("fresh", now[0]),
            ],
        )

    def errors_after_a_cycle() -> list[str]:
        dispatcher.poll_once(BrokenSource())
        return [error for _, _, error in poll_error_rows(queue)]

    assert errors_after_a_cycle() == ["edge", "fresh"]
    assert errors_after_a_cycle() == ["edge", "fresh"]
    now[0] += 2
    assert errors_after_a_cycle() == ["fresh"]
