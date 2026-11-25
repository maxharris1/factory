from __future__ import annotations

import json
import urllib.parse

from lib.ci_lane import SEGMENTS, ci_lane, ci_popup_runs
from lib.dashboard_pages.common import (
    _GH_REPO, ENCODE_URI_COMPONENT_SAFE, _chip, _e, _span, _tile_label, checks_named, duration, ticket_page_href,
)
from lib.dashboard_static import read_asset

CSS = read_asset("css/ci.css")


SCRIPT = read_asset("js/ci.js")


_RUN = {"in_progress": ("running", "accent"), "queued": ("queued", "warn")}
_JOB = {"in_progress": ("running", "accent"), "queued": ("queued", "warn")}
_DONE = {
    "success": ("passed", "ok"), "failure": ("failed", "bad"), "timed_out": ("timed out", "bad"),
    "cancelled": ("cancelled", "bad"), "action_required": ("action required", "bad"),
    "startup_failure": ("startup failure", "bad"), "stale": ("stale", "bad"),
}
_RUNNER = {"github-hosted": "GitHub-hosted", "none": "no runner"}


def _look(job: dict) -> tuple[str, str]:
    if job["status"] in _JOB:
        return _JOB[job["status"]]
    if job["conclusion"] in _DONE:
        return _DONE[job["conclusion"]]
    return (job["conclusion"].replace("_", " ") if job["conclusion"] else "done", "muted")


def _repo(row: dict) -> str | None:
    repo = str(row["repo"])
    return repo if _GH_REPO.fullmatch(repo) else None


def _link(cls: str | None, repo: str | None, path: str, text: object) -> str:
    if repo is None:
        return ""
    attr = f' class="{cls}"' if cls else ""
    return (
        f'<a{attr} href="{_e(f"https://github.com/{repo}/{path}")}" rel="noopener noreferrer" '
        f'target="_blank">{_e(text)}</a>'
    )


def _log(repo: str | None, row: dict) -> str:
    if not (row["run_id"] and row["job_id"]):
        return ""
    return _link("ci-a log", repo, f"actions/runs/{row['run_id']}/job/{row['job_id']}", "log ↗")


def _waiting(job: dict) -> bool:
    return not job["started_at"] and job["status"] != "completed"


def _runner(job: dict) -> str:
    if _waiting(job):
        who = "waiting for runner"
    elif job["runner_kind"] == "self-hosted":
        who = "self-hosted" + (f" · {job['runner_name']}" if job["runner_name"] else "")
    else:
        who = _RUNNER.get(job["runner_kind"], "runner unknown")
    return who if job["queue_seconds"] is None else f"{who} · queued {duration(job['queue_seconds'])}"


def _job(repo: str | None, job: dict) -> str:
    text, cls = _look(job)
    live = " live" if job["status"] == "in_progress" else ""
    return (
        f'<li class="ci-job{live}"><span class="ci-dot {cls}" aria-hidden="true"></span>'
        f'{_span("ci-name", job["name"])}{_chip(text, cls)}{_span("ci-t", duration(job["elapsed_seconds"]))}'
        f'{_span("ci-runner", _runner(job))}{_log(repo, job)}</li>'
    )


def _search(name: str) -> str:
    return "issues?q=" + urllib.parse.quote(f'is:issue "{name}"', safe=ENCODE_URI_COMPONENT_SAFE)


def _failure(row: dict) -> str:
    repo = _repo(row)
    text, cls = _DONE.get(row["conclusion"], ("failed", "bad"))
    head = row.get("head_short")
    tests = ""
    if row["tests"]:
        items = "".join(
            f"<li>{_link(None, repo, _search(name), name)}</li>" if repo else f"<li>{_e(name)}</li>"
            for name in row["tests"]
        )
        more = f'<li class="more">+{row["more_tests"]} more</li>' if row["more_tests"] else ""
        tests = f'<ul class="ci-tests">{items}{more}</ul>'
    return (
        f"<li>{_chip(text, cls)}"
        f'<a class="ticket" href="{_e(ticket_page_href(row))}">{_e(_tile_label(row))}</a>'
        f'{_span("what", row["name"])}'
        + (_link("ci-a", repo, f"commit/{head}", head) if head else "")
        + _log(repo, row)
        + _span("when", f"{duration(row['age_seconds'])} ago")
        + f"{tests}</li>"
    )


