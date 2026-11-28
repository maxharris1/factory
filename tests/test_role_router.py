"""Tests for E2-4: central pstack benchmark-driven role router (`config/routing.yml`)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib import cli
from lib.factory_md import FactoryConfig, parse_factory_md, trust_front_matter_changed
from lib.orchestrate import RouteError, RouteRequest, choose_route
from lib.pstack_pin import ROOT
from lib.role_router import (
    BUDGETS,
    ROUTING_PATH,
    RoutingError,
    apply_argv_flags,
    family_for,
    load_routing_yml,
    parse_routing_yml,
    resolve_pair,
    resolve_slot,
    routing_argv_for_detail,
)
from tests.test_dispatcher import factory


def _factory_md(extra: str = "") -> str:
    return (
        "---\n"
        "base_branch: main\n"
        "tickets: github\n"
        "builder:\n"
        "  harness: claude\n"
        "reviewer:\n"
        "  harness: codex\n"
        "review:\n"
        "  rounds:\n"
        "    low: 1\n"
        "    high: 3\n"
        "  verify: none\n"
        "ship: merge\n"
        "skills: []\n"
        f"{extra}"
        "---\n"
    )


def test_shipped_config_routing_yml_loads_cleanly() -> None:
    cfg, findings = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None
    assert findings == []
    assert cfg.budget == "medium"
    for slot in (
        "orchestrate",
        "plan",
        "build",
        "build:feature",
        "build:bug",
        "build:refactor",
        "build:perf",
        "review",
        "qa",
        "helper:deep",
        "helper:standard",
        "helper:fast",
    ):
        assert slot in cfg.roles


def test_role_slot_selection_across_ticket_types() -> None:
    cfg, findings = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None and not findings

    feat = resolve_slot("build:feature", routing=cfg)
    perf = resolve_slot("build:perf", routing=cfg)
    orch = resolve_slot("orchestrate", routing=cfg)
    qa = resolve_slot("qa", routing=cfg)

    assert (feat.harness, feat.family, feat.effort) == ("claude", "anthropic", "medium")
    assert (perf.harness, perf.family, perf.effort) == ("codex", "openai", "high")
    assert (orch.harness, orch.family) == ("claude", "anthropic")
    assert (qa.harness, qa.effort) == ("claude", "medium")


@pytest.mark.parametrize(
    ("built_by", "excluded_family", "expected_harness", "expected_family"),
    [
        (("claude", "claude-opus-4-6"), "anthropic", "gemini", "google"),
        (("codex", "gpt-5.4"), "openai", "gemini", "google"),
        (("gemini", "gemini-2.5-pro"), "google", "codex", "openai"),
        (("cursor", "claude-sonnet-4-6"), "anthropic", "gemini", "google"),
        (("cursor", "gpt-5.4"), "openai", "gemini", "google"),
        (("cursor", "gemini-2.5-pro"), "google", "codex", "openai"),
    ],
)
def test_mechanical_cross_family_review_across_all_four_harnesses(
    built_by: tuple[str, str],
    excluded_family: str,
    expected_harness: str,
    expected_family: str,
) -> None:
    cfg, _ = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None
    rev = resolve_slot("review", routing=cfg, built_by=built_by)
    assert rev.family != excluded_family
    assert (rev.harness, rev.family) == (expected_harness, expected_family)
    if expected_harness == "codex":
        assert rev.model == "gpt-6.2-sol"


def test_cross_family_review_falls_through_to_google_and_fails_closed_when_exhausted() -> None:
    cfg, _ = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None
    # Builder is anthropic; openai harnesses (codex + cursor gpt-5.4) are refused -> picks gemini (google)!
    rev = resolve_slot(
        "review",
        routing=cfg,
        built_by=("claude", "claude-opus-4-6"),
        refused_harnesses=("codex", "cursor"),
    )
    assert (rev.harness, rev.family) == ("gemini", "google")

    # Refuse gemini too -> no cross-family reviewer remains -> fails closed!
    with pytest.raises(RoutingError, match="no eligible candidate for role 'review'"):
        resolve_slot(
            "review",
            routing=cfg,
            built_by=("claude", "claude-opus-4-6"),
            refused_harnesses=("codex", "cursor", "gemini"),
        )


def test_budget_ladder_overrides_and_effort_clamping() -> None:
    cfg, _ = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None

    unlim_rev = resolve_slot("review", routing=cfg, budget="unlimited", built_by=("claude", "claude-opus-4-6"))
    assert (unlim_rev.harness, unlim_rev.model, unlim_rev.effort) == ("codex", "gpt-6.2-sol", "xhigh")

    small_build = resolve_slot("build", routing=cfg, budget="small")
    assert small_build.model == "claude-sonnet-5-5"
    assert small_build.effort == "medium"

    # Clamping applies when a slot has no explicit budget_override at that tier.
    small_plan = resolve_slot("plan", routing=cfg, budget="small")
    assert small_plan.effort == "medium"

    # Explicit unlimited orchestrate uses max effort.
    unlim_orch = resolve_slot("orchestrate", routing=cfg, budget="unlimited")
    assert unlim_orch.effort == "max"


def test_refused_and_unavailable_harnesses_skip_in_pool_order() -> None:
    cfg, _ = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None
    # Claude refused -> build:feature picks codex; review automatically picks gemini (cross-family from openai!).
    builder, reviewer = resolve_pair(
        routing=cfg,
        ticket_type="feature",
        refused_harnesses=("claude",),
    )
    assert (builder.harness, builder.family) == ("codex", "openai")
    assert (reviewer.harness, reviewer.family) == ("gemini", "google")

    # Cursor also filtered when unavailable.
    builder2, reviewer2 = resolve_pair(
        routing=cfg,
        ticket_type="feature",
        refused_harnesses=("cursor",),
        unavailable_harnesses=("claude",),
    )
    assert builder2.harness == "codex"
    assert reviewer2.harness == "gemini"


def test_factory_md_override_precedence_and_legacy_fallback() -> None:
    cfg, _ = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None

    # 1. Per-repo override takes precedence over config/routing.yml.
    f_override, findings = parse_factory_md(
        _factory_md(
            "routing:\n"
            "  budget: small\n"
            "  slots:\n"
            "    build:feature:\n"
            "      - harness: codex\n"
            "        model: gpt-5.4\n"
            "        effort: medium\n"
            "    review: inherit\n"
        )
    )
    assert f_override is not None and findings == []
    b, r = resolve_pair(routing=cfg, factory=f_override, ticket_type="feature")
    assert (b.harness, b.model, b.source) == ("codex", "gpt-5.4", "factory-override")
    # review: inherit defers to routing.yml (small budget override), picking cross-family anthropic.
    assert (r.harness, r.family, r.source) == ("claude", "anthropic", "routing.yml")

    # 2. When config/routing.yml is absent or specifies inherit, falls back to legacy builder/reviewer.
    f_legacy = replace(factory(ship="merge"), builder_harness="claude", builder_model="claude-opus-4-6", reviewer_harness="codex", reviewer_model="gpt-5.4")
    b_leg, r_leg = resolve_pair(routing=None, factory=f_legacy)
    assert (b_leg.harness, b_leg.model, b_leg.source) == ("claude", "claude-opus-4-6", "factory-fallback")
    assert (r_leg.harness, r_leg.model, r_leg.source) == ("codex", "gpt-5.4", "factory-fallback")

    # 3. Trust guard trips when a PR alters factory.md routing section.
    assert trust_front_matter_changed(_factory_md(), _factory_md("routing:\n  budget: small\n")) is True


def test_orchestrate_choose_route_integrates_role_router() -> None:
    cfg, _ = load_routing_yml(ROOT / ROUTING_PATH)
    assert cfg is not None
    req = RouteRequest(
        config=factory(ship="merge"),
        risk="high",
        urgent=False,
        summary="optimize hot path",
        likely_files=("lib/foo.py",),
    )
    decision = choose_route(req, routing=cfg, ticket_type="perf", refused_harnesses=())
    assert (decision.builder_harness, decision.builder_family) == (
        "codex",
        "openai",
    )
    assert (decision.reviewer_harness, decision.reviewer_family) == (
        "gemini",
        "google",
    )
    assert decision.builder_family != decision.reviewer_family

    # Fails closed via RouteError when no cross-family reviewer is available.
    with pytest.raises(RouteError):
        choose_route(
            req,
            routing=cfg,
            ticket_type="perf",
            refused_harnesses=("claude", "gemini", "cursor"),
        )


def test_argv_flag_injection_and_safety_checks() -> None:
    claude_cmd = apply_argv_flags(
        ["claude", "-p", "--dangerously-skip-permissions"],
        harness="claude",
        model="claude-opus-4-6",
        effort="high",
    )
    assert claude_cmd == [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--model",
        "claude-opus-4-6",
        "--effort",
        "high",
    ]

    # Codex trailing '-' stdin sentinel stays at the end!
    codex_cmd = apply_argv_flags(
        ["codex", "exec", "--full-auto", "-"],
        harness="codex",
        model="gpt-5.4",
        effort="xhigh",
    )
    assert codex_cmd == [
        "codex",
        "exec",
        "--full-auto",
        "--model",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=xhigh",
        "-",
    ]

    replaced = apply_argv_flags(
        ["claude", "-p", "--model", "claude-sonnet-4-6"],
        harness="claude",
        model="claude-opus-4-6",
        effort=None,
    )
    assert replaced == ["claude", "-p", "--model", "claude-opus-4-6"]

    # Rejects untrusted / shell-injection model tokens.
    with pytest.raises(RoutingError, match="invalid model token"):
        apply_argv_flags(["claude", "-p"], harness="claude", model="bad; rm -rf /", effort=None)

    # routing_argv_for_detail injects for recognized harness CLIs and ignores test wrappers.
    detail = json.dumps(
        {
            "action": "review",
            "routing": {
                "builder": "claude",
                "reviewer": "codex",
                "reviewer_model": "gpt-5.4",
                "reviewer_effort": "high",
            },
        }
    )
    assert routing_argv_for_detail(["codex", "exec", "-"], harness="codex", detail=detail) == [
        "codex",
        "exec",
        "--model",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=high",
        "-",
    ]
    assert routing_argv_for_detail(["python3", "-c", "pass"], harness="codex", detail=detail) == [
        "python3",
        "-c",
        "pass",
    ]


def test_parse_routing_yml_rejects_bad_configs_and_check_config_validates(tmp_path: Path) -> None:
    bad_cfg, findings = parse_routing_yml(
        "budget: huge\n"
        "roles:\n"
        "  review:\n"
        "    - harness: claude\n"
        "      model: gpt-5.4\n"
        "      effort: turbo\n"
    )
    assert bad_cfg is None
    msgs = [f.message for f in findings]
    assert any("budget must be one of" in m for m in msgs)
    assert any("effort must be one of" in m for m in msgs)

    # Contradicting model family on valid effort is refused.
    bad_fam, fam_findings = parse_routing_yml(
        "budget: medium\n"
        "roles:\n"
        "  review:\n"
        "    - harness: claude\n"
        "      model: gpt-5.4\n"
        "      effort: high\n"
    )
    assert bad_fam is None
    assert any("contradicts harness 'claude'" in f.message for f in fam_findings)

    # check-config passes on shipped config/routing.yml and fails on a bad --routing file.
    bad_file = tmp_path / "bad-routing.yml"
    bad_file.write_text("budget: bogus\nroles: {}\n", encoding="utf-8")
    assert cli.main(["check-config", "--allow-placeholders", "--routing", str(ROOT / ROUTING_PATH)]) == 0
    assert cli.main(["check-config", "--allow-placeholders", "--routing", str(bad_file)]) == 1


_ROLES_BLOCK = "roles:\n  build:\n    - harness: claude\n"


def test_shares_values_reach_the_routing_config() -> None:
    cfg, findings = parse_routing_yml(
        _ROLES_BLOCK
        + "shares:\n  gemini:\n    build: 0.25\n    review: 1\n"
        "    min_headroom: 0.3\n    max_builds_in_flight: 4\n"
    )
    assert findings == []
    assert cfg is not None
    assert cfg.share("gemini", "build") == 0.25
    assert cfg.share("gemini", "review") == 1
    assert cfg.shares["gemini"].min_headroom == 0.3
    assert cfg.shares["gemini"].max_builds_in_flight == 4


def test_a_gemini_block_with_only_build_takes_the_defaults() -> None:
    cfg, findings = parse_routing_yml(_ROLES_BLOCK + "shares:\n  gemini:\n    build: 0.5\n")
    assert findings == []
    assert cfg is not None
    assert cfg.share("gemini", "build") == 0.5
    assert cfg.share("gemini", "review") == 0.0
    assert cfg.shares["gemini"].min_headroom == 0.15
    assert cfg.shares["gemini"].max_builds_in_flight == 2


@pytest.mark.parametrize(
    ("shares", "message"),
    [
        ("shares:\n  gemini:\n    build: -0.1\n", "shares.gemini.build must be a number from 0 to 1"),
        ("shares:\n  gemini:\n    review: 1.5\n", "shares.gemini.review must be a number from 0 to 1"),
        ("shares:\n  gemini:\n    min_headroom: 2\n", "shares.gemini.min_headroom must be a number from 0 to 1"),
        ("shares:\n  gemini:\n    build: .nan\n", "shares.gemini.build must be a number from 0 to 1"),
        ("shares:\n  gemini:\n    build: true\n", "shares.gemini.build must be a number from 0 to 1"),
        ("shares:\n  gemini:\n    build: lots\n", "shares.gemini.build must be a number from 0 to 1"),
        ("shares:\n  gemini:\n    foo: 0.1\n", "unknown field shares.gemini.'foo'"),
        ("shares:\n  codex:\n    build: 0.1\n", "shares.'codex' must be one of gemini"),
        ("shares: 0.5\n", "shares must be a mapping"),
        ("shares:\n  gemini: 0.5\n", "shares.gemini must be a mapping"),
        (
            "shares:\n  gemini:\n    max_builds_in_flight: 2.0\n",
            "shares.gemini.max_builds_in_flight must be a whole number of at least 0",
        ),
        (
            "shares:\n  gemini:\n    max_builds_in_flight: -1\n",
            "shares.gemini.max_builds_in_flight must be a whole number of at least 0",
        ),
        (
            "shares:\n  gemini:\n    max_builds_in_flight: true\n",
            "shares.gemini.max_builds_in_flight must be a whole number of at least 0",
        ),
    ],
)
def test_an_invalid_shares_block_is_refused(shares: str, message: str) -> None:
    cfg, findings = parse_routing_yml(_ROLES_BLOCK + shares)
    assert cfg is None
    assert [f.message for f in findings if f.severity == "error"] == [message]


@pytest.mark.parametrize(
    "shares",
    ["", "shares:\n", "shares:\n  gemini:\n    build: 0\n    review: 0\n"],
)
def test_a_zero_or_absent_share_means_no_draw(shares: str) -> None:
    cfg, findings = parse_routing_yml(_ROLES_BLOCK + shares)
    assert findings == []
    assert cfg is not None
    assert cfg.share("gemini", "build") == 0.0
    assert cfg.share("gemini", "review") == 0.0


def _build_detail(harness: str, model: str, effort: str | None = None) -> str:
    routing = {"builder": harness, "builder_model": model}
    if effort is not None:
        routing["builder_effort"] = effort
    return json.dumps({"action": "build", "routing": routing})


def _plan_detail(
    harness: str, model: str, effort: str, plan_model: str | None, plan_effort: str | None
) -> str:
    routing = json.loads(_build_detail(harness, model, effort))["routing"]
    routing["planner"] = harness
    if plan_model is not None:
        routing["plan_model"] = plan_model
    if plan_effort is not None:
        routing["plan_effort"] = plan_effort
    return json.dumps({"action": "plan", "routing": routing})


@pytest.mark.parametrize(
    ("harness", "command", "model", "expected"),
    [
        (
            "cursor",
            ["cursor-agent", "-p", "-"],
            "gpt-5.4",
            ["cursor-agent", "-p", "--model", "gpt-5.4", "-"],
        ),
        (
            "cursor",
            ["/usr/local/bin/agent", "-p", "-"],
            "gpt-5.4",
            ["/usr/local/bin/agent", "-p", "--model", "gpt-5.4", "-"],
        ),
    ],
)
def test_routed_model_reaches_cursor_argv_without_an_effort_flag(
    harness: str, command: list[str], model: str, expected: list[str]
) -> None:
    detail = _build_detail(harness, model, effort="high")
    assert routing_argv_for_detail(command, harness=harness, detail=detail) == expected


def _shipped_waiter_command(tmp_path: Path, name: str, bot: str) -> list[str]:
    from lib import waiter

    folder = tmp_path / "config"
    folder.mkdir(mode=0o700)
    path = folder / f"waiter-{bot}.yml"
    path.write_text((ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    path.chmod(0o600)
    return list(waiter.load_harness_command(path, bot).command)


@pytest.mark.parametrize(
    ("harness", "binary"),
    [
        ("claude", "claude"),
        ("claude", "claude-harness"),
        ("codex", "codex"),
        ("codex", "codex-review-harness"),
        ("cursor", "cursor-agent"),
        ("cursor", "agent"),
        ("gemini", "agy"),
    ],
)
def test_routing_recognizes_every_binary_its_harness_driver_runs(harness: str, binary: str) -> None:
    from lib.harnesses import get

    driver = get(harness)
    assert driver.matches_binary(binary) or binary in driver.wrapper_binaries
    detail = _build_detail(harness, "model-x")
    for path in (binary, f"/opt/bin/{binary}"):
        routed = routing_argv_for_detail([path, "--flag"], harness=harness, detail=detail)
        assert routed[0] == path and "model-x" in routed, routed


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor", "gemini"])
def test_routing_leaves_a_binary_its_harness_driver_does_not_run_alone(harness: str) -> None:
    from lib.harnesses import all_drivers, get

    detail = _build_detail(harness, "model-x", effort="high")
    others = {"claude", "codex", "cursor-agent", "agent", "agy", "gemini", "claude-harness", "codex-review-harness"}
    for binary in sorted(others):
        if get(harness).matches_binary(binary) or binary in get(harness).wrapper_binaries:
            continue
        assert routing_argv_for_detail([binary, "-p"], harness=harness, detail=detail) == [binary, "-p"]
    assert {driver.name for driver in all_drivers()} == {"claude", "codex", "cursor", "gemini"}


@pytest.mark.parametrize(
    ("harness", "binaries", "pins"),
    [
        ("claude", ["claude", "claude-harness"], ["--model"]),
        ("codex", ["codex", "codex-review-harness"], ["--model", "-m"]),
        ("gemini", ["agy"], ["--model"]),
        ("cursor", ["cursor-agent", "agent"], ["--model"]),
    ],
)
def test_every_binary_and_wrapper_a_harness_runs_gets_the_routed_model(
    harness: str, binaries: list[str], pins: list[str]
) -> None:
    from lib.harnesses import get

    driver = get(harness)
    assert set(binaries) == {b for b in binaries if driver.matches_binary(b)} | driver.wrapper_binaries
    detail = _build_detail(harness, "model-new")
    for binary in binaries:
        assert routing_argv_for_detail([binary, "-p"], harness=harness, detail=detail) == [
            binary, "-p", "--model", "model-new",
        ], binary
        for pin in pins:
            assert routing_argv_for_detail([binary, pin, "old", "-p"], harness=harness, detail=detail) == [
                binary, "-p", "--model", "model-new",
            ], (binary, pin)


@pytest.mark.parametrize("binary", ["claude-harness", "/home/box/.local/bin/claude-harness"])
def test_a_plan_job_through_claude_harness_gets_the_routed_plan_pick(binary: str) -> None:
    detail = _plan_detail("claude", "claude-opus-5-5", "medium", "claude-fable-5-2", "high")
    command = [binary, "-p", "--model", "claude-opus-5-5", "--effort", "medium"]
    assert routing_argv_for_detail(command, harness="claude", detail=detail) == [
        binary, "-p", "--model", "claude-fable-5-2", "--effort", "high",
    ]


@pytest.mark.parametrize("pinned", [["-m", "gpt-5.5"], ["--model", "gpt-5.5"]])
def test_a_review_job_through_codex_review_harness_gets_the_routed_reviewer_pick(pinned: list[str]) -> None:
    detail = json.dumps({
        "action": "review",
        "routing": {"builder": "claude", "reviewer": "codex", "reviewer_model": "gpt-6.2-sol", "reviewer_effort": "high"},
    })
    command = ["codex-review-harness", "exec", *pinned, "-"]
    assert routing_argv_for_detail(command, harness="codex", detail=detail) == [
        "codex-review-harness", "exec", "--model", "gpt-6.2-sol", "-c", "model_reasoning_effort=high", "-",
    ]


@pytest.mark.parametrize("action", ["build", "fix"])
@pytest.mark.parametrize("pinned", [[], ["--model", "claude-fable-5-2"]])
def test_build_and_fix_jobs_through_claude_harness_get_the_routed_build_pick(
    action: str, pinned: list[str]
) -> None:
    routing = json.loads(_build_detail("claude", "claude-opus-5-5", "medium"))["routing"]
    detail = json.dumps({"action": action, "routing": routing})
    assert routing_argv_for_detail(["claude-harness", "-p", *pinned], harness="claude", detail=detail) == [
        "claude-harness", "-p", "--model", "claude-opus-5-5", "--effort", "medium",
    ]


def test_the_shipped_gemini_waiter_command_gets_the_routed_model_and_effort(tmp_path: Path) -> None:
    command = _shipped_waiter_command(tmp_path, "waiter-gemini-bot.example.yml", "gemini-bot")
    detail = json.dumps({
        "action": "build",
        "routing": {"builder": "gemini", "builder_model": "gemini-4-neon", "builder_effort": "high"},
    })
    assert routing_argv_for_detail(command, harness="gemini", detail=detail) == [
        *command, "--model", "gemini-4-neon", "--effort", "high",
    ]


@pytest.mark.parametrize("prompt_option", ["-p", "--print", "--prompt"])
@pytest.mark.parametrize("binary", ["agy", "/usr/local/bin/agy"])
def test_routed_agy_flags_never_split_a_prompt_option_from_its_stdin_value(
    prompt_option: str, binary: str
) -> None:
    detail = _build_detail("gemini", "gemini-4-neon", effort="high")
    assert routing_argv_for_detail([binary, prompt_option, "-"], harness="gemini", detail=detail) == [
        binary, "--model", "gemini-4-neon", "--effort", "high", prompt_option, "-",
    ]
    assert apply_argv_flags(
        [binary, prompt_option, "-"], harness="gemini", model="gemini-4-neon", effort=None
    ) == [binary, "--model", "gemini-4-neon", prompt_option, "-"]


@pytest.mark.parametrize(
    "pinned",
    [["--model", "gemini-3.1-pro"], ["-m", "gemini-3.1-pro"], ["--model=gemini-3.1-pro"],
     ["--effort", "low"], ["--effort=low"]],
)
def test_a_waiter_pinned_agy_model_or_effort_yields_to_the_routed_one(pinned: list[str]) -> None:
    detail = _build_detail("gemini", "gemini-4-neon", effort="high")
    command = ["agy", *pinned, "-p", "go", "--dangerously-skip-permissions"]
    assert routing_argv_for_detail(command, harness="gemini", detail=detail) == [
        "agy", "-p", "go", "--dangerously-skip-permissions",
        "--model", "gemini-4-neon", "--effort", "high",
    ]


def test_codex_prompt_mentioning_reasoning_effort_still_gets_routed_effort() -> None:
    detail = _build_detail("codex", "gpt-5.4", effort="high")
    assert routing_argv_for_detail(
        ["codex", "exec", "explain model_reasoning_effort"], harness="codex", detail=detail
    ) == [
        "codex",
        "exec",
        "explain model_reasoning_effort",
        "--model",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=high",
    ]


def test_model_after_the_option_terminator_is_prompt_text_not_configuration() -> None:
    detail = _build_detail("codex", "gpt-5.4")
    assert routing_argv_for_detail(["codex", "exec", "--", "--model"], harness="codex", detail=detail) == [
        "codex",
        "exec",
        "--model",
        "gpt-5.4",
        "--",
        "--model",
    ]


@pytest.mark.parametrize(
    "config",
    [["-c", "model_reasoning_effort=low"], ["--config", "model_reasoning_effort=low"], ["--config=model_reasoning_effort=low"]],
)
def test_a_pinned_codex_reasoning_effort_yields_to_the_routed_one(config: list[str]) -> None:
    detail = _build_detail("codex", "gpt-5.4", effort="high")
    assert routing_argv_for_detail(
        ["codex", "exec", "-c", "sandbox_mode=workspace-write", *config, "--", "go"], harness="codex", detail=detail
    ) == [
        "codex", "exec", "-c", "sandbox_mode=workspace-write",
        "--model", "gpt-5.4", "-c", "model_reasoning_effort=high", "--", "go",
    ]


def test_a_pinned_codex_effort_stays_when_the_route_names_none() -> None:
    detail = _build_detail("codex", "gpt-5.4")
    assert routing_argv_for_detail(
        ["codex", "exec", "-c", "model_reasoning_effort=low", "-"], harness="codex", detail=detail
    ) == ["codex", "exec", "-c", "model_reasoning_effort=low", "--model", "gpt-5.4", "-"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            ["claude", "-p", "--effort", "medium"],
            ["claude", "-p", "--model", "claude-fable-5-2", "--effort", "high"],
        ),
        (
            ["claude", "-p", "--effort=medium", "--model", "claude-opus-5-5"],
            ["claude", "-p", "--model", "claude-fable-5-2", "--effort", "high"],
        ),
        (
            ["claude", "-m", "claude-opus-5-5", "-p", "--dangerously-skip-permissions", "-"],
            ["claude", "-p", "--dangerously-skip-permissions", "--model", "claude-fable-5-2", "--effort", "high", "-"],
        ),
        (
            ["claude", "-p", "--", "--effort"],
            ["claude", "-p", "--model", "claude-fable-5-2", "--effort", "high", "--", "--effort"],
        ),
    ],
)
def test_a_waiter_pinned_claude_model_or_effort_yields_to_the_routed_plan(
    command: list[str], expected: list[str]
) -> None:
    detail = _plan_detail("claude", "claude-opus-5-5", "medium", "claude-fable-5-2", "high")
    assert routing_argv_for_detail(command, harness="claude", detail=detail) == expected


def test_a_plan_job_gets_the_plan_pick_and_a_build_job_the_build_pick() -> None:
    routing = json.loads(_plan_detail("claude", "claude-opus-5-5", "medium", "claude-fable-5-2", "high"))["routing"]
    for action, model, effort in (
        ("plan", "claude-fable-5-2", "high"),
        ("build", "claude-opus-5-5", "medium"),
        ("fix", "claude-opus-5-5", "medium"),
    ):
        detail = json.dumps({"action": action, "routing": routing})
        assert routing_argv_for_detail(["claude", "-p"], harness="claude", detail=detail) == [
            "claude", "-p", "--model", model, "--effort", effort,
        ], action


def test_a_plan_job_with_no_plan_pick_runs_on_the_build_pick() -> None:
    detail = json.dumps({"action": "plan", "routing": json.loads(_build_detail("claude", "claude-opus-5-5", "medium"))["routing"]})
    assert routing_argv_for_detail(["claude", "-p"], harness="claude", detail=detail) == [
        "claude", "-p", "--model", "claude-opus-5-5", "--effort", "medium",
    ]


@pytest.mark.parametrize(
    ("plan_model", "plan_effort", "build_effort", "waiter", "expected"),
    [
        (
            None,
            "high",
            "medium",
            ["claude", "-p", "--model", "claude-fable-5-2", "--effort", "high"],
            ["claude", "-p", "--model", "claude-fable-5-2", "--effort", "high"],
        ),
        (
            "claude-fable-5-2",
            None,
            "low",
            ["claude", "-p", "--effort", "medium"],
            ["claude", "-p", "--effort", "medium", "--model", "claude-fable-5-2"],
        ),
    ],
)
def test_a_plan_pick_leaves_the_fields_it_omits_to_the_waiter_not_the_build_pick(
    plan_model: str | None,
    plan_effort: str | None,
    build_effort: str,
    waiter: list[str],
    expected: list[str],
) -> None:
    detail = _plan_detail("claude", "claude-opus-5-5", build_effort, plan_model, plan_effort)
    assert routing_argv_for_detail(waiter, harness="claude", detail=detail) == expected


def test_non_harness_command_is_left_unchanged() -> None:
    detail = _build_detail("claude", "claude-opus-4-6", effort="high")
    assert routing_argv_for_detail(["bash", "run.sh"], harness="claude", detail=detail) == ["bash", "run.sh"]
