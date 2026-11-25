from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from lib.dashboard import collect_snapshot
from lib.dashboard_page import render_static_assets
from lib.max_actions import ACTIONS, Wait, applicable
from tests import test_dashboard_layout as layout
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_http import (  # noqa: F401
    MAX_TOKEN,
    actions_site,
    fake_github,
    serve_actions,
    site,
)

REPO = "maxharris1/factory"
HEAD = "a" * 40
VISUAL = {"repo": REPO, "ticket_id": "42", "pr": "42", "source": "#42", "state": "awaiting-ok",
          "wait": {"why": "visual-qa-review", "comment_id": "55", "detail": None},
          "actions": applicable(Wait("visual-qa-review", "55"), hand_off=True)}
QUESTION = {"repo": REPO, "ticket_id": "43", "pr": None, "source": "#43", "state": "waiting-max",
            "wait": {"why": "question", "comment_id": "77", "detail": None},
            "actions": applicable(Wait("question", "77"), hand_off=True)}
HEAD_BOUND = [name for name in VISUAL["actions"] if ACTIONS[name].head_bound]

DRIVER = r"""
Object.defineProperty(Node.prototype, 'value', {configurable: true,
  get() { return this._value === undefined ? '' : this._value; }, set(v) { this._value = String(v); }});
const scenario = JSON.parse(actionsText);
let current = status;
let generation = 0;
const posts = [];
const reply = (code, data) => Promise.resolve({ok: code < 300, status: code,
  json: () => Promise.resolve(JSON.parse(JSON.stringify(data)))});
global.fetch = (url, opts) => {
  if (url === 'status.json') return reply(200, current);
  if (url === 'tickets.json') return reply(200, JSON.parse(ticketsText));
  if (url === '/api/session') return reply(200, {signed_in: true, login: 'maxharris1', csrf: 'x'});
  posts.push({url, body: JSON.parse(opts.body)});
  const [code, data] = scenario.replies[url];
  return reply(code, data);
};
const realSetTimeout = setTimeout;
let pendingPoll = null;
global.setTimeout = (fn, ms) => {
  if (fn.name === 'poll') { pendingPoll = fn; return 0; }
  return realSetTimeout(fn, ms);
};
global.location = {hash, reload() {}, replace() {}};
Date.now = () => 1800000003000;
global.setInterval = () => 0;
const settle = () => new Promise(resolve => realSetTimeout(resolve, 20));
function shown(node) {
  for (let n = node; n; n = n.parentNode) if (n.hidden) return false;
  return true;
}
function within(spec) {
  const root = pick({attr: spec});
  if (!root) throw new Error('no row ' + JSON.stringify(spec));
  return root;
}
eval(script);
(async () => {
  await settle();
  for (const [kind, row, arg] of scenario.steps) {
    const root = kind === 'poll' ? null : within(row);
    if (kind === 'open') {
      const button = find(root, n => n.tagName === 'button' && Object.entries(arg).every(([k, v]) => n.attrs[k] === v));
      if (!button) throw new Error('no button ' + JSON.stringify(arg));
      fire(button, 'click');
    } else if (kind === 'type') {
      const text = find(root, n => n.tagName === 'textarea' && shown(n));
      text.value = arg;
      fire(text, 'input');
    } else if (kind === 'send') {
      const button = find(root, n => n.tagName === 'button' && n.parentNode.className === arg && shown(n));
      if (button.disabled) throw new Error('send is disabled: ' + button.textContent);
      fire(button, 'click');
    } else if (kind === 'poll') {
      current = Object.assign({}, status, row, {generated_at_epoch: status.generated_at_epoch + ++generation});
      pendingPoll();
    }
    await settle();
  }
  process.stdout.write(JSON.stringify({posts}), () => process.exit(0));
})().catch(err => { console.error(err.stack); process.exit(1); });
"""
HARNESS = layout.HARNESS[:layout.HARNESS.index("const steps = [];")] + DRIVER


def _snapshot(tmp_path: Path, rows: list[dict]) -> dict:
    db, log = seeded(tmp_path / "db")
    snap = copy.deepcopy(collect_snapshot(db, log, now=NOW))
    return _with(snap, rows)


