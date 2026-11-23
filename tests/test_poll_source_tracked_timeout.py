import subprocess
import time
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest

from lib.dispatch.errors_text import poll_error_detail
from lib.dispatcher import Dispatcher, TicketRef
from lib.github_actions import GhResult, run_gh
from lib.orch_yml import OrchConfig, Project
from lib.poll_source import LivePollSource, PollCycleError, PollSourceError
from lib.queue import SQLiteJobQueue
from tests.test_live_poll import FACTORY, FakeGh

HUNG_GH_SLEEP_SECONDS = 30


def dispatcher_tracking(tmp_path: Path, refs: list[tuple[str, str]]) -> Dispatcher:
    database = tmp_path / "orch.db"
    queue = SQLiteJobQueue(database, clock=lambda: 10)
    dispatcher = Dispatcher(
        database,
        queue,
        trusted_logins={"github": {"grok-login": {"grok-bot"}}, "linear": {}},
        review_actions=Mock(),
        grok_delivery=Mock(),
    )
    with queue.connect() as connection:
        connection.executemany(
            """
            INSERT INTO tracked_tickets (repo, ticket_id, tracker, tracked_at)
            VALUES (?, ?, 'github', 0)
            """,
            refs,
        )
    dispatcher.process = Mock()
    dispatcher._advance_merge_queues = Mock()
    dispatcher.reconcile_jobs = Mock()
    return dispatcher


def processed(dispatcher: Dispatcher) -> list[tuple[str, str]]:
    return sorted(
        (call.args[0].repo, call.args[0].ticket_id)
        for call in dispatcher.process.call_args_list
    )


def projects(*repos: str) -> OrchConfig:
    return OrchConfig(
        poll_seconds=60,
        projects=tuple(Project(repo, factory_ref="main") for repo in repos),
    )


def test_a_hung_gh_read_is_abandoned_and_the_cycle_goes_on(tmp_path: Path) -> None:
    hung_gh = tmp_path / "gh"
    hung_gh.write_text(f"#!/bin/sh\nexec sleep {HUNG_GH_SLEEP_SECONDS}\n")
    hung_gh.chmod(0o755)
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))

    def runner(argv, timeout):
        if "repos/acme/widgets/pulls/12" in argv:
            call = list(argv)
            call[call.index("gh")] = str(hung_gh)
            return run_gh(call, timeout)
        return gh(argv, timeout)

    dispatcher = dispatcher_tracking(
        tmp_path, [("acme/widgets", "12"), ("acme/widgets", "13")]
    )
    source = LivePollSource(
        projects("acme/widgets"),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        sleep=lambda seconds: None,
        gh_call_timeout_seconds=0.5,
        read_deadline_seconds=1.0,
    )

    started = time.monotonic()
    with pytest.raises(PollCycleError) as raised:
        dispatcher.poll_once(source)
    elapsed = time.monotonic() - started

    assert elapsed < HUNG_GH_SLEEP_SECONDS / 3
    assert "acme/widgets ticket 12 PollSourceError:gh api GET pulls/12 timed out" in str(
        raised.value
    )
    assert processed(dispatcher) == [("acme/widgets", "13")]
    dispatcher._advance_merge_queues.assert_called_once_with()
    dispatcher.reconcile_jobs.assert_called_once_with()


NO_RERUN_PATHS = FACTORY.format(tickets="github", ship="merge").replace(
    "skills: []\n", "skills: []\nci:\n  force_rerun_paths: []\n"
)


def test_a_source_over_its_tracked_read_budget_is_deferred_and_the_cycle_goes_on(
    tmp_path: Path,
) -> None:
    now = [0.0]
    gh = FakeGh(NO_RERUN_PATHS)

    def runner(argv, timeout):
        if any(part.startswith("repos/acme/widgets/pulls/") for part in argv):
            now[0] += 50.0
        return gh(argv, timeout)

    widgets = [("acme/widgets", str(number)) for number in range(12, 17)]
    dispatcher = dispatcher_tracking(tmp_path, [*widgets, ("acme/gadgets", "30")])
    deferrals: list[str] = []
    source = LivePollSource(
        projects("acme/widgets", "acme/gadgets"),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        deferral_sink=deferrals.append,
        clock=lambda: now[0],
        sleep=lambda seconds: None,
        tracked_read_budget_seconds=120.0,
    )

    dispatcher.poll_once(source)

    read = processed(dispatcher)
    assert ("acme/gadgets", "30") in read
    assert len([ref for ref in read if ref[0] == "acme/widgets"]) == 2
    assert deferrals == ["tracked-read budget deferred acme/widgets x3"]
    dispatcher._advance_merge_queues.assert_called_once_with()
    dispatcher.reconcile_jobs.assert_called_once_with()