CI_TINT = "#ff9e64"
CARD_STRIP_HEIGHT = 4
_BAD = "#f7768e"
CARD_STRIP = {"running": ("scan", CI_TINT), "queued": ("static", CI_TINT), "failed": ("static", _BAD)}
GROUP_LABEL = {"hosted": "GitHub-hosted", "queued": "Queued · no runner yet", "failed": "Failed · 24 h"}
SINCE = {"running": "started ", "queued": "started ", "failed": "failed "}


def _verb(name: str, seed: int) -> str:
    from lib.dashboard_pages.lanes import GLYPH_ORDER, TILE_STATE, _letters, _tone, verb_at

    spec = TILE_STATE[name]
    if not spec.get("verbs"):
        text = spec["text"]
        return f'<span class="verb{_tone(spec["tone"])}" role="img" aria-label="{_e(text)}">{_letters(text)}</span>'
    text = verb_at(name, 0, seed)
    cells = "".join('<i class="on"></i>' if q == GLYPH_ORDER[0] else "<i></i>" for q in range(4))
    return (
        f'<span class="glyph" aria-hidden="true">{cells}</span>'
        f'<span class="verb{_tone(spec["tone"])}" role="img" aria-label="{_e(text)}">{_letters(text)}</span>'
    )


def _bar(card: dict) -> str:
    p = card["progress"]
    segments = "".join(f'<i class="{name}" style="flex:{p[name]}"></i>' for name in SEGMENTS if p[name])
    return f'<div class="cibar" aria-hidden="true">{segments}</div>{_span("cibt", card["progress_text"])}'


def _card(key: str, card: dict, seed: int) -> str:
    from lib.dashboard_pages.shell import px_strip

    repo = _repo(card)
    status = card["status"]
    effect, tint = CARD_STRIP[status]
    pr, head, wait, p = card["pr"], card["head_short"], card["wait"], card["progress"]
    line1 = (
        _span("ticket", _tile_label(card))
        + (_link("ci-a", repo, f"pull/{pr}", f"PR #{pr} ↗") if pr else "")
        + (_link("ci-a", repo, f"commit/{head}", head) if head else "")
    )
    if wait:
        timing = (
            (_span("action", f"then {wait['step']}") if wait["step"] else "")
            + (_span("round", f"round {wait['round']}") if wait["round"] is not None else "")
            + _span("t", "waiting " + duration(wait["waiting_seconds"]))
        )
    else:
        timing = _span("t", SINCE[status] + duration(card["age_seconds"]) + " ago") if card["age_seconds"] is not None else ""
    line3 = (
        (_bar(card) if p["total"] else _span("what", checks_named(wait) if wait else "no checks yet"))
        + (_link("ci-a", repo, f"pull/{pr}/checks", "checks") if pr else "")
    )
    label = _tile_label(card)
    return (
        f'<div class="tile ci {status}{" bad" if status == "failed" else ""}" data-ci="{_e(key)}">'
        f'<a class="cover" href="{_e(ticket_page_href(card, open_ci=True))}" aria-label="{_e(f"CI for {label}")}"></a>'
        f'{px_strip(f"ci-{key}", effect=effect, tint=tint, seed=seed, height=CARD_STRIP_HEIGHT)}<div class="tile-body">'
        f'<div class="tile-l1">{line1}</div><div class="tile-l2">{_verb(f"ci-{status}", seed)}{timing}</div>'
        f'<div class="tile-l3">{line3}</div></div></div>'
    )


def _slot(slot: dict, lane: dict, seed: int) -> str:
    from lib.dashboard_pages.lanes import _idle_tile
    from lib.dashboard_pages.shell import px_strip

    runner, state = slot["runner"], slot["state"]
    if state == "lead":
        tile = _card(slot["card"], lane["cards"][slot["card"]], seed)
    elif state == "cont":
        text = f"↳ {_tile_label(lane['cards'][slot['card']])} · {slot['job']['name']}"
        strip = px_strip(f"ci-{runner}", effect="static", tint=CI_TINT, seed=seed, height=CARD_STRIP_HEIGHT)
        tile = f'<div class="tile empty ci-cont">{strip}{_span("lbl", text)}</div>'
    elif state == "busy":
        strip = px_strip(f"ci-{runner}", state="held", seed=seed, height=CARD_STRIP_HEIGHT)
        tile = f'<div class="tile empty px-held">{strip}<div class="lbl">busy</div></div>'
    else:
        tile = _idle_tile(f"ci-{runner}", state == "offline", seed, CARD_STRIP_HEIGHT)
    return f'<div class="ci-slot" data-runner="{_e(runner)}">{_span("ci-rn", runner)}{tile}</div>'


