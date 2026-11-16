import copy
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lib.factory_md import EXAMPLE_PATHS, load_factory_md, parse_factory_context, parse_factory_md
from lib.schema import AUT0_REPO, HARNESSES

ROOT = Path(__file__).resolve().parents[1]

VALID = {
    "base_branch": "main",
    "tickets": "github",
    "builder": {"harness": "claude"},
    "reviewer": {"harness": "codex"},
    "review": {"rounds": {"low": 1, "high": 3}, "verify": "none"},
    "ship": "merge",
    "skills": [],
}


def write_factory(directory: Path, data: dict) -> Path:
    path = directory / "factory.md"
    raw = yaml.safe_dump(data, sort_keys=False)
    path.write_text(f"---\n{raw}---\n", encoding="utf-8")
    return path


def without(path: str) -> dict:
    data = copy.deepcopy(VALID)
    cursor = data
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor[part]
    del cursor[parts[-1]]
    return data


def messages(path: Path) -> list[str]:
    _config, findings = load_factory_md(path)
    return [finding.message for finding in findings]


@pytest.mark.parametrize("relative", EXAMPLE_PATHS)
def test_examples_pass(relative: str) -> None:
    config, findings = load_factory_md(ROOT / relative)
    assert findings == []
    assert config is not None


def test_examples_match_the_spec_choices() -> None:
    aut0, aut0_findings = load_factory_md(ROOT / "examples/aut0/factory.md")
    cleffy, cleffy_findings = load_factory_md(ROOT / "examples/cleffy/factory.md")
    tracy, tracy_findings = load_factory_md(ROOT / "examples/tracy/factory.md")
    assert aut0_findings == [] and cleffy_findings == [] and tracy_findings == []
    assert aut0 is not None and cleffy is not None and tracy is not None

    assert aut0.base_branch == "dev"
    assert aut0.tickets == "linear"
    assert aut0.ship == "handoff"
    assert aut0.verify == "verify-aut0"
    assert aut0.skills == ("nsa-process", "aut0-review", "verify-aut0")
    assert aut0.github_issues is False
    assert aut0.github_labels is False

    assert cleffy.base_branch == "dev"
    assert cleffy.tickets == "github"
    assert cleffy.ship == "merge"
    assert cleffy.verify == "none"
    assert cleffy.rounds_low == 1
    assert cleffy.rounds_high == 3
    cleffy_text = (ROOT / "examples/cleffy/factory.md").read_text(encoding="utf-8")
    for phrase in (
        "migrations",
        "RLS",
        "Edge Functions",
        "supabase/",
        "Stripe",
        "tracking pixels",
        "public-domain",
        "Start free",
        "ui-check",
        "Max's merge",
    ):
        assert phrase in cleffy_text

    assert tracy.base_branch == "main"
    assert tracy.tickets == "github"
    assert tracy.ship == "after:tracy-site"
    assert tracy.verify == "none"
    assert tracy.rounds_low == 1
    assert tracy.rounds_high == 3
    tracy_text = (ROOT / "examples/tracy/factory.md").read_text(encoding="utf-8")
    for phrase in (
        "Vercel",
        "previews",
        "fast-forwarded",
        "no force-push",
        "no merge commit",
        "lint, typecheck and build",
        "Never ship",
        "CLAUDE.md",
        "on the box",
        "ok head=<sha>",
    ):
        assert phrase in tracy_text


def test_aut0_example_matches_cleanup_draft() -> None:
    cleanup = (ROOT / "docs/AUT0-CLEANUP.md").read_text(encoding="utf-8")
    match = re.search(
        r"## Draft `factory.md` for Aut0\n\n```markdown\n(.*?)```",
        cleanup,
        re.S,
    )
    assert match is not None
    assert (ROOT / "examples/aut0/factory.md").read_text(encoding="utf-8") == match.group(1)


def test_generic_example_matches_spec() -> None:
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    match = re.search(
        r"Generic example \(GitHub-issue repo, merge on approval\):\n\n```markdown\n(.*?)```",
        spec,
        re.S,
    )
    assert match is not None
    assert (ROOT / "docs/factory.example.md").read_text(encoding="utf-8") == match.group(1)


