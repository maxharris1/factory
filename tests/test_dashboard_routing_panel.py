import builtins
import json
import os
import re
from pathlib import Path

import pytest

from lib import dashboard, exposure
from lib.dashboard import render_pages
from lib.role_router import (
    BUDGETS, KNOWN_HARNESSES, ROLE_SLOTS, ROUTING_PATH, WAITER_BOTS, RoutingError, parse_routing_yml, resolve_slot,
    role_matrix, routing_argv_for_detail,
)
from lib.pstack_pin import ROOT
from lib.wait import OWNING_BOTS
from tests.test_dashboard import snapshot_for
from tests.test_dashboard_efficiency import _inner
from tests.test_dashboard_layout import _run_page, _text, needs_node

ROUTING = """\
budget: medium
roles:
  build:
    - harness: claude
      model: claude-opus-4-6
      effort: max
    - harness: codex
      model: gpt-5.4
      effort: low
    - harness: gemini
      model: gemini-2.5-pro
  review:
    - harness: codex
      model: gpt-5.4
      effort: high
  plan:
    - harness: claude
      model: claude-sonnet-4-6
  qa: inherit
budget_overrides:
  small:
    build:
      - harness: claude
        model: claude-sonnet-4-6
        effort: high
    review: inherit
"""


CLAUDE_WAITER = ("claude", "-p", "--effort", "medium")
GEMINI_WAITER = ("gemini", "--yolo")
WRAPPED_WAITER = ("env", "FOO=1", "claude", "-p", "--model", "claude-haiku-4-5")


def _matrix(tmp_path: Path, text: str = ROUTING, waiters: dict | None = None) -> dict:
    path = tmp_path / "routing.yml"
    path.write_text(text, encoding="utf-8")
    return role_matrix(path, waiters)


def _routed(model: str | None, effort: str | None) -> dict:
    return {"model": model, "model_source": "unknown" if model is None else "routing.yml",
            "effort": effort, "effort_source": "unknown" if effort is None else "routing.yml",
            "replaced": {}, "applied": True}


def _effective(matrix: dict, role: str, rank: int) -> dict:
    return _row(matrix, role)["candidates"][rank - 1]["effective"]


def _row(matrix: dict, role: str) -> dict:
    return next(row for row in matrix["roles"] if row["role"] == role)


def test_matrix_clamps_effort_to_the_budget_ceiling_and_keeps_the_configured_value(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    assert (matrix["state"], matrix["budget"], matrix["ceiling"]) == ("ok", "medium", "high")
    assert _row(matrix, "build") == {"role": "build", "source": "roles", "candidates": [
        {"rank": 1, "harness": "claude", "model": "claude-opus-4-6", "family": "anthropic",
         "effort": "high", "configured": "max", "effective": _routed("claude-opus-4-6", "high")},
        {"rank": 2, "harness": "codex", "model": "gpt-5.4", "family": "openai", "effort": "low", "configured": "low",
         "effective": _routed("gpt-5.4", "low")},
        {"rank": 3, "harness": "gemini", "model": "gemini-2.5-pro", "family": "google",
         "effort": None, "configured": None, "effective": _routed("gemini-2.5-pro", None)},
    ]}


def test_matrix_falls_back_from_a_build_kind_to_build(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    assert _row(matrix, "build:feature")["candidates"] == _row(matrix, "build")["candidates"]
    assert _row(matrix, "build:feature")["source"] == "roles"


def test_matrix_takes_the_tier_override_unclamped(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))
    assert (matrix["budget"], matrix["ceiling"]) == ("small", "medium")
    for role in ("build", "build:perf"):
        assert _row(matrix, role) == {"role": role, "source": "budget_overrides", "candidates": [
            {"rank": 1, "harness": "claude", "model": "claude-sonnet-4-6", "family": "anthropic",
             "effort": "high", "configured": "high", "effective": _routed("claude-sonnet-4-6", "high")},
        ]}


def test_matrix_shows_inherit_and_unset_slots_with_no_candidates(tmp_path: Path) -> None:
    medium = _matrix(tmp_path)
    assert _row(medium, "qa") == {"role": "qa", "source": "inherit", "candidates": [], "fallback": []}
    assert _row(medium, "helper:panel") == {"role": "helper:panel", "source": "unset", "candidates": [], "fallback": []}
    small = _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))
    assert _row(small, "review") == {"role": "review", "source": "inherit", "candidates": [], "fallback": []}


