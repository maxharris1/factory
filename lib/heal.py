"""Self-heal (issue #107): restart down or stale-code factory components.

`deploy/self-heal.sh once` runs every two minutes (a systemd user timer, or
a loop profile.sh starts, with a cron watchdog) and pipes
`orch health --json` into `orch heal --health-json -`. Nothing here calls
an LLM: `decide` is a pure function of the health report, the saved state,
this host's facts and the time, and each restart runs the component's own
service script with a fixed argv:

* poller: deploy/poller-service.sh restart (which runs deploy/run-poller.sh);
* waiter: deploy/waiter-service.sh <bot> restart;
* Linear listener and tunnel: deploy/linear-webhook-service.sh restart.

Self-heal never sends a signal itself. The waiter service stops only its
supervisor and that bot's `orch wait --autostart`, and each job's
`orch job run` runs in its own session, so a restart never stops a job.
The service scripts get this environment minus the Factory webhook and
Linear key variables.

Per component (its health label), in order:

1. Only a component on this device that the registry expects and whose
   service this account owns (an autostart flag in ~/.config/factory:
   poller-autostart, waiter-autostart-<bot>, linear-webhook-autostart) is
   considered, so one account never acts on another user's services.
2. A component held stopped (its *.stopped marker) is skipped: self-heal
   never overrides an operator's stop.
2a. A tunnel down because another owner holds its port on the tunnel host
   (health's "port held by another owner", issue #427) is not restarted:
   a restart cannot win the port. It escalates once when it enters that
   state, then is skipped quietly until it leaves it.
3. A component restarted under SETTLE_SECONDS ago is left to come up.
4. Down: restart. Stale code (its heartbeat names another commit than the
   checkout): restart once idle. The Linear listener is busy while a
   delivery is under REQUEST_DEADLINE_SECONDS old; the poller (at its cycle
   boundary) and a waiter (outside a claim) stop at a safe point themselves.
5. Backoff: at most MAX_ATTEMPTS restarts per WINDOW_SECONDS, then skipped
   until the oldest attempt leaves the window.
6. Escalation, once per component per window: the backoff tripped, the
   restart command failed MAX_ATTEMPTS times, or the component flaps
   (FLAP_CHANGES process changes in the window that self-heal did not
   cause). It POSTs a short JSON summary to the Factory webhook (orch.yml
   grok.webhook_url_env); the URL and auth values come from the environment
   and are never logged.

Drain (#219, #309): a waiter running stale code (and not down) is
restarted once it has stopped claiming. Its marker waiter-<bot>.drain,
naming the checkout's commit, makes the waiter stop claiming
(lib/waiter.py); the waiter then writes waiter-<bot>.drain.ack naming the
same commit, from a step that has already decided not to claim, so any
earlier claim has started and been recorded. The restart does not wait for
running jobs: they run in their own session, and the replacement waiter
adopts the remote ones as after a crash. The ack holds across a respawn: a
waiter on the marked commit honours an acknowledged marker too
(lib/waiter.py), so its replacement cannot claim before the restart.
Writing a new marker removes the old ack. A pass that finds a waiter
without an ack waits up to ACK_POLLS * ACK_POLL_SECONDS for it, so a
waiter that acknowledges in that time restarts in the same pass. A drain
longer than
DRAIN_ESCALATE_SECONDS escalates once per window and kills nothing. The
marker and ack of owned waiters no longer draining are removed, and the
waiter's service start removes both.

Each restart and escalation is one line in self-heal.log; a skip, a
deferral or a drain is logged only when a component's decision changes.
State is self-heal.state.json beside it, written atomically.

`orch heal deploy` (lib/self_deploy.py, #219) is the step before: it moves
the checkout to origin/main, so this pass sees the stale code. `orch heal
devices` (lib/device_deploy.py, #297) then brings each remote device's
factory checkout to the same commit.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from lib import health
from lib.grok_delivery import GrokDeliveryError, WebhookResult, authorization, send_webhook
from lib.linear_webhook import REQUEST_DEADLINE_SECONDS
from lib.machines_yml import orch_host

ROOT = Path(__file__).resolve().parent.parent

MAX_ATTEMPTS = 3
WINDOW_SECONDS = 30 * 60
SETTLE_SECONDS = 60
FLAP_CHANGES = 3
COMMAND_TIMEOUT = 900
POLLER_STOP_WAIT = "300"
LOG_NAME = "self-heal.log"
STATE_NAME = "self-heal.state.json"

POLLER_SERVICE = "poller"
LISTENER_SERVICE = "linear-webhook"

RESTART = "restart"
SKIP = "skip"
DEFER = "defer"
DRAIN = "drain"
ESCALATE = "escalate"
DRAIN_ESCALATE_SECONDS = 2 * 60 * 60
ACK_POLLS = 20
ACK_POLL_SECONDS = 0.5

Runner = Callable[[Sequence[str], Mapping[str, str], Path], int]
Poster = Callable[[str, str, bytes, float], WebhookResult]


@dataclass(frozen=True)
class Facts:
    """This host: its device, the services this account owns and holds
    stopped, and whether the Linear listener is mid-delivery."""

    device: str
    owned: frozenset[str] = frozenset()
    held: frozenset[str] = frozenset()
    listener_busy: bool = False
    # When each bot's drain marker was written (its mtime).
    drain_since: Mapping[str, float] = field(default_factory=dict)
    # The commit each bot's waiter acknowledged draining for, when its ack
    # matches the marker.
    drain_acked: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    kind: str
    label: str
    service: str
    reason: str
    state: str = ""
    attempts: int = 0
    flapping: bool = False


@dataclass
class KeyState:
    attempts: list[float] = field(default_factory=list)
    failures: list[float] = field(default_factory=list)
    changes: list[float] = field(default_factory=list)
    process: list | None = None
    observed_at: float | None = None
    escalated_at: float | None = None
    last: str | None = None
    alerted: str | None = None


def service_of(component: str, bot: str) -> str | None:
    """The service id that restarts a component, or None (the dashboard
    writer and device probes have none)."""
    if component == health.POLLER:
        return POLLER_SERVICE
    if component == health.WAITER and bot in health.OWNING_BOTS:
        return f"waiter:{bot}"
    if component in (health.LISTENER, health.TUNNEL):
        return LISTENER_SERVICE
    return None


def service_argv(service: str, deploy: Path) -> list[str]:
    if service == POLLER_SERVICE:
        return [str(deploy / "poller-service.sh"), "restart"]
    if service == LISTENER_SERVICE:
        return [str(deploy / "linear-webhook-service.sh"), "restart"]
    bot = service.split(":", 1)[1]
    return [str(deploy / "waiter-service.sh"), bot, "restart"]


def service_log(service: str, state_dir: Path) -> Path:
    if service == POLLER_SERVICE:
        return state_dir / "poller-service.log"
    if service == LISTENER_SERVICE:
        return state_dir / "linear-webhook-service.log"
    return state_dir / f"waiter-{service.split(':', 1)[1]}-service.log"


def port_held(item: Mapping) -> bool:
    return item.get("state") == health.DOWN and str(item.get("reason") or "").startswith(health.PORT_HELD)


def _recent(times: Sequence[float], now: float) -> list[float]:
    return [t for t in times if now - t < WINDOW_SECONDS]


def observe(report: Mapping, states: dict[str, KeyState], now: float) -> None:
    """Record each component's process (pid, started_at); a change that no
    self-heal restart since the last look explains counts toward flapping."""
    for item in report.get("components") or ():
        if item.get("pid") is None:
            continue
        key = states.setdefault(item["label"], KeyState())
        process = [item["pid"], item.get("started_at")]
        if key.process is not None and key.process != process:
            since = key.observed_at if key.observed_at is not None else now
            if not any(at >= since for at in key.attempts):
                key.changes.append(now)
        key.process = process
        key.observed_at = now
        key.changes = _recent(key.changes, now)


def decide(
    report: Mapping, states: Mapping[str, KeyState], facts: Facts, now: float
) -> list[Action]:
    """What to do for each component this account may act on (module rules)."""
    actions: list[Action] = []
    if not report.get("known"):
        return actions
    for item in report.get("components") or ():
        service = service_of(item.get("component", ""), item.get("bot", ""))
        if (
            service is None
            or item.get("device") != facts.device
            or not item.get("configured", True)
            or service not in facts.owned
        ):
            continue
        label = item["label"]
        key = states.get(label, KeyState())
        attempts = _recent(key.attempts, now)

        def act(kind: str, reason: str, *, flapping: bool = False) -> Action:
            return Action(kind, label, service, reason, item.get("state", ""),
                          len(attempts), flapping)

        if item.get("state") == health.DOWN:
            need = f"down: {item.get('reason') or 'down'}"
        elif item.get("stale_code"):
            need = f"stale code: running {item.get('version')}, checkout {report.get('code_version')}"
        else:
            need = None
        if service in facts.held:
            if need is not None:
                actions.append(act(SKIP, "held stopped"))
            continue
        if port_held(item):
            if key.alerted != item["reason"]:
                actions.append(act(ESCALATE, item["reason"]))
            actions.append(act(SKIP, f"not restarting: {item['reason']}"))
            continue
        escalation = _escalation(key, attempts, now, broken=need is not None)
        if escalation is not None:
            actions.append(act(ESCALATE, escalation, flapping=escalation.startswith("flapping")))
        if need is None or (attempts and now - max(attempts) < SETTLE_SECONDS):
            continue
        if item.get("state") != health.DOWN and service == LISTENER_SERVICE and facts.listener_busy:
            actions.append(act(DEFER, "stale code, busy with a delivery"))
            continue
        if item.get("state") != health.DOWN and service.startswith("waiter:"):
            bot = service.split(":", 1)[1]
            acked = bool(report.get("code_version")) and (
                facts.drain_acked.get(bot) == str(report.get("code_version")))
            if not acked:
                actions.append(act(DRAIN, "stale code, waiting for the waiter to stop claiming"))
                since = facts.drain_since.get(bot)
                if (escalation is None and since is not None
                        and now - since >= DRAIN_ESCALATE_SECONDS
                        and (key.escalated_at is None
                             or now - key.escalated_at >= WINDOW_SECONDS)):
                    actions.append(act(ESCALATE, f"draining for {int((now - since) // 60)}m, "
                                                 "waiting for the waiter to stop claiming"))
                continue
        if len(attempts) >= MAX_ATTEMPTS:
            actions.append(act(SKIP, f"backoff: {len(attempts)} restarts in "
                                     f"{WINDOW_SECONDS // 60}m ({need})"))
            continue
        actions.append(act(RESTART, need))
    return actions


def _escalation(key: KeyState, attempts: list[float], now: float, *, broken: bool) -> str | None:
    """Why to escalate now, if at all: once per window; backoff and failing
    restarts only while the component still needs one, flapping always."""
    if key.escalated_at is not None and now - key.escalated_at < WINDOW_SECONDS:
        return None
    if broken and len(attempts) >= MAX_ATTEMPTS:
        return f"backoff: {len(attempts)} restarts in {WINDOW_SECONDS // 60}m"
    failures = _recent(key.failures, now)
    if broken and len(failures) >= MAX_ATTEMPTS:
        return f"restart failing: {len(failures)} failed restarts in {WINDOW_SECONDS // 60}m"
    changes = _recent(key.changes, now)
    if len(changes) >= FLAP_CHANGES:
        return f"flapping: {len(changes)} process changes in {WINDOW_SECONDS // 60}m"
    return None


# --- state and log ---------------------------------------------------------------------------


def load_state(path: Path, log: Callable[[str], None]) -> dict[str, KeyState]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            label: KeyState(**{k: v for k, v in value.items() if k in KeyState.__dataclass_fields__})
            for label, value in data["keys"].items()
        }
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        log(f"state {path.name} unreadable; starting fresh")
        return {}


def save_state(path: Path, states: Mapping[str, KeyState]) -> None:
    body = json.dumps(
        {"keys": {label: vars(key) for label, key in sorted(states.items())}},
        sort_keys=True,
    )
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(body + "\n", encoding="utf-8")
    os.replace(temp, path)


def drain_path(state_dir: Path, bot: str) -> Path:
    """The marker that tells a bot's waiter to stop claiming (lib/waiter.py)."""
    return state_dir / f"waiter-{bot}.drain"


