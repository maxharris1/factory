from __future__ import annotations

from pathlib import Path

import pytest

from lib.factory_md import load_factory_md, parse_factory_md
from lib.feeder_gates import GateLines, Holds, PathPolicy
from tests.test_feeder import FACTORY_MD, make_feeder
from tests.test_feeder_gates import (
    CLOSED_PULL,
    HOME,
    MERGED,
    OPEN_PULL,
    fed_heads,
    issue,
    ref,
    states,
)

ROOT = Path(__file__).resolve().parents[1]
PIN = "feeder:\n  max_holders_per_path: 1\n"
DEFAULT_MD = FACTORY_MD.replace(PIN, "")
EXCLUSIVE_MD = FACTORY_MD.replace(PIN, "feeder:\n  exclusive_paths: [model/]\n")
TWO = PathPolicy(max_holders=2)


def touches(*paths: str) -> GateLines:
    return GateLines(touches=frozenset(paths))


def feeder_md(body: str) -> str:
    return DEFAULT_MD.replace("skills: []\n", f"skills: []\n{body}")


def test_max_holders_defaults_to_two_and_exclusive_to_none() -> None:
    config, findings = parse_factory_md(DEFAULT_MD)
    assert config is not None, findings
    assert (config.max_holders_per_path, config.exclusive_paths) == (2, ())
    empty, _ = parse_factory_md(feeder_md("feeder:\n"))
    assert empty is not None and (empty.max_holders_per_path, empty.exclusive_paths) == (2, ())


def test_feeder_settings_parse() -> None:
    config, findings = parse_factory_md(
        feeder_md("feeder:\n  max_holders_per_path: 1\n  exclusive_paths: [model/, lib/*.sql]\n")
    )
    assert config is not None, findings
    assert config.max_holders_per_path == 1
    assert config.exclusive_paths == ("model/", "lib/*.sql")


def test_this_repos_factory_md_sets_two_holders_and_keeps_model_exclusive() -> None:
    config, findings = load_factory_md(ROOT / "factory.md")
    assert config is not None, findings
    assert (config.max_holders_per_path, config.exclusive_paths) == (2, ("model/",))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("feeder:\n  max_holders_per_path: 0\n",
         "feeder.max_holders_per_path must be an integer of at least 1"),
        ("feeder:\n  max_holders_per_path: -1\n",
         "feeder.max_holders_per_path must be an integer of at least 1"),
        ("feeder:\n  max_holders_per_path: \"2\"\n",
         "feeder.max_holders_per_path must be an integer of at least 1"),
        ("feeder:\n  max_holders_per_path: true\n",
         "feeder.max_holders_per_path must be an integer of at least 1"),
        ("feeder:\n  exclusive_paths: model/\n", "feeder.exclusive_paths must be a list"),
        ("feeder:\n  exclusive_paths: [../x]\n",
         "feeder.exclusive_paths[0] must be a repo-relative path or glob"),
        ("feeder:\n  max_holder_per_path: 2\n", "unknown field 'feeder.max_holder_per_path'"),
        ("feeder: 2\n", "feeder must be a mapping"),
    ],
)
def test_bad_feeder_settings_are_refused(body: str, message: str) -> None:
    config, findings = parse_factory_md(feeder_md(body))
    assert config is None
    assert message in [item.message for item in findings if item.severity == "error"]


def test_lax_reading_warns_and_falls_back_to_one_holder() -> None:
    config, findings = parse_factory_md(
        feeder_md("feeder:\n  max_holders_per_path: 0\n  later_setting: x\n"), strict=False
    )
    assert config is not None
    assert config.max_holders_per_path == 1
    assert [(item.severity, item.message) for item in findings] == [
        ("warning", "unknown field 'feeder.later_setting'"),
        ("warning", "feeder.max_holders_per_path must be an integer of at least 1"),
    ]


def test_a_bare_holds_keeps_one_holder() -> None:
    holds = Holds()
    holds.take(ref(1), (), touches("lib/x.py"))
    wait = holds.collision(ref(2), touches("lib/x.py"))
    assert wait is not None and wait.reason == "waiting on file lib/x.py held by #1"


def test_two_holders_share_a_path_and_a_third_waits_naming_both() -> None:
    holds = Holds(policies={HOME: TWO})
    holds.take(ref(1), (), touches("lib/x.py"))
    assert holds.collision(ref(2), touches("lib/x.py")) is None
    holds.take(ref(2), (), touches("lib/x.py"))
    wait = holds.collision(ref(3), touches("lib/x.py"))
    assert wait is not None and wait.reason == "waiting on file lib/x.py held by #1, #2"