def test_matrix_has_one_row_per_role_in_order(tmp_path: Path) -> None:
    assert [row["role"] for row in _matrix(tmp_path)["roles"]] == list(ROLE_SLOTS)


def test_matrix_of_a_missing_file_is_missing(tmp_path: Path) -> None:
    assert role_matrix(tmp_path / "routing.yml") == {"state": "missing", "roles": [], "waiters": {}}
    assert role_matrix(tmp_path / "routing.yml", {"claude": None})["waiters"] == {"claude": "missing"}


def test_matrix_of_a_broken_file_is_invalid_with_the_parser_errors(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, ROUTING.replace("harness: gemini", "harness: grok"))
    assert matrix["state"] == "invalid" and matrix["roles"] == [] and matrix["waiters"] == {}
    assert [e for e in matrix["errors"] if "roles.build[2].harness" in e], matrix["errors"]
    assert _matrix(tmp_path, "budget: [")["state"] == "invalid"


def test_a_routed_value_replaces_the_one_the_waiter_pins(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": CLAUDE_WAITER})
    assert _effective(matrix, "build", 1) == {
        "model": "claude-opus-4-6", "model_source": "routing.yml", "effort": "high", "effort_source": "routing.yml",
        "replaced": {"effort": "medium"}, "applied": True}
    assert matrix["waiters"] == {"claude": "ok"}


def test_an_unrouted_value_comes_from_the_waiter_or_the_cli(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": CLAUDE_WAITER, "gemini": GEMINI_WAITER})
    assert _effective(matrix, "build", 3) == {
        "model": "gemini-2.5-pro", "model_source": "routing.yml", "effort": None, "effort_source": "CLI default",
        "replaced": {}, "applied": True}
    assert _effective(matrix, "plan", 1) == {
        "model": "claude-sonnet-4-6", "model_source": "routing.yml",
        "effort": "medium", "effort_source": "waiter-claude-bot.yml", "replaced": {}, "applied": True}


def test_a_routed_gemini_effort_never_reaches_the_cli(tmp_path: Path) -> None:
    text = ROUTING.replace("      model: gemini-2.5-pro\n", "      model: gemini-2.5-pro\n      effort: high\n", 1)
    matrix = _matrix(tmp_path, text, {"gemini": GEMINI_WAITER})
    assert (_effective(matrix, "build", 3)["effort"], _effective(matrix, "build", 3)["effort_source"]) == (
        None, "CLI default")
    unseen = _matrix(tmp_path, text, {"gemini": None})
    assert _effective(unseen, "build", 3) == _routed("gemini-2.5-pro", None)
    assert _effective(_matrix(tmp_path, text), "build", 3) == _routed("gemini-2.5-pro", None)


def test_a_missing_waiter_leaves_only_what_routing_names(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": None})
    assert _effective(matrix, "build", 1) == _routed("claude-opus-4-6", "high")
    assert _effective(matrix, "plan", 1) == _routed("claude-sonnet-4-6", None)
    assert matrix["waiters"] == {"claude": "missing"}


def test_a_wrapped_waiter_gets_no_routing_flags(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude": WRAPPED_WAITER})
    assert _effective(matrix, "build", 1) == {
        "model": "claude-haiku-4-5", "model_source": "waiter-claude-bot.yml",
        "effort": None, "effort_source": "CLI default", "replaced": {}, "applied": False}
    same = _matrix(tmp_path, waiters={"claude": ("env", "FOO=1", "claude", "-p", "--model", "claude-opus-4-6")})
    assert _effective(same, "build", 1)["model_source"] == "waiter-claude-bot.yml"


def test_a_slot_with_no_candidates_falls_back_to_each_readable_waiter(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"gemini": GEMINI_WAITER, "codex": None, "claude": CLAUDE_WAITER})
    assert _row(matrix, "qa")["fallback"] == [
        {"harness": "claude", "model": None, "model_source": "CLI default",
         "effort": "medium", "effort_source": "waiter-claude-bot.yml"},
        {"harness": "gemini", "model": None, "model_source": "CLI default", "effort": None, "effort_source": "CLI default"},
    ]
    assert "fallback" not in _row(matrix, "build")
    assert matrix["waiters"] == {"gemini": "ok", "codex": "missing", "claude": "ok"}


def test_no_waiters_looked_at_means_routing_alone(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    assert matrix["waiters"] == {}
    for row in matrix["roles"]:
        for cand in row["candidates"]:
            assert cand["effective"] == _routed(cand["model"], cand["effort"]), row["role"]


def test_waiter_bots_are_the_owning_bots_inverted() -> None:
    assert dict(WAITER_BOTS) == {harness: bot for bot, harness in OWNING_BOTS.items()}
    assert tuple(WAITER_BOTS) == KNOWN_HARNESSES


def test_matrix_of_the_shipped_file_is_ok_for_every_role() -> None:
    matrix = role_matrix()
    assert matrix["state"] == "ok"
    assert [row["role"] for row in matrix["roles"]] == list(ROLE_SLOTS)


@pytest.mark.parametrize("budget", BUDGETS)
def test_matrix_first_choice_is_what_the_router_resolves(tmp_path: Path, budget: str) -> None:
    text = (ROOT / ROUTING_PATH).read_text(encoding="utf-8").replace("budget: medium", f"budget: {budget}")
    matrix = _matrix(tmp_path, text)
    config, _ = parse_routing_yml(text)
    for row in matrix["roles"]:
        if not row["candidates"]:
            with pytest.raises(RoutingError):
                resolve_slot(row["role"], routing=config)
            continue
        first = row["candidates"][0]
        slot = resolve_slot(row["role"], routing=config)
        assert (slot.harness, slot.model, slot.effort, slot.family) == (
            first["harness"], first["model"], first["effort"], first["family"]), row["role"]


BOTH = ["server", pytest.param("script", marks=needs_node)]


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return snapshot_for(tmp_path_factory.mktemp("site"))


def _with(base: dict, matrix: dict) -> dict:
    return {**base, "routing": matrix}


def _panel(tmp_path: Path, snapshot: dict, render: str) -> str:
    if render == "server":
        return _inner(render_pages(snapshot)["efficiency.html"], "routing")
    return _run_page(tmp_path, snapshot, "efficiency", ["routing"])["html"]["routing"]


def _role(panel: str, role: str) -> str:
    match = re.search(rf'<tr><th scope="row">{re.escape(role)}</th>(.*?)</tr>', panel)
    assert match, role
    return match.group(1)


def test_the_snapshot_carries_the_shipped_matrix(base: dict) -> None:
    assert base["routing"] == role_matrix()


def test_the_snapshot_carries_the_waiter_values_but_no_argv(tmp_path: Path) -> None:
    waiters = {"claude": CLAUDE_WAITER}
    snapshot = snapshot_for(tmp_path, waiters=waiters)
    assert snapshot["routing"] == role_matrix(waiters=waiters)
    assert snapshot["routing"]["waiters"] == {"claude": "ok"}
    dumped = json.dumps(snapshot)
    assert '"-p"' not in dumped and '"--effort"' not in dumped


def test_waiter_argv_reads_only_the_waiter_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / ".config" / "factory"
    config.mkdir(parents=True)
    config.chmod(0o700)
    waiter = config / "waiter-claude-bot.yml"
    waiter.write_text("bot: claude-bot\ncommand: [claude, -p]\n", encoding="utf-8")
    waiter.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))
    opened: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_os_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", spy_open)
        patch.setattr(os, "open", spy_os_open)
        found = dashboard.waiter_argv()
    assert found == {"claude": ("claude", "-p"), "codex": None, "cursor": None, "gemini": None}
    assert opened and all(re.search(r"/waiter-[a-z]+-bot\.yml$", path) for path in opened), opened


WAITER_CASES = {
    "routed": {"claude": CLAUDE_WAITER},
    "unrouted": {"claude": CLAUDE_WAITER, "gemini": GEMINI_WAITER},
    "missing-waiter": {"claude": None},
    "wrapped": {"claude": WRAPPED_WAITER},
    "two-missing": {"claude": CLAUDE_WAITER, "codex": None, "gemini": None},
}


@needs_node
@pytest.mark.parametrize(("text", "waiters"), [
    (ROUTING, None), (None, None), (ROUTING.replace("harness: gemini", "harness: grok"), None),
    *((ROUTING, waiters) for waiters in WAITER_CASES.values()),
], ids=["ok", "missing", "invalid", *WAITER_CASES])
def test_server_and_script_draw_the_same_panel(
    tmp_path: Path, base: dict, text: str | None, waiters: dict | None
) -> None:
    matrix = role_matrix(tmp_path / "absent.yml", waiters) if text is None else _matrix(tmp_path, text, waiters)
    snapshot = _with(base, matrix)
    assert _panel(tmp_path, snapshot, "script") == _panel(tmp_path, snapshot, "server")


@pytest.mark.parametrize("render", BOTH)
def test_panel_heads_with_the_budget_and_its_ceiling(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _matrix(tmp_path)), render)
    assert panel.startswith(
        '<h2><span>Role routing</span><span class="count">medium budget · effort up to high</span></h2>')


@pytest.mark.parametrize("render", BOTH)
def test_panel_marks_a_clamped_effort_with_its_configured_value(tmp_path: Path, base: dict, render: str) -> None:
    build = _role(_panel(tmp_path, _with(base, _matrix(tmp_path)), render), "build")
    assert '<span class="rt-e clamped">max → high</span>' in build
    assert '<span class="rt-e">low</span>' in build
    small = _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))
    assert '<span class="rt-e">high</span>' in _role(_panel(tmp_path, _with(base, small), render), "build")


