"""settings.html (Settings, #777): one card per machines.yml device with its
enabled, slot_max, harnesses, held slots, drain state and its recent change
results from the settings audit log. Signed in through the #563 flow
(`ghAuth` in common.py), each card gets a toggle for enabled and drain and a
slot_max input that POST /api/settings; the orch host applies the queued
request (lib/settings_apply.py)."""

from __future__ import annotations

import json
import math
import time

from lib.account_requests import ADD_REFUSED
from lib.dashboard_pages.common import _chip, _e, _panel, _span, duration
from lib.dashboard_static import read_asset

RESULT_TONE = {"applied": "ok", "pending": "warn", "ok": "ok", "signed-out": "muted", "mismatch": "warn",
               "expired": "warn", "cancelled": "muted", "refused": "warn", "failed": "bad", "superseded": "muted",
               "other": "muted"}
RESULTS_PER_CARD = 5
QUEUED = "queued (applies within ~2 min)"
READ_ONLY = "read-only: sign-in is not available here"
WAITING = "waiting for the poller's next cycle"
NO_CHANGES = "no changes yet"
SIGN_IN_TONE = {"ok": "ok", "mismatch": "warn", "signed-out": "bad", "unreachable": "warn"}
UNCHECKED = "unchecked"
READING = {"ok": ("read {} ago", "ac-when"), "stale": ("stale, read {} ago", "warn"),
           "no reading": ("no reading", "ac-when")}
ACCOUNT_READ_ONLY = {"default": "read-only: default account", "external": "read-only: external account"}
NOT_SIGNED_IN = "not signed in on any device"
NO_ACCOUNTS = "no accounts: machines.yml lists none"
ADD_HARNESSES = ("claude", "codex", "cursor", "gemini")
SIGNIN_HARNESSES = ("claude", "codex")
SIGNIN_REMIND = "Paste a code only from a sign-in you just started here. Never paste a code someone sent you."


def drain_state(row: dict) -> tuple[str, str]:
    if not row.get("drain"):
        return "off", ""
    held = row.get("held") or 0
    return (f"draining, {held} running", "warn") if held else ("drained", "ok")


def device_facts(row: dict) -> list[tuple[str, str, str]]:
    slot_max = "—" if row.get("slot_max") is None else str(row["slot_max"])
    return [
        ("enabled", "yes" if row.get("enabled") else "no", "" if row.get("enabled") else "warn"),
        ("slot_max", slot_max, ""),
        ("harnesses", ", ".join(row.get("harnesses") or []) or "none", ""),
        ("slots", f"{row.get('held') or 0} of {slot_max} held", ""),
        ("drain", *drain_state(row)),
    ]


def _recent(snapshot: dict, keep) -> list[dict]:
    rows = (snapshot.get("settings") or {}).get("results") or []
    return [r for r in reversed(rows) if keep(r)][:RESULTS_PER_CARD]


def device_results(snapshot: dict, name: str) -> list[dict]:
    return _recent(snapshot, lambda r: r.get("device") == name and r.get("target") != "account")


def account_results(snapshot: dict, account_id: str) -> list[dict]:
    return _recent(snapshot, lambda r: r.get("target") == "account" and r.get("account") == account_id)


def percent(value: float | None) -> str:
    return "—" if value is None else f"{math.floor(value * 100 + 0.5)}%"


def headroom_fact(account: dict) -> tuple[str, str]:
    headroom, floor = account.get("headroom"), account["floor"]
    if headroom is not None and headroom < floor:
        return f"{percent(headroom)} · below floor {percent(floor)}", "bad"
    return f"{percent(headroom)} · floor {percent(floor)}", ""


def running_line(account: dict) -> str:
    jobs = account.get("running") or []
    return ", ".join(
        (f"{job['ticket']} " if job.get("ticket") else "") + f"{job['kind']} on {job['device']}" for job in jobs
    ) or "none"


def usage_facts(account: dict) -> list[tuple[str, str, str]]:
    if account.get("not_tracked"):
        return [("usage", f"not tracked: {account['not_tracked']}", "muted")]
    return [
        ("5h used", percent(account.get("five_hour_used")), ""),
        ("7d used", percent(account.get("seven_day_used")), ""),
        ("headroom", *headroom_fact(account)),
    ]


def account_facts(account: dict) -> list[tuple[str, str, str]]:
    return [
        ("harness", account["harness"], ""),
        *([("plan", account["plan"], "")] if account.get("plan") else []),
        ("max_jobs", "—" if account.get("max_jobs") is None else str(account["max_jobs"]), ""),
        ("paused", "yes" if account.get("paused") else "no", "warn" if account.get("paused") else ""),
        *usage_facts(account),
        ("running", running_line(account), ""),
    ]


def sign_in_label(entry: dict) -> str:
    login = entry.get("login")
    return f"sign in as {login} on {entry['device']}: " if login else f"sign in on {entry['device']}: "


def _value(value: object) -> str:
    if value is None:
        return "—"
    return json.dumps(value) if isinstance(value, bool) else str(value)


def result_line(row: dict) -> str:
    where = f" on {row['device']}" if row.get("target") == "account" and row.get("device") else ""
    return (f"{row.get('field') or '—'}{where} {_value(row.get('previous'))} → {_value(row.get('new'))}"
            f" · {row.get('actor') or '—'} · {row.get('applied_at') or row.get('requested_at') or '—'}")