def test_a_held_directory_never_goes_over_the_limit() -> None:
    holds = Holds(policies={HOME: TWO})
    holds.take(ref(1), (), touches("lib/"))
    assert holds.collision(ref(2), touches("lib/x.py")) is None
    holds.take(ref(2), (), touches("lib/x.py"))
    wait = holds.collision(ref(3), touches("lib/y.py"))
    assert wait is not None and wait.reason == "waiting on file lib/y.py held by #1, #2"


def test_an_exclusive_path_keeps_one_holder_from_either_side() -> None:
    policy = PathPolicy(exclusive=("model/",), max_holders=2)
    holds = Holds(policies={HOME: policy})
    holds.take(ref(1), (), touches("model/factory.yml"))
    wait = holds.collision(ref(2), touches("model/factory.yml"))
    assert wait is not None and wait.reason == "waiting on file model/factory.yml held by #1"
    held_side = Holds(policies={HOME: PathPolicy(exclusive=("model/x.yml",), max_holders=2)})
    held_side.take(ref(1), (), touches("model/x.yml"))
    wait = held_side.collision(ref(2), touches("model/"))
    assert wait is not None and wait.reason == "waiting on file model/x.yml held by #1"


@pytest.mark.parametrize(
    ("exclusive", "held", "asked", "reason"),
    [
        ("lib/model/", "lib/", "lib/", "waiting on file lib/ held by #1"),
        ("model/*.yml", "model/", "model/", "waiting on file model/ held by #1"),
        ("lib/model/", "lib/", "lib/model/x.py", "waiting on file lib/model/x.py held by #1"),
        ("**/*.sql", "db/", "db/", "waiting on file db/ held by #1"),
        ("lib/model/", "lib/", "lib/x.py", None),
        ("lib/model/", "lib/other/", "lib/", None),
        ("model/*.yml", "model/", "model/x.json", None),
        ("docs/**/*.sql", "lib/", "lib/", None),
        ("lib/**", "lib/", "lib/", "waiting on file lib/ held by #1"),
        ("model**/", "lib/", "lib/", None),
        ("a**b/", "lib/", "lib/", None),
        ("lib/m**/", "lib/x/", "lib/x/", None),
        ("lib/m**/", "lib/", "lib/", "waiting on file lib/ held by #1"),
        ("l**/x.sql", "lib/", "lib/", "waiting on file lib/ held by #1"),
        ("**lib/x", "lib/", "lib/", "waiting on file lib/ held by #1"),
        ("**/model/", "lib/", "lib/", "waiting on file lib/ held by #1"),
    ],
)
def test_an_exclusive_path_under_the_overlap_of_two_declarations_keeps_one_holder(
    exclusive: str, held: str, asked: str, reason: str | None
) -> None:
    holds = Holds(policies={HOME: PathPolicy(exclusive=(exclusive,), max_holders=2)})
    holds.take(ref(1), (), touches(held))
    wait = holds.collision(ref(2), touches(asked))
    assert (wait and wait.reason) == reason


def test_a_path_both_shared_and_exclusive_never_collides() -> None:
    policy = PathPolicy(shared=("docs/SPEC.md",), exclusive=("docs/SPEC.md",), max_holders=2)
    holds = Holds(policies={HOME: policy})
    holds.take(ref(1), (), touches("docs/SPEC.md"))
    holds.take(ref(2), (), touches("docs/SPEC.md"))
    assert holds.collision(ref(3), touches("docs/SPEC.md")) is None


def test_co_holders_names_each_shared_path_and_its_other_holders() -> None:
    holds = Holds(policies={HOME: PathPolicy(shared=("docs/SPEC.md",), max_holders=2)})
    holds.take(ref(1), (), touches("lib/x.py", "docs/SPEC.md"))
    holds.take(ref(2), (), touches("lib/"))
    holds.take(ref(4), (), touches("lib/z.py"))
    found = holds.co_holders(ref(3), touches("lib/x.py", "docs/SPEC.md", "bin/run"))
    assert found == {"lib/x.py": (ref(1), ref(2))}


def logged_feeder(tmp_path: Path, factory_text: str = DEFAULT_MD, now: list[float] | None = None):
    feeder, _, github, _ = make_feeder(tmp_path, box=4, factory_text=factory_text, now=now)
    log: list[str] = []
    feeder.log = log.append
    return feeder, github, log