@pytest.mark.parametrize("render", BOTH)
def test_panel_lists_candidates_in_preference_order(tmp_path: Path, base: dict, render: str) -> None:
    build = _role(_panel(tmp_path, _with(base, _matrix(tmp_path)), render), "build")
    assert '<ol class="rt-pool">' in build
    assert [_text(li) for li in re.findall(r"<li>(.*?)<div", build)] == [
        "claude · claude-opus-4-6 · max → high", "codex · gpt-5.4 · low", "gemini · gemini-2.5-pro"]


def _runs(panel: str, role: str) -> list[str]:
    return [_text(line) for line in re.findall(r'<div class="rt-runs">(.*?)</div>', _role(panel, role))]


@pytest.mark.parametrize("render", BOTH)
def test_each_candidate_says_what_runs_and_where_each_value_comes_from(
    tmp_path: Path, base: dict, render: str
) -> None:
    routed = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=WAITER_CASES["routed"])), render)
    assert '<span class="rt-v">high</span><span class="rt-src">routing.yml</span>' in _role(routed, "build")
    assert _runs(routed, "build")[0] == "runs claude-opus-4-6 routing.yml · high routing.yml waiter medium replaced"
    unrouted = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=WAITER_CASES["unrouted"])), render)
    assert '<span class="rt-v default">CLI default</span>' in _role(unrouted, "build")
    assert _runs(unrouted, "build")[2] == "runs gemini-2.5-pro routing.yml · CLI default"
    assert _runs(unrouted, "plan") == ["runs claude-sonnet-4-6 routing.yml · medium waiter-claude-bot.yml"]
    missing = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=WAITER_CASES["missing-waiter"])), render)
    assert _runs(missing, "plan") == ["runs claude-sonnet-4-6 routing.yml · unknown"]
    wrapped = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=WAITER_CASES["wrapped"])), render)
    assert _runs(wrapped, "build")[0] == (
        "runs claude-haiku-4-5 waiter-claude-bot.yml · CLI default routing.yml flags not added by this waiter")


