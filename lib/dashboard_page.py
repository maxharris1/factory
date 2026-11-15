"""The dashboard pages: inline CSS, the server-rendered panels and the script.

Five pages share one shell (#139, docs/design/mission-control artboards
TopBar, HeaderBand and StatusLine): the TopBar (brand with its pixel caret,
navigation in this order, link state, snapshot meta), the header band
(brightness = running / total slots), the attention strip (every item that
needs attention, hidden when clear) and the status line (slots per device,
queue depth and the vitals, each linking to System or Lanes):

- index.html (Lanes): factory status (with Linear webhook health when a
  listener has run), lanes and slots, the queue rail; with script, a click
  on a job opens its flow in a modal (without script the link goes to
  job.html#job-<id>);
- job.html (Tickets): every running or queued job with the path its ticket
  took through the statechart (model/chart.mmd, see lib/dashboard_flow.py),
  the current step and what can come next; the full chart is behind a
  collapsed toggle; job.html#job-<id> shows one job;
- activity.html: needs attention, recent handoffs and verdicts, tickets, log;
- efficiency.html: poll and Linear budget, handoff savings, tokens and cost
  per harness, repo and job (#93, lib/dashboard_telemetry.py). Each job's
  article (job page and modal) ends with its telemetry feed;
- health.html (System, #92): each component heartbeat (poller, waiters,
  Linear listener, tunnel, dashboard writer, device probes) as up, stale or
  down with last-seen in ET, pid and device, and each harness whose queued
  jobs have no live waiter. Down components and those harnesses are also
  needs-attention items, and a queued job with no waiter carries a
  "no waiter for <harness>" link to its Health row.

Every panel is rendered server-side here, so a page reads without script
(and then reloads every refresh_seconds). Everything is inline (no external
assets, nothing from a CDN), so the pages work offline on the tailnet and
under the static server's CSP. The look follows Omarchy (omarchy.org):
the Tokyo Night palette, square corners, 1px elevation rings, Geist and
JetBrains Mono. Those two fonts are vendored under lib/fonts and carried
in the script as bytes (see font_faces), so no font is ever fetched. The
script fetches the relative
`status.json` every POLL_MS (1 to 2 seconds) and rebuilds the current
page's panels in place with createElement/textContent only, never HTML
strings, so no value from the snapshot is ever parsed as markup (SVG nodes
are made with createElementNS, in the namespace of the server-drawn chart).
The full chart is a copy of the server-drawn one with the highlight moved. If
status.json cannot be fetched at all, the server-rendered page stays and
reloads every refresh_seconds.

PixelStrip (#138): px_strip renders a strip canvas from the PX_STATE table
and PX_SCRIPT draws every strip on the page under one animation loop (see
docs/design/mission-control/SPEC.md section 6). The shell places the caret,
the band and the attention block.
"""

from __future__ import annotations

import base64
import functools
import html
import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from lib.dashboard_flow import path_geometry, render_path_svg, render_svg
from lib.dashboard_telemetry import TELEMETRY_CSS, TELEMETRY_SCRIPT, job_block, rollup_panel
from lib.health import CODE_COMPONENTS
from lib.pr_title import label

POLL_MS = 1500

# The Omarchy pair (Geist for text, JetBrains Mono for numbers and labels),
# vendored under lib/fonts with their OFL licenses; see lib/fonts/README.md.
# Each is (family, file, weight range).
FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONTS = (
    ("Geist Variable", "geist-latin-wght-normal.woff2", "100 900"),
    ("JetBrains Mono Variable", "jetbrains-mono-latin-wght-normal.woff2", "100 800"),
)

CSS = """
:root{color-scheme:dark;
--bg:#1a1b26;--dim:#13141c;--panel:#1f2230;--panel-hi:#24283b;--field:#0e0e14;--sel:#292e42;
--line:#24283b;--line-hi:#414868;
--text:#c0caf5;--text-2:#a9b1d6;--muted:#8b93b8;
--brand:#9ece6a;--brand-soft:#9ece6a1f;--ink:#0c0e10;
--hdr-text:#ced8ff;--hdr-text-2:#b7bfea;--hdr-brand:#acdc7e;
--accent:#7dcfff;--ok:#9ece6a;--warn:#ff9e64;--bad:#f7768e;
--claude:#e0af68;--codex:#7aa2f7;--cursor:#bb9af7;--grok:#73daca;
--elev:0 0 0 1px rgba(255,255,255,.07);--elev-hi:0 0 0 1px rgba(255,255,255,.13);
--ease:cubic-bezier(.4,0,.2,1);
--sans:"Geist Variable",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"JetBrains Mono Variable",ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}
html{background:var(--bg);scrollbar-color:rgba(255,255,255,.14) transparent}
body{display:flex;flex-direction:column;margin:0;min-height:100vh;color:var(--text);background:var(--bg);
font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
::selection{background:var(--sel);color:var(--text)}
a{color:inherit;transition:color .15s var(--ease),background-color .15s var(--ease),box-shadow .15s var(--ease)}
.topbar{width:100%;background:var(--bg);color:var(--text)}
.top-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;min-height:56px;padding:8px 24px}
.brand{display:flex;align-items:center;gap:6px;font:600 15px/1 var(--mono);letter-spacing:-.01em;color:var(--hdr-text);white-space:nowrap}
.brand b{font-weight:600;color:var(--hdr-brand)}
.caret{display:block;width:8px}
.link{display:flex;align-items:center;gap:8px;font:500 12px/1 var(--mono);letter-spacing:.04em;color:var(--muted)}
.link .dot{width:8px;height:8px;background:currentColor}
.link.live{color:var(--ok)}.link.stale{color:var(--warn)}.link.lost{color:var(--bad)}.link.boot{color:var(--muted)}
.topbar .meta{margin-left:auto;color:var(--muted);font:12px/1.3 var(--mono)}
.topbar .meta span{color:var(--text-2)}
.attn{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;min-height:36px;padding:6px 24px;
background:color-mix(in srgb,var(--bad) 9%,transparent);border-bottom:1px solid color-mix(in srgb,var(--bad) 45%,transparent);
font:12px/1.5 var(--mono)}
.attn-head{display:flex;align-items:center;gap:8px}
.attn-blk{display:block;width:12px}
.attn-count{font-weight:700;color:var(--bad);text-decoration:none}
.attn-item{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.attn-who{font-weight:600;color:var(--text);text-decoration:none}
.attn-what{color:var(--text-2)}
.attn a:hover{text-decoration:underline}
.statusline{position:sticky;bottom:0;z-index:2;display:flex;flex-wrap:wrap;align-items:stretch;width:100%;min-height:32px;
background:var(--dim);border-top:1px solid var(--line);font:12px/1.3 var(--mono);color:var(--text-2)}
.sl-brand{display:flex;align-items:center;padding:6px 9px;background:var(--ok);color:var(--ink);font-weight:700;letter-spacing:.04em}
.sl-seg{display:flex;align-items:center;gap:6px;padding:6px 9px;border-right:1px solid var(--line)}
.sl-seg.sl-queue{border-right:0}
.sl-k{color:var(--muted)}.sl-v{color:var(--text)}
.sl-fill{flex:1 1 24px}
.sl-vital{display:flex;align-items:center;gap:7px;padding:6px 9px;border-left:1px solid var(--line);text-decoration:none}
.sl-vital:hover{background:var(--panel-hi)}
.sl-vital .blk{width:8px;height:8px;background:currentColor}
.sl-vital.ok{color:var(--text-2)}.sl-vital.ok .blk{background:var(--ok)}
main{position:relative;z-index:1;flex:1 0 auto;width:100%;display:grid;align-content:start;grid-template-columns:repeat(12,minmax(0,1fr));
gap:16px;max-width:1600px;margin:0 auto;padding:20px 24px 32px}
.panel{grid-column:span 4;position:relative;min-width:0;padding:14px 16px 16px;
background:var(--panel);box-shadow:var(--elev);transition:box-shadow .15s var(--ease)}
.panel:hover{box-shadow:var(--elev-hi)}
#kpis,#tickets,#log,#attention,#jobs,#jobview,#components,#no-waiter{grid-column:1/-1}#lanes{grid-column:span 9}#queue{grid-column:span 3}
#handoffs,#verdicts,#health,#savings{grid-column:span 6}
.panel h2{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:0 0 12px;
font:600 13px/1.3 var(--mono);letter-spacing:-.01em;color:var(--text)}
.panel h2>span:first-child::before{content:"";display:inline-block;width:7px;height:7px;margin-right:9px;
vertical-align:.12em;background:var(--brand)}
.panel h2 .count{color:var(--muted);font-weight:400;font-size:12px}
.kpis{display:flex;flex-wrap:wrap;gap:12px}
.kpi{flex:1 1 140px;padding:10px 14px 12px;background:var(--bg);box-shadow:var(--elev)}
.kpi b{display:block;font:600 30px/1.15 var(--mono);letter-spacing:-.03em;color:var(--text);font-variant-numeric:tabular-nums}
.kpi span{font:500 12px/1.3 var(--mono);color:var(--muted)}
.kpi.accent b{color:var(--accent)}.kpi.ok b{color:var(--ok)}.kpi.warn b{color:var(--warn)}.kpi.bad b{color:var(--bad)}
.devices{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.device{padding:12px;background:var(--bg);box-shadow:var(--elev)}
.device.disabled{color:var(--muted);box-shadow:none;outline:1px dashed var(--line-hi);outline-offset:-1px;
background:repeating-linear-gradient(135deg,var(--dim) 0 8px,var(--bg) 8px 16px)}
.device.disabled .name{color:var(--muted)}
.device header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.device .name{font:600 14px/1 var(--mono);letter-spacing:-.01em;color:var(--text)}
.device .cap{margin-left:auto;font:12px/1 var(--mono);color:var(--muted)}
.device .harness{margin:0 0 10px;font-size:12px;color:var(--muted)}
.slots{display:grid;gap:6px}
.slot{min-height:54px;padding:8px 10px;border:1px solid transparent;background:var(--panel-hi)}
.slot.idle{display:flex;align-items:center;justify-content:center;border:1px dashed var(--line-hi);background:transparent;
font:500 11px/1 var(--mono);letter-spacing:.12em;text-transform:lowercase;color:var(--muted)}
.job .top{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px}
.job .ticket{font:600 13px/1.25 var(--mono);color:var(--text);overflow-wrap:anywhere}
.job .action{font-size:12px;color:var(--text-2)}
.job .times{display:flex;justify-content:space-between;gap:8px;margin:5px 0 6px;font:12px/1.2 var(--mono);color:var(--muted)}
.job .times b{font-weight:600;color:var(--text)}
.job.overdue{border-color:var(--bad);box-shadow:inset 0 0 0 1px var(--bad)}
.hb{height:3px;background:var(--line-hi);overflow:hidden}
.hb i{display:block;height:100%;width:0;background-color:var(--ok);
background-image:linear-gradient(90deg,transparent 0,rgba(255,255,255,.35) 50%,transparent 100%);
background-size:200% 100%;animation:shimmer 2.4s linear infinite;transition:width .9s linear}
.hb.warn i{background-color:var(--warn)}.hb.bad i{background-color:var(--bad)}
.bot,.chip,.kind{display:inline-block;padding:0 6px;border:1px solid currentColor;
border-color:color-mix(in srgb,currentColor 40%,transparent);background:color-mix(in srgb,currentColor 9%,transparent);
font:500 11px/1.6 var(--mono);letter-spacing:.02em;white-space:nowrap}
.bot{text-transform:lowercase}
.bot.claude{color:var(--claude)}.bot.codex{color:var(--codex)}.bot.cursor{color:var(--cursor)}
.bot.grok{color:var(--grok)}.bot.other{color:var(--muted)}
.chip{text-transform:lowercase}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.accent{color:var(--accent)}.muted{color:var(--muted)}
.feed{margin:0;padding:0;list-style:none}
.feed li{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;padding:7px 6px;border-bottom:1px solid var(--line);
transition:background-color .15s var(--ease)}
.feed li:hover{background:var(--panel-hi)}
.feed li:last-child{border-bottom:0}
.feed .ticket{font:600 13px/1.25 var(--mono)}.feed .what{color:var(--text-2);font-size:12px}
.feed .when{margin-left:auto;font:12px/1.2 var(--mono);color:var(--muted)}
.more{margin:8px 0 0;font:12px/1.4 var(--mono);color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:8px 0}
.stat{padding:8px 10px;background:var(--bg);box-shadow:var(--elev)}
.stat b{display:block;font:600 18px/1.25 var(--mono);font-variant-numeric:tabular-nums}.stat span{font-size:12px;color:var(--muted)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:4px 0}
.row .label{min-width:92px;font:500 12px/1.2 var(--mono);color:var(--muted)}
.gauge{display:flex;align-items:center;gap:18px}
.ring{--p:0;display:grid;flex:none;place-items:center;width:100px;height:100px;border-radius:50%;
background:conic-gradient(var(--brand) calc(var(--p) * 1%),var(--line-hi) 0)}
.ring span{display:grid;place-items:center;width:80px;height:80px;border-radius:50%;background:var(--panel);
font:600 20px/1 var(--mono);color:var(--text)}
.meter{display:flex;height:6px;margin:10px 0;background:var(--line-hi);overflow:hidden}
.meter i{display:block;height:100%;transition:width .6s var(--ease)}
.meter .scripted{background:var(--accent)}.meter .bots{background:var(--grok)}
.empty{margin:4px 0;color:var(--muted);font:12px/1.5 var(--mono)}
.fresh{animation:flash 2.4s ease-out}
details summary{cursor:pointer;color:var(--text-2);font:500 12px/1.8 var(--mono)}
details summary:hover{color:var(--text)}
details summary:focus-visible,a:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
pre{margin:8px 0 0;padding:10px 12px;max-height:340px;overflow:auto;background:var(--field);box-shadow:var(--elev);
color:var(--text-2);font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;margin:0 0 8px}
th,td{padding:6px 12px 6px 0;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font:500 12px/1.4 var(--mono)}
td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
td.bad{color:var(--bad);font-weight:600}
#static{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:12px 24px 32px}
#static h1{font:600 22px/1.3 var(--mono);letter-spacing:-.02em;color:var(--text)}
#static h2{margin:24px 0 8px;font:600 14px/1.3 var(--mono);color:var(--text)}
#static h2::before{content:"";display:inline-block;width:7px;height:7px;margin-right:9px;vertical-align:.12em;background:var(--brand)}
#static .tiles{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
#static .tile{min-width:120px;padding:10px 14px;background:var(--panel);box-shadow:var(--elev);color:var(--muted)}
#static .tile b{display:block;font:600 22px/1.2 var(--mono);color:var(--text)}
#static .meta{color:var(--muted)}#static .stale{color:var(--bad);font-weight:600}
.nav{display:flex;flex-wrap:wrap;gap:2px}
.nav a{padding:6px 12px;color:var(--hdr-text-2);text-decoration:none;font:500 14px/1.2 var(--sans)}
.nav a:hover{background:var(--panel-hi);color:var(--hdr-text)}
.nav a[aria-current=page]{color:var(--hdr-text);background:var(--panel-hi);box-shadow:inset 0 -2px 0 var(--brand)}
a.slot,a.queue-item,.jobs a{color:inherit;text-decoration:none}
a.slot{display:block}
a.slot:hover,a.slot:focus-visible{border-color:var(--brand)}
#lanes .devices{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
#lanes .slot{min-height:84px}
.slot.held{display:flex;align-items:center;justify-content:center;border:1px dashed var(--warn);background:transparent;
font:500 11px/1 var(--mono);letter-spacing:.12em;text-transform:lowercase;color:var(--warn)}
.job .step{display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:3px;font-size:12px;color:var(--muted)}
.job .step b{font-weight:600;color:var(--text)}
.kind{padding:0 4px;color:var(--muted);font-size:10px}
.queue-item{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;width:100%}
.queue-item:hover .ticket,.jobs a:hover .ticket{color:var(--brand)}
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
.modal{position:fixed;inset:0;z-index:10;display:flex;align-items:flex-start;justify-content:center;
padding:6vh 16px;overflow-y:auto;background:color-mix(in srgb,var(--ink) 78%,transparent)}
.modal-box{position:relative;width:min(1300px,100%);background:var(--panel);box-shadow:var(--elev-hi)}
.modal-box .jobpage{box-shadow:none}
.modal-box .jobpage h2{padding-right:92px}
.modal-box .more{padding:0 16px 14px}
.modal-close{position:absolute;top:10px;right:12px;z-index:1;padding:2px 10px;border:1px solid var(--line-hi);
background:var(--panel-hi);color:var(--text-2);font:500 12px/1.6 var(--mono);cursor:pointer}
.modal-close:hover{color:var(--text);border-color:var(--brand)}
.modal-close:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
body.modal-open{overflow:hidden}
.definition{margin:8px 0 4px;color:var(--text-2)}
[hidden]{display:none !important}
.px{display:block;width:100%;height:16px;background-color:var(--field);
background-image:repeating-linear-gradient(90deg,transparent 0 4px,var(--field) 4px 8px),
repeating-linear-gradient(0deg,color-mix(in srgb,var(--px,var(--ok)) 30%,var(--field)) 0 4px,transparent 4px 8px)}
.px[data-px=off],.px[data-px=twinkle]{background-image:none}
.px[data-load]{background-image:repeating-linear-gradient(90deg,transparent 0 4px,var(--field) 4px 8px),
repeating-linear-gradient(0deg,color-mix(in srgb,var(--px,var(--ok)) calc(6% + var(--l,1)*24%),var(--field)) 0 4px,transparent 4px 8px)}
.px[data-px=fill]{background-image:linear-gradient(90deg,color-mix(in srgb,var(--px,var(--ok)) 62%,var(--field)) 0 calc(var(--p,0)*100%),transparent 0)}
.px-held{position:relative}
.px-held::after{content:"";position:absolute;inset:-1px;pointer-events:none;
background-image:repeating-linear-gradient(90deg,var(--warn) 0 8px,transparent 8px 16px),
repeating-linear-gradient(90deg,var(--warn) 0 8px,transparent 8px 16px),
repeating-linear-gradient(180deg,var(--warn) 0 8px,transparent 8px 16px),
repeating-linear-gradient(180deg,var(--warn) 0 8px,transparent 8px 16px);
background-size:100% 2px,100% 2px,2px 100%,2px 100%;background-repeat:repeat-x,repeat-x,repeat-y,repeat-y;
background-position:0 0,0 100%,100% 0,0 0;animation:px-march 2s steps(4) infinite}
@keyframes px-march{to{background-position:16px 0,-16px 100%,100% 16px,0 -16px}}
@keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
@keyframes breathe{0%,100%{opacity:.18;stroke:var(--accent);stroke-width:3}50%{opacity:.8;stroke:var(--accent);stroke-width:7}}
@keyframes flash{from{box-shadow:0 0 0 1px var(--brand),0 0 0 4px var(--brand-soft)}to{box-shadow:var(--elev)}}
@media (max-width:1100px){.panel,#lanes,#queue,#handoffs,#verdicts,#health,#savings{grid-column:1/-1}}
@media (max-width:640px){.top-row{padding:8px 14px}.attn{padding:6px 14px}main{padding:14px 12px 24px;gap:12px}
.topbar .meta{margin-left:0}}
@media (prefers-reduced-motion: reduce){
*,*::before,*::after{animation: none !important;transition: none !important;scroll-behavior:auto !important}
.fresh{outline:2px solid var(--brand);outline-offset:-2px}
.flow .visit.breathe .halo{animation:none;opacity:.45}
.px-held::after{animation:none}}
"""