def status_line(row: dict) -> tuple[str, str] | None:
    if row.get("result") == "superseded":
        return "st-sup", f"superseded by {row.get('superseded_by')}"
    if row.get("result") != "pending":
        return None
    if row.get("waiting"):
        return "st-err", WAITING
    if not row.get("pending"):
        return None
    return "st-err", "restart pending: " + ", ".join(row["pending"])


def _class(cls: str) -> str:
    return f' class="{cls}"' if cls else ""


def _facts(facts: list[tuple[str, str, str]]) -> str:
    rows = "".join(f"<div><dt>{_e(label)}</dt><dd{_class(cls)}>{_e(text)}</dd></div>" for label, text, cls in facts)
    return f'<dl class="st-facts">{rows}</dl>'


def _results(results: list[dict]) -> str:
    if not results:
        return f'<p class="empty">{_e(NO_CHANGES)}</p>'
    return '<ul class="st-results">' + "".join(
        '<li class="st-res">' + _chip(r["result"], RESULT_TONE.get(r["result"], "muted"))
        + _span("st-what", result_line(r))
        + (_span("st-err", r["error"]) if r.get("error") else "")
        + (_span(*status) if (status := status_line(r)) else "") + "</li>"
        for r in results
    ) + "</ul>"


def _card(snapshot: dict, row: dict) -> str:
    where = _chip("local · orch host", "ok") if row.get("local") else _chip("remote")
    return (
        f'<article class="st-card" data-device="{_e(row["name"])}">'
        f'<h3>{_span("st-name", row["name"])}{where}</h3>'
        f'{_facts(device_facts(row))}{_results(device_results(snapshot, row["name"]))}</article>'
    )


def _settings(snapshot: dict) -> str:
    slots = snapshot.get("slots") or {}
    devices = slots.get("devices") or []
    inner = f'<p class="st-note">{_e(READ_ONLY)}</p>'
    if devices:
        inner += '<div class="st-grid">' + "".join(_card(snapshot, row) for row in devices) + "</div>"
    else:
        inner += f'<p class="empty">no devices: machines.yml {_e(slots.get("machines_yml") or "absent")}</p>'
    return _panel("settings", "Devices", inner, count=f"{len(devices)} devices")


def _sign_in(entry: dict) -> str:
    status = entry.get("status")
    checked = entry.get("checked_at")
    same = entry.get("same_login_as") or []
    command = entry.get("command")
    read = READING.get(entry.get("reading"))
    return (
        f'<li class="ac-dev" data-device="{_e(entry["device"])}">{_span("ac-name", entry["device"])}'
        + _chip(status or UNCHECKED, SIGN_IN_TONE.get(status, "muted"))
        + _span(None, entry.get("auth_method")) + _span(None, entry.get("subscription_type"))
        + (_span("ac-when", "checked " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(checked)))
           if checked is not None else "")
        + (_span(read[1], read[0].format(duration(entry.get("reading_age")))) if read else "")
        + (_span(None, "same login as " + ", ".join(same)) if same else "")
        + (f'<span class="ac-sign">{_e(sign_in_label(entry))}<code class="ac-cmd">{_e(command)}</code></span>'
           if command is not None else "")
        + "</li>"
    )


def _account_card(snapshot: dict, account: dict) -> str:
    reason = ACCOUNT_READ_ONLY.get(account.get("read_only"))
    entries = account.get("signed_in_on") or []
    devices = (
        '<ul class="ac-devs">' + "".join(_sign_in(entry) for entry in entries) + "</ul>"
        if entries else f'<p class="empty">{_e(NOT_SIGNED_IN)}</p>'
    )
    return (
        f'<article class="ac-card" data-account="{_e(account["id"])}">'
        f'<h3>{_span("st-name", account["id"])}{_chip(reason) if reason else ""}</h3>'
        f'{_facts(account_facts(account))}{devices}{_results(account_results(snapshot, account["id"]))}</article>'
    )


def _accounts(snapshot: dict) -> str:
    accounts = snapshot.get("accounts") or []
    inner = (
        '<div class="st-grid">' + "".join(_account_card(snapshot, account) for account in accounts) + "</div>"
        if accounts else f'<p class="empty">{_e(NO_ACCOUNTS)}</p>'
    )
    return _panel("accounts", "Accounts", inner, count=f"{len(accounts)} accounts")


CSS = read_asset("css/settings.css")


_SCRIPT = read_asset("js/settings.js")
SCRIPT = _SCRIPT.replace("__ST__", json.dumps(
    {"tone": RESULT_TONE, "perCard": RESULTS_PER_CARD, "queued": QUEUED, "readOnly": READ_ONLY,
     "waiting": WAITING, "noChanges": NO_CHANGES, "signIn": SIGN_IN_TONE, "unchecked": UNCHECKED,
     "reading": READING, "accountReadOnly": ACCOUNT_READ_ONLY, "notSignedIn": NOT_SIGNED_IN, "noAccounts": NO_ACCOUNTS,
     "harnesses": ADD_HARNESSES, "signinHarnesses": SIGNIN_HARNESSES, "remind": SIGNIN_REMIND,
     "addRefused": dict(ADD_REFUSED)}
))
