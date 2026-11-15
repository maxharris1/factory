"""job.html#job-<id>: one job's article, with the path its ticket took through the
statechart (model/chart.mmd, see lib/dashboard_flow.py) and the full chart template."""

from __future__ import annotations

from lib.dashboard_flow import render_path_svg, render_svg
from lib.dashboard_telemetry import job_block
from lib.dashboard_pages.common import (
    anchor, _bot, _chip, duration, _e, _job_kind, _job_label, _span, _state_chip,
)
from lib.dashboard_pages.tickets import _active_jobs


CSS = """\
#jobview{display:grid;gap:16px}
.jobpage .row{margin:0 0 8px}
.jobpage .where{margin:8px 0;color:var(--text-2)}.jobpage .where b{color:var(--accent);font-family:var(--mono)}
.jobpage .path{margin:4px 0 10px;font:12px/1.5 var(--mono);color:var(--muted);overflow-wrap:anywhere}
.jobpage:target{box-shadow:0 0 0 1px var(--brand)}
#jobview:has(.jobpage:target) .jobpage:not(:target){display:none}
.flowbox{overflow-x:auto;margin:8px 0 10px;padding:6px 0;background:var(--field);box-shadow:var(--elev)}
.flow{display:block;font:500 11px/1 var(--mono)}
.flow text{fill:var(--text);text-anchor:middle}
.flow .gloss{fill:var(--muted);font-size:9px;font-weight:400}
.flow .node rect{fill:var(--panel);stroke:var(--g,var(--line-hi));stroke-width:1.5}
.flow .node:not(.visited){opacity:.55}
.flow .node.end rect{stroke-width:2.5}
.flow .node.visited rect{fill:var(--panel-hi);stroke-width:2.5}
.flow .node.current rect{fill:var(--sel);stroke:var(--accent);stroke-width:4}
.flow .node.current text{fill:var(--accent)}
.flow .edge path,.flow .entry path{fill:none;stroke:var(--line-hi);stroke-width:1.4}
.flow .edge polygon,.flow .entry polygon,.flow .entry circle{fill:var(--line-hi)}
.flow .edge.taken path,.flow .entry.taken path{stroke:var(--accent);stroke-width:2.6}
.flow .edge.taken polygon,.flow .entry.taken polygon,.flow .entry.taken circle{fill:var(--accent)}
.g-blue{--g:var(--codex)}.g-purple{--g:var(--cursor)}.g-orange{--g:var(--claude)}.g-yellow{--g:var(--warn)}
.g-teal{--g:var(--ok)}.g-tan{--g:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin:0;font:12px/1.5 var(--mono);color:var(--muted)}
.legend .key{color:var(--g,var(--muted))}
.legend .key::before{content:"";display:inline-block;width:9px;height:9px;margin-right:6px;border:2px solid currentColor}
.legend .key.path{color:var(--accent)}
.legend .key.now{color:var(--accent)}.legend .key.now::before{background:var(--accent)}
.legend .key.next::before{border-style:dashed;border-color:var(--line-hi)}
.flow.steps .visit .box{fill:var(--panel-hi);stroke:var(--g,var(--line-hi));stroke-width:2}
.flow.steps .visit.current .box{fill:var(--sel);stroke:var(--accent);stroke-width:3}
.flow.steps .visit.current text{fill:var(--accent)}
.flow.steps .halo{fill:none;stroke:var(--accent);stroke-width:4;opacity:.35}
.flow.steps .meta{fill:var(--text);font-size:10px}
.flow.steps .next .box{fill:none;stroke:var(--line-hi);stroke-width:1.5;stroke-dasharray:5 4}
.flow.steps .next text{fill:var(--muted)}
.flow.steps .hop path{fill:none;stroke:var(--accent);stroke-width:2}
.flow.steps .hop polygon{fill:var(--accent)}
.flow.steps .hop.jump path{stroke-dasharray:2 4}
.flow.steps .hop.next path{stroke:var(--line-hi);stroke-width:1.4;stroke-dasharray:5 4}
.flow.steps .hop.next polygon{fill:var(--line-hi)}
.flow .visit.breathe .halo{animation:breathe 2.6s ease-in-out infinite}
.fullchart{margin-top:6px}
"""