SCRIPT = r"""
(function () {
  'use strict';
  // The vendored fonts travel inside the page as base64. A FontFace built
  // from bytes makes no request, so nothing is fetched and the static
  // server's CSP needs no font-src. Without FontFace the system fonts stay.
  var FONTS = __FONTS__;
  if (typeof FontFace !== 'function' || !document.fonts || typeof atob !== 'function') return;
  FONTS.forEach(function (font) {
    try {
      var raw = atob(font.data);
      var bytes = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      var face = new FontFace(font.family, bytes, {weight: font.weight, style: 'normal', display: 'swap'});
      document.fonts.add(face);
      var loading = face.load();
      if (loading && loading.catch) loading.catch(function () {});
    } catch (err) { /* keep the fallback font */ }
  });
})();
(function () {
  'use strict';
  var POLL_MS = __POLL_MS__;
  // The job view's simplified diagram (lib/dashboard_flow.py path_geometry).
  var FLOW = __FLOW__;
  var PX = __PIXELSTRIP__;
  // The shell's link table and the components that carry a code version.
  var SHELL = __SHELL__;
  var STATUS_URL = 'status.json';
  var FETCH_TIMEOUT_MS = 5000;
  var LINK_LOST_MS = 6000;
  var STALE_SECONDS = 180;
  var LIST_MAX = 12;
  var BOTS = {claude: 1, codex: 1, cursor: 1, grok: 1};
  var body = document.body;
  var PAGE = body.getAttribute('data-page') || 'main';
  var refreshSeconds = Number(body.getAttribute('data-refresh')) || 20;
  var generated = Number(body.getAttribute('data-generated')) || 0;
  function byId(id) { return document.getElementById(id); }
  var lastOk = 0;
  var everOk = false;
  var failures = 0;
  var inflight = false;
  var timer = null;
  var reloadArmed = false;
  var firstRender = true;
  var latest = null;
  var healthKey = '';
  var tickers = [];
  var prevKeys = {};
  var nextKeys = {};
  var openDetails = {};
  var modal = null;
  var trails = [];
  var caretState = 'boot';

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function add(parent, child) { parent.appendChild(child); return child; }
  function link(cls, href) { var a = el('a', cls); a.setAttribute('href', href); return a; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dur(s) {
    if (s === null || s === undefined || !isFinite(s)) return '—';
    s = Math.max(0, Math.floor(s));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + pad(s % 60) + 's';
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + pad(Math.floor(s % 3600 / 60)) + 'm';
    return Math.floor(s / 86400) + 'd ' + Math.floor(s % 86400 / 3600) + 'h';
  }
  function num(n) {
    if (n === null || n === undefined) return '—';
    return typeof n === 'number' ? n.toLocaleString('en-US') : String(n);
  }
  function since() { return Math.max(0, Date.now() / 1000 - generated); }
  function short(repo) {
    var text = String(repo === null || repo === undefined ? '' : repo);
    var cut = text.lastIndexOf('/');
    return cut >= 0 ? text.slice(cut + 1) : text;
  }
  // #115: the source issue first, the PR second.
  function prOf(row, first) { return row.pr && String(row.pr) !== String(first) ? ' (PR #' + row.pr + ')' : ''; }
  function ticketIn(repo, row) {
    var first = row.source ? String(row.source).replace(/^#/, '') : row.ticket_id;
    return repo + '#' + first + prOf(row, first);
  }
  function ticketOf(row) { return ticketIn(short(row.repo), row); }
  function jobLabel(r) { return r.tracker === 'linear' ? String(r.ticket_id) + prOf(r, r.ticket_id) : ticketOf(r); }
  function jobKind(r) { return r.tracker === 'github' ? 'PR' : r.tracker === 'linear' ? 'ticket' : null; }
  function anchor(job) { return 'job-' + String(job).replace(/[^A-Za-z0-9_-]/g, '-'); }
  function bot(name) { return el('span', 'bot ' + (BOTS[name] ? name : 'other'), name); }
  function chip(text, cls) { return el('span', 'chip ' + (cls || 'muted'), text); }
  // #92: only a link into the Health tab, never a value used as a URL.
  function healthLink(href) { return /^health\.html#[A-Za-z0-9_-]+$/.test(String(href)) ? String(href) : 'health.html'; }
  // #75: lib/job_state.py's state of a running job, as a chip.
  var JOB_STATE = {claimed: ['claimed, not started', 'warn'], running: ['running', 'ok'],
                   stale: ['stale heartbeat', 'warn'], dead: ['runner dead', 'bad']};
  function stateChip(r) {
    var s = JOB_STATE[r.job_state];
    return s ? chip(s[0], s[1]) : null;
  }
  function ticking(node, base, prefix, suffix) {
    prefix = prefix || '';
    suffix = suffix || '';
    if (base === null || base === undefined) { node.textContent = prefix + '—'; return node; }
    var item = {run: function (d) { node.textContent = prefix + dur(base + d) + suffix; }};
    tickers.push(item);
    item.run(since());
    return node;
  }
  function mark(panel, key, node) {
    var keys = nextKeys[panel] || (nextKeys[panel] = {});
    keys[key] = true;
    if (!firstRender && !(prevKeys[panel] || {})[key]) node.classList.add('fresh');
    return node;
  }
  function panel(id, title, count) {
    var root = byId(id);
    if (!root) return null;
    root.replaceChildren();
    var head = add(root, el('h2'));
    add(head, el('span', null, title));
    if (count !== undefined && count !== null) add(head, el('span', 'count', count));
    return root;
  }
  function details(root, id, summary, cls) {
    var box = add(root, el('details', cls));
    box.open = !!openDetails[id];
    if (box.addEventListener) box.addEventListener('toggle', function () { openDetails[id] = box.open; });
    add(box, el('summary', null, summary));
    return box;
  }
  function list(root, rows, emptyText, draw) {
    if (!rows || !rows.length) { add(root, el('p', 'empty', emptyText)); return; }
    var ul = add(root, el('ul', 'feed'));
    rows.slice(0, LIST_MAX).forEach(function (row) { add(ul, draw(row)); });
    if (rows.length > LIST_MAX) add(root, el('p', 'more', '+' + (rows.length - LIST_MAX) + ' more'));
  }

  // --- every page ---------------------------------------------------------

  // The lanes the page draws: devices with their running jobs and the slots
  // in use, plus grok wakes. Mirrors lane_devices in lib/dashboard_page.py.
  function laneDevices(d) {
    var slots = d.slots || {};
    var box = slots.box || {};
    var devices = (slots.devices || []).map(function (x) {
      return {name: x.name, slot_max: x.slot_max, held: x.held || 0, enabled: x.enabled,
              placeholder: x.placeholder, harnesses: x.harnesses || [], listed: true, jobs: []};
    });
    var byName = {};
    devices.forEach(function (x) { byName[x.name] = x; });
    var wakes = [];
    (d.running || []).forEach(function (r) {
      if (r.device === null || r.device === undefined) { wakes.push(r); return; }
      if (!Object.prototype.hasOwnProperty.call(byName, r.device)) {
        var dev = {name: r.device, slot_max: r.device === 'box' ? box.slot_max : null, held: 0, enabled: true,
                   placeholder: false, harnesses: [], listed: r.device === 'box' && !devices.length, jobs: []};
        devices.push(dev);
        byName[r.device] = dev;
      }
      byName[r.device].jobs.push(r);
    });
    if (!devices.length) {
      devices.push({name: 'box', slot_max: box.slot_max, held: 0, enabled: true, placeholder: false,
                    harnesses: [], listed: true, jobs: []});
    }
    // #63 FU-1: a slot held in device_slots is in use even when its job is
    // not (or no longer) running, so a lane counts max(held, running).
    devices.forEach(function (dev) { dev.used = Math.max(dev.held, dev.jobs.length); });
    return {devices: devices, wakes: wakes};
  }

  // Running / total slots over the devices that can take work (header_load).
  function headerLoad(d) {
    var used = 0;
    var total = 0;
    laneDevices(d).devices.forEach(function (dev) {
      if (dev.enabled && !dev.placeholder && dev.slot_max) { used += dev.used; total += dev.slot_max; }
    });
    return total ? Math.max(0, Math.min(1, used / total)) : 0;
  }

  function renderBand(d) {
    var root = byId('band');
    if (root) root.replaceChildren(PX.strip('band', {state: 'band', load: headerLoad(d), height: 24}));
  }

  function attentionItems(d) {
    var a = d.attention || {items: []};
    return (a.items || []).concat(agedHealth(d).extra);
  }

  // The attention strip (#139): every item the count covers, hidden when clear.
  function renderAttentionStrip(d) {
    var root = byId('attention-strip');
    if (!root) return;
    var items = attentionItems(d);
    root.replaceChildren();
    root.hidden = !items.length;
    if (!items.length) return;
    var head = add(root, el('div', 'attn-head'));
    var blk = add(head, el('span', 'attn-blk'));
    blk.setAttribute('aria-hidden', 'true');
    add(blk, PX.strip('attention', {state: 'attention', height: 16}));
    var count = add(head, link('attn-count', 'activity.html#attention'));
    add(count, el('b', null, items.length)).setAttribute('id', 'attention-count');
    add(count, el('span', null, items.length === 1 ? ' needs attention' : ' need attention'));
    items.forEach(function (x) {
      var row = add(root, el('div', 'attn-item'));
      add(row, chip(x.kind, ATTENTION[x.kind] || 'muted'));
      add(row, link('attn-who', x.component ? healthLink(x.link) : 'job.html')).textContent =
        x.component ? x.component : ticketOf(x);
      add(row, el('span', 'attn-what', x.detail));
    });
  }

  // "behind": configured code components not on the checkout's version
  // (lib/health.py stale_code). Mirrors version_drift.
  function versionDrift(h) {
    h = h || {};
    var rows = (h.components || []).filter(function (c) {
      return c.configured !== false && SHELL.code.indexOf(c.component) >= 0;
    });
    return {
      newest: String(h.code_version || 'unknown'),
      behind: rows.filter(function (c) { return !!c.stale_code; }).map(function (c) { return c.label; }),
      unknown: rows.filter(function (c) { return c.version === 'unknown'; }).map(function (c) { return c.label; })
    };
  }

  function segment(key, text, state, label, age, prefix, href) {
    return {key: key, label: label === undefined ? null : label, text: text,
            state: state === undefined ? null : state,
            href: href || (Object.prototype.hasOwnProperty.call(SHELL.links, key) ? SHELL.links[key] : null),
            age: age === undefined ? null : age, prefix: prefix === undefined ? null : prefix};
  }
  function present(v) { return v !== null && v !== undefined; }

  // The status line's segments, each from the snapshot. Mirrors status_segments.
  function statusSegments(d) {
    var out = [];
    laneDevices(d).devices.forEach(function (dev) {
      var value = !dev.enabled || dev.placeholder ? 'off'
        : dev.used + '/' + (present(dev.slot_max) ? dev.slot_max : '—');
      out.push(segment('device:' + dev.name, value, null, dev.name));
    });
    out.push(segment('queue', num(((d.queue || {}).totals || {}).queued || 0), null, 'queue'));
    var p = d.poller || {};
    var h = p.health || {state: 'unknown'};
    var ps = POLL_STATE[h.state] || POLL_STATE.unknown;
    var poll = 'poll ' + ps[0].toLowerCase();
    var cycle = h.last_cycle_age_seconds;
    if (present(cycle)) out.push(segment('poll', poll + ' · ' + dur(cycle), ps[1], null, cycle, poll + ' · '));
    else out.push(segment('poll', poll, ps[1]));
    var errors = (p.errors || {}).last_hour || 0;
    out.push(segment('poll-errors', num(errors) + ' poll error' + (errors === 1 ? '' : 's') + ' / 1 h', errors ? 'bad' : 'ok'));
    var b = p.linear_budget || {state: 'ok'};
    var bs = BUDGET_STATE[b.state] || ['OTHER', 'muted'];
    out.push(segment('linear', 'linear ' + bs[0].toLowerCase(), bs[1]));
    var w = p.linear_webhook;
    if (!w) {
      out.push(segment('webhook', 'no webhook', 'muted', undefined, undefined, undefined, SHELL.absent));
    } else if (!present(w.last_received_age_seconds)) {
      out.push(segment('webhook', 'webhook never', present(w.warning) ? 'bad' : 'warn'));
    } else {
      out.push(segment('webhook', 'webhook ' + dur(w.last_received_age_seconds), present(w.warning) ? 'bad' : 'ok',
                       null, w.last_received_age_seconds, 'webhook '));
    }
    var f = d.feeder;
    if (!f) out.push(segment('feeder', 'no feeder', 'muted', undefined, undefined, undefined, SHELL.absent));
    else {
      var failing = (f.items || []).some(function (x) { return x.state === 'error'; });
      out.push(segment('feeder', 'feeder ' + num(f.fed) + ' fed', failing ? 'bad' : 'ok'));
    }
    var hs = d.health || {};
    if (!hs.known) {
      out.push(segment('components', 'components unknown', hs.error ? 'bad' : 'muted'));
    } else {
      var rows = agedHealth(d).rows.filter(function (c) { return c.configured !== false; });
      var up = rows.filter(function (c) { return c.state === 'up'; }).length;
      var down = rows.some(function (c) { return c.state === 'down'; });
      out.push(segment('components', up + ' / ' + rows.length + ' up', down ? 'bad' : up < rows.length ? 'warn' : 'ok'));
    }
    var drift = versionDrift(hs);
    if (drift.newest === 'unknown') out.push(segment('version', 'version unknown', 'warn'));
    else {
      out.push(segment('version', drift.newest.slice(0, 7) + ' · ' + drift.behind.length + ' behind',
                       drift.behind.length ? 'warn' : 'ok'));
    }
    return out;
  }

  function renderStatusLine(d) {
    var root = byId('statusline');
    if (!root) return;
    root.replaceChildren();
    add(root, el('span', 'sl-brand', 'factory'));
    var filled = false;
    statusSegments(d).forEach(function (s) {
      if (s.state === null) {
        var plain = add(root, el('span', 'sl-seg' + (s.key === 'queue' ? ' sl-queue' : '')));
        plain.setAttribute('data-seg', s.key);
        add(plain, el('span', 'sl-k', s.label));
        add(plain, el('span', 'sl-v', s.text));
        return;
      }
      if (!filled) { add(root, el('span', 'sl-fill')); filled = true; }
      var a = add(root, link('sl-vital ' + s.state, s.href));
      a.setAttribute('data-seg', s.key);
      add(a, el('span', 'blk')).setAttribute('aria-hidden', 'true');
      var text = add(a, el('span', null, s.text));
      if (s.age !== null) ticking(text, s.age, s.prefix);
    });
  }

  // #92: a snapshot vouches for the states it observed until its next
  // publication is overdue (stale_beats publication intervals; a one-off
  // snapshot vouches for none). Past that, a component that is not still
  // inside its up window is unknown, not down: the page cannot see it. Only
  // the dashboard writer, whose beats are the publications, ages to stale
  // and down; with no writer row that can alert, the overdue snapshot is one
  // attention item.
  function agedHealth(d) {
    var h = d.health || {};
    var t = h.thresholds || {};
    var staleBeats = t.stale_beats || 3;
    var downBeats = t.down_beats || 8;
    var age = since();
    var vouched = age <= staleBeats * (Number(h.publish_seconds) || 0);
    var extra = [];
    var writer = false;
    var unknown = false;
    var waiterUnknown = false;
    var rows = (h.components || []).map(function (c) {
      var isWriter = c.component === 'dashboard';
      writer = writer || (isWriter && c.configured !== false);
      var base = c.last_seen_age_seconds;
      if (vouched || c.state === 'down' || base === null || base === undefined) return c;
      var seen = base + age;
      var interval = Math.max(Number(c.interval_seconds) || 15, 1);
      var state = seen > downBeats * interval ? 'down' : seen > staleBeats * interval ? 'stale' : c.state;
      if (!isWriter && (state !== 'up' || c.state !== 'up')) {
        unknown = true;
        waiterUnknown = waiterUnknown || c.component === 'waiter';
        return Object.assign({}, c, {state: 'unknown', reason: 'no snapshot for ' + dur(age)});
      }
      if (state === c.state) return c;
      var reason = 'no heartbeat for ' + dur(seen);
      if (state === 'down' && c.configured !== false) {
        extra.push({kind: 'down', repo: '', ticket_id: '', component: c.label,
                    link: 'health.html#' + c.anchor, detail: 'down (' + reason + ')'});
      }
      return Object.assign({}, c, {state: state, reason: reason});
    });
    if (unknown && !writer) {
      extra.push({kind: 'health', repo: '', ticket_id: '', component: 'health snapshot',
                  link: 'health.html#components', detail: 'unknown: no snapshot for ' + dur(age)});
    }
    return {rows: rows, extra: extra, waiterUnknown: waiterUnknown, age: age,
            key: rows.map(function (c) { return c.state; }).join(',')};
  }

  // --- main page: status, lanes and slots, the queue rail ------------------

  function renderKpis(d) {
    var root = panel('kpis', 'Factory status');
    if (!root) return;
    var t = d.queue.totals;
    var stuck = d.stuck.tickets.length + d.stuck.overdue_jobs.length;
    var row = add(root, el('div', 'kpis'));
    [
      ['running', t.running, 'accent'],
      ['queued', t.queued, t.queued ? 'warn' : ''],
      ['done', t.done, 'ok'],
      ['failed', t.failed, t.failed ? 'bad' : ''],
      ['stuck / overdue', stuck, stuck ? 'bad' : 'ok']
    ].forEach(function (k) {
      var tile = add(row, el('div', 'kpi ' + k[2]));
      add(tile, el('b', null, num(k[1])));
      add(tile, el('span', null, k[0]));
    });
  }

  // Webhook health (#56): a small block appended to the Factory status
  // panel that renderKpis has just rebuilt, so it is redrawn from
  // status.json on every poll; absent when no listener has run. The heading
  // is split so a page without a listener never contains the phrase
  // (tests/test_linear_webhook_health.py).
  function renderLinearWebhook(d) {
    var root = byId('kpis');
    var w = d.poller && d.poller.linear_webhook;
    if (!root || !w) return;
    var received;
    if (w.last_received_age_seconds !== null && w.last_received_age_seconds !== undefined) {
      received = dur(w.last_received_age_seconds) + ' ago' + (w.last_event ? ' (' + w.last_event + ')' : '');
    } else if (w.listener_age_seconds === null || w.listener_age_seconds === undefined) {
      received = 'never (listener has not started)';
    } else {
      received = 'never';
    }
    var box = add(root, el('div'));
    box.setAttribute('id', 'linear-webhook');
    add(box, el('h2', null, 'Linear' + ' webhook'));
    add(box, el('p', null, 'last received ' + received + ' · dirty queue ' + w.dirty));
    if (w.warning !== null && w.warning !== undefined) add(box, el('p', 'bad', 'warning: ' + w.warning));
    if (w.note !== null && w.note !== undefined) add(box, el('p', 'muted', 'note: ' + w.note));
  }

  // Backlog feeder (#85): appended to Factory status like the webhook block.
  var FEEDER_STATE = {'eligible': 'eligible', 'waiting-capacity': 'waiting for capacity',
    'waiting-blocked': 'waiting on a blocker', 'feeding': 'feeding', 'error': 'feed error, retrying',
    'fed': 'fed', 'done': 'fed, job queued', 'closed': 'PR closed, not fed'};
  function renderFeeder(d) {
    var root = byId('kpis');
    var f = d.feeder;
    if (!root || !f) return;
    var box = add(root, el('div'));
    box.setAttribute('id', 'feeder');
    add(box, el('h2', null, 'Feeder'));
    add(box, el('p', null, 'eligible ' + f.eligible + ' · fed ' + f.fed +
      ' · waiting for capacity ' + f.waiting_capacity +
      ' · waiting on blockers or files ' + f.waiting_blocked));
    var list = add(box, el('ul'));
    (f.items || []).forEach(function (item) {
      add(list, el('li', null, item.repo + '#' + item.issue + ' ' +
        (item.reason || FEEDER_STATE[item.state] || item.state) +
        (item.pr ? ' → PR #' + item.pr : '')));
    });
  }

  function slotCard(r) {
    var card = opensModal(link('slot job' + (r.overdue ? ' overdue' : ''), 'job.html#' + anchor(r.job)), r.job);
    card.title = ticketIn(r.repo, r) + ' (job ' + r.job + ')';
    var top = add(card, el('div', 'top'));
    add(top, bot(r.recipient));
    var kind = jobKind(r);
    if (kind) add(top, el('span', 'kind', kind));
    add(top, el('span', 'ticket', jobLabel(r)));
    var state = stateChip(r);
    if (state) add(top, state);
    if (r.overdue) add(top, chip('OVERDUE', 'bad'));
    var step = add(card, el('div', 'step'));
    add(step, el('span', 'action', r.action));
    add(step, el('span', 'round', 'round ' + (r.round === null || r.round === undefined ? '—' : r.round)));
    var times = add(card, el('div', 'times'));
    var elapsed = add(times, el('span'));
    if (r.job_state === 'claimed') {
      // Claimed and never started: no runner, so no T+ or heartbeat bar.
      elapsed.title = 'no orch job run has started this job';
      add(elapsed, el('span', null, 'claimed '));
      ticking(add(elapsed, el('b')), r.elapsed_seconds, '', ' ago');
      add(times, el('span', 'warn', 'not started'));
      return mark('lanes', r.job, card);
    }
    elapsed.title = r.elapsed_from === 'placed' ? 'since it was placed on this device' : 'since it was queued';
    add(elapsed, el('span', null, 'T+ '));
    ticking(add(elapsed, el('b')), r.elapsed_seconds);
    var beat = add(times, el('span'));
    beat.title = 'last heartbeat';
    add(beat, el('span', null, 'hb '));
    ticking(add(beat, el('b')), r.heartbeat_age_seconds, '', ' ago');
    var bar = add(card, el('div', 'hb'));
    var fill = add(bar, el('i'));
    var base = r.heartbeat_age_seconds;
    var timeout = r.timeout_seconds || 1;
    var item = {run: function (dt) {
      var frac = Math.min(1, (base + dt) / timeout);
      fill.style.width = Math.max(2, Math.round(frac * 100)) + '%';
      bar.className = 'hb' + (frac >= 1 ? ' bad' : frac >= 0.6 ? ' warn' : '');
    }};
    tickers.push(item);
    item.run(since());
    return mark('lanes', r.job, card);
  }

  function renderLanes(d) {
    var running = d.running || [];
    var root = panel('lanes', 'Lanes and slots', running.length + ' running');
    if (!root) return;
    var lanes = laneDevices(d);
    var grid = add(root, el('div', 'devices'));
    lanes.devices.forEach(function (dev) {
      var jobs = dev.jobs;
      var used = dev.used;
      var card = add(grid, el('section', 'device' + (dev.enabled ? '' : ' disabled')));
      card.setAttribute('data-device', dev.name);
      var head = add(card, el('header'));
      add(head, el('span', 'name', dev.name));
      if (!dev.enabled) add(head, chip('DISABLED', 'muted'));
      else if (dev.placeholder) add(head, chip('PLACEHOLDER', 'warn'));
      else if (!dev.listed) add(head, chip('UNLISTED', 'warn'));
      else add(head, chip('ONLINE', 'ok'));
      add(head, el('span', 'cap', used + ' / ' + (dev.slot_max === null || dev.slot_max === undefined ? '?' : dev.slot_max) + ' slots'));
      if (dev.harnesses.length) add(card, el('p', 'harness', dev.harnesses.join(' · ')));
      var slots = add(card, el('div', 'slots'));
      jobs.forEach(function (r) { add(slots, slotCard(r)); });
      var held = used - jobs.length;
      for (var h = 0; h < held; h++) add(slots, el('div', 'slot held', 'held'));
      var free = Math.max(0, (dev.slot_max || 0) - used);
      if (!used && !free) free = 1;
      for (var i = 0; i < free; i++) add(slots, el('div', 'slot idle', dev.enabled ? 'idle' : 'off'));
    });
    var wakes = lanes.wakes;
    if (wakes.length) {
      var card = add(grid, el('section', 'device'));
      card.setAttribute('data-device', 'grok-wakes');
      var head = add(card, el('header'));
      add(head, el('span', 'name', 'grok wakes'));
      add(head, chip('BOT-TO-BOT', 'accent'));
      var slots = add(card, el('div', 'slots'));
      wakes.forEach(function (r) { add(slots, slotCard(r)); });
    }
  }

  function renderQueue(d) {
    var rows = d.queued || [];
    var root = panel('queue', 'Queue', rows.length + ' waiting');
    if (!root) return;
    list(root, rows, 'queue empty', function (r) {
      var li = el('li');
      var a = add(li, opensModal(link('queue-item', 'job.html#' + anchor(r.job)), r.job));
      add(a, bot(r.recipient));
      add(a, el('span', 'ticket', jobLabel(r)));
      add(a, el('span', 'what', r.action));
      ticking(add(a, el('span', 'when')), r.age_seconds, 'waiting ');
      if (r.no_waiter) add(li, link('chip bad', healthLink(r.no_waiter_link))).textContent = 'no waiter for ' + r.no_waiter;
      return mark('queue', r.job, li);
    });
  }

  // --- activity page ---------------------------------------------------------

  var ATTENTION = {stuck: 'bad', overdue: 'bad', dead: 'bad', 'needs-attention': 'bad', stale: 'warn', paused: 'warn',
                   down: 'bad', 'no-waiter': 'bad', health: 'bad'};
  function renderAttention(d) {
    var items = attentionItems(d);
    var root = panel('attention', 'Needs attention', items.length + (items.length === 1 ? ' item' : ' items'));
    if (!root) return;
    list(root, items, 'all clear', function (x) {
      var li = el('li');
      add(li, chip(x.kind, ATTENTION[x.kind] || 'muted'));
      if (x.component) add(li, link('ticket', healthLink(x.link))).textContent = x.component;
      else add(li, el('span', 'ticket', ticketOf(x)));
      add(li, el('span', 'what', x.detail));
      return mark('attention', x.kind + ':' + (x.component || x.repo + '#' + x.ticket_id), li);
    });
  }

  var HANDOFF = {scripted: ['scripted', 'ok'], bot_to_bot: ['bot-to-bot', 'accent'],
                 failed: ['failed', 'bad'], done: ['done', 'muted']};
  function renderHandoffs(d) {
    var rows = d.handoff_savings.recent || [];
    var root = panel('handoffs', 'Recent handoffs');
    if (!root) return;
    list(root, rows, 'no finished jobs yet', function (r) {
      var li = el('li');
      var kind = HANDOFF[r.kind] || [r.kind, 'muted'];
      add(li, chip(kind[0], kind[1]));
      add(li, bot(r.recipient));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.action));
      ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
      return mark('handoffs', r.job + ':' + r.state, li);
    });
  }

  var VERDICT = {routed: 'accent', shipped: 'ok', stale: 'muted', rejected: 'warn', stuck: 'bad',
                 blocked: 'bad', waiting: 'warn', other: 'muted'};
  function renderVerdicts(d) {
    var rows = d.poller.verdicts;
    var root = panel('verdicts', 'Recent verdicts');
    if (!root) return;
    if (rows === null || rows === undefined) { add(root, el('p', 'empty', 'no handled comments recorded yet')); return; }
    list(root, rows, 'no verdicts yet', function (r) {
      var li = el('li');
      add(li, chip(r.kind, VERDICT[r.kind] || 'muted'));
      add(li, el('span', 'ticket', ticketOf(r)));
      add(li, el('span', 'what', r.outcome));
      return mark('verdicts', r.repo + '#' + r.ticket_id + ':' + r.outcome, li);
    });
  }

  function renderTickets(d) {
    var rows = d.tickets || [];
    var root = panel('tickets', 'Tickets', rows.length + ' tracked');
    if (!root) return;
    if (!rows.length) { add(root, el('p', 'empty', 'no tracked tickets')); return; }
    var table = add(root, el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['ticket', 'tracker', 'state', 'round', 'gates', 'stuck'].forEach(function (h) { add(head, el('th', null, h)); });
    var tbody = add(table, el('tbody'));
    rows.forEach(function (r) {
      var tr = add(tbody, el('tr'));
      add(tr, el('td', null, ticketIn(r.repo, r)));
      add(tr, el('td', null, r.tracker));
      add(tr, el('td', r.stuck ? 'bad' : null, r.state));
      add(tr, el('td', 'num', r.round));
      add(tr, el('td', 'num', r.gate_count));
      add(tr, el('td', r.stuck ? 'bad' : null, r.stuck ? 'yes' : 'no'));
    });
  }

  function renderLog(d) {
    var log = d.poller.log;
    var root = panel('log', 'poller.log', log.present ? 'written ' + dur(log.age_seconds) + ' before the snapshot' : 'absent');
    if (!root) return;
    add(root, el('p', 'muted', 'last ' + log.tail.length + ' lines, redacted'));
    add(root, el('pre', null, log.tail.join('\n')));
  }

  // --- efficiency page --------------------------------------------------------

  var POLL_STATE = {ok: ['OK', 'ok'], late: ['LATE', 'warn'], stale: ['STALE', 'bad'], unknown: ['UNKNOWN', 'muted']};
  var BUDGET_STATE = {ok: ['OK', 'ok'], deferring: ['DEFERRING', 'warn'], exhausted: ['EXHAUSTED', 'bad'],
                      rate_limited: ['RATE LIMITED', 'bad']};
  function renderHealth(d) {
    var p = d.poller;
    var h = p.health || {state: 'unknown'};
    var b = p.linear_budget || {state: 'ok'};
    var root = panel('health', 'Poll and Linear budget');
    if (!root) return;
    var row = add(root, el('div', 'row'));
    add(row, el('span', 'label', 'poll'));
    var ps = POLL_STATE[h.state] || POLL_STATE.unknown;
    add(row, chip(ps[0], ps[1]));
    if (h.last_cycle_age_seconds === null || h.last_cycle_age_seconds === undefined) {
      add(row, el('span', 'muted', 'cycle time unknown (live push off)'));
    } else {
      ticking(add(row, el('span')), h.last_cycle_age_seconds, 'last cycle ', ' ago');
    }
    var row2 = add(root, el('div', 'row'));
    add(row2, el('span', 'label', 'linear'));
    var bs = BUDGET_STATE[b.state] || ['OTHER', 'muted'];
    add(row2, chip(bs[0], bs[1]));
    add(row2, el('span', 'muted', num(b.requests_per_hour) + ' req/h · ' + num(b.complexity_per_hour) + ' complexity/h'));
    var stats = add(root, el('div', 'stats'));
    [
      [p.errors.last_hour, 'poll errors, 1 h', p.errors.last_hour ? 'bad' : ''],
      [p.errors.last_day, 'poll errors, 24 h', ''],
      [b.deferrals_last_hour, 'Linear deferrals, 1 h', b.deferrals_last_hour ? 'warn' : ''],
      [(b.exhausted_last_hour || 0) + (b.rate_limited_last_hour || 0), 'Linear limit hits, 1 h',
       (b.exhausted_last_hour || b.rate_limited_last_hour) ? 'bad' : '']
    ].forEach(function (s) {
      var box = add(stats, el('div', 'stat'));
      add(box, el('b', s[2], num(s[0])));
      add(box, el('span', null, s[1]));
    });
    var activity = add(root, el('p', 'muted'));
    ticking(activity, p.last_activity_age_seconds, 'last poller activity ', ' ago');
    var recent = (p.errors.recent || []).slice(0, 3);
    if (recent.length) {
      list(root, recent, '', function (r) {
        var li = el('li');
        add(li, el('span', 'ticket', r.repo === 'poll' || r.ticket_id === '-' ? r.repo : ticketOf(r)));
        add(li, el('span', 'what', r.error));
        ticking(add(li, el('span', 'when')), r.age_seconds, '', ' ago');
        return li;
      });
    }
  }

  function renderSavings(d) {
    var s = d.handoff_savings;
    var root = panel('savings', 'Handoff savings');
    if (!root) return;
    var gauge = add(root, el('div', 'gauge'));
    var ring = add(gauge, el('div', 'ring'));
    var pct = s.scripted_share === null ? null : Math.round(s.scripted_share * 100);
    ring.style.setProperty('--p', pct === null ? 0 : pct);
    add(ring, el('span', null, pct === null ? '—' : pct + '%'));
    var stats = add(gauge, el('div'));
    [[s.scripted, 'scripted handoffs', 'accent'], [s.scripted_in_flight, 'scripted in flight', ''],
     [s.bot_to_bot, 'bot-to-bot (grok wakes)', '']].forEach(function (x) {
      var line = add(stats, el('div', 'row'));
      add(line, el('b', x[2], num(x[0])));
      add(line, el('span', 'muted', x[1]));
    });
    var total = (s.scripted || 0) + (s.bot_to_bot || 0);
    var meter = add(root, el('div', 'meter'));
    if (total) {
      add(meter, el('i', 'scripted')).style.width = (100 * s.scripted / total) + '%';
      add(meter, el('i', 'bots')).style.width = (100 * s.bot_to_bot / total) + '%';
    }
    add(root, el('p', 'definition', s.scripted_share_definition)).setAttribute('id', 'share-definition');
    add(details(root, 'savings-def', 'how each count is made'), el('p', 'muted', s.definition));
  }

  // --- job page -----------------------------------------------------------------

  function activeJobs(d) {
    return (d.running || []).map(function (r) { return {row: r, state: 'running'}; })
      .concat((d.queued || []).map(function (r) { return {row: r, state: 'queued'}; }));
  }

  function renderJobs(d) {
    var jobs = activeJobs(d);
    var root = panel('jobs', 'Jobs', (d.running || []).length + ' running · ' + (d.queued || []).length + ' queued');
    if (!root) return;
    list(root, jobs, 'no running or queued jobs', function (j) {
      var li = el('li');
      var a = add(li, link('queue-item', '#' + anchor(j.row.job)));
      add(a, bot(j.row.recipient));
      add(a, el('span', 'ticket', jobLabel(j.row)));
      add(a, el('span', 'what', j.row.action));
      add(a, chip(j.state, j.state === 'running' ? 'accent' : 'warn'));
      return li;
    });
  }

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

  // --- the job modal on the lanes page (#78) ----------------------------------

  // A slot or queue link opens its job's flow in a modal; without script (or
  // with a modifier key) the link still goes to job.html#job-<id>.
  function opensModal(a, job) {
    if (PAGE !== 'main') return a;
    a.addEventListener('click', function (ev) {
      if (ev.defaultPrevented || ev.button || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      openModal(job, a);
    });
    if (modal && modal.job === job) modal.opener = a;
    return a;
  }

  function fillModal() {
    if (!modal || !latest) return;
    var box = modal.box;
    box.replaceChildren();
    var close = add(box, el('button', 'modal-close', '✕ close'));
    close.setAttribute('type', 'button');
    close.setAttribute('aria-label', 'Close');
    close.addEventListener('click', function () { closeModal(); });
    modal.close = close;
    var found = activeJobs(latest).filter(function (j) { return j.row.job === modal.job; })[0];
    if (found) {
      add(box, jobArticle(found.row, found.state, latest));
      modal.node.setAttribute('aria-label', jobLabel(found.row) + ' flow');
    } else {
      var gone = add(box, el('section', 'panel jobpage'));
      add(add(gone, el('h2')), el('span', null, anchor(modal.job)));
      add(gone, el('p', 'empty', 'That job is no longer running or queued.'));
      var ended = finishedTelemetry(latest, modal.job);
      if (ended) add(gone, telemetryBlock(ended));
    }
    var more = add(box, el('p', 'more'));
    add(more, link(null, 'job.html#' + anchor(modal.job))).textContent = 'open on the job page';
  }

  function openModal(job, opener) {
    if (modal && modal.node.parentNode) modal.node.parentNode.removeChild(modal.node);
    var node = el('div', 'modal');
    node.setAttribute('id', 'job-modal');
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-modal', 'true');
    // A click on the backdrop (outside the box) closes it.
    node.addEventListener('click', function (ev) { if (ev.target === node) closeModal(); });
    modal = {job: job, node: node, box: add(node, el('div', 'modal-box')), opener: opener, close: null};
    fillModal();
    add(body, node);
    body.classList.add('modal-open');
    scrollTrails();
    if (modal.close && modal.close.focus) modal.close.focus();
  }

  function closeModal() {
    if (!modal) return;
    var m = modal;
    modal = null;
    if (m.node.parentNode) m.node.parentNode.removeChild(m.node);
    body.classList.remove('modal-open');
    if (m.opener && m.opener.isConnected && m.opener.focus) m.opener.focus();
  }

  document.addEventListener('keydown', function (ev) {
    if (modal && (ev.key === 'Escape' || ev.key === 'Esc')) { ev.preventDefault(); closeModal(); }
  });

  // Long paths scroll; keep the current step (at the right end) in view.
  function scrollTrails() {
    trails.forEach(function (box) { if (box.scrollWidth) box.scrollLeft = box.scrollWidth; });
    trails = [];
  }

__TELEMETRY__

  // --- health page (#92) -------------------------------------------------------

  var HEALTH_STATE = {up: ['UP', 'ok'], stale: ['STALE', 'warn'], down: ['DOWN', 'bad'],
                      unknown: ['UNKNOWN', 'muted']};
  function renderComponents(d) {
    var h = d.health || {known: false, components: []};
    var rows = agedHealth(d).rows;
    var down = rows.filter(function (c) { return c.state === 'down' && c.configured !== false; }).length;
    var root = panel('components', 'Components', rows.length + ' components · ' + down + ' down');
    if (!root) return;
    if (!h.known) { add(root, el('p', 'empty', h.error ? 'unknown: ' + h.error : 'no heartbeats recorded yet')); return; }
    var table = add(root, el('table'));
    var head = add(add(table, el('thead')), el('tr'));
    ['component', 'bot', 'device', 'state', 'last seen (ET)', 'ago', 'pid', 'version', 'started (ET)'].forEach(function (t) { add(head, el('th', null, t)); });
    var tbody = add(table, el('tbody'));
    rows.forEach(function (c) {
      var tr = add(tbody, el('tr'));
      tr.setAttribute('id', c.anchor);
      add(tr, el('td', null, c.component));
      add(tr, el('td', null, c.bot || '—'));
      add(tr, el('td', null, c.device));
      var cell = add(tr, el('td'));
      var st = HEALTH_STATE[c.state] || ['OTHER', 'muted'];
      add(cell, chip(st[0], st[1]));
      if (c.reason) add(cell, el('span', 'muted', ' ' + c.reason));
      if (c.configured === false) add(cell, el('span', 'muted', ' (not configured)'));
      add(tr, el('td', null, c.last_seen_et || 'never'));
      ticking(add(tr, el('td', 'num')), c.last_seen_age_seconds, '', '');
      add(tr, el('td', 'num', c.pid === null || c.pid === undefined ? '—' : c.pid));
      var ver = add(tr, el('td', null, c.version));
      if (c.stale_code) add(ver, el('span', 'warn', ' stale code'));
      add(tr, el('td', null, c.started_at_et || '—'));
      mark('components', c.anchor + ':' + c.state, tr);
    });
    var t = h.thresholds || {};
    add(root, el('p', 'muted', 'up: a beat within ' + (t.stale_beats || 3) + ' intervals (' + (t.beat_seconds || 15) +
      ' s each; device probes every ' + (t.probe_seconds || 60) + ' s) · stale: within ' + (t.down_beats || 8) +
      ' · down: older, stopped or never seen · unknown: this snapshot is too old to tell'));
  }

  function renderNoWaiter(d) {
    var h = d.health || {};
    var rows = h.no_waiter || [];
    var root = panel('no-waiter', 'Queued jobs with no waiter', rows.length ? rows.length + ' harnesses' : 'none');
    if (!root) return;
    if (!rows.length) {
      var aged = agedHealth(d);
      add(root, el('p', 'empty', !h.known ? 'unknown until a component heartbeats'
        : aged.waiterUnknown ? 'unknown: no snapshot for ' + dur(aged.age)
        : 'every queued job has a live waiter'));
      return;
    }
    var ul = add(root, el('ul', 'feed'));
    rows.forEach(function (n) {
      var li = add(ul, el('li'));
      li.setAttribute('id', n.anchor);
      add(li, chip('no waiter', 'bad'));
      add(li, el('span', 'ticket', 'waiter ' + n.bot));
      add(li, el('span', 'what', 'no waiter for ' + n.harness + ': ' + n.count + ' queued job' + (n.count === 1 ? '' : 's') + ' (' + n.jobs.join(', ') + ')'));
      mark('no-waiter', n.anchor, li);
    });
  }

  var PAGES = {
    main: [renderKpis, renderLinearWebhook, renderFeeder, renderLanes, renderQueue],
    job: [renderJobs, renderJobView],
    activity: [renderAttention, renderHandoffs, renderVerdicts, renderTickets, renderLog],
    efficiency: [renderHealth, renderSavings, renderTokens],
    health: [renderComponents, renderNoWaiter]
  };
  var RENDERERS = [renderBand, renderAttentionStrip, renderStatusLine].concat(PAGES[PAGE] || []);

  function render(d) {
    latest = d;
    tickers = [];
    nextKeys = {};
    RENDERERS.forEach(function (draw) {
      try { draw(d); } catch (err) { if (window.console) console.error('dashboard panel', err); }
    });
    if (modal) {
      try { fillModal(); } catch (err) { if (window.console) console.error('dashboard modal', err); }
    }
    scrollTrails();
    PX.bind();
    prevKeys = nextKeys;
    firstRender = false;
    healthKey = agedHealth(d).key;
  }

  function updateLink() {
    var link = byId('link');
    var text = byId('link-text');
    var age = since();
    var state;
    var label;
    if (!everOk) { state = 'boot'; label = failures ? 'NO LINK · static view' : 'ACQUIRING LINK'; }
    else if (Date.now() - lastOk > LINK_LOST_MS) { state = 'lost'; label = 'LINK LOST'; }
    else if (age > STALE_SECONDS) { state = 'stale'; label = 'STALE'; }
    else { state = 'live'; label = 'LIVE'; }
    if (link) link.className = 'link ' + state;
    if (text) text.textContent = label;
    var caret = byId('caret');
    if (caret && state !== caretState) {
      caretState = state;
      caret.replaceChildren(PX.strip('caret', {state: 'link-' + state, height: 12}));
      PX.bind();
    }
    var ageNode = byId('age');
    if (ageNode) ageNode.textContent = dur(age) + ' ago';
    var clock = byId('clock');
    if (clock) clock.textContent = new Date(Date.now()).toISOString().slice(11, 19) + ' UTC';
  }

  function tick() {
    var d = since();
    tickers.forEach(function (t) { t.run(d); });
    if (latest && agedHealth(latest).key !== healthKey) render(latest);
    updateLink();
  }

  function accept(d) {
    if (!d || typeof d !== 'object' || d.schema !== 'factory-dashboard/v1') { failed(); return; }
    lastOk = Date.now();
    everOk = true;
    failures = 0;
    if (firstRender || d.generated_at_epoch !== generated) {
      generated = Number(d.generated_at_epoch) || generated;
      render(d);
    }
    tick();
  }

  function failed() {
    failures += 1;
    if (!everOk && failures >= 3 && !reloadArmed) {
      // status.json cannot be fetched (blocked or missing): behave like a
      // page without script and reload the server-rendered view every
      // refresh_seconds.
      reloadArmed = true;
      setTimeout(function () { location.reload(); }, refreshSeconds * 1000);
    }
    updateLink();
  }

  function schedule() {
    if (timer === null && !inflight && !document.hidden) timer = setTimeout(poll, POLL_MS);
  }

  function poll() {
    timer = null;
    if (inflight || document.hidden) return;
    inflight = true;
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    var guard = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
    var options = {cache: 'no-store', credentials: 'same-origin'};
    if (ctrl) options.signal = ctrl.signal;
    var request;
    try { request = fetch(STATUS_URL, options); } catch (err) { request = Promise.reject(err); }
    request
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(accept, function () { failed(); })
      .then(function () { clearTimeout(guard); inflight = false; schedule(); },
            function () { clearTimeout(guard); inflight = false; schedule(); });
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && timer === null && !inflight) poll();
  });
  if (window.addEventListener) {
    window.addEventListener('hashchange', function () { if (latest) { render(latest); tick(); } });
  }
  setInterval(tick, 1000);
  updateLink();
  PX.bind();
  poll();
})();
"""