def drain_ack_path(state_dir: Path, bot: str) -> Path:
    """Where the waiter acknowledges the drain, after its last claim."""
    return state_dir / f"waiter-{bot}.drain.ack"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _mark_drain(state_dir: Path, bot: str, version: str) -> None:
    """Write the drain marker naming the checkout's commit; an unchanged
    marker keeps its mtime, which dates the drain, and its ack. A new
    marker drops the old ack first, so only an ack written after it counts."""
    path = drain_path(state_dir, bot)
    if _read(path) == version:
        return
    drain_ack_path(state_dir, bot).unlink(missing_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(version + "\n", encoding="utf-8")
    os.replace(temp, path)


def stamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


# --- running ---------------------------------------------------------------------------------


def run_service(argv: Sequence[str], env: Mapping[str, str], log_path: Path) -> int:
    """Run one service script with its output in its own service log."""
    with open(log_path, "ab") as out:
        try:
            return subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=out,
                                  stderr=subprocess.STDOUT, env=dict(env),
                                  timeout=COMMAND_TIMEOUT, check=False).returncode
        except subprocess.TimeoutExpired:
            return 124
        except OSError:
            return 126


@dataclass(frozen=True)
class Webhook:
    """Where an escalation goes: env variable names, never values."""

    url_env: str = "FACTORY_WEBHOOK_URL"
    auth_env: str | None = "FACTORY_WEBHOOK_AUTH"
    key_env: str = "FACTORY_WEBHOOK_KEY"
    linear_key_env: str | None = None

    @property
    def secret_names(self) -> tuple[str, ...]:
        names = (self.url_env, self.auth_env, self.key_env, self.linear_key_env)
        return tuple(name for name in names if name)


