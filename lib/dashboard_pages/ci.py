from __future__ import annotations

import json
import urllib.parse

from lib.ci_lane import SEGMENTS, ci_lane, ci_popup_runs
from lib.dashboard_pages.common import (
    _GH_REPO, ENCODE_URI_COMPONENT_SAFE, _chip, _e, _span, _tile_label, checks_named, duration, ticket_page_href,
)

CSS = """\
.ci-body{flex:1 1 400px;min-width:0;display:flex;flex-direction:column;gap:8px}
.ci-body .tiles{flex:none}
.ci-slot{display:flex;flex-direction:column;gap:4px;min-width:0}
.ci-slot>.tile{flex:1 0 auto}
.ci-rn{font:11px/1.2 var(--mono);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ci-group{display:flex;flex-direction:column;gap:6px}
.ci-group .igroup{margin:6px 0 0}
.tile.ci{position:relative}
.tile.ci .cover{position:absolute;inset:0;z-index:0}
.tile.ci:hover,.tile.ci:focus-within{border-color:var(--brand)}
.tile.ci.bad{border-color:var(--bad)}
.tile.ci .tile-body{position:relative;pointer-events:none}
.tile.ci .tile-body a{position:relative;z-index:1;pointer-events:auto}
.tile.ci .tile-l4{flex-wrap:wrap;gap:4px 8px}
.tile.ci .tile-l4 .what{min-width:0;overflow-wrap:anywhere;color:var(--text-2)}
.cibar{flex:1 1 100%;display:flex;gap:1px;height:4px;background:var(--line-hi)}
.cibar i{display:block;min-width:2px}
.cibar .passed{background:var(--ok)}.cibar .failed{background:var(--bad)}.cibar .running{background:var(--accent)}
.cibar .queued{background:var(--warn)}.cibar .skipped{background:var(--muted)}
.cibt{color:var(--text-2);font-variant-numeric:tabular-nums}
.tile.empty.ci-cont .lbl{padding:0 8px;text-align:center;letter-spacing:0;color:var(--text-2);overflow-wrap:anywhere}
.ci-open{display:inline-block;margin:12px 0 0;padding:4px 10px;border:1px solid var(--line-hi);
font:600 12px/1.4 var(--mono);color:var(--text);text-decoration:none}
.ci-open:hover{border-color:var(--brand)}
.ci-pop{position:fixed;inset:5vh 0 auto;z-index:50;width:min(920px,calc(100vw - 24px));max-height:90vh;overflow:auto;
margin:0 auto;padding:12px 14px;border:1px solid var(--line-hi);background:var(--panel);color:var(--text);box-shadow:var(--elev)}
.ci-pop-head{display:flex;align-items:baseline;gap:8px;margin:0 0 8px}
.ci-pop-t{font:600 14px/1.25 var(--mono)}
.ci-x{margin-left:auto;font:600 16px/1 var(--mono);color:var(--muted);text-decoration:none}
.ci-x:hover{color:var(--text)}
.ci-run{min-width:0;padding:10px 12px;background:var(--bg);box-shadow:var(--elev)}
.ci-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;margin:0 0 6px}
.ci-head .ticket{min-width:0;overflow-wrap:anywhere;font:600 13px/1.25 var(--mono);color:var(--text);text-decoration:none}
.ci-head .ticket:hover{color:var(--brand)}
.ci-a{font:500 12px/1.5 var(--mono);color:var(--accent);text-decoration:none;white-space:nowrap}
.ci-a:hover{text-decoration:underline}
.ci-head .when,.ci-fails .when{margin-left:auto;font:12px/1.2 var(--mono);color:var(--muted);white-space:nowrap}
.ci-jobs{margin:0;padding:0;list-style:none}
.ci-job{display:grid;grid-template-columns:8px minmax(0,2fr) 96px 72px minmax(0,3fr) 44px;
grid-template-areas:"dot name chip t runner log";align-items:baseline;gap:2px 12px;padding:6px 0;
border-top:1px solid var(--line);font:12px/1.4 var(--mono)}
.ci-dot{grid-area:dot;width:7px;height:7px;background:currentColor;align-self:center}
.ci-name{grid-area:name;min-width:0;overflow-wrap:anywhere;color:var(--text)}
.ci-job .chip{grid-area:chip;justify-self:start}
.ci-t{grid-area:t;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-2);white-space:nowrap}
.ci-runner{grid-area:runner;min-width:0;overflow-wrap:anywhere;color:var(--muted)}
.ci-job .log{grid-area:log}
.ci-sub{margin:16px 0 4px;font:11px/1.3 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.ci-fails .what{min-width:0;overflow-wrap:anywhere;color:var(--text-2);font:12px/1.4 var(--mono)}
.ci-fails .ticket{color:var(--text);text-decoration:none}
.ci-tests{flex:1 0 100%;display:flex;flex-direction:column;gap:2px;margin:2px 0 0;padding:0 0 0 12px;list-style:none}
.feed .ci-tests li{display:block;padding:0;border:0;background:none}
.ci-tests a{min-width:0;overflow-wrap:anywhere;font:12px/1.5 var(--mono);color:var(--text-2);text-underline-offset:3px}
.ci-tests a:hover{color:var(--text)}
.ci-tests .more{margin:0}
@keyframes ci-pulse{50%{opacity:.3}}
@media (prefers-reduced-motion: no-preference){.ci-job.live .ci-dot{animation:ci-pulse 1.6s var(--ease) infinite}}
@media (max-width:640px){.ci-run{padding:10px}
.ci-job{grid-template-columns:8px auto minmax(0,1fr) auto;grid-template-areas:"dot name name chip" ". t runner log";
gap:2px 8px}
.ci-job .chip{justify-self:end}
.ci-t{text-align:left}}
"""