REPOS = (
    "acme/widgets",
    "acme/auth-service",
    "acme/session-keys",
    "acme/token-service",
    "acme/api-key-store",
)


@pytest.mark.parametrize("repo", REPOS)
def test_the_cycle_error_names_the_repo_ticket_and_failing_call(repo: str) -> None:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))

    def runner(argv, timeout):
        if f"repos/{repo}/pulls/12" in argv:
            return GhResult(1, "", "boom")
        return gh(argv, timeout)

    source = LivePollSource(projects(repo), gh_runner=runner)
    source.begin_cycle()

    tracked = source.tracked_tickets(
        (
            TicketRef(repo, "12", "github"),
            TicketRef(repo, "13", "github"),
        )
    )

    assert [ticket.ticket_id for ticket in tracked] == ["13"]
    with pytest.raises(PollCycleError) as raised:
        source.end_cycle()
    assert str(raised.value) == (
        f"poll cycle recorded 1 source error(s): {repo} x1; "
        f"{repo} ticket 12 PollSourceError:gh api GET pulls/12 exited 1"
    )


def test_the_cycle_error_lists_at_most_five_details() -> None:
    def runner(argv, timeout):
        return GhResult(1, "", "boom")

    source = LivePollSource(projects("acme/widgets"), gh_runner=runner)
    source.begin_cycle()

    source.tracked_tickets(
        tuple(TicketRef("acme/widgets", str(number), "github") for number in range(12, 19))
    )

    with pytest.raises(PollCycleError) as raised:
        source.end_cycle()
    message = str(raised.value)
    assert message.startswith("poll cycle recorded 7 source error(s): acme/widgets x7; ")
    assert message.count("PollSourceError:") == 5
    assert message.endswith("; and 2 more")


def held_factory() -> str:
    return FACTORY.format(tickets="github", ship="merge").replace("skills: []\n", "skills: []\nhold: {}\n")


def test_a_ticket_whose_reads_outlast_the_source_budget_is_cut_off_and_deferred() -> None:
    now = [0.0]
    starts: list[float] = []
    gh = FakeGh(held_factory())

    def runner(argv, timeout):
        if any(part.startswith("repos/acme/widgets/") for part in argv):
            starts.append(now[0])
            if 19.0 > timeout:
                now[0] += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            now[0] += 19.0
        return gh(argv, timeout)

    errors: list[tuple[str, str, str]] = []
    deferrals: list[str] = []
    source = LivePollSource(
        projects("acme/widgets", "acme/gadgets"),
        gh_runner=runner,
        error_sink=lambda *error: errors.append(error),
        deferral_sink=deferrals.append,
        clock=lambda: now[0],
        sleep=lambda seconds: None,
        tracked_read_budget_seconds=120.0,
    )
    source.begin_cycle()

    tracked = source.tracked_tickets(
        (
            TicketRef("acme/widgets", "12", "github"),
            TicketRef("acme/widgets", "13", "github"),
            TicketRef("acme/gadgets", "30", "github"),
        )
    )
    source.end_cycle()

    assert now[0] <= 120.0
    assert starts and max(starts) < 120.0
    assert [(ticket.repo, ticket.ticket_id) for ticket in tracked] == [("acme/gadgets", "30")]
    assert errors == []
    assert deferrals == ["tracked-read budget deferred acme/widgets x2"]


def test_tickets_deferred_by_the_budget_are_read_in_a_later_cycle(tmp_path: Path) -> None:
    now = [0.0]
    gh = FakeGh(NO_RERUN_PATHS)

    def runner(argv, timeout):
        if any(part.startswith("repos/acme/widgets/") for part in argv):
            now[0] += 10.0
        return gh(argv, timeout)

    widgets = [("acme/widgets", str(number)) for number in range(12, 17)]
    dispatcher = dispatcher_tracking(tmp_path, [*widgets, ("acme/gadgets", "30")])
    source = LivePollSource(
        projects("acme/widgets", "acme/gadgets"),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        clock=lambda: now[0],
        sleep=lambda seconds: None,
        tracked_read_budget_seconds=120.0,
    )

    seen: set[tuple[str, str]] = set()
    for _ in range(2):
        dispatcher.process.reset_mock()
        dispatcher.poll_once(source)
        seen.update(processed(dispatcher))

    assert set(widgets) <= seen