def escalate(
    actions: Sequence[Action],
    *,
    device: str,
    now: float,
    webhook: Webhook,
    environ: Mapping[str, str],
    post: Poster = send_webhook,
) -> str:
    """POST one escalation; the outcome as a log word (never a URL or value)."""
    url = environ.get(webhook.url_env)
    if not url:
        return f"escalation skipped: missing-env:{webhook.url_env}"
    window = int(now // WINDOW_SECONDS * WINDOW_SECONDS)
    body = json.dumps(
        {
            "action": "self-heal",
            "kind": "escalation",
            "logical_key": f"self-heal:{device}:{actions[0].label}:{window}",
            "target_bot": "grok-bot",
            "device": device,
            "components": [
                {"label": a.label, "state": a.state, "reason": a.reason,
                 "attempts": a.attempts, "flapping": a.flapping}
                for a in actions
            ],
            "summary": "; ".join(f"{a.label}: {a.reason}" for a in actions),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        result = post(url, authorization(environ, auth_env=webhook.auth_env,
                                         key_env=webhook.key_env), body, 8.0)
    except GrokDeliveryError as exc:
        return f"escalation failed: {exc.detail}"
    except Exception as exc:
        return f"escalation failed: {type(exc).__name__}"
    return "escalated" if result.status == 200 else f"escalation failed: HTTP-{result.status}"


def heal(
    report: Mapping,
    *,
    facts: Facts,
    state_dir: Path,
    deploy: Path,
    webhook: Webhook,
    environ: Mapping[str, str],
    now: float,
    run: Runner = run_service,
    post: Poster = send_webhook,
    clock: Callable[[], float] = time.monotonic,
    dry_run: bool = False,
    refresh: Callable[[], Facts] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Action]:
    """One self-heal pass: observe, decide, act, log, save."""
    log_path = state_dir / LOG_NAME

    def log(line: str) -> None:
        with open(log_path, "a", encoding="utf-8") as out:
            out.write(f"{stamp(now)} self-heal: {line}\n")

    states = load_state(state_dir / STATE_NAME, log)
    observe(report, states, now)
    actions = decide(report, states, facts, now)
    version = str(report.get("code_version") or "unknown")
    if not dry_run:
        for action in actions:
            if action.kind == DRAIN:
                _mark_drain(state_dir, action.service.split(":", 1)[1], version)
        unacked = {a.service.split(":", 1)[1] for a in actions if a.kind == DRAIN}
        if unacked and refresh is not None:
            for _ in range(ACK_POLLS):
                sleep(ACK_POLL_SECONDS)
                facts = refresh()
                if all(facts.drain_acked.get(bot) == version for bot in unacked):
                    break
            actions = decide(report, states, facts, now)
    child_env = {k: v for k, v in environ.items() if k not in webhook.secret_names}
    done: dict[str, int] = {}
    draining = {a.service.split(":", 1)[1] for a in actions if a.kind == DRAIN}
    for action in actions:
        key = states.setdefault(action.label, KeyState())
        if action.kind in (SKIP, DEFER, DRAIN):
            note = f"{action.kind} {action.label} ({action.reason})"
            if key.last != note:
                log(note)
            key.last = note
            continue
        if action.kind == ESCALATE:
            continue
        key.last = None
        if dry_run:
            log(f"would restart {action.label} ({action.reason})")
            continue
        if action.service not in done:
            env = dict(child_env)
            if action.service == POLLER_SERVICE:
                env["ORCH_POLL_STOP_WAIT"] = POLLER_STOP_WAIT
            started = clock()
            code = run(service_argv(action.service, deploy), env,
                       service_log(action.service, state_dir))
            done[action.service] = code
            log(f"restart {action.label} ({action.reason}) exit={code} "
                f"{clock() - started:.1f}s")
        else:
            log(f"restart {action.label} ({action.reason}) with {action.service}")
        key.attempts = _recent(key.attempts, now) + [now]
        if done[action.service] != 0:
            key.failures = _recent(key.failures, now) + [now]
    if report.get("known") and not dry_run:
        for service in facts.owned:
            bot = service.split(":", 1)[1] if service.startswith("waiter:") else None
            if bot is not None and bot not in draining:
                drain_path(state_dir, bot).unlink(missing_ok=True)
                drain_ack_path(state_dir, bot).unlink(missing_ok=True)
    acted = {action.label for action in actions}
    held_port = {item.get("label") for item in report.get("components") or () if port_held(item)}
    for label, key in states.items():
        if label not in acted:
            key.last = None
        if report.get("known") and label not in held_port:
            key.alerted = None
    escalations = [a for a in actions if a.kind == ESCALATE]
    if escalations and not dry_run:
        outcome = escalate(escalations, device=facts.device, now=now, webhook=webhook,
                           environ=environ, post=post)
        for action in escalations:
            key = states.setdefault(action.label, KeyState())
            key.escalated_at = now
            if action.label in held_port and outcome == "escalated":
                key.alerted = action.reason
            log(f"escalate {action.label} ({action.reason}) {outcome}")
    for key in states.values():
        key.attempts = _recent(key.attempts, now)
        key.failures = _recent(key.failures, now)
    save_state(state_dir / STATE_NAME, states)
    return actions


# --- orch heal -------------------------------------------------------------------------------

USAGE = """\
orch heal --health-json PATH|- [--state DIR] [--deploy DIR] [--config DIR]
          [--machines PATH] [--orch PATH] [--device NAME] [--dry-run]
orch heal deploy [--state DIR] [--checkout DIR] [--machines PATH] [--orch PATH]
                 [--device NAME] [--dry-run]
orch heal devices [--state DIR] [--db PATH] [--checkout DIR] [--machines PATH]
                  [--orch PATH] [--dry-run]
"""


def facts_for(device: str, config_dir: Path, state_dir: Path, db: Path | None,
              now: float) -> Facts:
    owned: set[str] = set()
    held: set[str] = set()
    if (config_dir / "poller-autostart").is_file():
        owned.add(POLLER_SERVICE)
    if (config_dir / "linear-webhook-autostart").is_file():
        owned.add(LISTENER_SERVICE)
    drains: dict[str, float] = {}
    acked: dict[str, str] = {}
    for bot in health.OWNING_BOTS:
        if (config_dir / f"waiter-autostart-{bot}").is_file():
            owned.add(f"waiter:{bot}")
            try:
                drains[bot] = drain_path(state_dir, bot).stat().st_mtime
            except OSError:
                pass
            ack = _read(drain_ack_path(state_dir, bot))
            if ack is not None and ack == _read(drain_path(state_dir, bot)):
                acked[bot] = ack
        if (state_dir / f"waiter-{bot}.stopped").exists():
            held.add(f"waiter:{bot}")
    if (state_dir / "poller.stopped").exists():
        held.add(POLLER_SERVICE)
    if (state_dir / "linear-webhook.stopped").exists():
        held.add(LISTENER_SERVICE)
    return Facts(device, frozenset(owned), frozenset(held), _listener_busy(db, now),
                 drains, acked)


def _listener_busy(db: Path | None, now: float) -> bool:
    if db is None or not db.is_file():
        return False
    try:
        connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT last_received_at FROM linear_webhook_health WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0] is not None and now - float(row[0]) < REQUEST_DEADLINE_SECONDS)


def _webhook(orch_path: Path | None) -> Webhook:
    from lib.orch_yml import load_orch_yml

    path = orch_path if orch_path is not None else ROOT / "orch.yml"
    if not path.is_file():
        return Webhook()
    config, _ = load_orch_yml(path, allow_placeholders=True)
    if config is None:
        return Webhook()
    grok = config.grok
    linear_key = config.linear.api_key_env if config.linear is not None else None
    if grok is None:
        return Webhook(linear_key_env=linear_key)
    return Webhook(grok.webhook_url_env, grok.webhook_auth_env, grok.webhook_key_env, linear_key)


def main(argv: list[str], *, stdin: TextIO | None = None,
         clock: Callable[[], float] = time.time) -> int:
    if argv[:1] == ["deploy"]:
        from lib import self_deploy

        return self_deploy.main(argv[1:], clock=clock)
    if argv[:1] == ["devices"]:
        from lib import device_deploy

        return device_deploy.main(argv[1:], clock=clock)
    parser = argparse.ArgumentParser(prog="orch heal", usage=USAGE)
    parser.add_argument("--health-json", required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--deploy", type=Path, default=ROOT / "deploy")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--machines", type=Path)
    parser.add_argument("--orch", type=Path)
    parser.add_argument("--device")
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    home = Path.home()
    state_dir = args.state or Path(os.environ.get("ORCH_STATE_DIR") or home / ".local/state/factory")
    config_dir = args.config or home / ".config/factory"
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        if args.health_json == "-":
            text = (stdin if stdin is not None else sys.stdin).read()
        else:
            text = Path(args.health_json).read_text(encoding="utf-8")
        report = json.loads(text)
        if not isinstance(report, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        with open(state_dir / LOG_NAME, "a", encoding="utf-8") as out:
            out.write(f"{stamp(clock())} self-heal: health report unreadable "
                      f"({type(exc).__name__}); nothing done\n")
        return 1
    now = clock()
    db = os.environ.get("ORCH_DB")
    device = args.device or orch_host(health.load_machines(args.machines))
    db_path = Path(db) if db else None
    heal(
        report,
        facts=facts_for(device, config_dir, state_dir, db_path, now),
        refresh=lambda: facts_for(device, config_dir, state_dir, db_path, now),
        state_dir=state_dir,
        deploy=args.deploy,
        webhook=_webhook(args.orch),
        environ=os.environ,
        now=now,
        dry_run=args.dry_run,
    )
    return 0
