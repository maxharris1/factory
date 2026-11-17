import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.dashboard import RECORDS, render_pages, tickets_json  # noqa: E402
from tests.test_dashboard_lanes import Browser  # noqa: E402
from tests.test_dashboard_layout import _snapshot  # noqa: E402
from tests.test_dashboard_ticket_detail import NOW, RECORD, _quote, _visit  # noqa: E402

PROBE = """(function () {
  var art = document.querySelector('article.ticketpage');
  return {ticket: art && art.getAttribute('data-ticket'), h1: [].map.call(document.querySelectorAll('h1'), function (h) { return h.textContent; }),
          sections: [].map.call(document.querySelectorAll('.ticketpage .jd-panel h2 .t, .ticketpage .jd-chart summary'), function (n) { return n.firstChild.textContent.trim(); }),
          scrollWidth: document.documentElement.scrollWidth};
})()"""


STOPPED_AT = NOW - 266400


def _with_stopped(snapshot: dict) -> dict:
    record = snapshot[RECORDS][RECORD["stopped"]]
    record["visits"] = [_visit("intake", "script", NOW - 280000, 3600, None),
                        _visit("questions", "claude", NOW - 276400, 4000, "brief posted"),
                        _visit("planning", "claude", NOW - 272400, 6000, "answers posted"),
                        _visit("stuck", "script", STOPPED_AT, None, "job dies twice (T6)", current=True)]
    record.update(hops=3, state_age_seconds=266400)
    record["stopped"] = {"state": "stuck", "at": STOPPED_AT, "stopped_in": "planning",
                         "reason": "The plan job died twice on the same step (T6): wall-clock budget spent twice.",
                         "ways_out": [{"to": "planning", "label": "authorized unstick"},
                                      {"to": "abandoned", "label": "Max drops it"}]}
    return snapshot


def _page_with_stubs(snapshot: dict) -> str:
    shim = (
        "<script>window.fetch = function (url) { var body = url === 'tickets.json' ? "
        f"{tickets_json(snapshot).strip()} : {json.dumps(snapshot)};"
        " return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve(body); }}); };</script>"
    )
    return render_pages(snapshot)["ticket.html"].replace("<script>", shim + "<script>", 1)


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        snapshot = _with_stopped(_snapshot(Path(tmp)))
        html = _page_with_stubs(snapshot)
    chrome = Browser()
    try:
        address = RECORD["stopped"]
        for width in (1440, 375):
            chrome.open(html, width)
            chrome.eval(f"""new Promise(function (done) {{
              location.hash = {json.dumps("t=" + _quote(address))};
              (function wait() {{ if (document.querySelector('article.ticketpage canvas.jd-journey')) setTimeout(done, 5000, true); else setTimeout(wait, 50); }})();
            }})""")
            print(width, json.dumps(chrome.eval(PROBE)))
            out = ROOT / "docs" / "screenshots" / f"issue-669-ticket-stopped-{width}.png"
            chrome.screenshot(out, width)
            print(out)
    finally:
        chrome.close()


if __name__ == "__main__":
    main()