@pytest.mark.parametrize("render", BOTH)
def test_an_empty_slot_lists_each_waiter_it_falls_back_to(tmp_path: Path, base: dict, render: str) -> None:
    waited = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=WAITER_CASES["unrouted"])), render)
    qa = _role(waited, "qa")
    assert '<ul class="rt-fallback">' in qa
    assert [_text(li) for li in re.findall(r"<li>(.*?)</li>", qa)] == [
        "claude: CLI default · medium waiter-claude-bot.yml", "gemini: CLI default · CLI default"]
    assert "rt-fallback" not in _panel(tmp_path, _with(base, _matrix(tmp_path)), render)


@pytest.mark.parametrize("render", BOTH)
def test_the_note_says_what_a_runs_line_means(tmp_path: Path, base: dict, render: str) -> None:
    assert ("Each runs line is the model and effort a new job gets once the bot&#x27;s waiter-&lt;bot&gt;.yml argv "
            "is applied; CLI default means the harness picks.") in _panel(tmp_path, _with(base, _matrix(tmp_path)), render)


@pytest.mark.parametrize("render", BOTH)
def test_panel_names_the_bots_whose_waiter_config_was_not_read(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=WAITER_CASES["two-missing"])), render)
    line = ('<p class="rt-waiters">waiter-&lt;bot&gt;.yml not read for codex-bot, gemini-bot; '
            "their unrouted values show unknown</p>")
    assert panel.count(line) == 1
    assert panel.index(line) < panel.index('<p class="rt-note">')
    for waiters in (None, {"claude": CLAUDE_WAITER}):
        assert "rt-waiters" not in _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=waiters)), render)