def logged_cycle_error(tmp_path: Path, repo: str, runner_for) -> tuple[str, str]:
    gh = FakeGh(FACTORY.format(tickets="github", ship="merge"))
    dispatcher = dispatcher_tracking(tmp_path, [(repo, "12"), (repo, "13")])
    source = LivePollSource(
        projects(repo), gh_runner=runner_for(gh), error_sink=dispatcher.record_poll_error
    )
    with pytest.raises(PollCycleError) as raised:
        dispatcher.poll_once(source)

    log = StringIO()
    with redirect_stderr(log):
        dispatcher.record_poll_exception("poll", "-", raised.value)
    with dispatcher.queue.connect() as connection:
        (detail,) = connection.execute(
            "SELECT detail FROM poll_errors WHERE repo = 'poll'"
        ).fetchone()
    return log.getvalue(), detail


@pytest.mark.parametrize("repo", REPOS)
def test_the_poller_log_and_stored_detail_name_the_repo_ticket_and_failing_call(
    tmp_path: Path, repo: str
) -> None:
    def runner_for(gh):
        def runner(argv, timeout):
            if f"repos/{repo}/pulls/12" in argv:
                return GhResult(1, "", "boom")
            return gh(argv, timeout)

        return runner

    log, detail = logged_cycle_error(tmp_path, repo, runner_for)

    named = f"{repo} ticket 12 PollSourceError:gh api GET pulls/12 exited 1"
    assert named in log
    assert named in detail
    assert "<credential omitted>" not in log
    assert "<credential omitted>" not in detail


def test_the_poller_log_and_stored_detail_keep_secrets_in_source_errors_redacted(
    tmp_path: Path,
) -> None:
    repo = "acme/auth-service"

    def runner_for(gh):
        def runner(argv, timeout):
            if f"repos/{repo}/pulls/12" in argv:
                raise RuntimeError(
                    "gh read failed Authorization: Bearer abc123def456 "
                    "password=hunter2 ghp_abcdefghij1234567890"
                )
            if f"repos/{repo}/pulls/13" in argv:
                raise PollSourceError("gh read failed token=s3cr3tvalue passphrase tail2of2")
            return gh(argv, timeout)

        return runner

    log, detail = logged_cycle_error(tmp_path, repo, runner_for)

    for text in (log, detail):
        assert f"{repo} ticket 12" in text
        for secret in (
            "abc123def456",
            "hunter2",
            "ghp_abcdefghij1234567890",
            "s3cr3tvalue",
            "tail2of2",
        ):
            assert secret not in text


def test_a_cycle_error_redacts_url_and_token_shapes_in_reasons_and_names() -> None:
    error = PollCycleError.of(
        [
            (
                "acme/token-service",
                "12",
                "PollSourceError:gh api GET https://api.github.com/repos/x exited 1 "
                "ghp_abcdefghij1234567890",
            ),
            ("acme/ghp_abcdefghij1234567890", "github.com", "PollSourceError:boom"),
        ]
    )

    detail = poll_error_detail(error)

    assert (
        "acme/token-service ticket 12 PollSourceError:gh api GET <url> exited 1 <secret>"
    ) in detail
    assert "acme/<secret> ticket <host> PollSourceError:boom" in detail
    for leaked in ("ghp_abcdefghij1234567890", "api.github.com", "github.com"):
        assert leaked not in detail


def test_a_cycle_error_not_built_from_source_errors_is_still_cut() -> None:
    detail = poll_error_detail(PollCycleError("acme/auth-service ticket 12 failed"))

    assert detail.endswith("PollCycleError: acme/<credential omitted>")


def test_the_ticket_that_spends_the_budget_yields_its_turn_to_the_next(tmp_path: Path) -> None:
    now = [0.0]
    tried: list[str] = []
    gh = FakeGh(held_factory())

    def runner(argv, timeout):
        pulls = [part for part in argv if part.startswith("repos/acme/widgets/pulls/")]
        if pulls:
            tried.append(pulls[0].split("/")[4])
        if any(part.startswith("repos/acme/widgets/") for part in argv):
            if 19.0 > timeout:
                now[0] += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            now[0] += 19.0
        return gh(argv, timeout)

    widgets = [("acme/widgets", str(number)) for number in range(12, 17)]
    dispatcher = dispatcher_tracking(tmp_path, [*widgets, ("acme/gadgets", "30")])
    source = LivePollSource(
        projects("acme/widgets", "acme/gadgets"),
        gh_runner=runner,
        error_sink=dispatcher.record_poll_error,
        clock=lambda: now[0],
        sleep=lambda seconds: None,
        tracked_read_budget_seconds=120.0,
    )

    for _ in range(6):
        dispatcher.poll_once(source)

    assert {number for _, number in widgets} <= set(tried)