@pytest.mark.parametrize(
    ("remove", "message"),
    [
        ("base_branch", "missing required field 'base_branch'"),
        ("tickets", "missing required field 'tickets'"),
        ("builder", "missing required field 'builder.harness'"),
        ("builder.harness", "missing required field 'builder.harness'"),
        ("reviewer", "missing required field 'reviewer.harness'"),
        ("reviewer.harness", "missing required field 'reviewer.harness'"),
        ("review", "missing required field 'review.rounds.low'"),
        ("review.rounds", "missing required field 'review.rounds.low'"),
        ("review.rounds.low", "missing required field 'review.rounds.low'"),
        ("review.rounds.high", "missing required field 'review.rounds.high'"),
        ("review.verify", "missing required field 'review.verify'"),
        ("ship", "missing required field 'ship'"),
        ("skills", "missing required field 'skills'"),
    ],
)
def test_missing_required_field(tmp_path: Path, remove: str, message: str) -> None:
    path = write_factory(tmp_path, without(remove))
    config, findings = load_factory_md(path)
    assert config is None
    assert message in messages(path)
    assert any(finding.severity == "error" for finding in findings)


def _actors(
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
) -> dict:
    data = copy.deepcopy(VALID)
    data["builder"] = {"harness": builder_harness}
    if builder_model is not None:
        data["builder"]["model"] = builder_model
    data["reviewer"] = {"harness": reviewer_harness}
    if reviewer_model is not None:
        data["reviewer"]["model"] = reviewer_model
    return data


@pytest.mark.parametrize("harness", HARNESSES)
def test_same_family_reviewer_is_refused(tmp_path: Path, harness: str) -> None:
    data = copy.deepcopy(VALID)
    data["builder"]["harness"] = harness
    data["reviewer"]["harness"] = harness
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    found = [finding.message for finding in findings]
    if harness == "cursor":
        assert found == [
            "same-family reviewer: builder family not established: "
            "give a claude*/sonnet*/opus*/haiku*/gpt*/codex*/o<n> model hint",
            "same-family reviewer: reviewer family not established: "
            "give a claude*/sonnet*/opus*/haiku*/gpt*/codex*/o<n> model hint",
        ]
        return
    family = "anthropic" if harness == "claude" else "openai"
    assert found == [
        "same-family reviewer: "
        f"builder family {family!r} and reviewer family {family!r}"
    ]


@pytest.mark.parametrize(
    ("builder_harness", "builder_model", "reviewer_harness", "reviewer_model"),
    [
        ("claude", "claude-sonnet", "cursor", "claude-sonnet"),
        ("claude", None, "cursor", "claude-opus"),
        ("codex", None, "cursor", "gpt-6-astra"),
        ("codex", "codex-mini", "cursor", "codex-mini"),
        ("cursor", "claude-sonnet", "claude", None),
        ("cursor", "claude-sonnet", "claude", "claude-haiku"),
    ],
)
def test_cursor_hint_in_the_same_family_is_refused(
    tmp_path: Path,
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
) -> None:
    data = _actors(builder_harness, builder_model, reviewer_harness, reviewer_model)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    family = "openai" if builder_harness == "codex" else "anthropic"
    assert [finding.message for finding in findings] == [
        "same-family reviewer: "
        f"builder family {family!r} and reviewer family {family!r}"
    ]


@pytest.mark.parametrize(
    "hint",
    [
        "xsonnet",
        "magnum-opus",
        "opusmt",
        "haikus",
        "claudette",
        "opus-mt-en-de",
        "codexes",
        "gpt-j",
        "gpt-neo-2.7b",
        "o3x",
    ],
)
def test_family_word_needs_a_boundary(tmp_path: Path, hint: str) -> None:
    data = _actors("claude", None, "cursor", hint)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(
        "reviewer family not established" in finding.message for finding in findings
    )
    assert not any("reviewer family 'anthropic'" in f.message for f in findings)


@pytest.mark.parametrize(
    ("hint", "family"),
    [
        ("claude", "anthropic"),
        ("opus", "anthropic"),
        ("claude-3.5", "anthropic"),
        ("sonnet:latest", "anthropic"),
        ("haiku_4", "anthropic"),
        ("gpt5", "openai"),
        ("gpt-4o", "openai"),
        ("codex-mini", "openai"),
        ("o3", "openai"),
        ("o4-mini", "openai"),
    ],
)
def test_family_word_with_a_boundary_resolves(
    tmp_path: Path, hint: str, family: str
) -> None:
    other = "codex" if family == "anthropic" else "claude"
    data = _actors(other, None, "cursor", hint)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert findings == []
    assert config is not None