@pytest.mark.parametrize("render", BOTH)
def test_panel_names_the_source_and_the_fallback_of_an_empty_slot(tmp_path: Path, base: dict, render: str) -> None:
    small = _panel(tmp_path, _with(base, _matrix(tmp_path, ROUTING.replace("budget: medium", "budget: small"))), render)
    assert _role(small, "build").startswith("<td>budget override</td>")
    fallback = '<td><span class="muted">falls back to factory.md {}</span></td>'
    assert _role(small, "review") == "<td>inherit</td>" + fallback.format("reviewer")
    assert _role(small, "helper:panel") == "<td>not set</td>" + fallback.format("builder")
    medium = _panel(tmp_path, _with(base, _matrix(tmp_path)), render)
    assert _role(medium, "build").startswith("<td>roles</td>")


@pytest.mark.parametrize("render", BOTH)
def test_panel_says_the_file_is_missing(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, role_matrix(tmp_path / "absent.yml")), render)
    assert panel == '<h2><span>Role routing</span></h2><p class="empty">config/routing.yml not found</p>'


@pytest.mark.parametrize("render", BOTH)
def test_panel_lists_the_errors_of_a_broken_file_escaped(tmp_path: Path, base: dict, render: str) -> None:
    matrix = _matrix(tmp_path, "<script>x()</script>: 1\n" + ROUTING.replace("harness: gemini", "harness: grok"))
    panel = _panel(tmp_path, _with(base, matrix), render)
    assert panel.startswith('<h2><span>Role routing</span></h2><p class="empty">config/routing.yml is invalid:</p>')
    errors = re.findall(r"<li>(.*?)</li>", panel)
    assert "unknown field &#x27;&lt;script&gt;x()&lt;/script&gt;&#x27;" in errors
    assert [e for e in errors if e.startswith("roles.build[2].harness")]
    assert "<script>" not in panel


def test_panel_holds_no_control(tmp_path: Path, base: dict) -> None:
    for matrix in (_matrix(tmp_path), role_matrix(tmp_path / "absent.yml")):
        panel = _inner(render_pages(_with(base, matrix))["efficiency.html"], "routing")
        assert not re.search(r"<(form|button|input|select|textarea)\b", panel)