# The PixelStrip engine (#138, docs/design/mission-control/SPEC.md section 6):
# the drawing methods of reference/artboards/PixelStrip.dc.html, copied onto
# Strip.prototype, under one shared requestAnimationFrame loop. These lines
# differ from the artboard: decay's amount follows the strip's progress
# (heartbeat age / timeout) instead of a timer, and decay is therefore still;
# a caret with rate 0 is still too (#139: the lost link is static), and a
# still caret is always lit, so a 0.4 Hz caret never freezes dark.
PX_SCRIPT = r"""
(function () {
  var TABLE = __PX__;
  var FPS = 20;
  var DATA = ['progress', 'load', 'seed', 'speed', 'used', 'slots', 'rate', 'done', 'burst'];
  var STRIPS = new Map();
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  var sysStill = !!(mq && mq.matches);
  var io = typeof IntersectionObserver === 'function' ? new IntersectionObserver(seen) : null;
  var raf = 0;
  var last = -1000;

  function clamp(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

  function num(v) {
    if (v === null || v === undefined || v === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function tintOf(v) {
    if (TABLE.bots.hasOwnProperty(v)) return TABLE.bots[v];
    return /^#[0-9a-f]{6}$/.test(String(v)) ? String(v) : TABLE.fallback;
  }

  function readProps(el, beats) {
    var effect = el.getAttribute('data-px');
    var p = {
      effect: TABLE.effects.hasOwnProperty(effect) ? effect : 'off',
      tint: tintOf(el.getAttribute('data-tint')),
      tintMix: 1, cell: 4, fps: FPS, still: effect === 'static', beat: beats
    };
    DATA.forEach(function (k) {
      var v = el.getAttribute('data-' + k);
      if (v !== null) p[k] = v;
    });
    return p;
  }

  function Strip(el) {
    this._el = el;
    this._ctx = null;
    this._sig = null;
    this.props = readProps(el, 0);
    this.beats = 0;
    this.hb = num(el.getAttribute('data-hb'));
    this.onScreen = true;
    this._dead = false;
    this._t0 = performance.now();
    this._beatSeen = 0;
    this._burstSeen = this._num(this.props.burst, 0);
    this._beatAt = -1000;
    this._burstAt = -1000;
    this._doneAt = this._bool(this.props.done) ? -1000 : null;
    this._sysStill = sysStill;
  }

  Strip.prototype._num = function (v, d) {
    if (v === null || v === undefined || v === '') return d;
    var n = Number(v);
    return isFinite(n) ? n : d;
  };

  Strip.prototype._bool = function (v) {
    return v === true || v === 'true';
  };

  Strip.prototype._h = function (x, y) {
    var n = Math.sin(x * 127.1 + y * 311.7 + this._seed) * 43758.5453;
    return n - Math.floor(n);
  };

  Strip.prototype._noise = function (x, y) {
    var xi = Math.floor(x), yi = Math.floor(y);
    var xf = x - xi, yf = y - yi;
    var u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    var a = this._h(xi, yi), b = this._h(xi + 1, yi);
    var c = this._h(xi, yi + 1), d = this._h(xi + 1, yi + 1);
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  };

  Strip.prototype._bayer = function () {
    if (this._bay) return this._bay;
    var m = [0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26,
      12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22,
      3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25,
      15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21];
    var out = [];
    for (var i = 0; i < 64; i++) out.push((m[i] + 0.5) / 64);
    this._bay = out;
    return out;
  };

  Strip.prototype._hex = function (s) {
    var m = /^#?([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(String(s || '').trim());
    if (!m) return null;
    var h = m[1];
    if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
    return [parseInt(h.substr(0, 2), 16), parseInt(h.substr(2, 2), 16), parseInt(h.substr(4, 2), 16)];
  };

  Strip.prototype._mix = function (a, b, k) {
    return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
  };

  Strip.prototype._css = function (c) {
    return 'rgb(' + Math.round(c[0]) + ',' + Math.round(c[1]) + ',' + Math.round(c[2]) + ')';
  };

  /* 5-tone palette: tint mixed into the near-black field, plus a crest. */
  Strip.prototype._pal = function (tint, mixK) {
    var key = String(tint) + '|' + mixK;
    if (!this._pals) this._pals = {};
    if (this._pals[key]) return this._pals[key];
    var bg = [14, 14, 20], green = [158, 206, 106];
    var tc = this._hex(tint) || green;
    var k = mixK < 0 ? 0 : mixK > 1 ? 1 : mixK;
    var c = this._mix(green, tc, k);
    var pal = [null,
      this._css(this._mix(bg, c, 0.3)),
      this._css(this._mix(bg, c, 0.62)),
      this._css(c),
      this._css(this._mix(c, [255, 255, 255], 0.45))];
    this._pals[key] = pal;
    return pal;
  };

  /* Per-frame values each effect needs, computed once. */
  Strip.prototype._prep = function (effect, t, C, R, p) {
    var f = { t: t, C: C, R: R };
    var ph, k;
    if (effect === 'field' || effect === 'decay') {
      f.ft = effect === 'decay' ? 3.1 : t;
      f.gain = 0.2 + 0.8 * Math.max(0, Math.min(1, this._num(p.load, 1)));
      if (effect === 'field') {
        k = Math.floor(t / 2.6);
        f.rAge = t - k * 2.6;
        f.rx = this._h(k, 7.3) * C;
        f.ry = this._h(k, 9.1) * R;
      } else {
        f.frac = Math.min(0.85, Math.floor(Math.max(0, Math.min(1, this._num(p.progress, 0))) * 10) * 0.085);
      }
    } else if (effect === 'fill') {
      f.front = Math.max(0, Math.min(1, this._num(p.progress, 0.5))) * C;
    } else if (effect === 'scan') {
      ph = (t / 3.6) % 1;
      f.dir = ph < 0.5 ? 1 : -1;
      f.head = Math.floor((ph < 0.5 ? ph * 2 : 2 - ph * 2) * (C - 1));
    } else if (effect === 'patch') {
      f.pass = Math.floor(t / 3.4);
      ph = (t / 3.4) % 1;
      f.head = Math.floor(Math.min(1, ph / 0.82) * (C + 2)) - 1;
    } else if (effect === 'spin3') {
      f.step = Math.floor(t * 6) % 8;
      f.s = R >= 6 ? 2 : 1;
      f.y0 = Math.max(0, Math.floor((R - 3 * f.s) / 2));
    } else if (effect === 'twinkle') {
      k = Math.floor(t / 3.3);
      f.env = Math.sin(Math.PI * ((t / 3.3) % 1));
      f.ax = Math.floor(this._h(k, 1.1) * C);
      f.ay = Math.floor(this._h(k, 2.2) * R);
      f.bx = Math.floor(this._h(k, 3.3) * C);
      f.by = Math.floor(this._h(k, 4.4) * R);
    } else if (effect === 'conveyor') {
      f.off = Math.floor(t * Math.max(0.5, Math.min(6, this._num(p.speed, 1))));
      f.row = Math.floor(R / 2);
    } else if (effect === 'meter') {
      f.slots = Math.max(1, Math.round(this._num(p.slots, 8)));
      f.used = Math.round(this._num(p.used, 0));
      f.bw = Math.max(2, Math.floor(C / f.slots));
    } else if (effect === 'caret') {
      var rate = this._num(p.rate, 1);
      f.on = rate <= 0 || this._bool(p.still) || this._sysStill ? true : ((t * rate) % 1) < 0.5;
    }
    return f;
  };

  /* Intensity 0..1 for one cell. */
  Strip.prototype._val = function (effect, f, c, r) {
    var v, d, rb, hc, colH, code, inLine, gx, gy, idx, blk, cyc;
    switch (effect) {
      case 'field':
      case 'decay':
        v = this._noise(c * 0.16 + f.ft * 0.22, r * 0.3 - f.ft * 0.07) * 0.85
          + (0.5 + 0.5 * Math.sin(f.ft * 1.05 + this._h(c, r) * 6.283)) * 0.2 - 0.14;
        if (effect === 'field') {
          if (f.rAge < 0.9) {
            d = Math.sqrt((c - f.rx) * (c - f.rx) + (r - f.ry) * (r - f.ry));
            d = 1 - Math.abs(d - f.rAge * 16) / 1.6;
            if (d > 0) v += d * (1 - f.rAge / 0.9) * 0.7;
          }
          return v * f.gain;
        }
        if (this._h(c + 50.5, r + 50.5) < f.frac) v = Math.min(v, 0.16);
        return v;

      case 'fill':
        colH = (f.front - c) / 7 + (this._h(c, 3.7) - 0.5) * 0.3;
        colH = colH < 0 ? 0 : colH > 1 ? 1 : colH;
        hc = Math.round(colH * f.R);
        rb = f.R - 1 - r;
        if (rb < hc) return (rb === hc - 1 && hc < f.R) ? 0.86 : 0.5 + 0.22 * this._h(c, r);
        if (hc < f.R && c < f.front + 2 && c > f.front - 9) {
          cyc = Math.floor(f.t / 1.2 + this._h(c, 5.9));
          if (this._h(c + cyc * 7.7, 8.8) < 0.55) {
            d = Math.floor(((f.t / 1.2 + this._h(c, 5.9)) % 1) * (f.R - hc + 2));
            if (r === d) return 1;
          }
        }
        return rb === 0 ? 0.2 : 0;

      case 'scan':
      case 'patch':
        code = (r % 2) === 1;
        inLine = code
          && c >= Math.floor(this._h(r, 13.1) * 3) * 2
          && c < (0.4 + 0.58 * this._h(r, 11.3)) * f.C
          && this._h(Math.floor(c / 6), r + 20.2) > 0.2;
        v = inLine ? 0.27 : 0;
        if (effect === 'scan') {
          d = (f.head - c) * f.dir;
          if (d >= 0 && d < 2) v += code ? 1 : 0.5;
          else if (d >= 2 && d < 8) v += (code ? 0.8 : 0.4) * (1 - (d - 2) / 6);
          return v;
        }
        if (inLine && this._h(c + f.pass * 31.7, r + f.pass * 17.3) < 0.16) v = c <= f.head ? 0.86 : 0;
        d = f.head - c;
        if (d >= 0 && d < 2) v = Math.max(v, code ? 1 : 0.45);
        return v;

      case 'spin3':
        if (c < 1 || r < f.y0) return 0;
        gx = Math.floor((c - 1) / f.s);
        gy = Math.floor((r - f.y0) / f.s);
        if (gx > 2 || gy > 2 || (gx === 1 && gy === 1)) return 0;
        idx = gy === 0 ? gx : gx === 2 ? 2 + gy : gy === 2 ? 6 - gx : 7;
        d = (f.step - idx + 8) % 8;
        return d === 0 ? 1 : d === 1 ? 0.75 : d === 2 ? 0.5 : 0.25;

      case 'static':
        return 0.375;

      case 'twinkle':
        return ((c === f.ax && r === f.ay) || (c === f.bx && r === f.by)) ? f.env * 0.62 : 0;

      case 'conveyor':
        if (r !== f.row) return 0;
        return ((c + f.off) % 4) === 0 ? 0.75 : 0.1;

      case 'meter':
        blk = Math.floor(c / f.bw);
        if (blk >= f.slots || (c % f.bw) === f.bw - 1) return 0;
        return blk < f.used ? 0.75 + 0.2 * Math.sin(f.t * 2.2 + blk * 0.9) : 0.25;

      case 'caret':
        return f.on ? 0.75 : 0;

      default:
        return 0;
    }
  };

  Strip.prototype._draw = function (now) {
    var el = this._el || (this._cid ? document.getElementById(this._cid) : null);
    if (!el || !el.getContext) return;
    var p = this.props;
    var cssW = el.clientWidth, cssH = el.clientHeight;
    if (!cssW || !cssH) return;

    var cell = Math.max(2, Math.min(16, Math.round(this._num(p.cell, 4))));
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var W = Math.round(cssW * dpr), H = Math.round(cssH * dpr);
    if (el.width !== W || el.height !== H) {
      el.width = W;
      el.height = H;
      this._sig = null;
    }
    var ctx = this._ctx || (this._ctx = el.getContext('2d'));
    if (!ctx) return;

    var C = Math.ceil(cssW / cell), R = Math.max(1, Math.round(cssH / cell));
    var effect = String(p.effect || 'field');
    var still = this._bool(p.still) || this._sysStill;
    var seed = this._num(p.seed, 1);
    this._seed = seed * 17.31;
    var t = still ? 2.4 : (now - this._t0) / 1000 + seed * 1.37;

    /* one-shot triggers: a prop that changes is a real event */
    var beat = this._num(p.beat, 0), burst = this._num(p.burst, 0), done = this._bool(p.done);
    if (beat !== this._beatSeen) { this._beatSeen = beat; this._beatAt = t; }
    if (burst !== this._burstSeen) { this._burstSeen = burst; this._burstAt = t; }
    if (done && this._doneAt === null) this._doneAt = t;
    if (!done) this._doneAt = null;

    var beatAge = still ? 99 : t - this._beatAt;
    var burstAge = still ? 99 : t - this._burstAt;
    var doneAge = this._doneAt === null ? -1 : (still ? 99 : t - this._doneAt);

    /* skip the frame when nothing on this strip can have changed */
    var oneShot = beatAge < 0.7 || burstAge < 0.5 || (doneAge >= 0 && doneAge < 0.8);
    var loops = !(effect === 'static' || effect === 'off' || effect === 'decay' || doneAge >= 0.8
      || (effect === 'caret' && this._num(p.rate, 1) <= 0));
    var animated = !still && (loops || oneShot);
    var sig = [effect, p.tint, p.tintMix, p.progress, p.used, p.slots, p.load, cell, W, H, done, still].join('|');
    if (!animated && sig === this._sig) return;
    this._sig = animated ? null : sig;

    var pal = this._pal(p.tint, this._num(p.tintMix, 1));
    var palDone = this._pal('#9ece6a', 1);
    var bay = this._bayer();
    var f = this._prep(effect, t, C, R, p);
    var px = cell * dpr;
    var size = Math.ceil(px);
    var doneFront = doneAge >= 0 ? (doneAge / 0.6) * (C + 3) : -1;
    var rk = beatAge / 0.6;
    var rippleFront = (1 - (1 - rk) * (1 - rk)) * C * 0.7;
    var last = null;
    var r, c, v, usePal, d, s, b, lvl, col;

    ctx.fillStyle = '#0e0e14';
    ctx.fillRect(0, 0, W, H);

    for (r = 0; r < R; r++) {
      for (c = 0; c < C; c++) {
        usePal = pal;
        if (doneAge >= 0 && c < doneFront) {
          /* done settles: a green sweep, then still */
          usePal = palDone;
          v = (doneFront - c < 3) ? 1 : 0.44;
        } else {
          v = this._val(effect, f, c, r);
          if (beatAge < 0.6) {
            /* heartbeat ripple from the left edge */
            d = rippleFront - c;
            if (d >= 0 && d < 4) v = Math.max(v, (1 - rk) * (1.15 - d / 4));
          }
          if (burstAge < 0.45 && this._h(c + this._burstSeen * 13.7, r + 5.1) < 0.4) {
            v = Math.max(v, 1 - burstAge / 0.45);
          }
        }
        if (v <= 0) continue;
        if (v > 1) v = 1;
        s = v * 4;
        b = Math.floor(s);
        lvl = b + ((s - b) > bay[((r & 7) << 3) | (c & 7)] ? 1 : 0);
        if (lvl <= 0) continue;
        if (lvl > 4) lvl = 4;
        col = usePal[lvl];
        if (col !== last) { ctx.fillStyle = col; last = col; }
        ctx.fillRect(Math.round(c * px), Math.round(r * px), size, size);
      }
    }
  };

  function paint(s, now) {
    if (s._dead || !s.onScreen || document.hidden) return;
    try { s._draw(now); } catch (e) { s._dead = true; }
  }

  function start() {
    if (!raf && !document.hidden && window.requestAnimationFrame) raf = window.requestAnimationFrame(tick);
  }

  function tick(now) {
    raf = 0;
    if (document.hidden) return;
    if (now - last < 1000 / FPS - 3) { start(); return; }
    last = now;
    var live = false;
    STRIPS.forEach(function (s) {
      if (s._dead || !s.onScreen) return;
      paint(s, now);
      if (!s._dead && s._sig === null) live = true;
    });
    if (live) start();
  }

  function seen(entries) {
    entries.forEach(function (entry) {
      var s = STRIPS.get(entry.target.getAttribute('data-px-key'));
      if (s && s._el === entry.target) s.onScreen = !!entry.isIntersecting;
    });
    start();
  }

  function bind() {
    var found = document.querySelectorAll ? document.querySelectorAll('canvas.px') : [];
    var next = new Map();
    for (var i = 0; i < found.length; i++) {
      var el = found[i];
      var key = el.getAttribute('data-px-key');
      if (!key || next.has(key)) continue;
      var s = STRIPS.get(key);
      if (!s) {
        s = new Strip(el);
        if (io) io.observe(el);
      } else {
        var hb = num(el.getAttribute('data-hb'));
        if (hb !== null && s.hb !== null && hb < s.hb) s.beats += 1;
        s.hb = hb;
        if (s._el !== el) {
          if (io) { io.unobserve(s._el); io.observe(el); }
          s._el = el;
          s._ctx = null;
          s._sig = null;
        }
        s.props = readProps(el, s.beats);
      }
      next.set(key, s);
      paint(s, performance.now());
    }
    STRIPS.forEach(function (s, key) { if (!next.has(key) && io) io.unobserve(s._el); });
    STRIPS = next;
    start();
  }

  function state(row) {
    if (!row) return 'unknown';
    if (row.job_state === 'dead') return 'dead';
    if (row.overdue) return 'overdue';
    if (row.job_state === 'stale') return 'stale';
    if (row.job_state === 'claimed') return 'claimed';
    if (row.job_state === 'running' && TABLE.actions.hasOwnProperty(row.action)) return TABLE.actions[row.action];
    return 'unknown';
  }

  function progressOf(name, row) {
    var timeout = Number(row.timeout_seconds) || 1;
    if (name === 'build') return clamp((Number(row.elapsed_seconds) || 0) / timeout);
    if (name === 'stale') return clamp((Number(row.heartbeat_age_seconds) || 0) / timeout);
    return null;
  }

  function strip(key, opts) {
    var entry = TABLE.states[opts.state] || TABLE.states.unknown;
    var effect = opts.effect || entry.effect;
    var tint = opts.tint || entry.tint;
    if (tint === 'bot') tint = TABLE.bots.hasOwnProperty(opts.bot) ? opts.bot : TABLE.fallback;
    if (!TABLE.bots.hasOwnProperty(tint) && !/^#[0-9a-f]{6}$/.test(String(tint))) tint = TABLE.fallback;
    var height = Math.max(2, Math.min(96, Math.round(Number(opts.height) || 16)));
    var el = document.createElement('canvas');
    el.setAttribute('class', 'px');
    el.setAttribute('data-px', TABLE.effects.hasOwnProperty(effect) ? effect : 'off');
    el.setAttribute('data-tint', tint);
    var style = '--px:' + (TABLE.bots.hasOwnProperty(tint) ? 'var(--' + tint + ')' : tint) + ';';
    if (opts.progress !== null && opts.progress !== undefined) {
      var p = clamp(Number(opts.progress) || 0).toFixed(3);
      el.setAttribute('data-progress', p);
      style += '--p:' + p + ';';
    }
    if (opts.heartbeat !== null && opts.heartbeat !== undefined) el.setAttribute('data-hb', String(Math.max(0, Math.floor(Number(opts.heartbeat) || 0))));
    if (opts.load !== null && opts.load !== undefined) {
      var l = clamp(Number(opts.load) || 0).toFixed(3);
      el.setAttribute('data-load', l);
      style += '--l:' + l + ';';
    }
    var rate = opts.rate !== null && opts.rate !== undefined ? opts.rate : entry.rate;
    if (rate !== null && rate !== undefined) el.setAttribute('data-rate', String(Number(rate)));
    var seed = opts.seed !== null && opts.seed !== undefined ? opts.seed : entry.seed;
    if (seed !== null && seed !== undefined) el.setAttribute('data-seed', String(Number(seed)));
    el.setAttribute('data-px-key', String(key));
    el.setAttribute('aria-hidden', 'true');
    el.setAttribute('style', style + 'height:' + height + 'px');
    return el;
  }

  function jobStrip(row, height) {
    var name = state(row);
    return strip('job-' + row.job, {
      state: name, bot: row.recipient, progress: progressOf(name, row),
      heartbeat: name === 'claimed' ? null : row.heartbeat_age_seconds, height: height
    });
  }

  function frame(name) { return (TABLE.states[name] || {}).frame || ''; }

  document.addEventListener('visibilitychange', function () { if (!document.hidden) start(); });
  if (mq) {
    var onMotion = function () {
      sysStill = !!mq.matches;
      STRIPS.forEach(function (s) { s._sysStill = sysStill; s._sig = null; paint(s, performance.now()); });
      start();
    };
    if (mq.addEventListener) mq.addEventListener('change', onMotion);
    else if (mq.addListener) mq.addListener(onMotion);
  }

  return {bind: bind, state: state, strip: strip, jobStrip: jobStrip, frame: frame, strips: function () { return STRIPS; }, Strip: Strip, table: TABLE};
})()
"""


