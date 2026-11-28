"""Issue #182 (T05b): the factory repo's own hold lives in factory.md.

The root factory.md names hold.approver maxharris1 (the default max is no
login and could never clear a hold) and holds the trust paths. The factory
repo is ship: handoff, and since T05c (#183) the hold applies there too.
"""

from pathlib import Path

import pytest
import yaml

from lib import cli
from lib.dispatcher import _holds
from lib.factory_md import HoldConfig, load_factory_md
from lib.hold import decide, glob_can_match_under, glob_matches, held_paths
from lib.orch_yml import hold_approver_findings, load_orch_yml
from tests.test_ship_hold import (
    HEAD,
    approve,
    grok_jobs,
    ready,
    setup,
    snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
FACTORY_MD = ROOT / "factory.md"
ORCH_YML = ROOT / "orch.yml"

EXPECTED_PATHS = (
    "orch.yml",
    "lib/hold.py",
    "lib/orch_yml.py",
    "lib/harness_env.py",
    "lib/dispatch/holds.py",
    "lib/approver_commands.py",
    "lib/github_app_token.py",
    ".github/**",
    "factory.md",
)

HELD = (
    "orch.yml",
    "lib/hold.py",
    "lib/orch_yml.py",
    "lib/harness_env.py",
    "lib/dispatch/holds.py",
    "lib/approver_commands.py",
    "lib/github_app_token.py",
    ".github/workflows/ci.yml",
    ".github/workflows/new.yml",
    "factory.md",
)

NOT_HELD = (
    "lib/dispatcher.py",
    "lib/dispatch/comments.py",
    "lib/hold_extra.py",
    "skills/other/x.md",
    "skills/factory/SKILL.md",
    "skills/pstack/x/y.md",
    "skills/skills.lock",
    "grok/prompt.md",
    ".claude/skills/a/SKILL.md",
    "examples/cleffy/factory.md",
    "docs/orch.yml",
)


def root_config():
    config, findings = load_factory_md(FACTORY_MD)
    assert config is not None
    assert [f for f in findings if f.severity == "error"] == []
    return config, findings


def test_this_repos_factory_md_holds_the_trust_paths() -> None:
    config, _ = root_config()

    assert config.hold == HoldConfig(
        label="factory:hold",
        approver="maxharris1",
        paths=EXPECTED_PATHS,
    )


def test_the_held_globs_cover_the_trust_files() -> None:
    config, _ = root_config()

    assert held_paths(config.hold.paths, HELD + NOT_HELD) == tuple(sorted(HELD))


def test_check_config_accepts_the_factory_hold_approver(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _ = root_config()
    orch, _ = load_orch_yml(ORCH_YML, allow_placeholders=True)
    assert orch is not None

    assert [
        finding.severity for finding in hold_approver_findings(orch, config.hold)
    ] == ["warning"]
    assert (
        cli._check_config(
            [
                "--orch",
                str(ORCH_YML),
                "--allow-placeholders",
                "--factory",
                str(FACTORY_MD),
            ]
        )
        == 0
    )
    assert "ok" in capsys.readouterr().out.splitlines()


@pytest.mark.parametrize(
    "replacement",
    ["  approver: max\n", ""],
    ids=["explicit-max", "default-max"],
)
def test_check_config_still_refuses_approver_max(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    replacement: str,
) -> None:
    text = FACTORY_MD.read_text(encoding="utf-8")
    assert text.count("  approver: maxharris1\n") == 1
    factory = tmp_path / "factory.md"
    factory.write_text(text.replace("  approver: maxharris1\n", replacement))

    code = cli._check_config(
        [
            "--orch",
            str(ORCH_YML),
            "--allow-placeholders",
            "--factory",
            str(factory),
        ]
    )

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == 1
    assert "hold.approver max is not in approver_logins" in output
    assert "hold.approver max is not a trusted_logins key" in output


def test_orch_yml_has_no_hold_block() -> None:
    data = yaml.safe_load(ORCH_YML.read_text(encoding="utf-8"))

    assert "hold" not in data
    for project in data.get("projects") or []:
        assert "hold" not in project


def test_the_factory_repo_applies_its_hold_on_ship_handoff(tmp_path: Path) -> None:
    config, findings = root_config()

    assert config.ship == "handoff"
    assert config.hold is not None
    assert not any("hold has no effect" in item.message for item in findings)

    would_block = decide(
        config.hold,
        labels=(),
        changed_files=("orch.yml",),
        label_events=(),
        comments=(),
        approver_allowed=True,
        covers=lambda _head: False,
        factory_md_trust_changed=False,
    )
    assert would_block.blocks is True

    held = snapshot(
        approve(),
        factory=config,
        labels=("factory:hold",),
        files=("orch.yml",),
    )
    assert _holds(held) is True

    dispatcher, queue, actions = setup(tmp_path)
    ready(dispatcher, queue, HEAD)
    actions.states.clear()

    dispatcher.process(held)

    assert actions.handoffs == []
    assert actions.states == ["awaiting-ok"]
    assert any(row["action"] == "awaiting-ok" for row in grok_jobs(queue))
    assert actions.merges == []


def root_decision(files, trust):
    config, _ = root_config()
    return decide(
        config.hold,
        labels=(),
        changed_files=files,
        label_events=(),
        comments=(),
        approver_allowed=True,
        covers=lambda _head: False,
        factory_md_trust_changed=trust,
    )


def test_no_hold_glob_reaches_skills_grok_or_the_lock() -> None:
    config, _ = root_config()

    for pattern in config.hold.paths:
        for prefix in ("skills/", "grok/", ".claude/skills/"):
            assert not glob_can_match_under(pattern, prefix), (pattern, prefix)
        assert not glob_matches(pattern, "skills/skills.lock"), pattern


@pytest.mark.parametrize(
    "files",
    [
        ("skills/pstack/x.md",),
        ("grok/prompt.md",),
        ("skills/skills.lock", ".claude/skills/a/SKILL.md"),
        ("factory.md", "skills/factory/SKILL.md"),
    ],
)
def test_skills_grok_and_factory_md_prose_are_not_path_held(files) -> None:
    decision = root_decision(files, trust=False)

    assert decision.reasons == ()
    assert decision.blocks is False


def test_a_factory_md_trust_change_is_path_held() -> None:
    decision = root_decision(("factory.md", "grok/prompt.md"), trust=True)

    assert decision.reasons == ("paths",)
    assert decision.paths == ("factory.md",)
    assert decision.blocks is True


def test_an_unread_factory_md_comparison_fails_closed() -> None:
    decision = root_decision(("factory.md",), trust=None)

    assert decision.unread == "factory.md trust sections"
    assert decision.blocks is True


@pytest.mark.parametrize("trust", [True, False, None])
@pytest.mark.parametrize(
    "path",
    [
        "lib/hold.py",
        "orch.yml",
        "lib/orch_yml.py",
        "lib/harness_env.py",
        ".github/workflows/ci.yml",
        ".github/workflows/new.yml",
    ],
)
def test_the_safety_files_stay_held_whatever_factory_md_says(path, trust) -> None:
    decision = root_decision((path,), trust=trust)

    assert decision.reasons == ("paths",)
    assert decision.paths == (path,)
    assert decision.blocks is True


def test_a_nested_factory_md_is_not_the_root_exception() -> None:
    hold = HoldConfig(approver="maxharris1", paths=("**/factory.md",))

    decision = decide(
        hold,
        labels=(),
        changed_files=("examples/cleffy/factory.md",),
        label_events=(),
        comments=(),
        approver_allowed=True,
        covers=lambda _head: False,
        factory_md_trust_changed=False,
    )

    assert decision.paths == ("examples/cleffy/factory.md",)