# --- what each role's launcher consumes (review round 1 of #1661) --------------------


PINNED_WAITER = ("claude", "-p", "--model", "claude-haiku-4-5", "--effort", "low")
OPAQUE_WAITER = ("env", "-S", "/opt/bin/claude -p --model sonnet --effort low")
INHERITED_BUILD = ROUTING.replace("""\
  build:
    - harness: claude
      model: claude-opus-4-6
      effort: max
    - harness: codex
      model: gpt-5.4
      effort: low
    - harness: gemini
      model: gemini-2.5-pro
""", "  build: inherit\n")
CODEX_ONLY_PLAN = ROUTING.replace("""\
  plan:
    - harness: claude
      model: claude-sonnet-4-6
""", """\
  plan:
    - harness: codex
      model: gpt-5.4
      effort: low
""")
UNKNOWN = {"model": None, "model_source": "unknown", "effort": None, "effort_source": "unknown"}


def _pinned(harness: str, bot: str, model: str, effort: str) -> dict:
    return {"harness": harness, "model": model, "model_source": f"waiter-{bot}.yml",
            "effort": effort, "effort_source": f"waiter-{bot}.yml"}


def test_an_inherited_slot_credits_factory_md_and_the_waiter_not_routing(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, INHERITED_BUILD, {"claude-bot": PINNED_WAITER})
    build = _row(matrix, "build")
    assert (build["source"], build["launch"], build["candidates"]) == ("inherit", "dispatcher", [])
    assert "fallback" not in build
    assert matrix["unrouted"] == [_pinned("claude", "claude-bot", "claude-haiku-4-5", "low")]
    detail = json.dumps({"action": "build", "routing": {"builder": "claude", "reviewer": "codex"}})
    launched = routing_argv_for_detail(list(PINNED_WAITER), harness="claude", detail=detail)
    assert exposure.parsed(launched, "claude") == exposure.Chosen("claude-haiku-4-5", "low")


def test_the_unrouted_values_are_what_a_repository_without_routing_runs(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude-bot": PINNED_WAITER, "gemini-bot": GEMINI_WAITER, "codex-bot": None})
    assert matrix["unrouted"] == [
        _pinned("claude", "claude-bot", "claude-haiku-4-5", "low"),
        {"harness": "gemini", "model": None, "model_source": "CLI default", "effort": None,
         "effort_source": "CLI default"},
    ]
    assert _effective(matrix, "build", 1)["model"] == "claude-opus-4-6"


def test_an_unread_waiter_makes_every_value_unknown(tmp_path: Path) -> None:
    missing = _matrix(tmp_path, waiters={"claude-bot": None})
    assert _effective(missing, "build", 1) == {**UNKNOWN, "replaced": {}, "applied": None}
    assert missing["waiters"] == {"claude-bot": "missing"}
    unseen = _matrix(tmp_path)
    for row in unseen["roles"]:
        for cand in row.get("candidates", []):
            if row["launch"] == "dispatcher":
                assert cand["effective"] == {**UNKNOWN, "replaced": {}, "applied": None}, row["role"]
    assert unseen["unrouted"] == []


def test_the_plan_row_follows_the_builders_harness(tmp_path: Path) -> None:
    waiters = {"claude-bot": CLAUDE_WAITER, "codex-bot": ("codex", "exec")}
    only_codex = _row(_matrix(tmp_path, CODEX_ONLY_PLAN, waiters), "plan")
    assert (only_codex["builder"], only_codex["plans"]) == ("claude", "build")
    assert only_codex["candidates"][0]["on_builder"] is False
    on_claude = _row(_matrix(tmp_path, waiters=waiters), "plan")
    assert (on_claude["builder"], on_claude["plans"]) == ("claude", "plan")
    assert on_claude["candidates"][0]["on_builder"] is True
    assert "on_builder" not in _row(_matrix(tmp_path, waiters=waiters), "build")["candidates"][0]
    unknown_builder = _row(_matrix(tmp_path, INHERITED_BUILD, waiters), "plan")
    assert (unknown_builder["builder"], unknown_builder["plans"]) == (None, None)
    assert unknown_builder["candidates"][0]["on_builder"] is None


def test_the_orchestrate_row_reports_the_orchestrator_waiter(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"orchestrator": PINNED_WAITER, "claude-bot": CLAUDE_WAITER})
    row = _row(matrix, "orchestrate")
    assert row["launch"] == "waiter-orchestrator.yml"
    assert row["runs"] == {"model": "claude-haiku-4-5", "model_source": "waiter-orchestrator.yml",
                           "effort": "low", "effort_source": "waiter-orchestrator.yml"}
    assert matrix["waiters"] == {"claude-bot": "ok", "orchestrator": "ok"}
    assert _row(_matrix(tmp_path, waiters={"orchestrator": None}), "orchestrate")["runs"] == UNKNOWN
    assert _row(_matrix(tmp_path, waiters={"orchestrator": ("codex", "exec")}), "orchestrate")["runs"] == {
        "model": None, "model_source": "CLI default", "effort": None, "effort_source": "CLI default"}


