"""Self-heal decisions and actions (issue #107): fakes only, no process is run."""

from __future__ import annotations

import io
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from lib import account_doctor, heal, health
from lib.grok_delivery import WebhookResult
from lib.heal import DEFER, DRAIN, ESCALATE, RESTART, SKIP, Facts, KeyState, Webhook
from lib.queue import SQLiteJobQueue
from tests.test_health import TUNNEL_URL
from tests.test_health import orch as orch_config

NOW = 1_800_000_000.0
CHECKOUT = "ba9876543210"
OWNED = frozenset({"poller", "waiter:codex-bot", "linear-webhook", "signin-broker"})
SECRETS = {
    "FACTORY_WEBHOOK_URL": "https://hooks.example.test/secret-path",
    "FACTORY_WEBHOOK_AUTH": "Bearer secret-auth-value",
    "LINEAR_API_KEY": "lin_api_secretvalue",
}
WEBHOOK = Webhook(config_secret_envs=("LINEAR_API_KEY",))


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
    ("signin-broker", "", "signin-broker"),
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


def test_the_signin_broker_is_its_own_service() -> None:
    assert heal.service_of(health.SIGNIN_BROKER, "") == "signin-broker"


def test_a_signin_broker_with_stale_code_waits_while_a_pair_is_held() -> None:
    item = component("signin-broker", bot="", version="0123456789ab")
    (action,) = heal.decide(report(item), {}, facts(signin_busy=True), NOW)
    assert (action.kind, action.service, action.reason) == (
        DEFER, "signin-broker", "stale code, a sign-in or sign-out holds a pair")


@pytest.mark.parametrize("item, host", [
    (component("signin-broker", bot="", version="0123456789ab"), facts(listener_busy=True)),
    (component("signin-broker", bot="", state="down"), facts(signin_busy=True)),
    (component("linear-listener", bot="", version="0123456789ab"), facts(signin_busy=True)),
], ids=["stale-not-busy", "down-busy", "listener-not-held-by-a-pair"])
def test_only_a_stale_broker_waits_for_a_held_pair(item: dict, host: Facts) -> None:
    (action,) = heal.decide(report(item), {}, host, NOW)
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


def test_the_signin_broker_restarts_with_its_own_script_log_and_no_secrets(
        tmp_path: Path) -> None:
    _, runs, _ = run_heal(tmp_path, report(component("signin-broker", bot="", state="down")))
    (argv, env, log_path), = runs.calls
    assert argv == ["/srv/factory/deploy/signin-broker-service.sh", "restart"]
    assert log_path == tmp_path / "signin-broker-service.log"
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


def test_the_signin_broker_facts_come_from_its_autostart_flag_and_stop_marker(
        tmp_path: Path) -> None:
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()
    state.mkdir()
    assert "signin-broker" not in heal.facts_for("box", config, state, None, NOW).owned
    (config / "signin-broker-autostart").touch()
    (state / "signin-broker.stopped").touch()
    host = heal.facts_for("box", config, state, None, NOW)
    assert host.owned == frozenset({"signin-broker"})
    assert host.held == frozenset({"signin-broker"})
    assert host.signin_busy is False


def signout_db(tmp_path: Path, *since: float) -> Path:
    import sqlite3

    from lib import account_doctor

    db = tmp_path / "orch.db"
    with sqlite3.connect(db) as connection:
        account_doctor.signing_out(connection, NOW)
        connection.executemany(
            f"INSERT INTO {account_doctor.SIGNOUTS} (account, device, since) VALUES (?, 'box', ?)",
            [(f"account-{n}", at) for n, at in enumerate(since)])
    return db


def test_the_signin_broker_is_busy_while_a_signout_lock_is_live(tmp_path: Path) -> None:
    from lib.account_doctor import SIGNOUT_LEASE_SECONDS

    db = signout_db(tmp_path, NOW - SIGNOUT_LEASE_SECONDS - 1, NOW - 30)
    assert heal.facts_for("box", tmp_path, tmp_path, db, NOW).signin_busy is True
    assert heal.facts_for("box", tmp_path, tmp_path, db, NOW + SIGNOUT_LEASE_SECONDS).signin_busy is False