SCRIPT = r"""
  var CI_RUN = {in_progress: ['running', 'accent'], queued: ['queued', 'warn']};
  var CI_JOB = {in_progress: ['running', 'accent'], queued: ['queued', 'warn']};
  var CI_DONE = {success: ['passed', 'ok'], failure: ['failed', 'bad'], timed_out: ['timed out', 'bad'],
                 cancelled: ['cancelled', 'bad'], action_required: ['action required', 'bad'],
                 startup_failure: ['startup failure', 'bad'], stale: ['stale', 'bad']};
  var CI_RUNNER = {'github-hosted': 'GitHub-hosted', none: 'no runner'};
  function ciLook(j) {
    if (CI_JOB[j.status]) return CI_JOB[j.status];
    if (CI_DONE[j.conclusion]) return CI_DONE[j.conclusion];
    return [j.conclusion ? String(j.conclusion).replace(/_/g, ' ') : 'done', 'muted'];
  }
  function ciRepo(r) { var repo = String(r.repo); return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) ? repo : null; }
  function ciLink(parent, cls, repo, path, text) {
    if (!repo) return;
    var a = add(parent, link(cls, 'https://github.com/' + repo + '/' + path));
    a.setAttribute('rel', 'noopener noreferrer');
    a.setAttribute('target', '_blank');
    a.textContent = text;
  }
  function ciLog(parent, repo, x) {
    if (x.run_id && x.job_id) ciLink(parent, 'ci-a log', repo, 'actions/runs/' + x.run_id + '/job/' + x.job_id, 'log ↗');
  }
  function ciRunner(j) {
    if (!j.started_at && j.status !== 'completed') return 'waiting for runner';
    if (j.runner_kind === 'self-hosted') return 'self-hosted' + (j.runner_name ? ' · ' + j.runner_name : '');
    return CI_RUNNER[j.runner_kind] || 'runner unknown';
  }
  function ciJob(repo, j) {
    var look = ciLook(j);
    var li = el('li', 'ci-job' + (j.status === 'in_progress' ? ' live' : ''));
    add(li, el('span', 'ci-dot ' + look[1])).setAttribute('aria-hidden', 'true');
    add(li, el('span', 'ci-name', j.name));
    add(li, chip(look[0], look[1]));
    var t = add(li, el('span', 'ci-t'));
    if (j.status === 'in_progress') ticking(t, j.elapsed_seconds);
    else t.textContent = dur(j.elapsed_seconds);
    var runner = add(li, el('span', 'ci-runner'));
    var who = ciRunner(j);
    if (j.queue_seconds === null || j.queue_seconds === undefined) runner.textContent = who;
    else if (!j.started_at && j.status !== 'completed') ticking(runner, j.queue_seconds, who + ' · queued ');
    else runner.textContent = who + ' · queued ' + dur(j.queue_seconds);
    ciLog(li, repo, j);
    return li;
  }
  function ciFail(f) {
    var repo = ciRepo(f);
    var look = CI_DONE[f.conclusion] || ['failed', 'bad'];
    var li = el('li');
    add(li, chip(look[0], look[1]));
    add(li, link('ticket', ticketPageHref(f))).textContent = tileLabel(f);
    add(li, el('span', 'what', f.name));
    if (f.head_short) ciLink(li, 'ci-a', repo, 'commit/' + f.head_short, f.head_short);
    ciLog(li, repo, f);
    ticking(add(li, el('span', 'when')), f.age_seconds, '', ' ago');
    if (f.tests.length) {
      var tests = add(li, el('ul', 'ci-tests'));
      f.tests.forEach(function (name) {
        var item = add(tests, el('li'));
        if (repo) ciLink(item, null, repo, 'issues?q=' + encodeURIComponent('is:issue "' + name + '"'), name);
        else item.textContent = name;
      });
      if (f.more_tests) add(tests, el('li', 'more', '+' + f.more_tests + ' more'));
    }
    return li;
  }
  var CI = __CI__;
  function ciVerb(l2, name, seed) {
    var spec = LANES.tiles[name];
    var verb;
    if (spec.verbs) {
      var glyph = add(l2, el('span', 'glyph'));
      glyph.setAttribute('aria-hidden', 'true');
      var cells = [];
      for (var q = 0; q < 4; q++) cells.push(add(glyph, el('i', q === LANES.glyph[0] ? 'on' : null)));
      verb = add(l2, el('span', 'verb ' + spec.tone));
      verb.setAttribute('role', 'img');
      letters(verb, verbAt(name, 0, seed));
      tileAnims.push({state: name, seed: seed, cells: cells, verb: verb});
    } else {
      verb = add(l2, el('span', 'verb' + (spec.tone ? ' ' + spec.tone : '')));
      verb.setAttribute('role', 'img');
      letters(verb, spec.text);
    }
  }
  function ciCardEl(key, c, seed) {
    var repo = ciRepo(c);
    var look = CI.strip[c.status];
    var card = el('div', 'tile ci ' + c.status + (c.status === 'failed' ? ' bad' : ''));
    card.setAttribute('data-ci', key);
    add(card, link('cover', ticketPageHref(c, true))).setAttribute('aria-label', 'CI for ' + tileLabel(c));
    add(card, PX.strip('ci-' + key, {effect: look[0], tint: look[1], seed: seed, height: 16}));
    var bodyEl = add(card, el('div', 'tile-body'));
    var l1 = add(bodyEl, el('div', 'tile-l1'));
    add(l1, el('span', 'ticket', tileLabel(c)));
    if (c.pr) ciLink(l1, 'ci-a', repo, 'pull/' + c.pr, 'PR #' + c.pr + ' ↗');
    if (c.head_short) ciLink(l1, 'ci-a', repo, 'commit/' + c.head_short, c.head_short);
    ciVerb(add(bodyEl, el('div', 'tile-l2')), 'ci-' + c.status, seed);
    var l3 = add(bodyEl, el('div', 'tile-l3'));
    var w = c.wait;
    if (w) {
      if (w.step) add(l3, el('span', 'action', 'then ' + w.step));
      if (w.round !== null && w.round !== undefined) add(l3, el('span', 'round', 'round ' + w.round));
      ticking(add(l3, el('span', 't')), w.waiting_seconds, 'waiting ');
    } else if (c.age_seconds !== null && c.age_seconds !== undefined) {
      ticking(add(l3, el('span', 't')), c.age_seconds, CI.since[c.status], ' ago');
    }
    var l4 = add(bodyEl, el('div', 'tile-l4'));
    var p = c.progress;
    if (p.total) {
      var bar = add(l4, el('div', 'cibar'));
      bar.setAttribute('aria-hidden', 'true');
      CI.segments.forEach(function (n) { if (p[n]) add(bar, el('i', n)).setAttribute('style', 'flex:' + p[n]); });
      add(l4, el('span', 'cibt', c.progress_text));
    } else add(l4, el('span', 'what', w ? checksNamed(w) : 'no checks yet'));
    if (c.pr) ciLink(l4, 'ci-a', repo, 'pull/' + c.pr + '/checks', 'checks');
    return mark('lanes', 'ci-' + key, card);
  }
  function ciSlotEl(s, lane, seed) {
    var box = el('div', 'ci-slot');
    box.setAttribute('data-runner', s.runner);
    add(box, el('span', 'ci-rn', s.runner));
    var tile;
    if (s.state === 'lead') add(box, ciCardEl(s.card, lane.cards[s.card], seed));
    else if (s.state === 'cont') {
      tile = add(box, el('div', 'tile empty ci-cont'));
      add(tile, PX.strip('ci-' + s.runner, {effect: 'static', tint: CI.tint, seed: seed, height: 16}));
      add(tile, el('span', 'lbl', '↳ ' + tileLabel(lane.cards[s.card]) + ' · ' + s.job.name));
    } else if (s.state === 'busy') {
      tile = add(box, el('div', 'tile empty px-held'));
      add(tile, PX.strip('ci-' + s.runner, {state: 'held', seed: seed, height: 16}));
      add(tile, el('div', 'lbl', 'busy'));
    } else add(box, idleTile('ci-' + s.runner, s.state === 'offline', seed));
    return box;
  }
  function ciPoolText(pool) {
    if (pool.state === 'live') return 'pool live';
    if (pool.state === 'stale') return 'pool list ' + dur(pool.age_seconds) + ' old';
    return 'pool unread';
  }
  function ciLaneEl(lane) {
    var row = el('div', 'lane');
    row.setAttribute('data-device', 'ci');
    var head = add(row, el('div', 'lane-head'));
    var lh1 = add(head, el('div', 'lh1'));
    add(lh1, el('span', 'name', 'CI'));
    if (lane.failed_24h) add(lh1, chip(lane.failed_24h + ' failed in 24 h', 'bad'));
    var n = Math.max(1, lane.slots.length);
    var used = lane.slots.filter(function (s) { return s.state === 'lead' || s.state === 'cont' || s.state === 'busy'; }).length;
    var meter = add(lh1, el('div', 'lane-meter'));
    meter.setAttribute('aria-hidden', 'true');
    meter.setAttribute('style', 'width:' + (12 * n) + 'px');
    add(meter, PX.strip('meter-ci', {effect: 'meter', tint: '#9ece6a', used: used, slots: n, seed: 30, height: 8,
                                     still: !lane.slots.length}));
    var lh2 = add(head, el('div', 'lh2'));
    add(lh2, el('span', 'sub', lane.running + ' running · ' + lane.queued + ' queued'));
    add(lh2, el('span', 'harness', ciPoolText(lane.pool)));
    var bodyEl = add(row, el('div', 'ci-body'));
    var grid = add(bodyEl, el('div', 'tiles'));
    lane.slots.forEach(function (s, i) { add(grid, ciSlotEl(s, lane, i + 1)); });
    var seed = lane.slots.length;
    lane.groups.forEach(function (g) {
      var box = add(bodyEl, el('div', 'ci-group'));
      box.setAttribute('data-group', g.name);
      add(box, el('span', 'igroup', CI.groups[g.name]));
      var tiles = add(box, el('div', 'tiles'));
      g.cards.forEach(function (key) { seed += 1; add(tiles, ciCardEl(key, lane.cards[key], seed)); });
    });
    return row;
  }
  function ciPopRuns(d, address) {
    var ci = d.ci_runs || {runs: [], failures: []};
    var want = String(address).toLowerCase();
    function mine(r) { return ticketAddress(r).toLowerCase() === want; }
    return [ci.runs.filter(mine), ci.failures.filter(mine)];
  }
  function ciPopRun(r) {
    var repo = ciRepo(r);
    var look = CI_RUN[r.status] || ['other', 'muted'];
    var box = el('div', 'ci-run');
    box.setAttribute('data-run', r.head_short || '');
    var head = add(box, el('div', 'ci-head'));
    add(head, chip(look[0], look[1]));
    if (r.head_short) ciLink(head, 'ci-a', repo, 'commit/' + r.head_short, r.head_short);
    if (r.age_seconds !== null && r.age_seconds !== undefined) ticking(add(head, el('span', 'when')), r.age_seconds, 'started ', ' ago');
    var ul = add(box, el('ul', 'ci-jobs'));
    r.jobs.forEach(function (j) { add(ul, ciJob(repo, j)); });
    return box;
  }
  var CI_CLOSE = null;
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && CI_CLOSE && byId('ci-pop')) location.replace(CI_CLOSE);
  });
  function ciPopup(d, address, row) {
    var found = ciPopRuns(d, address);
    var dlg = el('dialog', 'ci-pop');
    dlg.setAttribute('id', 'ci-pop');
    dlg.setAttribute('open', 'open');
    var head = add(dlg, el('div', 'ci-pop-head'));
    add(head, el('span', 'ci-pop-t', 'CI · ' + tileLabel(row)));
    CI_CLOSE = ticketPageHref(row);
    var x = add(head, link('ci-x', CI_CLOSE));
    x.setAttribute('aria-label', 'close');
    x.textContent = '✕';
    if (!found[0].length) add(dlg, el('p', 'empty', 'no CI queued or running'));
    found[0].forEach(function (r) { add(dlg, ciPopRun(r)); });
    add(dlg, el('h3', 'ci-sub', 'Recent failures (24 h)'));
    if (!found[1].length) add(dlg, el('p', 'empty', 'no CI failures in the last 24 h'));
    else {
      var ul = add(dlg, el('ul', 'feed ci-fails'));
      found[1].forEach(function (f) { add(ul, ciFail(f)); });
    }
    return dlg;
  }
  function ciExtras(view, d, address, row, open) {
    var found = ciPopRuns(d, address);
    if (!found[0].length && !found[1].length) return;
    add(view, link('ci-open', ticketPageHref(row, true))).textContent = 'CI';
    if (open) add(view, ciPopup(d, address, row));
  }
"""


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
        line3 = (
            (_span("action", f"then {wait['step']}") if wait["step"] else "")
            + (_span("round", f"round {wait['round']}") if wait["round"] is not None else "")
            + _span("t", "waiting " + duration(wait["waiting_seconds"]))
        )
    else:
        line3 = _span("t", SINCE[status] + duration(card["age_seconds"]) + " ago") if card["age_seconds"] is not None else ""
    line4 = (
        (_bar(card) if p["total"] else _span("what", checks_named(wait) if wait else "no checks yet"))
        + (_link("ci-a", repo, f"pull/{pr}/checks", "checks") if pr else "")
    )
    label = _tile_label(card)
    return (
        f'<div class="tile ci {status}{" bad" if status == "failed" else ""}" data-ci="{_e(key)}">'
        f'<a class="cover" href="{_e(ticket_page_href(card, ci=True))}" aria-label="{_e(f"CI for {label}")}"></a>'
        f'{px_strip(f"ci-{key}", effect=effect, tint=tint, seed=seed, height=16)}<div class="tile-body">'
        f'<div class="tile-l1">{line1}</div><div class="tile-l2">{_verb(f"ci-{status}", seed)}</div>'
        f'<div class="tile-l3">{line3}</div><div class="tile-l4">{line4}</div></div></div>'
    )


