
var FM_TINT = __TINT__;
var FM_RED = '__RED__';
var FM_FONT = "'JetBrains Mono', ui-monospace, Menlo, monospace";

function fmHex(s) {
  var m = /^#?([0-9a-f]{6})/i.exec(String(s || ''));
  if (!m) return [224, 175, 104];
  return [parseInt(m[1].substr(0, 2), 16), parseInt(m[1].substr(2, 2), 16), parseInt(m[1].substr(4, 2), 16)];
}

function fmMix(rgb, k) {
  return fmRgb(14 + (rgb[0] - 14) * k, 14 + (rgb[1] - 14) * k, 20 + (rgb[2] - 20) * k);
}

function fmRgb(r, g, b) {
  return 'rgb(' + Math.round(r) + ',' + Math.round(g) + ',' + Math.round(b) + ')';
}

function fmCells(pts) {
  var out = [], i, x = pts[0][0], y = pts[0][1], tx, ty;
  out.push([x, y]);
  for (i = 1; i < pts.length; i++) {
    tx = pts[i][0];
    ty = pts[i][1];
    while (x !== tx || y !== ty) {
      if (x !== tx) x += tx > x ? 1 : -1;
      else y += ty > y ? 1 : -1;
      out.push([x, y]);
    }
  }
  return out;
}

function fmHead(cells) {
  var n = cells.length;
  if (n < 2) return [];
  var lx = cells[n - 1][0], ly = cells[n - 1][1];
  var dx = lx - cells[n - 2][0], dy = ly - cells[n - 2][1];
  return [[lx - dx + dy, ly - dy + dx], [lx - dx - dy, ly - dy - dx]];
}

function fmPerim(n, NW, NH) {
  var out = [], j;
  for (j = 0; j < NW; j++) out.push([n.x + j, n.y]);
  for (j = 1; j < NH; j++) out.push([n.x + NW - 1, n.y + j]);
  for (j = NW - 2; j >= 0; j--) out.push([n.x + j, n.y + NH - 1]);
  for (j = NH - 2; j >= 1; j--) out.push([n.x, n.y + j]);
  return out;
}