def test_rows_without_a_dispatcher_launch_carry_no_waiter_claims(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude-bot": CLAUDE_WAITER})
    qa = _row(matrix, "qa")
    assert (qa["launch"], qa["runs"]) == ("factory.md", {
        "model": None, "model_source": "factory.md", "effort": None, "effort_source": "runner default"})
    for role in ("orchestrate", "qa", "helper:deep", "helper:panel"):
        row = _row(matrix, role)
        assert all("effective" not in cand for cand in row["candidates"]), role
    assert _row(matrix, "helper:deep")["launch"] is None and "runs" not in _row(matrix, "helper:deep")
    assert {row["launch"] for row in matrix["roles"] if row["role"].startswith("build") or row["role"] in (
        "plan", "review")} == {"dispatcher"}


def test_a_waiter_command_the_parser_cannot_read_is_unknown_not_cli_default(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, waiters={"claude-bot": OPAQUE_WAITER})
    assert _effective(matrix, "build", 1) == {**UNKNOWN, "replaced": {}, "applied": False}
    assert matrix["waiters"] == {"claude-bot": "opaque"}
    assert matrix["unrouted"] == []
    assert exposure.parsed(list(OPAQUE_WAITER), "claude") is None
    assert exposure.parsed(["claude", "-p", "--settings", "/etc/claude.json"], "claude") is None
    assert exposure.parsed(["claude", "-p", "--settings", '{"model": "x"}'], "claude") == exposure.Chosen(
        "x", exposure.UNKNOWN)


def test_waiter_argv_includes_the_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / ".config" / "factory"
    config.mkdir(parents=True)
    config.chmod(0o700)
    waiter = config / "waiter-orchestrator.yml"
    waiter.write_text("bot: orchestrator\ncommand: [claude, -p]\n", encoding="utf-8")
    waiter.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dashboard.waiter_argv() == {
        "claude-bot": None, "codex-bot": None, "cursor-bot": None, "gemini-bot": None, "orchestrator": ("claude", "-p")}


LAUNCH_CASES = {
    "inherited": (INHERITED_BUILD, {"claude-bot": PINNED_WAITER}),
    "unread": (ROUTING, {"claude-bot": None, "orchestrator": None}),
    "plan-on-builder": (CODEX_ONLY_PLAN, {"claude-bot": CLAUDE_WAITER, "codex-bot": ("codex", "exec")}),
    "orchestrator": (ROUTING, {"orchestrator": PINNED_WAITER, "claude-bot": CLAUDE_WAITER}),
    "opaque": (ROUTING, {"claude-bot": OPAQUE_WAITER, "gemini-bot": GEMINI_WAITER}),
}


@needs_node
@pytest.mark.parametrize(("text", "waiters"), LAUNCH_CASES.values(), ids=list(LAUNCH_CASES))
def test_server_and_script_draw_the_same_launchers(tmp_path: Path, base: dict, text: str, waiters: dict) -> None:
    snapshot = _with(base, _matrix(tmp_path, text, waiters))
    assert _panel(tmp_path, snapshot, "script") == _panel(tmp_path, snapshot, "server")


@pytest.mark.parametrize("render", BOTH)
def test_panel_says_what_an_inherited_slot_and_an_unrouted_repository_run(tmp_path: Path, base: dict, render: str) -> None:
    panel = _panel(tmp_path, _with(base, _matrix(tmp_path, INHERITED_BUILD, {"claude-bot": PINNED_WAITER})), render)
    assert _role(panel, "build") == (
        '<td>inherit</td><td><span class="muted">falls back to factory.md builder: its model, else the waiter&#x27;s</span></td>')
    unrouted = re.search(r'<p class="rt-unrouted">(.*?)</p>', panel)
    assert unrouted and _text(unrouted.group(1)) == (
        "A repository whose factory.md has no routing: block, or a factory.md slot naming no model, runs each "
        "waiter's own values: claude: claude-haiku-4-5 waiter-claude-bot.yml · low waiter-claude-bot.yml")
    assert "A repository&#x27;s factory.md routing.slots override runs as written and is not shown." in panel
    assert "rt-unrouted" not in _panel(tmp_path, _with(base, _matrix(tmp_path)), render)


@pytest.mark.parametrize("render", BOTH)
def test_panel_marks_unread_and_opaque_waiters_as_unknown(tmp_path: Path, base: dict, render: str) -> None:
    unread = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters={"claude-bot": None})), render)
    assert _runs(unread, "build")[0] == "runs unknown · unknown"
    opaque = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters={"claude-bot": OPAQUE_WAITER})), render)
    assert _runs(opaque, "build")[0] == "runs unknown · unknown routing.yml flags not added by this waiter"
    assert "CLI default" not in _role(opaque, "build")
    line = '<p class="rt-waiters">waiter-&lt;bot&gt;.yml not interpreted for claude-bot; its values show unknown</p>'
    assert opaque.count(line) == 1


