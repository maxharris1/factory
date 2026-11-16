"""Self-heal decisions and actions (issue #107): fakes only, no process is run."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from lib import heal, health
from lib.grok_delivery import WebhookResult
from lib.heal import DEFER, ESCALATE, RESTART, SKIP, Facts, KeyState, Webhook

NOW = 1_800_000_000.0
CHECKOUT = "ba9876543210"
OWNED = frozenset({"poller", "waiter:codex-bot", "linear-webhook"})
SECRETS = {
    "FACTORY_WEBHOOK_URL": "https://hooks.example.test/secret-path",
    "FACTORY_WEBHOOK_AUTH": "Bearer secret-auth-value",
    "LINEAR_API_KEY": "lin_api_secretvalue",
}
WEBHOOK = Webhook(linear_key_env="LINEAR_API_KEY")


def component(name: str = "waiter", *, bot: str = "codex-bot", device: str = "box",
              state: str = "up", pid: int | None = 4242, started_at: float = NOW - 600,
              version: str = CHECKOUT, configured: bool = True, reason: str = "") -> dict:
    who = f"{name} {bot}" if bot else name
    return {
        "component": name, "bot": bot, "device": device, "state": state, "reason": reason,
        "pid": pid, "started_at": started_at, "version": version, "configured": configured,
        "stale_code": version not in (CHECKOUT, "unknown"), "label": f"{who} @ {device}",
    }


def report(*components: dict) -> dict:
    return {"known": True, "code_version": CHECKOUT, "components": list(components)}


def facts(**changes) -> Facts:
    values = dict(device="box", owned=OWNED, held=frozenset(), listener_busy=False)
    values.update(changes)
    return Facts(**values)


def kinds(actions) -> list[tuple[str, str]]:
    return [(a.kind, a.label) for a in actions]


DOWN = component(state="down", reason="no heartbeat for 3m")


# --- decide ------------------------------------------------------------------------------------


def test_a_down_component_is_restarted() -> None:
    (action,) = heal.decide(report(DOWN), {}, facts(), NOW)
    assert (action.kind, action.service, action.reason) == (
        RESTART, "waiter:codex-bot", "down: no heartbeat for 3m")


def test_a_healthy_component_is_left_alone() -> None:
    assert heal.decide(report(component()), {}, facts(), NOW) == []


@pytest.mark.parametrize("name, bot, service", [
    ("poller", "", "poller"),
    ("waiter", "codex-bot", "waiter:codex-bot"),
    ("linear-listener", "", "linear-webhook"),
])
def test_stale_code_restarts_an_idle_component(name: str, bot: str, service: str) -> None:
    (action,) = heal.decide(report(component(name, bot=bot, version="0123456789ab")), {},
                            facts(drain_acked={"codex-bot": CHECKOUT}), NOW)
    assert (action.kind, action.service) == (RESTART, service)
    assert action.reason == "stale code: running 0123456789ab, checkout ba9876543210"


def test_a_busy_listener_with_stale_code_is_deferred() -> None:
    item = component("linear-listener", bot="", version="0123456789ab")
    (action,) = heal.decide(report(item), {}, facts(listener_busy=True), NOW)
    assert action.kind == DEFER


def test_a_down_busy_listener_is_still_restarted() -> None:
    item = component("linear-listener", bot="", state="down")
    (action,) = heal.decide(report(item), {}, facts(listener_busy=True), NOW)
    assert action.kind == RESTART


def test_a_held_stop_is_never_overridden() -> None:
    (action,) = heal.decide(report(DOWN), {}, facts(held=frozenset({"waiter:codex-bot"})), NOW)
    assert (action.kind, action.reason) == (SKIP, "held stopped")


@pytest.mark.parametrize("item, host", [
    (component(state="down", bot="claude-bot"), facts()),
    (component(state="down", device="pc"), facts()),
    (component(state="down", configured=False), facts()),
    (component("dashboard", bot="", state="down"), facts()),
    (component("device-probe", bot="", device="pc", state="down"), facts(device="pc")),
    (DOWN, facts(owned=frozenset())),
], ids=["not-owned", "other-device", "not-configured", "dashboard", "device-probe", "no-flags"])
def test_components_this_account_does_not_own_are_ignored(item: dict, host: Facts) -> None:
    assert heal.decide(report(item), {}, host, NOW) == []


def test_an_unknown_report_does_nothing() -> None:
    assert heal.decide({"known": False, "components": [DOWN]}, {}, facts(), NOW) == []


def test_a_fresh_restart_is_left_to_settle() -> None:
    states = {DOWN["label"]: KeyState(attempts=[NOW - 30])}
    assert heal.decide(report(DOWN), states, facts(), NOW) == []
    states = {DOWN["label"]: KeyState(attempts=[NOW - 90])}
    assert kinds(heal.decide(report(DOWN), states, facts(), NOW)) == [(RESTART, DOWN["label"])]


def test_backoff_after_three_restarts_in_the_window_then_escalate_once() -> None:
    states = {DOWN["label"]: KeyState(attempts=[NOW - 1500, NOW - 900, NOW - 300])}
    actions = heal.decide(report(DOWN), states, facts(), NOW)
    assert [a.kind for a in actions] == [ESCALATE, SKIP]
    assert actions[0].reason == "backoff: 3 restarts in 30m"
    assert actions[1].reason.startswith("backoff: 3 restarts in 30m (down:")

    states[DOWN["label"]].escalated_at = NOW - 60
    assert [a.kind for a in heal.decide(report(DOWN), states, facts(), NOW)] == [SKIP]


def test_three_restarts_that_fixed_it_do_not_escalate() -> None:
    states = {DOWN["label"]: KeyState(attempts=[NOW - 1500, NOW - 900, NOW - 300],
                                      failures=[NOW - 1500, NOW - 900, NOW - 300])}
    assert heal.decide(report(component()), states, facts(), NOW) == []


def test_backoff_ends_when_the_oldest_attempt_leaves_the_window() -> None:
    states = {DOWN["label"]: KeyState(attempts=[NOW - 1801, NOW - 900, NOW - 300])}
    assert [a.kind for a in heal.decide(report(DOWN), states, facts(), NOW)] == [RESTART]


def test_a_restart_that_keeps_failing_escalates() -> None:
    states = {DOWN["label"]: KeyState(failures=[NOW - 900, NOW - 600, NOW - 300],
                                      attempts=[NOW - 300])}
    actions = heal.decide(report(DOWN), states, facts(), NOW)
    assert actions[0].kind == ESCALATE
    assert actions[0].reason.startswith("restart failing: 3 failed restarts")


def test_pid_churn_self_heal_did_not_cause_is_flapping() -> None:
    states: dict[str, KeyState] = {}
    for step, pid in enumerate((10, 11, 12, 13)):
        heal.observe(report(component(pid=pid)), states, NOW + step * 120)
    later = NOW + 4 * 120
    (action,) = heal.decide(report(component(pid=13)), states, facts(), later)
    assert (action.kind, action.flapping) == (ESCALATE, True)
    assert action.reason == "flapping: 3 process changes in 30m"


def test_a_process_change_self_heal_caused_is_not_flapping() -> None:
    states: dict[str, KeyState] = {}
    for step, pid in enumerate((10, 11, 12, 13)):
        at = NOW + step * 120
        heal.observe(report(component(pid=pid)), states, at)
        states[component()["label"]].attempts.append(at + 1)
    assert states[component()["label"]].changes == []


# --- heal: acting, logging, escalating --------------------------------------------------------


class Runs:
    def __init__(self, code: int = 0):
        self.code = code
        self.calls: list[tuple[list[str], dict, Path]] = []

    def __call__(self, argv, env, log_path):
        self.calls.append((list(argv), dict(env), log_path))
        return self.code


class Posts:
    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, url, authorization, body, timeout):
        self.calls.append((url, authorization, json.loads(body)))
        return WebhookResult(self.status, {})


def run_heal(tmp_path: Path, data: dict, *, now: float = NOW, runs: Runs | None = None,
             posts: Posts | None = None, environ: dict | None = None, **kw):
    runs = runs or Runs()
    posts = posts or Posts()
    actions = heal.heal(data, facts=kw.pop("host", facts()), state_dir=tmp_path,
                        deploy=Path("/srv/factory/deploy"), webhook=WEBHOOK,
                        environ=environ if environ is not None else {"PATH": "/usr/bin", **SECRETS},
                        now=now, run=runs, post=posts, clock=lambda: 0.0, **kw)
    return actions, runs, posts


def log_lines(tmp_path: Path) -> list[str]:
    path = tmp_path / "self-heal.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_restarts_run_the_components_own_service_script(tmp_path: Path) -> None:
    data = report(
        component("poller", bot="", state="down"),
        DOWN,
        component("linear-listener", bot="", version="0123456789ab"),
        component("tunnel", bot="", state="down"),
    )
    _, runs, _ = run_heal(tmp_path, data)
    assert [call[0] for call in runs.calls] == [
        ["/srv/factory/deploy/poller-service.sh", "restart"],
        ["/srv/factory/deploy/waiter-service.sh", "codex-bot", "restart"],
        ["/srv/factory/deploy/linear-webhook-service.sh", "restart"],
    ]
    assert [call[2].name for call in runs.calls] == [
        "poller-service.log", "waiter-codex-bot-service.log", "linear-webhook-service.log"]
    assert runs.calls[0][1]["ORCH_POLL_STOP_WAIT"] == "300"
    assert "ORCH_POLL_STOP_WAIT" not in runs.calls[1][1]
    lines = log_lines(tmp_path)
    assert lines[0] == (
        "2027-01-15T08:00:00Z self-heal: restart poller @ box (down: down) exit=0 0.0s")
    assert lines[-1].endswith(
        "restart tunnel @ box (down: down) with linear-webhook")
    state = json.loads((tmp_path / "self-heal.state.json").read_text(encoding="utf-8"))
    assert state["keys"]["tunnel @ box"]["attempts"] == [NOW]


def test_service_scripts_never_get_the_webhook_or_linear_secrets(tmp_path: Path) -> None:
    _, runs, _ = run_heal(tmp_path, report(DOWN))
    (_, env, _), = runs.calls
    assert env == {"PATH": "/usr/bin"}


def test_skips_and_defers_are_logged_once_per_change(tmp_path: Path) -> None:
    host = facts(held=frozenset({"waiter:codex-bot"}))
    for step in range(3):
        run_heal(tmp_path, report(DOWN), now=NOW + step * 120, host=host)
    assert log_lines(tmp_path) == [
        "2027-01-15T08:00:00Z self-heal: skip waiter codex-bot @ box (held stopped)"]
    run_heal(tmp_path, report(component()), now=NOW + 400, host=host)
    run_heal(tmp_path, report(DOWN), now=NOW + 500, host=host)
    assert len(log_lines(tmp_path)) == 2


def test_backoff_escalates_once_with_no_secret_in_the_log_or_body(tmp_path: Path) -> None:
    runs = Runs()
    posts = Posts()
    for step in range(6):
        run_heal(tmp_path, report(DOWN), now=NOW + step * 120, runs=runs, posts=posts)
    assert len(runs.calls) == 3
    (url, authorization, body), = posts.calls
    assert url == SECRETS["FACTORY_WEBHOOK_URL"]
    assert authorization == SECRETS["FACTORY_WEBHOOK_AUTH"]
    assert body["action"] == "self-heal" and body["kind"] == "escalation"
    assert body["target_bot"] == "grok-bot" and body["device"] == "box"
    assert body["components"] == [{"label": "waiter codex-bot @ box", "state": "down",
                                   "reason": "backoff: 3 restarts in 30m", "attempts": 3,
                                   "flapping": False}]
    assert body["logical_key"].startswith("self-heal:box:waiter codex-bot @ box:")
    text = (tmp_path / "self-heal.log").read_text(encoding="utf-8") + json.dumps(body)
    text += (tmp_path / "self-heal.state.json").read_text(encoding="utf-8")
    for value in SECRETS.values():
        assert value not in text
    assert "hooks.example.test" not in text
    assert any(line.endswith("(backoff: 3 restarts in 30m) escalated") for line in log_lines(tmp_path))


def test_an_escalation_without_the_webhook_env_is_logged_as_skipped(tmp_path: Path) -> None:
    states = {DOWN["label"]: vars(KeyState(attempts=[NOW - 900, NOW - 600, NOW - 300]))}
    (tmp_path / "self-heal.state.json").write_text(json.dumps({"keys": states}), encoding="utf-8")
    _, _, posts = run_heal(tmp_path, report(DOWN), environ={"PATH": "/usr/bin"})
    assert posts.calls == []
    assert log_lines(tmp_path)[-1].endswith(
        "escalate waiter codex-bot @ box (backoff: 3 restarts in 30m) "
        "escalation skipped: missing-env:FACTORY_WEBHOOK_URL")


@pytest.mark.parametrize("status, word", [(503, "escalation failed: HTTP-503")])
def test_a_failed_escalation_names_only_the_status(tmp_path: Path, status: int, word: str) -> None:
    states = {DOWN["label"]: vars(KeyState(attempts=[NOW - 900, NOW - 600, NOW - 300]))}
    (tmp_path / "self-heal.state.json").write_text(json.dumps({"keys": states}), encoding="utf-8")
    run_heal(tmp_path, report(DOWN), posts=Posts(status))
    assert log_lines(tmp_path)[-1].endswith(word)


def test_a_failing_restart_command_is_recorded_and_escalates(tmp_path: Path) -> None:
    runs = Runs(code=1)
    posts = Posts()
    for step in range(3):
        run_heal(tmp_path, report(DOWN), now=NOW + step * 120, runs=runs, posts=posts)
    state = json.loads((tmp_path / "self-heal.state.json").read_text(encoding="utf-8"))
    assert len(state["keys"]["waiter codex-bot @ box"]["failures"]) == 3
    run_heal(tmp_path, report(DOWN), now=NOW + 3 * 120, runs=runs, posts=posts)
    assert posts.calls[0][2]["components"][0]["reason"].startswith("backoff")


def test_a_corrupt_state_file_is_logged_and_replaced(tmp_path: Path) -> None:
    (tmp_path / "self-heal.state.json").write_text("{not json", encoding="utf-8")
    _, runs, _ = run_heal(tmp_path, report(DOWN))
    assert len(runs.calls) == 1
    assert "state self-heal.state.json unreadable; starting fresh" in log_lines(tmp_path)[0]
    json.loads((tmp_path / "self-heal.state.json").read_text(encoding="utf-8"))


def test_dry_run_logs_and_runs_nothing(tmp_path: Path) -> None:
    _, runs, _ = run_heal(tmp_path, report(DOWN), dry_run=True)
    assert runs.calls == []
    assert log_lines(tmp_path)[-1].endswith("would restart waiter codex-bot @ box "
                                            "(down: no heartbeat for 3m)")


# --- orch heal ---------------------------------------------------------------------------------


def test_facts_come_from_autostart_flags_and_stop_markers(tmp_path: Path) -> None:
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()
    state.mkdir()
    (config / "poller-autostart").touch()
    (config / "waiter-autostart-codex-bot").touch()
    (state / "waiter-codex-bot.stopped").touch()
    host = heal.facts_for("box", config, state, None, NOW)
    assert host.owned == frozenset({"poller", "waiter:codex-bot"})
    assert host.held == frozenset({"waiter:codex-bot"})
    assert host.listener_busy is False


def test_the_listener_is_busy_while_a_delivery_is_fresh(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE linear_webhook_health (id INTEGER PRIMARY KEY, "
                           "started_at REAL, last_received_at REAL, last_event TEXT)")
        connection.execute("INSERT INTO linear_webhook_health VALUES (1, 0, ?, '-')", (NOW - 3,))
    assert heal.facts_for("box", tmp_path, tmp_path, db, NOW).listener_busy is True
    assert heal.facts_for("box", tmp_path, tmp_path, db, NOW + 60).listener_busy is False


def test_orch_heal_reads_the_report_from_stdin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ORCH_DB", raising=False)
    code = heal.main(["--health-json", "-", "--state", str(tmp_path / "state"), "--dry-run",
                      "--device", "box", "--config", str(tmp_path)],
                     stdin=io.StringIO(json.dumps(report(DOWN))), clock=lambda: NOW)
    assert code == 0
    assert (tmp_path / "state" / "self-heal.state.json").is_file()


def test_orch_heal_refuses_an_unreadable_report(tmp_path: Path) -> None:
    code = heal.main(["--health-json", "-", "--state", str(tmp_path)],
                     stdin=io.StringIO("not json"), clock=lambda: NOW)
    assert code == 1
    assert log_lines(tmp_path) == [
        "2027-01-15T08:00:00Z self-heal: health report unreadable (JSONDecodeError); nothing done"]


def test_orch_heal_is_listed_in_the_cli_help(capsys) -> None:
    from lib.cli import main as cli_main

    cli_main(["--help"])
    text = capsys.readouterr().out
    assert "  heal             restart down or stale-code components" in text
    assert "  health           component heartbeats: report or forget (issue #92)" in text


def test_health_help_names_only_subcommands_the_parser_accepts(capsys, tmp_path) -> None:
    from lib.cli import main as cli_main

    cli_main(["--help"])
    (line,) = [l for l in capsys.readouterr().out.splitlines() if l.startswith("  health ")]
    verbs = re.findall(r"\b[a-z]+\b", line.split(":", 1)[1].split("(")[0])
    verbs = [v for v in verbs if v not in {"or", "report"}]
    assert verbs
    for verb in verbs:
        try:
            cli_main(["health", verb, "--db", str(tmp_path / "orch.db")])
        except SystemExit:
            pass
        err = capsys.readouterr().err
        assert f"unrecognized arguments: {verb}" not in err, line
        assert "invalid choice" not in err, line


HELD_TUNNEL = component("tunnel", bot="", state="down",
                        reason="port held by another owner: 8787 on pc-wsl-cursor-bot")


def test_a_tunnel_whose_port_another_owner_holds_alerts_once_and_is_not_restarted(
    tmp_path: Path,
) -> None:
    actions, runs, posts = run_heal(tmp_path, report(HELD_TUNNEL))
    assert kinds(actions) == [(ESCALATE, "tunnel @ box"), (SKIP, "tunnel @ box")]
    assert runs.calls == [] and len(posts.calls) == 1
    for later in (NOW + 120, NOW + 3 * heal.WINDOW_SECONDS):
        actions, runs, posts = run_heal(tmp_path, report(HELD_TUNNEL), now=later)
        assert kinds(actions) == [(SKIP, "tunnel @ box")]
        assert runs.calls == [] and posts.calls == []


def test_a_dry_run_leaves_the_port_held_alert_to_the_next_live_pass(tmp_path: Path) -> None:
    actions, _, posts = run_heal(tmp_path, report(HELD_TUNNEL), dry_run=True)
    assert kinds(actions) == [(ESCALATE, "tunnel @ box"), (SKIP, "tunnel @ box")]
    assert posts.calls == []
    actions, runs, posts = run_heal(tmp_path, report(HELD_TUNNEL), now=NOW + 120)
    assert kinds(actions) == [(ESCALATE, "tunnel @ box"), (SKIP, "tunnel @ box")]
    assert runs.calls == [] and len(posts.calls) == 1


def test_a_port_held_again_after_it_was_freed_alerts_again(tmp_path: Path) -> None:
    run_heal(tmp_path, report(HELD_TUNNEL))
    run_heal(tmp_path, report(component("tunnel", bot="")), now=NOW + 120)
    _, _, posts = run_heal(tmp_path, report(HELD_TUNNEL), now=NOW + 240)
    assert len(posts.calls) == 1


def test_a_failed_port_held_alert_is_sent_again_on_the_next_pass(tmp_path: Path) -> None:
    run_heal(tmp_path, report(HELD_TUNNEL), posts=Posts(status=500))
    _, _, posts = run_heal(tmp_path, report(HELD_TUNNEL), now=NOW + 120)
    assert len(posts.calls) == 1


def test_a_port_held_alert_skipped_for_a_missing_url_is_sent_once_configured(
    tmp_path: Path,
) -> None:
    unconfigured = {"PATH": "/usr/bin", **SECRETS}
    unconfigured.pop(WEBHOOK.url_env)
    _, _, posts = run_heal(tmp_path, report(HELD_TUNNEL), environ=unconfigured)
    assert posts.calls == []
    sent = [len(run_heal(tmp_path, report(HELD_TUNNEL), now=NOW + later)[2].calls)
            for later in (120, 3600)]
    assert sent == [1, 0]


def test_an_unknown_report_does_not_end_a_port_held_episode(tmp_path: Path) -> None:
    run_heal(tmp_path, report(HELD_TUNNEL))
    run_heal(tmp_path, {"known": False, "error": "cannot read health: OperationalError"},
             now=NOW + 60)
    sent = [len(run_heal(tmp_path, report(HELD_TUNNEL), now=NOW + later)[2].calls)
            for later in (120, 3600)]
    assert sent == [0, 0]


def held_tunnel_health(now: float) -> dict:
    report = health.build_report(
        [], now, expected=health.Expected((health.Slot("tunnel", ("box",)),), frozenset({"tunnel"})),
        tunnel_held="8787 on pc-wsl-cursor-bot", tunnel_held_since=NOW - 3600,
        tunnel_alert_seconds=30 * 60)
    return health.report_json(report, now)


def test_a_held_tunnel_that_needs_attention_alerts_once_and_is_never_restarted(
    tmp_path: Path,
) -> None:
    passes = []
    for now in range(int(NOW), int(NOW) + 2 * 3600 + 1, 120):
        data = held_tunnel_health(now)
        assert [item["kind"] for item in data["needs_attention"]] == ["tunnel-down"]
        actions, runs, _ = run_heal(tmp_path, data, now=now)
        assert runs.calls == []
        passes.append(kinds(actions))
    assert passes[0] == [(ESCALATE, "tunnel @ box"), (SKIP, "tunnel @ box")]
    assert all(actions == [(SKIP, "tunnel @ box")] for actions in passes[1:])
