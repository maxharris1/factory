"""#822: the Activity page's Needs attention list says why each ticket waits
on Max and links the comment that explains it, to everyone. Signed in, each
row also offers one button per action the snapshot lists for it."""

from __future__ import annotations

import contextlib
import copy
import functools
import http.server
import itertools
import json
import re
import threading
import time
from pathlib import Path

import pytest

from lib.dashboard import collect_snapshot
from lib.dashboard_page import render_pages
from tests import test_dashboard_activity as activity
from tests import test_dashboard_layout as layout
from tests.test_dashboard import NOW, seeded
from tests.test_dashboard_system import CHROME, QuietHandler, _Chrome

REPO = "maxharris1/factory"
LABEL = {"ok": "Merge OK", "approve-plan": "Approve plan as-is", "split": "Split", "another-round": "Another round",
         "answer": "Answer", "retry": "Requeue", "resume": "Resume", "close": "Close", "hand-off": "Hand off"}
# why, kind, state, ticket, source, pr, comment id, detail, actions as the snapshot lists them
WAITS = (
    ("awaiting-ok", "paused", "awaiting-ok", "31", "#30", "31", "1001", None, ["ok", "close", "hand-off"]),
    ("plan-size", "paused", "waiting-max", "32", "#32", None, "1002", None, ["close", "approve-plan", "split"]),
    ("question", "paused", "waiting-max", "33", None, "33", "1003", None, ["answer", "close", "hand-off"]),
    ("repeat-stop", "paused", "waiting-max", "34", None, "34", "1004", None, ["close", "hand-off"]),
    ("unsat", "paused", "waiting-max", "35", None, "35", "1005", None, ["another-round", "close", "hand-off"]),
    ("capped", "paused", "capped", "36", None, "36", "1006", None, ["another-round", "close", "hand-off"]),
    ("blocked", "paused", "blocked", "37", None, "37", None, None, ["resume", "close", "hand-off"]),
    ("stuck", "stuck", "stuck", "38", None, "38", "1008", "job-timeout", ["retry", "close", "hand-off"]),
    ("waiting-external", "paused", "waiting-external", "39", None, None, "1009", None, ["close", "hand-off"]),
)
SAYS = {
    "awaiting-ok": "waits for your merge OK",
    "plan-size": "plan is over the size limit",
    "question": "asks you a question",
    "repeat-stop": "review repeated an earlier finding",
    "unsat": "review calls a criterion unsatisfiable",
    "capped": "hit the review round cap",
    "blocked": "blocked",
    "stuck": "stuck: job-timeout",
    "waiting-external": "waits on someone outside the factory",
}
COMMENT = {
    "awaiting-ok": f"https://github.com/{REPO}/issues/31#issuecomment-1001",
    "plan-size": f"https://github.com/{REPO}/issues/32#issuecomment-1002",
    "question": f"https://github.com/{REPO}/issues/33#issuecomment-1003",
    "repeat-stop": f"https://github.com/{REPO}/issues/34#issuecomment-1004",
    "unsat": f"https://github.com/{REPO}/issues/35#issuecomment-1005",
    "capped": f"https://github.com/{REPO}/issues/36#issuecomment-1006",
    "stuck": f"https://github.com/{REPO}/issues/38#issuecomment-1008",
}
TABLE_ORDER = list(LABEL)


def _key(kind: str, ticket: str) -> str:
    return f"{kind}:{REPO}#{ticket}"


KEYS = {why: _key(kind, ticket) for why, kind, _, ticket, *_ in WAITS}


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> dict:
    db, log = seeded(tmp_path_factory.mktemp("attention"))
    snap = copy.deepcopy(collect_snapshot(db, log, now=NOW))
    snap["attention"] = {"count": len(WAITS), "items": [
        {"kind": kind, "repo": REPO, "ticket_id": ticket, "source": source, "pr": pr,
         "detail": f"{state} detail", "state": state,
         "wait": {"why": why, "comment_id": comment_id, "detail": detail}, "actions": actions}
        for why, kind, state, ticket, source, pr, comment_id, detail, actions in WAITS
    ]}
    return snap


def _rows(markup: str) -> dict[str, str]:
    return dict(re.findall(r'<div class="at-row" data-attn="([^"]+)">(.*?)</div>', markup))