@functools.lru_cache(maxsize=1)
def font_faces() -> str:
    """The vendored fonts as a JSON list of {family, weight, data} for the page
    script, data being the woff2 file in base64. A missing or unreadable file
    is left out, and the page then uses the fallback fonts."""
    faces = []
    for family, name, weight in FONTS:
        try:
            data = (FONT_DIR / name).read_bytes()
        except OSError:
            continue
        if data[:4] != b"wOF2":
            continue
        faces.append(
            {"family": family, "weight": weight, "data": base64.b64encode(data).decode("ascii")}
        )
    return json.dumps(faces, separators=(",", ":"))


def px_script() -> str:
    return PX_SCRIPT.strip().replace("__PX__", json.dumps(px_table(), separators=(",", ":")))


def page_script() -> str:
    return (
        SCRIPT.replace("__TELEMETRY__", TELEMETRY_SCRIPT)
        .replace("__PIXELSTRIP__", px_script())
        .replace("__SHELL__", json.dumps(shell_table(), separators=(",", ":")))
        .replace("__POLL_MS__", str(POLL_MS))
        .replace("__FLOW__", json.dumps(path_geometry(), separators=(",", ":")))
        .replace("__FONTS__", font_faces())
    )


# --- server-side rendering ----------------------------------------------------------
#
# Each page's panels are rendered here with the same markup the script
# builds, so a page reads the same without script (it then reloads every
# refresh_seconds) and the script only has to replace their contents.