def test_cursor_reviewer_with_another_family_is_accepted(tmp_path: Path) -> None:
    data = _actors("claude", "claude-sonnet", "cursor", "gpt-6-astra")
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is not None
    assert findings == []
    assert config.reviewer_harness == "cursor"
    assert config.reviewer_model == "gpt-6-astra"


@pytest.mark.parametrize(
    ("builder_harness", "builder_model", "reviewer_harness", "reviewer_model"),
    [
        ("cursor", None, "cursor", None),
        ("cursor", None, "claude", None),
        ("claude", None, "cursor", None),
        ("cursor", "grok-4", "cursor", "composer-2"),
    ],
)
def test_unresolved_family_is_refused(
    tmp_path: Path,
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
) -> None:
    data = _actors(builder_harness, builder_model, reviewer_harness, reviewer_model)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any("family not established" in finding.message for finding in findings)
    assert any(
        "give a claude*/sonnet*/opus*/haiku*/gpt*/codex*/o<n> model hint"
        in finding.message
        for finding in findings
    )


@pytest.mark.parametrize("hint", ["o3", "O3", "o4-mini"])
def test_o_series_hint_is_openai(tmp_path: Path, hint: str) -> None:
    data = _actors("codex", None, "cursor", hint)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(
        "same-family reviewer" in finding.message and "openai" in finding.message
        for finding in findings
    )
    assert not any("not established" in finding.message for finding in findings)
    assert not any("unknown model" in finding.message for finding in findings)


def test_opus_hint_is_anthropic_not_openai(tmp_path: Path) -> None:
    data = _actors("claude", None, "cursor", "opus")
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(
        "reviewer family 'anthropic'" in finding.message for finding in findings
    )
    assert not any("reviewer family 'openai'" in finding.message for finding in findings)


def test_ollama_hint_stays_unresolved_not_openai(tmp_path: Path) -> None:
    data = _actors("claude", None, "cursor", "ollama")
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(
        "reviewer family not established" in finding.message for finding in findings
    )
    assert not any("reviewer family 'openai'" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "hint",
    ["sonnet-4", "opus-4.1", "haiku-3.5", "SoNnEt-4", "OpUs-4.1", "HaIkU-3.5"],
)
def test_cursor_claude_style_hint_matches_claude_builder(
    tmp_path: Path, hint: str
) -> None:
    data = _actors("claude", None, "cursor", hint)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(
        "builder family 'anthropic' and reviewer family 'anthropic'"
        in finding.message
        for finding in findings
    )
    assert not any("not established" in finding.message for finding in findings)
    assert not any("unknown model" in finding.message for finding in findings)


@pytest.mark.parametrize("hint", ["sonnet-4", "opus-4.1", "haiku-3.5"])
def test_cursor_claude_style_hint_is_accepted_against_codex(
    tmp_path: Path, hint: str
) -> None:
    data = _actors("codex", None, "cursor", hint)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is not None
    assert findings == []


@pytest.mark.parametrize(
    ("builder_harness", "builder_model", "reviewer_harness", "reviewer_model", "role"),
    [
        ("claude", "gpt-x", "claude", None, "builder"),
        ("claude", None, "claude", "gpt-x", "reviewer"),
        ("codex", "claude-sonnet", "codex", None, "builder"),
        ("codex", "sonnet-4", "claude", None, "builder"),
        ("codex", None, "codex", "claude-opus", "reviewer"),
        ("claude", "gpt-x", "cursor", "gpt-6-astra", "builder"),
    ],
)
def test_hint_that_contradicts_harness_is_refused(
    tmp_path: Path,
    builder_harness: str,
    builder_model: str | None,
    reviewer_harness: str,
    reviewer_model: str | None,
    role: str,
) -> None:
    data = _actors(builder_harness, builder_model, reviewer_harness, reviewer_model)
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(
        role in finding.message and "contradicts" in finding.message
        for finding in findings
    )


