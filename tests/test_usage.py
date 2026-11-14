"""F1-08: best-fit routing and the usage reserve.

Fixtures stand in for harness usage. Nothing here reads an account.
"""

import ast
from pathlib import Path

import pytest
import yaml

from lib.factory_md import EXAMPLE_PATHS, FactoryConfig, load_factory_md
from lib.usage import (
    RESERVE_REMAINING,
    FitRow,
    ReserveCheck,
    StaticUsage,
    UsageReport,
    fit_table,
    job_is_urgent,
    reserve_check,
    route_job,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "usage"


def config(**overrides: object) -> FactoryConfig:
    values: dict[str, object] = {
        "base_branch": "main",
        "tickets": "github",
        "builder_harness": "claude",
        "reviewer_harness": "codex",
        "rounds_low": 1,
        "rounds_high": 3,
        "verify": "none",
        "ship": "merge",
        "skills": (),
    }
    values.update(overrides)
    return FactoryConfig(**values)  # type: ignore[arg-type]


def fixture(name: str) -> UsageReport:
    data = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    return UsageReport(
        harness=data["harness"],
        remaining=data["remaining"],
        verified=data["verified"],
    )


def usage(*reports: UsageReport) -> StaticUsage:
    return StaticUsage({report.harness: report for report in reports})


def test_reserve_is_the_last_20_percent() -> None:
    assert RESERVE_REMAINING == 0.20


def test_fit_table_ranks_the_preferred_harness_first() -> None:
    table = fit_table(
        config(builder_harness="codex", builder_model="gpt-5"),
        "builder",
    )
    assert [(row.rank, row.harness, row.model, row.family) for row in table] == [
        (0, "codex", "gpt-5", "openai"),
        (1, "claude", None, "anthropic"),
        (2, "cursor", None, None),
    ]
    assert all(isinstance(row, FitRow) for row in table)


def test_reviewer_fit_uses_the_reviewer_hint() -> None:
    table = fit_table(
        config(reviewer_harness="cursor", reviewer_model="claude-sonnet"),
        "reviewer",
    )
    assert [(row.harness, row.model, row.family) for row in table] == [
        ("cursor", "claude-sonnet", "anthropic"),
        ("claude", None, "anthropic"),
        ("codex", None, "openai"),
    ]


def test_examples_route_best_fit_and_stay_cross_family() -> None:
    for relative in EXAMPLE_PATHS:
        loaded, findings = load_factory_md(ROOT / relative)
        assert findings == []
        assert loaded is not None
        route = route_job(loaded)
        assert route.ok
        assert route.reason is None
        assert route.builder is not None and route.reviewer is not None
        assert route.builder.harness == loaded.builder_harness
        assert route.builder.model == loaded.builder_model
        assert route.reviewer.harness == loaded.reviewer_harness
        assert route.reviewer.model == loaded.reviewer_model
        assert route.builder.family != route.reviewer.family
        assert route.builder.using_reserve is False
        assert route.reviewer.using_reserve is False


def test_missing_usage_source_does_not_block() -> None:
    route = route_job(config())
    assert route.ok
    assert route.builder is not None and route.builder.harness == "claude"
    assert route.reviewer is not None and route.reviewer.harness == "codex"


def test_unverified_report_stays_on_best_fit() -> None:
    route = route_job(
        config(reviewer_harness="cursor", reviewer_model="gpt-5"),
        usage=usage(fixture("cursor-unverified.yml")),
    )
    assert route.ok
    assert route.reviewer is not None
    assert route.reviewer.harness == "cursor"
    assert route.reviewer.model == "gpt-5"
    assert route.reviewer.using_reserve is False


def test_above_the_reserve_does_not_change_fit() -> None:
    route = route_job(
        config(reviewer_harness="cursor", reviewer_model="gpt-5"),
        usage=usage(
            fixture("cursor-above.yml"),
            UsageReport("codex", 0.99, True),
        ),
    )
    assert route.ok
    assert route.reviewer is not None
    assert route.reviewer.harness == "cursor"
    assert route.reviewer.using_reserve is False


def test_non_urgent_reviewer_reroutes_to_the_next_fit() -> None:
    route = route_job(
        config(reviewer_harness="cursor", reviewer_model="gpt-5"),
        usage=usage(fixture("cursor-reserve.yml")),
    )
    assert route.ok
    assert route.builder is not None
    assert route.builder.harness == "claude"
    assert route.builder.family == "anthropic"
    assert route.reviewer is not None
    assert route.reviewer.harness == "codex"
    assert route.reviewer.model is None
    assert route.reviewer.family == "openai"
    assert route.reviewer.using_reserve is False
    assert route.builder.family != route.reviewer.family


def test_reroute_follows_fit_order_not_remaining_quota() -> None:
    route = route_job(
        config(
            builder_harness="cursor",
            builder_model="claude-sonnet",
            reviewer_harness="codex",
        ),
        usage=usage(
            UsageReport("cursor", 0.0, True),
            UsageReport("claude", 0.25, True),
            UsageReport("codex", 0.99, True),
        ),
    )
    assert route.ok
    assert route.builder is not None
    assert route.builder.harness == "claude"
    assert route.builder.model is None
    assert route.builder.family == "anthropic"
    assert route.builder.using_reserve is False
    assert route.reviewer is not None
    assert route.reviewer.harness == "codex"
    assert route.reviewer.family == "openai"


def test_non_urgent_pair_does_not_use_the_reserve() -> None:
    route = route_job(config(), usage=usage(fixture("claude-reserve.yml")))
    assert route.ok is False
    assert route.builder is None
    assert route.reviewer is None
    assert route.reason == "reserve"


def test_non_urgent_reviewer_is_not_kept_when_nothing_else_fits() -> None:
    route = route_job(config(), usage=usage(UsageReport("codex", 0.2, True)))
    assert route.ok is False
    assert route.builder is None
    assert route.reviewer is None
    assert route.reason == "reserve"


@pytest.mark.parametrize(
    "flag",
    ["resumed_paused_step", "past_ship", "marked_urgent"],
)
def test_urgent_job_keeps_best_fit_inside_the_reserve(flag: str) -> None:
    route = route_job(
        config(),
        usage=usage(fixture("claude-reserve.yml")),
        **{flag: True},
    )
    assert route.ok
    assert route.builder is not None
    assert route.builder.harness == "claude"
    assert route.builder.model is None
    assert route.builder.using_reserve is True
    assert route.reviewer is not None
    assert route.reviewer.harness == "codex"
    assert route.reviewer.using_reserve is False
    assert route.builder.family != route.reviewer.family


def test_urgent_reviewer_may_stay_on_a_reserved_harness() -> None:
    route = route_job(
        config(reviewer_harness="cursor", reviewer_model="gpt-5"),
        usage=usage(fixture("cursor-reserve.yml")),
        marked_urgent=True,
    )
    assert route.ok
    assert route.reviewer is not None
    assert route.reviewer.harness == "cursor"
    assert route.reviewer.model == "gpt-5"
    assert route.reviewer.using_reserve is True


def test_cursor_without_a_family_is_not_a_route() -> None:
    route = route_job(
        config(),
        usage=usage(
            UsageReport("claude", 0.0, True),
            UsageReport("codex", 0.0, True),
        ),
    )
    assert route.ok is False
    assert route.reason == "reserve"
    assert route.builder is None
    assert route.reviewer is None


def test_owning_bot_reroutes_a_non_urgent_reserve() -> None:
    check = reserve_check("claude", usage(fixture("claude-reserve.yml")))
    assert check == ReserveCheck("claude", start=False, reserve=True, reason="reroute")


def test_owning_bot_allows_work_above_the_reserve() -> None:
    check = reserve_check("claude", usage(fixture("claude-above.yml")))
    assert check.start is True
    assert check.reserve is False
    assert check.reason == "above-reserve"


@pytest.mark.parametrize(
    "flag",
    ["resumed_paused_step", "past_ship", "marked_urgent"],
)
def test_owning_bot_allows_an_urgent_reserve(flag: str) -> None:
    check = reserve_check(
        "claude",
        usage(fixture("claude-reserve.yml")),
        **{flag: True},
    )
    assert check.start is True
    assert check.reserve is True
    assert check.reason == "urgent"


def test_owning_bot_missing_source_does_not_block() -> None:
    assert reserve_check("codex") == ReserveCheck(
        "codex",
        start=True,
        reserve=False,
        reason="no-verified-source",
    )
    unverified = reserve_check("cursor", usage(fixture("cursor-unverified.yml")))
    assert unverified.start is True
    assert unverified.reason == "no-verified-source"


def test_owning_bot_ignores_a_report_for_another_harness() -> None:
    class Other:
        def read(self, harness: str) -> UsageReport | None:
            return fixture("claude-reserve.yml")

    check = reserve_check("codex", Other())
    assert check.start is True
    assert check.reason == "no-verified-source"


def test_job_is_urgent_only_for_the_three_spec_cases() -> None:
    assert job_is_urgent() is False
    assert job_is_urgent(resumed_paused_step=True) is True
    assert job_is_urgent(past_ship=True) is True
    assert job_is_urgent(marked_urgent=True) is True


def test_usage_report_rejects_a_bad_fraction() -> None:
    with pytest.raises(ValueError):
        UsageReport("claude", 1.1, True)
    with pytest.raises(ValueError):
        UsageReport("claude", True, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        UsageReport("claude", float("nan"), True)


def test_reserve_check_rejects_an_unknown_harness() -> None:
    with pytest.raises(ValueError):
        reserve_check("grok")


def test_fit_table_rejects_an_unknown_role() -> None:
    with pytest.raises(ValueError):
        fit_table(config(), "judge")  # type: ignore[arg-type]


def test_usage_module_does_not_touch_credentials() -> None:
    text = (ROOT / "lib" / "usage.py").read_text(encoding="utf-8")
    for token in (".env", "auth.json", ".runner", "GH_TOKEN", "credentials"):
        assert token not in text


@pytest.mark.parametrize(
    ("role", "overrides"),
    [
        ("builder", {"builder_harness": "grok"}),
        ("reviewer", {"reviewer_harness": "grok"}),
        ("builder", {"builder_harness": "cursor"}),
        ("reviewer", {"reviewer_harness": "cursor"}),
        ("reviewer", {"reviewer_harness": "cursor", "reviewer_model": "gemini-3"}),
    ],
)
def test_route_refuses_a_preference_check_config_refuses(
    role: str, overrides: dict[str, object]
) -> None:
    bad = config(**overrides)
    with pytest.raises(ValueError):
        fit_table(bad, role)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        route_job(bad)


def test_lib_modules_import_no_private_factory_md_names() -> None:
    for relative in ("lib/usage.py", "lib/orchestrate.py"):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "lib.factory_md"
            for alias in node.names
        ]
        assert imported
        assert [name for name in imported if name.startswith("_")] == [], relative


def test_later_round_reviewer_excludes_the_family_that_built() -> None:
    route = route_job(config(), built_by=("cursor", "gpt-5"))
    assert route.ok
    assert route.builder is not None and route.reviewer is not None
    assert (route.builder.harness, route.builder.model, route.builder.family) == (
        "cursor",
        "gpt-5",
        "openai",
    )
    assert route.reviewer.harness == "claude"
    assert route.reviewer.family == "anthropic"
    same = route_job(config(), built_by=("claude", "claude-opus"))
    assert same.reviewer is not None and same.reviewer.harness == "codex"
    blocked = route_job(
        config(),
        usage=usage(UsageReport("claude", 0.0, True)),
        built_by=("cursor", "gpt-5"),
    )
    assert blocked.ok is False
    assert blocked.reason == "reserve"
    for built_by in (("grok", None), ("cursor", None), ("cursor", "gemini-3")):
        with pytest.raises(ValueError):
            route_job(config(), built_by=built_by)