function drawFlowMap(ctx, layout, model, opts) {
  var NW = layout.node.w, NH = layout.node.h, px = layout.grid.cell;
  var W = layout.grid.w * px, H = layout.grid.h * px;
  var put = function (x, y) { ctx.fillRect(x * px, y * px, px, px); };
  var byName = {}, i, j, k, n, c, d, w, name, v;
  var nodes = layout.nodes;
  for (i = 0; i < nodes.length; i++) {
    byName[nodes[i].name] = { def: nodes[i], visits: 0, current: false, next: false };
  }
  var edgeSet = {};
  for (i = 0; i < layout.edges.length; i++) edgeSet[layout.edges[i]] = true;

  var visits = (model && model.visits) || [], path = [], curWho = null;
  for (i = 0; i < visits.length; i++) {
    v = visits[i];
    if (!byName[v.state]) continue;
    path.push(v);
    byName[v.state].visits += 1;
    if (v.current) curWho = v.who;
  }
  var curName = model && model.state && byName[model.state] ? model.state : null;
  var cur = curName ? byName[curName] : null;
  if (cur) cur.current = true;
  var halted = !!(cur && cur.def.stop);
  var reduced = halted || !!(opts && opts.reduced);
  var t = reduced ? 0 : ((opts && opts.t) || 0);

  var wire = [], heads = [], hops = [], cols = [], taken = {}, a, b, id, cells, jump, hex, rgb, mid;
  for (i = 1; i < path.length; i++) {
    a = path[i - 1].state;
    b = path[i].state;
    if (a === b) continue;
    id = a + '>' + b;
    if (!layout.routes[id]) continue;
    jump = !edgeSet[id] || !!path[i].off_chart;
    taken[id] = true;
    cells = fmCells(layout.routes[id]);
    hex = byName[b].def.stop ? FM_RED : (FM_TINT[path[i].who] || FM_TINT.script);
    rgb = fmHex(hex);
    cols.push({
      lit: hex,
      mid: fmMix(rgb, 0.66),
      crest: fmRgb((rgb[0] + 255) / 2, (rgb[1] + 255) / 2, (rgb[2] + 255) / 2)
    });
    for (j = 0; j < cells.length; j++) {
      wire.push([cells[j][0], cells[j][1], jump ? 1 : 0, j, cols.length - 1]);
    }
    heads.push(fmHead(cells));
    mid = cells[Math.floor(cells.length / 2)];
    hops.push({ label: String(hops.length + 1), x: mid[0] * px - 5, y: mid[1] * px - 5, color: hex });
  }

  var nextWires = [], nexts = (model && model.next) || [];
  for (i = 0; i < nexts.length; i++) {
    if (!cur || !byName[nexts[i]]) continue;
    byName[nexts[i]].next = true;
    id = curName + '>' + nexts[i];
    if (nexts[i] === curName || taken[id] || !layout.routes[id]) continue;
    cells = fmCells(layout.routes[id]);
    nextWires.push({ cells: cells, head: fmHead(cells) });
  }

  ctx.fillStyle = '#0e0e14';
  ctx.fillRect(0, 0, W, H);

  ctx.fillStyle = '#24283b';
  for (i = layout.rail.x0; i < layout.rail.x1; i += 2) put(i, layout.rail.y);

  ctx.fillStyle = '#6b7399';
  for (i = 0; i < nextWires.length; i++) {
    w = nextWires[i].cells;
    for (j = 0; j < w.length; j++) {
      if (j % 3 !== 2) put(w[j][0], w[j][1]);
    }
    w = nextWires[i].head;
    for (j = 0; j < w.length; j++) put(w[j][0], w[j][1]);
  }

  var total = wire.length;
  var pulse = reduced ? -99 : (t * 30) % (total + 45);
  for (i = 0; i < total; i++) {
    c = wire[i];
    if (c[2] === 1 && c[3] % 3 === 2) continue;
    ctx.fillStyle = cols[c[4]].mid;
    put(c[0], c[1]);
  }
  if (!reduced) {
    for (i = 0; i < total; i++) {
      c = wire[i];
      d = pulse - i;
      if (d < 0 || d >= 14) continue;
      if (c[2] === 1 && c[3] % 3 === 2) continue;
      ctx.fillStyle = d < 4 ? cols[c[4]].crest : cols[c[4]].lit;
      put(c[0], c[1]);
    }
  }
  for (i = 0; i < heads.length; i++) {
    ctx.fillStyle = cols[i].lit;
    for (j = 0; j < heads[i].length; j++) put(heads[i][j][0], heads[i][j][1]);
  }

  var tintRgb = fmHex((opts && opts.tint) || FM_TINT[curWho] || FM_TINT.script);
  var tint = fmRgb(tintRgb[0], tintRgb[1], tintRgb[2]);
  var tintDim = fmMix(tintRgb, 0.3);
  var march = Math.floor(t * 14), band = Math.floor(t * 10), perim, edge, dashed;
  for (i = 0; i < nodes.length; i++) {
    n = byName[nodes[i].name];
    d = n.def;
    ctx.fillStyle = n.current ? '#292e42' : n.visits > 0 ? '#16222d' : '#13141c';
    ctx.fillRect((d.x + 1) * px, (d.y + 1) * px, (NW - 2) * px, (NH - 2) * px);
    perim = fmPerim(d, NW, NH);
    edge = n.current ? '#7dcfff' : n.visits > 0 ? '#5386a6' : n.next ? '#8b93b8' : '#343a55';
    dashed = !n.current && n.visits === 0 && n.next;
    ctx.fillStyle = edge;
    for (j = 0; j < perim.length; j++) {
      if (dashed && j % 2 === 1) continue;
      put(perim[j][0], perim[j][1]);
    }
    if (d.end) {
      put(d.x + 1, d.y + 1);
      put(d.x + NW - 2, d.y + 1);
      put(d.x + 1, d.y + NH - 2);
      put(d.x + NW - 2, d.y + NH - 2);
    }
    if (n.current) {
      if (!reduced) {
        ctx.fillStyle = '#b8e5ff';
        for (j = 0; j < 8; j++) {
          k = (((march - j) % perim.length) + perim.length) % perim.length;
          put(perim[k][0], perim[k][1]);
        }
      }
      for (j = 1; j < NW - 1; j++) {
        if (halted) {
          ctx.fillStyle = j % 2 === 0 ? FM_RED : '#542d39';
          put(d.x + j, d.y + NH - 3);
          ctx.fillStyle = j % 2 === 0 ? '#542d39' : FM_RED;
          put(d.x + j, d.y + NH - 2);
          continue;
        }
        k = (((j - band) % 12) + 12) % 12;
        ctx.fillStyle = k < 3 ? tint : tintDim;
        put(d.x + j, d.y + NH - 3);
        put(d.x + j, d.y + NH - 2);
      }
    }
  }

  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = '400 11px ' + FM_FONT;
  ctx.letterSpacing = '0.88px';
  ctx.fillStyle = '#8b93b8';
  for (i = 0; i < layout.heads.length; i++) {
    w = layout.heads[i];
    ctx.fillText(w.label.toUpperCase(), w.x + w.w / 2, 17);
  }
  ctx.letterSpacing = '0px';
  var left, top, height, cx, cy;
  for (i = 0; i < nodes.length; i++) {
    n = byName[nodes[i].name];
    d = n.def;
    left = d.x * px;
    top = d.y * px;
    height = n.current ? 26 : 36;
    cx = left + 68;
    cy = top + height / 2;
    ctx.font = '500 11px ' + FM_FONT;
    ctx.fillStyle = n.current ? '#7dcfff' : n.visits > 0 ? '#c0caf5' : n.next ? '#a9b1d6' : '#8b93b8';
    ctx.fillText(d.name, cx, d.gloss ? cy - 5.5 : cy);
    if (d.gloss) {
      ctx.font = (d.gloss.length > 23 ? '400 8px ' : '400 9px ') + FM_FONT;
      ctx.fillStyle = '#8b93b8';
      ctx.fillText(d.gloss, cx, cy + 6.5);
    }
  }
  ctx.font = '700 10px ' + FM_FONT;
  for (i = 0; i < nodes.length; i++) {
    n = byName[nodes[i].name];
    if (n.visits < 2) continue;
    d = n.def;
    name = '×' + n.visits;
    w = ctx.measureText(name).width + 8;
    left = (d.x + NW) * px - 26;
    top = d.y * px - 7;
    ctx.fillStyle = '#7dcfff';
    ctx.fillRect(left, top, w, 14);
    ctx.fillStyle = '#0c0e10';
    ctx.fillText(name, left + w / 2, top + 7);
  }
  ctx.font = '700 9px ' + FM_FONT;
  ctx.lineWidth = 1;
  for (i = 0; i < hops.length; i++) {
    ctx.fillStyle = '#0e0e14';
    ctx.fillRect(hops[i].x, hops[i].y, 14, 14);
    ctx.strokeStyle = hops[i].color;
    ctx.strokeRect(hops[i].x + 0.5, hops[i].y + 0.5, 13, 13);
    ctx.fillStyle = hops[i].color;
    ctx.fillText(hops[i].label, hops[i].x + 7, hops[i].y + 7);
  }
  ctx.textAlign = 'left';
  ctx.font = '400 11px ' + FM_FONT;
  ctx.letterSpacing = '0.88px';
  ctx.fillStyle = '#8b93b8';
  ctx.fillText('STOP STATES', layout.stop_label.x, layout.stop_label.y + 7);
  ctx.letterSpacing = '0px';
}