def _with(snap: dict, rows: list[dict]) -> dict:
    return {**snap, "tickets": rows, "attention": {"count": len(rows), "items": [
        {**row, "kind": "paused", "detail": f"{row['state']} detail"} for row in rows]}}


class Surface(NamedTuple):
    page: str
    ids: list[str]
    open: str
    send: str
    row: Callable[[dict], dict[str, str]]


PAGES = {
    "activity": Surface("activity", ["attention"], "data-act", "at-form",
                        lambda row: {"data-attn": f"paused:{row['repo']}#{row['ticket_id']}"}),
    "cockpit": Surface("main", ["needs-max"], "data-ck-act", "ck-form-row",
                       lambda row: {"data-cockpit-ticket": f"{row['repo']}#{row['ticket_id']}"}),
}


def _drive(tmp_path: Path, surface: str, snap: dict, steps: list, replies: dict) -> list[dict]:
    page = PAGES[surface]
    script = render_static_assets(snap)["app.js"]
    scenario = {"steps": steps, "replies": replies}
    return layout.run_harness(tmp_path, HARNESS, script, snap, page.page, page.ids, scenario,
                              tickets="null")["posts"]


def _act_status(site_dir: Path, snap: dict) -> None:
    (site_dir / "status.json").write_text(json.dumps({"schema": snap["schema"], "tickets": snap["tickets"]}),
                                          encoding="utf-8")


@layout.needs_node
@pytest.mark.parametrize("surface", list(PAGES))
@pytest.mark.parametrize("action", HEAD_BOUND)
def test_every_head_bound_action_posts_the_head_it_read_and_the_server_takes_it(
    tmp_path: Path, surface: str, action: str, serve_actions, fake_github, actions_site: Path,  # noqa: F811
) -> None:
    page = PAGES[surface]
    snap = _snapshot(tmp_path, [VISUAL])
    row = page.row(VISUAL)
    posts = _drive(tmp_path / "run", surface, snap, [
        ["open", row, {page.open: action}],
        ["type", row, "Looks right on this head."],
        ["send", row, page.send],
    ], {"/api/head": [200, {"head": HEAD}], "/api/act": [201, {"url": f"https://github.com/{REPO}/pull/42"}]})
    assert [post["url"] for post in posts] == ["/api/head", "/api/act"]
    body = posts[1]["body"]
    assert (body["action"], body["head"]) == (action, HEAD)

    _act_status(actions_site, snap)
    fake_github.heads[(REPO, 42)] = HEAD
    client = serve_actions(audit=tmp_path / "audit.jsonl")
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    assert client.post("/api/head", posts[0]["body"]) == (200, {"head": HEAD})
    assert client.post("/api/act", body)[0] == 201


@layout.needs_node
@pytest.mark.parametrize("surface", list(PAGES))
def test_an_answer_stays_bound_to_the_question_it_was_opened_for(
    tmp_path: Path, surface: str, serve_actions, fake_github, actions_site: Path,  # noqa: F811
) -> None:
    page = PAGES[surface]
    snap = _snapshot(tmp_path, [QUESTION])
    later = {**QUESTION, "wait": {**QUESTION["wait"], "comment_id": "78"}}
    row = page.row(QUESTION)
    posts = _drive(tmp_path / "run", surface, snap, [
        ["open", row, {page.open: "answer"}],
        ["type", row, "Yes, use OAuth"],
        ["poll", _with(snap, [later])],
        ["send", row, page.send],
    ], {"/api/act": [409, {"error": "already-answered"}]})
    assert [post["url"] for post in posts] == ["/api/act"]
    body = posts[0]["body"]
    assert (body["words"], body["expect_comment_id"]) == ("Yes, use OAuth", "77")

    _act_status(actions_site, _with(snap, [later]))
    client = serve_actions(audit=tmp_path / "audit.jsonl")
    assert client.sign_in(fake_github, MAX_TOKEN) == "signed-in"
    assert client.post("/api/act", body) == (409, {"error": "already-answered"})