def _pool_text(pool: dict) -> str:
    if pool["state"] == "live":
        return "pool live"
    if pool["state"] == "stale":
        return f"pool list {duration(pool['age_seconds'])} old"
    return "pool unread"


def ci_lane_html(snapshot: dict) -> str:
    from lib.dashboard_pages.shell import px_strip

    lane = ci_lane(snapshot)
    slots = lane["slots"]
    used = sum(slot["state"] in ("lead", "cont", "busy") for slot in slots)
    meter = px_strip("meter-ci", effect="meter", tint="#9ece6a", used=used, slots=max(1, len(slots)), seed=30,
                     height=8, still=not slots)
    failed = _chip(f"{lane['failed_24h']} failed in 24 h", "bad") if lane["failed_24h"] else ""
    held = _chip(f"base red {lane['base_red']}", "warn") if lane["base_red"] else ""
    stale = _chip(f"stale {lane['stale']}", "bad") if lane["stale"] else ""
    counts = f"{lane['running']} running · {lane['queued']} queued"
    seed = len(slots)
    groups = []
    for group in lane["groups"]:
        cards = []
        for key in group["cards"]:
            seed += 1
            cards.append(_card(key, lane["cards"][key], seed))
        groups.append(
            f'<div class="ci-group" data-group="{group["name"]}">{_span("igroup", GROUP_LABEL[group["name"]])}'
            f'<div class="tiles">{"".join(cards)}</div></div>'
        )
    return (
        '<div class="lane" data-device="ci"><div class="lane-head"><div class="lh1"><span class="name">CI</span>'
        f'{failed}{held}{stale}<div class="lane-meter" aria-hidden="true" style="width:{12 * max(1, len(slots))}px">{meter}</div></div>'
        f'<div class="lh2">{_span("sub", counts)}'
        f'{_span("harness", _pool_text(lane["pool"]))}</div></div>'
        f'<div class="ci-body"><div class="tiles">{"".join(_slot(s, lane, i + 1) for i, s in enumerate(slots))}</div>'
        f'{"".join(groups)}</div></div>'
    )


def _popup_run(run: dict) -> str:
    repo = _repo(run)
    text, cls = _RUN.get(run["status"], ("other", "muted"))
    head = run.get("head_short")
    return (
        f'<div class="ci-run" data-run="{_e(head or "")}"><div class="ci-head">{_chip(text, cls)}'
        + (_link("ci-a", repo, f"commit/{head}", head) if head else "")
        + (_span("when", f"started {duration(run['age_seconds'])} ago") if run["age_seconds"] is not None else "")
        + f'</div><ul class="ci-jobs">{"".join(_job(repo, job) for job in run["jobs"])}</ul></div>'
    )


def ci_popup(snapshot: dict, address: str, row: dict) -> str:
    runs, failures = ci_popup_runs(snapshot, address)
    jobs = "".join(_popup_run(run) for run in runs) if runs else '<p class="empty">no CI queued or running</p>'
    failed = (
        f'<ul class="feed ci-fails">{"".join(_failure(f) for f in failures)}</ul>'
        if failures else '<p class="empty">no CI failures in the last 24 h</p>'
    )
    return (
        f'<dialog class="ci-pop" id="ci-pop" open="open"><div class="ci-pop-head">{_span("ci-pop-t", f"CI · {_tile_label(row)}")}'
        f'<a class="ci-x" href="{_e(ticket_page_href(row))}" aria-label="close">✕</a></div>'
        f'{jobs}<h3 class="ci-sub">Recent failures (24 h)</h3>{failed}</dialog>'
    )


def ci_extras(snapshot: dict, address: str, row: dict, open_: bool) -> str:
    runs, failures = ci_popup_runs(snapshot, address)
    if not (open_ or runs or failures):
        return ""
    button = f'<a class="ci-open" href="{_e(ticket_page_href(row, open_ci=True))}">CI</a>'
    return button + (ci_popup(snapshot, address, row) if open_ else "")


SCRIPT = SCRIPT.replace("__CI__", json.dumps(
    {"strip": CARD_STRIP, "groups": GROUP_LABEL, "since": SINCE, "segments": SEGMENTS, "tint": CI_TINT,
     "strip_h": CARD_STRIP_HEIGHT},
    ensure_ascii=False, separators=(",", ":"),
))