def test_each_row_says_why_it_waits_and_links_the_comment_with_no_controls(snapshot: dict) -> None:
    attention = activity._server(snapshot)["attention"]
    rows = _rows(attention)
    for why, key in KEYS.items():
        link = COMMENT.get(why)
        anchor = f'<a href="{link}" rel="noopener noreferrer" target="_blank">comment ↗</a>' if link else ""
        assert f'<span class="at-why"><span>{SAYS[why]}</span>{anchor}</span>' in rows[key], why
    assert "issuecomment" not in rows[KEYS["blocked"]] + rows[KEYS["waiting-external"]]
    assert not re.search(r"<(button|textarea|form)\b", attention)


@layout.needs_node
def test_signed_out_the_script_draws_the_rows_like_the_server(tmp_path: Path, snapshot: dict) -> None:
    assert activity._script(tmp_path, snapshot)["html"]["attention"] == activity._server(snapshot)["attention"]


STUB = r"""
(() => {
  const SESSION = __SESSION__, REPLIES = __REPLIES__;
  const real = window.fetch.bind(window);
  window.posts = [];
  window.sessionAsked = false;
  window.fetch = (url, opts) => {
    opts = opts || {};
    if (url === '/api/session') {
      window.sessionAsked = true;
      return SESSION ? Promise.resolve(new Response(JSON.stringify(SESSION), {status: 200})) : real(url, opts);
    }
    if (url in REPLIES) {
      window.posts.push({url, body: JSON.parse(opts.body), csrf: opts.headers['X-Factory-CSRF']});
      const reply = REPLIES[url];
      return Promise.resolve(new Response(JSON.stringify(reply.body), {status: reply.status}));
    }
    return real(url, opts);
  };
})();
"""
SIGNED_IN = {"signed_in": True, "login": "maxharris1", "csrf": "x"}
needs_chrome = pytest.mark.skipif(CHROME is None, reason="Chrome is not installed")