@pytest.mark.parametrize("render", BOTH)
def test_panel_says_the_plan_runs_on_the_builders_harness(tmp_path: Path, base: dict, render: str) -> None:
    waiters = {"claude-bot": CLAUDE_WAITER, "codex-bot": ("codex", "exec")}
    panel = _panel(tmp_path, _with(base, _matrix(tmp_path, CODEX_ONLY_PLAN, waiters)), render)
    plan = _role(panel, "plan")
    assert '<span class="rt-flag">skipped: the builder runs on claude</span>' in plan
    assert '<div class="rt-plans">no plan candidate on claude: the build pick plans</div>' in plan
    on_claude = _role(_panel(tmp_path, _with(base, _matrix(tmp_path, waiters=waiters)), render), "plan")
    assert "rt-plans" not in on_claude and "skipped" not in on_claude


@pytest.mark.parametrize("render", BOTH)
def test_panel_reports_each_dedicated_launcher(tmp_path: Path, base: dict, render: str) -> None:
    waiters = {"orchestrator": PINNED_WAITER, "claude-bot": CLAUDE_WAITER}
    panel = _panel(tmp_path, _with(base, _matrix(tmp_path, waiters=waiters)), render)
    orchestrate = _role(panel, "orchestrate")
    assert "<div" not in re.search(r"<ol.*?</ol>", orchestrate).group(0)
    assert _runs(panel, "orchestrate") == [
        "runs claude-haiku-4-5 waiter-orchestrator.yml · low waiter-orchestrator.yml routing.yml not applied"]
    assert _runs(panel, "qa") == ["runs factory.md · runner default routing.yml not applied"]
    assert _runs(panel, "helper:deep") == []
    assert '<span class="muted">no factory launcher</span>' in _role(panel, "helper:deep")
