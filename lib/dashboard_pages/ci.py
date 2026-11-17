"""The CI section on index.html (#717): in-flight runs per ticket head and the
recent failures, from status.json `ci_runs` (lib/dashboard.py `_ci_runs`)."""

from __future__ import annotations

import urllib.parse

from lib.dashboard_pages.common import (
    _GH_REPO, ENCODE_URI_COMPONENT_SAFE, _chip, _e, _panel, _span, _tile_label, duration, ticket_page_href,
)

CSS = """\
#hud #ci-runs{grid-column:1/-1}
#ci-runs h2{flex-wrap:wrap;gap:4px 8px}
.ci-runs{display:flex;flex-direction:column;gap:10px}
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
                 startup_failure: ['startup failure', 'bad']};
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
  function ciRun(r) {
    var repo = ciRepo(r);
    var look = CI_RUN[r.status] || ['other', 'muted'];
    var box = el('div', 'ci-run');
    var key = ticketAddress(r) + '@' + (r.head_short || '');
    box.setAttribute('data-run', key);
    var head = add(box, el('div', 'ci-head'));
    add(head, link('ticket', ticketPageHref(r))).textContent = tileLabel(r);
    if (r.pr) ciLink(head, 'ci-a', repo, 'pull/' + r.pr, 'PR #' + r.pr + ' ↗');
    if (r.head_short) ciLink(head, 'ci-a', repo, 'commit/' + r.head_short, r.head_short);
    add(head, chip(look[0], look[1]));
    if (r.age_seconds !== null && r.age_seconds !== undefined) ticking(add(head, el('span', 'when')), r.age_seconds, 'started ', ' ago');
    var ul = add(box, el('ul', 'ci-jobs'));
    r.jobs.forEach(function (j) { add(ul, ciJob(repo, j)); });
    mark('ci-runs', key, box);
    return box;
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
  function renderCiRuns(d) {
    var ci = d.ci_runs || {runs: [], failures: []};
    var running = ci.runs.filter(function (r) { return r.status === 'in_progress'; }).length;
    var root = panel('ci-runs', 'CI', running + ' running · ' + (ci.runs.length - running) + ' queued');
    if (!root) return;
    if (!ci.runs.length) add(root, el('p', 'empty', 'no CI queued or running'));
    else {
      var runs = add(root, el('div', 'ci-runs'));
      ci.runs.forEach(function (r) { add(runs, ciRun(r)); });
    }
    add(root, el('h3', 'ci-sub', 'Recent failures (24 h)'));
    if (!ci.failures.length) { add(root, el('p', 'empty', 'no CI failures in the last 24 h')); return; }
    var ul = add(root, el('ul', 'feed ci-fails'));
    ci.failures.forEach(function (f) { add(ul, ciFail(f)); });
  }
"""


_RUN = {"in_progress": ("running", "accent"), "queued": ("queued", "warn")}
_JOB = {"in_progress": ("running", "accent"), "queued": ("queued", "warn")}
_DONE = {
    "success": ("passed", "ok"), "failure": ("failed", "bad"), "timed_out": ("timed out", "bad"),
    "cancelled": ("cancelled", "bad"), "action_required": ("action required", "bad"),
    "startup_failure": ("startup failure", "bad"),
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


def _key(row: dict) -> str:
    return f"{row['repo']}#{row['ticket_id']}@{row['head_short'] or ''}"


def _run(run: dict) -> str:
    repo = _repo(run)
    text, cls = _RUN.get(run["status"], ("other", "muted"))
    pr = run.get("pr")
    head = run.get("head_short")
    return (
        f'<div class="ci-run" data-run="{_e(_key(run))}"><div class="ci-head">'
        f'<a class="ticket" href="{_e(ticket_page_href(run))}">{_e(_tile_label(run))}</a>'
        + (_link("ci-a", repo, f"pull/{pr}", f"PR #{pr} ↗") if pr else "")
        + (_link("ci-a", repo, f"commit/{head}", head) if head else "")
        + _chip(text, cls)
        + (_span("when", f"started {duration(run['age_seconds'])} ago") if run["age_seconds"] is not None else "")
        + f'</div><ul class="ci-jobs">{"".join(_job(repo, job) for job in run["jobs"])}</ul></div>'
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


def _ci_section(snapshot: dict) -> str:
    ci = snapshot.get("ci_runs") or {"runs": [], "failures": []}
    running = sum(run["status"] == "in_progress" for run in ci["runs"])
    runs = (
        f'<div class="ci-runs">{"".join(_run(run) for run in ci["runs"])}</div>'
        if ci["runs"] else '<p class="empty">no CI queued or running</p>'
    )
    failures = (
        f'<ul class="feed ci-fails">{"".join(_failure(row) for row in ci["failures"])}</ul>'
        if ci["failures"] else '<p class="empty">no CI failures in the last 24 h</p>'
    )
    inner = f'{runs}<h3 class="ci-sub">Recent failures (24 h)</h3>{failures}'
    return _panel("ci-runs", "CI", inner, count=f"{running} running · {len(ci['runs']) - running} queued")