def _slot(slot: dict, lane: dict, seed: int) -> str:
    from lib.dashboard_pages.lanes import _idle_tile
    from lib.dashboard_pages.shell import px_strip

    runner, state = slot["runner"], slot["state"]
    if state == "lead":
        tile = _card(slot["card"], lane["cards"][slot["card"]], seed)
    elif state == "cont":
        text = f"↳ {_tile_label(lane['cards'][slot['card']])} · {slot['job']['name']}"
        strip = px_strip(f"ci-{runner}", effect="static", tint=CI_TINT, seed=seed, height=16)
        tile = f'<div class="tile empty ci-cont">{strip}{_span("lbl", text)}</div>'
    elif state == "busy":
        strip = px_strip(f"ci-{runner}", state="held", seed=seed, height=16)
        tile = f'<div class="tile empty px-held">{strip}<div class="lbl">busy</div></div>'
    else:
        tile = _idle_tile(f"ci-{runner}", state == "offline", seed)
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
        f'{failed}<div class="lane-meter" aria-hidden="true" style="width:{12 * max(1, len(slots))}px">{meter}</div></div>'
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
    """The CI dialog on ticket.html#t=<address>&ci=1 (ciPopup in the script): that ticket's per-job rows
    and its failures in the last day. ✕ links back to the ticket without &ci=1."""
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
    """The ticket page's CI button, and the CI dialog when the hash carries ci=1."""
    runs, failures = ci_popup_runs(snapshot, address)
    if not (runs or failures):
        return ""
    button = f'<a class="ci-open" href="{_e(ticket_page_href(row, ci=True))}">CI</a>'
    return button + (ci_popup(snapshot, address, row) if open_ else "")


SCRIPT = SCRIPT.replace("__CI__", json.dumps(
    {"strip": CARD_STRIP, "groups": GROUP_LABEL, "since": SINCE, "segments": SEGMENTS, "tint": CI_TINT},
    ensure_ascii=False, separators=(",", ":"),
))
