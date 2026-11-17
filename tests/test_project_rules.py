"""Issue #152: each project's ship mode and round caps in status.json.

The poller stores what it parsed from each project's factory.md
(lib/project_rules.py); the read-only dashboard reads it back as
`projects`. These run the live poll source over a fake gh through a real
poll_once, then collect_snapshot, so nothing is written by hand except
where a test needs a row the poller cannot produce.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lib import project_rules
from lib.dashboard import collect_snapshot
from lib.dispatcher import Dispatcher
from lib.factory_md import load_factory_md
from lib.orch_yml import OrchConfig, Project
from lib.poll_source import LivePollSource
from lib.project_rules import FactoryRead
from lib.queue import SQLiteJobQueue
from tests.test_dispatcher import FakeGrokDelivery, FakeReviewActions
from tests.test_live_poll import FACTORY, FakeGh

ROOT = Path(__file__).resolve().parents[1]
MERGE_REPO = "maxharris1/cleffy"
HANDOFF_REPO = "maxharris1/factory"
APPROVER_REPO = "maxharris1/tracy-harris-coaching"
FIXTURES = {
    MERGE_REPO: ("dev", ROOT / "examples/cleffy/factory.md"),
    HANDOFF_REPO: ("main", ROOT / "factory.md"),
    APPROVER_REPO: ("main", ROOT / "examples/tracy/factory.md"),
}
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class ReposGh:
    """A gh runner serving a different factory.md per repo."""

    def __init__(self, texts: dict[str, str]):
        self.fakes = {repo: FakeGh(text) for repo, text in texts.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        call = list(argv)
        self.calls.append(call)
        endpoint = next(part for part in call if part.startswith("repos/"))
        repo = "/".join(endpoint.split("/")[1:3])
        return self.fakes[repo](argv, timeout)

    def factory_calls(self, repo: str) -> int:
        return sum(
            any(part == f"repos/{repo}/contents/factory.md" for part in call)
            for call in self.calls
        )


class World:
    def __init__(self, tmp_path: Path, projects: tuple[Project, ...], texts: dict[str, str]):
        self.orch = OrchConfig(poll_seconds=60, projects=projects)
        self.gh = ReposGh(texts)
        self.clock = 100.0
        self.source = LivePollSource(self.orch, gh_runner=self.gh, sleep=lambda seconds: None)
        self.path = tmp_path / "orch.db"
        self.queue = SQLiteJobQueue(self.path, clock=lambda: self.clock)
        self.dispatcher = Dispatcher(
            self.path,
            self.queue,
            trusted_logins={"github": {}, "linear": {}},
            review_actions=FakeReviewActions(),
            grok_delivery=FakeGrokDelivery(),
        )
        self.log = tmp_path / "poller.log"
        self.log.write_text("", encoding="utf-8")

    def poll(self) -> None:
        try:
            self.dispatcher.poll_once(self.source)
        except Exception:
            # end_cycle raises PollCycleError when the poll recorded source
            # errors (an unreadable factory.md is one); the reads are stored first.
            pass

    def projects(self, now: float = 112.0) -> list[dict]:
        return collect_snapshot(self.path, self.log, now=now, orch=self.orch)["projects"]


def fixture_world(tmp_path: Path) -> World:
    return World(
        tmp_path,
        tuple(Project(repo, factory_ref=ref) for repo, (ref, _path) in FIXTURES.items()),
        {repo: path.read_text(encoding="utf-8") for repo, (_ref, path) in FIXTURES.items()},
    )


def test_projects_match_merge_handoff_and_approver_fixtures(tmp_path: Path) -> None:
    world = fixture_world(tmp_path)
    world.poll()
    projects = world.projects()
    assert [item["repo"] for item in projects] == list(FIXTURES)
    for item in projects:
        config, _findings = load_factory_md(FIXTURES[item["repo"]][1])
        assert config is not None
        ship, _, approver = config.ship.partition(":")
        assert item == {
            "repo": item["repo"],
            "status": "ok",
            "tracker": config.tickets,
            "ship": ship,
            "approver": approver or None,
            "rounds": {"low": config.rounds_low, "high": config.rounds_high},
            "read_age_seconds": 12,
        }
    assert [(item["ship"], item["approver"]) for item in projects] == [
        ("merge", None),
        ("handoff", None),
        ("after", "tracy-site"),
    ]


def test_factory_reads_use_the_poll_cycle_cache(tmp_path: Path) -> None:
    world = fixture_world(tmp_path)
    world.poll()
    assert [world.gh.factory_calls(repo) for repo in FIXTURES] == [1, 1, 1]
    world.poll()
    assert [world.gh.factory_calls(repo) for repo in FIXTURES] == [2, 2, 2]


def test_round_caps_come_from_factory_md(tmp_path: Path) -> None:
    text = FACTORY.format(tickets="github", ship="merge").replace("high: 3", "high: 2")
    world = World(tmp_path, (Project("acme/widgets", factory_ref="main"),), {"acme/widgets": text})
    world.poll()
    assert world.projects()[0]["rounds"] == {"low": 1, "high": 2}


@pytest.mark.parametrize(
    ("project", "text", "fail", "reason"),
    [
        (
            Project("acme/widgets", factory_ref="main"),
            FACTORY.format(tickets="github", ship="sometimes"),
            False,
            "acme/widgets factory.md: ship must be merge, handoff, or after:<approver>",
        ),
        (
            Project("acme/widgets"),
            FACTORY.format(tickets="github", ship="merge"),
            False,
            "acme/widgets has no projects[].factory_ref in orch.yml",
        ),
        (
            Project("acme/widgets", factory_ref="dev"),
            FACTORY.format(tickets="github", ship="merge"),
            False,
            "acme/widgets factory.md base_branch is main, not configured factory_ref dev",
        ),
        (
            Project("acme/widgets", factory_ref="main"),
            FACTORY.format(tickets="github", ship="merge"),
            True,
            "gh api GET contents/factory.md returned HTTP 404",
        ),
    ],
    ids=["invalid-front-block", "no-factory-ref", "base-branch-mismatch", "gh-failure"],
)
def test_unreadable_factory_md_is_unknown_with_reason(
    tmp_path: Path, project: Project, text: str, fail: bool, reason: str
) -> None:
    world = World(tmp_path, (project,), {"acme/widgets": text})
    world.gh.fakes["acme/widgets"].fail_factory = fail
    world.poll()
    assert world.projects() == [
        {
            "repo": "acme/widgets",
            "status": "unknown",
            "tracker": "unknown",
            "ship": "unknown",
            "approver": None,
            "rounds": None,
            "reason": reason,
            "read_age_seconds": 12,
        }
    ]


def test_a_failed_read_replaces_an_older_good_one(tmp_path: Path) -> None:
    world = World(
        tmp_path,
        (Project("acme/widgets", factory_ref="main"),),
        {"acme/widgets": FACTORY.format(tickets="github", ship="merge")},
    )
    world.poll()
    assert world.projects()[0]["status"] == "ok"
    world.gh.fakes["acme/widgets"].fail_factory = True
    world.clock = 110.0
    world.poll()
    assert world.projects()[0]["status"] == "unknown"
    assert world.projects()[0]["read_age_seconds"] == 2


def test_no_row_is_unknown_not_read_yet(tmp_path: Path) -> None:
    """An older database (no table) and a project the poller has not read."""
    path = tmp_path / "orch.db"
    SQLiteJobQueue(path, clock=lambda: 100.0)
    with sqlite3.connect(path) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'project_rules'"
        ).fetchone()
    log = tmp_path / "poller.log"
    orch = OrchConfig(poll_seconds=60, projects=(Project("acme/widgets", factory_ref="main"),))
    snapshot = collect_snapshot(path, log, now=112.0, orch=orch)
    assert snapshot["projects"] == [
        {
            "repo": "acme/widgets",
            "status": "unknown",
            "tracker": "unknown",
            "ship": "unknown",
            "approver": None,
            "rounds": None,
            "reason": "not read by the poller yet",
            "read_age_seconds": None,
        }
    ]
    assert collect_snapshot(path, log, now=112.0)["projects"] == []


def test_a_project_dropped_from_orch_yml_loses_its_row(tmp_path: Path) -> None:
    world = fixture_world(tmp_path)
    world.poll()
    world.source.config = OrchConfig(
        poll_seconds=60, projects=(Project(HANDOFF_REPO, factory_ref="main"),)
    )
    world.poll()
    with sqlite3.connect(world.path) as connection:
        repos = [row[0] for row in connection.execute("SELECT repo FROM project_rules")]
    assert repos == [HANDOFF_REPO]


def test_a_source_without_factory_reads_stores_nothing(tmp_path: Path) -> None:
    world = fixture_world(tmp_path)

    class Plain:
        def github_candidates(self, label_prefix, *, exclude=frozenset()):
            return ()

        def linear_candidates(self, query, *, exclude, reserve):
            return ()

        def tracked_tickets(self, refs):
            return ()

    world.dispatcher.poll_once(Plain())
    assert {item["status"] for item in world.projects()} == {"unknown"}


def test_a_storage_failure_is_a_poll_error(tmp_path: Path) -> None:
    world = fixture_world(tmp_path)

    def broken() -> tuple[FactoryRead, ...]:
        raise RuntimeError("boom")

    world.source.factory_reads = broken  # type: ignore[method-assign]
    world.poll()
    with sqlite3.connect(world.path) as connection:
        errors = connection.execute("SELECT repo, error FROM poll_errors").fetchall()
    assert ("factory", "RuntimeError") in errors


def test_table_holds_values_or_a_reason_never_both(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    project_rules.ensure_table(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO project_rules VALUES ('a/b', 1.0, 'github', 'merge', 1, 3, 'why')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO project_rules VALUES ('a/b', 1.0, NULL, NULL, NULL, NULL, NULL)"
        )
    with pytest.raises(ValueError):
        FactoryRead("a/b", None, None)


def test_project_reason_is_redacted(tmp_path: Path) -> None:
    world = World(
        tmp_path,
        (Project("acme/widgets", factory_ref="main"),),
        {"acme/widgets": FACTORY.format(tickets="github", ship="merge")},
    )
    world.poll()
    with sqlite3.connect(world.path) as connection:
        project_rules.record_reads(
            connection,
            [FactoryRead("acme/widgets", None, f"failed with token {GITHUB_TOKEN}")],
            100.0,
        )
    snapshot = collect_snapshot(world.path, world.log, now=112.0, orch=world.orch)
    entry = snapshot["projects"][0]
    assert entry["status"] == "unknown"
    assert entry["reason"].startswith("failed with token ")
    assert GITHUB_TOKEN not in json.dumps(snapshot)
