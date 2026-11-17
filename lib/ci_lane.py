"""The CI lane on index.html (#823), worked out from a snapshot's ci_pool, ci_runs and tickets.

Pure: no I/O and no HTML. The server render and the page script both draw `ci_lane(snapshot)`, so
placement and the progress math live only here.

    {pool: {state, age_seconds}, running, queued, failed_24h,
     slots: [{runner, os, state: lead|cont|idle|offline|busy, card, job}],
     groups: [{name: hosted|queued|failed, cards: [key]}],
     cards: {key: {repo, ticket_id, source, tracker, pr, head_short, status: running|queued|failed,
                   age_seconds, wait: {step, round, waiting_seconds, checks} | None,
                   progress: {passed, failed, running, queued, skipped, total}, progress_text}}}

One slot per self-hosted runner: the pool's runners (factory-linux first, then by name), then any
runner a job ran on that the pool list does not have yet. A ticket's card sits in the first slot it
holds; every other runner it holds is a `cont` slot naming that job, so no ticket is drawn twice.
After the slots come the GitHub-hosted overflow (#711), tickets with no runner yet, and tickets that
failed in the last day with nothing running now.
"""

from __future__ import annotations

from lib.dispatch.types import CHECK_FAILED

SEGMENTS = ("passed", "failed", "running", "queued", "skipped")
STEP_AFTER_CI = {"ci-pending": "review", "approved-ci-pending": "merge"}
LOCAL_POOL = "factory-linux"
_REF = ("repo", "ticket_id", "source", "tracker", "pr", "head_short")


def segment(job: dict) -> str:
    if job["status"] == "in_progress":
        return "running"
    if job["status"] == "queued":
        return "queued"
    if job["conclusion"] == "success":
        return "passed"
    return "failed" if job["conclusion"] in CHECK_FAILED else "skipped"


def progress(jobs: list[dict]) -> dict:
    counts = dict.fromkeys(SEGMENTS, 0)
    for job in jobs:
        counts[segment(job)] += 1
    return {**counts, "total": len(jobs)}


def progress_text(p: dict) -> str:
    text = f"{p['passed']}/{p['total']} passed, {p['running']} running, {p['failed']} failed"
    return text + "".join(f", {p[name]} {name}" for name in ("queued", "skipped") if p[name])


def card_key(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}"


def _same(row: dict) -> tuple[str, str]:
    return (str(row["repo"]).casefold(), str(row["ticket_id"]))


def _wait(ticket: dict | None) -> dict | None:
    ci = (ticket or {}).get("ci_wait")
    if not ci:
        return None
    step = STEP_AFTER_CI.get(ci["reason"])
    return {
        "step": step,
        "round": ci.get("round") if step == "review" else None,
        "waiting_seconds": ci["waiting_seconds"],
        "checks": ci["checks"],
    }


def _card(row: dict, status: str, age: object, wait: dict | None, p: dict) -> dict:
    return {**{name: row.get(name) for name in _REF}, "status": status, "age_seconds": age, "wait": wait,
            "progress": p, "progress_text": progress_text(p)}


def _runners(pool: dict, runs: list[dict]) -> list[dict]:
    found: dict[str, dict] = {}
    for runner in pool.get("runners") or []:
        seen = found.setdefault(runner["name"], {"runner": runner["name"], "os": runner["os"], "local": False,
                                                 "online": False, "busy": False})
        seen["local"] |= LOCAL_POOL in runner["labels"]
        seen["online"] |= runner["status"] == "online"
        seen["busy"] |= runner["busy"]
    ordered = sorted(found.values(), key=lambda r: (not r["local"], r["runner"]))
    for run in runs:
        for job in run["jobs"]:
            name = job["runner_name"]
            if job["runner_kind"] == "self-hosted" and name and name not in found:
                found[name] = {"runner": name, "os": None, "local": False, "online": True, "busy": True}
                ordered.append(found[name])
    return ordered


def ci_lane(snapshot: dict) -> dict:
    ci = snapshot.get("ci_runs") or {"runs": [], "failures": []}
    pool = snapshot.get("ci_pool") or {"state": "unread", "age_seconds": None, "runners": []}
    tickets = {_same(t): t for t in snapshot.get("tickets") or []}
    cards: dict[str, dict] = {}
    holds: dict[str, tuple[str, dict]] = {}
    hosted: list[str] = []
    for run in ci["runs"]:
        key = card_key(run)
        if key in cards:
            continue
        status = "running" if run["status"] == "in_progress" else "queued"
        cards[key] = _card(run, status, run["age_seconds"], _wait(tickets.get(_same(run))), progress(run["jobs"]))
        for job in run["jobs"]:
            if job["status"] != "in_progress":
                continue
            if job["runner_kind"] == "self-hosted" and job["runner_name"]:
                holds.setdefault(job["runner_name"], (key, job))
            elif job["runner_kind"] == "github-hosted" and key not in hosted:
                hosted.append(key)
    slots, led = [], set()
    for runner in _runners(pool, ci["runs"]):
        slot = {"runner": runner["runner"], "os": runner["os"], "state": "idle", "card": None, "job": None}
        if runner["runner"] in holds:
            key, job = holds[runner["runner"]]
            slot.update(card=key, state="cont" if key in led else "lead")
            if key in led:
                slot["job"] = {"name": job["name"], "elapsed_seconds": job["elapsed_seconds"]}
            led.add(key)
        elif not runner["online"]:
            slot["state"] = "offline"
        elif runner["busy"]:
            slot["state"] = "busy"
        slots.append(slot)
    hosted = [key for key in hosted if key not in led]
    queued = [key for key in cards if key not in led and key not in hosted]
    failed_heads: dict[str, list[dict]] = {}
    for row in ci["failures"]:
        key = card_key(row)
        if key not in cards and all(f["head_short"] == row["head_short"] for f in failed_heads.get(key, [row])):
            failed_heads.setdefault(key, []).append(row)
    waits = [t for t in tickets.values() if t.get("ci_wait") and card_key(t) not in cards]
    for ticket in waits:
        if card_key(ticket) not in failed_heads:
            cards[card_key(ticket)] = _card(ticket, "queued", None, _wait(ticket), progress([]))
            queued.append(card_key(ticket))
    failed = []
    for key, rows in failed_heads.items():
        p = {**dict.fromkeys(SEGMENTS, 0), "failed": len(rows), "total": len(rows)}
        cards[key] = _card(rows[0], "failed", rows[0]["age_seconds"], _wait(tickets.get(_same(rows[0]))), p)
        failed.append(key)
    groups = [{"name": name, "cards": keys} for name, keys in
              (("hosted", hosted), ("queued", queued), ("failed", failed)) if keys]
    return {
        "pool": {"state": pool["state"], "age_seconds": pool["age_seconds"]},
        "running": sum(card["status"] == "running" for card in cards.values()),
        "queued": sum(card["status"] == "queued" for card in cards.values()),
        "failed_24h": len(failed),
        "slots": slots,
        "groups": groups,
        "cards": cards,
    }


def ci_popup_runs(snapshot: dict, address: str) -> tuple[list[dict], list[dict]]:
    """The active runs and the last day's failures of the ticket at `address` (repo#id, any case)."""
    ci = snapshot.get("ci_runs") or {"runs": [], "failures": []}
    want = address.casefold()
    return (
        [run for run in ci["runs"] if card_key(run).casefold() == want],
        [row for row in ci["failures"] if card_key(row).casefold() == want],
    )