def test_different_models_on_one_harness_are_still_same_family(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["builder"] = {"harness": "claude", "model": "claude-a"}
    data["reviewer"] = {"harness": "claude", "model": "claude-b"}
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(finding.severity == "error" for finding in findings)


@pytest.mark.parametrize("flag", ["github_issues", "github_labels"])
def test_linear_plus_github_creation_is_refused(tmp_path: Path, flag: str) -> None:
    data = copy.deepcopy(VALID)
    data["tickets"] = "linear"
    data[flag] = True
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any(flag in finding.message and "linear" in finding.message for finding in findings)


def test_linear_without_github_flags_passes(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["tickets"] = "linear"
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert findings == []
    assert config is not None
    assert config.github_issues is False
    assert config.github_labels is False


def test_github_flags_are_allowed_when_tickets_are_github(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["github_issues"] = True
    data["github_labels"] = True
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert findings == []
    assert config is not None
    assert config.github_issues is True
    assert config.github_labels is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("low", 2),
        ("high", 4),
        ("high", 10),
    ],
)
def test_round_caps_above_section_8_are_refused(
    tmp_path: Path, name: str, value: int
) -> None:
    data = copy.deepcopy(VALID)
    data["review"]["rounds"][name] = value
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    ceiling = 1 if name == "low" else 3
    assert any(
        f"review.rounds.{name} must be at most {ceiling}" in finding.message
        for finding in findings
    )


def test_unknown_model_warns_and_still_passes(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["builder"]["model"] = "not-a-real-model"
    data["reviewer"]["model"] = "gpt-6-astra"
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is not None
    assert config.builder_model == "not-a-real-model"
    assert [finding.severity for finding in findings] == ["warning"]
    assert "unknown model name 'not-a-real-model'" in findings[0].message
    assert "builder.model" in findings[0].message


def test_yaml_off_is_not_a_verify_value(tmp_path: Path) -> None:
    text = """---
base_branch: main
tickets: github
builder:
  harness: claude
reviewer:
  harness: codex
review:
  rounds:
    low: 1
    high: 3
  verify: off
ship: merge
skills: []
---
"""
    path = tmp_path / "factory.md"
    path.write_text(text, encoding="utf-8")
    config, findings = load_factory_md(path)
    assert config is None
    assert any("review.verify" in finding.message for finding in findings)


def test_unknown_field_is_refused(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["owner_bot"] = "grok"
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert config is None
    assert any("unknown field 'owner_bot'" in finding.message for finding in findings)


def test_unknown_fields_warn_when_read_leniently(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["owner_bot"] = "grok"
    data["builder"]["temperature"] = 1
    text = write_factory(tmp_path, data).read_text(encoding="utf-8")

    config, findings = parse_factory_md(text, strict=False)

    assert config is not None
    assert config.builder_harness == "claude"
    assert [(f.severity, f.message) for f in findings] == [
        ("warning", "unknown field 'owner_bot'"),
        ("warning", "unknown field 'builder.temperature'"),
    ]
    context, context_findings = parse_factory_context(text, strict=False)
    assert context is not None
    assert context_findings == findings


@pytest.mark.parametrize(
    ("section", "extra"),
    [
        ("builder", {"model": "claude-opus"}),
        ("reviewer", {"model": "gpt-5"}),
        ("review", {}),
        ("review.rounds", {}),
        ("hold", {"approver": "max-login", "paths": ["src/**"], "label": "qa:hold"}),
    ],
)
def test_lenient_read_drops_only_an_unknown_nested_key(
    tmp_path: Path, section: str, extra: dict
) -> None:
    data = copy.deepcopy(VALID)
    data.setdefault("hold", {})
    parts = section.split(".")
    cursor = data
    for part in parts:
        cursor = cursor[part]
    cursor.update(extra)
    known, known_findings = parse_factory_md(
        write_factory(tmp_path, data).read_text(encoding="utf-8")
    )
    assert known is not None and known_findings == []
    cursor["future_field"] = True
    text = write_factory(tmp_path, data).read_text(encoding="utf-8")

    config, findings = parse_factory_md(text, strict=False)

    assert config == known
    assert [(f.severity, f.message) for f in findings] == [
        ("warning", f"unknown field '{section}.future_field'")
    ]


def test_lenient_read_still_refuses_a_bad_known_field(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["owner_bot"] = "grok"
    data["ship"] = "yolo"
    text = write_factory(tmp_path, data).read_text(encoding="utf-8")

    config, findings = parse_factory_md(text, strict=False)

    assert config is None
    assert [f.severity for f in findings if "owner_bot" in f.message] == ["warning"]
    assert any(f.severity == "error" and "ship" in f.message for f in findings)


def test_ci_runner_is_optional(tmp_path: Path) -> None:
    data = copy.deepcopy(VALID)
    data["ci_runner"] = "factory-local"
    config, findings = load_factory_md(write_factory(tmp_path, data))
    assert findings == []
    assert config is not None
    assert config.ci_runner == "factory-local"


def test_prose_sections_are_not_required(tmp_path: Path) -> None:
    config, findings = load_factory_md(write_factory(tmp_path, VALID))
    assert findings == []
    assert config is not None


def test_duplicate_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "factory.md"
    path.write_text(
        "---\nbase_branch: main\nbase_branch: dev\ntickets: github\n---\n",
        encoding="utf-8",
    )
    config, findings = load_factory_md(path)
    assert config is None
    assert any("duplicate key" in finding.message for finding in findings)


def test_aut0_repo_constant() -> None:
    assert AUT0_REPO == "aut0-code/aut0"


def test_f2_03_runner_checklist_holds_no_token_and_names_the_wiring() -> None:
    """F2-03: the checklist is Max's; it carries no token and keeps deploys hosted."""
    path = Path(__file__).resolve().parents[1] / "docs" / "F2-03-RUNNER-CHECKLIST.md"
    text = path.read_text(encoding="utf-8")
    # GitHub runner registration/removal tokens are ~29 upper-case
    # alphanumerics; PATs and app tokens have known prefixes.
    assert re.search(r"\b[A-Z0-9]{25,}\b", text) is None
    assert re.search(r"\b(gh[pousr]_|github_pat_)", text) is None
    assert re.search(r"--token\b", text) is None
    for needle in (
        "ci_runner: factory-pc-wsl",
        "runs-on: [self-hosted, factory-pc-wsl]",
        "factory-mac",
        "Credential-holding deploy workflows stay on hosted runners",
        "Max only",
    ):
        assert needle in text, needle
    config, findings = parse_factory_md(
        "---\nbase_branch: main\ntickets: github\nbuilder:\n  harness: claude\n"
        "reviewer:\n  harness: codex\nreview:\n  rounds:\n    low: 1\n    high: 3\n"
        "  verify: none\nship: handoff\nskills: []\nci_runner: factory-pc-wsl\n---\n"
    )
    assert [item for item in findings if item.severity == "error"] == []
    assert config is not None and config.ci_runner == "factory-pc-wsl"


def with_pre_ready(value: object) -> dict:
    data = copy.deepcopy(VALID)
    data["review"]["pre_ready"] = value
    return data


def test_pre_ready_parses_to_its_command_and_timeout(tmp_path: Path) -> None:
    path = write_factory(tmp_path, with_pre_ready({"command": "make test", "timeout_seconds": 900}))
    config, findings = load_factory_md(path)

    assert findings == []
    assert config is not None and config.pre_ready is not None
    assert (config.pre_ready.command, config.pre_ready.timeout_seconds) == ("make test", 900)


def test_pre_ready_is_unset_without_the_key(tmp_path: Path) -> None:
    config, _ = load_factory_md(write_factory(tmp_path, VALID))

    assert config is not None and config.pre_ready is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"command": "", "timeout_seconds": 60}, "review.pre_ready.command must be a non-empty string"),
        ({"command": "  ", "timeout_seconds": 60}, "review.pre_ready.command must be a non-empty string"),
        ({"timeout_seconds": 60}, "review.pre_ready.command must be a non-empty string"),
        ({"command": "make", "timeout_seconds": 901}, "review.pre_ready.timeout_seconds must be an integer from 1 to 900"),
        ({"command": "make", "timeout_seconds": 0}, "review.pre_ready.timeout_seconds must be an integer from 1 to 900"),
        ({"command": "make", "timeout_seconds": True}, "review.pre_ready.timeout_seconds must be an integer from 1 to 900"),
        ({"command": "make", "timeout_seconds": "60"}, "review.pre_ready.timeout_seconds must be an integer from 1 to 900"),
        ({"command": "make"}, "review.pre_ready.timeout_seconds must be an integer from 1 to 900"),
        ("make test", "review.pre_ready must be a mapping"),
        ({"command": "make", "timeout_seconds": 60, "env": {}}, "unknown field 'review.pre_ready.env'"),
    ],
)
def test_pre_ready_refuses_bad_values(tmp_path: Path, value: object, message: str) -> None:
    config, findings = load_factory_md(write_factory(tmp_path, with_pre_ready(value)))

    assert config is None
    assert message in [finding.message for finding in findings]


def test_check_config_refuses_a_pre_ready_timeout_over_900(tmp_path: Path) -> None:
    path = write_factory(tmp_path, with_pre_ready({"command": "make", "timeout_seconds": 901}))
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "orch"), "check-config", "--factory", str(path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )

    assert result.returncode != 0
    assert "review.pre_ready.timeout_seconds must be an integer from 1 to 900" in (
        result.stdout + result.stderr
    )