SCRIPT = r"""
  function walk(node, fn) {
    fn(node);
    var kids = node.children || [];
    for (var i = 0; i < kids.length; i++) walk(kids[i], fn);
  }
  function classes(node, drop, extra) {
    var kept = String(node.getAttribute('class') || '').split(/\s+/).filter(function (c) {
      return c && drop.indexOf(c) < 0;
    });
    node.setAttribute('class', kept.concat(extra).join(' '));
  }
  // The diagram is the server-rendered statechart (model/chart.mmd) in
  // #flow-template; a job gets a copy with its path and state marked.
  function highlight(svg, flow) {
    var path = flow.path || [];
    var seen = {};
    var taken = {};
    path.forEach(function (s, i) {
      seen[s] = true;
      if (i) taken[path[i - 1] + '\n' + s] = true;
    });
    walk(svg, function (n) {
      if (!n.getAttribute) return;
      var state = n.getAttribute('data-state');
      var from = n.getAttribute('data-from');
      var cls = String(n.getAttribute('class') || '');
      if (state !== null) {
        classes(n, ['visited', 'current'], (seen[state] ? ['visited'] : []).concat(state === flow.state ? ['current'] : []));
      } else if (from !== null) {
        classes(n, ['taken'], taken[from + '\n' + n.getAttribute('data-to')] ? ['taken'] : []);
      } else if (/(^|\s)entry(\s|$)/.test(cls)) {
        classes(n, ['taken'], path.length ? ['taken'] : []);
      }
    });
    return svg;
  }

  // --- the simplified flow: the path taken, now, what can come next ----------

  function fmt(v) { return String(Math.round(v * 10) / 10); }
  function svgNode(ns, tag, attrs) {
    var node = document.createElementNS(ns, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, String(attrs[k])); });
    return node;
  }
  function arrowPoints(x, y, cx, cy) {
    var dx = x - cx, dy = y - cy;
    var len = Math.sqrt(dx * dx + dy * dy) || 1;
    var ux = dx / len, uy = dy / len, a = FLOW.arrow;
    var bx = x - ux * a * 1.6, by = y - uy * a * 1.6;
    return [[x, y], [bx - uy * a * 0.7, by + ux * a * 0.7], [bx + uy * a * 0.7, by - ux * a * 0.7]]
      .map(function (p) { return fmt(p[0]) + ',' + fmt(p[1]); }).join(' ');
  }
  // Mirrors render_path_svg: one node per visit, left to right, arrows in
  // that order; the current step highlighted (breathing unless the job is
  // finished); the chart's direct successors of it grayed, dashed.
  function trailSvg(ns, chart, flow, meta, label) {
    var path = flow.path || [];
    var at = flow.state ? path.lastIndexOf(flow.state) : -1;
    var nexts = at >= 0 ? (flow.next || []) : [];
    var group = {};
    (chart.states || []).forEach(function (st) { group[st.name] = st.group; });
    var labels = {};
    (chart.edges || []).forEach(function (e) { labels[e.from + '\n' + e.to] = e.label; });
    var step = FLOW.w + FLOW.hop;
    var rows = Math.max(1, nexts.length);
    var y0 = FLOW.top + (rows - 1) * FLOW.row / 2;
    var mid = y0 + FLOW.h / 2;
    var width = 2 * FLOW.left + Math.max(1, path.length + (nexts.length ? 1 : 0)) * step - FLOW.hop;
    var height = 2 * FLOW.top + (rows - 1) * FLOW.row + FLOW.h;
    var svg = svgNode(ns, 'svg', {'class': 'flow steps', viewBox: '0 0 ' + fmt(width) + ' ' + fmt(height),
                                  width: fmt(width), height: fmt(height), role: 'img', 'aria-label': label});
    function title(g, text) { add(g, svgNode(ns, 'title')).textContent = text; }
    function hopTitle(a, b) {
      var key = a + '\n' + b;
      if (!(key in labels)) return a + ' → ' + b + ' (no direct transition on the chart)';
      return a + ' → ' + b + (labels[key] ? ': ' + labels[key] : '');
    }
    function lines(g, x, y, name, extra) {
      var texts = [['', name]];
      if (FLOW.gloss[name]) texts.push(['gloss', FLOW.gloss[name]]);
      if (extra) texts.push(['meta', extra]);
      var bases = {1: [FLOW.h / 2 + 4], 2: [21, 36], 3: [17, 30, 43]}[texts.length];
      return texts.map(function (t, i) {
        var node = add(g, svgNode(ns, 'text', {x: fmt(x + FLOW.w / 2), y: fmt(y + bases[i])}));
        if (t[0]) node.setAttribute('class', t[0]);
        node.textContent = t[1];
        return node;
      });
    }
    for (var i = 1; i < path.length; i++) {
      var a = path[i - 1], b = path[i];
      var x1 = FLOW.left + (i - 1) * step + FLOW.w, x2 = FLOW.left + i * step;
      var hop = add(svg, svgNode(ns, 'g', {'class': (a + '\n' + b) in labels ? 'hop' : 'hop jump',
                                           'data-from': a, 'data-to': b, 'data-step': i}));
      title(hop, hopTitle(a, b));
      add(hop, svgNode(ns, 'path', {d: 'M' + fmt(x1) + ' ' + fmt(mid) + ' L' + fmt(x2 - FLOW.arrow * 1.6) + ' ' + fmt(mid)}));
      add(hop, svgNode(ns, 'polygon', {points: arrowPoints(x2, mid, x1, mid)}));
    }
    var sx = FLOW.left + at * step + FLOW.w;
    var nx = FLOW.left + path.length * step;
    nexts.forEach(function (name, j) {
      var ny = FLOW.top + j * FLOW.row + FLOW.h / 2;
      var bend = (nx - sx) / 2;
      var hop = add(svg, svgNode(ns, 'g', {'class': 'hop next', 'data-from': flow.state, 'data-to': name}));
      title(hop, hopTitle(flow.state, name));
      add(hop, svgNode(ns, 'path', {d: 'M' + fmt(sx) + ' ' + fmt(mid) + ' C' + fmt(sx + bend) + ' ' + fmt(mid) + ' ' +
        fmt(nx - bend) + ' ' + fmt(ny) + ' ' + fmt(nx - FLOW.arrow * 1.6) + ' ' + fmt(ny)}));
      add(hop, svgNode(ns, 'polygon', {points: arrowPoints(nx, ny, nx - bend, ny)}));
    });
    var metaNode = null;
    path.forEach(function (name, i) {
      var x = FLOW.left + i * step;
      var cls = 'visit' + (i === at ? ' current' + (flow.terminal ? '' : ' breathe') : '');
      var g = add(svg, svgNode(ns, 'g', {'class': cls, 'data-state': name, 'data-step': i}));
      title(g, name + (i === at ? ' (now)' : ''));
      add(g, svgNode(ns, 'rect', {x: fmt(x), y: fmt(y0), width: FLOW.w, height: FLOW.h, rx: 6,
                                  'class': 'box g-' + (group[name] || 'other')}));
      if (i === at) {
        add(g, svgNode(ns, 'rect', {'class': 'halo', x: fmt(x - 4), y: fmt(y0 - 4), width: FLOW.w + 8,
                                    height: FLOW.h + 8, rx: 9}));
      }
      var texts = lines(g, x, y0, name, i === at ? meta.text : '');
      if (i === at && meta.text) metaNode = texts[texts.length - 1];
    });
    nexts.forEach(function (name, j) {
      var x = nx, y = FLOW.top + j * FLOW.row;
      var g = add(svg, svgNode(ns, 'g', {'class': 'next', 'data-state': name}));
      title(g, name + ' (can come next)');
      add(g, svgNode(ns, 'rect', {x: fmt(x), y: fmt(y), width: FLOW.w, height: FLOW.h, rx: 6, 'class': 'box'}));
      lines(g, x, y, name, '');
    });
    // The time in state ticks like every other age on the page.
    if (metaNode && meta.age !== null) ticking(metaNode, meta.age, meta.round ? meta.round + ' · ' : '');
    return svg;
  }

  function jobArticle(r, state, d) {
    var art = el('article', 'panel jobpage');
    art.setAttribute('id', anchor(r.job));
    var head = add(art, el('h2'));
    add(head, el('span', null, jobLabel(r)));
    add(head, el('span', 'count', state + ' · job ' + r.job));
    var row = add(art, el('div', 'row'));
    add(row, bot(r.recipient));
    var kind = jobKind(r);
    if (kind) add(row, el('span', 'kind', kind));
    add(row, el('span', 'ticket', jobLabel(r)));
    add(row, el('span', 'what', 'step ' + r.action));
    if (state === 'running') ticking(add(row, el('span', 'muted')), r.elapsed_seconds, 'T+ ');
    else ticking(add(row, el('span', 'muted')), r.age_seconds, 'waiting ');
    var jobState = stateChip(r);
    if (jobState) add(row, jobState);
    if (r.overdue) add(row, chip('OVERDUE', 'bad'));
    if (r.retries) add(row, chip('retried ' + r.retries + 'x'));
    var flow = r.flow || {state: null, path: [], state_age_seconds: null};
    var where = add(art, el('p', 'where'));
    add(where, el('span', null, 'now '));
    add(where, el('b', null, flow.state || 'state not recorded'));
    add(where, el('span', null, ' · round ' + (r.round === null || r.round === undefined ? '—' : r.round)));
    if (flow.state && flow.state_age_seconds !== null && flow.state_age_seconds !== undefined) {
      add(where, el('span', null, ' · '));
      ticking(add(where, el('span')), flow.state_age_seconds, '', ' in ' + flow.state);
    }
    add(art, el('p', 'path', 'path: ' + ((flow.path || []).length ? flow.path.join(' → ') : '—')));
    add(art, telemetryBlock(r.telemetry));
    // The full chart drawn on the server (#flow-template) is the source of
    // the SVG namespace too, so the page names no URL.
    var tpl = byId('flow-template');
    var src = tpl && tpl.children ? tpl.children[0] : null;
    if (!src || !d.statechart) {
      add(add(art, el('div', 'flowbox')), el('p', 'empty', 'statechart unavailable'));
      return art;
    }
    var round = r.round === null || r.round === undefined ? '' : 'r' + r.round;
    var age = flow.state && flow.state_age_seconds !== null && flow.state_age_seconds !== undefined
      ? flow.state_age_seconds : null;
    var meta = {round: round, age: age, text: [round, age === null ? '' : dur(age)].filter(Boolean).join(' · ')};
    var box = add(art, el('div', 'flowbox trail'));
    add(box, trailSvg(src.namespaceURI, d.statechart, flow, meta, jobLabel(r) + ': the path its ticket took'));
    trails.push(box);
    var keys = add(art, el('p', 'legend'));
    add(keys, el('span', 'key path', 'path taken'));
    add(keys, el('span', 'key now', 'now'));
    add(keys, el('span', 'key next', 'can come next'));
    var full = details(art, 'full-chart:' + r.job, 'full chart', 'fullchart');
    add(add(full, el('div', 'flowbox')), highlight(src.cloneNode(true), flow));
    var legend = add(full, el('p', 'legend'));
    add(legend, el('span', 'key path', 'path taken'));
    (d.statechart.legend || []).forEach(function (k) { add(legend, el('span', 'key g-' + k.group, k.name)); });
    return art;
  }

  function renderJobView(d) {
    var root = byId('jobview');
    if (!root) return;
    root.replaceChildren();
    var jobs = activeJobs(d);
    var hash = String(location.hash || '');
    if (hash.indexOf('#job-') === 0) {
      jobs = jobs.filter(function (j) { return '#' + anchor(j.row.job) === hash; });
      if (!jobs.length) {
        var gone = add(root, el('section', 'panel'));
        add(add(gone, el('h2')), el('span', null, hash.slice(1)));
        add(gone, el('p', 'empty', 'That job is no longer running or queued. Pick one from the list above.'));
        return;
      }
    }
    if (!jobs.length) { add(root, el('p', 'empty', 'no running or queued jobs')); return; }
    jobs.forEach(function (j) { add(root, jobArticle(j.row, j.state, d)); });
  }
"""