def test_a_lapsed_or_missing_signout_lock_is_not_busy(tmp_path: Path) -> None:
    import sqlite3

    from lib.account_doctor import SIGNOUT_LEASE_SECONDS

    lapsed = signout_db(tmp_path, NOW - SIGNOUT_LEASE_SECONDS - 1)
    assert heal.facts_for("box", tmp_path, tmp_path, lapsed, NOW).signin_busy is False
    assert heal.facts_for("box", tmp_path, tmp_path, None, NOW).signin_busy is False
    assert heal.facts_for("box", tmp_path, tmp_path, tmp_path / "absent.db", NOW).signin_busy is False
    bare = tmp_path / "bare.db"
    sqlite3.connect(bare).close()
    assert heal.facts_for("box", tmp_path, tmp_path, bare, NOW).signin_busy is False
    with sqlite3.connect(bare) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


STALE_BROKER = component("signin-broker", bot="", version="0123456789ab")


def test_a_signin_admitted_during_an_earlier_restart_defers_the_stale_broker(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for service in ("poller", "signin-broker"):
        (config / f"{service}-autostart").touch()
    db = tmp_path / "orch.db"

    def snapshot() -> Facts:
        return heal.facts_for("box", config, tmp_path, db, NOW)

    calls = []

    def run(argv, env, log_path):
        script = Path(argv[0]).name
        if script == "poller-service.sh":
            with sqlite3.connect(db) as connection:
                assert account_doctor.reserve_pair(connection, "claude-c", "box", "signin-1", NOW)
        calls.append((script, snapshot().signin_busy))
        return 0

    data = report(component("poller", bot="", version="0123456789ab"), STALE_BROKER)
    actions = heal.heal(data, facts=snapshot(), state_dir=tmp_path, deploy=Path("/srv/factory/deploy"),
                        webhook=WEBHOOK, environ={}, now=NOW, run=run, refresh=snapshot)

    assert calls == [("poller-service.sh", True)]
    assert kinds(actions) == [(RESTART, "poller @ box"), (DEFER, "signin-broker @ box")]
    assert actions[1].reason == "stale code, a sign-in or sign-out holds a pair"
    assert log_lines(tmp_path)[-1].endswith(
        "defer signin-broker @ box (stale code, a sign-in or sign-out holds a pair)")


def test_signins_are_fenced_while_a_stale_broker_restarts(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    sqlite3.connect(db).close()
    fenced = []

    def run(argv, env, log_path):
        with closing(sqlite3.connect(db, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            fenced.append(account_doctor.signins_fenced(connection, NOW))
            connection.execute("ROLLBACK")
        return 0

    actions, _, _ = run_heal(tmp_path, report(STALE_BROKER), runs=run, host=facts(db=db))

    assert fenced == [True]
    assert kinds(actions) == [(RESTART, "signin-broker @ box")]
    with closing(sqlite3.connect(db)) as connection:
        assert account_doctor.signins_fenced(connection, NOW) is False


def test_a_down_broker_restarts_while_a_pair_is_held(tmp_path: Path) -> None:
    db = signout_db(tmp_path, NOW - 30)
    actions, runs, _ = run_heal(tmp_path, report(component("signin-broker", bot="", state="down")),
                                host=facts(db=db, signin_busy=True))
    assert kinds(actions) == [(RESTART, "signin-broker @ box")]
    assert len(runs.calls) == 1


def test_a_stale_broker_waits_when_the_fence_cannot_be_written(tmp_path: Path) -> None:
    db = tmp_path / "orch.db"
    db.write_bytes(b"not a database")
    actions, runs, _ = run_heal(tmp_path, report(STALE_BROKER), host=facts(db=db))
    assert kinds(actions) == [(DEFER, "signin-broker @ box")]
    assert runs.calls == []


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


def test_a_same_host_tunnel_the_listener_answers_is_never_restarted_or_escalated(
    tmp_path: Path,
) -> None:
    db, state_dir = tmp_path / "database" / "orch.db", tmp_path / "state"
    db.parent.mkdir()
    state_dir.mkdir()
    SQLiteJobQueue(db, clock=lambda: NOW)
    now = NOW
    store = health.HealthStore(db, devices=frozenset({"box"}), clock=lambda: now)
    tunnel = health.TunnelProber(store, "box", TUNNEL_URL, fetch=lambda url, t: 405, pid=7,
                                 version="unknown")
    for now in range(int(NOW), int(NOW + heal.WINDOW_SECONDS + 240), 120):
        store.beat("linear-listener", "box", pid=7, started_at=NOW, version="unknown")
        tunnel.probe_once()
        data = health.report_json(health.load_report(db, now, orch=orch_config(listener=True)), now)
        assert [(c["state"], c["reason"]) for c in data["components"]
                if c["component"] == "tunnel"] == [("up", "")], now
        actions, runs, posts = run_heal(state_dir, data, now=now,
                                        host=facts(owned=frozenset({"linear-webhook"})))
        assert [a.kind for a in actions
                if a.label == "tunnel @ box" and a.kind in (RESTART, ESCALATE)] == [], now
        assert runs.calls == [] and posts.calls == [], now
    assert not [line for line in log_lines(state_dir)
                if "restart tunnel" in line or "escalate tunnel" in line]


# --- version updates and the restart budget (issue #748) --------------------------------------

COMMITS = [letter * 12 for letter in "abcdefg"]
POLLER = "poller @ box"


def moved(running: str, checkout: str, name: str = "poller", **kw) -> dict:
    item = component(name, bot=kw.pop("bot", ""), version=running, **kw)
    item["stale_code"] = running != checkout
    return {"known": True, "code_version": checkout, "components": [item]}


def saved(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "self-heal.state.json").read_text(encoding="utf-8"))["keys"]


def restart_lines(tmp_path: Path) -> list[str]:
    return [line for line in log_lines(tmp_path) if " self-heal: restart " in line]


def test_more_than_three_version_updates_in_the_window_all_restart(tmp_path: Path) -> None:
    runs = Runs()
    for step in range(5):
        data = moved(COMMITS[step], COMMITS[step + 1], pid=100 + step)
        run_heal(tmp_path, data, now=NOW + step * 180, runs=runs)
    lines = restart_lines(tmp_path)
    assert len(lines) == 5
    for step, line in enumerate(lines):
        assert line.endswith(f"(stale code: running {COMMITS[step]}, "
                             f"checkout {COMMITS[step + 1]}) exit=0 0.0s")
    assert not any("backoff" in line for line in log_lines(tmp_path))
    assert not any(" escalate " in line for line in log_lines(tmp_path))
    assert saved(tmp_path)[POLLER]["attempts"] == []


def test_a_free_update_still_lets_the_next_new_commit_restart() -> None:
    states = {POLLER: KeyState(restarted_at=NOW - 300, target=COMMITS[1])}
    (action,) = heal.decide(moved(COMMITS[1], COMMITS[2]), states, facts(), NOW)
    assert (action.kind, action.reason) == (
        RESTART, f"stale code: running {COMMITS[1]}, checkout {COMMITS[2]}")


def test_a_version_update_that_never_lands_backs_off_and_escalates(tmp_path: Path) -> None:
    stuck = moved(COMMITS[0], COMMITS[1])
    for step in range(6):
        run_heal(tmp_path, stuck, now=NOW + step * 120)
    assert len(restart_lines(tmp_path)) == 4
    need = f"stale code: running {COMMITS[0]}, checkout {COMMITS[1]}"
    assert f"2027-01-15T08:08:00Z self-heal: skip {POLLER} (backoff: 3 restarts in 30m ({need}))" \
        in log_lines(tmp_path)
    escalations = [line for line in log_lines(tmp_path) if " escalate " in line]
    assert len(escalations) == 1 and f"escalate {POLLER} (backoff: 3 restarts in 30m)" in escalations[0]


def test_updates_toward_a_moving_checkout_that_never_land_back_off_and_escalate(
    tmp_path: Path,
) -> None:
    runs = Runs()
    posts = Posts()
    for step in range(6):
        run_heal(tmp_path, moved(COMMITS[0], COMMITS[step + 1]), now=NOW + step * 120,
                 runs=runs, posts=posts)
    assert len(runs.calls) == 4
    assert f"2027-01-15T08:08:00Z self-heal: skip {POLLER} (backoff: 3 restarts in 30m " \
        f"(stale code: running {COMMITS[0]}, checkout {COMMITS[5]}))" in log_lines(tmp_path)
    (_, _, body), = posts.calls
    assert body["components"][0]["reason"] == "backoff: 3 restarts in 30m"
    assert len(saved(tmp_path)[POLLER]["attempts"]) == 3


LISTENER = "linear-listener @ box"
TUNNEL = "tunnel @ box"
NEVER_UP_TUNNEL = component("tunnel", bot="", state="down", reason="never reached",
                            pid=None, version="unknown")


def test_a_listener_update_does_not_restart_past_its_tunnels_spent_budget(
    tmp_path: Path,
) -> None:
    runs = Runs()
    posts = Posts()
    running, pid = COMMITS[0], 100
    passes = []
    for step in range(6):
        checkout = COMMITS[step + 1]
        listener = component("linear-listener", bot="", version=running, pid=pid)
        listener["stale_code"] = True
        before = len(runs.calls)
        actions, _, _ = run_heal(
            tmp_path, {"known": True, "code_version": checkout,
                       "components": [listener, NEVER_UP_TUNNEL]},
            now=NOW + step * 120, runs=runs, posts=posts)
        passes.append(actions)
        if len(runs.calls) > before:
            running, pid = checkout, pid + 1
    assert [call[0] for call in runs.calls] == [
        ["/srv/factory/deploy/linear-webhook-service.sh", "restart"]] * 3
    (skip,) = [a for a in passes[3] if a.label == LISTENER]
    assert skip.kind == SKIP and skip.reason.startswith("backoff: linear-webhook shared")
    (_, _, body), = posts.calls
    assert [c["label"] for c in body["components"]] == [TUNNEL]


@pytest.mark.parametrize("tunnel_first", [True, False], ids=["tunnel-first", "listener-first"])
def test_a_shared_service_in_backoff_skips_every_component_on_it(tunnel_first: bool) -> None:
    listener = component("linear-listener", bot="", version=COMMITS[0])
    items = [NEVER_UP_TUNNEL, listener] if tunnel_first else [listener, NEVER_UP_TUNNEL]
    states = {TUNNEL: KeyState(attempts=[NOW - 600, NOW - 480, NOW - 360])}
    actions = heal.decide(report(*items), states, facts(), NOW)
    assert RESTART not in [a.kind for a in actions]
    (skip,) = [a for a in actions if a.label == LISTENER]
    assert (skip.kind, skip.reason) == (
        SKIP, f"backoff: linear-webhook shared with a component in backoff "
              f"(stale code: running {COMMITS[0]}, checkout {CHECKOUT})")


def test_failed_stale_restarts_count_even_toward_new_commits(tmp_path: Path) -> None:
    runs = Runs(code=1)
    posts = Posts()
    for step in range(4):
        run_heal(tmp_path, moved(COMMITS[0], COMMITS[step + 1]), now=NOW + step * 120,
                 runs=runs, posts=posts)
    assert len(runs.calls) == 3
    assert len(saved(tmp_path)[POLLER]["failures"]) == 3
    assert log_lines(tmp_path)[-2].endswith(
        f"skip {POLLER} (backoff: 3 restarts in 30m (stale code: running {COMMITS[0]}, "
        f"checkout {COMMITS[4]}))")
    (_, _, body), = posts.calls
    assert body["components"][0]["reason"] == "backoff: 3 restarts in 30m"


def test_a_component_that_crashes_on_each_new_version_backs_off(tmp_path: Path) -> None:
    runs = Runs()
    passes = [moved(COMMITS[0], COMMITS[1]), moved(COMMITS[1], COMMITS[1], state="down"),
              moved(COMMITS[1], COMMITS[2]), moved(COMMITS[2], COMMITS[2], state="down"),
              moved(COMMITS[2], COMMITS[3]), moved(COMMITS[3], COMMITS[3], state="down"),
              moved(COMMITS[3], COMMITS[3], state="down")]
    for step, data in enumerate(passes):
        run_heal(tmp_path, data, now=NOW + step * 120, runs=runs)
    assert len(runs.calls) == 6
    assert log_lines(tmp_path)[-2].endswith(
        f"skip {POLLER} (backoff: 3 restarts in 30m (down: down))")
    assert log_lines(tmp_path)[-1].endswith(f"escalate {POLLER} (backoff: 3 restarts in 30m) escalated")


def test_a_component_held_back_restarts_once_onto_the_latest_checkout(tmp_path: Path) -> None:
    listener = "linear-listener @ box"
    busy = facts(listener_busy=True)
    for step in range(4):
        actions, runs, _ = run_heal(tmp_path, moved(COMMITS[0], COMMITS[step + 1], "linear-listener"),
                                    now=NOW + step * 120, host=busy)
        assert kinds(actions) == [(DEFER, listener)] and runs.calls == []
    actions, runs, _ = run_heal(tmp_path, moved(COMMITS[0], COMMITS[4], "linear-listener"),
                                now=NOW + 4 * 120)
    assert kinds(actions) == [(RESTART, listener)] and len(runs.calls) == 1
    (line,) = restart_lines(tmp_path)
    assert f"checkout {COMMITS[4]})" in line
    assert saved(tmp_path)[listener]["target"] == COMMITS[4]
    actions, runs, _ = run_heal(tmp_path, moved(COMMITS[4], COMMITS[4], "linear-listener", pid=9),
                                now=NOW + 5 * 120)
    assert actions == [] and runs.calls == []


@pytest.mark.parametrize("name, bot, restarted, host, expected", [
    ("poller", "", NOW - 300, facts(held=frozenset({"poller"})), [(SKIP, "held stopped")]),
    ("poller", "", NOW - 30, facts(), []),
    ("linear-listener", "", NOW - 300, facts(listener_busy=True),
     [(DEFER, "stale code, busy with a delivery")]),
    ("waiter", "codex-bot", NOW - 300, facts(),
     [(DRAIN, "stale code, waiting for the waiter to stop claiming")]),
], ids=["held", "settling", "busy-listener", "unacked-waiter"])
def test_guards_still_hold_after_free_updates(name: str, bot: str, restarted: float,
                                              host: Facts, expected: list) -> None:
    data = moved(COMMITS[1], COMMITS[2], name, bot=bot)
    states = {data["components"][0]["label"]: KeyState(restarted_at=restarted, target=COMMITS[1])}
    actions = heal.decide(data, states, host, NOW)
    assert [(a.kind, a.reason) for a in actions] == expected


def test_a_free_updates_process_change_is_not_flapping() -> None:
    states: dict[str, KeyState] = {}
    for step, pid in enumerate((10, 11, 12, 13)):
        at = NOW + step * 120
        heal.observe(report(component(pid=pid)), states, at)
        states[component()["label"]].restarted_at = at + 1
    assert states[component()["label"]].changes == []