@contextlib.contextmanager
def _browse(tmp_path: Path, snapshot: dict, session: dict | None, replies: dict | None = None):
    site = tmp_path / "site"
    site.mkdir()
    (site / "activity.html").write_text(render_pages(snapshot)["activity.html"], encoding="utf-8")
    polls = itertools.count()

    class Publishing(QuietHandler):
        """Each poll reads a newly published status.json, so the page redraws."""

        def do_GET(self) -> None:
            if not self.path.startswith("/status.json"):
                super().do_GET()
                return
            body = json.dumps({**snapshot, "generated_at_epoch": snapshot["generated_at_epoch"] + next(polls)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Publishing, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    chrome = _Chrome(tmp_path / "profile")
    try:
        stub = STUB.replace("__SESSION__", json.dumps(session)).replace("__REPLIES__", json.dumps(replies or {}))
        chrome.send("Page.enable", chrome.session)
        chrome.send("Page.addScriptToEvaluateOnNewDocument", chrome.session, source=stub)
        chrome.send("Page.navigate", chrome.session,
                    url=f"http://127.0.0.1:{server.server_address[1]}/activity.html")
        chrome.wait_for(f"window.sessionAsked && document.querySelectorAll('#attention .at-row').length >= {len(WAITS)}",
                        "the attention rows")
        _rerendered(chrome)
        yield chrome
    finally:
        chrome.close()
        server.shutdown()


def _row(key: str) -> str:
    return f"document.querySelector('#attention [data-attn=\"{key}\"]')"


def _rerendered(chrome: _Chrome) -> None:
    """Wait for a status.json poll to redraw the panel."""
    chrome.run("document.querySelectorAll('#attention .at-row').forEach(r => { r.seen = true; })")
    chrome.wait_for("!document.querySelector('#attention .at-row').seen", "a redraw")


CONTROLS = "document.querySelectorAll('#attention button, #attention textarea, #attention form, #attention input').length"


@needs_chrome
@pytest.mark.parametrize("session", [{"signed_in": False}, None], ids=["signed out", "no server"])
def test_signed_out_the_panel_has_no_action_control(tmp_path: Path, snapshot: dict, session: dict | None) -> None:
    with _browse(tmp_path, snapshot, session) as chrome:
        controls = chrome.run(CONTROLS)
        why = chrome.run(f"{_row(KEYS['question'])}.querySelector('.at-why').textContent")
    assert controls == 0
    assert why == "asks you a question" + "comment ↗"


@needs_chrome
def test_signed_in_each_row_offers_its_listed_actions_in_table_order(tmp_path: Path, snapshot: dict) -> None:
    with _browse(tmp_path, snapshot, SIGNED_IN) as chrome:
        shown = chrome.run(
            "Object.fromEntries([...document.querySelectorAll('#attention .at-row')].map(r => [r.dataset.attn,"
            " [...r.querySelectorAll('button[data-act]')].map(b => [b.dataset.act, b.textContent])]))"
        )
        hidden_forms = chrome.run("[...document.querySelectorAll('#attention textarea')].every(t => !t.checkVisibility())")
    for why, *_, actions in WAITS:
        listed = sorted(actions, key=TABLE_ORDER.index)
        assert shown[KEYS[why]] == [[name, LABEL[name]] for name in listed], why
    assert hidden_forms


def _open(chrome: _Chrome, key: str, action: str) -> None:
    chrome.run(f"{_row(key)}.querySelector('[data-act=\"{action}\"]').click()")
    chrome.wait_for(f"{_row(key)}.querySelector('.at-form textarea').checkVisibility()", f"the {action} form")


def _send(key: str) -> str:
    return f"{_row(key)}.querySelector('.at-form button')"


def _type(chrome: _Chrome, key: str, words: str) -> None:
    chrome.run(f"{_row(key)}.querySelector('.at-form textarea').focus()")
    chrome.send("Input.insertText", chrome.session, text=words)


@needs_chrome
def test_answer_posts_the_words_and_the_expected_state_and_shows_the_comment(tmp_path: Path, snapshot: dict) -> None:
    url = f"https://github.com/{REPO}/issues/33#issuecomment-2001"
    key = KEYS["question"]
    with _browse(tmp_path, snapshot, SIGNED_IN, {"/api/act": {"status": 201, "body": {"url": url}}}) as chrome:
        _open(chrome, key, "answer")
        empty = chrome.run(f"[{_send(key)}.disabled, {_send(key)}.textContent]")
        _type(chrome, key, "Use the second option.")
        _rerendered(chrome)
        kept = chrome.run(f"[{_row(key)}.querySelector('textarea').value,"
                          f" document.activeElement === {_row(key)}.querySelector('textarea'), {_send(key)}.disabled]")
        chrome.run(f"{_send(key)}.click()")
        chrome.wait_for(f"/sent/.test({_row(key)}.querySelector('.at-out').textContent)", "sent")
        out = chrome.run(f"(o => [o.textContent, o.querySelector('a').href])({_row(key)}.querySelector('.at-out'))")
        posts = chrome.run("window.posts")
    assert empty == [True, "Send Answer"]
    assert kept == ["Use the second option.", True, False]
    assert posts == [{"url": "/api/act", "csrf": "x", "body": {
        "repo": REPO, "ticket_id": "33", "action": "answer", "expect_state": "waiting-max",
        "expect_why": "question", "words": "Use the second option."}}]
    assert out == [f"sent {url}", url]


@needs_chrome
def test_ok_sends_the_head_it_shows_and_a_changed_head_keeps_the_words(tmp_path: Path, snapshot: dict) -> None:
    old, new = "a" * 40, "b" * 40
    replies = {"/api/head": {"status": 200, "body": {"head": old}},
               "/api/act": {"status": 409, "body": {"error": "head-changed", "head": new}}}
    key = KEYS["awaiting-ok"]
    with _browse(tmp_path, snapshot, SIGNED_IN, replies) as chrome:
        _open(chrome, key, "ok")
        chrome.wait_for(f"{_send(key)}.textContent === 'OK {old}'", "the head")
        shown = chrome.run(f"{_row(key)}.innerText")
        _type(chrome, key, "Ship it.")
        chrome.run(f"{_send(key)}.click()")
        chrome.wait_for(f"/{new}/.test({_row(key)}.querySelector('.at-out').textContent)", "the new head")
        _rerendered(chrome)
        after = chrome.run(f"[{_row(key)}.querySelector('.at-out').textContent,"
                           f" {_row(key)}.querySelector('textarea').value, {_send(key)}.textContent]")
        posts = chrome.run("window.posts")
    assert old in shown
    assert posts == [
        {"url": "/api/head", "csrf": "x", "body": {"repo": REPO, "pr": 31}},
        {"url": "/api/act", "csrf": "x", "body": {
            "repo": REPO, "ticket_id": "31", "action": "ok", "expect_state": "awaiting-ok",
            "expect_why": "awaiting-ok", "words": "Ship it.", "head": old}},
    ]
    assert after == [f"error: head-changed, the PR head is now {new}", "Ship it.", f"OK {new}"]