def _legend(chart: dict) -> str:
    keys = _span("key path", "path taken") + "".join(
        _span(f"key g-{item['group']}", item["name"]) for item in chart["legend"]
    )
    return f'<p class="legend">{keys}</p>'


_TRAIL_LEGEND = (
    '<p class="legend">' + _span("key path", "path taken") + _span("key now", "now")
    + _span("key next", "can come next") + "</p>"
)


def _meta(row: dict, flow: dict) -> str:
    """The current step's label: the job's round and time in that state,
    where the data has them."""
    parts = []
    if row.get("round") is not None:
        parts.append(f"r{row['round']}")
    if flow["state"] and flow.get("state_age_seconds") is not None:
        parts.append(duration(flow["state_age_seconds"]))
    return " · ".join(parts)


def _job_article(row: dict, state: str, chart: dict | None) -> str:
    flow = row.get("flow") or {"state": None, "path": [], "state_age_seconds": None}
    kind = _job_kind(row)
    rnd = "—" if row.get("round") is None else row["round"]
    timing = (
        _span("muted", f"T+ {duration(row['elapsed_seconds'])}")
        if state == "running"
        else _span("muted", f"waiting {duration(row['age_seconds'])}")
    )
    where = (
        f'<p class="where"><span>now </span><b>{_e(flow["state"] or "state not recorded")}</b>'
        f"<span> · round {_e(rnd)}</span>"
        + (
            f"<span> · </span><span>{duration(flow['state_age_seconds'])} in {_e(flow['state'])}</span>"
            if flow["state"] and flow["state_age_seconds"] is not None
            else ""
        )
        + "</p>"
    )
    path = " → ".join(flow["path"]) if flow["path"] else "—"
    if chart is None:
        diagram = '<div class="flowbox"><p class="empty">statechart unavailable</p></div>'
        legend = ""
    else:
        trail = render_path_svg(
            chart, flow["path"], flow["state"],
            next_steps=flow.get("next"), terminal=flow.get("terminal"),
            meta=_meta(row, flow), label=f"{_job_label(row)}: the path its ticket took",
        )
        full = render_svg(
            chart, flow["path"], flow["state"], label=f"{_job_label(row)} on the ticket statechart"
        )
        diagram = f'<div class="flowbox trail">{trail}</div>'
        legend = (
            _TRAIL_LEGEND
            + '<details class="fullchart"><summary>full chart</summary>'
            + f'<div class="flowbox">{full}</div>{_legend(chart)}</details>'
        )
    count = f"{state} · job {row['job']}"
    return (
        f'<article class="panel jobpage" id="{anchor(row["job"])}">'
        f'<h2>{_span(None, _job_label(row))}{_span("count", count)}</h2>'
        f'<div class="row">{_bot(row["recipient"])}'
        + (_span("kind", kind) if kind else "")
        + _span("ticket", _job_label(row))
        + _span("what", f"step {row['action']}")
        + timing
        + _state_chip(row)
        + (_chip("OVERDUE", "bad") if row.get("overdue") else "")
        + (_chip(f"retried {row['retries']}x") if row.get("retries") else "")
        + "</div>"
        + where
        + f'<p class="path">path: {_e(path)}</p>'
        + job_block(row.get("telemetry"))
        + diagram
        + legend
        + "</article>"
    )


def _jobview(snapshot: dict) -> str:
    chart = snapshot.get("statechart")
    jobs = _active_jobs(snapshot)
    inner = (
        "".join(_job_article(row, state, chart) for row, state in jobs)
        if jobs
        else '<p class="empty">no running or queued jobs</p>'
    )
    return f'<div id="jobview">{inner}</div>'


def _flow_template(snapshot: dict) -> str:
    chart = snapshot.get("statechart")
    if chart is None:
        return ""
    return f'<div id="flow-template" hidden>{render_svg(chart)}</div>'
