"""#1310: the dispatcher's moved-head probe seam, read once per poll."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib import cli
from lib.dispatcher import Dispatcher
from lib.queue import SQLiteJobQueue
from lib.secret_scan import ScanResult
from lib.update_check import MovedHead
from tests.test_dispatcher import (
    TRUSTED_LOGINS,
    FakeGrokDelivery,
    FakeReviewActions,
    FakeSource,
    ticket,
)

OK = "a" * 40
HEAD = "b" * 40
READ = MovedHead((OK, "c" * 40), True, True, frozenset({"x.py"}), ScanResult("clean", ()))


class CountingProbe:
    def __init__(self, *outcomes: MovedHead | Exception):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, repo: str, base: str, ok: str, head: str) -> MovedHead:
        self.calls.append((repo, base, ok, head))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def dispatcher(tmp_path: Path, probe: CountingProbe | None, log: list[str]) -> Dispatcher:
    path = tmp_path / "d.db"
    return Dispatcher(
        path,
        SQLiteJobQueue(path, clock=lambda: 100.0),
        trusted_logins=TRUSTED_LOGINS,
        review_actions=FakeReviewActions(),
        grok_delivery=FakeGrokDelivery(),
        log=log.append,
        moved_head=probe,
    )


def test_one_poll_reads_a_moved_head_once_and_the_next_poll_reads_it_again(
    tmp_path: Path,
) -> None:
    probe = CountingProbe(READ)
    built = dispatcher(tmp_path, probe, [])
    tracked = ticket(head=HEAD)

    assert built._moved_head(tracked, OK, HEAD) == READ
    assert built._moved_head(ticket(head=HEAD, repo="Owner/Repo"), OK, HEAD) == READ
    assert probe.calls == [("owner/repo", "main", OK, HEAD)]

    built.poll_once(FakeSource())

    assert built._moved_head(tracked, OK, HEAD) == READ
    assert len(probe.calls) == 2


@pytest.mark.parametrize("error", [ValueError("no commit"), OSError("git gone")])
def test_a_probe_that_raises_is_unknown_and_logged(tmp_path: Path, error: Exception) -> None:
    log: list[str] = []
    built = dispatcher(tmp_path, CountingProbe(error), log)

    assert built._moved_head(ticket(head=HEAD), OK, HEAD) is None
    assert [line for line in log if "moved head" in line] == [
        f"moved head unread: owner/repo#42: {type(error).__name__}"
    ]


def test_no_probe_is_unknown(tmp_path: Path) -> None:
    assert dispatcher(tmp_path, None, [])._moved_head(ticket(head=HEAD), OK, HEAD) is None


def test_the_live_probe_reads_the_fetched_clone_against_origin_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetched: list[tuple[str, tuple[str, ...]]] = []
    probed: list[tuple[Path, str, str, str]] = []

    def fetched_clone(root, repo, heads, template):
        fetched.append((repo, tuple(heads)))
        return tmp_path / "clone.git"

    def probe(clone, base_ref, ok, head):
        probed.append((clone, base_ref, ok, head))
        return READ

    monkeypatch.setattr(cli.worktrees, "fetched_clone", fetched_clone)
    monkeypatch.setattr(cli, "moved_head_probe", probe)

    assert cli._moved_head_probe("acme/widgets", "main", OK, HEAD) == READ
    assert fetched == [("acme/widgets", (OK, HEAD))]
    assert probed == [(tmp_path / "clone.git", "refs/remotes/origin/main", OK, HEAD)]


def test_orch_poll_wires_the_live_probe(tmp_path: Path) -> None:
    database = tmp_path / "orch.db"
    orch = tmp_path / "orch.yml"
    orch.write_text(
        "poll_seconds: 60\n"
        "grok:\n  webhook_url_env: U\n  webhook_key_env: K\n"
        "trusted_logins:\n  grok-login: [grok-bot]\n"
        "projects:\n  - repo: acme/widgets\n    factory_ref: main\n",
        encoding="utf-8",
    )
    cli._runtime(database, orch, create=True)

    built, _ = cli._runtime(database, orch, create=False)

    assert built.moved_head is cli._moved_head_probe