def test_two_tickets_on_one_path_feed_and_a_third_waits_naming_both(tmp_path: Path) -> None:
    feeder, github, log = logged_feeder(tmp_path)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/x.py"),
        issue(2, created_at=2.0, body="Touches: lib/x.py"),
        issue(3, created_at=3.0, body="Touches: lib/x.py"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1", "factory/issue-2"]
    assert states(tmp_path)["3"] == ("waiting-blocked", "waiting on file lib/x.py held by #1, #2")
    assert "feeder: owner/app#2 shares lib/x.py with #1" in log
    assert not any("owner/app#1 shares" in line for line in log)


def fed_pair(tmp_path: Path):
    now = [10_000.0]
    feeder, github, log = logged_feeder(tmp_path, now=now)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/x.py"),
        issue(2, created_at=2.0, body="Touches: lib/x.py"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1", "factory/issue-2"]
    github.statuses = {ref(501): OPEN_PULL, ref(502): OPEN_PULL}
    github.issues[HOME] = [issue(3, created_at=3.0, body="Touches: lib/x.py")]
    now[0] += 60
    assert feeder.feed_once().fed == ()
    assert states(tmp_path)["3"] == ("waiting-blocked", "waiting on file lib/x.py held by #1, #2")
    return feeder, github, log, now


def test_open_pr_holders_keep_the_limit_across_polls(tmp_path: Path) -> None:
    fed_pair(tmp_path)


@pytest.mark.parametrize("ended", [MERGED, CLOSED_PULL])
def test_a_holder_whose_pr_ends_frees_its_spot_next_poll(tmp_path: Path, ended) -> None:
    feeder, github, log, now = fed_pair(tmp_path)
    pulls = len(github.pulls)
    github.statuses[ref(501)] = ended
    now[0] += 60
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-3"]
    assert len(github.pulls) == pulls + 1
    assert states(tmp_path)["2"][0] in ("fed", "done")
    assert "feeder: owner/app#3 shares lib/x.py with #2" in log


def test_conflicts_with_still_keeps_one_holder(tmp_path: Path) -> None:
    feeder, github, _ = logged_feeder(tmp_path)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/x.py"),
        issue(2, created_at=2.0, body="Touches: docs/a.md\nConflicts with: #1"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on #1 (conflicts with it)")


def test_an_exclusive_path_keeps_one_holder(tmp_path: Path) -> None:
    feeder, github, log = logged_feeder(tmp_path, EXCLUSIVE_MD)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: model/factory.yml"),
        issue(2, created_at=2.0, body="Touches: model/factory.yml"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == (
        "waiting-blocked", "waiting on file model/factory.yml held by #1"
    )
    assert not any(" shares " in line for line in log)


def test_an_exclusive_directory_held_waits_a_file_under_it(tmp_path: Path) -> None:
    feeder, github, _ = logged_feeder(tmp_path, EXCLUSIVE_MD)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: model/"),
        issue(2, created_at=2.0, body="Touches: model/x.yml"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file model/x.yml held by #1")


def test_a_path_shared_and_exclusive_never_collides_in_the_feeder(tmp_path: Path) -> None:
    text = FACTORY_MD.replace(
        PIN, "shared_paths: [docs/SPEC.md]\nfeeder:\n  max_holders_per_path: 1\n"
        "  exclusive_paths: [docs/SPEC.md]\n"
    )
    feeder, github, log = logged_feeder(tmp_path, text)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: docs/SPEC.md"),
        issue(2, created_at=2.0, body="Touches: docs/SPEC.md"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1", "factory/issue-2"]
    assert not any(" shares " in line for line in log)


def test_parent_directory_declarations_wait_on_an_exclusive_descendant(tmp_path: Path) -> None:
    text = FACTORY_MD.replace(PIN, "feeder:\n  exclusive_paths: [lib/model/]\n")
    feeder, github, log = logged_feeder(tmp_path, text)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/"),
        issue(2, created_at=2.0, body="Touches: lib/"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1"]
    assert states(tmp_path)["2"] == ("waiting-blocked", "waiting on file lib/ held by #1")
    assert not any(" shares " in line for line in log)


def test_an_exclusive_glob_that_cannot_reach_a_directory_keeps_its_holder_limit(
    tmp_path: Path,
) -> None:
    text = FACTORY_MD.replace(PIN, "feeder:\n  exclusive_paths: [model**/]\n")
    feeder, github, _ = logged_feeder(tmp_path, text)
    github.issues[HOME] = [
        issue(1, created_at=1.0, body="Touches: lib/"),
        issue(2, created_at=2.0, body="Touches: lib/"),
    ]
    assert fed_heads(github, feeder.feed_once()) == ["factory/issue-1", "factory/issue-2"]