# In nav order (#78).
PAGES = ("index.html", "job.html", "activity.html", "efficiency.html", "health.html")
_PAGE_NAMES = {
    "index.html": ("main", "Lanes"),
    "job.html": ("job", "Tickets"),
    "activity.html": ("activity", "Activity"),
    "efficiency.html": ("efficiency", "Efficiency"),
    "health.html": ("health", "System"),
}
LIST_MAX = 12
_BOTS = ("claude", "codex", "cursor", "grok")


def _e(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return html.escape(str(value), quote=True)


# --- PixelStrip (#138, SPEC section 6) ----------------------------------------------
#
# One table maps a state to its strip effect and tint; the page script gets
# the same table (px_table) so the server and the script cannot drift. An
# effect is animated when it changes from frame to frame.
PX_EFFECTS = {
    "field": True,
    "fill": True,
    "scan": True,
    "patch": True,
    "spin3": True,
    "decay": False,
    "static": False,
    "twinkle": True,
    "conveyor": True,
    "meter": True,
    "caret": True,
    "off": False,
}
PX_ONESHOTS = {"beat": 600, "burst": 450, "done": 600}
PX_BOT_TINTS = {"claude": "#e0af68", "codex": "#7aa2f7", "cursor": "#bb9af7", "grok": "#73daca"}
PX_FALLBACK = "#9ece6a"
_PX_BAD = "#f7768e"
PX_BROKEN = ("dead", "overdue", "stuck", "blocked", "capped", "refused", "no_waiter", "down")
_PX_LINK = {"live": ("#9ece6a", 1), "stale": ("#ff9e64", 0.4), "lost": (_PX_BAD, 0), "boot": ("#8b93b8", 0)}
PX_STATE: dict[str, dict[str, str | float]] = {
    "plan": {"effect": "field", "tint": "bot"},
    "build": {"effect": "fill", "tint": "bot"},
    "review": {"effect": "scan", "tint": "bot"},
    "fix": {"effect": "patch", "tint": "bot"},
    "claimed": {"effect": "spin3", "tint": "bot"},
    "stale": {"effect": "decay", "tint": "bot"},
    **{name: {"effect": "static", "tint": _PX_BAD} for name in PX_BROKEN},
    "idle": {"effect": "twinkle", "tint": PX_FALLBACK},
    "held": {"effect": "off", "tint": PX_FALLBACK, "frame": "px-held"},
    "unknown": {"effect": "off", "tint": PX_FALLBACK},
    # #139: the brand caret per link state (rate 1 blinks at 1 Hz, 0 is solid).
    **{f"link-{name}": {"effect": "caret", "tint": tint, "rate": rate} for name, (tint, rate) in _PX_LINK.items()},
    "band": {"effect": "field", "tint": PX_FALLBACK, "seed": 21},
    "attention": {"effect": "static", "tint": _PX_BAD, "seed": 5},
}
PX_ACTIONS = {"plan": "plan", "build": "build", "review": "review", "fix": "fix"}
_PX_HEX = re.compile(r"#[0-9a-f]{6}")


def px_table() -> dict:
    return {
        "effects": PX_EFFECTS,
        "oneshots": PX_ONESHOTS,
        "states": PX_STATE,
        "actions": PX_ACTIONS,
        "bots": PX_BOT_TINTS,
        "fallback": PX_FALLBACK,
    }


def px_state(row: dict) -> str:
    """The PX_STATE key for a running job row of the snapshot. Broken rows win
    over the action, and anything the table does not know is "unknown" (still)."""
    job_state = row.get("job_state")
    if job_state == "dead":
        return "dead"
    if row.get("overdue"):
        return "overdue"
    if job_state in ("stale", "claimed"):
        return str(job_state)
    if job_state == "running" and row.get("action") in PX_ACTIONS:
        return PX_ACTIONS[row["action"]]
    return "unknown"


def _px_progress(state: str, row: dict) -> float | None:
    timeout = row.get("timeout_seconds") or 1
    if state == "build":
        return (row.get("elapsed_seconds") or 0) / timeout
    if state == "stale":
        return (row.get("heartbeat_age_seconds") or 0) / timeout
    return None


def px_frame(state: str) -> str:
    """The class for the box around a strip in this state ("px-held": the
    marching dashed border), or ""."""
    return PX_STATE.get(state, {}).get("frame", "")


def _fixed3(value: float) -> str:
    """0..1 to three places, rounded half up like the script's toFixed(3)."""
    return str(Decimal(max(0.0, min(1.0, float(value)))).quantize(Decimal("0.001"), ROUND_HALF_UP))


def px_strip(
    key: object,
    *,
    state: str = "unknown",
    effect: str | None = None,
    tint: str | None = None,
    bot: object = None,
    progress: float | None = None,
    heartbeat: float | None = None,
    load: float | None = None,
    rate: float | None = None,
    seed: float | None = None,
    height: int = 16,
) -> str:
    """A PixelStrip canvas (the script draws it; without script its CSS
    gradients are the still frame). `state` picks the effect, tint, rate and
    seed from PX_STATE; the keywords override them. Same markup as PX.strip."""
    entry = PX_STATE.get(state, PX_STATE["unknown"])
    effect = str(effect or entry["effect"])
    tint = tint or entry["tint"]
    rate = entry.get("rate") if rate is None else rate
    seed = entry.get("seed") if seed is None else seed
    if tint == "bot":
        tint = str(bot) if bot in PX_BOT_TINTS else PX_FALLBACK
    if tint not in PX_BOT_TINTS and not _PX_HEX.fullmatch(str(tint)):
        tint = PX_FALLBACK
    height = max(2, min(96, round(height)))
    attrs = [
        ("class", "px"),
        ("data-px", effect if effect in PX_EFFECTS else "off"),
        ("data-tint", tint),
    ]
    style = f"--px:{f'var(--{tint})' if tint in PX_BOT_TINTS else tint};"
    if progress is not None:
        value = _fixed3(progress)
        attrs.append(("data-progress", value))
        style += f"--p:{value};"
    if heartbeat is not None:
        attrs.append(("data-hb", str(max(0, int(heartbeat)))))
    if load is not None:
        value = _fixed3(load)
        attrs.append(("data-load", value))
        style += f"--l:{value};"
    if rate is not None:
        attrs.append(("data-rate", f"{float(rate):g}"))
    if seed is not None:
        attrs.append(("data-seed", f"{float(seed):g}"))
    attrs += [("data-px-key", str(key)), ("aria-hidden", "true"), ("style", f"{style}height:{height}px")]
    return "<canvas " + " ".join(f'{name}="{_e(value)}"' for name, value in attrs) + "></canvas>"


def px_job_strip(row: dict, height: int = 16) -> str:
    """The strip of a running job row, keyed by its job id (PX.jobStrip)."""
    state = px_state(row)
    return px_strip(
        f"job-{row.get('job')}",
        state=state,
        bot=row.get("recipient"),
        progress=_px_progress(state, row),
        heartbeat=None if state == "claimed" else row.get("heartbeat_age_seconds"),
        height=height,
    )


def duration(seconds: object) -> str:
    """Like the script's dur(): 45s, 3m 05s, 2h 07m, 1d 4h."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"


def _num(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _e(value)


def _short(repo: object) -> str:
    return str(repo).rsplit("/", 1)[-1]


def _ticket(row: dict, *, full: bool = False) -> str:
    repo = str(row["repo"]) if full else _short(row["repo"])
    return label(repo, str(row["ticket_id"]), row.get("source"), row.get("pr"))


def _job_label(row: dict) -> str:
    if row.get("tracker") == "linear":
        pr = row.get("pr")
        return f"{row['ticket_id']} (PR #{pr})" if pr else str(row["ticket_id"])
    return _ticket(row)


def _job_kind(row: dict) -> str | None:
    return {"github": "PR", "linear": "ticket"}.get(str(row.get("tracker")))


def anchor(job: object) -> str:
    return "job-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(job))


def _b(value: object, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<b{attr}>{_num(value)}</b>"


def _span(cls: str | None, text: object) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<span{attr}>{_e(text)}</span>"


def _bot(name: object) -> str:
    return _span(f"bot {name if name in _BOTS else 'other'}", name)


def _chip(text: object, cls: str = "muted") -> str:
    return _span(f"chip {cls}", text)


def _panel(ident: str, title: str, inner: str, *, count: object = None, extra: str = "") -> str:
    head = _span(None, title) + ("" if count is None else _span("count", count))
    classes = f"panel {extra}".strip()
    return (
        f'<section class="{classes}" id="{ident}" aria-label="{_e(title)}">'
        f"<h2>{head}</h2>{inner}</section>"
    )


def _feed(rows: list, empty: str, draw) -> str:
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    items = "".join(f"<li>{draw(row)}</li>" for row in rows[:LIST_MAX])
    more = (
        f'<p class="more">+{len(rows) - LIST_MAX} more</p>' if len(rows) > LIST_MAX else ""
    )
    return f'<ul class="feed">{items}</ul>{more}'


def _when(seconds: object, prefix: str = "", suffix: str = "") -> str:
    return _span("when", prefix + duration(seconds) + suffix)


# --- the shell on every page (#139) -----------------------------------------------------
#
# The TopBar (brand and its caret, nav, link state, snapshot meta), the header
# band, the attention strip and the status line. Each has a script twin in
# SCRIPT that builds the same markup from status.json.


def _topbar(snapshot: dict, page: str) -> str:
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{name}"{current if name == page else ""}>{label}</a>'
        for name, (_, label) in _PAGE_NAMES.items()
    )
    return (
        '<header class="topbar" id="topbar"><div class="top-row">'
        '<div class="brand"><span>Factory <b>Mission Control</b></span>'
        f'<span class="caret" id="caret" aria-hidden="true">{px_strip("caret", state="link-boot", height=12)}'
        "</span></div>"
        f'<nav class="nav" id="nav" aria-label="Pages">{links}</nav>'
        '<div class="link boot" id="link" role="status" aria-live="polite">'
        '<span class="dot" aria-hidden="true"></span>'
        '<span id="link-text">STATIC SNAPSHOT</span></div>'
        f'<div class="meta">snapshot <span id="age">{_e(snapshot["generated_at"])}</span>'
        ' · <span id="clock"></span> · read-only, redacted</div>'
        "</div>"
        f'<div class="band" id="band">{_band(snapshot)}</div>'
        f"{_attention_strip(snapshot)}"
        "</header>"
    )


def header_load(snapshot: dict) -> float:
    """Running / total slots over the enabled, non-placeholder devices that
    have a slot_max (the header band's brightness), 0 with no slots."""
    devices, _ = lane_devices(snapshot)
    used = total = 0
    for device in devices:
        if device["enabled"] and not device["placeholder"] and device["slot_max"]:
            used += device["used"]
            total += device["slot_max"]
    return max(0.0, min(1.0, used / total)) if total else 0.0


def _band(snapshot: dict) -> str:
    return px_strip("band", state="band", load=header_load(snapshot), height=24)


def _attention_strip(snapshot: dict) -> str:
    """Every item behind the attention count, inline with its chip and link;
    empty and hidden when there is none."""
    items = (snapshot.get("attention") or {}).get("items") or []
    if not items:
        return '<div class="attn" id="attention-strip" hidden></div>'

    def item(row: dict) -> str:
        if row.get("component"):
            href, who = _health_link(row.get("link")), row["component"]
        else:
            href, who = "job.html", _ticket(row)
        return (
            '<div class="attn-item">'
            + _chip(row["kind"], _ATTENTION.get(row["kind"], "muted"))
            + f'<a class="attn-who" href="{_e(href)}">{_e(who)}</a>'
            + _span("attn-what", row["detail"])
            + "</div>"
        )

    need = " needs attention" if len(items) == 1 else " need attention"
    return (
        '<div class="attn" id="attention-strip">'
        '<div class="attn-head"><span class="attn-blk" aria-hidden="true">'
        f'{px_strip("attention", state="attention", height=16)}</span>'
        '<a class="attn-count" href="activity.html#attention">'
        f'<b id="attention-count">{len(items)}</b>{_span(None, need)}</a></div>'
        + "".join(item(row) for row in items)
        + "</div>"
    )


# Where each status-line segment links (System anchors or Lanes).
STATUS_LINKS = {
    "poll": "efficiency.html#health",
    "poll-errors": "efficiency.html#health",
    "linear": "efficiency.html#health",
    "webhook": "index.html#linear-webhook",
    "feeder": "index.html#feeder",
    "components": "health.html#components",
    "version": "health.html#components",
}
# A webhook or feeder segment with no block of its own links to the panel that would hold it.
STATUS_ABSENT_LINK = "index.html#kpis"


def shell_table() -> dict:
    """What the page script's shell needs from here (SHELL in SCRIPT)."""
    return {"links": STATUS_LINKS, "absent": STATUS_ABSENT_LINK, "code": sorted(CODE_COMPONENTS)}


def version_drift(health: dict | None) -> dict:
    """newest: the checkout's version; behind: configured code components on
    another known version (lib/health.py stale_code); unknown: configured code
    components whose version is unknown, not counted as behind."""
    health = health or {}
    rows = [
        row for row in health.get("components") or []
        if row.get("configured") is not False and row.get("component") in CODE_COMPONENTS
    ]
    return {
        "newest": str(health.get("code_version") or "unknown"),
        "behind": [row["label"] for row in rows if row.get("stale_code")],
        "unknown": [row["label"] for row in rows if row.get("version") == "unknown"],
    }


def _segment(key: str, text: str, state: str | None = None, label: str | None = None,
             age: object = None, prefix: str | None = None, href: str | None = None) -> dict:
    return {"key": key, "label": label, "text": text, "state": state,
            "href": href or STATUS_LINKS.get(key), "age": age, "prefix": prefix}


def _count(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def status_segments(snapshot: dict) -> list[dict]:
    """The status line, one segment per figure, each computed from the
    snapshot: devices and queue (plain), then the vitals with a state
    (ok, warn, bad, muted) and a link. Mirrors statusSegments in SCRIPT."""
    out = []
    devices, _ = lane_devices(snapshot)
    for device in devices:
        if not device["enabled"] or device["placeholder"]:
            value = "off"
        else:
            value = f'{device["used"]}/{"—" if device["slot_max"] is None else device["slot_max"]}'
        out.append(_segment(f'device:{device["name"]}', value, label=str(device["name"])))
    out.append(_segment("queue", _count(snapshot["queue"]["totals"]["queued"]), label="queue"))
    poller = snapshot["poller"]
    health = poller.get("health") or {"state": "unknown"}
    label, cls = _POLL_STATE.get(health["state"], _POLL_STATE["unknown"])
    poll = f"poll {label.lower()}"
    cycle = health.get("last_cycle_age_seconds")
    if cycle is None:
        out.append(_segment("poll", poll, cls))
    else:
        out.append(_segment("poll", f"{poll} · {duration(cycle)}", cls, age=cycle, prefix=f"{poll} · "))
    errors = (poller.get("errors") or {}).get("last_hour") or 0
    plural = "" if errors == 1 else "s"
    out.append(_segment("poll-errors", f"{_count(errors)} poll error{plural} / 1 h", "bad" if errors else "ok"))
    budget = poller.get("linear_budget") or {"state": "ok"}
    blabel, bcls = _BUDGET_STATE.get(budget["state"], ("OTHER", "muted"))
    out.append(_segment("linear", f"linear {blabel.lower()}", bcls))
    webhook = poller.get("linear_webhook")
    if not webhook:
        out.append(_segment("webhook", "no webhook", "muted", href=STATUS_ABSENT_LINK))
    elif webhook.get("last_received_age_seconds") is None:
        out.append(_segment("webhook", "webhook never",
                            "bad" if webhook.get("warning") is not None else "warn"))
    else:
        age = webhook["last_received_age_seconds"]
        out.append(_segment("webhook", f"webhook {duration(age)}",
                            "bad" if webhook.get("warning") is not None else "ok",
                            age=age, prefix="webhook "))
    feeder = snapshot.get("feeder")
    if not feeder:
        out.append(_segment("feeder", "no feeder", "muted", href=STATUS_ABSENT_LINK))
    else:
        failing = any(item.get("state") == "error" for item in feeder.get("items") or [])
        out.append(_segment("feeder", f"feeder {_count(feeder['fed'])} fed", "bad" if failing else "ok"))
    components = snapshot.get("health") or {}
    if not components.get("known"):
        out.append(_segment("components", "components unknown",
                            "bad" if components.get("error") else "muted"))
    else:
        rows = [row for row in components.get("components") or [] if row.get("configured") is not False]
        up = sum(1 for row in rows if row["state"] == "up")
        down = any(row["state"] == "down" for row in rows)
        out.append(_segment("components", f"{up} / {len(rows)} up",
                            "bad" if down else "warn" if up < len(rows) else "ok"))
    drift = version_drift(components)
    if drift["newest"] == "unknown":
        out.append(_segment("version", "version unknown", "warn"))
    else:
        behind = len(drift["behind"])
        out.append(_segment("version", f'{drift["newest"][:7]} · {behind} behind',
                            "warn" if behind else "ok"))
    return out


def _statusline(snapshot: dict) -> str:
    parts = ['<span class="sl-brand">factory</span>']
    filled = False
    for seg in status_segments(snapshot):
        if seg["state"] is None:
            cls = "sl-seg sl-queue" if seg["key"] == "queue" else "sl-seg"
            parts.append(
                f'<span class="{cls}" data-seg="{_e(seg["key"])}">'
                f'{_span("sl-k", seg["label"])}{_span("sl-v", seg["text"])}</span>'
            )
            continue
        if not filled:
            parts.append('<span class="sl-fill"></span>')
            filled = True
        parts.append(
            f'<a class="sl-vital {seg["state"]}" href="{_e(seg["href"])}" data-seg="{_e(seg["key"])}">'
            f'<span class="blk" aria-hidden="true"></span>{_span(None, seg["text"])}</a>'
        )
    return f'<footer class="statusline" id="statusline">{"".join(parts)}</footer>'


# --- main page -------------------------------------------------------------------------


def _kpis(snapshot: dict) -> str:
    totals = snapshot["queue"]["totals"]
    stuck = len(snapshot["stuck"]["tickets"]) + len(snapshot["stuck"]["overdue_jobs"])
    tiles = [
        ("running", totals["running"], "accent"),
        ("queued", totals["queued"], "warn" if totals["queued"] else ""),
        ("done", totals["done"], "ok"),
        ("failed", totals["failed"], "bad" if totals["failed"] else ""),
        ("stuck / overdue", stuck, "bad" if stuck else "ok"),
    ]
    inner = "".join(
        f'<div class="kpi {cls}"><b>{_num(value)}</b>{_span(None, label)}</div>'
        for label, value, cls in tiles
    )
    return _panel(
        "kpis",
        "Factory status",
        f'<div class="kpis">{inner}</div>' + _linear_webhook(snapshot) + _feeder(snapshot),
    )


_FEEDER_STATE = {
    "eligible": "eligible",
    "waiting-capacity": "waiting for capacity",
    "waiting-blocked": "waiting on a blocker",
    "feeding": "feeding",
    "error": "feed error, retrying",
    "fed": "fed",
    "done": "fed, job queued",
    "closed": "PR closed, not fed",
}


def _feeder(snapshot: dict) -> str:
    """The backlog feeder (#85), a small block inside Factory status; only
    once a feeder has run. The script's renderFeeder builds the same markup."""
    feeder = snapshot.get("feeder")
    if not feeder:
        return ""
    rows = "".join(
        f"<li>{_e(item['repo'])}#{_e(item['issue'])} "
        f"{_e(item.get('reason') or _FEEDER_STATE.get(item['state'], item['state']))}"
        + (f" → PR #{_e(item['pr'])}" if item.get("pr") else "")
        + "</li>"
        for item in feeder["items"]
    )
    return (
        '<div id="feeder"><h2>Feeder</h2>'
        f"<p>eligible {_e(feeder['eligible'])} · fed {_e(feeder['fed'])} · "
        f"waiting for capacity {_e(feeder['waiting_capacity'])} · "
        f"waiting on blockers or files {_e(feeder.get('waiting_blocked', 0))}</p>"
        f"<ul>{rows}</ul></div>"
    )


def _linear_webhook(snapshot: dict) -> str:
    """Linear webhook health (#56), a small block inside Factory status; only
    when a listener or webhook-mode poller has run on this database. The
    script's renderLinearWebhook builds the same markup from status.json."""
    webhook = snapshot["poller"].get("linear_webhook")
    if webhook is None:
        return ""
    if webhook["last_received_age_seconds"] is not None:
        received = f'{duration(webhook["last_received_age_seconds"])} ago' + (
            f' ({webhook["last_event"]})' if webhook["last_event"] else ""
        )
    elif webhook["listener_age_seconds"] is None:
        received = "never (listener has not started)"
    else:
        received = "never"
    warning = (
        f'<p class="bad">warning: {_e(webhook["warning"])}</p>'
        if webhook["warning"] is not None
        else ""
    )
    if webhook.get("note") is not None:
        warning += f'<p class="muted">note: {_e(webhook["note"])}</p>'
    return (
        '<div id="linear-webhook"><h2>Linear webhook</h2>'
        f"<p>last received {_e(received)} · dirty queue {_e(webhook['dirty'])}</p>"
        f"{warning}</div>"
    )


# #75: lib/job_state.py's state of a running job, as a chip (JOB_STATE in the script).
_JOB_STATE = {
    "claimed": ("claimed, not started", "warn"),
    "running": ("running", "ok"),
    "stale": ("stale heartbeat", "warn"),
    "dead": ("runner dead", "bad"),
}


def _state_chip(row: dict) -> str:
    found = _JOB_STATE.get(row.get("job_state"))  # type: ignore[arg-type]
    return _chip(found[0], found[1]) if found else ""


def _slot(row: dict) -> str:
    kind = _job_kind(row)
    rnd = "—" if row.get("round") is None else row["round"]
    age = row["heartbeat_age_seconds"]
    frac = min(1.0, age / (row["timeout_seconds"] or 1))
    level = " bad" if frac >= 1 else " warn" if frac >= 0.6 else ""
    since = (
        "since it was placed on this device"
        if row.get("elapsed_from") == "placed"
        else "since it was queued"
    )
    if row.get("job_state") == "claimed":
        # Claimed and never started: no runner, so no T+ or heartbeat bar.
        times = (
            '<div class="times"><span title="no orch job run has started this job">'
            f'<span>claimed </span><b>{duration(row["elapsed_seconds"])} ago</b></span>'
            + _span("warn", "not started")
            + "</div>"
        )
    else:
        times = (
            f'<div class="times"><span title="{since}"><span>T+ </span>'
            f'<b>{duration(row["elapsed_seconds"])}</b></span>'
            f'<span title="last heartbeat"><span>hb </span><b>{duration(age)} ago</b></span></div>'
            f'<div class="hb{level}"><i style="width:{max(2, round(frac * 100))}%"></i></div>'
        )
    return (
        f'<a class="slot job{" overdue" if row["overdue"] else ""}" '
        f'href="job.html#{anchor(row["job"])}" '
        f'title="{_e(_ticket(row, full=True))} (job {_e(row["job"])})">'
        f'<div class="top">{_bot(row["recipient"])}'
        + (_span("kind", kind) if kind else "")
        + _span("ticket", _job_label(row))
        + _state_chip(row)
        + (_chip("OVERDUE", "bad") if row["overdue"] else "")
        + "</div>"
        f'<div class="step">{_span("action", row["action"])}{_span("round", f"round {rnd}")}</div>'
        + times
        + "</a>"
    )


def lane_devices(snapshot: dict) -> tuple[list[dict], list[dict]]:
    """The lanes the script draws: (devices with their running jobs, grok
    wakes). Mirrors renderLanes in the page script."""
    slots = snapshot["slots"]
    devices = [
        {
            "name": row["name"],
            "slot_max": row["slot_max"],
            "held": row.get("held") or 0,
            "enabled": row["enabled"],
            "placeholder": row["placeholder"],
            "harnesses": row["harnesses"],
            "listed": True,
            "jobs": [],
        }
        for row in slots["devices"]
    ]
    by_name = {device["name"]: device for device in devices}
    wakes: list[dict] = []
    for row in snapshot["running"]:
        if row["device"] is None:
            wakes.append(row)
            continue
        if row["device"] not in by_name:
            device = {
                "name": row["device"],
                "slot_max": slots["box"]["slot_max"] if row["device"] == "box" else None,
                "held": 0,
                "enabled": True,
                "placeholder": False,
                "harnesses": [],
                "listed": row["device"] == "box" and not devices,
                "jobs": [],
            }
            devices.append(device)
            by_name[row["device"]] = device
        by_name[row["device"]]["jobs"].append(row)
    if not devices:
        devices.append(
            {"name": "box", "slot_max": slots["box"]["slot_max"], "held": 0, "enabled": True,
             "placeholder": False, "harnesses": [], "listed": True, "jobs": []}
        )
    for device in devices:
        # #63 FU-1: a slot held in device_slots is in use even when its job
        # is not (or no longer) running, so a lane counts max(held, running).
        device["used"] = max(device["held"], len(device["jobs"]))
    return devices, wakes


def _lanes(snapshot: dict) -> str:
    devices, wakes = lane_devices(snapshot)
    cards = []
    for device in devices:
        if not device["enabled"]:
            state = _chip("DISABLED", "muted")
        elif device["placeholder"]:
            state = _chip("PLACEHOLDER", "warn")
        elif not device["listed"]:
            state = _chip("UNLISTED", "warn")
        else:
            state = _chip("ONLINE", "ok")
        cap = "?" if device["slot_max"] is None else device["slot_max"]
        used = device["used"]
        held = used - len(device["jobs"])
        free = max(0, (device["slot_max"] or 0) - used)
        if not used and not free:
            free = 1
        slots = "".join(_slot(row) for row in device["jobs"])
        slots += '<div class="slot held">held</div>' * held
        slots += f'<div class="slot idle">{"idle" if device["enabled"] else "off"}</div>' * free
        harness = (
            f'<p class="harness">{_e(" · ".join(device["harnesses"]))}</p>'
            if device["harnesses"]
            else ""
        )
        cards.append(
            f'<section class="device{"" if device["enabled"] else " disabled"}" '
            f'data-device="{_e(device["name"])}">'
            f'<header>{_span("name", device["name"])}{state}'
            f'{_span("cap", f"{used} / {cap} slots")}</header>'
            f'{harness}<div class="slots">{slots}</div></section>'
        )
    if wakes:
        cards.append(
            '<section class="device" data-device="grok-wakes"><header>'
            f'{_span("name", "grok wakes")}{_chip("BOT-TO-BOT", "accent")}</header>'
            f'<div class="slots">{"".join(_slot(row) for row in wakes)}</div></section>'
        )
    return _panel(
        "lanes",
        "Lanes and slots",
        f'<div class="devices">{"".join(cards)}</div>',
        count=f"{len(snapshot['running'])} running",
    )


def _queue(snapshot: dict) -> str:
    rows = snapshot["queued"]

    def draw(row: dict) -> str:
        return (
            f'<a class="queue-item" href="job.html#{anchor(row["job"])}">'
            f'{_bot(row["recipient"])}{_span("ticket", _job_label(row))}'
            f'{_span("what", row["action"])}{_when(row["age_seconds"], "waiting ")}</a>'
            + (
                f'<a class="chip bad" href="{_e(_health_link(row.get("no_waiter_link")))}">'
                f'no waiter for {_e(row["no_waiter"])}</a>'
                if row.get("no_waiter")
                else ""
            )
        )

    return _panel(
        "queue", "Queue", _feed(rows, "queue empty", draw),
        count=f"{len(rows)} waiting", extra="rail",
    )


# --- activity page -----------------------------------------------------------------


_ATTENTION = {
    "stuck": "bad", "overdue": "bad", "dead": "bad", "needs-attention": "bad",
    "stale": "warn", "paused": "warn", "down": "bad", "no-waiter": "bad", "health": "bad",
}
_HEALTH_LINK = re.compile(r"health\.html#[A-Za-z0-9_-]+")


def _health_link(href: object) -> str:
    """Only a link into the Health tab (#92); anything else is the tab itself."""
    return str(href) if isinstance(href, str) and _HEALTH_LINK.fullmatch(href) else "health.html"


def _attention(snapshot: dict) -> str:
    attention = snapshot["attention"]

    def draw(row: dict) -> str:
        who = (
            f'<a class="ticket" href="{_e(_health_link(row.get("link")))}">'
            f'{_e(row["component"])}</a>'
            if row.get("component")
            else _span("ticket", _ticket(row))
        )
        return (
            _chip(row["kind"], _ATTENTION.get(row["kind"], "muted"))
            + who
            + _span("what", row["detail"])
        )

    count = attention["count"]
    return _panel(
        "attention", "Needs attention", _feed(attention["items"], "all clear", draw),
        count=f"{count} item" + ("" if count == 1 else "s"),
    )


_HANDOFF = {"scripted": ("scripted", "ok"), "bot_to_bot": ("bot-to-bot", "accent"),
            "failed": ("failed", "bad"), "done": ("done", "muted")}


def _handoffs(snapshot: dict) -> str:
    def draw(row: dict) -> str:
        label, cls = _HANDOFF.get(row["kind"], (row["kind"], "muted"))
        return (
            _chip(label, cls) + _bot(row["recipient"]) + _span("ticket", _ticket(row))
            + _span("what", row["action"]) + _when(row["age_seconds"], suffix=" ago")
        )

    rows = snapshot["handoff_savings"]["recent"]
    return _panel("handoffs", "Recent handoffs", _feed(rows, "no finished jobs yet", draw))


_VERDICT = {"routed": "accent", "shipped": "ok", "stale": "muted", "rejected": "warn",
            "stuck": "bad", "blocked": "bad", "waiting": "warn", "other": "muted"}


def _verdicts(snapshot: dict) -> str:
    rows = snapshot["poller"]["verdicts"]
    if rows is None:
        inner = '<p class="empty">no handled comments recorded yet</p>'
    else:
        inner = _feed(
            rows,
            "no verdicts yet",
            lambda row: _chip(row["kind"], _VERDICT.get(row["kind"], "muted"))
            + _span("ticket", _ticket(row)) + _span("what", row["outcome"]),
        )
    return _panel("verdicts", "Recent verdicts", inner)


def _td(value: object, cls: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<td{attr}>{_e(value)}</td>"


def _tickets(snapshot: dict) -> str:
    rows = snapshot["tickets"]
    if not rows:
        inner = '<p class="empty">no tracked tickets</p>'
    else:
        head = "".join(
            f"<th>{name}</th>"
            for name in ("ticket", "tracker", "state", "round", "gates", "stuck")
        )
        body = "".join(
            "<tr>"
            + _td(_ticket(row, full=True))
            + _td(row["tracker"])
            + _td(row["state"], "bad" if row["stuck"] else None)
            + _td(row["round"], "num")
            + _td(row["gate_count"], "num")
            + _td("yes" if row["stuck"] else "no", "bad" if row["stuck"] else None)
            + "</tr>"
            for row in rows
        )
        inner = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    return _panel("tickets", "Tickets", inner, count=f"{len(rows)} tracked")


def _log(snapshot: dict) -> str:
    log = snapshot["poller"]["log"]
    count = (
        f"written {duration(log['age_seconds'])} before the snapshot"
        if log["present"]
        else "absent"
    )
    inner = (
        f'<p class="muted">last {len(log["tail"])} lines, redacted</p>'
        "<pre>" + "\n".join(_e(line) for line in log["tail"]) + "</pre>"
    )
    return _panel("log", "poller.log", inner, count=count)


# --- efficiency page ----------------------------------------------------------------


_POLL_STATE = {"ok": ("OK", "ok"), "late": ("LATE", "warn"), "stale": ("STALE", "bad"),
               "unknown": ("UNKNOWN", "muted")}
_BUDGET_STATE = {"ok": ("OK", "ok"), "deferring": ("DEFERRING", "warn"),
                 "exhausted": ("EXHAUSTED", "bad"), "rate_limited": ("RATE LIMITED", "bad")}


def _health(snapshot: dict) -> str:
    poller = snapshot["poller"]
    health = poller["health"] or {"state": "unknown"}
    budget = poller["linear_budget"] or {"state": "ok"}
    label, cls = _POLL_STATE.get(health["state"], _POLL_STATE["unknown"])
    cycle = health.get("last_cycle_age_seconds")
    cycle_text = (
        _span("muted", "cycle time unknown (live push off)")
        if cycle is None
        else _span(None, f"last cycle {duration(cycle)} ago")
    )
    blabel, bcls = _BUDGET_STATE.get(budget["state"], ("OTHER", "muted"))
    errors = poller["errors"]
    limit_hits = (budget.get("exhausted_last_hour") or 0) + (budget.get("rate_limited_last_hour") or 0)
    stats = [
        (errors["last_hour"], "poll errors, 1 h", "bad" if errors["last_hour"] else ""),
        (errors["last_day"], "poll errors, 24 h", ""),
        (budget.get("deferrals_last_hour"), "Linear deferrals, 1 h",
         "warn" if budget.get("deferrals_last_hour") else ""),
        (limit_hits, "Linear limit hits, 1 h", "bad" if limit_hits else ""),
    ]
    stat_html = "".join(
        f'<div class="stat">{_b(v, c)}{_span(None, text)}</div>'
        for v, text, c in stats
    )
    recent = errors["recent"][:3]
    recent_html = (
        _feed(
            recent,
            "",
            lambda row: _span(
                "ticket",
                row["repo"] if row["repo"] == "poll" or row["ticket_id"] == "-" else _ticket(row),
            )
            + _span("what", row["error"])
            + _when(row["age_seconds"], suffix=" ago"),
        )
        if recent
        else ""
    )
    inner = (
        f'<div class="row">{_span("label", "poll")}{_chip(label, cls)}{cycle_text}</div>'
        f'<div class="row">{_span("label", "linear")}{_chip(blabel, bcls)}'
        + _span(
            "muted",
            f"{_num(budget.get('requests_per_hour'))} req/h · "
            f"{_num(budget.get('complexity_per_hour'))} complexity/h",
        )
        + "</div>"
        f'<div class="stats">{stat_html}</div>'
        f'<p class="muted">last poller activity {duration(poller["last_activity_age_seconds"])} ago</p>'
        + recent_html
    )
    return _panel("health", "Poll and Linear budget", inner)


# --- health page (#92) ---------------------------------------------------------------


_HEALTH_STATE = {"up": ("UP", "ok"), "stale": ("STALE", "warn"), "down": ("DOWN", "bad")}
_ANCHOR = re.compile(r"[A-Za-z0-9_-]+")


def _anchor_attr(value: object) -> str:
    return f' id="{value}"' if isinstance(value, str) and _ANCHOR.fullmatch(value) else ""


def _components(snapshot: dict) -> str:
    health = snapshot.get("health") or {"known": False, "components": []}
    rows = health.get("components") or []
    count = f"{len(rows)} components · {health.get('down') or 0} down"
    if not health.get("known"):
        text = f"unknown: {health['error']}" if health.get("error") else "no heartbeats recorded yet"
        return _panel("components", "Components", f'<p class="empty">{_e(text)}</p>', count=count)
    heads = ("component", "bot", "device", "state", "last seen (ET)", "ago", "pid", "version",
             "started (ET)")
    body = []
    for row in rows:
        label, cls = _HEALTH_STATE.get(row["state"], ("OTHER", "muted"))
        reason = _span("muted", " " + row["reason"]) if row.get("reason") else ""
        if row.get("configured") is False:
            reason += _span("muted", " (not configured)")
        body.append(
            f'<tr{_anchor_attr(row.get("anchor"))}>'
            + _td(row["component"])
            + _td(row["bot"] or None)
            + _td(row["device"])
            + f"<td>{_chip(label, cls)}{reason}</td>"
            + _td(row["last_seen_et"] or "never")
            + _td(duration(row["last_seen_age_seconds"]), "num")
            + _td(row["pid"], "num")
            + (f'<td>{_e(row["version"])}{_span("warn", " stale code")}</td>'
               if row.get("stale_code") else _td(row["version"]))
            + _td(row["started_at_et"])
            + "</tr>"
        )
    thresholds = health.get("thresholds") or {}
    note = (
        f"up: a beat within {thresholds.get('stale_beats', 3)} intervals "
        f"({_e(thresholds.get('beat_seconds', 15))} s each; device probes every "
        f"{_e(thresholds.get('probe_seconds', 60))} s) · stale: within "
        f"{thresholds.get('down_beats', 8)} · down: older, stopped or never seen"
    )
    inner = (
        "<table><thead><tr>"
        + "".join(f"<th>{_e(text)}</th>" for text in heads)
        + "</tr></thead><tbody>"
        + "".join(body)
        + f'</tbody></table><p class="muted">{_e(note)}</p>'
    )
    return _panel("components", "Components", inner, count=count)


def _no_waiter(snapshot: dict) -> str:
    health = snapshot.get("health") or {}
    rows = health.get("no_waiter") or []
    if not rows:
        text = (
            "every queued job has a live waiter"
            if health.get("known")
            else "unknown until a component heartbeats"
        )
        return _panel("no-waiter", "Queued jobs with no waiter", f'<p class="empty">{_e(text)}</p>',
                      count="none")
    items = "".join(
        f'<li{_anchor_attr(row.get("anchor"))}>'
        + _chip("no waiter", "bad")
        + _span("ticket", f"waiter {row['bot']}")
        + _span(
            "what",
            f"no waiter for {row['harness']}: {row['count']} queued job"
            + ("" if row["count"] == 1 else "s")
            + f" ({', '.join(row['jobs'])})",
        )
        + "</li>"
        for row in rows
    )
    return _panel("no-waiter", "Queued jobs with no waiter", f'<ul class="feed">{items}</ul>',
                  count=f"{len(rows)} harnesses")


def _savings(snapshot: dict) -> str:
    savings = snapshot["handoff_savings"]
    share = savings["scripted_share"]
    pct = None if share is None else round(share * 100)
    lines = "".join(
        f'<div class="row">{_b(v, c)}{_span("muted", text)}</div>'
        for v, text, c in (
            (savings["scripted"], "scripted handoffs", "accent"),
            (savings["scripted_in_flight"], "scripted in flight", ""),
            (savings["bot_to_bot"], "bot-to-bot (grok wakes)", ""),
        )
    )
    total = (savings["scripted"] or 0) + (savings["bot_to_bot"] or 0)
    meter = (
        f'<i class="scripted" style="width:{100 * savings["scripted"] / total:g}%"></i>'
        f'<i class="bots" style="width:{100 * savings["bot_to_bot"] / total:g}%"></i>'
        if total
        else ""
    )
    inner = (
        f'<div class="gauge"><div class="ring" style="--p:{0 if pct is None else pct}">'
        f'<span>{"—" if pct is None else f"{pct}%"}</span></div><div>{lines}</div></div>'
        f'<div class="meter">{meter}</div>'
        f'<p class="definition" id="share-definition">{_e(savings["scripted_share_definition"])}</p>'
        "<details><summary>how each count is made</summary>"
        f'<p class="muted">{_e(savings["definition"])}</p></details>'
    )
    return _panel("savings", "Handoff savings", inner)


# --- job page ---------------------------------------------------------------------


def _active_jobs(snapshot: dict) -> list[tuple[dict, str]]:
    return [(row, "running") for row in snapshot["running"]] + [
        (row, "queued") for row in snapshot["queued"]
    ]


def _jobs(snapshot: dict) -> str:
    def draw(item: tuple[dict, str]) -> str:
        row, state = item
        return (
            f'<a class="queue-item" href="#{anchor(row["job"])}">'
            f'{_bot(row["recipient"])}{_span("ticket", _job_label(row))}'
            f'{_span("what", row["action"])}{_chip(state, "accent" if state == "running" else "warn")}</a>'
        )

    count = f"{len(snapshot['running'])} running · {len(snapshot['queued'])} queued"
    return _panel(
        "jobs", "Jobs", _feed(_active_jobs(snapshot), "no running or queued jobs", draw),
        count=count,
    )


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


_PANELS = {
    "index.html": (_kpis, _lanes, _queue),
    "activity.html": (_attention, _handoffs, _verdicts, _tickets, _log),
    "efficiency.html": (_health, _savings, rollup_panel),
    "health.html": (_components, _no_waiter),
    "job.html": (_jobs, _jobview),
}


def _page(snapshot: dict, name: str) -> str:
    page, label = _PAGE_NAMES[name]
    refresh = int(snapshot["refresh_seconds"])
    panels = "".join(draw(snapshot) for draw in _PANELS[name])
    # The job page and the lanes page's job modal copy the full chart from here.
    extra = _flow_template(snapshot) if name in ("job.html", "index.html") else ""
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            # Without script the page reloads itself; with script it polls
            # status.json and never reloads.
            f'<noscript><meta http-equiv="refresh" content="{refresh}"></noscript>',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta name="robots" content="noindex">',
            '<meta name="color-scheme" content="dark">',
            f"<title>factory mission control · {label.lower()}</title>",
            f"<style>{CSS}{TELEMETRY_CSS}</style></head>",
            f'<body data-page="{page}" '
            f'data-generated="{float(snapshot["generated_at_epoch"]):.3f}" '
            f'data-refresh="{refresh}">',
            _topbar(snapshot, name),
            f'<main id="hud">{panels}</main>',
            _statusline(snapshot),
            extra,
            f"<script>{page_script()}</script>",
            "</body></html>",
        ]
    ) + "\n"


def render_pages(snapshot: dict) -> dict[str, str]:
    """Every page, by file name. Every value is escaped and already redacted."""
    return {name: _page(snapshot, name) for name in PAGES}
